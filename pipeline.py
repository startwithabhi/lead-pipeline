"""
Core pipeline logic: Lead -> Audit -> Rank -> Build -> Outreach.
Used by app.py. All functions take API keys as arguments (never stored on disk).
"""
import json
import re
import time
from datetime import datetime

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

TEXTSEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PAGESPEED_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-5"

BUILDER_SIGNATURES = {
    "Wix": ["wix.com", "static.wixstatic.com"],
    "Squarespace": ["squarespace.com", "static1.squarespace.com"],
    "WooCommerce": ["woocommerce"],
    "Shopify": ["cdn.shopify.com", "shopify.com"],
    "GoDaddy Website Builder": ["godaddysites.com"],
    "WordPress (generic)": ["wp-content", "wp-includes"],
}


class PipelineError(Exception):
    pass


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------

def call_claude(prompt, anthropic_key, system=None, max_tokens=1500):
    if not anthropic_key:
        raise PipelineError("Anthropic API key is missing.")
    headers = {
        "x-api-key": anthropic_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system
    resp = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=120)
    if resp.status_code == 401:
        raise PipelineError("Anthropic API key was rejected (401). Check the key.")
    resp.raise_for_status()
    data = resp.json()
    return "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()


# ---------------------------------------------------------------------------
# STEP 1: SCRAPE
# ---------------------------------------------------------------------------

def scrape_leads(niche, location, google_key, limit=20):
    """Uses Places API (New) - the legacy Text Search API can no longer be
    enabled on new Google Cloud projects, so this calls the current endpoint,
    which conveniently returns rating/reviews/phone/website in one call
    (no separate Place Details request needed)."""
    if not google_key:
        raise PipelineError("Google Maps API key is missing.")
    query = f"{niche} in {location}"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": google_key,
        "X-Goog-FieldMask": (
            "places.displayName,places.formattedAddress,places.nationalPhoneNumber,"
            "places.internationalPhoneNumber,places.rating,places.userRatingCount,"
            "places.websiteUri,nextPageToken"
        ),
    }
    leads = []
    page_token = None
    for _ in range(3):  # up to 3 pages, 20 results/page = 60 max, matches old behavior
        body = {"textQuery": query, "pageSize": min(limit, 20)}
        if page_token:
            body["pageToken"] = page_token
        resp = requests.post(TEXTSEARCH_URL, headers=headers, json=body, timeout=30)
        if resp.status_code in (400, 403):
            try:
                msg = resp.json().get("error", {}).get("message", resp.text)
            except Exception:
                msg = resp.text
            raise PipelineError(f"Google Places request denied: {msg}")
        resp.raise_for_status()
        data = resp.json()
        for p in data.get("places", []):
            leads.append({
                "name": p.get("displayName", {}).get("text", ""),
                "address": p.get("formattedAddress", ""),
                "phone": p.get("nationalPhoneNumber") or p.get("internationalPhoneNumber") or "",
                "phone_intl": re.sub(r"[^\d]", "", p.get("internationalPhoneNumber") or ""),
                "rating": p.get("rating", ""),
                "review_count": p.get("userRatingCount", ""),
                "website": p.get("websiteUri", ""),
            })
            if len(leads) >= limit:
                break
        page_token = data.get("nextPageToken")
        if not page_token or len(leads) >= limit:
            break
        time.sleep(2)  # next page token needs a moment to become valid
    return leads[:limit]


# ---------------------------------------------------------------------------
# STEP 2: AUDIT
# ---------------------------------------------------------------------------

def _guess_builder(html):
    lower = html.lower()
    for builder, sigs in BUILDER_SIGNATURES.items():
        if any(s in lower for s in sigs):
            return builder
    return "Unknown / custom-coded"


def _pagespeed_score(url, google_key):
    params = {"url": url, "strategy": "mobile"}
    if google_key:
        params["key"] = google_key
    try:
        resp = requests.get(PAGESPEED_URL, params=params, timeout=45).json()
        perf = resp["lighthouseResult"]["categories"]["performance"]["score"]
        fcp = resp["lighthouseResult"]["audits"]["first-contentful-paint"]["numericValue"]
        return round(fcp / 1000, 2), round(perf * 100)
    except Exception:
        return None, None


def audit_website(url, google_key):
    result = {
        "url": url, "reachable": False, "load_time_sec": None, "pagespeed_score": None,
        "mobile_viewport_tag": False, "has_whatsapp_click_to_chat": False, "builder": None,
        "title": "", "meta_description": "", "copyright_year_found": None, "abandoned_signals": [],
    }
    try:
        start = time.time()
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (compatible; LeadAuditBot/1.0)"})
        result["load_time_sec"] = round(time.time() - start, 2)
        result["reachable"] = resp.status_code < 400
        html = resp.text
    except Exception as e:
        result["abandoned_signals"].append(f"Site did not load ({e.__class__.__name__})")
        return result

    if BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        t = soup.find("title")
        result["title"] = t.get_text(strip=True) if t else ""
        meta = soup.find("meta", attrs={"name": "description"})
        result["meta_description"] = meta.get("content", "").strip() if meta else ""
        result["mobile_viewport_tag"] = bool(soup.find("meta", attrs={"name": "viewport"}))
    else:
        tm = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        result["title"] = tm.group(1).strip() if tm else ""
        result["mobile_viewport_tag"] = 'name="viewport"' in html.lower()

    result["has_whatsapp_click_to_chat"] = bool(re.search(r"wa\.me/|api\.whatsapp\.com", html, re.IGNORECASE))
    result["builder"] = _guess_builder(html)

    years = re.findall(r"(?:©|copyright)\s*(\d{4})", html, re.IGNORECASE)
    if years:
        latest = max(int(y) for y in years)
        result["copyright_year_found"] = latest
        if latest < datetime.now().year - 1:
            result["abandoned_signals"].append(f"Footer copyright year is {latest}, not updated")

    if not result["title"]:
        result["abandoned_signals"].append("Missing <title> tag")
    if not result["meta_description"]:
        result["abandoned_signals"].append("Missing meta description")
    if not result["mobile_viewport_tag"]:
        result["abandoned_signals"].append("No mobile viewport tag - likely broken on phones")
    if not result["has_whatsapp_click_to_chat"]:
        result["abandoned_signals"].append("No WhatsApp click-to-chat link found")

    load_time, perf_score = _pagespeed_score(url, google_key)
    if load_time is not None:
        result["load_time_sec"] = load_time
        result["pagespeed_score"] = perf_score
        if perf_score is not None and perf_score < 50:
            result["abandoned_signals"].append(f"PageSpeed performance score is low ({perf_score}/100)")

    return result


def summarize_audit(lead, audit, anthropic_key):
    prompt = (
        f"Business: {lead['name']}\nWebsite audit data (raw, machine-collected):\n{json.dumps(audit, indent=2)}\n\n"
        "Summarize this in 3-4 bullet points covering: page load speed, mobile responsiveness, "
        "WhatsApp/contact click-to-chat presence, builder/platform used, SEO basics (title/meta), "
        "and whether the site looks abandoned/unmaintained. Be concrete and specific, not generic."
    )
    return call_claude(prompt, anthropic_key, max_tokens=400)


def audit_leads(leads, google_key, anthropic_key, progress_cb=None):
    audited = []
    for i, lead in enumerate(leads):
        if progress_cb:
            progress_cb(f"Auditing {i + 1}/{len(leads)}: {lead['name']}")
        entry = dict(lead)
        website = (lead.get("website") or "").strip()
        if not website:
            entry["audit"] = {"note": "No website found."}
            entry["audit_summary"] = (
                "- No website at all: zero owned web presence beyond Maps/social.\n"
                "- Anyone searching Google for this business by name finds only the Maps listing / social pages.\n"
                "- Full opportunity: any decent site is a 100% upgrade over nothing."
            )
        else:
            raw = audit_website(website, google_key)
            entry["audit"] = raw
            try:
                entry["audit_summary"] = summarize_audit(lead, raw, anthropic_key)
            except Exception as e:
                entry["audit_summary"] = f"(Could not generate AI summary: {e})"
        audited.append(entry)
    return audited


# ---------------------------------------------------------------------------
# STEP 3: RANK
# ---------------------------------------------------------------------------

def _repair_truncated_array(text):
    """Best-effort recovery when the array got cut off mid-object (hit the
    token limit) - keep whichever leading objects are fully closed and drop
    the truncated tail, rather than losing the whole response."""
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    last_good_end = None
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                last_good_end = i
    if last_good_end is None:
        return None
    candidate = text[start:last_good_end + 1] + "]"
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, list) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_json_array(text):
    """Robustly pull a JSON array out of a Claude response, even if it's
    wrapped in prose, a markdown fence without a language tag, a dict, or
    got cut off mid-object because it ran out of tokens."""
    candidates = [text.strip()]
    candidates.append(re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).strip())
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            parsed = json.loads(c)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, list):
                    return v
    return _repair_truncated_array(text)


def rank_leads(audited_leads, anthropic_key, top_n=3):
    leads_block = "\n\n".join(
        f"Business: {l['name']}\nRating: {l.get('rating', 'N/A')} ({l.get('review_count', 'N/A')} reviews)\n"
        f"Website: {l.get('website') or 'NONE'}\nAudit notes: {l.get('audit_summary', '')}"
        for l in audited_leads
    )
    prompt = (
        f"Here are leads with audit notes:\n\n{leads_block}\n\n"
        f"Rank the top {top_n} by: (a) how bad their current site/online presence is, "
        "(b) how likely they are to have budget (review count/rating as a proxy for business size), "
        "(c) how easy they'd be to reach (WhatsApp/Instagram active, phone listed). "
        "For each, estimate a plausible monthly revenue loss range in INR from poor web presence, "
        "and explain your reasoning in 1-2 short sentences (be concise - you're ranking multiple "
        "leads, not writing an essay on each one).\n\n"
        "Respond ONLY as a JSON array, no preamble, no markdown fences, in this shape:\n"
        '[{"business": "...", "rank": 1, "reasoning": "...", '
        '"estimated_monthly_revenue_loss_inr": "e.g. 15,000-40,000"}]'
    )
    raw = call_claude(prompt, anthropic_key, max_tokens=4096)
    parsed = _extract_json_array(raw)
    if parsed is None:
        return [{"business": None, "raw_response": raw or "(Claude returned an empty response)"}]
    return parsed


# ---------------------------------------------------------------------------
# STEP 4 & 5: BUILD PROMPT + OUTREACH
# ---------------------------------------------------------------------------

def generate_build_prompt(lead, anthropic_key):
    prompt = (
        f"Business: {lead['name']}\nCurrent site issues: {lead.get('audit_summary', 'No website exists.')}\n\n"
        "Write a complete website-build prompt for an AI website builder (Lovable/v0/Claude Code). Include: "
        "page structure (home, about, services/products, contact), tone/style suited to this business type, "
        "must-have elements (WhatsApp click-to-chat, Google Maps embed, testimonials section), "
        "and a suggested color palette. Write it as a single ready-to-paste prompt block."
    )
    return call_claude(prompt, anthropic_key, max_tokens=900)


def generate_outreach(lead, anthropic_key, language="Hinglish"):
    weakness = lead.get("audit_summary", "no website / weak online presence")
    prompt = (
        f"Write a short WhatsApp message to {lead['name']} introducing that I built them a free sample "
        f"website based on their current online presence, noting this is likely costing them customers: "
        f"{weakness}\nCasual, not salesy, in {language}. Also include a separate follow-up message for if "
        "they don't reply in 2 days. Label them clearly as 'First message:' and 'Follow-up (2 days):'."
    )
    return call_claude(prompt, anthropic_key, max_tokens=500)
