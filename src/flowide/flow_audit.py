#!/usr/bin/env python3
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
