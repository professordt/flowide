#!/usr/bin/env python3
from datetime import datetime, timezone
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
import json
import os
import socket
import sqlite3
import sys

DEFAULT_DB = Path(os.environ.get("FLOW_DB_PATH", "flow_substrate.db"))
BIND_HOST = os.environ.get("FLOW_DASH_HOST", "0.0.0.0")
# Auteur's number (9898). Override with FLOW_DASH_PORT.
BIND_PORT = int(os.environ.get("FLOW_DASH_PORT", "9898"))

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FLOW IDE // 3200-NODE SUBSTRATE MONITOR</title>
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
  .inject-form { display: grid; grid-template-columns: 90px 70px 1fr 1fr auto; gap: 8px; margin-top: 10px; }
  input, button { background: var(--bg); color: var(--fg); border: 1px solid var(--border); font-family: var(--mono); font-size: 12px; padding: 6px 8px; }
  button { cursor: pointer; border-color: var(--active); color: var(--active); font-weight: 600; }
  button:hover { background: rgba(34, 187, 255, 0.12); }
</style>
</head>
<body>
<header>
  <div><span class="title">FLOW IDE // RATIONAL SUBSTRATE TERMINAL</span><span style="color:var(--muted); margin-left:10px;">E_round = 0/1 | 235/19 Metonic</span></div>
  <div><span id="gate-badge" class="badge">POLLING...</span><span id="clock" style="color:var(--muted); margin-left:10px;">--:--:--</span></div>
</header>
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
<div class="card">
  <h2>5. Central Roadmap Atomic Queue &amp; Direct Coordinate Injection</h2>
  <form class="inject-form" onsubmit="injectNode(event)">
    <input type="number" id="in-node" min="0" max="3199" placeholder="Node (500-1349)" required>
    <input type="number" id="in-band" min="1" max="32" placeholder="Band" required>
    <input type="text" id="in-path" placeholder="Target path (e.g. src/parser.py)" required>
    <input type="text" id="in-title" placeholder="Atomic task title (<=150 LOC)" required>
    <button type="submit">+ INJECT NODE</button>
  </form>
  <table style="margin-top:12px;"><thead><tr><th>Coord</th><th>Band</th><th>Progress</th><th>Stalls</th><th>Target Path</th><th>Atomic Slice Title</th></tr></thead><tbody id="roadmap-tbody"></tbody></table>
</div>
<script>
const CIRCUMFERENCE = 2 * Math.PI * 54;
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
    document.getElementById('kv-probe').textContent = h.platform + ' @ ' + (h.sampled_at.split('T')[1] || '').slice(0, 8);
    const p = data.phase;
    document.getElementById('dial-center').textContent = p.accum_str;
    document.getElementById('dial-sub').textContent = p.regime_state;
    document.getElementById('kv-phase').textContent = p.accum_str;
    document.getElementById('kv-metonic').textContent = p.metonic_tick;
    document.getElementById('kv-regime').textContent = p.regime_state;
    const arcLen = Math.min(1, p.accum_num / p.accum_den) * CIRCUMFERENCE;
    const arcEl = document.getElementById('phase-arc');
    arcEl.setAttribute('stroke-dasharray', `${arcLen.toFixed(2)} ${CIRCUMFERENCE.toFixed(2)}`);
    arcEl.setAttribute('stroke', p.regime_state === 'AIRGAP_5_32' ? '#f59e0b' : '#22bbff');
    const rm = data.roadmap_summary;
    document.getElementById('kv-nodes-done').textContent = `${rm.done} / ${rm.total}`;
    document.getElementById('kv-nodes-active').textContent = rm.inflight;
    document.getElementById('kv-nodes-stalled').textContent = rm.stalled;
    document.getElementById('kv-next-node').textContent = rm.next_coord;
    document.getElementById('bar-done').style.width = (rm.total ? (rm.done / rm.total) * 100 : 0).toFixed(1) + '%';
    document.getElementById('bar-inflight').style.width = (rm.total ? (rm.inflight / rm.total) * 100 : 0).toFixed(1) + '%';
    if (data.batons.length > 0) {
      document.getElementById('baton-tbody').innerHTML = data.batons.map(b => {
        const cls = b.epistemic_scar.startsWith('[FAIL') ? 'scar-fail' : 'scar-pass';
        return `<tr><td>${b.baton_id}</td><td>${b.roadmap_node_id}/3200</td><td>B${b.from_band} &rarr; B${b.to_band}</td><td>${b.phase_shift}</td><td>${b.fuel}</td><td>${b.touched_paths}</td><td class="${cls}">${escapeHtml(b.epistemic_scar)}</td></tr>`;
      }).join('');
    }
    document.getElementById('roadmap-tbody').innerHTML = data.nodes.map(n => `<tr><td>${n.node_id}/3200</td><td>B${n.band}</td><td>${n.progress}</td><td>${n.stall_count}</td><td>${escapeHtml(n.target_path)}</td><td>${escapeHtml(n.title)}</td></tr>`).join('');
  } catch (err) {}
}
async function injectNode(ev) {
  ev.preventDefault();
  const res = await fetch('/api/nodes', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      node_id: parseInt(document.getElementById('in-node').value, 10),
      band: parseInt(document.getElementById('in-band').value, 10),
      target_path: document.getElementById('in-path').value.trim(),
      title: document.getElementById('in-title').value.trim()
    })
  });
  if (res.ok) { document.getElementById('in-title').value = ''; refreshState(); }
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

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            body = HTML_TEMPLATE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            payload = json.dumps(query_substrate(self.db_path)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/api/nodes":
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                insert_manual_node(self.db_path, json.loads(raw.decode("utf-8")))
                self.send_response(201)
                self.end_headers()
                self.wfile.write(b'{"status":"inserted"}')
            except Exception as exc:
                msg = str(exc).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
        else:
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
    print(f"Flow IDE Browser GUI -> Local: [http://127.0.0.1](http://127.0.0.1):{BIND_PORT} | LAN: http://{get_lan_ip()}:{BIND_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()

if __name__ == "__main__":
    main()
