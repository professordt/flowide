#!/usr/bin/env python3
"""
flow — Unified CLI Entrypoint for the Local Flow IDE Substrate

Subcommands:
  flow init [repo]                 Scaffold .flow/ substrate directory & .gitignore rules
  flow slice [repo]                Decompose codebase into <=150 LOC roadmap_nodes (Py/Rs/TS/Go)
  flow up [repo] [--port N]        Launch harvester, sweeper, dashboard, & ephemeral runners
  flow ui [repo] [--port N]        Telemetry, sweeper, & browser GUI only (no SLM hops)
  flow down [repo]                 Stop the supervisor recorded in .flow/flow_up.pid
  flow hop [repo]                  Execute a single stateless 1/32 Relay Baton hop and exit
  flow sweep [repo]                Run one deterministic 5/32 Airgap lint/squash/archive sweep
  flow status [repo]               Print exact rational Fuel Index I(t), Phase Dial, & node counts
  flow audit [repo] [flags]        Verify the SHA-256 chain of relay_batons_archive
  flow verify [repo] [flags]       Same as audit verify (kept for compatibility)
  flow unstall [repo] [flags]      Inspect or reset stalled nodes (--apply, --pivot-b19)
  flow export [repo] [flags]       Export milestone summary (--format markdown|json, --out FILE)
  flow --help                      Show this help
"""

from fractions import Fraction
from pathlib import Path
import os
import signal
import sqlite3
import subprocess
import sys
import time

# Modules install beside this file; nothing outside the package is required.
MODULE_DIR = Path(__file__).resolve().parent
GITIGNORE_ENTRY = ".flow/"
DEFAULT_DASH_PORT = "9898"

HELP_TEXT = __doc__.strip()


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


def _pid_alive(pid: int) -> bool:
    """Cross-platform liveness probe. Never touches /proc."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


def stop_supervisor(repo_root: Path) -> int:
    pid_file = repo_root / ".flow" / "flow_up.pid"
    if not pid_file.exists():
        print(f"[DOWN] No supervisor pid file at {pid_file} (already stopped).")
        return 0
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError) as exc:
        print(f"[DOWN] Unreadable pid file {pid_file}: {exc}")
        return 1
    if not _pid_alive(pid):
        print(f"[DOWN] Supervisor PID {pid} is not running; leaving pid file untouched.")
        return 0
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
        else:
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _pid_alive(pid):
                time.sleep(0.1)
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        print(f"[DOWN] Could not signal PID {pid}: {exc}")
        return 1
    print(f"[DOWN] Supervisor PID {pid} stopped.")
    return 0


def parse_repo_and_flags(tail_args: list[str]) -> tuple[Path, list[str]]:
    if tail_args and not tail_args[0].startswith("-"):
        return Path(tail_args[0]).resolve(), tail_args[1:]
    return Path(".").resolve(), tail_args


def extract_port(flags: list[str], env: dict[str, str]) -> list[str]:
    """Pull --port N (or --port=N) out of the pass-through flags into FLOW_DASH_PORT."""
    kept: list[str] = []
    i = 0
    while i < len(flags):
        tok = flags[i]
        if tok == "--port" and i + 1 < len(flags):
            env["FLOW_DASH_PORT"] = flags[i + 1]
            i += 2
            continue
        if tok.startswith("--port="):
            env["FLOW_DASH_PORT"] = tok.split("=", 1)[1]
            i += 1
            continue
        kept.append(tok)
        i += 1
    env.setdefault("FLOW_DASH_PORT", DEFAULT_DASH_PORT)
    return kept


def main() -> None:
    args = sys.argv[1:]
    if not args:
        cmd = "status"
        target_repo, extra_flags = Path(".").resolve(), []
    elif args[0] in ("-h", "--help", "help"):
        print(HELP_TEXT)
        return
    else:
        cmd = args[0]
        target_repo, extra_flags = parse_repo_and_flags(args[1:])

    db_path = target_repo / ".flow" / "flow_substrate.db"
    env = os.environ.copy()
    env["FLOW_DB_PATH"] = str(db_path)

    if cmd == "init":
        init_project(target_repo)
    elif cmd == "slice":
        init_project(target_repo)
        subprocess.run([sys.executable, str(MODULE_DIR / "decomposer.py"), str(target_repo), str(db_path)], env=env, check=True)
    elif cmd in ("up", "ui"):
        init_project(target_repo)
        flags = extract_port(extra_flags, env)
        if cmd == "ui":
            flags = ["--no-runner", *flags]
        os.execvpe(
            sys.executable,
            [sys.executable, str(MODULE_DIR / "flow_up.py"), str(target_repo), "--scripts-dir", str(MODULE_DIR), "--db", str(db_path), *flags],
            env,
        )
    elif cmd == "down":
        sys.exit(stop_supervisor(target_repo))
    elif cmd == "hop":
        os.execvpe(sys.executable, [sys.executable, str(MODULE_DIR / "flow_runner.py"), str(target_repo)], env)
    elif cmd == "sweep":
        os.execvpe(sys.executable, [sys.executable, str(MODULE_DIR / "airgap_sweeper.py"), str(target_repo), str(db_path), "--once"], env)
    elif cmd == "status":
        print_status(target_repo)
    elif cmd == "audit":
        os.execvpe(sys.executable, [sys.executable, str(MODULE_DIR / "flow_audit.py"), "--db", str(db_path), "verify", *extra_flags], env)
    elif cmd in ("verify", "unstall", "export"):
        os.execvpe(sys.executable, [sys.executable, str(MODULE_DIR / "flow_audit.py"), "--db", str(db_path), cmd, *extra_flags], env)
    else:
        print(HELP_TEXT)
        print(f"\nUnknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
