#!/usr/bin/env python3
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

def probe_windows_thermals() -> Fraction:
    # No stdlib thermal readout on Windows (WMI is third-party).
    # Return the same neutral quarter used when Linux sysfs is unavailable.
    return Fraction(1, 4)

def probe_allostatic_load() -> Fraction:
    getloadavg = getattr(os, "getloadavg", None)
    if getloadavg is None:
        # Windows has no load average; fall back to a sane exact fraction.
        return Fraction(1, 5)
    try:
        load_1m, _, _ = getloadavg()
        cores = os.cpu_count() or 1
        # Exact re-entry from the platform decimal string (no binary float in state).
        return clamp_unit(Fraction(str(load_1m)) / max(1, cores))
    except (OSError, AttributeError, ValueError, ZeroDivisionError):
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
    if sys_name == "Darwin":
        c_therm = probe_macos_thermals()
    elif sys_name == "Windows":
        c_therm = probe_windows_thermals()
    else:
        c_therm = probe_linux_thermals()
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
