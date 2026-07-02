"""
Web Dashboard — versione browser della dashboard, basata su http.server (stdlib).

Zero dipendenze esterne. Serve una pagina HTML che, via JavaScript, interroga
ogni N secondi l'endpoint /api/state e ridisegna i pannelli leggendo
state_snapshot.json (scritto dal bot ad ogni ciclo).

Avvio:
    python main.py dashboard --web                 # http://127.0.0.1:8787
    python main.py dashboard --web --port 9000
"""
from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logger = logging.getLogger("regime-trader")

# Pagina servita su "/". Il JavaScript fa polling su /api/state.
# Design: base neutra scura, UN solo colore accento (blu elettrico), verde/rosso
# riservati alla semantica P&L, numeri in monospace, icone SVG inline (no emoji).
_HTML_PAGE = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Regime Trader — Dashboard</title>
<style>
  :root {
    --bg:#0a0c10; --surface:#10141b; --surface2:#161c26; --border:#1f2733;
    --border-soft:rgba(31,39,51,.55);
    --text:#e8edf3; --muted:#76879c; --faint:#4a5868;
    --accent:#4cc2ff;
    --green:#2fd180; --red:#ff5d5d; --yellow:#e8b341;
    --mono:"JetBrains Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  html { scrollbar-color:var(--border) var(--bg); }
  body { background:var(--bg); color:var(--text); font-size:14.5px; line-height:1.45;
         font-family:"Geist",-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
         -webkit-font-smoothing:antialiased; }
  .num, td.num, .metric .val, .mono { font-family:var(--mono); font-variant-numeric:tabular-nums; }
  svg { display:block; }

  /* ── Header sticky ─────────────────────────────────────────────── */
  header { position:sticky; top:0; z-index:20; background:rgba(10,12,16,.86);
           backdrop-filter:blur(10px); border-bottom:1px solid var(--border);
           padding:0 22px; height:64px; display:flex; align-items:center; gap:26px; }
  .logo { display:flex; align-items:center; gap:9px; font-weight:650; font-size:14px;
          letter-spacing:.14em; white-space:nowrap; }
  .logo svg { color:var(--accent); }
  .logo b { color:var(--text); font-weight:650; } .logo span { color:var(--muted); font-weight:450; }
  .hsep { width:1px; height:26px; background:var(--border); }
  .metric { display:flex; flex-direction:column; gap:1px; white-space:nowrap; }
  .metric .lbl { font-size:11px; text-transform:uppercase; color:var(--muted); letter-spacing:.09em; }
  .metric .val { font-size:17px; font-weight:600; display:flex; align-items:center; gap:7px; }
  .metric .sub { font-size:12.5px; color:var(--muted); font-weight:450; }
  .spacer { flex:1; }
  .pill { padding:4px 12px; border-radius:99px; font-weight:650; font-size:12px;
          letter-spacing:.06em; border:1px solid; }
  .pill.PAPER { color:var(--yellow); border-color:rgba(232,179,65,.4); background:rgba(232,179,65,.07); }
  .pill.LIVE  { color:var(--red); border-color:rgba(255,93,93,.4); background:rgba(255,93,93,.07); }
  .pill.DRYRUN{ color:var(--accent); border-color:rgba(76,194,255,.4); background:rgba(76,194,255,.07); }
  .conn { display:flex; align-items:center; gap:7px; font-size:12.5px; color:var(--muted); }
  .dot { width:7px; height:7px; border-radius:50%; background:var(--faint); }
  .dot.ok { background:var(--green); animation:pulse 2.2s ease-out infinite; }
  .dot.stale { background:var(--yellow); }
  .dot.down { background:var(--red); }
  @keyframes pulse { 0% { box-shadow:0 0 0 0 rgba(47,209,128,.45); }
                     70% { box-shadow:0 0 0 7px rgba(47,209,128,0); }
                     100% { box-shadow:0 0 0 0 rgba(47,209,128,0); } }

  /* ── Griglia ───────────────────────────────────────────────────── */
  .wrap { max-width:1400px; margin:0 auto; padding:18px 22px 40px;
          display:grid; gap:14px; grid-template-columns:repeat(12,1fr); }
  .panel { background:var(--surface); border:1px solid var(--border); border-radius:10px;
           padding:16px 18px; min-width:0; }
  .s12 { grid-column:span 12; } .s8 { grid-column:span 8; } .s7 { grid-column:span 7; }
  .s5 { grid-column:span 5; } .s4 { grid-column:span 4; }
  @media (max-width:1080px){ .s8,.s7,.s5,.s4 { grid-column:span 12; }
    header { gap:16px; overflow-x:auto; } }
  .panel h2 { font-size:11.5px; text-transform:uppercase; letter-spacing:.12em; color:var(--muted);
              font-weight:600; margin-bottom:13px; display:flex; align-items:center; gap:8px; }
  .panel h2 svg { color:var(--faint); }
  .panel h2 .right { margin-left:auto; font-size:12.5px; text-transform:none; letter-spacing:0;
                     color:var(--muted); font-weight:450; }

  /* stagger di ingresso (solo primo render) */
  .reveal .panel { opacity:0; transform:translateY(6px);
                   animation:rise .45s cubic-bezier(.16,1,.3,1) forwards;
                   animation-delay:calc(var(--i)*70ms); }
  @keyframes rise { to { opacity:1; transform:none; } }

  /* ── Sparkline equity ──────────────────────────────────────────── */
  .spark { width:100%; height:120px; }
  .spark-meta { display:flex; gap:22px; margin-top:10px; flex-wrap:wrap; }
  .kv { display:flex; flex-direction:column; gap:1px; }
  .kv .k { font-size:11px; text-transform:uppercase; letter-spacing:.09em; color:var(--muted); }
  .kv .v { font-size:15px; font-weight:600; }

  /* ── Posizioni ─────────────────────────────────────────────────── */
  .pos { padding:11px 0; border-bottom:1px solid var(--border-soft); }
  .pos:last-child { border-bottom:none; padding-bottom:2px; }
  .pos-head { display:flex; justify-content:space-between; align-items:baseline; gap:10px; }
  .pos-sym { font-weight:650; font-size:17px; letter-spacing:.02em; }
  .pos-qty { color:var(--muted); font-size:13px; margin-left:9px; }
  .pos-pnl { font-weight:600; font-size:16px; }
  .pos-sub { display:flex; justify-content:space-between; font-size:13px; color:var(--muted); margin-top:5px; gap:10px; }
  .barwrap { height:5px; background:var(--bg); border-radius:3px; margin-top:8px; overflow:hidden; position:relative; }
  .barwrap > .mid { position:absolute; left:50%; top:0; bottom:0; width:1px; background:var(--border); }
  .barwrap > span { position:absolute; top:0; bottom:0; border-radius:3px; }

  /* ── Tabelle ───────────────────────────────────────────────────── */
  table { width:100%; border-collapse:collapse; }
  th { color:var(--faint); text-transform:uppercase; font-size:11px; letter-spacing:.1em;
       font-weight:600; text-align:left; padding:0 10px 9px 0; border-bottom:1px solid var(--border); }
  td { padding:10px 10px 10px 0; border-bottom:1px solid var(--border-soft); white-space:nowrap; font-size:14px; }
  tr:last-child td { border-bottom:none; }
  tbody tr { transition:background .15s ease; }
  tbody tr:hover { background:rgba(76,194,255,.035); }
  td.num, th.num { text-align:right; }
  .status { display:inline-flex; align-items:center; padding:3px 9px; border-radius:5px;
            border:1px solid var(--border); font-size:12px; font-weight:650; letter-spacing:.04em;
            color:var(--muted); background:var(--surface2); }
  .status.ok { color:var(--green); border-color:rgba(47,209,128,.45); background:rgba(47,209,128,.06); }
  .status.warn { color:var(--yellow); border-color:rgba(232,179,65,.45); background:rgba(232,179,65,.06); }
  .status.skip { color:var(--faint); }

  /* ── Eventi ────────────────────────────────────────────────────── */
  .ev { display:flex; align-items:center; gap:11px; padding:8px 0; font-size:14px;
        border-bottom:1px solid var(--border-soft); }
  .ev:last-child { border-bottom:none; }
  .ev .t { color:var(--faint); font-size:12.5px; min-width:62px; font-family:var(--mono); }
  .ev .tag { font-size:11px; font-weight:650; letter-spacing:.05em; padding:3px 0; border-radius:4px;
             min-width:60px; text-align:center; border:1px solid var(--border); color:var(--muted); }
  .tag.BUY  { color:var(--green); border-color:rgba(47,209,128,.45); background:rgba(47,209,128,.06); }
  .tag.SELL,.tag.CLOSE { color:var(--accent); border-color:rgba(76,194,255,.45); background:rgba(76,194,255,.06); }
  .tag.STOP { color:var(--yellow); border-color:rgba(232,179,65,.45); background:rgba(232,179,65,.06); }
  .tag.SKIP,.tag.REJECT { color:var(--faint); }
  .ev .sym { font-weight:650; min-width:50px; }
  .ev .d { color:var(--muted); font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

  /* ── Righe chiave/valore, barre rischio, chip ──────────────────── */
  .row { display:flex; justify-content:space-between; align-items:center; padding:7px 0;
         border-bottom:1px solid var(--border-soft); font-size:14px; }
  .row:last-child { border-bottom:none; }
  .muted { color:var(--muted); }
  .green { color:var(--green); } .red { color:var(--red); } .yellow { color:var(--yellow); }
  .small { font-size:13px; }
  .bar2 { height:7px; background:var(--bg); border-radius:4px; overflow:hidden; margin:5px 0 12px; }
  .bar2 > span { display:block; height:100%; border-radius:4px;
                 transition:width .6s cubic-bezier(.16,1,.3,1); }
  .chips { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:13px; }
  .chip { padding:4px 10px; border-radius:5px; font-size:12.5px; font-weight:550;
          font-family:var(--mono); background:var(--surface2);
          border:1px solid var(--border); color:var(--muted); }
  .chip.active { color:var(--green); border-color:rgba(47,209,128,.5); }
  .chip.first { color:var(--accent); border-color:rgba(76,194,255,.5); }

  /* ── Stati vuoti / offline ─────────────────────────────────────── */
  .empty { display:flex; flex-direction:column; align-items:center; gap:8px;
           padding:26px 10px; color:var(--faint); text-align:center; }
  .empty svg { opacity:.5; }
  .empty .hint { font-size:13px; color:var(--faint); }
  #offline { max-width:380px; margin:90px auto; text-align:center; color:var(--muted);
             display:flex; flex-direction:column; align-items:center; gap:14px; }
  #offline svg { color:var(--faint); animation:breathe 2.6s ease-in-out infinite; }
  @keyframes breathe { 50% { opacity:.35; } }
  #offline code { font-family:var(--mono); background:var(--surface2); border:1px solid var(--border);
                  padding:2px 7px; border-radius:5px; font-size:13.5px; color:var(--text); }
</style>
</head>
<body>
<header>
  <span class="logo">
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M3 17 8 11l4 3 6-8"/><path d="M15 6h3v3"/>
    </svg>
    <b>REGIME</b><span>TRADER</span>
  </span>
  <div class="hsep"></div>
  <div class="metric"><span class="lbl">Regime</span><span class="val" id="m-regime">—</span></div>
  <div class="metric"><span class="lbl">Equity</span><span class="val" id="m-equity">—</span></div>
  <div class="metric"><span class="lbl">P&amp;L oggi</span><span class="val" id="m-pnl">—</span></div>
  <div class="metric"><span class="lbl">Esposizione</span><span class="val" id="m-exp">—</span></div>
  <div class="spacer"></div>
  <span class="pill" id="m-mode" style="display:none"></span>
  <span class="conn"><span class="dot" id="m-dot"></span><span id="m-upd">—</span></span>
</header>
<div id="content"></div>

<script>
const REFRESH_MS = 5000;
const usd  = v => "$" + (v||0).toLocaleString("en-US",{maximumFractionDigits:0});
const usd2 = v => "$" + (v||0).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const pct  = v => ((v>=0?"+":"") + (100*(v||0)).toFixed(2) + "%");
const cls  = v => v>=0 ? "green" : "red";

/* Icone SVG inline (stroke 1.5, 14px) — niente emoji */
const I = s => `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${s}</svg>`;
const ICON = {
  positions: I('<path d="M3 17 8 11l4 3 6-8"/><path d="M3 21h18"/>'),
  scan:      I('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>'),
  events:    I('<path d="M13 2 4 14h6l-1 8 9-12h-6l1-8z"/>'),
  shield:    I('<path d="M12 22s8-3.5 8-10V5l-8-3-8 3v7c0 6.5 8 10 8 10z"/>'),
  globe:     I('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>'),
  equity:    I('<path d="M3 12h4l3-8 4 16 3-8h4"/>'),
};

/* Storia equity lato client per la sparkline (max ~1h a 5s) */
let eqHist = [];
let lastSavedAt = null;
let firstRender = true;

function ring(p){
  const r=8.5, c=2*Math.PI*r;
  const col = p>=0.70 ? "var(--green)" : (p>=0.55 ? "var(--yellow)" : "var(--red)");
  return `<svg width="22" height="22" viewBox="0 0 22 22">
    <circle cx="11" cy="11" r="${r}" fill="none" stroke="var(--border)" stroke-width="2.6"/>
    <circle cx="11" cy="11" r="${r}" fill="none" stroke="${col}" stroke-width="2.6"
      stroke-dasharray="${(p*c).toFixed(1)} ${c.toFixed(1)}" stroke-linecap="round"
      transform="rotate(-90 11 11)"/>
  </svg>`;
}

function sparkline(){
  if (eqHist.length < 2)
    return `<div class="empty">${ICON.equity}<div>Raccolgo i punti equity…</div>
            <div class="hint">il grafico si costruisce a ogni aggiornamento (${REFRESH_MS/1000}s)</div></div>`;
  const w=600, h=120, pad=6;
  const vals = eqHist.map(p=>p.v);
  const min=Math.min(...vals), max=Math.max(...vals), range=(max-min)||1;
  const pts = eqHist.map((p,i)=>[
    pad + i*(w-2*pad)/(eqHist.length-1),
    h-pad - (p.v-min)*(h-2*pad)/range,
  ]);
  const line = pts.map(p=>p[0].toFixed(1)+","+p[1].toFixed(1)).join(" ");
  const up = vals[vals.length-1] >= vals[0];
  const col = up ? "var(--green)" : "var(--red)";
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <polygon points="${pad},${h-pad} ${line} ${w-pad},${h-pad}" fill="${col}" opacity="0.08"/>
    <polyline points="${line}" fill="none" stroke="${col}" stroke-width="1.8"
      stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

function pnlBar(pnlpc){
  const r = Math.max(-1, Math.min(1, (pnlpc||0)/0.05));   // scala ±5%
  if (r >= 0) return `<span style="left:50%;width:${(r*50).toFixed(1)}%;background:var(--green)"></span>`;
  return `<span style="right:50%;width:${(-r*50).toFixed(1)}%;background:var(--red)"></span>`;
}

function riskBar(value, limit){
  const ratio = limit>0 ? Math.min(value/limit,1) : 0;
  const color = ratio<0.5 ? "var(--green)" : (ratio<0.8 ? "var(--yellow)" : "var(--red)");
  return `<div class="bar2"><span style="width:${(100*ratio).toFixed(0)}%;background:${color}"></span></div>`;
}

function statusClass(status){
  if (["APERTO","IN TARGET","AUMENTA"].includes(status)) return "ok";
  if ((status||"").startsWith("SKIP")) return "skip";
  return "warn";
}

function emptyState(icon, text, hint){
  return `<div class="empty">${icon}<div>${text}</div>${hint?`<div class="hint">${hint}</div>`:""}</div>`;
}

function render(data){
  const d = data.dashboard || {};
  const positions = data.positions || {};
  const equity = d.equity || 0;

  /* ── Header ── */
  const prob = d.probability || 0;
  document.getElementById("m-regime").innerHTML =
    `${ring(prob)} ${d.regime||"?"} <span class="sub">${(100*prob).toFixed(0)}%</span>`;
  document.getElementById("m-equity").textContent = usd(equity);
  const pnlEl = document.getElementById("m-pnl");
  pnlEl.textContent = (d.daily_pnl>=0?"+":"") + usd(d.daily_pnl) + " (" + pct(d.daily_pnl_pct) + ")";
  pnlEl.className = "val " + cls(d.daily_pnl);

  let exposure = 0;
  for (const p of Object.values(positions)) exposure += (p.qty||0)*(p.current_price||0);
  const expPc = equity>0 ? exposure/equity : 0;
  document.getElementById("m-exp").innerHTML =
    `${usd(exposure)} <span class="sub">${(100*expPc).toFixed(0)}%</span>`;

  const mode = d.trading_mode || "";
  const modeEl = document.getElementById("m-mode");
  if (mode){ modeEl.style.display=""; modeEl.textContent = mode; modeEl.className = "pill " + mode.replace("-",""); }

  /* Freschezza dati: verde <60s, giallo se stantio */
  const saved = data.saved_at || "";
  document.getElementById("m-upd").textContent = saved.replace("T"," ").substring(11,19) || "—";
  const ageS = saved ? (Date.now() - new Date(saved).getTime())/1000 : 1e9;
  document.getElementById("m-dot").className = "dot " + (ageS<60 ? "ok" : "stale");

  /* Storia equity per sparkline (un punto per snapshot nuovo) */
  if (saved && saved !== lastSavedAt && equity > 0){
    lastSavedAt = saved;
    eqHist.push({t:saved, v:equity});
    if (eqHist.length > 720) eqHist = eqHist.slice(-720);
  }

  /* ── Pannello equity ── */
  const vals = eqHist.map(p=>p.v);
  const eqMin = vals.length?Math.min(...vals):0, eqMax = vals.length?Math.max(...vals):0;
  const sessDelta = vals.length>1 ? vals[vals.length-1]-vals[0] : 0;
  const eqCard = `<div class="panel s8" style="--i:0"><h2>${ICON.equity} Equity — sessione dashboard
      <span class="right num ${cls(sessDelta)}">${vals.length>1 ? (sessDelta>=0?"+":"")+usd2(sessDelta) : ""}</span></h2>
    ${sparkline()}
    <div class="spark-meta">
      <div class="kv"><span class="k">Attuale</span><span class="v num">${usd2(equity)}</span></div>
      <div class="kv"><span class="k">Min</span><span class="v num">${vals.length?usd2(eqMin):"—"}</span></div>
      <div class="kv"><span class="k">Max</span><span class="v num">${vals.length?usd2(eqMax):"—"}</span></div>
      <div class="kv"><span class="k">Punti</span><span class="v num">${eqHist.length}</span></div>
    </div></div>`;

  /* ── Rischio ── */
  const cb = d.circuit_breaker || "NORMAL";
  const riskCard = `<div class="panel s4" style="--i:1"><h2>${ICON.shield} Rischio</h2>
    <div class="muted small">Drawdown giornaliero <span class="num">${(100*(d.daily_dd||0)).toFixed(1)}%</span> / <span class="num">${(100*(d.daily_dd_limit||0.03)).toFixed(0)}%</span></div>
    ${riskBar(d.daily_dd||0, d.daily_dd_limit||0.03)}
    <div class="muted small">Drawdown dal picco <span class="num">${(100*(d.peak_dd||0)).toFixed(1)}%</span> / <span class="num">${(100*(d.peak_dd_limit||0.1)).toFixed(0)}%</span></div>
    ${riskBar(d.peak_dd||0, d.peak_dd_limit||0.1)}
    <div class="row"><span class="muted">Circuit breaker</span><span class="${cb==='NORMAL'?'green':'red'}">${cb}</span></div>
    <div class="row"><span class="muted">Leva</span><span class="num">${(d.leverage||1).toFixed(2)}x</span></div>
    <div class="row"><span class="muted">Allocazione target</span><span class="num">${(100*(d.allocation_pct||0)).toFixed(0)}%</span></div>
  </div>`;

  /* ── Posizioni ── */
  let posHtml = "";
  const syms = Object.keys(positions);
  if (!syms.length){
    posHtml = emptyState(ICON.positions, "Nessuna posizione aperta — flat.",
                         "le posizioni compaiono qui appena il bot esegue un BUY");
  } else {
    for (const [sym,p] of Object.entries(positions)){
      const inv = (p.qty||0)*(p.avg_entry_price||0);
      const pnl = p.unrealized_pnl||0, pnlpc = p.unrealized_pnl_pct||0;
      const stop = p.stop_level ? usd2(p.stop_level) : '<span class="red">nessuno — verificare!</span>';
      const stopDist = p.stop_level && p.current_price ? ` <span class="muted">(${pct((p.stop_level-p.current_price)/p.current_price)})</span>` : "";
      posHtml += `
        <div class="pos">
          <div class="pos-head">
            <div><span class="pos-sym">${sym}</span><span class="pos-qty num">${(p.qty||0).toFixed(0)} az · ingr. ${usd2(p.avg_entry_price)}</span></div>
            <div class="pos-pnl num ${cls(pnl)}">${pct(pnlpc)}</div>
          </div>
          <div class="barwrap"><div class="mid"></div>${pnlBar(pnlpc)}</div>
          <div class="pos-sub">
            <span class="num">Prezzo ${usd2(p.current_price)} · Inv. ${usd(inv)}</span>
            <span class="num ${cls(pnl)}">${pnl>=0?"+":""}${usd2(pnl)}</span>
          </div>
          <div class="pos-sub"><span class="num">Stop: ${stop}${stopDist}</span><span>${p.regime_at_entry||""}</span></div>
        </div>`;
    }
  }
  const posCard = `<div class="panel s12" style="--i:2"><h2>${ICON.positions} Posizioni aperte
      <span class="right num">${syms.length}</span></h2>${posHtml}</div>`;

  /* ── Titoli valutati ── */
  const considered = d.considered_signals || [];
  let consideredHtml = "";
  if (!considered.length){
    consideredHtml = emptyState(ICON.scan, "Nessun titolo valutato nell'ultimo ciclo.",
                                "la pipeline gira a ogni barra chiusa del timeframe");
  } else {
    consideredHtml = `<table>
      <thead><tr>
        <th>Titolo</th><th>Stato</th><th class="num">Prezzo</th><th class="num">&Delta; prezzo</th>
        <th class="num">Peso</th><th class="num">Target</th><th class="num">Stop</th>
      </tr></thead><tbody>`;
    for (const s of considered){
      const change = s.price_change_pct || 0;
      const stop = s.active_stop || s.signal_stop || 0;
      consideredHtml += `<tr>
        <td><strong>${s.symbol}</strong></td>
        <td><span class="status ${statusClass(s.status)}">${s.status || "—"}</span></td>
        <td class="num">${usd2(s.price)}</td>
        <td class="num ${cls(change)}">${pct(change)}</td>
        <td class="num">${(100*(s.current_weight||0)).toFixed(1)}%</td>
        <td class="num">${(100*(s.target_weight||0)).toFixed(1)}%</td>
        <td class="num">${stop ? usd2(stop) : '<span class="red">nessuno</span>'}</td>
      </tr>`;
    }
    consideredHtml += "</tbody></table>";
  }
  const consideredCard = `<div class="panel s12" style="--i:3"><h2>${ICON.scan} Titoli valutati nell'ultimo ciclo</h2>${consideredHtml}</div>`;

  /* ── Eventi ── */
  let evHtml = "";
  const events = d.events || [];
  if (!events.length){
    evHtml = emptyState(ICON.events, "Nessun evento ancora.",
                        "qui compaiono BUY, SELL, CLOSE, STOP, SKIP e REJECT");
  } else {
    for (const e of events.slice(0,15)){
      evHtml += `<div class="ev">
        <span class="t">${e.time||""}</span>
        <span class="tag ${e.kind}">${e.kind}</span>
        <span class="sym">${e.symbol||""}</span>
        <span class="d">${(e.detail||"").substring(0,70)}</span>
      </div>`;
    }
  }
  const evCard = `<div class="panel s7" style="--i:4"><h2>${ICON.events} Eventi</h2>${evHtml}</div>`;

  /* ── Universo & sistema ── */
  const chips = (d.symbols||[]).map((s,i) => {
    const c = positions[s] ? "active" : (i===0 ? "first" : "");
    return `<span class="chip ${c}">${s}</span>`;
  }).join("");
  const sysCard = `<div class="panel s5" style="--i:5"><h2>${ICON.globe} Universo &amp; sistema</h2>
    <div class="chips">${chips||'<span class="muted">—</span>'}</div>
    <div class="row"><span class="muted">Titoli in portafoglio</span><span class="num">${syms.length} / ${(d.symbols||[]).length}</span></div>
    <div class="row"><span class="muted">Flicker regime</span><span class="num">${(d.flicker_rate||0).toFixed(0)} / ${d.flicker_window||20}</span></div>
    <div class="row"><span class="muted">Data feed</span><span class="${d.data_feed_ok?'green':'red'}">${d.data_feed_ok?'ok':'down'}</span></div>
    <div class="row"><span class="muted">API Alpaca</span><span class="${d.api_ok?'green':'red'}">${d.api_ok?'ok':'persa'}</span></div>
    <div class="row"><span class="muted">Modello HMM</span><span>${d.hmm_age_str||'?'}</span></div>
  </div>`;

  document.getElementById("content").innerHTML =
    `<div class="wrap ${firstRender?"reveal":""}">${eqCard}${riskCard}${posCard}${consideredCard}${evCard}${sysCard}</div>`;
  firstRender = false;
}

async function poll(){
  const dot = document.getElementById("m-dot");
  try {
    const r = await fetch("/api/state", {cache:"no-store"});
    if (r.status === 204){
      document.getElementById("content").innerHTML = `<div id="offline">
        ${I('<path d="M3 12h4l3-8 4 16 3-8h4"/>').replace('width="14" height="14"','width="34" height="34"')}
        <div>In attesa dello snapshot del bot…</div>
        <div class="small">Avvia con <code>./start.sh dry</code> o <code>./start.sh live</code></div></div>`;
      dot.className = "dot stale";
      return;
    }
    render(await r.json());
  } catch(e){
    document.getElementById("content").innerHTML = `<div id="offline">
      ${I('<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/>').replace('width="14" height="14"','width="34" height="34"')}
      <div>Connessione al server della dashboard persa.</div>
      <div class="small">Riavvia con <code>./start.sh web</code></div></div>`;
    dot.className = "dot down";
    firstRender = true;
  }
}
poll();
setInterval(poll, REFRESH_MS);
</script>
</body>
</html>"""


def _make_handler(snapshot_path: Path):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silenzia il log di default
            pass

        def _send(self, code: int, body: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/index"):
                self._send(200, _HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path.startswith("/api/state"):
                if not snapshot_path.exists():
                    self._send(204, b"", "application/json")
                    return
                try:
                    data = snapshot_path.read_bytes()
                    self._send(200, data, "application/json")
                except Exception:
                    self._send(204, b"", "application/json")
            else:
                self._send(404, b"not found", "text/plain")

    return _Handler


def serve(snapshot_path: Path, host: str = "127.0.0.1", port: int = 8787) -> None:
    """Avvia il server web della dashboard (bloccante). Ctrl+C per fermare."""
    handler = _make_handler(snapshot_path)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}"
    print(f"\n🌐 Dashboard web attiva su {url}")
    print("   Aprila nel browser. Ctrl+C per fermare.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard web fermata.")
    finally:
        httpd.server_close()
