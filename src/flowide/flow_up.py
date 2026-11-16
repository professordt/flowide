#!/usr/bin/env python3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import argparse
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time

HOP_CADENCE_SECONDS = 3
WATCHDOG_POLL_SECONDS = 1
SHUTDOWN_GRACE_SECONDS = 5
DEFAULT_DASH_PORT = "9898"
RUNNING = True
IS_WINDOWS = os.name == "nt"

def _handle_shutdown(signum: int, frame: object) -> None:
    global RUNNING
    RUNNING = False

def _install_signal_handlers() -> None:
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle_shutdown)
        except (ValueError, OSError, AttributeError):
            continue

def _lan_ip() -> str:
    """Best-effort LAN IPv4 for the startup banner. Never touches /proc."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _pid_alive(pid: int) -> bool:
    """Cross-platform liveness probe. Never touches /proc."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
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

def _spawn_kwargs() -> Dict[str, object]:
    """Detach the child so it outlives the supervisor's terminal."""
    if IS_WINDOWS:
        creationflags = 0
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": creationflags}
    return {"start_new_session": True}

def _terminate_process(proc: "subprocess.Popen[bytes]", force: bool) -> None:
    """Kill the child (and its group where the OS has process groups)."""
    if proc.poll() is not None:
        return
    if not IS_WINDOWS and hasattr(os, "killpg") and hasattr(os, "getpgid"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL if force else signal.SIGTERM)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        if force:
            proc.kill()
        else:
            proc.terminate()
    except OSError:
        pass

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
                if _pid_alive(existing_pid):
                    raise RuntimeError(f"Flow IDE supervisor already running (PID {existing_pid}).")
            except (ValueError, OSError):
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
        proc = subprocess.Popen(
            self._daemon_cmd(name),
            cwd=self.repo_root,
            env=self._build_env(),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            **_spawn_kwargs(),
        )
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
                _terminate_process(proc, force=False)
        deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
        for name, proc in self.daemons.items():
            if proc is None:
                continue
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
                print(f"[STOP] {name:<15} -> exited cleanly", flush=True)
            except subprocess.TimeoutExpired:
                _terminate_process(proc, force=True)
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
            dash_port = os.environ.get("FLOW_DASH_PORT", DEFAULT_DASH_PORT)
            dash_host = os.environ.get("FLOW_DASH_HOST", "0.0.0.0")
            print(
                f"\n[LIVE] Flow IDE active | bind {dash_host}:{dash_port} | local http://127.0.0.1:{dash_port}"
                f" | LAN http://{_lan_ip()}:{dash_port} | Ctrl+C to stop.\n",
                flush=True,
            )
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
    _install_signal_handlers()
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--scripts-dir", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--db", default=os.environ.get("FLOW_DB_PATH", ""))
    parser.add_argument("--hop-interval", type=int, default=HOP_CADENCE_SECONDS)
    parser.add_argument("--port", type=int, default=None, help="dashboard port (default 9898)")
    parser.add_argument("--no-runner", action="store_true")
    args = parser.parse_args()

    if args.port is not None:
        os.environ["FLOW_DASH_PORT"] = str(args.port)
    else:
        os.environ.setdefault("FLOW_DASH_PORT", DEFAULT_DASH_PORT)

    repo_root = Path(args.repo).resolve()
    flow_dir = repo_root / ".flow"
    db_path = Path(args.db).resolve() if args.db else (flow_dir / "flow_substrate.db")
    FlowSupervisor(repo_root, Path(args.scripts_dir).resolve(), db_path, flow_dir / "logs", flow_dir / "flow_up.pid", max(1, args.hop_interval), args.no_runner).serve_forever()

if __name__ == "__main__":
    main()
