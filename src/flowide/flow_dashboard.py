#!/usr/bin/env python3
from datetime import datetime, timezone
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time

DEFAULT_DB = Path(os.environ.get("FLOW_DB_PATH", "flow_substrate.db"))
# Default bind is 0.0.0.0 so the dashboard is reachable from other machines on
# the LAN -- a localhost-only monitor "does me no good" if it cannot be opened
# from the network (Auteur directive, 2026-11-16). Set FLOW_DASH_HOST=127.0.0.1
# to restrict to loopback. The dashboard has NO authentication: trusted LAN only.
BIND_HOST = os.environ.get("FLOW_DASH_HOST", "0.0.0.0")
# Auteur's number (9898). Override with FLOW_DASH_PORT.
BIND_PORT = int(os.environ.get("FLOW_DASH_PORT", "9898"))

# Modules live beside this file (both in the repo tree and in the installed
# ~/.local/share/flow-ide copy) -- exactly how cli.py locates them.
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPO = os.environ.get("FLOW_REPO_ROOT", "/home/pdt/flowide")
# Action endpoints run repo operations as the invoking user. Fence the input:
# a repo path must resolve to an existing directory INSIDE these roots.
ALLOWED_ROOTS: Tuple[Path, ...] = (Path("/home/pdt"), Path("/tmp/opencode"))
OUTPUT_CAP = 4000
# Long actions must never pin an HTTP thread forever.
ACTION_DEFAULT_TIMEOUT = 60
ACTION_TIMEOUTS = {"slice": 120, "hop": 120}


class ActionError(Exception):
    """Raised when an action's input is rejected before it ever runs."""


def resolve_repo(payload: Dict[str, Any]) -> Path:
    """Validate the repo argument. Raises ActionError with a human reason."""
    raw = str(payload.get("repo") or DEFAULT_REPO).strip()
    if not raw:
        raise ActionError("repo is empty")
    try:
        candidate = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ActionError(f"repo path unusable: {exc}") from exc
    if not candidate.exists():
        raise ActionError(f"repo does not exist: {candidate}")
    if not candidate.is_dir():
        raise ActionError(f"repo is not a directory: {candidate}")
    allowed = tuple(root.resolve() for root in ALLOWED_ROOTS)
    if not any(candidate == root or root in candidate.parents for root in allowed):
        raise ActionError(
            "repo outside allowed roots: "
            + ", ".join(str(r) for r in allowed)
            + f" (got {candidate})"
        )
    return candidate


def _run(argv: List[str], timeout: int, cwd: Optional[Path] = None) -> Dict[str, Any]:
    """Run one action as an argv list (never a shell string)."""
    env = os.environ.copy()
    env["FLOW_DB_PATH"] = str(FlowDashboardHandler.db_path)
    env["PYTHONUNBUFFERED"] = "1"
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(cwd) if cwd else None,
        )
        combined = (proc.stdout or "") + ("\n" if proc.stdout and proc.stderr else "") + (proc.stderr or "")
        return {
            "ok": proc.returncode == 0,
            "rc": proc.returncode,
            "output": combined[-OUTPUT_CAP:] or "(no output)",
            "seconds": round(time.monotonic() - started, 2),
        }
    except subprocess.TimeoutExpired as exc:
        partial = ""
        for stream in (exc.stdout, exc.stderr):
            if isinstance(stream, bytes):
                partial += stream.decode("utf-8", "replace")
            elif isinstance(stream, str):
                partial += stream
        return {
            "ok": False,
            "rc": -1,
            "output": (f"TIMEOUT: action exceeded {timeout}s and was killed.\n" + partial)[-OUTPUT_CAP:],
            "seconds": round(time.monotonic() - started, 2),
        }
    except FileNotFoundError as exc:
        return {"ok": False, "rc": -1, "output": f"executable/script not found: {exc}", "seconds": 0}
    except OSError as exc:
        return {"ok": False, "rc": -1, "output": f"could not run action: {exc}", "seconds": 0}


def action_init(payload: Dict[str, Any]) -> Dict[str, Any]:
    repo = resolve_repo(payload)
    return _run([sys.executable, str(SCRIPT_DIR / "cli.py"), "init", str(repo)], ACTION_DEFAULT_TIMEOUT)


def action_slice(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Decompose <repo> into <=150 LOC roadmap_nodes INSIDE THIS CONSOLE'S
    substrate DB. Same decomposer module `flow slice` runs; the only difference
    is the db target, so the board you are looking at is the board that fills."""
    repo = resolve_repo(payload)
    return _run(
        [sys.executable, str(SCRIPT_DIR / "decomposer.py"), str(repo), str(FlowDashboardHandler.db_path)],
        ACTION_TIMEOUTS["slice"],
    )


def action_hop(payload: Dict[str, Any]) -> Dict[str, Any]:
    repo = resolve_repo(payload)
    result = _run([sys.executable, str(SCRIPT_DIR / "flow_runner.py"), str(repo)], ACTION_TIMEOUTS["hop"], cwd=repo)
    if not result["ok"] and ("Connection refused" in result["output"] or "URLError" in result["output"]):
        hint = (
            "HOP needs a local SLM endpoint (FLOW_LLM_URL, default http://127.0.0.1:11434/api/generate). "
            "Nothing is listening there, so the hop could not run. Every other action works without it.\n\n"
        )
        result["output"] = (hint + result["output"])[-OUTPUT_CAP:]
    return result


def action_sweep(payload: Dict[str, Any]) -> Dict[str, Any]:
    repo = resolve_repo(payload)
    return _run(
        [sys.executable, str(SCRIPT_DIR / "airgap_sweeper.py"), str(repo), str(FlowDashboardHandler.db_path), "--once"],
        ACTION_DEFAULT_TIMEOUT,
        cwd=repo,
    )


def action_audit(payload: Dict[str, Any]) -> Dict[str, Any]:
    resolve_repo(payload)
    return _run(
        [sys.executable, str(SCRIPT_DIR / "flow_audit.py"), "--db", str(FlowDashboardHandler.db_path), "verify"],
        ACTION_DEFAULT_TIMEOUT,
    )


def action_export(payload: Dict[str, Any]) -> Dict[str, Any]:
    fmt = str(payload.get("format") or "markdown").strip().lower()
    if fmt not in ("markdown", "json"):
        raise ActionError("format must be 'markdown' or 'json'")
    resolve_repo(payload)
    return _run(
        [sys.executable, str(SCRIPT_DIR / "flow_audit.py"), "--db", str(FlowDashboardHandler.db_path), "export", "--format", fmt],
        ACTION_DEFAULT_TIMEOUT,
    )


def action_unstall(payload: Dict[str, Any]) -> Dict[str, Any]:
    apply_reset = bool(payload.get("apply"))
    resolve_repo(payload)
    argv = [sys.executable, str(SCRIPT_DIR / "flow_audit.py"), "--db", str(FlowDashboardHandler.db_path), "unstall"]
    if apply_reset:
        argv.append("--apply")
    return _run(argv, ACTION_DEFAULT_TIMEOUT)


ACTIONS = {
    "init": action_init,
    "slice": action_slice,
    "hop": action_hop,
    "sweep": action_sweep,
    "audit": action_audit,
    "export": action_export,
    "unstall": action_unstall,
}

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FLOW IDE // OPERATOR CONSOLE</title>
<style>
  :root {
    --bg: #0b0e11; --panel: #12171d; --border: #232d38; --fg: #d6e2ee;
    --muted: #6e8299; --active: #22bbff; --airgap: #f59e0b; --pass: #10b981; --fail: #ef4444;
    --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--fg); font-family: var(--mono); font-size: 13px; line-height: 1.45; padding: 16px; }
  header { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; border: 1px solid var(--border); background: var(--panel); padding: 12px 16px; margin-bottom: 16px; }
  .title { font-weight: 700; letter-spacing: 0.08em; color: var(--active); }
  .badge { padding: 3px 8px; border: 1px solid var(--border); font-size: 11px; text-transform: uppercase; }
  .badge.open { border-color: var(--pass); color: var(--pass); }
  .badge.hold { border-color: var(--airgap); color: var(--airgap); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 16px; margin-bottom: 16px; }
  .card { border: 1px solid var(--border); background: var(--panel); padding: 14px; }
  .card.console { border-color: var(--active); margin-bottom: 16px; }
  .card h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); border-bottom: 1px solid var(--border); padding-bottom: 8px; margin-bottom: 12px; }
  .dial-wrap { display: flex; align-items: center; gap: 20px; }
  .kv-list { width: 100%; }
  .kv-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px dashed #1c242d; }
  .kv-row span:first-child { color: var(--muted); }
  .kv-row span:last-child { font-weight: 600; }
  .bar-track { width: 100%; height: 10px; background: #1a222b; border: 1px solid var(--border); margin: 8px 0 12px; display: flex; }
  .bar-done { background: var(--pass); height: 100%; }
  .bar-inflight { background: var(--active); height: 100%; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #1b222b; vertical-align: top; }
  th { color: var(--muted); font-weight: 600; }
  .scar-pass { color: var(--pass); }
  .scar-fail { color: var(--fail); }
  .st-done { color: var(--pass); }
  .st-stalled { color: var(--airgap); }
  .st-in_flight { color: var(--active); }
  .st-pending { color: var(--muted); }
  .inject-form { display: grid; grid-template-columns: 90px 70px 1fr 1fr auto; gap: 8px; margin-top: 10px; }
  input, button, select { background: var(--bg); color: var(--fg); border: 1px solid var(--border); font-family: var(--mono); font-size: 12px; padding: 6px 8px; }
  button { cursor: pointer; border-color: var(--active); color: var(--active); font-weight: 600; }
  button:hover:not(:disabled) { background: rgba(34, 187, 255, 0.12); }
  button:disabled { opacity: 0.45; cursor: progress; border-color: var(--border); color: var(--muted); }
  button.warn { border-color: var(--airgap); color: var(--airgap); }
  .act-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .act-row input.repo { flex: 1 1 320px; min-width: 240px; }
  .act-buttons { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
  .hint { color: var(--muted); font-size: 11px; margin-top: 8px; }
  #log-pane { margin-top: 12px; border: 1px solid var(--border); background: #070a0d; }
  #log-pane summary { cursor: pointer; padding: 6px 8px; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }
  #log-body { max-height: 260px; overflow: auto; padding: 8px; white-space: pre-wrap; word-break: break-word; font-size: 11px; color: #a8bccd; }
  .log-err { color: var(--fail); }
  .log-ok { color: var(--pass); }
  .empty { color: var(--airgap); padding: 10px 8px; }
</style>
</head>
<body>
<header>
  <div><span class="title">FLOW IDE // OPERATOR CONSOLE</span><span style="color:var(--muted); margin-left:10px;">E_round = 0/1 | 235/19 Metonic</span></div>
  <div><span id="gate-badge" class="badge">POLLING...</span><span id="clock" style="color:var(--muted); margin-left:10px;">--:--:--</span></div>
</header>

<div class="card console">
  <h2>0. Actions — run the substrate from here</h2>
  <div class="act-row">
    <input type="text" id="act-repo" class="repo" value="__DEFAULT_REPO__" placeholder="/home/pdt/flowide" spellcheck="false">
    <span style="color:var(--muted); font-size:11px;">repo root</span>
  </div>
  <div class="act-buttons">
    <button id="btn-slice" data-action="slice">SLICE (decompose)</button>
    <button id="btn-init" data-action="init">INIT (.flow/)</button>
    <button id="btn-hop" data-action="hop">HOP (1/32 baton)</button>
    <button id="btn-sweep" data-action="sweep">SWEEP (5/32 airgap)</button>
    <button id="btn-audit" data-action="audit">AUDIT (sha256 chain)</button>
    <button id="btn-export" data-action="export">EXPORT (summary)</button>
    <button id="btn-unstall" data-action="unstall" class="warn">UNSTALL (inspect)</button>
  </div>
  <div class="hint">Slice writes the roadmap INTO THIS CONSOLE's substrate DB (it does not edit the target repo). Unstall is inspect-only unless you tick APPLY.</div>
  <label class="hint" style="display:flex; gap:6px; align-items:center;">
    <input type="checkbox" id="act-apply" style="width:auto;"> Unstall APPLY (resets stalled nodes to 0/1)
    <input type="checkbox" id="act-json" style="width:auto; margin-left:12px;"> Export as JSON
  </label>
  <details id="log-pane" open>
    <summary>Action log</summary>
    <div id="log-body">Idle. Press a button.</div>
  </details>
</div>

<div class="grid">
  <div class="card">
    <h2>1. Phase-Shift Dial (27/32 Active vs 5/32 Airgap)</h2>
    <div class="dial-wrap">
      <svg id="phase-svg" width="136" height="136" viewBox="0 0 136 136">
        <circle cx="68" cy="68" r="54" fill="none" stroke="#1e2731" stroke-width="12"/>
        <circle cx="68" cy="68" r="54" fill="none" stroke="#f59e0b" stroke-width="12" stroke-dasharray="53.01 339.29" stroke-dashoffset="-286.28" transform="rotate(-90 68 68)" opacity="0.35"/>
        <circle id="phase-arc" cx="68" cy="68" r="54" fill="none" stroke="#22bbff" stroke-width="12" stroke-dasharray="0 339.29" transform="rotate(-90 68 68)"/>
        <text id="dial-center" x="68" y="65" text-anchor="middle" fill="#d6e2ee" font-size="16" font-weight="bold">0/32</text>
        <text id="dial-sub" x="68" y="83" text-anchor="middle" fill="#6e8299" font-size="10">ACTIVE_27_32</text>
      </svg>
      <div class="kv-list">
        <div class="kv-row"><span>Accumulated Phase</span><span id="kv-phase">0/32</span></div>
        <div class="kv-row"><span>Active Cap / Void</span><span>27/32 | 5/32</span></div>
        <div class="kv-row"><span>Metonic Epoch Tick</span><span id="kv-metonic">0</span></div>
        <div class="kv-row"><span>Regime State</span><span id="kv-regime">ACTIVE_27_32</span></div>
      </div>
    </div>
  </div>
  <div class="card">
    <h2>2. Energy Harvester &amp; TOU Conservation (16:00-21:00)</h2>
    <div class="kv-list">
      <div class="kv-row"><span>Fuel Index I(t) [Gate >= 1/5]</span><span id="kv-fuel" style="color:var(--pass);">--</span></div>
      <div class="kv-row"><span>Thermal Ceiling C_t</span><span id="kv-therm">--</span></div>
      <div class="kv-row"><span>Allostatic Queue H_t</span><span id="kv-load">--</span></div>
      <div class="kv-row"><span>Grid / Battery Strain T_t</span><span id="kv-grid">--</span></div>
      <div class="kv-row"><span>Ground Capacitance G_t</span><span id="kv-ground">--</span></div>
      <div class="kv-row"><span>Probe / Sampled</span><span id="kv-probe">--</span></div>
    </div>
  </div>
  <div class="card">
    <h2>3. Central Roadmap (3200-Node Lattice)</h2>
    <div class="kv-row"><span>Completed Nodes (1/1)</span><span id="kv-nodes-done">0 / 0</span></div>
    <div class="bar-track"><div id="bar-done" class="bar-done" style="width:0%"></div><div id="bar-inflight" class="bar-inflight" style="width:0%"></div></div>
    <div class="kv-list">
      <div class="kv-row"><span>In-Flight Nodes (1/2)</span><span id="kv-nodes-active">0</span></div>
      <div class="kv-row"><span>Stalled Nodes (BSI >= 2)</span><span id="kv-nodes-stalled" style="color:var(--airgap);">0</span></div>
      <div class="kv-row"><span>Next Ready Coordinate</span><span id="kv-next-node">--</span></div>
    </div>
  </div>
</div>
<div class="card" style="margin-bottom:16px;">
  <h2>4. Live Relay Baton Stream (Stateless Handoffs &amp; 512B Epistemic Scars)</h2>
  <table><thead><tr><th>Baton ID</th><th>Node / 3200</th><th>Band Hop</th><th>Phase</th><th>Fuel I(t)</th><th>Touched Path</th><th>Epistemic Scar R(h)</th></tr></thead>
  <tbody id="baton-tbody"><tr><td colspan="7" style="color:var(--muted);">Waiting for first Relay Baton hop...</td></tr></tbody></table>
</div>
<div class="card" style="margin-bottom:16px;">
  <h2>5. Roadmap Nodes (GET /api/nodes)</h2>
  <table><thead><tr><th>Coord</th><th>Band</th><th>Status</th><th>Progress</th><th>Stalls</th><th>Target Path</th><th>Atomic Slice Title</th></tr></thead>
  <tbody id="roadmap-tbody"><tr><td colspan="7" class="empty">Board empty — run SLICE above.</td></tr></tbody></table>
</div>
<div class="card">
  <h2>6. Direct Coordinate Injection</h2>
  <form class="inject-form" onsubmit="injectNode(event)">
    <input type="number" id="in-node" min="0" max="3199" placeholder="Node (500-1349)" required>
    <input type="number" id="in-band" min="1" max="32" placeholder="Band" required>
    <input type="text" id="in-path" placeholder="Target path (e.g. src/parser.py)" required>
    <input type="text" id="in-title" placeholder="Atomic task title (<=150 LOC)" required>
    <button type="submit">+ INJECT NODE</button>
  </form>
</div>
<script>
const CIRCUMFERENCE = 2 * Math.PI * 54;
let actionInFlight = false;

function logLine(text, kind) {
  const body = document.getElementById('log-body');
  const stamp = new Date().toLocaleTimeString();
  const cls = kind === 'err' ? 'log-err' : (kind === 'ok' ? 'log-ok' : '');
  body.innerHTML = '<span class="' + cls + '">[' + stamp + '] ' + escapeHtml(text) + '</span>\n' + body.innerHTML;
  document.getElementById('log-pane').open = true;
}

function setBusy(busy, activeName) {
  actionInFlight = busy;
  document.querySelectorAll('[data-action]').forEach(btn => {
    btn.disabled = busy;
    if (busy && btn.dataset.action === activeName) {
      btn.dataset.relabel = btn.textContent;
      btn.textContent = '… RUNNING';
    } else if (btn.dataset.relabel) {
      btn.textContent = btn.dataset.relabel;
      delete btn.dataset.relabel;
    }
  });
}

async function runAction(name) {
  if (actionInFlight) return;
  const repo = document.getElementById('act-repo').value.trim();
  const payload = { repo };
  if (name === 'unstall') payload.apply = document.getElementById('act-apply').checked;
  if (name === 'export') payload.format = document.getElementById('act-json').checked ? 'json' : 'markdown';
  setBusy(true, name);
  logLine('POST /api/' + name + ' ' + JSON.stringify(payload));
  try {
    const res = await fetch('/api/' + name, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    });
    let data;
    try { data = await res.json(); }
    catch (parseErr) { data = { ok: false, rc: res.status, cmd: name, output: 'HTTP ' + res.status + ' — non-JSON response' }; }
    logLine((data.ok ? 'OK ' : 'FAIL ') + (data.cmd || name) + ' rc=' + data.rc + (data.seconds !== undefined ? ' (' + data.seconds + 's)' : '') + '\n' + (data.output || ''), data.ok ? 'ok' : 'err');
  } catch (err) {
    logLine('ERROR ' + name + ': ' + (err && err.message ? err.message : err), 'err');
  } finally {
    setBusy(false, name);
    refreshState();
  }
}

document.querySelectorAll('[data-action]').forEach(btn => {
  btn.addEventListener('click', () => runAction(btn.dataset.action));
});

async function refreshNodes() {
  try {
    const nodes = await (await fetch('/api/nodes')).json();
    const tbody = document.getElementById('roadmap-tbody');
    if (!Array.isArray(nodes) || nodes.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty">Board empty — run SLICE above.</td></tr>';
      return;
    }
    tbody.innerHTML = nodes.map(n => '<tr><td>' + escapeHtml(n.coord) + '</td><td>B' + escapeHtml(n.band) + '</td>'
      + '<td class="st-' + escapeHtml(n.status) + '">' + escapeHtml(n.status) + '</td>'
      + '<td>' + escapeHtml(n.progress) + '</td><td>' + escapeHtml(n.stall_count) + '</td>'
      + '<td>' + escapeHtml(n.target_path) + '</td><td>' + escapeHtml(n.title) + '</td></tr>').join('');
  } catch (err) { /* the live node table must never break the console */ }
}

async function refreshState() {
  try {
    const data = await (await fetch('/api/state')).json();
    document.getElementById('clock').textContent = new Date().toLocaleTimeString();
    const h = data.harvester;
    const badge = document.getElementById('gate-badge');
    badge.textContent = h.gate_status;
    badge.className = 'badge ' + (h.gate_status === 'OPEN_27_32' ? 'open' : 'hold');
    document.getElementById('kv-fuel').textContent = h.fuel;
    document.getElementById('kv-therm').textContent = h.thermal;
    document.getElementById('kv-load').textContent = h.allostatic;
    document.getElementById('kv-grid').textContent = h.grid_strain;
    document.getElementById('kv-ground').textContent = h.ground_cap;
    document.getElementById('kv-probe').textContent = h.platform + ' @ ' + ((h.sampled_at || '').split('T')[1] || '').slice(0, 8);
    const p = data.phase;
    document.getElementById('dial-center').textContent = p.accum_str;
    document.getElementById('dial-sub').textContent = p.regime_state;
    document.getElementById('kv-phase').textContent = p.accum_str;
    document.getElementById('kv-metonic').textContent = p.metonic_tick;
    document.getElementById('kv-regime').textContent = p.regime_state;
    const arcLen = Math.min(1, p.accum_num / p.accum_den) * CIRCUMFERENCE;
    const arcEl = document.getElementById('phase-arc');
    arcEl.setAttribute('stroke-dasharray', arcLen.toFixed(2) + ' ' + CIRCUMFERENCE.toFixed(2));
    arcEl.setAttribute('stroke', p.regime_state === 'AIRGAP_5_32' ? '#f59e0b' : '#22bbff');
    const rm = data.roadmap_summary;
    document.getElementById('kv-nodes-done').textContent = rm.done + ' / ' + rm.total;
    document.getElementById('kv-nodes-active').textContent = rm.inflight;
    document.getElementById('kv-nodes-stalled').textContent = rm.stalled;
    document.getElementById('kv-next-node').textContent = rm.next_coord;
    document.getElementById('bar-done').style.width = (rm.total ? (rm.done / rm.total) * 100 : 0).toFixed(1) + '%';
    document.getElementById('bar-inflight').style.width = (rm.total ? (rm.inflight / rm.total) * 100 : 0).toFixed(1) + '%';
    if (data.batons.length > 0) {
      document.getElementById('baton-tbody').innerHTML = data.batons.map(b => {
        const cls = b.epistemic_scar.startsWith('[FAIL') ? 'scar-fail' : 'scar-pass';
        return '<tr><td>' + escapeHtml(b.baton_id) + '</td><td>' + escapeHtml(b.roadmap_node_id) + '/3200</td><td>B' + escapeHtml(b.from_band) + ' &rarr; B' + escapeHtml(b.to_band) + '</td><td>' + escapeHtml(b.phase_shift) + '</td><td>' + escapeHtml(b.fuel) + '</td><td>' + escapeHtml(b.touched_paths) + '</td><td class="' + cls + '">' + escapeHtml(b.epistemic_scar) + '</td></tr>';
      }).join('');
    }
    refreshNodes();
  } catch (err) {}
}

async function injectNode(ev) {
  ev.preventDefault();
  try {
    const res = await fetch('/api/nodes', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        node_id: parseInt(document.getElementById('in-node').value, 10),
        band: parseInt(document.getElementById('in-band').value, 10),
        target_path: document.getElementById('in-path').value.trim(),
        title: document.getElementById('in-title').value.trim()
      })
    });
    if (res.ok) { document.getElementById('in-title').value = ''; logLine('node injected', 'ok'); refreshState(); }
    else { logLine('inject failed: HTTP ' + res.status, 'err'); }
  } catch (err) { logLine('inject error: ' + (err && err.message ? err.message : err), 'err'); }
}

function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
refreshState(); setInterval(refreshState, 2000);
</script>
</body>
</html>
"""


def query_substrate(db_path: Path) -> Dict[str, Any]:
    if not db_path.exists():
        return {
            "harvester": {"fuel": "1/2", "thermal": "0/1", "allostatic": "0/1", "grid_strain": "0/1", "ground_cap": "1/1", "gate_status": "AWAITING_DB", "platform": "local", "sampled_at": ""},
            "phase": {"accum_num": 0, "accum_den": 32, "accum_str": "0/32", "metonic_tick": 0, "regime_state": "ACTIVE_27_32"},
            "roadmap_summary": {"total": 0, "done": 0, "inflight": 0, "stalled": 0, "next_coord": "--"},
            "batons": [], "nodes": [],
        }

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    def table_exists(name: str) -> bool:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?;", (name,)).fetchone() is not None

    harvester = {"fuel": "1/2", "thermal": "0/1", "allostatic": "0/1", "grid_strain": "0/1", "ground_cap": "1/1", "gate_status": "OPEN_27_32", "platform": "local", "sampled_at": ""}
    if table_exists("harvester_telemetry"):
        row = conn.execute("SELECT * FROM harvester_telemetry WHERE singleton_id = 1;").fetchone()
        if row:
            harvester = {
                "fuel": f"{Fraction(row['fuel_num'], row['fuel_den'])}",
                "thermal": f"{Fraction(row['thermal_num'], row['thermal_den'])}",
                "allostatic": f"{Fraction(row['allostatic_num'], row['allostatic_den'])}",
                "grid_strain": f"{Fraction(row['grid_strain_num'], row['grid_strain_den'])}",
                "ground_cap": f"{Fraction(row['ground_cap_num'], row['ground_cap_den'])}",
                "gate_status": row["gate_status"], "platform": row["platform_probe"], "sampled_at": row["sampled_at_iso"],
            }

    phase = {"accum_num": 0, "accum_den": 32, "accum_str": "0/32", "metonic_tick": 0, "regime_state": "ACTIVE_27_32"}
    if table_exists("phase_cycle_state"):
        row = conn.execute("SELECT * FROM phase_cycle_state WHERE singleton_id = 1;").fetchone()
        if row:
            phase = {"accum_num": row["accum_phase_num"], "accum_den": row["accum_phase_den"], "accum_str": f"{row['accum_phase_num']}/{row['accum_phase_den']}", "metonic_tick": row["metonic_tick"], "regime_state": row["regime_state"]}

    nodes_out: List[Dict[str, Any]] = []
    total = done = inflight = stalled = 0
    next_coord = "--"
    if table_exists("roadmap_nodes"):
        counts = conn.execute("""
            SELECT COUNT(*) AS total,
              SUM(CASE WHEN progress_num = progress_den THEN 1 ELSE 0 END) AS done,
              SUM(CASE WHEN progress_num > 0 AND progress_num < progress_den THEN 1 ELSE 0 END) AS inflight,
              SUM(CASE WHEN stall_count >= 2 THEN 1 ELSE 0 END) AS stalled
            FROM roadmap_nodes;
        """).fetchone()
        total, done, inflight, stalled = counts["total"] or 0, counts["done"] or 0, counts["inflight"] or 0, counts["stalled"] or 0
        nxt = conn.execute("SELECT node_id, band FROM roadmap_nodes WHERE progress_num = 0 ORDER BY node_id ASC LIMIT 1;").fetchone()
        if nxt:
            next_coord = f"{nxt['node_id']}/3200 (Band {nxt['band']})"
        for r in conn.execute("SELECT * FROM roadmap_nodes ORDER BY node_id ASC LIMIT 100;").fetchall():
            nodes_out.append({"node_id": r["node_id"], "band": r["band"], "progress": f"{r['progress_num']}/{r['progress_den']}", "stall_count": r["stall_count"], "target_path": r["target_path"], "title": r["title"]})

    batons_out: List[Dict[str, Any]] = []
    if table_exists("relay_batons"):
        for b in conn.execute("SELECT * FROM relay_batons ORDER BY rowid DESC LIMIT 15;").fetchall():
            batons_out.append({"baton_id": b["baton_id"], "roadmap_node_id": b["roadmap_node_id"], "from_band": b["from_band"], "to_band": b["to_band"], "phase_shift": f"{b['phase_shift_num']}/{b['phase_shift_den']}", "fuel": f"{Fraction(b['fuel_num'], b['fuel_den'])}", "touched_paths": b["touched_paths"], "epistemic_scar": b["epistemic_scar"]})

    conn.close()
    return {"harvester": harvester, "phase": phase, "roadmap_summary": {"total": total, "done": done, "inflight": inflight, "stalled": stalled, "next_coord": next_coord}, "batons": batons_out, "nodes": nodes_out}


def node_status(progress_num: int, progress_den: int, stall_count: int) -> str:
    if progress_den > 0 and progress_num >= progress_den:
        return "done"
    if stall_count >= 2:
        return "stalled"
    if progress_num > 0:
        return "in_flight"
    return "pending"


def query_nodes(db_path: Path) -> List[Dict[str, Any]]:
    """The full board, for GET /api/nodes."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='roadmap_nodes';").fetchone() is None:
            return []
        rows = conn.execute(
            "SELECT node_id, band, title, target_path, max_loc_budget, progress_num, progress_den, stall_count, updated_at_iso "
            "FROM roadmap_nodes ORDER BY node_id ASC;"
        ).fetchall()
    finally:
        conn.close()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": r["node_id"],
            "node_id": r["node_id"],
            "coord": f"{r['node_id']}/3200",
            "band": r["band"],
            "target_path": r["target_path"],
            "title": r["title"],
            "status": node_status(r["progress_num"], r["progress_den"], r["stall_count"]),
            "progress": f"{r['progress_num']}/{r['progress_den']}",
            "stall_count": r["stall_count"],
            "max_loc_budget": r["max_loc_budget"],
            "updated_at_iso": r["updated_at_iso"],
        })
    return out


def insert_manual_node(db_path: Path, payload: Dict[str, Any]) -> None:
    node_id, band = int(payload["node_id"]), int(payload["band"])
    if not (0 <= node_id < 3200) or not (1 <= band <= 32):
        raise ValueError("Coordinate must be in [0, 3199] and band in [1, 32].")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO roadmap_nodes (node_id, band, title, target_path, max_loc_budget, progress_num, progress_den, stall_count, updated_at_iso)
        VALUES (?, ?, ?, ?, 150, 0, 1, 0, ?)
        ON CONFLICT(node_id) DO UPDATE SET band=excluded.band, title=excluded.title, target_path=excluded.target_path, progress_num=0, progress_den=1, stall_count=0, updated_at_iso=excluded.updated_at_iso;
        """,
        (node_id, band, str(payload["title"])[:140], str(payload["target_path"])[:256], now_iso),
    )
    conn.commit()
    conn.close()


class FlowDashboardHandler(BaseHTTPRequestHandler):
    db_path: Path = DEFAULT_DB

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, code: int, obj: Any) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            body = HTML_TEMPLATE.replace("__DEFAULT_REPO__", DEFAULT_REPO).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self._send_json(200, query_substrate(self.db_path))
        elif self.path == "/api/nodes":
            self._send_json(200, query_nodes(self.db_path))
        elif self.path == "/api/actions":
            self._send_json(200, {"actions": sorted(ACTIONS), "allowed_roots": [str(r) for r in ALLOWED_ROOTS], "default_repo": DEFAULT_REPO})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        name = self.path[len("/api/"):] if self.path.startswith("/api/") else ""
        if self.path == "/api/nodes":
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                insert_manual_node(self.db_path, json.loads(raw.decode("utf-8")))
                self._send_json(201, {"status": "inserted", "ok": True, "cmd": "inject node", "rc": 0, "output": ""})
            except Exception as exc:
                self._send_json(400, {"ok": False, "cmd": "inject node", "rc": 1, "output": str(exc)})
            return
        if name in ACTIONS:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ActionError("request body must be a JSON object")
            except ActionError as exc:
                self._send_json(400, {"ok": False, "cmd": f"flow {name}", "rc": 1, "output": str(exc)})
                return
            except (ValueError, UnicodeDecodeError) as exc:
                self._send_json(400, {"ok": False, "cmd": f"flow {name}", "rc": 1, "output": f"malformed JSON body: {exc}"})
                return
            try:
                result = ACTIONS[name](payload)
                result["cmd"] = f"flow {name}"
                self._send_json(200, result)
            except ActionError as exc:
                self._send_json(400, {"ok": False, "cmd": f"flow {name}", "rc": 1, "output": f"REJECTED: {exc}"})
            except Exception as exc:  # never fail silently -- the operator must see it
                self._send_json(500, {"ok": False, "cmd": f"flow {name}", "rc": 1, "output": f"INTERNAL ERROR: {type(exc).__name__}: {exc}"})
            return
        self.send_error(404)


def get_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def main() -> None:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    FlowDashboardHandler.db_path = db_path.resolve()
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), FlowDashboardHandler)
    print(f"Flow IDE Operator Console -> bind {BIND_HOST}:{BIND_PORT} | local http://127.0.0.1:{BIND_PORT} | LAN http://{get_lan_ip()}:{BIND_PORT}", flush=True)
    print(f"  substrate DB: {FlowDashboardHandler.db_path}", flush=True)
    print(f"  actions: {', '.join(sorted(ACTIONS))} | allowed repo roots: {', '.join(str(r) for r in ALLOWED_ROOTS)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
