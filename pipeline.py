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

TEXTSEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
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
    resp = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=60)
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
    if not google_key:
        raise PipelineError("Google Maps API key is missing.")
    query = f"{niche} in {location}"
    leads = []
    params = {"query": query, "key": google_key}
    for _ in range(3):  # Google paginates in 3 pages max, 20 results/page
        resp = requests.get(TEXTSEARCH_URL, params=params, timeout=30).json()
        status = resp.get("status")
        if status == "REQUEST_DENIED":
            raise PipelineError(f"Google Places request denied: {resp.get('error_message', 'check API key / billing / Places API enabled')}")
        if status not in ("OK", "ZERO_RESULTS"):
            raise PipelineError(f"Places Text Search error: {status} - {resp.get('error_message', '')}")
        for place in resp.get("results", []):
            leads.append({"place_id": place["place_id"], "name": place.get("name", "")})
            if len(leads) >= limit:
                break
        next_token = resp.get("next_page_token")
        if not next_token or len(leads) >= limit:
            break
        time.sleep(2)
        params = {"pagetoken": next_token, "key": google_key}

    detailed = []
    fields = "name,formatted_phone_number,international_phone_number,website,rating,user_ratings_total,formatted_address"
    for lead in leads:
        d = requests.get(
            DETAILS_URL,
            params={"place_id": lead["place_id"], "fields": fields, "key": google_key},
            timeout=30,
        ).json().get("result", {})
        detailed.append({
            "name": d.get("name", lead["name"]),
            "address": d.get("formatted_address", ""),
            "phone": d.get("formatted_phone_number") or d.get("international_phone_number") or "",
            "phone_intl": re.sub(r"[^\d]", "", d.get("international_phone_number") or ""),
            "rating": d.get("rating", ""),
            "review_count": d.get("user_ratings_total", ""),
            "website": d.get("website", ""),
        })
    return detailed


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
        "and explain your reasoning in 2-3 sentences.\n\n"
        "Respond ONLY as a JSON array, no preamble, no markdown fences, in this shape:\n"
        '[{"business": "...", "rank": 1, "reasoning": "...", '
        '"estimated_monthly_revenue_loss_inr": "e.g. 15,000-40,000"}]'
    )
    raw = call_claude(prompt, anthropic_key, max_tokens=1200)
    cleaned = re.sub(r"^```json|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return [{"business": None, "raw_response": raw}]


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
