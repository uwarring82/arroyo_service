# Arroyo TEC Gateway — Phase 2

Maintenance gateway for Arroyo Instruments 7154-05-12 multi-channel
TEC controllers. Read-only dashboard plus bounded writes with
maintenance lock, readback verification, and stability checking.

See `arroyo-tec-gateway-blueprint-v0.2.3.md` for the frozen
architectural specification.

**Status:** bench-test prototype. Not production-ready.
See "Known Limitations" below.

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Edit config.yaml: set device IPs, driver_mode, software limits
python -m arroyo_gateway.main
```

Open `http://localhost:8400` in a browser.

## Simulator vs. Hardware

Controlled via `config.yaml`:

```yaml
gateway:
  driver_mode: "simulator"   # "simulator" | "hardware"
```

No source code edits required to switch modes.

## Bench Test Procedure

1. Set `driver_mode: "hardware"` and the real device IP in `config.yaml`.
2. Start the gateway. Verify the read-only dashboard shows live values.
3. Validate each query step by step on one channel:
   `TEC:T?`, `TEC:SET:T?`, `TEC:ITE?`, `TEC:V?`, `TEC:COND?`, `TEC:OUTput?`
4. Acquire maintenance lock via the UI.
5. Apply a small setpoint change within the software window.
6. Toggle output only if the TEC load is safe (non-critical fixture).
7. Check readback verification and stability confirmation in the audit log.

**Do not** test on a unit that is simultaneously under live DAQ control.
The code ships with Level A (advisory) locking only.

## Command Set

Verified against the Arroyo Computer Interfacing Manual (Rev 2021-01).
Channel selection via `TEC:CHAN <n>` before each command group.
Responses are bare numeric values. Commands terminated by CR/LF.

## Data Files

The audit database (`audit.db`) is created next to `config.yaml`.

## Known Limitations (pre-production)

- **Level A lock only.** Level B cooperative locking is required before
  deployment on DAQ-coupled instruments (blueprint §5.2).
- **No authentication.** Config-based `default_user`/`default_role`
  bypass. Real login/session handling deferred to Phase 3.
- **Audit access not role-restricted.** All users see all audit entries.
- **`contested_params` not surfaced.** Stability checker logs events
  but the status API always returns an empty list.

## Project Structure

```
arroyo-gateway/
├── config.yaml                 # Site-specific configuration
├── pyproject.toml              # Python project metadata (v0.2.0)
├── arroyo_gateway/
│   ├── app.py                  # FastAPI application
│   ├── audit.py                # SQLite audit store (append-only)
│   ├── config.py               # Configuration loader
│   ├── driver.py               # Arroyo 7154 TCP adapter + simulator
│   ├── lock.py                 # Per-device maintenance lock manager
│   ├── main.py                 # Entry point
│   ├── policy.py               # Software limit validation
│   └── stability.py            # Post-write stability checker
└── static/
    └── index.html              # Dashboard UI (no external dependencies)
```

## Phase Roadmap

- **Phase 1** (done): Read-only dashboard, SSE live updates, audit.
- **Phase 2** (this release): Maintenance lock (Level A), bounded writes,
  readback verification, stability checks. Bench-test ready.
- **Phase 3** (pending): Authentication, Level B/C locking, admin console,
  Tier 3 access, role-restricted audit, HTTPS.
