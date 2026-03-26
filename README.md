# Arroyo TEC Gateway — Phase 1

Read-only maintenance dashboard for Arroyo Instruments 7154-05-12
multi-channel TEC controllers.

See `arroyo-tec-gateway-blueprint-v0.2.md` for full architectural
specification.

## Quickstart

```bash
# 1. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -e .

# 3. Edit config.yaml with your device IPs/names

# 4. Run (simulator mode by default — no hardware needed)
python -m arroyo_gateway.main
```

Open `http://localhost:8400` in a browser.

## Simulator vs. Hardware

The driver mode is controlled in `config.yaml`:

```yaml
gateway:
  driver_mode: "simulator"   # "simulator" | "hardware"
```

By default the gateway starts with `SimulatedDriver` (no network
connection to real instruments). To connect to actual Arroyo 7154
units:

1. Set `driver_mode: "hardware"` in `config.yaml`.
2. Verify your device IPs are reachable on the lab network.
3. Verify command mnemonics against the 7154-05-12 programming
   manual (see blueprint §4, Open Question #2).

No source code edits are required to switch modes.

## Data Files

The audit database (`audit.db`) is created next to `config.yaml`.
Its location does not depend on the working directory from which
the service is started.

## Project Structure

```
arroyo-gateway/
├── config.yaml                 # Site-specific configuration
├── pyproject.toml              # Python project metadata
├── arroyo_gateway/
│   ├── __init__.py
│   ├── app.py                  # FastAPI application
│   ├── audit.py                # SQLite audit store
│   ├── config.py               # Configuration loader
│   ├── driver.py               # Instrument adapter + simulator
│   └── main.py                 # Entry point
└── static/
    └── index.html              # Dashboard UI (no external dependencies)
```

## Phase Roadmap

- **Phase 1** (this release): Read-only dashboard, SSE live updates,
  simulated and real drivers, audit logging for connection events.
- **Phase 2**: Maintenance lock (Level B), bounded writes, readback
  verification, stability checks.
- **Phase 3**: Admin console, Tier 3 access, DAQ integration, HTTPS.
