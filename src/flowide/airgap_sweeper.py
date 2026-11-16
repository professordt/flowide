#!/usr/bin/env python3
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

def _install_signal_handlers() -> None:
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle_sigterm)
        except (ValueError, OSError, AttributeError):
            continue

def main() -> None:
    _install_signal_handlers()
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
