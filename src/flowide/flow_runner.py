#!/usr/bin/env python3
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
