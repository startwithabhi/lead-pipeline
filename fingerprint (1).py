"""
fingerprint.py — tech-stack detection + gap mapping for lead-pipeline.

Drop this into the repo next to your existing pipeline modules.
Dependencies: requests only.

Typical use:

    from fingerprint import fingerprint_many, score_leads

    results = fingerprint_many(["example.com", "another-store.com"])
    ranked  = score_leads(results)

    for lead in ranked:
        print(lead["domain"], lead["score"], lead["missing"])

Everything here uses plain HTTP. No paid APIs, no headless browser.
Results are cached on disk so re-runs are free.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

CACHE_PATH = os.environ.get("FINGERPRINT_CACHE", ".fingerprint_cache.json")
CACHE_TTL_SECONDS = 60 * 60 * 24 * 14  # 14 days

TIMEOUT = 10
MAX_BYTES = 1_500_000  # stop reading a page after ~1.5MB
MAX_WORKERS = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# --------------------------------------------------------------------------
# Signatures
# --------------------------------------------------------------------------
# Matching is plain substring on a lowercased blob of HTML + script srcs.
# Add to these as you find false negatives — that is the main tuning job.

VENDOR_SIGNATURES: dict[str, dict[str, list[str]]] = {
    "platform": {
        "shopify": ["cdn.shopify.com", "shopify.com/s/files", "shopifycdn", "window.shopify"],
        "woocommerce": ["woocommerce", "wp-content/plugins/woocommerce", "wc-ajax"],
        "bigcommerce": ["bigcommerce.com", "cdn11.bigcommerce"],
        "wix": ["wixstatic.com", "wix.com", "_wixcssimports"],
        "squarespace": ["squarespace.com", "static1.squarespace"],
        "magento": ["mage/cookies", "magento", "static/version"],
        "wordpress": ["wp-content", "wp-includes"],
    },
    "chatbot": {
        "tidio": ["tidio.co", "tidiochat"],
        "tawk": ["tawk.to", "embed.tawk"],
        "crisp": ["crisp.chat", "client.crisp"],
        "intercom": ["intercom.io", "widget.intercom"],
        "gorgias": ["gorgias.chat", "gorgias.com"],
        "freshchat": ["freshchat", "wchat.freshchat"],
        "zendesk": ["zendesk", "zdassets.com"],
        "manychat": ["manychat", "mctable"],
        "zoho": ["salesiq.zoho", "zohopublic"],
        "whatsapp_widget": ["wa.me/", "api.whatsapp.com/send"],
    },
    "order_tracking": {
        "aftership": ["aftership", "track.aftership"],
        "shiprocket": ["shiprocket", "srtracking"],
        "parcelpanel": ["parcelpanel"],
        "17track": ["17track"],
        "narvar": ["narvar"],
        "clickpost": ["clickpost.in", "clickpost.ai"],
        "trackingmore": ["trackingmore"],
        "wonderment": ["wonderment.com"],
    },
    "cart_recovery": {
        "klaviyo": ["klaviyo.com", "static.klaviyo"],
        "omnisend": ["omnisend", "omnisrc.com"],
        "recart": ["recart.com", "recart.io"],
        "privy": ["privy.com", "privymktg"],
        "mailchimp": ["mailchimp", "chimpstatic", "list-manage.com"],
        "wigzo": ["wigzo"],
        "carthook": ["carthook"],
        "pushowl": ["pushowl"],
    },
    "quick_checkout": {
        "gokwik": ["gokwik", "pdp.gokwik"],
        "razorpay_magic": ["razorpay magic", "magic-checkout", "checkout.razorpay"],
        "simpl": ["getsimpl", "simpl.js"],
        "snapmint": ["snapmint"],
        "shiprocket_checkout": ["shiprocket checkout", "checkout.shiprocket"],
        "shopify_one_page": ["one-page-checkout", "shop_pay", "shopifypay"],
        "fastrr": ["fastrr", "shiprocket.com/checkout"],
    },
    "tryon_sizing": {
        "kiwi_sizing": ["kiwisizing"],
        "size_chart": ["size-chart", "sizechart", "size_chart", "size guide"],
        "perfectcorp": ["perfectcorp", "ymk.perfectcorp"],
        "3dlook": ["3dlook", "mobile-tailor"],
        "vueai": ["vue.ai", "vueai"],
        "virtual_tryon": ["virtual try", "virtual-try", "try-on", "tryon"],
    },
    "fbt_upsell": {
        "rebuy": ["rebuyengine", "rebuy.com"],
        "frequently_bought": ["frequently-bought", "frequently bought together"],
        "bold_upsell": ["bold-upsell", "boldapps"],
        "zoorix": ["zoorix"],
        "wisepops": ["wisepops"],
        "selleasy": ["selleasy"],
    },
    "reviews": {
        "judgeme": ["judge.me", "judgeme"],
        "loox": ["loox.io", "loox.app"],
        "yotpo": ["yotpo", "staticw2.yotpo"],
        "stamped": ["stamped.io"],
        "okendo": ["okendo"],
    },
    "analytics_spend": {
        "gtm": ["googletagmanager.com"],
        "meta_pixel": ["connect.facebook.net", "fbevents.js"],
        "hotjar": ["hotjar"],
        "clarity": ["clarity.ms"],
        "tiktok_pixel": ["analytics.tiktok.com"],
    },
}

# Categories that map to a product you sell. Order matters for the pitch:
# highest value_score first when everything is missing.
PRODUCT_CATEGORIES: dict[str, dict[str, Any]] = {
    "quick_checkout": {
        "product": "Quick / one-page checkout",
        "value_score": 10,
        "pitch": "Their checkout is the stock multi-step flow. Direct revenue leak.",
    },
    "cart_recovery": {
        "product": "Abandoned cart recovery",
        "value_score": 9,
        "pitch": "No email/SMS recovery running. Carts are abandoned and never chased.",
    },
    "chatbot": {
        "product": "AI support chatbot",
        "value_score": 7,
        "pitch": "No live chat or chatbot. Pre-purchase questions go unanswered.",
    },
    "order_tracking": {
        "product": "AI order tracking",
        "value_score": 6,
        "pitch": "No tracking page. 'Where is my order' lands in their inbox instead.",
    },
    "fbt_upsell": {
        "product": "Frequently bought together / upsell",
        "value_score": 5,
        "pitch": "No cross-sell on the product page. AOV is leaving on the table.",
    },
    "tryon_sizing": {
        "product": "AI virtual try-on / sizing",
        "value_score": 4,
        "pitch": "No sizing help or try-on. Drives returns in apparel.",
    },
}

# --------------------------------------------------------------------------
# Regex helpers
# --------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
INSTAGRAM_RE = re.compile(r"instagram\.com/([a-zA-Z0-9_.]{2,30})")
# Only pull phones out of tel: links. A loose digit regex matches timestamps,
# product IDs and tracking numbers, which poisons the outreach columns.
PHONE_RE = re.compile(r"tel:([+\d][\d\s\-().]{6,})", re.I)
SCRIPT_SRC_RE = re.compile(r"<script[^>]+src=[\"']([^\"']+)[\"']", re.I)
LINK_HREF_RE = re.compile(r"href=[\"']([^\"'#?]+)", re.I)

# Emails that are never worth contacting
JUNK_EMAIL_PARTS = (
    "sentry.io", "example.com", "wixpress", "@2x", ".png", ".jpg", ".svg",
    "domain.com", "yourstore", "email.com", "sentry-next",
)

# Product URL patterns by platform
PRODUCT_PATH_HINTS = ("/products/", "/product/", "/shop/", "/collections/", "/p/", "/item/")


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass  # cache is a nicety, never fail the run over it


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def normalise_domain(raw: str) -> str:
    """'https://www.Example.com/collections/all?x=1' -> 'example.com'"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "//" not in raw:
        raw = "https://" + raw
    host = urlparse(raw).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _fetch(url: str, session: requests.Session) -> tuple[str, str] | None:
    """Return (final_url, text) or None. Never raises."""
    try:
        resp = session.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True,
                           allow_redirects=True)
        if resp.status_code >= 400:
            return None
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype and ctype:
            return None
        chunks, total = [], 0
        for chunk in resp.iter_content(chunk_size=65536, decode_unicode=False):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_BYTES:
                break
        body = b"".join(chunks).decode(resp.encoding or "utf-8", errors="ignore")
        return resp.url, body
    except requests.RequestException:
        return None


def _find_product_url(base_url: str, html: str) -> str | None:
    for href in LINK_HREF_RE.findall(html):
        low = href.lower()
        if any(hint in low for hint in PRODUCT_PATH_HINTS):
            full = urljoin(base_url, href)
            if urlparse(full).netloc == urlparse(base_url).netloc:
                return full
    return None


def _count_products(html: str) -> int:
    """Very rough catalog-size proxy from the homepage/collection markup."""
    hits = set()
    for href in LINK_HREF_RE.findall(html):
        low = href.lower()
        if "/products/" in low or "/product/" in low:
            hits.add(low.split("?")[0])
    return len(hits)


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

def _match_signatures(blob: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for category, vendors in VENDOR_SIGNATURES.items():
        hits = [name for name, needles in vendors.items()
                if any(n in blob for n in needles)]
        if hits:
            found[category] = hits
    return found


def _extract_contacts(blob: str, domain: str) -> dict[str, Any]:
    emails = []
    for e in EMAIL_RE.findall(blob):
        e = e.lower().strip(".")
        if any(j in e for j in JUNK_EMAIL_PARTS):
            continue
        if len(e) > 60:
            continue
        if e not in emails:
            emails.append(e)

    # Prefer addresses on their own domain
    root = domain.split(".")[0]
    emails.sort(key=lambda e: (root not in e.split("@")[-1], len(e)))

    handles = []
    for h in INSTAGRAM_RE.findall(blob):
        h = h.strip(".").lower()
        if h in ("p", "reel", "explore", "accounts", "tv", "stories"):
            continue
        if h not in handles:
            handles.append(h)

    phones = []
    for p in PHONE_RE.findall(blob):
        digits = re.sub(r"\D", "", p)
        if 8 <= len(digits) <= 15 and digits not in phones:
            phones.append(digits)

    return {
        "emails": emails[:5],
        "instagram": handles[:3],
        "phones": phones[:3],
    }


def fingerprint(domain: str, *, use_cache: bool = True,
                session: requests.Session | None = None) -> dict[str, Any]:
    """Fetch homepage + a product page + /cart, detect stack, map gaps."""
    domain = normalise_domain(domain)
    if not domain:
        return {"domain": domain, "status": "invalid", "detected": {},
                "missing": [], "contacts": {}, "signals": {}}

    cache = _load_cache() if use_cache else {}
    hit = cache.get(domain)
    if hit and (time.time() - hit.get("_cached_at", 0)) < CACHE_TTL_SECONDS:
        return hit

    own_session = session is None
    session = session or requests.Session()

    result: dict[str, Any] = {
        "domain": domain,
        "status": "ok",
        "pages_fetched": [],
        "detected": {},
        "missing": [],
        "contacts": {},
        "signals": {},
    }

    try:
        home = _fetch(f"https://{domain}/", session)
        if home is None:
            home = _fetch(f"http://{domain}/", session)
        if home is None:
            result["status"] = "unreachable"
            return result

        base_url, home_html = home
        result["pages_fetched"].append(base_url)
        parts = [home_html]

        product_url = _find_product_url(base_url, home_html)
        if product_url:
            got = _fetch(product_url, session)
            if got:
                result["pages_fetched"].append(got[0])
                parts.append(got[1])

        cart = _fetch(urljoin(base_url, "/cart"), session)
        if cart:
            result["pages_fetched"].append(cart[0])
            parts.append(cart[1])

        blob = "\n".join(parts)
        # Fold external script URLs in explicitly — some apps only appear there
        blob += "\n" + "\n".join(SCRIPT_SRC_RE.findall(blob))
        blob = blob.lower()

        detected = _match_signatures(blob)
        result["detected"] = detected
        result["contacts"] = _extract_contacts(blob, domain)

        paid_app_categories = [
            c for c in detected
            if c not in ("platform", "analytics_spend", "wordpress")
        ]
        result["signals"] = {
            "platform": detected.get("platform", ["unknown"])[0],
            "product_count": _count_products(blob),
            "detected_app_count": sum(
                len(v) for k, v in detected.items() if k != "platform"
            ),
            "paid_app_categories": paid_app_categories,
            "has_reviews_app": "reviews" in detected,
            "has_tracking_pixels": "analytics_spend" in detected,
            "page_count": len(result["pages_fetched"]),
        }

        result["missing"] = build_gap_map(detected)

    finally:
        if own_session:
            session.close()

    if use_cache:
        result["_cached_at"] = time.time()
        cache = _load_cache()
        cache[domain] = result
        _save_cache(cache)

    return result


def build_gap_map(detected: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Absent category -> an opportunity for one of your products."""
    gaps = []
    for category, meta in PRODUCT_CATEGORIES.items():
        if category in detected:
            continue
        gaps.append({
            "category": category,
            "product": meta["product"],
            "status": "missing",
            "value_score": meta["value_score"],
            "evidence": f"No {category.replace('_', ' ')} vendor found in page source.",
            "pitch": meta["pitch"],
        })
    gaps.sort(key=lambda g: g["value_score"], reverse=True)
    return gaps


def fingerprint_many(domains: list[str], *, use_cache: bool = True,
                     max_workers: int = MAX_WORKERS,
                     progress=None) -> list[dict[str, Any]]:
    """Fingerprint a batch in parallel. `progress` is an optional callback(done, total)."""
    domains = [d for d in (normalise_domain(x) for x in domains) if d]
    domains = list(dict.fromkeys(domains))  # dedupe, keep order
    out: list[dict[str, Any]] = []
    total = len(domains)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fingerprint, d, use_cache=use_cache): d for d in domains}
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            try:
                out.append(fut.result())
            except Exception as exc:  # never let one bad domain kill the run
                out.append({"domain": futures[fut], "status": f"error: {exc}",
                            "detected": {}, "missing": [], "contacts": {},
                            "signals": {}})
            if progress:
                progress(i, total)
    return out


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def _revenue_proxy(lead: dict, pagespeed: int | None = None) -> float:
    s = lead.get("signals", {})
    score = 0.0
    score += min(s.get("product_count", 0), 60) / 60 * 40      # catalog size
    score += min(s.get("detected_app_count", 0), 8) / 8 * 30   # software spend
    score += 15 if s.get("has_reviews_app") else 0             # sells enough to collect reviews
    score += 15 if s.get("has_tracking_pixels") else 0         # runs paid ads
    return score  # 0-100ish


def _reachability(lead: dict) -> float:
    c = lead.get("contacts", {})
    score = 0.0
    if c.get("emails"):
        score += 50
    if c.get("instagram"):
        score += 35
    if c.get("phones"):
        score += 15
    return score


def score_leads(leads: list[dict[str, Any]],
                pagespeed_by_domain: dict[str, int] | None = None
                ) -> list[dict[str, Any]]:
    """Attach `score`, `score_parts` and `top_opportunity`, return sorted desc."""
    pagespeed_by_domain = pagespeed_by_domain or {}
    max_gap = sum(m["value_score"] for m in PRODUCT_CATEGORIES.values()) or 1

    for lead in leads:
        if lead.get("status") != "ok":
            lead["score"] = 0.0
            lead["score_parts"] = {}
            lead["top_opportunity"] = None
            continue

        s = lead.get("signals", {})
        gap_raw = sum(g["value_score"] for g in lead.get("missing", []))
        gap_value = gap_raw / max_gap * 100

        revenue = _revenue_proxy(lead)
        reach = _reachability(lead)

        ps = pagespeed_by_domain.get(lead["domain"])
        if ps is not None and ps < 50:
            lead.setdefault("missing", []).append({
                "category": "site_speed",
                "product": "Site speed / rebuild",
                "status": "weak",
                "value_score": 6,
                "evidence": f"Mobile PageSpeed score is {ps}.",
                "pitch": "Slow mobile load. Costs conversions before anything else can help.",
            })

        # Dead-store penalty: no apps AND a tiny catalog usually means no revenue.
        penalty = 0.0
        if s.get("detected_app_count", 0) == 0 and s.get("product_count", 0) < 10:
            penalty = 35.0

        score = (revenue * 0.4) + (gap_value * 0.4) + (reach * 0.2) - penalty
        lead["score"] = round(max(score, 0.0), 1)
        lead["score_parts"] = {
            "revenue_proxy": round(revenue, 1),
            "gap_value": round(gap_value, 1),
            "reachability": round(reach, 1),
            "dead_store_penalty": penalty,
        }
        lead["top_opportunity"] = lead["missing"][0] if lead.get("missing") else None

    return sorted(leads, key=lambda x: x.get("score", 0), reverse=True)


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------

CSV_COLUMNS = [
    "domain", "score", "platform", "product_count", "detected_apps",
    "missing_products", "top_opportunity", "email", "instagram", "phone", "status",
]


def to_csv_rows(leads: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for lead in leads:
        c = lead.get("contacts", {})
        s = lead.get("signals", {})
        detected_flat = [f"{cat}:{v}" for cat, vals in lead.get("detected", {}).items()
                         for v in vals]
        rows.append({
            "domain": lead.get("domain", ""),
            "score": str(lead.get("score", "")),
            "platform": s.get("platform", ""),
            "product_count": str(s.get("product_count", "")),
            "detected_apps": "; ".join(detected_flat),
            "missing_products": "; ".join(g["product"] for g in lead.get("missing", [])),
            "top_opportunity": (lead.get("top_opportunity") or {}).get("product", ""),
            "email": (c.get("emails") or [""])[0],
            "instagram": (c.get("instagram") or [""])[0],
            "phone": (c.get("phones") or [""])[0],
            "status": lead.get("status", ""),
        })
    return rows


# --------------------------------------------------------------------------
# Manual test
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    targets = sys.argv[1:]
    if not targets:
        print("usage: python fingerprint.py domain1.com domain2.com ...")
        raise SystemExit(1)

    def _tick(done, total):
        print(f"  ... {done}/{total}", flush=True)

    print(f"Fingerprinting {len(targets)} domain(s)\n")
    leads = score_leads(fingerprint_many(targets, progress=_tick))

    for lead in leads:
        print("=" * 70)
        print(f"{lead['domain']}   score={lead.get('score')}   status={lead['status']}")
        if lead["status"] != "ok":
            continue
        print(f"  platform : {lead['signals'].get('platform')}")
        print(f"  products : ~{lead['signals'].get('product_count')}")
        print(f"  detected : {lead.get('detected')}")
        print(f"  missing  : {[g['product'] for g in lead.get('missing', [])]}")
        print(f"  contacts : {lead.get('contacts')}")
        print(f"  parts    : {lead.get('score_parts')}")
