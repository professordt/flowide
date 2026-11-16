#!/usr/bin/env python3
"""
bootstrap_flow.py — Self-Contained One-Step Flow IDE Installer
Embeds and writes all 8 local-first Flow IDE modules to ~/.local/share/flow-ide/
and installs the unified `flow` CLI command at ~/.local/bin/flow.
Zero external dependencies (Python standard library only).
"""

from pathlib import Path
from typing import Dict
import argparse
import os
import stat

SHARE_DIR = Path.home() / ".local" / "share" / "flow-ide"
BIN_DIR = Path.home() / ".local" / "bin"
FLOW_BIN = BIN_DIR / "flow"

MODULES: Dict[str, str] = {}

# ==============================================================================
# MODULE 1: harvesterd.py (Energy, Thermal, Battery & 16:00-21:00 TOU Telemetry)
# ==============================================================================
MODULES["harvesterd.py"] = r'''#!/usr/bin/env python3
from datetime import datetime, time
from fractions import Fraction
from pathlib import Path
from typing import Tuple
import os
import platform
import re
import signal
import sqlite3
import subprocess
import sys
import time as pytime

MIN_FUEL_GATE = Fraction(1, 5)
TOU_START = time(16, 0)
TOU_END = time(21, 0)
POLL_INTERVAL_SECONDS = 15
RUNNING = True

def _handle_sigterm(signum: int, frame: object) -> None:
    global RUNNING
    RUNNING = False

def clamp_unit(val: Fraction) -> Fraction:
    if val < Fraction(0, 1):
        return Fraction(0, 1)
    if val > Fraction(1, 1):
        return Fraction(1, 1)
    return val

def is_tou_peak(now_local: datetime) -> bool:
    t = now_local.time()
    return TOU_START <= t < TOU_END

def probe_linux_thermals() -> Fraction:
    temps_millic: list[int] = []
    for base in (Path("/sys/class/thermal"), Path("/sys/class/hwmon")):
        if not base.exists():
            continue
        for pat in ("**/temp*_input", "thermal_zone*/temp"):
            for f in base.glob(pat):
                try:
                    raw = int(f.read_text().strip())
                    if 10_000 <= raw <= 130_000:
                        temps_millic.append(raw)
                except (OSError, ValueError):
                    continue
    if not temps_millic:
        return Fraction(1, 4)
    return clamp_unit(Fraction(max(temps_millic) - 40_000, 55_000))

def probe_macos_thermals() -> Fraction:
    try:
        out = subprocess.check_output(
            ["sysctl", "-n", "machdep.xcpm.cpu_thermal_level"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        return clamp_unit(Fraction(int(out), 100))
    except Exception:
        pass
    try:
        out = subprocess.check_output(["pmset", "-g", "therm"], stderr=subprocess.DEVNULL, text=True)
        m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
        if m:
            return clamp_unit(Fraction(100 - int(m.group(1)), 100))
    except Exception:
        pass
    return Fraction(1, 4)

def probe_allostatic_load() -> Fraction:
    try:
        load_1m, _, _ = os.getloadavg()
        cores = os.cpu_count() or 1
        return clamp_unit(Fraction(int(round(load_1m * 100)), cores * 100))
    except OSError:
        return Fraction(1, 5)

def probe_grid_and_battery(now_local: datetime) -> Tuple[Fraction, Fraction]:
    sys_name = platform.system()
    on_ac = True
    charge_pct = 100

    if sys_name == "Linux":
        ps_base = Path("/sys/class/power_supply")
        if ps_base.exists():
            for supply in ps_base.iterdir():
                tf = supply / "type"
                if not tf.exists():
                    continue
                stype = tf.read_text().strip()
                if stype == "Mains" and (supply / "online").exists():
                    on_ac = (supply / "online").read_text().strip() == "1"
                elif stype == "Battery" and (supply / "capacity").exists():
                    try:
                        charge_pct = int((supply / "capacity").read_text().strip())
                    except ValueError:
                        pass
    elif sys_name == "Darwin":
        try:
            out = subprocess.check_output(["pmset", "-g", "batt"], text=True)
            on_ac = "AC Power" in out
            m = re.search(r"(\d+)%", out)
            if m:
                charge_pct = int(m.group(1))
        except Exception:
            pass

    if is_tou_peak(now_local):
        return Fraction(1, 1), Fraction(1, 2)
    if on_ac:
        return Fraction(0, 1), Fraction(1, 1)
    return clamp_unit(Fraction(100 - charge_pct, 100)), (Fraction(3, 4) if charge_pct >= 40 else Fraction(1, 4))

def compute_fuel_index(c_therm: Fraction, h_load: Fraction, t_grid: Fraction, g_cap: Fraction) -> Fraction:
    return clamp_unit(
        Fraction(2, 5) * (Fraction(1, 1) - c_therm)
        + Fraction(3, 10) * (Fraction(1, 1) - h_load)
        + Fraction(1, 5) * (Fraction(1, 1) - t_grid)
        + Fraction(1, 10) * g_cap
    )

def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS harvester_telemetry (
            singleton_id        INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            fuel_num            INTEGER NOT NULL,
            fuel_den            INTEGER NOT NULL CHECK (fuel_den > 0),
            thermal_num         INTEGER NOT NULL,
            thermal_den         INTEGER NOT NULL CHECK (thermal_den > 0),
            allostatic_num      INTEGER NOT NULL,
            allostatic_den      INTEGER NOT NULL CHECK (allostatic_den > 0),
            grid_strain_num     INTEGER NOT NULL,
            grid_strain_den     INTEGER NOT NULL CHECK (grid_strain_den > 0),
            ground_cap_num      INTEGER NOT NULL,
            ground_cap_den      INTEGER NOT NULL CHECK (ground_cap_den > 0),
            gate_status         TEXT NOT NULL,
            platform_probe      TEXT NOT NULL,
            sampled_at_iso      TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn

def sample_once(conn: sqlite3.Connection) -> None:
    now_local = datetime.now().astimezone()
    sys_name = platform.system()
    c_therm = probe_macos_thermals() if sys_name == "Darwin" else probe_linux_thermals()
    h_load = probe_allostatic_load()
    t_grid, g_cap = probe_grid_and_battery(now_local)
    fuel = compute_fuel_index(c_therm, h_load, t_grid, g_cap)

    if is_tou_peak(now_local):
        gate = "HOLD_TOU_PEAK"
    elif fuel < MIN_FUEL_GATE:
        gate = "HOLD_LOW_FUEL"
    else:
        gate = "OPEN_27_32"

    conn.execute(
        """
        INSERT INTO harvester_telemetry (
            singleton_id, fuel_num, fuel_den, thermal_num, thermal_den,
            allostatic_num, allostatic_den, grid_strain_num, grid_strain_den,
            ground_cap_num, ground_cap_den, gate_status, platform_probe, sampled_at_iso
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(singleton_id) DO UPDATE SET
            fuel_num=excluded.fuel_num, fuel_den=excluded.fuel_den,
            thermal_num=excluded.thermal_num, thermal_den=excluded.thermal_den,
            allostatic_num=excluded.allostatic_num, allostatic_den=excluded.allostatic_den,
            grid_strain_num=excluded.grid_strain_num, grid_strain_den=excluded.grid_strain_den,
            ground_cap_num=excluded.ground_cap_num, ground_cap_den=excluded.ground_cap_den,
            gate_status=excluded.gate_status, platform_probe=excluded.platform_probe,
            sampled_at_iso=excluded.sampled_at_iso;
        """,
        (
            fuel.numerator, fuel.denominator, c_therm.numerator, c_therm.denominator,
            h_load.numerator, h_load.denominator, t_grid.numerator, t_grid.denominator,
            g_cap.numerator, g_cap.denominator, gate, sys_name, now_local.isoformat(),
        ),
    )
    conn.commit()
    print(f"[{now_local.strftime('%H:%M:%S')}] Gate={gate} | I(t)={fuel} | Therm={c_therm} | Load={h_load} | Grid={t_grid}", flush=True)

def main() -> None:
    signal.signal(signal.SIGINT, _handle_sigterm)
    signal.signal(signal.SIGTERM, _handle_sigterm)
    db_path = Path(os.environ.get("FLOW_DB_PATH", "flow_substrate.db"))
    conn = init_db(db_path)
    if "--once" in sys.argv:
        sample_once(conn)
        conn.close()
        return
    print(f"harvesterd active on {platform.system()} -> {db_path}", flush=True)
    while RUNNING:
        sample_once(conn)
        for _ in range(POLL_INTERVAL_SECONDS):
            if not RUNNING:
                break
            pytime.sleep(1)
    conn.close()

if __name__ == "__main__":
    main()
'''

# ==============================================================================
# MODULE 2: airgap_vcs.py (Local Git Airgap & Rollback Sandbox)
# ==============================================================================
MODULES["airgap_vcs.py"] = r'''#!/usr/bin/env python3
from pathlib import Path
from typing import Tuple
import subprocess

class GitAirgapSandbox:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()

    def _run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=self.repo_root, capture_output=True, text=True, check=False)

    def ensure_clean_baseline(self) -> None:
        if self._run_git("status", "--porcelain").returncode != 0:
            raise RuntimeError(f"Not a valid git repository: {self.repo_root}")

    def apply_slice_mutation(self, rel_path: str, start_line: int, end_line: int, replacement_code: str) -> None:
        target = (self.repo_root / rel_path).resolve()
        if not str(target).startswith(str(self.repo_root)):
            raise ValueError(f"Path traversal blocked: {rel_path}")
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        new_lines = [ln if ln.endswith("\n") else f"{ln}\n" for ln in replacement_code.splitlines()]
        target.write_text("".join(lines[: start_line - 1] + new_lines + lines[end_line:]), encoding="utf-8")

    def run_verification(self, test_cmd: list[str], timeout_sec: int = 45) -> Tuple[bool, str]:
        try:
            proc = subprocess.run(test_cmd, cwd=self.repo_root, capture_output=True, text=True, timeout=timeout_sec, check=False)
            combined = (proc.stdout + "\n" + proc.stderr).strip()
            return (proc.returncode == 0), (combined[-380:] if len(combined) > 380 else combined)
        except subprocess.TimeoutExpired:
            return False, f"Verification timed out after {timeout_sec}s."

    def commit_hop(self, node_id: int, band: int, rel_path: str, scar: str) -> str:
        self._run_git("add", rel_path)
        res = self._run_git("commit", "-m", f"flow(node-{node_id}/3200, band-{band}): {scar[:72]}")
        if res.returncode != 0:
            return "NO_DIFF_COMMITTED"
        return self._run_git("rev-parse", "--short", "HEAD").stdout.strip()

    def rollback_path(self, rel_path: str) -> None:
        self._run_git("checkout", "--", rel_path)
'''

# ==============================================================================
# MODULE 3: flow_runner.py (Multi-Language Ephemeral Runner with TEVAA Scoring)
# ==============================================================================
MODULES["flow_runner.py"] = r'''#!/usr/bin/env python3
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import json
import os
import re
import shutil
import sqlite3
import sys
import urllib.request
import uuid

from airgap_vcs import GitAirgapSandbox

LATTICE_SIZE = 3200
ACTIVE_CAP = Fraction(27, 32)
MIN_FUEL_GATE = Fraction(1, 5)
MAX_SCAR_BYTES = 512

LOCAL_LLM_URL = os.environ.get("FLOW_LLM_URL", "http://127.0.0.1:11434/api/generate")
BAND_MODELS = {
    "recon": os.environ.get("FLOW_MODEL_RECON", "qwen2.5-coder:3b"),
    "mutate": os.environ.get("FLOW_MODEL_MUTATE", "qwen2.5-coder:7b"),
    "pivot": os.environ.get("FLOW_MODEL_PIVOT", "qwen2.5-coder:14b"),
}
FENCE_LANG_ALIASES: Dict[str, str] = {"python": "python", "rust": "rust", "typescript": "typescript", "go": "go"}

def tevaa_rational_score(q: Fraction, k: Fraction) -> Fraction:
    one_third = Fraction(1, 3)
    return (q - one_third * (q ** 3)) + (k - one_third * (k ** 3)) - ((q ** 2) * k + q * (k ** 2))

def compute_lexical_rational(query_tokens: set[str], doc_text: str) -> Fraction:
    if not query_tokens:
        return Fraction(0, 1)
    doc_tokens = set(re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", doc_text))
    return Fraction(len(query_tokens & doc_tokens), len(query_tokens))

def detect_language_from_path(rel_path: str, db_lang: Optional[str] = None) -> str:
    if db_lang and db_lang in FENCE_LANG_ALIASES:
        return db_lang
    ext = Path(rel_path).suffix.lower()
    if ext == ".rs":
        return "rust"
    if ext in (".ts", ".tsx"):
        return "typescript"
    if ext == ".go":
        return "go"
    return "python"

def resolve_verification_command(repo_root: Path, lang: str, rel_path: str) -> List[str]:
    if lang == "rust":
        if (repo_root / "Cargo.toml").exists() and shutil.which("cargo"):
            return ["cargo", "test", "--quiet"]
        if shutil.which("rustc"):
            return ["rustc", "--emit=metadata", rel_path]
    elif lang == "go":
        if (repo_root / "go.mod").exists() and shutil.which("go"):
            pkg_dir = str(Path(rel_path).parent)
            return ["go", "test", f"./{pkg_dir}" if pkg_dir != "." else "./..."]
        if shutil.which("gofmt"):
            return ["gofmt", "-e", rel_path]
    elif lang == "typescript":
        pkg_json = repo_root / "package.json"
        if pkg_json.exists() and shutil.which("npm"):
            try:
                scripts = json.loads(pkg_json.read_text(encoding="utf-8")).get("scripts", {})
                if "test" in scripts:
                    return ["npm", "test", "--", "--watchAll=false"]
                if "build" in scripts:
                    return ["npm", "run", "build"]
            except Exception:
                pass
        if shutil.which("npx"):
            return ["npx", "--no-install", "tsc", "--noEmit"]

    if shutil.which("pytest") and any(repo_root.rglob("test_*.py")):
        return ["pytest", "-q"]
    return [sys.executable, "-m", "py_compile", rel_path]

def select_model_for_band(band: int, stall_count: int) -> str:
    if stall_count >= 2 or band == 19:
        return BAND_MODELS["pivot"]
    if band <= 5 or band >= 28:
        return BAND_MODELS["recon"]
    return BAND_MODELS["mutate"]

def call_local_slm(model: str, prompt: str) -> str:
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": 4096},
    }).encode("utf-8")
    req = urllib.request.Request(LOCAL_LLM_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8")).get("response", "")

def extract_code_and_scar(raw_response: str, lang: str) -> Tuple[str, str]:
    fence_regex = re.compile(r"```(?:python|py|rust|rs|typescript|ts|tsx|go|golang)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
    matches = fence_regex.findall(raw_response)
    if matches:
        code = max(matches, key=len).rstrip("\n")
    else:
        code = "\n".join(ln for ln in raw_response.splitlines() if not ln.strip().startswith("SCAR:")).strip()

    scar_match = re.search(r"SCAR:\s*(.+)", raw_response)
    scar = scar_match.group(1).strip() if scar_match else f"Applied atomic {lang} slice mutation."
    return code, scar.encode("utf-8")[:MAX_SCAR_BYTES].decode("utf-8", errors="ignore")

class EphemeralFlowRunner:
    def __init__(self, repo_root: Path, db_path: Path, override_test_cmd: Optional[List[str]] = None) -> None:
        self.repo_root = repo_root.resolve()
        self.db_path = db_path.resolve()
        self.override_test_cmd = override_test_cmd
        self.sandbox = GitAirgapSandbox(self.repo_root)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def check_energy_and_phase_gates(self, conn: sqlite3.Connection) -> Tuple[bool, Fraction, str]:
        telem = conn.execute("SELECT fuel_num, fuel_den, gate_status FROM harvester_telemetry WHERE singleton_id = 1;").fetchone()
        fuel = Fraction(telem["fuel_num"], telem["fuel_den"]) if telem else Fraction(1, 2)
        gate = telem["gate_status"] if telem else "OPEN_27_32"

        if gate != "OPEN_27_32" or fuel < MIN_FUEL_GATE:
            return False, fuel, f"GATE_HOLD ({gate}, I(t)={fuel})"

        cycle = conn.execute("SELECT accum_phase_num, accum_phase_den FROM phase_cycle_state WHERE singleton_id = 1;").fetchone()
        accum = Fraction(cycle["accum_phase_num"], cycle["accum_phase_den"]) if cycle else Fraction(0, 1)
        if accum >= ACTIVE_CAP:
            conn.execute("""
                UPDATE phase_cycle_state
                SET accum_phase_num = 0, accum_phase_den = 32,
                    metonic_tick = metonic_tick + 1, regime_state = 'AIRGAP_5_32'
                WHERE singleton_id = 1;
            """)
            conn.commit()
            return False, fuel, "AIRGAP_COOLING_5_32 (Active 27/32 arc completed; venting heat)"
        return True, fuel, "OPEN_27_32"

    def claim_next_node(self, conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(symbol_index);").fetchall()}
        lang_expr = "s.language AS sym_language" if "language" in cols else "NULL AS sym_language"
        rows = conn.execute(f"""
            SELECT r.*, s.start_line, s.end_line, s.signature_text, s.symbol_qualname, {lang_expr}
            FROM roadmap_nodes r
            LEFT JOIN symbol_index s ON s.node_id = r.node_id
            WHERE r.progress_num = 0
            ORDER BY r.node_id ASC;
        """).fetchall()
        for row in rows:
            unmet = conn.execute("""
                SELECT COUNT(*) AS cnt FROM roadmap_edges e
                JOIN roadmap_nodes up ON up.node_id = e.upstream_id
                WHERE e.downstream_id = ? AND up.progress_num < up.progress_den;
            """, (row["node_id"],)).fetchone()["cnt"]
            if unmet == 0:
                return row
        return None

    def build_tevaa_context(self, conn: sqlite3.Connection, node: sqlite3.Row, target_slice: str) -> Tuple[str, Optional[sqlite3.Row]]:
        query_tokens = set(re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", target_slice))
        q_band_norm = Fraction(node["band"], 32)
        up_rows = conn.execute("""
            SELECT s.file_path, s.signature_text, r.band, r.node_id
            FROM roadmap_edges e
            JOIN symbol_index s ON s.node_id = e.upstream_id
            JOIN roadmap_nodes r ON r.node_id = e.upstream_id
            WHERE e.downstream_id = ?;
        """, (node["node_id"],)).fetchall()

        scored: List[Tuple[Fraction, str]] = []
        for u in up_rows:
            k_lex = compute_lexical_rational(query_tokens, u["signature_text"])
            band_prox = Fraction(1, 1) - Fraction(abs(node["band"] - u["band"]), 32)
            score = tevaa_rational_score(q_band_norm, (k_lex + band_prox) / 2)
            scored.append((score, f"// Upstream Node {u['node_id']}/3200 ({u['file_path']})\n{u['signature_text']}"))
        scored.sort(key=lambda item: item[0], reverse=True)
        latest_baton = conn.execute("SELECT * FROM relay_batons ORDER BY rowid DESC LIMIT 1;").fetchone()
        return ("\n\n".join(t for _, t in scored[:6]) or "// No upstream dependencies."), latest_baton

    def execute_single_hop(self) -> str:
        self.sandbox.ensure_clean_baseline()
        conn = self._connect()
        allowed, fuel, status_msg = self.check_energy_and_phase_gates(conn)
        if not allowed:
            conn.close()
            return status_msg

        node = self.claim_next_node(conn)
        if node is None:
            conn.close()
            return "LATTICE_COMPLETE: All Central Roadmap nodes are at 1/1."

        node_id = node["node_id"]
        rel_path = node["target_path"]
        lang = detect_language_from_path(rel_path, node["sym_language"])
        start_line = node["start_line"] or 1
        end_line = node["end_line"] or 150
        stall_count = node["stall_count"]

        active_band = 19 if stall_count >= 3 else node["band"]
        if stall_count >= 3:
            conn.execute("UPDATE roadmap_nodes SET stall_count = 0 WHERE node_id = ?;", (node_id,))
            conn.commit()

        full_lines = (self.repo_root / rel_path).read_text(encoding="utf-8").splitlines()
        target_slice = "\n".join(full_lines[start_line - 1 : end_line])
        tevaa_context, prev_baton = self.build_tevaa_context(conn, node, target_slice)
        prev_scar = prev_baton["epistemic_scar"] if prev_baton else "Cold start from Band 18 centroid."
        from_band = prev_baton["to_band"] if prev_baton else 18

        prompt = (
            f"You are a stateless Flow IDE {lang} specialist operating in Band {active_band}/32.\n"
            f"Target Coordinate: {node_id}/3200 | Language: {lang} | File: {rel_path} (L{start_line}-L{end_line})\n"
            f"Incoming Relay Baton Scar: {prev_scar}\n\n"
            f"### TEVAA-Selected Upstream Signatures:\n{tevaa_context}\n\n"
            f"### Target Code Slice:\n```{lang}\n{target_slice}\n```\n\n"
            f"Task: Refine, implement, or fix this {lang} slice cleanly within <=150 LOC.\n"
            f"Output ONLY the replacement {lang} code inside a ```{lang} fence, followed by a single line:\n"
            "SCAR: <concise 1-sentence summary of what changed>"
        )

        raw_out = call_local_slm(select_model_for_band(active_band, stall_count), prompt)
        new_code, scar = extract_code_and_scar(raw_out, lang)

        test_cmd = self.override_test_cmd or resolve_verification_command(self.repo_root, lang, rel_path)
        self.sandbox.apply_slice_mutation(rel_path, start_line, end_line, new_code)
        passed, test_tail = self.sandbox.run_verification(test_cmd)

        now_iso = datetime.now(timezone.utc).isoformat()
        baton_id = uuid.uuid4().hex[:16]

        if passed:
            commit_rev = self.sandbox.commit_hop(node_id, active_band, rel_path, scar)
            final_scar = f"[PASS {lang} ({test_cmd[0]}) {commit_rev}] {scar}"[:MAX_SCAR_BYTES]
            conn.execute("UPDATE roadmap_nodes SET progress_num=1, progress_den=1, stall_count=0, updated_at_iso=? WHERE node_id=?;", (now_iso, node_id))
        else:
            self.sandbox.rollback_path(rel_path)
            final_scar = f"[FAIL {lang} ({test_cmd[0]}) -> ROLLBACK] {test_tail}"[:MAX_SCAR_BYTES]
            conn.execute("UPDATE roadmap_nodes SET stall_count=stall_count+1, updated_at_iso=? WHERE node_id=?;", (now_iso, node_id))

        conn.execute(
            """
            INSERT INTO relay_batons (
                baton_id, roadmap_node_id, from_band, to_band,
                phase_shift_num, phase_shift_den, fuel_num, fuel_den,
                touched_paths, epistemic_scar, created_at_iso
            ) VALUES (?, ?, ?, ?, 1, 32, ?, ?, ?, ?, ?);
            """,
            (baton_id, node_id, from_band, active_band, fuel.numerator, fuel.denominator, json.dumps([rel_path]), final_scar, now_iso),
        )
        conn.execute("UPDATE phase_cycle_state SET accum_phase_num = accum_phase_num + 1, regime_state = 'ACTIVE_27_32' WHERE singleton_id = 1;")
        conn.commit()
        conn.close()

        return f"HOP_COMPLETE | Node={node_id}/3200 | Lang={lang} | Band={active_band} | Verify={' '.join(test_cmd)} | Passed={passed} | Baton={baton_id}\n{final_scar}"

if __name__ == "__main__":
    repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    default_db = repo.resolve() / ".flow" / "flow_substrate.db"
    db = Path(os.environ.get("FLOW_DB_PATH", str(default_db)))
    print(EphemeralFlowRunner(repo_root=repo, db_path=db).execute_single_hop())
'''

# ==============================================================================
# MODULE 4: decomposer.py (Multi-Language AST & Bracket-Aware Task Slicer)
# ==============================================================================
MODULES["decomposer.py"] = r'''#!/usr/bin/env python3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
import ast
import os
import re
import sqlite3
import sys

LATTICE_SIZE = 3200
MAX_LOC_BUDGET = 150
IGNORE_DIRS = {".git", ".flow", ".venv", "venv", "__pycache__", "node_modules", "target", "dist", "build", ".next", "vendor"}

LANG_PATTERNS: Dict[str, List[Tuple[str, re.Pattern[str]]]] = {
    "rust": [
        ("fn", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"[^\"]+\"\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)")),
        ("struct", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+([A-Za-z_][A-Za-z0-9_]*)")),
        ("impl", re.compile(r"^\s*(?:unsafe\s+)?impl(?:<[^>]+>)?\s+(?:[A-Za-z_][A-Za-z0-9_:<>]*\s+for\s+)?([A-Za-z_][A-Za-z0-9_]*)")),
    ],
    "typescript": [
        ("fn", re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)")),
        ("const_fn", re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^=]+)?=>")),
        ("type", re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:abstract\s+)?(?:class|interface|type|enum)\s+([A-Za-z_$][A-Za-z0-9_$]*)")),
    ],
    "go": [
        ("method", re.compile(r"^\s*func\s*\([^)]+\)\s*([A-Za-z_][A-Za-z0-9_]*)")),
        ("fn", re.compile(r"^\s*func\s+([A-Za-z_][A-Za-z0-9_]*)")),
        ("type", re.compile(r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:struct|interface)\b")),
    ],
}

COMMON_KEYWORDS = {
    "if", "else", "for", "while", "loop", "match", "switch", "case", "return",
    "let", "const", "var", "mut", "pub", "fn", "func", "type", "struct", "enum",
    "trait", "impl", "class", "interface", "export", "import", "from", "async",
    "await", "self", "Self", "this", "super", "true", "false", "null", "nil",
    "None", "Some", "Ok", "Err", "string", "number", "boolean", "void", "any",
    "u8", "u16", "u32", "u64", "usize", "i8", "i16", "i32", "i64", "isize",
    "f32", "f64", "bool", "str", "String", "Vec", "Option", "Result", "int",
    "int64", "uint64", "float64", "byte", "rune", "error", "err", "ctx"
}

@dataclass
class ExtractedSymbol:
    qualname: str
    file_path: str
    language: str
    start_line: int
    end_line: int
    signature_text: str
    referenced_names: Set[str] = field(default_factory=set)
    chunk_index: int = 0
    total_chunks: int = 1

    @property
    def loc_count(self) -> int:
        return max(1, self.end_line - self.start_line + 1)

def _slice_oversized_symbols(symbols: List[ExtractedSymbol]) -> List[ExtractedSymbol]:
    sliced: List[ExtractedSymbol] = []
    for sym in symbols:
        if sym.loc_count <= MAX_LOC_BUDGET:
            sliced.append(sym)
            continue
        num_chunks = (sym.loc_count + MAX_LOC_BUDGET - 1) // MAX_LOC_BUDGET
        for idx in range(num_chunks):
            c_start = sym.start_line + idx * MAX_LOC_BUDGET
            c_end = min(sym.end_line, c_start + MAX_LOC_BUDGET - 1)
            sliced.append(ExtractedSymbol(
                qualname=f"{sym.qualname}[part_{idx + 1}/{num_chunks}]",
                file_path=sym.file_path, language=sym.language,
                start_line=c_start, end_line=c_end,
                signature_text=f"{sym.signature_text}  // Slice {idx + 1}/{num_chunks} (L{c_start}-L{c_end})",
                referenced_names=sym.referenced_names, chunk_index=idx, total_chunks=num_chunks,
            ))
    return sliced

def _format_py_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, prefix: str = "") -> str:
    async_kw = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    args_str = ast.unparse(node.args)
    ret_str = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    doc = ast.get_docstring(node, clean=True)
    doc_line = f'    """{doc.splitlines()[0]}"""' if doc else ""
    header = f"{async_kw}def {prefix}{node.name}({args_str}){ret_str}:"
    return f"{header}\n{doc_line}" if doc_line else header

def _collect_py_identifiers(node: ast.AST) -> Set[str]:
    names: Set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names - COMMON_KEYWORDS

def extract_python_symbols(file_path: Path, repo_root: Path) -> List[ExtractedSymbol]:
    rel_path = file_path.relative_to(repo_root).as_posix()
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=rel_path)
    raw_symbols: List[ExtractedSymbol] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_line = getattr(node, "end_lineno", node.lineno)
            raw_symbols.append(ExtractedSymbol(
                qualname=f"{rel_path}::{node.name}", file_path=rel_path, language="python",
                start_line=node.lineno, end_line=end_line, signature_text=_format_py_signature(node),
                referenced_names=_collect_py_identifiers(node) - {node.name},
            ))
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            raw_symbols.append(ExtractedSymbol(
                qualname=f"{rel_path}::{node.name}", file_path=rel_path, language="python",
                start_line=node.lineno, end_line=node.lineno,
                signature_text=f"class {node.name}({bases}):" if bases else f"class {node.name}:",
                referenced_names={ast.unparse(b) for b in node.bases},
            ))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    end_line = getattr(item, "end_lineno", item.lineno)
                    raw_symbols.append(ExtractedSymbol(
                        qualname=f"{rel_path}::{node.name}.{item.name}", file_path=rel_path, language="python",
                        start_line=item.lineno, end_line=end_line,
                        signature_text=_format_py_signature(item, prefix=f"{node.name}."),
                        referenced_names=_collect_py_identifiers(item) - {item.name, "self", "cls"},
                    ))
    return _slice_oversized_symbols(raw_symbols)

def _find_matching_brace_line(lines: List[str], start_idx: int) -> int:
    depth = 0
    saw_open = False
    in_block_comment = False
    for idx in range(start_idx, len(lines)):
        line = lines[idx]
        i, n = 0, len(line)
        in_str: Optional[str] = None
        while i < n:
            ch = line[i]
            nxt = line[i + 1] if i + 1 < n else ""
            if in_block_comment:
                if ch == "*" and nxt == "/":
                    in_block_comment = False
                    i += 2
                    continue
                i += 1
                continue
            if in_str is not None:
                if ch == "\\" and in_str != "`":
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
                i += 1
                continue
            if ch == "/" and nxt == "/":
                break
            if ch == "/" and nxt == "*":
                in_block_comment = True
                i += 2
                continue
            if ch in ('"', "'", "`"):
                in_str = ch
                i += 1
                continue
            if ch == "{":
                depth += 1
                saw_open = True
            elif ch == "}":
                depth -= 1
                if saw_open and depth <= 0:
                    return idx
            i += 1
        if not saw_open and line.rstrip().endswith(";"):
            return idx
    return len(lines) - 1

def _extract_brace_signature(lines: List[str], start_idx: int, end_idx: int) -> str:
    sig_lines: List[str] = []
    for idx in range(start_idx, min(end_idx + 1, start_idx + 8)):
        text = lines[idx].rstrip()
        if "{" in text:
            head = text.split("{", 1)[0].rstrip()
            if head:
                sig_lines.append(head + " { ... }")
            break
        sig_lines.append(text)
        if text.endswith(";"):
            break
    return "\n".join(sig_lines).strip()

def extract_brace_language_symbols(file_path: Path, repo_root: Path, lang: str) -> List[ExtractedSymbol]:
    rel_path = file_path.relative_to(repo_root).as_posix()
    lines = file_path.read_text(encoding="utf-8").splitlines()
    patterns = LANG_PATTERNS[lang]
    raw_symbols: List[ExtractedSymbol] = []
    idx, n_lines = 0, len(lines)

    while idx < n_lines:
        line = lines[idx]
        matched_kind: Optional[str] = None
        matched_name: Optional[str] = None
        for kind, regex in patterns:
            m = regex.match(line)
            if m:
                matched_kind, matched_name = kind, m.group(1)
                break
        if matched_kind and matched_name:
            end_idx = _find_matching_brace_line(lines, idx)
            block_text = "\n".join(lines[idx : end_idx + 1])
            refs = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", block_text)) - COMMON_KEYWORDS - {matched_name}
            sig_text = _extract_brace_signature(lines, idx, end_idx)
            if lang == "rust" and matched_kind == "impl" and (end_idx - idx + 1) > MAX_LOC_BUDGET:
                raw_symbols.append(ExtractedSymbol(
                    qualname=f"{rel_path}::impl_{matched_name}", file_path=rel_path, language=lang,
                    start_line=idx + 1, end_line=idx + 1, signature_text=sig_text, referenced_names={matched_name},
                ))
                idx += 1
                continue
            qual = f"{rel_path}::impl_{matched_name}" if matched_kind == "impl" else f"{rel_path}::{matched_name}"
            raw_symbols.append(ExtractedSymbol(
                qualname=qual, file_path=rel_path, language=lang,
                start_line=idx + 1, end_line=end_idx + 1, signature_text=sig_text, referenced_names=refs,
            ))
            idx = end_idx + 1
        else:
            idx += 1
    return _slice_oversized_symbols(raw_symbols)

def detect_language(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    if ext == ".py":
        return "python"
    if ext == ".rs":
        return "rust"
    if ext in (".ts", ".tsx"):
        return "typescript"
    if ext == ".go":
        return "go"
    return None

def resolve_dependencies(symbols: List[ExtractedSymbol]) -> Dict[str, Set[str]]:
    short_name_map: Dict[str, List[str]] = {}
    for sym in symbols:
        base = sym.qualname.split("::")[-1].split("[")[0].split(".")[-1]
        if base.startswith("impl_"):
            base = base[5:]
        short_name_map.setdefault(base, []).append(sym.qualname)

    deps: Dict[str, Set[str]] = {sym.qualname: set() for sym in symbols}
    by_base_qual: Dict[str, List[ExtractedSymbol]] = {}
    for sym in symbols:
        by_base_qual.setdefault(sym.qualname.split("[")[0], []).append(sym)
    for slices in by_base_qual.values():
        if len(slices) > 1:
            slices.sort(key=lambda s: s.chunk_index)
            for i in range(1, len(slices)):
                deps[slices[i].qualname].add(slices[i - 1].qualname)

    for sym in symbols:
        for ref in sym.referenced_names:
            for target_q in short_name_map.get(ref, []):
                if target_q != sym.qualname:
                    deps[sym.qualname].add(target_q)
    return deps

def topological_sort_symbols(symbols: List[ExtractedSymbol], deps: Dict[str, Set[str]]) -> List[ExtractedSymbol]:
    sym_map = {s.qualname: s for s in symbols}
    in_degree = {q: len(d) for q, d in deps.items()}
    reverse_deps: Dict[str, Set[str]] = {q: set() for q in sym_map}
    for downstream, upstreams in deps.items():
        for up in upstreams:
            reverse_deps[up].add(downstream)

    ready = sorted([q for q, deg in in_degree.items() if deg == 0])
    ordered: List[ExtractedSymbol] = []
    visited: Set[str] = set()
    while len(ordered) < len(symbols):
        nxt = sorted(set(sym_map.keys()) - visited)[0] if not ready else ready.pop(0)
        if nxt in visited:
            continue
        visited.add(nxt)
        ordered.append(sym_map[nxt])
        for child in sorted(reverse_deps[nxt]):
            in_degree[child] -= 1
            if in_degree[child] <= 0 and child not in visited:
                ready.append(child)
        ready.sort()
    return ordered

def assign_rational_coordinates(ordered_symbols: List[ExtractedSymbol]) -> List[Tuple[int, int, ExtractedSymbol]]:
    total = len(ordered_symbols)
    if total == 0:
        return []
    if total > 850:
        raise ValueError(f"Repository slice count ({total}) exceeds single-pass active capacity (850 nodes).")
    assigned: List[Tuple[int, int, ExtractedSymbol]] = []
    for idx, sym in enumerate(ordered_symbols):
        node_id = 500 + ((idx * 850) // total)
        assigned.append((node_id, (node_id // 100) + 1, sym))
    return assigned

def populate_sqlite(db_path: Path, assigned: List[Tuple[int, int, ExtractedSymbol]], deps: Dict[str, Set[str]]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS roadmap_nodes (
            node_id         INTEGER PRIMARY KEY CHECK (node_id >= 0 AND node_id < 3200),
            band            INTEGER NOT NULL CHECK (band >= 1 AND band <= 32),
            title           TEXT NOT NULL,
            target_path     TEXT NOT NULL,
            max_loc_budget  INTEGER NOT NULL DEFAULT 150,
            progress_num    INTEGER NOT NULL DEFAULT 0,
            progress_den    INTEGER NOT NULL DEFAULT 1 CHECK (progress_den > 0),
            stall_count     INTEGER NOT NULL DEFAULT 0,
            updated_at_iso  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS roadmap_edges (
            upstream_id     INTEGER NOT NULL REFERENCES roadmap_nodes(node_id) ON DELETE CASCADE,
            downstream_id   INTEGER NOT NULL REFERENCES roadmap_nodes(node_id) ON DELETE CASCADE,
            PRIMARY KEY (upstream_id, downstream_id),
            CHECK (upstream_id != downstream_id)
        );
        CREATE TABLE IF NOT EXISTS symbol_index (
            symbol_qualname TEXT PRIMARY KEY,
            language        TEXT NOT NULL,
            file_path       TEXT NOT NULL,
            start_line      INTEGER NOT NULL,
            end_line        INTEGER NOT NULL,
            loc_count       INTEGER NOT NULL,
            signature_text  TEXT NOT NULL,
            node_id         INTEGER REFERENCES roadmap_nodes(node_id) ON DELETE SET NULL
        );
    """)
    now_iso = datetime.now(timezone.utc).isoformat()
    qual_to_node: Dict[str, int] = {sym.qualname: nid for nid, _, sym in assigned}

    for node_id, band, sym in assigned:
        title = f"[{sym.language}] {sym.qualname} (L{sym.start_line}-L{sym.end_line}, {sym.loc_count} LOC)"
        conn.execute(
            """
            INSERT INTO roadmap_nodes (
                node_id, band, title, target_path, max_loc_budget,
                progress_num, progress_den, stall_count, updated_at_iso
            ) VALUES (?, ?, ?, ?, ?, 0, 1, 0, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                band=excluded.band, title=excluded.title, target_path=excluded.target_path,
                max_loc_budget=excluded.max_loc_budget, updated_at_iso=excluded.updated_at_iso;
            """,
            (node_id, band, title, sym.file_path, MAX_LOC_BUDGET, now_iso),
        )
        conn.execute(
            """
            INSERT INTO symbol_index (
                symbol_qualname, language, file_path, start_line, end_line,
                loc_count, signature_text, node_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol_qualname) DO UPDATE SET
                language=excluded.language, file_path=excluded.file_path,
                start_line=excluded.start_line, end_line=excluded.end_line,
                loc_count=excluded.loc_count, signature_text=excluded.signature_text,
                node_id=excluded.node_id;
            """,
            (sym.qualname, sym.language, sym.file_path, sym.start_line, sym.end_line, sym.loc_count, sym.signature_text, node_id),
        )

    for downstream_q, upstream_set in deps.items():
        down_id = qual_to_node.get(downstream_q)
        if down_id is None:
            continue
        for up_q in upstream_set:
            up_id = qual_to_node.get(up_q)
            if up_id is not None and up_id < down_id:
                conn.execute("INSERT OR IGNORE INTO roadmap_edges (upstream_id, downstream_id) VALUES (?, ?);", (up_id, down_id))
    conn.commit()
    conn.close()

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 decomposer.py <repo_dir> [db_path]", file=sys.stderr)
        sys.exit(1)
    repo_root = Path(sys.argv[1]).resolve()
    default_db = repo_root / ".flow" / "flow_substrate.db"
    db_path = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path(os.environ.get("FLOW_DB_PATH", str(default_db)))

    all_symbols: List[ExtractedSymbol] = []
    lang_counts: Dict[str, int] = {}
    for src_file in sorted(repo_root.rglob("*")):
        if not src_file.is_file():
            continue
        if any(part in IGNORE_DIRS or part.startswith(".") for part in src_file.relative_to(repo_root).parts):
            continue
        lang = detect_language(src_file)
        if not lang:
            continue
        extracted = extract_python_symbols(src_file, repo_root) if lang == "python" else extract_brace_language_symbols(src_file, repo_root, lang)
        all_symbols.extend(extracted)
        lang_counts[lang] = lang_counts.get(lang, 0) + len(extracted)

    deps = resolve_dependencies(all_symbols)
    ordered = topological_sort_symbols(all_symbols, deps)
    assigned = assign_rational_coordinates(ordered)
    populate_sqlite(db_path, assigned, deps)

    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(lang_counts.items())) or "none"
    print(f"Indexed {len(assigned)} atomic nodes (<= {MAX_LOC_BUDGET} LOC) [{breakdown}] -> {db_path}")

if __name__ == "__main__":
    main()
'''

# ==============================================================================
# MODULE 5: airgap_sweeper.py (5/32 Airgap Sweeper & SHA-256 Baton Archiver)
# ==============================================================================
MODULES["airgap_sweeper.py"] = r'''#!/usr/bin/env python3
from datetime import datetime, time, timezone
from fractions import Fraction
from pathlib import Path
from typing import List, Optional
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time as pytime

ACTIVE_CAP = Fraction(27, 32)
TOU_START = time(16, 0)
TOU_END = time(21, 0)
SWEEP_POLL_SECONDS = 10
HOT_BATON_RETENTION = 32
RUNNING = True

def _handle_sigterm(signum: int, frame: object) -> None:
    global RUNNING
    RUNNING = False

def is_tou_peak(now_local: datetime) -> bool:
    t = now_local.time()
    return TOU_START <= t < TOU_END

def init_archive_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS relay_batons_archive (
            archive_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            baton_id        TEXT UNIQUE NOT NULL,
            roadmap_node_id INTEGER NOT NULL,
            from_band       INTEGER NOT NULL,
            to_band         INTEGER NOT NULL,
            phase_shift_num INTEGER NOT NULL,
            phase_shift_den INTEGER NOT NULL,
            fuel_num        INTEGER NOT NULL,
            fuel_den        INTEGER NOT NULL,
            touched_paths   TEXT NOT NULL,
            epistemic_scar  TEXT NOT NULL,
            created_at_iso  TEXT NOT NULL,
            archived_at_iso TEXT NOT NULL,
            chain_sha256    TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS prevent_archive_update
        BEFORE UPDATE ON relay_batons_archive
        BEGIN
            SELECT RAISE(ABORT, 'Immutable archive: updates forbidden (d(Memory_LTM)/dQ = 0/1)');
        END;
        CREATE TRIGGER IF NOT EXISTS prevent_archive_delete
        BEFORE DELETE ON relay_batons_archive
        BEGIN
            SELECT RAISE(ABORT, 'Immutable archive: deletions forbidden (d(Memory_LTM)/dQ = 0/1)');
        END;
    """)
    conn.commit()

def run_deterministic_lint_and_format(repo_root: Path, paths: List[str]) -> str:
    existing = [p for p in paths if (repo_root / p).exists()]
    if not existing:
        return "No modified files to lint."
    actions: List[str] = []

    py_files = [p for p in existing if p.endswith(".py")]
    if py_files:
        if shutil.which("ruff"):
            subprocess.run(["ruff", "check", "--fix", "--quiet", *py_files], cwd=repo_root, check=False)
            subprocess.run(["ruff", "format", "--quiet", *py_files], cwd=repo_root, check=False)
            actions.append(f"ruff({len(py_files)})")
        elif shutil.which("black"):
            subprocess.run(["black", "--quiet", *py_files], cwd=repo_root, check=False)
            actions.append(f"black({len(py_files)})")
        else:
            for pf in py_files:
                subprocess.run([sys.executable, "-m", "py_compile", pf], cwd=repo_root, check=False)
            actions.append(f"py_compile({len(py_files)})")

    rs_files = [p for p in existing if p.endswith(".rs")]
    if rs_files and shutil.which("rustfmt"):
        subprocess.run(["rustfmt", *rs_files], cwd=repo_root, check=False)
        actions.append(f"rustfmt({len(rs_files)})")

    go_files = [p for p in existing if p.endswith(".go")]
    if go_files and shutil.which("gofmt"):
        subprocess.run(["gofmt", "-w", *go_files], cwd=repo_root, check=False)
        actions.append(f"gofmt({len(go_files)})")

    subprocess.run(["git", "add", *existing], cwd=repo_root, check=False)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_root, check=False).returncode != 0:
        subprocess.run(["git", "commit", "-m", "airgap(bands-28-32): deterministic Ricci format & lint pass"], cwd=repo_root, capture_output=True, check=False)
        actions.append("committed_format_fix")

    return ", ".join(actions) if actions else "Clean syntax verified."

def consolidate_git_micro_commits(repo_root: Path, metonic_tick: int) -> str:
    log_proc = subprocess.run(["git", "log", "-n", "32", "--pretty=format:%H|%s"], cwd=repo_root, capture_output=True, text=True, check=False)
    if log_proc.returncode != 0 or not log_proc.stdout.strip():
        return "No git history to consolidate."

    flow_commits: List[tuple[str, str]] = []
    for line in log_proc.stdout.strip().splitlines():
        sha, subj = line.split("|", 1)
        if subj.startswith("flow(node-") or subj.startswith("airgap(bands-28-32):"):
            flow_commits.append((sha, subj))
        else:
            break

    if len(flow_commits) < 3:
        return f"Retained {len(flow_commits)} micro-commit(s)."
    if subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True, check=False).stdout.strip():
        return "Skipped squash: working tree dirty."

    oldest_sha = flow_commits[-1][0]
    if subprocess.run(["git", "rev-parse", f"{oldest_sha}^"], cwd=repo_root, capture_output=True, check=False).returncode != 0:
        return "Skipped squash: root commit reached."
    if subprocess.run(["git", "reset", "--soft", f"{oldest_sha}^"], cwd=repo_root, capture_output=True, check=False).returncode != 0:
        return "Skipped squash: soft reset failed."

    bullets = "\n".join(f"- {subj}" for _, subj in reversed(flow_commits))
    msg = f"milestone(metonic-{metonic_tick}, 27/32-arc): consolidated {len(flow_commits)} flow hops\n\n{bullets}"
    subprocess.run(["git", "commit", "-m", msg], cwd=repo_root, capture_output=True, check=False)
    return f"Consolidated {len(flow_commits)} micro-commits."

def archive_old_batons(conn: sqlite3.Connection) -> int:
    total_hot = conn.execute("SELECT COUNT(*) FROM relay_batons;").fetchone()[0]
    if total_hot <= HOT_BATON_RETENTION:
        return 0
    overflow = total_hot - HOT_BATON_RETENTION
    old_rows = conn.execute(
        """
        SELECT rowid, baton_id, roadmap_node_id, from_band, to_band,
               phase_shift_num, phase_shift_den, fuel_num, fuel_den,
               touched_paths, epistemic_scar, created_at_iso
        FROM relay_batons ORDER BY rowid ASC LIMIT ?;
        """,
        (overflow,),
    ).fetchall()

    last_hash_row = conn.execute("SELECT chain_sha256 FROM relay_batons_archive ORDER BY archive_id DESC LIMIT 1;").fetchone()
    prev_hash = last_hash_row[0] if last_hash_row else ("0" * 64)
    now_iso = datetime.now(timezone.utc).isoformat()
    archived_count = 0

    for r in old_rows:
        rowid, baton_id, node_id, f_band, t_band, ps_num, ps_den, fuel_num, fuel_den, touched_json, scar, created_iso = r
        chain_hash = hashlib.sha256(f"{prev_hash}|{baton_id}|{node_id}|{scar}|{created_iso}".encode("utf-8")).hexdigest()
        conn.execute(
            """
            INSERT OR IGNORE INTO relay_batons_archive (
                baton_id, roadmap_node_id, from_band, to_band,
                phase_shift_num, phase_shift_den, fuel_num, fuel_den,
                touched_paths, epistemic_scar, created_at_iso,
                archived_at_iso, chain_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (baton_id, node_id, f_band, t_band, ps_num, ps_den, fuel_num, fuel_den, touched_json, scar, created_iso, now_iso, chain_hash),
        )
        conn.execute("DELETE FROM relay_batons WHERE rowid = ?;", (rowid,))
        prev_hash = chain_hash
        archived_count += 1

    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    return archived_count

def execute_sweep_cycle(repo_root: Path, db_path: Path) -> Optional[str]:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    init_archive_schema(conn)
    now_local = datetime.now().astimezone()
    tou_active = is_tou_peak(now_local)

    cycle_row = conn.execute("SELECT accum_phase_num, accum_phase_den, metonic_tick, regime_state FROM phase_cycle_state WHERE singleton_id = 1;").fetchone()
    if cycle_row is None:
        conn.close()
        return None

    accum_num, accum_den, metonic_tick, regime_state = cycle_row
    if not ((regime_state == "AIRGAP_5_32") or (Fraction(accum_num, accum_den) >= ACTIVE_CAP) or tou_active):
        conn.close()
        return None

    touched_set: set[str] = set()
    for (p_json,) in conn.execute("SELECT DISTINCT touched_paths FROM relay_batons;").fetchall():
        try:
            for p in json.loads(p_json):
                touched_set.add(p)
        except Exception:
            continue

    lint_report = run_deterministic_lint_and_format(repo_root, sorted(touched_set))
    git_report = consolidate_git_micro_commits(repo_root, metonic_tick)
    archived_num = archive_old_batons(conn)

    next_regime = "TOU_CONSERVATION" if tou_active else "ACTIVE_27_32"
    conn.execute("UPDATE phase_cycle_state SET accum_phase_num = 0, accum_phase_den = 32, regime_state = ? WHERE singleton_id = 1;", (next_regime,))
    conn.commit()
    conn.close()

    reason = "TOU_16_21" if tou_active else "AIRGAP_5_32"
    return f"[{now_local.strftime('%H:%M:%S')}] SWEEP({reason}) -> Next={next_regime} | Lint: {lint_report} | Git: {git_report} | Archived: {archived_num}"

def main() -> None:
    signal.signal(signal.SIGINT, _handle_sigterm)
    signal.signal(signal.SIGTERM, _handle_sigterm)
    repo_root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    db_path = Path(sys.argv[2] if len(sys.argv) > 2 else os.environ.get("FLOW_DB_PATH", "flow_substrate.db")).resolve()

    if "--once" in sys.argv:
        print(execute_sweep_cycle(repo_root, db_path) or "No sweep needed (in ACTIVE_27_32 below 27/32 cap and outside TOU peak).")
        return

    print(f"5/32 Airgap Sweeper active on {repo_root} -> {db_path}", flush=True)
    while RUNNING:
        report = execute_sweep_cycle(repo_root, db_path)
        if report:
            print(report, flush=True)
        for _ in range(SWEEP_POLL_SECONDS):
            if not RUNNING:
                break
            pytime.sleep(1)

if __name__ == "__main__":
    main()
'''

# ==============================================================================
# MODULE 6: flow_dashboard.py (Local-Network Browser Terminal GUI on :8732)
# ==============================================================================
MODULES["flow_dashboard.py"] = r'''#!/usr/bin/env python3
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
BIND_PORT = int(os.environ.get("FLOW_DASH_PORT", "8732"))

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
'''

# ==============================================================================
# MODULE 7: flow_up.py (Unified Local Flow IDE Supervisor)
# ==============================================================================
MODULES["flow_up.py"] = r'''#!/usr/bin/env python3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import argparse
import os
import signal
import sqlite3
import subprocess
import sys
import time

HOP_CADENCE_SECONDS = 3
WATCHDOG_POLL_SECONDS = 1
SHUTDOWN_GRACE_SECONDS = 5
RUNNING = True

def _handle_shutdown(signum: int, frame: object) -> None:
    global RUNNING
    RUNNING = False

def ensure_substrate_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS roadmap_nodes (
            node_id         INTEGER PRIMARY KEY CHECK (node_id >= 0 AND node_id < 3200),
            band            INTEGER NOT NULL CHECK (band >= 1 AND band <= 32),
            title           TEXT NOT NULL,
            target_path     TEXT NOT NULL,
            max_loc_budget  INTEGER NOT NULL DEFAULT 150,
            progress_num    INTEGER NOT NULL DEFAULT 0,
            progress_den    INTEGER NOT NULL DEFAULT 1 CHECK (progress_den > 0),
            stall_count     INTEGER NOT NULL DEFAULT 0,
            updated_at_iso  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS roadmap_edges (
            upstream_id     INTEGER NOT NULL REFERENCES roadmap_nodes(node_id) ON DELETE CASCADE,
            downstream_id   INTEGER NOT NULL REFERENCES roadmap_nodes(node_id) ON DELETE CASCADE,
            PRIMARY KEY (upstream_id, downstream_id),
            CHECK (upstream_id != downstream_id)
        );
        CREATE TABLE IF NOT EXISTS relay_batons (
            baton_id        TEXT PRIMARY KEY,
            roadmap_node_id INTEGER NOT NULL REFERENCES roadmap_nodes(node_id),
            from_band       INTEGER NOT NULL CHECK (from_band >= 1 AND from_band <= 32),
            to_band         INTEGER NOT NULL CHECK (to_band >= 1 AND to_band <= 32),
            phase_shift_num INTEGER NOT NULL,
            phase_shift_den INTEGER NOT NULL CHECK (phase_shift_den > 0),
            fuel_num        INTEGER NOT NULL,
            fuel_den        INTEGER NOT NULL CHECK (fuel_den > 0),
            touched_paths   TEXT NOT NULL,
            epistemic_scar  TEXT NOT NULL CHECK (length(epistemic_scar) <= 512),
            created_at_iso  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS phase_cycle_state (
            singleton_id        INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            metonic_tick        INTEGER NOT NULL DEFAULT 0,
            accum_phase_num     INTEGER NOT NULL DEFAULT 0,
            accum_phase_den     INTEGER NOT NULL DEFAULT 32 CHECK (accum_phase_den > 0),
            regime_state        TEXT NOT NULL DEFAULT 'ACTIVE_27_32'
        );
        INSERT OR IGNORE INTO phase_cycle_state (singleton_id) VALUES (1);
    """)
    conn.commit()
    conn.close()

class FlowSupervisor:
    def __init__(self, repo_root: Path, scripts_dir: Path, db_path: Path, logs_dir: Path, pid_file: Path, hop_interval: int, no_runner: bool) -> None:
        self.repo_root = repo_root
        self.scripts_dir = scripts_dir
        self.db_path = db_path
        self.logs_dir = logs_dir
        self.pid_file = pid_file
        self.hop_interval = hop_interval
        self.no_runner = no_runner
        self.daemons: Dict[str, Optional[subprocess.Popen[bytes]]] = {"harvesterd": None, "airgap_sweeper": None, "flow_dashboard": None}
        self.log_handles: Dict[str, object] = {}

    def acquire_pid_lock(self) -> None:
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        if self.pid_file.exists():
            try:
                existing_pid = int(self.pid_file.read_text().strip())
                os.kill(existing_pid, 0)
                raise RuntimeError(f"Flow IDE supervisor already running (PID {existing_pid}).")
            except (ProcessLookupError, ValueError):
                pass
        self.pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

    def release_pid_lock(self) -> None:
        try:
            if self.pid_file.exists() and self.pid_file.read_text().strip() == str(os.getpid()):
                self.pid_file.unlink()
        except OSError:
            pass

    def _build_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        env["FLOW_DB_PATH"] = str(self.db_path)
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _daemon_cmd(self, name: str) -> List[str]:
        py = sys.executable
        if name == "harvesterd":
            return [py, str(self.scripts_dir / "harvesterd.py")]
        if name == "airgap_sweeper":
            return [py, str(self.scripts_dir / "airgap_sweeper.py"), str(self.repo_root), str(self.db_path)]
        if name == "flow_dashboard":
            return [py, str(self.scripts_dir / "flow_dashboard.py"), str(self.db_path)]
        raise ValueError(f"Unknown daemon: {name}")

    def spawn_daemon(self, name: str) -> None:
        script_file = self.scripts_dir / f"{name}.py"
        if not script_file.exists():
            return
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.logs_dir / f"{name}.log"
        log_fh = open(log_path, "ab", buffering=0)
        self.log_handles[name] = log_fh
        proc = subprocess.Popen(self._daemon_cmd(name), cwd=self.repo_root, env=self._build_env(), stdout=log_fh, stderr=subprocess.STDOUT, start_new_session=True)
        self.daemons[name] = proc
        print(f"[BOOT] {name:<15} -> PID {proc.pid}", flush=True)

    def run_initial_harvest_probe(self) -> None:
        hs = self.scripts_dir / "harvesterd.py"
        if hs.exists():
            subprocess.run([sys.executable, str(hs), "--once"], cwd=self.repo_root, env=self._build_env(), check=False)

    def check_and_heal_daemons(self) -> None:
        for name, proc in list(self.daemons.items()):
            if proc is not None and proc.poll() is not None and RUNNING:
                print(f"[HEAL] {name} exited; restarting...", flush=True)
                self.spawn_daemon(name)

    def run_single_ephemeral_hop(self) -> None:
        rs = self.scripts_dir / "flow_runner.py"
        if not rs.exists():
            return
        res = subprocess.run([sys.executable, str(rs), str(self.repo_root)], cwd=self.repo_root, env=self._build_env(), capture_output=True, text=True, check=False)
        out = (res.stdout or res.stderr or "").strip()
        if out:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {out.splitlines()[0]}", flush=True)

    def shutdown_all(self) -> None:
        print("\n[STOP] Shutting down Flow IDE substrate daemons...", flush=True)
        for proc in self.daemons.values():
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
        for name, proc in self.daemons.items():
            if proc is None:
                continue
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
                print(f"[STOP] {name:<15} -> exited cleanly", flush=True)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for fh in self.log_handles.values():
            try:
                fh.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        self.release_pid_lock()

    def serve_forever(self) -> None:
        self.acquire_pid_lock()
        try:
            ensure_substrate_db(self.db_path)
            self.run_initial_harvest_probe()
            self.spawn_daemon("harvesterd")
            self.spawn_daemon("airgap_sweeper")
            self.spawn_daemon("flow_dashboard")
            dash_port = os.environ.get("FLOW_DASH_PORT", "8732")
            print(f"\n[LIVE] Flow IDE active | Dashboard: [http://127.0.0.1](http://127.0.0.1):{dash_port} | Ctrl+C to stop.\n", flush=True)
            last_hop_ts = 0.0
            while RUNNING:
                self.check_and_heal_daemons()
                if not self.no_runner and (time.monotonic() - last_hop_ts) >= self.hop_interval:
                    self.run_single_ephemeral_hop()
                    last_hop_ts = time.monotonic()
                time.sleep(WATCHDOG_POLL_SECONDS)
        finally:
            self.shutdown_all()

def main() -> None:
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--scripts-dir", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--db", default=os.environ.get("FLOW_DB_PATH", ""))
    parser.add_argument("--hop-interval", type=int, default=HOP_CADENCE_SECONDS)
    parser.add_argument("--no-runner", action="store_true")
    args = parser.parse_args()

    repo_root = Path(args.repo).resolve()
    flow_dir = repo_root / ".flow"
    db_path = Path(args.db).resolve() if args.db else (flow_dir / "flow_substrate.db")
    FlowSupervisor(repo_root, Path(args.scripts_dir).resolve(), db_path, flow_dir / "logs", flow_dir / "flow_up.pid", max(1, args.hop_interval), args.no_runner).serve_forever()

if __name__ == "__main__":
    main()
'''

# ==============================================================================
# MODULE 8: flow_audit.py (SHA-256 Chain Verifier, Stall Resetter & Exporter)
# ==============================================================================
MODULES["flow_audit.py"] = r'''#!/usr/bin/env python3
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional
import argparse
import hashlib
import json
import os
import sqlite3
import sys

GENESIS_HASH = "0" * 64

def _connect(db_path: Path, readonly: bool = False) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Substrate database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro" if readonly else f"file:{db_path}", uri=True)
    conn.row_factory = sqlite3.Row
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?;", (table_name,)).fetchone() is not None

def verify_archive_chain(db_path: Path) -> int:
    conn = _connect(db_path, readonly=True)
    if not _table_exists(conn, "relay_batons_archive"):
        print("[INFO] relay_batons_archive table does not exist yet (0 archived hops).")
        conn.close()
        return 0
    rows = conn.execute("SELECT archive_id, baton_id, roadmap_node_id, epistemic_scar, created_at_iso, chain_sha256 FROM relay_batons_archive ORDER BY archive_id ASC;").fetchall()
    conn.close()
    if not rows:
        print("[OK]   relay_batons_archive is empty (0 links to verify).")
        return 0

    prev_hash = GENESIS_HASH
    broken: List[Dict[str, Any]] = []
    for r in rows:
        expected = hashlib.sha256(f"{prev_hash}|{r['baton_id']}|{r['roadmap_node_id']}|{r['epistemic_scar']}|{r['created_at_iso']}".encode("utf-8")).hexdigest()
        if expected != r["chain_sha256"]:
            broken.append({"archive_id": r["archive_id"], "baton_id": r["baton_id"], "expected": expected, "stored": r["chain_sha256"]})
        prev_hash = r["chain_sha256"]

    if broken:
        print(f"[FAIL] Archive integrity breach! {len(broken)}/{len(rows)} broken link(s).")
        return 1
    print(f"[PASS] Verified {len(rows)} archived Relay Baton(s). Tip SHA-256: {rows[-1]['chain_sha256']}")
    return 0

def inspect_or_reset_stalls(db_path: Path, apply_reset: bool = False, pivot_band_19: bool = False, min_stalls: int = 2, include_inflight: bool = True) -> int:
    conn = _connect(db_path, readonly=not apply_reset)
    if not _table_exists(conn, "roadmap_nodes"):
        conn.close()
        return 0
    cond = "stall_count >= ? OR (progress_num > 0 AND progress_num < progress_den)" if include_inflight else "stall_count >= ?"
    stalled = conn.execute(f"SELECT node_id, band, title, progress_num, progress_den, stall_count FROM roadmap_nodes WHERE {cond} ORDER BY node_id ASC;", (min_stalls,)).fetchall()
    if not stalled:
        print(f"[OK]   No stalled (stall_count >= {min_stalls}) or orphaned in-flight nodes found.")
        conn.close()
        return 0
    for r in stalled:
        print(f"  - Node {r['node_id']}/3200 | Band {r['band']} | Progress {r['progress_num']}/{r['progress_den']} | Stalls={r['stall_count']} | {r['title']}")
    if not apply_reset:
        print("\nRun with '--apply' to reset these nodes to 0/1 (add '--pivot-b19' to route to Band 19).")
        conn.close()
        return 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for r in stalled:
        conn.execute("UPDATE roadmap_nodes SET progress_num=0, progress_den=1, stall_count=0, band=?, updated_at_iso=? WHERE node_id=?;", (19 if pivot_band_19 else r["band"], now_iso, r["node_id"]))
    conn.commit()
    conn.close()
    print(f"[HEAL] Reset {len(stalled)} node(s) to 0/1.")
    return 0

def export_milestone_summary(db_path: Path, fmt: str = "markdown", out_file: Optional[Path] = None) -> int:
    conn = _connect(db_path, readonly=True)
    nodes = conn.execute("SELECT node_id, band, title, target_path, progress_num, progress_den FROM roadmap_nodes ORDER BY node_id ASC;").fetchall() if _table_exists(conn, "roadmap_nodes") else []
    done = [n for n in nodes if n["progress_num"] == n["progress_den"]]
    batons = conn.execute("SELECT baton_id, roadmap_node_id, from_band, to_band, epistemic_scar FROM relay_batons ORDER BY rowid DESC LIMIT 25;").fetchall() if _table_exists(conn, "relay_batons") else []
    conn.close()

    if fmt == "json":
        rendered = json.dumps({"completed": len(done), "total": len(nodes), "scars": [dict(b) for b in batons]}, indent=2)
    else:
        lines = [f"# Flow IDE Milestone Summary ({len(done)}/{len(nodes)} nodes at 1/1)", ""]
        for n in done:
            lines.append(f"- `{n['node_id']}/3200` (B{n['band']}) `{n['target_path']}` — {n['title']}")
        lines.extend(["", "## Recent Epistemic Scars"])
        for b in batons:
            lines.append(f"- `{b['baton_id']}` (`{b['roadmap_node_id']}/3200`, B{b['from_band']}->B{b['to_band']}): {b['epistemic_scar']}")
        rendered = "\n".join(lines) + "\n"

    if out_file:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(rendered, encoding="utf-8")
        print(f"[EXPORT] Wrote {fmt} summary -> {out_file}")
    else:
        print(rendered, end="")
    return 0

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.environ.get("FLOW_DB_PATH", ".flow/flow_substrate.db"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify")
    p_u = sub.add_parser("unstall")
    p_u.add_argument("--apply", action="store_true")
    p_u.add_argument("--pivot-b19", action="store_true")
    p_u.add_argument("--min-stalls", type=int, default=2)
    p_u.add_argument("--no-inflight", action="store_true")
    p_e = sub.add_parser("export")
    p_e.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p_e.add_argument("--out", default="")
    args = parser.parse_args()
    db_path = Path(args.db).resolve()
    if args.command == "verify":
        sys.exit(verify_archive_chain(db_path))
    elif args.command == "unstall":
        sys.exit(inspect_or_reset_stalls(db_path, args.apply, args.pivot_b19, args.min_stalls, not args.no_inflight))
    elif args.command == "export":
        sys.exit(export_milestone_summary(db_path, args.format, Path(args.out).resolve() if args.out else None))

if __name__ == "__main__":
    main()
'''

# ==============================================================================
# UNIFIED CLI WRAPPER (~/.local/bin/flow)
# ==============================================================================
FLOW_CLI_WRAPPER = r'''#!/usr/bin/env python3
"""
flow — Unified CLI Entrypoint for the Local Flow IDE Substrate
Subcommands:
  flow init [repo]                 Scaffold .flow/ substrate directory & .gitignore rules
  flow slice [repo]                Decompose codebase into <=150 LOC roadmap_nodes (Py/Rs/TS/Go)
  flow up [repo]                   Launch harvester, sweeper, LAN dashboard, & ephemeral runners
  flow ui [repo]                   Launch telemetry, sweeper, & LAN browser GUI only (no SLM hops)
  flow hop [repo]                  Execute a single stateless 1/32 Relay Baton hop and exit
  flow sweep [repo]                Run one deterministic 5/32 Airgap lint/squash/archive sweep
  flow status [repo]               Print exact rational Fuel Index I(t), Phase Dial, & node counts
  flow verify [repo]               Verify cryptographic SHA-256 chain of relay_batons_archive
  flow unstall [repo] [flags]      Inspect or reset stalled nodes (--apply, --pivot-b19)
  flow export [repo] [flags]       Export milestone summary (--format markdown|json, --out FILE)
"""

from fractions import Fraction
from pathlib import Path
import os
import sqlite3
import subprocess
import sys

SHARE_DIR = Path.home() / ".local" / "share" / "flow-ide"
GITIGNORE_ENTRY = ".flow/"

def init_project(repo_root: Path) -> None:
    flow_dir = repo_root / ".flow"
    (flow_dir / "logs").mkdir(parents=True, exist_ok=True)
    gitignore = repo_root / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        lines = {ln.strip() for ln in content.splitlines()}
        if ".flow/" not in lines and ".flow" not in lines:
            sep = "" if content.endswith("\n") or not content else "\n"
            gitignore.write_text(f"{content}{sep}\n# Flow IDE local substrate\n{GITIGNORE_ENTRY}\n", encoding="utf-8")
            print(f"[GIT] Appended '{GITIGNORE_ENTRY}' to {gitignore}")
    else:
        gitignore.write_text(f"# Flow IDE local substrate\n{GITIGNORE_ENTRY}\n", encoding="utf-8")
        print(f"[GIT] Created {gitignore} with '{GITIGNORE_ENTRY}'")
    print(f"[INIT] Flow substrate ready at {flow_dir}")

def print_status(repo_root: Path) -> None:
    db_path = repo_root / ".flow" / "flow_substrate.db"
    if not db_path.exists():
        print(f"No substrate DB found at {db_path}. Run 'flow slice' first.")
        return
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = conn.cursor()

    def has_table(name: str) -> bool:
        return cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?;", (name,)).fetchone() is not None

    print(f"=== FLOW IDE STATUS ({repo_root.name}) ===")
    if has_table("harvester_telemetry"):
        row = cur.execute("SELECT fuel_num, fuel_den, gate_status, sampled_at_iso FROM harvester_telemetry WHERE singleton_id=1;").fetchone()
        if row:
            print(f"Fuel Index I(t) : {Fraction(row[0], row[1])} | Gate: {row[2]} (sampled {row[3]})")
    if has_table("phase_cycle_state"):
        row = cur.execute("SELECT accum_phase_num, accum_phase_den, metonic_tick, regime_state FROM phase_cycle_state WHERE singleton_id=1;").fetchone()
        if row:
            print(f"Phase Dial      : {row[0]}/{row[1]} (Active Cap: 27/32 | Airgap: 5/32) | Regime: {row[3]} | Metonic Tick: {row[2]}")
    if has_table("roadmap_nodes"):
        tot, done, stalled = cur.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN progress_num = progress_den THEN 1 ELSE 0 END),
                   SUM(CASE WHEN stall_count >= 2 THEN 1 ELSE 0 END)
            FROM roadmap_nodes;
        """).fetchone()
        print(f"Central Roadmap : {done or 0}/{tot or 0} completed (1/1) | Stalled: {stalled or 0}")
    if has_table("relay_batons"):
        last = cur.execute("SELECT baton_id, roadmap_node_id, from_band, to_band, epistemic_scar FROM relay_batons ORDER BY rowid DESC LIMIT 1;").fetchone()
        if last:
            print(f"Latest Baton    : {last[0]} | Node {last[1]}/3200 (B{last[2]} -> B{last[3]})")
            print(f"Epistemic Scar  : {last[4]}")
    conn.close()

def parse_repo_and_flags(tail_args: list[str]) -> tuple[Path, list[str]]:
    if tail_args and not tail_args[0].startswith("-"):
        return Path(tail_args[0]).resolve(), tail_args[1:]
    return Path(".").resolve(), tail_args

def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    target_repo, extra_flags = parse_repo_and_flags(args[1:])
    db_path = target_repo / ".flow" / "flow_substrate.db"
    env = os.environ.copy()
    env["FLOW_DB_PATH"] = str(db_path)

    if cmd == "init":
        init_project(target_repo)
    elif cmd == "slice":
        init_project(target_repo)
        subprocess.run([sys.executable, str(SHARE_DIR / "decomposer.py"), str(target_repo), str(db_path)], env=env, check=True)
    elif cmd == "up":
        init_project(target_repo)
        os.execvpe(sys.executable, [sys.executable, str(SHARE_DIR / "flow_up.py"), str(target_repo), "--scripts-dir", str(SHARE_DIR), "--db", str(db_path), *extra_flags], env)
    elif cmd == "ui":
        init_project(target_repo)
        os.execvpe(sys.executable, [sys.executable, str(SHARE_DIR / "flow_up.py"), str(target_repo), "--scripts-dir", str(SHARE_DIR), "--db", str(db_path), "--no-runner", *extra_flags], env)
    elif cmd == "hop":
        os.execvpe(sys.executable, [sys.executable, str(SHARE_DIR / "flow_runner.py"), str(target_repo)], env)
    elif cmd == "sweep":
        os.execvpe(sys.executable, [sys.executable, str(SHARE_DIR / "airgap_sweeper.py"), str(target_repo), str(db_path), "--once"], env)
    elif cmd == "status":
        print_status(target_repo)
    elif cmd in ("verify", "unstall", "export"):
        os.execvpe(sys.executable, [sys.executable, str(SHARE_DIR / "flow_audit.py"), "--db", str(db_path), cmd, *extra_flags], env)
    else:
        print(__doc__.strip())
        sys.exit(1)

if __name__ == "__main__":
    main()
'''

def bootstrap(scaffold_repo: str = "") -> None:
    SHARE_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)

    for filename, source_code in MODULES.items():
        dst = SHARE_DIR / filename
        dst.write_text(source_code.lstrip("\n"), encoding="utf-8")
        dst.chmod(dst.stat().st_mode | stat.S_IXUSR)
        print(f"[WRITE] {filename:<20} -> {dst}")

    FLOW_BIN.write_text(FLOW_CLI_WRAPPER.lstrip("\n"), encoding="utf-8")
    FLOW_BIN.chmod(FLOW_BIN.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"[CLI]   Installed `flow` command  -> {FLOW_BIN} ({len(MODULES)}/8 modules embedded)")

    if scaffold_repo:
        target = Path(scaffold_repo).resolve()
        (target / ".flow" / "logs").mkdir(parents=True, exist_ok=True)
        gitignore = target / ".gitignore"
        if gitignore.exists():
            content = gitignore.read_text(encoding="utf-8")
            if ".flow/" not in {ln.strip() for ln in content.splitlines()}:
                sep = "" if content.endswith("\n") or not content else "\n"
                gitignore.write_text(f"{content}{sep}\n# Flow IDE local substrate\n.flow/\n", encoding="utf-8")
                print(f"[GIT]   Appended '.flow/' to {gitignore}")
        else:
            gitignore.write_text("# Flow IDE local substrate\n.flow/\n", encoding="utf-8")
            print(f"[GIT]   Created {gitignore} with '.flow/'")

    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    if str(BIN_DIR) not in path_dirs:
        print(f"\n[NOTE] Ensure {BIN_DIR} is on your $PATH:")
        print('       export PATH="$HOME/.local/bin:$PATH"')

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Self-extracting one-step Flow IDE installer.")
    parser.add_argument("--scaffold-repo", default="", help="Optional repository path to initialize .flow/ and .gitignore")
    args = parser.parse_args()
    bootstrap(scaffold_repo=args.scaffold_repo)
