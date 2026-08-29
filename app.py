"""
Lead Pipeline web app.
Run with:  python app.py
Then open: http://127.0.0.1:5050
"""
import threading
import uuid
from datetime import datetime

from flask import Flask, jsonify, render_template_string, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import pipeline as pl

app = Flask(__name__)

# Visitors bring their own API keys, so their usage costs are theirs -
# but this still limits how hard any one visitor can hit *your* server.
limiter = Limiter(get_remote_address, app=app, default_limits=["60 per hour"], storage_uri="memory://")

JOBS = {}
JOBS_LOCK = threading.Lock()


def _find_lead(leads, name):
    if not name:
        return None
    for l in leads:
        if l["name"].strip().lower() == name.strip().lower():
            return l
    matches = [l for l in leads if name.strip().lower() in l["name"].strip().lower()]
    return matches[0] if len(matches) == 1 else None


def _log(job_id, stage, message):
    with JOBS_LOCK:
        JOBS[job_id]["log"].append({"stage": stage, "message": message, "t": datetime.now().isoformat()})


def _run_job(job_id, niche, location, limit, top, language, google_key, anthropic_key):
    try:
        _log(job_id, 1, f"Searching Google Places for '{niche}' in '{location}'...")
        leads = pl.scrape_leads(niche, location, google_key, limit)
        with JOBS_LOCK:
            JOBS[job_id]["leads"] = leads
        _log(job_id, 1, f"Found {len(leads)} businesses.")

        _log(job_id, 2, "Auditing websites...")
        audited = pl.audit_leads(
            leads, google_key, anthropic_key,
            progress_cb=lambda msg: _log(job_id, 2, msg),
        )
        with JOBS_LOCK:
            JOBS[job_id]["audited"] = audited

        _log(job_id, 3, f"Ranking leads to find the top {top} opportunities...")
        ranked = pl.rank_leads(audited, anthropic_key, top)
        _log(job_id, 3, "Ranking complete.")

        results = []
        for entry in ranked:
            biz_name = entry.get("business")
            lead = _find_lead(audited, biz_name) if biz_name else None
            if not lead:
                results.append({"rank_info": entry, "lead": None})
                continue
            _log(job_id, 4, f"Writing build prompt for {biz_name}...")
            build_text = pl.generate_build_prompt(lead, anthropic_key)
            _log(job_id, 5, f"Drafting outreach for {biz_name}...")
            outreach_text = pl.generate_outreach(lead, anthropic_key, language)
            results.append({
                "rank_info": entry,
                "lead": lead,
                "build_prompt": build_text,
                "outreach": outreach_text,
            })

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["results"] = results
        _log(job_id, 5, "Done.")

    except pl.PipelineError as e:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = f"Unexpected error: {e}"


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


@app.route("/api/run", methods=["POST"])
@limiter.limit("5 per hour")
def api_run():
    data = request.get_json(force=True)
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "running", "log": [], "leads": [], "audited": [], "results": [], "error": None}
    t = threading.Thread(
        target=_run_job,
        args=(
            job_id, data.get("niche", ""), data.get("location", ""),
            int(data.get("limit", 20)), int(data.get("top", 3)),
            data.get("language", "Hinglish"), data.get("google_key", ""), data.get("anthropic_key", ""),
        ),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/run/<job_id>")
def api_run_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Unknown job id"}), 404
        return jsonify({"ok": True, **job})


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
      <label>Niche</label>
      <input id="niche" placeholder="e.g. clothing boutique">
    </div>
    <div class="field">
      <label>Location</label>
      <input id="location" placeholder="e.g. Siliguri, West Bengal">
    </div>
  </div>
  <div class="row" style="margin-top:12px;">
    <div class="field">
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
    <div class="stage" data-stage="1"><div class="dot">1</div><div class="label">Scrape</div></div>
    <div class="stage" data-stage="2"><div class="dot">2</div><div class="label">Audit</div></div>
    <div class="stage" data-stage="3"><div class="dot">3</div><div class="label">Rank</div></div>
    <div class="stage" data-stage="4"><div class="dot">4</div><div class="label">Build</div></div>
    <div class="stage" data-stage="5"><div class="dot">5</div><div class="label">Outreach</div></div>
  </div>
  <div class="log" id="logBox"></div>
</div>

<div id="errorPanel"></div>

<div id="resultsSection"></div>

<div class="footer-note" id="downloadRow"></div>

</div>

<script>
let currentJob = null;
let pollTimer = null;
let lastResults = null;

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
    const resp = await fetch('/api/scrape', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({niche, location, limit, ...getKeys()})});
    const data = await resp.json();
    if(!data.ok){ errBox.innerHTML = `<div class="error-box">${esc(data.error)}</div>`; return; }
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
    errBox.innerHTML = `<div class="error-box">Request failed: ${esc(e.message)}</div>`;
  }finally{
    btn.disabled = false; btn.textContent = 'Preview leads';
  }
}

async function runPipeline(){
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

  const resp = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({niche, location, limit, top, language, ...keys})});
  const data = await resp.json();
  if(!data.ok){ errBox.innerHTML = `<div class="error-box">${esc(data.error)}</div>`; document.getElementById('runBtn').disabled = false; return; }
  currentJob = data.job_id;
  let shownLogCount = 0;
  const runStartedAt = Date.now();
  const MAX_WAIT_MS = 6 * 60 * 1000; // 6 minutes - long enough for a big batch, short enough to catch a real hang
  pollTimer = setInterval(async () => {
    let job;
    try{
      const r = await fetch(`/api/run/${currentJob}`);
      job = await r.json();
    }catch(e){
      clearInterval(pollTimer);
      document.getElementById('runBtn').disabled = false;
      errBox.innerHTML = `<div class="error-box">Lost connection while checking progress: ${esc(e.message)}. The server may have restarted — try running the pipeline again, maybe with fewer leads.</div>`;
      return;
    }
    if(!job.ok){
      clearInterval(pollTimer);
      document.getElementById('runBtn').disabled = false;
      errBox.innerHTML = `<div class="error-box">Lost track of this run (the server likely restarted mid-job, which can happen on a free hosting tier). Nothing wrong with your keys or data — just click "Run full pipeline" again. If it keeps happening, try scanning fewer leads at once.</div>`;
      return;
    }
    renderLog(job.log, shownLogCount);
    shownLogCount = job.log.length;
    const stages = job.log.map(l => l.stage);
    updateRail(stages.length ? Math.max(...stages) : 0, job.status);
    if(job.status === 'done'){
      clearInterval(pollTimer);
      document.getElementById('runBtn').disabled = false;
      renderResults(job.results, niche, location);
    } else if(job.status === 'error'){
      clearInterval(pollTimer);
      document.getElementById('runBtn').disabled = false;
      errBox.innerHTML = `<div class="error-box">${esc(job.error)}</div>`;
    } else if(Date.now() - runStartedAt > MAX_WAIT_MS){
      clearInterval(pollTimer);
      document.getElementById('runBtn').disabled = false;
      errBox.innerHTML = `<div class="error-box">This run has been going for over 6 minutes with no result, which usually means something got stuck server-side rather than genuinely still working. Try again with fewer "leads to scan" (e.g. 10 instead of 20).</div>`;
    }
  }, 1200);
}

function resetRail(){
  document.querySelectorAll('.stage').forEach(s => s.classList.remove('active','done'));
  document.getElementById('railFill').style.width = '0%';
}
function updateRail(maxStage, status){
  document.querySelectorAll('.stage').forEach(s => {
    const n = parseInt(s.dataset.stage);
    s.classList.remove('active','done');
    if(n < maxStage || (status === 'done')) s.classList.add('done');
    else if(n === maxStage) s.classList.add('active');
  });
  const pct = status === 'done' ? 100 : ((maxStage - 1) / 4) * 100;
  document.getElementById('railFill').style.width = pct + '%';
}
function renderLog(log, fromIndex){
  const box = document.getElementById('logBox');
  for(let i = fromIndex; i < log.length; i++){
    const div = document.createElement('div');
    div.className = 's' + log[i].stage;
    div.textContent = `[${log[i].stage}] ${log[i].message}`;
    box.appendChild(div);
  }
  box.scrollTop = box.scrollHeight;
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
    const waLink = waNumber ? `https://wa.me/${waNumber}?text=${encodeURIComponent(firstMsg)}` : null;
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

        <div class="subhead">Build prompt <button class="ghost small" id="build-btn-${idx}" onclick="copyText('build-${idx}')">Copy</button></div>
        <div class="codebox" id="build-${idx}" data-raw="${esc(r.build_prompt)}">${esc(r.build_prompt)}</div>

        <div class="subhead">Outreach (${document.getElementById('language').value}) <button class="ghost small" onclick="copyText('outreach-${idx}')">Copy</button></div>
        <div class="outreach-box" id="outreach-${idx}" data-raw="${esc(r.outreach)}">${esc(r.outreach)}</div>
        ${waLink ? `<a class="wa-link" href="${waLink}" target="_blank">Open in WhatsApp &rarr;</a>` : ''}
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
    md += `\n**Build prompt:**\n\`\`\`\n${r.build_prompt}\n\`\`\`\n`;
    md += `\n**Outreach:**\n${r.outreach}\n`;
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
    port = int(__import__("os").environ.get("PORT", 5050))
    print(f"Lead Pipeline running at http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
