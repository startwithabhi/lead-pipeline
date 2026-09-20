"""
Lead Pipeline web app - fully stateless per-step API.
The browser drives the pipeline by calling one endpoint per lead per stage.
There is NO server-side job state (no in-memory dict, no background thread),
so a mid-run server restart/redeploy/hiccup only ever costs the single
in-flight request - never the whole run - and the browser can just retry it.
"""
import os

from flask import Flask, jsonify, render_template_string, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import fingerprint as fpmod
import pipeline as pl

app = Flask(__name__)

# Visitors bring their own API keys, so their usage costs are theirs -
# this just limits how hard any one visitor can hit *your* server.
limiter = Limiter(get_remote_address, app=app, default_limits=["400 per hour"], storage_uri="memory://")


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"ok": False, "error": "Too many requests from this browser right now. Please wait a bit and try again."}), 429


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/api/scrape", methods=["POST"])
@limiter.limit("15 per hour")
def api_scrape():
    data = request.get_json(force=True)
    try:
        leads = pl.scrape_leads(
            data.get("niche", ""), data.get("location", ""),
            data.get("google_key", ""), int(data.get("limit", 20)),
        )
        return jsonify({"ok": True, "leads": leads})
    except pl.PipelineError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Unexpected error: {e}"}), 500


@app.route("/api/audit_one", methods=["POST"])
@limiter.limit("200 per hour")
def api_audit_one():
    data = request.get_json(force=True)
    lead = data.get("lead", {}) or {}
    google_key = data.get("google_key", "")
    anthropic_key = data.get("anthropic_key", "")
    try:
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
            raw = pl.audit_website(website, google_key)
            entry["audit"] = raw
            entry["audit_summary"] = pl.summarize_audit(lead, raw, anthropic_key)
        return jsonify({"ok": True, "lead": entry})
    except pl.PipelineError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Unexpected error auditing {lead.get('name', 'this lead')}: {e}"}), 500


@app.route("/api/rank", methods=["POST"])
@limiter.limit("30 per hour")
def api_rank():
    data = request.get_json(force=True)
    try:
        ranked = pl.rank_leads(data.get("leads", []), data.get("anthropic_key", ""), int(data.get("top", 3)))
        return jsonify({"ok": True, "ranked": ranked})
    except pl.PipelineError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Unexpected error ranking leads: {e}"}), 500


@app.route("/api/build_one", methods=["POST"])
@limiter.limit("100 per hour")
def api_build_one():
    data = request.get_json(force=True)
    lead = data.get("lead", {}) or {}
    try:
        text = pl.generate_build_prompt(lead, data.get("anthropic_key", ""))
        return jsonify({"ok": True, "build_prompt": text})
    except pl.PipelineError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Unexpected error building prompt for {lead.get('name','this lead')}: {e}"}), 500


@app.route("/api/outreach_one", methods=["POST"])
@limiter.limit("100 per hour")
def api_outreach_one():
    data = request.get_json(force=True)
    lead = data.get("lead", {}) or {}
    try:
        text = pl.generate_outreach(lead, data.get("anthropic_key", ""), data.get("language", "Hinglish"))
        return jsonify({"ok": True, "outreach": text})
    except pl.PipelineError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Unexpected error drafting outreach for {lead.get('name','this lead')}: {e}"}), 500


@app.route("/api/fingerprint_batch", methods=["POST"])
@limiter.limit("200 per hour")
def api_fingerprint_batch():
    """Fingerprint a small chunk of domains.

    The browser sends chunks rather than the whole list so each request stays
    short - a 25-domain call would be a 30-60s request, which is exactly the
    shape that dies on a restart. fingerprint_many() still parallelises
    inside the chunk, and one bad domain is already handled in there.
    """
    data = request.get_json(force=True)
    domains = data.get("domains") or []
    if not isinstance(domains, list) or not domains:
        return jsonify({"ok": False, "error": "No domains supplied."}), 400
    if len(domains) > 12:
        return jsonify({"ok": False, "error": "Chunk too large; send 12 domains or fewer."}), 400
    try:
        leads = fpmod.fingerprint_many(domains, use_cache=bool(data.get("use_cache", True)))
        return jsonify({"ok": True, "leads": leads})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Fingerprint chunk failed: {e}"}), 500


@app.route("/api/pagespeed_batch", methods=["POST"])
@limiter.limit("200 per hour")
def api_pagespeed_batch():
    """Optional. Returns {domain: mobile_score}. Failures are simply omitted."""
    data = request.get_json(force=True)
    domains = data.get("domains") or []
    google_key = data.get("google_key", "")
    out = {}
    for d in domains[:12]:
        try:
            _, score = pl._pagespeed_score(f"https://{d}/", google_key)
            if score is not None:
                out[d] = score
        except Exception:
            pass  # a domain with no speed score just gets no speed gap
    return jsonify({"ok": True, "pagespeed": out})


@app.route("/api/score", methods=["POST"])
@limiter.limit("60 per hour")
def api_score():
    """Ecommerce-mode ranking. Local mode still uses /api/rank (Claude)."""
    data = request.get_json(force=True)
    try:
        ranked = fpmod.score_leads(
            data.get("leads") or [],
            data.get("pagespeed_by_domain") or {},
        )
        return jsonify({"ok": True, "leads": ranked})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Scoring failed: {e}"}), 500


INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Lead Pipeline</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500;600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --ink:#14161c;
  --panel:#1b1e27;
  --panel-2:#21242f;
  --border:#2c303c;
  --text:#edeae3;
  --muted:#9497a6;
  --amber:#e8a33d;
  --amber-dim:#8a6529;
  --rust:#c2542d;
  --sage:#7fa687;
}
*{box-sizing:border-box;}
body{
  margin:0; background:var(--ink); color:var(--text);
  font-family:'Inter',sans-serif; font-size:15px; line-height:1.5;
  padding-bottom:80px;
}
h1,h2,h3{font-family:'Space Grotesk',sans-serif; margin:0;}
.mono{font-family:'IBM Plex Mono',monospace;}
.wrap{max-width:980px; margin:0 auto; padding:32px 20px;}

header.top{display:flex; align-items:baseline; justify-content:space-between; margin-bottom:28px; flex-wrap:wrap; gap:8px;}
header.top h1{font-size:26px; letter-spacing:-0.02em;}
header.top .tag{color:var(--muted); font-size:13px;}

.panel{background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:20px 22px; margin-bottom:20px;}
.panel h2{font-size:14px; text-transform:uppercase; letter-spacing:0.08em; color:var(--muted); margin-bottom:14px;}
.panel h2 .mono{color:var(--amber);}

.row{display:flex; gap:14px; flex-wrap:wrap;}
.field{flex:1; min-width:180px; display:flex; flex-direction:column; gap:6px;}
.field label{font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:0.05em;}
input, select{
  background:var(--panel-2); border:1px solid var(--border); color:var(--text);
  padding:9px 11px; border-radius:6px; font-size:14px; font-family:inherit;
}
input:focus, select:focus{outline:1px solid var(--amber); border-color:var(--amber);}
small.hint{color:var(--muted); font-size:12px; display:block; margin-top:6px;}
small.hint a{color:var(--amber);}

button{
  background:var(--amber); color:#1a1204; border:none; font-weight:600;
  padding:11px 18px; border-radius:6px; cursor:pointer; font-size:14px; font-family:inherit;
}
button:hover{background:#f0ae4d;}
button:disabled{background:var(--amber-dim); color:#66500f; cursor:not-allowed;}
button.ghost{background:transparent; border:1px solid var(--border); color:var(--text); font-weight:500;}
button.ghost:hover{border-color:var(--amber); color:var(--amber);}
button.small{padding:5px 10px; font-size:12px;}

.actions{display:flex; gap:10px; margin-top:16px; flex-wrap:wrap;}

/* Pipeline rail */
.rail{display:flex; justify-content:space-between; position:relative; margin:26px 0 8px; padding:0 6px;}
.rail::before{content:""; position:absolute; top:15px; left:22px; right:22px; height:2px; background:var(--border); z-index:0;}
.rail .fill{position:absolute; top:15px; left:22px; height:2px; background:var(--amber); z-index:1; transition:width .5s ease; width:0%;}
.stage{position:relative; z-index:2; display:flex; flex-direction:column; align-items:center; gap:8px; flex:1;}
.stage .dot{width:30px; height:30px; border-radius:50%; background:var(--panel-2); border:2px solid var(--border); display:flex; align-items:center; justify-content:center; font-family:'IBM Plex Mono',monospace; font-size:13px; color:var(--muted); transition:all .3s;}
.stage.active .dot{border-color:var(--amber); color:var(--amber); background:#2a2109; box-shadow:0 0 0 4px rgba(232,163,61,0.12);}
.stage.done .dot{border-color:var(--sage); color:var(--sage); background:#182119;}
.stage .label{font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.05em; text-align:center;}
.stage.active .label{color:var(--text);}

.log{background:#0f1116; border:1px solid var(--border); border-radius:8px; padding:12px 14px; max-height:160px; overflow-y:auto; font-family:'IBM Plex Mono',monospace; font-size:12px; color:var(--muted); margin-top:16px;}
.log div{padding:2px 0;}
.log div.s5{color:var(--sage);}
.log div.err{color:var(--rust);}

table{width:100%; border-collapse:collapse; font-size:13px;}
th{text-align:left; color:var(--muted); font-weight:500; font-size:11px; text-transform:uppercase; letter-spacing:0.05em; padding:8px 10px; border-bottom:1px solid var(--border);}
td{padding:9px 10px; border-bottom:1px solid var(--border);}
.badge{display:inline-block; padding:2px 8px; border-radius:99px; font-size:11px; font-family:'IBM Plex Mono',monospace;}
.badge.no-site{background:#2a2109; color:var(--amber); border:1px solid var(--amber-dim);}
.badge.has-site{background:#182119; color:var(--sage); border:1px solid #2a4a35;}

.card{background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:22px 24px; margin-bottom:18px;}
.card .rank-num{font-family:'IBM Plex Mono',monospace; color:var(--amber); font-size:13px;}
.card h3{font-size:19px; margin:2px 0 10px;}
.meta-row{display:flex; gap:16px; flex-wrap:wrap; color:var(--muted); font-size:13px; margin-bottom:14px;}
.reasoning{font-size:14px; color:var(--text); margin-bottom:6px;}
.loss{font-family:'IBM Plex Mono',monospace; color:var(--rust); font-size:13px; margin-bottom:14px;}
.subhead{font-size:12px; text-transform:uppercase; letter-spacing:0.06em; color:var(--muted); margin:16px 0 8px; display:flex; justify-content:space-between; align-items:center;}
.audit-summary{font-size:13.5px; white-space:pre-line; color:var(--text); background:var(--panel-2); border-radius:8px; padding:12px 14px;}
.codebox{background:#0f1116; border:1px solid var(--border); border-radius:8px; padding:14px; font-family:'IBM Plex Mono',monospace; font-size:12.5px; white-space:pre-wrap; color:#d8d5cd; max-height:280px; overflow-y:auto;}
.outreach-box{background:var(--panel-2); border-radius:8px; padding:14px; font-size:13.5px; white-space:pre-line;}
.wa-link{display:inline-flex; align-items:center; gap:6px; color:#25d366; text-decoration:none; font-size:13px; font-weight:600; margin-top:8px;}
.wa-link:hover{text-decoration:underline;}

.error-box{background:#2a1414; border:1px solid #5a2a2a; color:#e59a9a; padding:14px 16px; border-radius:8px; font-size:13.5px; margin-bottom:16px;}
.hidden{display:none;}

textarea{
  background:var(--panel-2); border:1px solid var(--border); color:var(--text);
  padding:9px 11px; border-radius:6px; font-size:13px; font-family:'IBM Plex Mono',monospace;
  resize:vertical; width:100%;
}
textarea:focus{outline:1px solid var(--amber); border-color:var(--amber);}
input[type=file]{font-size:12px; color:var(--muted); padding:7px 0; border:none; background:none;}

/* Fingerprint results table */
.fp-table{width:100%; border-collapse:collapse; font-size:13px; table-layout:auto;}
.fp-table th{white-space:nowrap;}
.fp-table td{vertical-align:top;}
.fp-row{cursor:pointer;}
.fp-row:hover{background:var(--panel-2);}
.fp-row td{border-bottom:1px solid var(--border);}
.fp-domain{font-family:'IBM Plex Mono',monospace; color:var(--text); font-weight:500;}
.fp-caret{color:var(--muted); font-size:10px; margin-right:6px;}
.score-cell{font-family:'IBM Plex Mono',monospace; font-weight:600; white-space:nowrap;}
.score-hi{color:var(--sage);} .score-mid{color:var(--amber);} .score-lo{color:var(--muted);}
.chip{display:inline-block; padding:2px 7px; border-radius:99px; font-size:10.5px;
  font-family:'IBM Plex Mono',monospace; margin:0 4px 4px 0; white-space:nowrap;}
.chip.has{background:#182119; color:var(--sage); border:1px solid #2a4a35;}
.chip.gap{background:#2a2109; color:var(--amber); border:1px solid var(--amber-dim);}
.chip.plat{background:var(--panel-2); color:var(--muted); border:1px solid var(--border);}
.contact-cell{font-size:12px; color:var(--muted); word-break:break-all;}
.contact-cell a{color:var(--amber); text-decoration:none;}
.detail-row td{background:#0f1116; border-bottom:1px solid var(--border); padding:16px 14px;}
.detail-grid{display:flex; gap:24px; flex-wrap:wrap; margin-bottom:14px;}
.parts-item{font-family:'IBM Plex Mono',monospace; font-size:12px; color:var(--muted);}
.parts-item b{color:var(--text); font-weight:600;}
.gap-item{border-left:2px solid var(--amber-dim); padding:6px 0 6px 12px; margin-bottom:10px;}
.gap-item .gap-name{font-weight:600; font-size:13px;}
.gap-item .gap-ev{font-size:12px; color:var(--muted); font-family:'IBM Plex Mono',monospace;}
.gap-item .gap-pitch{font-size:13px; color:var(--text); margin-top:3px;}
.status-bad{color:var(--rust); font-size:12px; font-family:'IBM Plex Mono',monospace;}
.footer-note{color:var(--muted); font-size:12px; text-align:center; margin-top:30px;}
</style>
</head>
<body>
<div class="wrap">

<header class="top">
  <div>
    <h1>Lead Pipeline</h1>
    <div class="tag">Lead &rarr; Audit &rarr; Rank &rarr; Build &rarr; Outreach, run end to end.</div>
  </div>
</header>

<div class="panel" id="settingsPanel">
  <h2><span class="mono">[00]</span> API Keys</h2>
  <div class="row">
    <div class="field">
      <label>Google Maps API key</label>
      <input type="password" id="googleKey" placeholder="AIza...">
      <small class="hint">Places API + PageSpeed Insights. Get one at <a href="https://console.cloud.google.com/" target="_blank">console.cloud.google.com</a></small>
    </div>
    <div class="field">
      <label>Anthropic API key</label>
      <input type="password" id="anthropicKey" placeholder="sk-ant-...">
      <small class="hint">Get one at <a href="https://console.anthropic.com/" target="_blank">console.anthropic.com</a></small>
    </div>
  </div>
  <div class="actions">
    <button class="ghost small" onclick="saveKeys()">Save keys in this browser</button>
    <span id="keyStatus" class="mono" style="font-size:12px;color:var(--muted);align-self:center;"></span>
  </div>
  <small class="hint">Keys are stored only in your browser's local storage and sent directly with each request. Nothing is saved on a server.</small>
</div>

<div class="panel">
  <h2><span class="mono">[01]</span> Configure the run</h2>

  <div class="row">
    <div class="field">
      <label>Lead source</label>
      <select id="leadSource" onchange="onSourceChange()">
        <option value="local">Local businesses (Google Places)</option>
        <option value="ecom">Ecommerce stores (paste domains / CSV)</option>
      </select>
    </div>
  </div>

  <div id="localFields">
    <div class="row" style="margin-top:12px;">
      <div class="field">
        <label>Niche</label>
        <input id="niche" placeholder="e.g. clothing boutique">
      </div>
      <div class="field">
        <label>Location</label>
        <input id="location" placeholder="e.g. Siliguri, West Bengal">
      </div>
    </div>
  </div>

  <div id="ecomFields" class="hidden">
    <div class="row" style="margin-top:12px;">
      <div class="field">
        <label>Domains (one per line)</label>
        <textarea id="domains" rows="6" placeholder="example-store.com&#10;anotherbrand.in&#10;https://third-store.com/collections/all"></textarea>
        <small class="hint">Full URLs are fine - they get reduced to the bare domain. No Google key needed in this mode.</small>
      </div>
    </div>
    <div class="row" style="margin-top:12px;">
      <div class="field">
        <label>...or upload a CSV with a "domain" column</label>
        <input type="file" id="csvFile" accept=".csv,text/csv" onchange="loadCsv()">
        <small class="hint" id="csvStatus"></small>
      </div>
      <div class="field">
        <label>PageSpeed check</label>
        <label style="display:flex;align-items:center;gap:8px;text-transform:none;font-size:13px;color:var(--text);">
          <input type="checkbox" id="runPagespeed" style="width:auto;">
          Also run mobile PageSpeed
        </label>
        <small class="hint">Adds ~15s per domain. Only adds a site-speed opportunity row - it does not change the score.</small>
      </div>
    </div>
  </div>

  <div class="row" style="margin-top:12px;">
    <div class="field" id="limitField">
      <label>Leads to scan</label>
      <input id="limit" type="number" value="20" min="3" max="60">
    </div>
    <div class="field">
      <label>Top leads to fully build out</label>
      <input id="top" type="number" value="3" min="1" max="5">
    </div>
    <div class="field">
      <label>Outreach language</label>
      <select id="language">
        <option value="Hinglish">Hinglish</option>
        <option value="English">English</option>
      </select>
    </div>
  </div>
  <div class="actions">
    <button class="ghost" onclick="previewLeads()" id="previewBtn">Preview leads</button>
    <button onclick="runPipeline()" id="runBtn">Run full pipeline</button>
  </div>
  <div id="previewError"></div>
  <div id="previewTable"></div>
</div>

<div class="panel hidden" id="progressPanel">
  <h2><span class="mono">[02]</span> Running</h2>
  <div class="rail" id="rail">
    <div class="fill" id="railFill"></div>
    <div class="stage" data-stage="1"><div class="dot">1</div><div class="label" data-local="Scrape" data-ecom="Source">Scrape</div></div>
    <div class="stage" data-stage="2"><div class="dot">2</div><div class="label" data-local="Audit" data-ecom="Fingerprint">Audit</div></div>
    <div class="stage" data-stage="3"><div class="dot">3</div><div class="label" data-local="Rank" data-ecom="Score">Rank</div></div>
    <div class="stage" data-stage="4"><div class="dot">4</div><div class="label" data-local="Build" data-ecom="Build">Build</div></div>
    <div class="stage" data-stage="5"><div class="dot">5</div><div class="label" data-local="Outreach" data-ecom="Outreach">Outreach</div></div>
  </div>
  <div class="log" id="logBox"></div>
</div>

<div id="errorPanel"></div>

<div id="resultsSection"></div>

<div class="footer-note" id="downloadRow"></div>

</div>

<script>
let lastResults = null;
let lastEcomLeads = null;

function saveKeys(){
  localStorage.setItem('lp_google_key', document.getElementById('googleKey').value.trim());
  localStorage.setItem('lp_anthropic_key', document.getElementById('anthropicKey').value.trim());
  document.getElementById('keyStatus').textContent = 'saved.';
  setTimeout(()=>document.getElementById('keyStatus').textContent='', 2000);
}
function loadKeys(){
  const g = localStorage.getItem('lp_google_key');
  const a = localStorage.getItem('lp_anthropic_key');
  if(g) document.getElementById('googleKey').value = g;
  if(a) document.getElementById('anthropicKey').value = a;
}
loadKeys();

function getKeys(){
  return {
    google_key: document.getElementById('googleKey').value.trim(),
    anthropic_key: document.getElementById('anthropicKey').value.trim(),
  };
}

function esc(s){
  return (s || '').toString().replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function postJSON(url, body){
  const resp = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const data = await resp.json();
  if(!data.ok) throw new Error(data.error || `Request to ${url} failed`);
  return data;
}

async function previewLeads(){
  const errBox = document.getElementById('previewError');
  const tableBox = document.getElementById('previewTable');
  errBox.innerHTML = ''; tableBox.innerHTML = '';
  const niche = document.getElementById('niche').value.trim();
  const location = document.getElementById('location').value.trim();
  const limit = document.getElementById('limit').value;
  if(!niche || !location){ errBox.innerHTML = '<div class="error-box">Enter a niche and location first.</div>'; return; }
  const btn = document.getElementById('previewBtn');
  btn.disabled = true; btn.textContent = 'Searching...';
  try{
    const data = await postJSON('/api/scrape', {niche, location, limit, ...getKeys()});
    let rows = data.leads.map(l => `
      <tr>
        <td>${esc(l.name)}</td>
        <td>${esc(l.phone) || '&mdash;'}</td>
        <td>${l.rating || '&mdash;'} (${l.review_count || 0})</td>
        <td>${l.website ? `<span class="badge has-site">HAS SITE</span>` : `<span class="badge no-site">NO SITE</span>`}</td>
      </tr>`).join('');
    tableBox.innerHTML = `
      <table style="margin-top:14px;">
        <thead><tr><th>Business</th><th>Phone</th><th>Rating</th><th>Website</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <small class="hint" style="margin-top:8px;">${data.leads.length} found. Run the full pipeline to audit, rank, and build outreach for these.</small>`;
  }catch(e){
    errBox.innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
  }finally{
    btn.disabled = false; btn.textContent = 'Preview leads';
  }
}

function resetRail(){
  document.querySelectorAll('.stage').forEach(s => s.classList.remove('active','done'));
  document.getElementById('railFill').style.width = '0%';
}
function updateRail(stageNum, status){
  document.querySelectorAll('.stage').forEach(s => {
    const n = parseInt(s.dataset.stage);
    s.classList.remove('active','done');
    if(n < stageNum || status === 'done') s.classList.add('done');
    else if(n === stageNum) s.classList.add('active');
  });
  const pct = status === 'done' ? 100 : ((stageNum - 1) / 4) * 100;
  document.getElementById('railFill').style.width = pct + '%';
}
function logLine(stage, message, isErr){
  const box = document.getElementById('logBox');
  const div = document.createElement('div');
  div.className = 's' + stage + (isErr ? ' err' : '');
  div.textContent = `[${stage}] ${message}`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  updateRail(stage, 'running');
}

function findLeadByName(leads, name){
  if(!name) return null;
  const exact = leads.find(l => l.name.trim().toLowerCase() === name.trim().toLowerCase());
  if(exact) return exact;
  const matches = leads.filter(l => l.name.toLowerCase().includes(name.trim().toLowerCase()));
  return matches.length === 1 ? matches[0] : null;
}

/* ---------- lead source switching ---------- */
function currentSource(){ return document.getElementById('leadSource').value; }

function onSourceChange(){
  const ecom = currentSource() === 'ecom';
  document.getElementById('localFields').classList.toggle('hidden', ecom);
  document.getElementById('ecomFields').classList.toggle('hidden', !ecom);
  document.getElementById('limitField').classList.toggle('hidden', ecom);
  document.getElementById('previewBtn').classList.toggle('hidden', ecom);
  document.querySelectorAll('.stage .label').forEach(el => {
    el.textContent = ecom ? el.dataset.ecom : el.dataset.local;
  });
}
onSourceChange();

function parseDomains(){
  const raw = document.getElementById('domains').value;
  return raw.split(/[\n,]/).map(s => s.trim()).filter(Boolean);
}

function loadCsv(){
  const f = document.getElementById('csvFile').files[0];
  const status = document.getElementById('csvStatus');
  if(!f) return;
  const reader = new FileReader();
  reader.onload = () => {
    try{
      const lines = reader.result.split(/\r?\n/).filter(l => l.trim());
      if(!lines.length) throw new Error('File is empty');
      const header = lines[0].split(',').map(h => h.trim().toLowerCase().replace(/^"|"$/g,''));
      let idx = header.indexOf('domain');
      if(idx === -1) idx = header.findIndex(h => h.includes('domain') || h.includes('website') || h.includes('url'));
      if(idx === -1) throw new Error('No "domain" column found in the header row');
      const vals = lines.slice(1).map(l => {
        const cells = l.match(/("([^"]|"")*"|[^,]*)(,|$)/g) || [];
        return (cells[idx] || '').replace(/,$/,'').trim().replace(/^"|"$/g,'');
      }).filter(Boolean);
      document.getElementById('domains').value = vals.join('\n');
      status.textContent = `Loaded ${vals.length} domains from ${f.name}.`;
      status.style.color = 'var(--sage)';
    }catch(e){
      status.textContent = e.message;
      status.style.color = 'var(--rust)';
    }
  };
  reader.readAsText(f);
}

function chunk(arr, n){
  const out = [];
  for(let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n));
  return out;
}

/* ---------- ecommerce mode ---------- */
async function runEcomPipeline(){
  const errBox = document.getElementById('errorPanel');
  errBox.innerHTML = '';
  document.getElementById('resultsSection').innerHTML = '';
  document.getElementById('downloadRow').innerHTML = '';

  const domains = parseDomains();
  const keys = getKeys();
  const wantPagespeed = document.getElementById('runPagespeed').checked;
  if(!domains.length){ errBox.innerHTML = '<div class="error-box">Paste at least one domain, or upload a CSV.</div>'; return; }

  document.getElementById('runBtn').disabled = true;
  document.getElementById('progressPanel').classList.remove('hidden');
  document.getElementById('logBox').innerHTML = '';
  resetRail();

  try{
    logLine(1, `${domains.length} domain(s) queued.`);

    // Stage 2 - fingerprint in chunks so no single request runs long
    const batches = chunk(domains, 6);
    let all = [], done = 0;
    for(const batch of batches){
      try{
        const d = await postJSON('/api/fingerprint_batch', {domains: batch, use_cache: true});
        all = all.concat(d.leads);
      }catch(e){
        logLine(2, `Chunk failed (${batch.join(', ')}): ${e.message}`, true);
      }
      done += batch.length;
      logLine(2, `Fingerprinted ${Math.min(done, domains.length)}/${domains.length}`);
    }
    if(!all.length) throw new Error('No domains could be fingerprinted - see the log above.');

    const reachable = all.filter(l => l.status === 'ok').length;
    logLine(2, `${reachable} reachable, ${all.length - reachable} unreachable/errored.`);

    // Optional PageSpeed, also chunked
    let psMap = {};
    if(wantPagespeed){
      const okDomains = all.filter(l => l.status === 'ok').map(l => l.domain);
      let psDone = 0;
      for(const batch of chunk(okDomains, 4)){
        try{
          const d = await postJSON('/api/pagespeed_batch', {domains: batch, google_key: keys.google_key});
          psMap = Object.assign(psMap, d.pagespeed);
        }catch(e){
          logLine(2, `PageSpeed chunk skipped: ${e.message}`, true);
        }
        psDone += batch.length;
        logLine(2, `PageSpeed ${Math.min(psDone, okDomains.length)}/${okDomains.length}`);
      }
    }

    // Stage 3 - score
    logLine(3, 'Scoring and ranking...');
    const scored = await postJSON('/api/score', {leads: all, pagespeed_by_domain: psMap});
    logLine(3, 'Scoring complete.');
    updateRail(3, 'done');

    lastEcomLeads = scored.leads;
    renderFingerprintTable(scored.leads);
  }catch(e){
    errBox.innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
  }finally{
    document.getElementById('runBtn').disabled = false;
  }
}

/* ---------- results table ---------- */
function scoreClass(s){ return s >= 55 ? 'score-hi' : (s >= 30 ? 'score-mid' : 'score-lo'); }

function renderFingerprintTable(leads){
  const box = document.getElementById('resultsSection');
  let rows = '';
  leads.forEach((l, i) => {
    if(l.status !== 'ok'){
      rows += `<tr class="fp-row"><td class="fp-domain">${esc(l.domain)}</td>
        <td colspan="7" class="status-bad">${esc(l.status)}</td></tr>`;
      return;
    }
    const sig = l.signals || {};
    const detectedChips = Object.entries(l.detected || {})
      .filter(([c]) => c !== 'platform')
      .flatMap(([c, vs]) => vs.map(v => `<span class="chip has">${esc(v)}</span>`)).join('') || '<span class="chip plat">none</span>';
    const gapChips = (l.missing || []).map(g => `<span class="chip gap">${esc(g.product)}</span>`).join('') || '<span class="chip plat">no gaps</span>';
    const c = l.contacts || {};
    const email = (c.emails || [])[0] || '';
    const ig = (c.instagram || [])[0] || '';
    const parts = l.score_parts || {};

    rows += `
      <tr class="fp-row" onclick="toggleDetail(${i})">
        <td class="fp-domain"><span class="fp-caret" id="caret-${i}">&#9654;</span>${esc(l.domain)}</td>
        <td class="score-cell ${scoreClass(l.score)}">${l.score}</td>
        <td><span class="chip plat">${esc(sig.platform || 'unknown')}</span></td>
        <td>${detectedChips}</td>
        <td>${gapChips}</td>
        <td style="font-size:12.5px;">${esc((l.top_opportunity || {}).product || '-')}</td>
        <td class="contact-cell">${email ? `<a href="mailto:${esc(email)}">${esc(email)}</a>` : '-'}</td>
        <td class="contact-cell">${ig ? `<a href="https://instagram.com/${esc(ig)}" target="_blank">@${esc(ig)}</a>` : '-'}</td>
      </tr>
      <tr class="detail-row hidden" id="detail-${i}">
        <td colspan="8">
          <div class="detail-grid">
            <span class="parts-item">revenue proxy <b>${parts.revenue_proxy ?? '-'}</b></span>
            <span class="parts-item">gap value <b>${parts.gap_value ?? '-'}</b></span>
            <span class="parts-item">reachability <b>${parts.reachability ?? '-'}</b></span>
            <span class="parts-item">dead-store penalty <b>-${parts.dead_store_penalty ?? 0}</b></span>
            <span class="parts-item">~products <b>${sig.product_count ?? '-'}</b></span>
          </div>
          ${(l.missing || []).map(g => `
            <div class="gap-item">
              <div class="gap-name">${esc(g.product)} <span class="parts-item">(value ${g.value_score})</span></div>
              <div class="gap-ev">${esc(g.evidence)}</div>
              <div class="gap-pitch">${esc(g.pitch)}</div>
            </div>`).join('') || '<div class="parts-item">No gaps detected - they already run everything we sell.</div>'}
        </td>
      </tr>`;
  });

  box.innerHTML = `
    <div class="panel">
      <h2><span class="mono">[03]</span> Fingerprinted leads &mdash; ${leads.length} scanned</h2>
      <div style="overflow-x:auto;">
        <table class="fp-table">
          <thead><tr>
            <th>Domain</th><th>Score</th><th>Platform</th><th>Detected apps</th>
            <th>Missing products</th><th>Top opportunity</th><th>Email</th><th>Instagram</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <small class="hint">Click any row to see the score breakdown and the evidence behind each gap.</small>
    </div>`;
}

function toggleDetail(i){
  const row = document.getElementById(`detail-${i}`);
  const caret = document.getElementById(`caret-${i}`);
  const hidden = row.classList.toggle('hidden');
  caret.innerHTML = hidden ? '&#9654;' : '&#9660;';
}

async function runPipeline(){
  if(currentSource() === 'ecom') return runEcomPipeline();
  const errBox = document.getElementById('errorPanel');
  errBox.innerHTML = '';
  document.getElementById('resultsSection').innerHTML = '';
  document.getElementById('downloadRow').innerHTML = '';
  const niche = document.getElementById('niche').value.trim();
  const location = document.getElementById('location').value.trim();
  const limit = document.getElementById('limit').value;
  const top = document.getElementById('top').value;
  const language = document.getElementById('language').value;
  const keys = getKeys();
  if(!niche || !location){ errBox.innerHTML = '<div class="error-box">Enter a niche and location first.</div>'; return; }
  if(!keys.google_key || !keys.anthropic_key){ errBox.innerHTML = '<div class="error-box">Enter and save both API keys first.</div>'; return; }

  document.getElementById('runBtn').disabled = true;
  document.getElementById('progressPanel').classList.remove('hidden');
  document.getElementById('logBox').innerHTML = '';
  resetRail();

  try{
    logLine(1, `Searching Google Places for '${niche}' in '${location}'...`);
    const scrapeData = await postJSON('/api/scrape', {niche, location, limit, ...keys});
    const leads = scrapeData.leads;
    logLine(1, `Found ${leads.length} businesses.`);

    const audited = [];
    for(let i = 0; i < leads.length; i++){
      logLine(2, `Auditing ${i+1}/${leads.length}: ${leads[i].name}`);
      try{
        const d = await postJSON('/api/audit_one', {lead: leads[i], ...keys});
        audited.push(d.lead);
      }catch(e){
        logLine(2, `Skipped ${leads[i].name} (${e.message})`, true);
      }
    }
    if(audited.length === 0) throw new Error('No leads could be audited - check the log above for why each one failed.');

    logLine(3, `Ranking leads to find the top ${top} opportunities...`);
    const rankData = await postJSON('/api/rank', {leads: audited, top, anthropic_key: keys.anthropic_key});
    logLine(3, 'Ranking complete.');

    const results = [];
    for(const entry of rankData.ranked){
      const bizName = entry.business;
      const lead = findLeadByName(audited, bizName);
      if(!lead){ results.push({rank_info: entry, lead: null}); continue; }
      try{
        logLine(4, `Writing build prompt for ${bizName}...`);
        const bData = await postJSON('/api/build_one', {lead, anthropic_key: keys.anthropic_key});
        logLine(5, `Drafting outreach for ${bizName}...`);
        const oData = await postJSON('/api/outreach_one', {lead, anthropic_key: keys.anthropic_key, language});
        results.push({rank_info: entry, lead, build_prompt: bData.build_prompt, outreach: oData.outreach});
      }catch(e){
        logLine(5, `Failed for ${bizName}: ${e.message}`, true);
        results.push({rank_info: entry, lead, build_prompt: null, outreach: null, step_error: e.message});
      }
    }

    logLine(5, 'Done.');
    updateRail(5, 'done');
    renderResults(results, niche, location);
  }catch(e){
    errBox.innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
  }finally{
    document.getElementById('runBtn').disabled = false;
  }
}

function copyText(id){
  const el = document.getElementById(id);
  navigator.clipboard.writeText(el.dataset.raw || el.textContent);
  const btn = event.target;
  const old = btn.textContent;
  btn.textContent = 'Copied';
  setTimeout(()=>btn.textContent = old, 1200);
}

function renderResults(results, niche, location){
  lastResults = results;
  const box = document.getElementById('resultsSection');
  let html = `<div class="panel"><h2><span class="mono">[03]</span> Top leads &mdash; ${esc(niche)} in ${esc(location)}</h2></div>`;
  results.forEach((r, idx) => {
    if(!r.lead){
      html += `<div class="card"><span class="rank-num">#${idx+1}</span><h3>${esc(r.rank_info.business || 'Unmatched result')}</h3><div class="reasoning">${esc(r.rank_info.raw_response || r.rank_info.reasoning || 'No data')}</div></div>`;
      return;
    }
    const l = r.lead;
    const waNumber = l.phone_intl;
    const firstMsg = (r.outreach || '').split(/Follow-up/i)[0].replace(/First message:?/i, '').trim();
    const waLink = waNumber && r.outreach ? `https://wa.me/${waNumber}?text=${encodeURIComponent(firstMsg)}` : null;
    html += `
      <div class="card">
        <span class="rank-num mono">RANK #${r.rank_info.rank || idx+1}</span>
        <h3>${esc(l.name)}</h3>
        <div class="meta-row">
          <span>${esc(l.phone) || 'no phone listed'}</span>
          <span>${l.rating || '&mdash;'} &#9733; (${l.review_count || 0} reviews)</span>
          <span>${l.website ? `<a href="${esc(l.website)}" target="_blank" style="color:var(--muted)">${esc(l.website)}</a>` : '<span class="badge no-site">NO WEBSITE</span>'}</span>
        </div>
        <div class="reasoning">${esc(r.rank_info.reasoning || '')}</div>
        <div class="loss">Est. monthly revenue loss: &#8377; ${esc(r.rank_info.estimated_monthly_revenue_loss_inr || 'n/a')}</div>

        <div class="subhead">Audit summary</div>
        <div class="audit-summary">${esc(l.audit_summary || '')}</div>

        ${r.step_error ? `<div class="error-box">Build/outreach step failed for this lead: ${esc(r.step_error)}</div>` : `
        <div class="subhead">Build prompt <button class="ghost small" onclick="copyText('build-${idx}')">Copy</button></div>
        <div class="codebox" id="build-${idx}" data-raw="${esc(r.build_prompt)}">${esc(r.build_prompt)}</div>

        <div class="subhead">Outreach (${esc(document.getElementById('language').value)}) <button class="ghost small" onclick="copyText('outreach-${idx}')">Copy</button></div>
        <div class="outreach-box" id="outreach-${idx}" data-raw="${esc(r.outreach)}">${esc(r.outreach)}</div>
        ${waLink ? `<a class="wa-link" href="${waLink}" target="_blank">Open in WhatsApp &rarr;</a>` : ''}
        `}
      </div>`;
  });
  box.innerHTML = html;
  document.getElementById('downloadRow').innerHTML = `<button class="ghost small" onclick="downloadReport('${esc(niche)}','${esc(location)}')">Download full report (.md)</button>`;
}

function downloadReport(niche, location){
  if(!lastResults) return;
  let md = `# Lead pipeline report: ${niche} in ${location}\nGenerated ${new Date().toISOString()}\n`;
  lastResults.forEach((r, idx) => {
    if(!r.lead) return;
    const l = r.lead;
    md += `\n## #${r.rank_info.rank || idx+1} ${l.name}\n`;
    md += `Phone: ${l.phone || 'N/A'} | Rating: ${l.rating || 'N/A'} (${l.review_count || 0} reviews) | Website: ${l.website || 'NONE'}\n`;
    md += `\n**Why this lead:** ${r.rank_info.reasoning || ''}\n`;
    md += `\n**Estimated monthly revenue loss:** INR ${r.rank_info.estimated_monthly_revenue_loss_inr || 'n/a'}\n`;
    md += `\n**Audit summary:**\n${l.audit_summary || ''}\n`;
    if(r.build_prompt) md += `\n**Build prompt:**\n\`\`\`\n${r.build_prompt}\n\`\`\`\n`;
    if(r.outreach) md += `\n**Outreach:**\n${r.outreach}\n`;
  });
  const blob = new Blob([md], {type:'text/markdown'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `lead_report_${Date.now()}.md`;
  a.click();
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"Lead Pipeline running at http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
