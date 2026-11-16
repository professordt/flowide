# flowide

## Install

**Linux / macOS**

```bash
curl -fsSL https://raw.githubusercontent.com/professordt/flowide/main/install.sh | bash
```

**Windows (PowerShell)**

```powershell
irm https://raw.githubusercontent.com/professordt/flowide/main/install.ps1 | iex
```

Then, in a new shell:

```bash
flow up
```

The dashboard opens on **http://127.0.0.1:9898** (and, because it binds `0.0.0.0`,
on `http://<this-machine-LAN-IP>:9898` from other machines — see
[Open it on your network](#open-it-on-your-network)).

Or install with pip:

```bash
pip install git+https://github.com/professordt/flowide.git
flow up
```

---

A local-first IDE substrate that decomposes a codebase into a 3200-node roadmap, paces it with an exact-rational fuel gate (no floating point anywhere in state), and serves a zero-dependency telemetry dashboard.

Requires **Python 3.9+**. No third-party packages — standard library only.

## What it does

- `flow slice` walks your repo and cuts it into ≤150-LOC roadmap nodes wired into a 3200-slot band lattice.
- `flow up` runs three local daemons: a fuel harvester (thermal / load / grid / battery), a 5/32 airgap sweeper (lint + archive + git hygiene), and the dashboard.
- `flow hop` executes one stateless relay-baton hop; `flow status` prints the exact fractions; `flow audit` verifies the SHA-256 baton chain.

## Usage

```
flow init [repo]                 Scaffold .flow/ and .gitignore rules
flow slice [repo]                Decompose the codebase into roadmap nodes
flow up [repo] [--port N]        Launch harvester, sweeper, dashboard, runners
flow ui [repo] [--port N]        Telemetry + sweeper + dashboard only (no hops)
flow down [repo]                 Stop the supervisor recorded in .flow/flow_up.pid
flow hop [repo]                  Execute one baton hop and exit
flow sweep [repo]                One 5/32 lint/squash/archive sweep
flow status [repo]               Fuel index, phase dial, node counts
flow audit [repo]                Verify the baton archive chain
flow unstall [repo]              Inspect or reset stalled nodes (--apply)
flow export [repo]               Milestone summary (--format markdown|json)
flow --help                      This list
```

### Port

The dashboard defaults to **9898**. Override it either way:

```bash
flow up --port 9000
FLOW_DASH_PORT=9000 flow up
```

`FLOW_DASH_HOST` sets the bind address (default `0.0.0.0`, i.e. LAN-visible; use `FLOW_DASH_HOST=127.0.0.1` for loopback only).

### Open it on your network

The dashboard binds `0.0.0.0` by default, so you can open it from any machine on the
same local network:

```
http://<this-machine-LAN-IP>:9898
```

Find the IP with `hostname -I` (Linux/macOS) or `ipconfig` (Windows). The supervisor
prints both the loopback and LAN URLs on startup, for example:

```
[LIVE] Flow IDE active | bind 0.0.0.0:9898 | local http://127.0.0.1:9898 | LAN http://192.168.1.23:9898 | Ctrl+C to stop.
```

Want it local-only instead? Start with `FLOW_DASH_HOST=127.0.0.1 flow up`.

**Security:** the dashboard has **no authentication**. It is intended for a trusted
LAN (your own machines, your own network). Do not expose it to the public internet;
if you need remote access, put it behind a VPN — do not port-forward it.

All local state lives in `<repo>/.flow/` (git-ignored) — database, logs, pid file.

## Platform support

| | Linux | macOS | Windows |
|---|---|---|---|
| CLI, slicing, dashboard | yes | yes | yes |
| Fuel harvester | sysfs thermal/battery | `pmset` / `sysctl` | neutral fallback (no stdlib sensor API) |
| Daemon detach | `setsid` | `setsid` | `DETACHED_PROCESS` |
| Process-group shutdown | `killpg` | `killpg` | `terminate()` / `taskkill /T` |

On Windows the harvester has no standard-library thermal or battery interface, so it reports a neutral quarter for thermal load and relies on the TOU clock and load fallbacks; the gate still works, the sensor detail does not.

`install.ps1` was written and reviewed but **not executed** on a Windows host.

## Alternate bootstrap

`bootstrap_flow.py` is the original single-file self-extractor. Run it with Python 3.9+ if you prefer not to use the shell installers:

```bash
python3 bootstrap_flow.py
```

## Layout

```
flowide/
  install.sh, install.ps1    one-line installers
  bootstrap_flow.py          legacy single-file self-extractor
  src/flowide/
    cli.py                   `flow` entry point
    decomposer.py            codebase -> 3200-node roadmap
    flow_up.py               supervisor (spawn / heal / shutdown)
    harvesterd.py            fuel index I(t) telemetry daemon
    airgap_sweeper.py        5/32 airgap sweep daemon
    flow_dashboard.py        dashboard (port 9898)
    flow_runner.py           single baton hop
    flow_audit.py            chain verify / unstall / export
    airgap_vcs.py            git helpers used by the sweeper
```

## License

MIT — © 2026 Hamboning Media LLC.
