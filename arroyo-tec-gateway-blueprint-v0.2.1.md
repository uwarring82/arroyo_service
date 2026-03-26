# Arroyo TEC Gateway — Architectural Blueprint

**Version:** 0.2.1  
**Date:** 2026-03-26  
**Endorsement Marker:** Local stewardship (U. Warring). Not externally endorsed.  
**Status:** Architecture frozen.  
**Changelog:** v0.2.0 → v0.2.1: Phase 1 strictly read-only; truly append-only audit model (stability outcomes as separate events, ref_event as logical reference); Level B lock source requirements specified (atomic acquisition, ownership, stale-lock detection); lock scope explicitly per-device; poll_interval_hz → poll_rate_hz; viewer audit access removed; alarm-visible/alarm-authoritative boundary sharpened; §5.7 scoped to Phase 2+; output-enable rationale added; Phase 2 acceptance wording aligned with role-restricted audit visibility; T17 stale-cache test added; editorial fixes.

---

## 1  Purpose and Scope

This document specifies a local web-based maintenance gateway for Arroyo Instruments 7154-05-12 multi-channel TEC controllers that are already integrated into a live DAQ environment.

The gateway enables support staff and technicians to monitor and, within tightly bounded limits, adjust TEC channel parameters through a simple browser interface on a dedicated service laptop — without requiring direct instrument access, proprietary software, or deep knowledge of the Arroyo command set.

The system is **not** a replacement for the DAQ. It is a parallel maintenance window with read-heavy, write-guarded semantics. "Parallel" refers to availability alongside the DAQ, not to concurrent write authority: at any moment, exactly one controller owns writes (see §5).

### 1.1  Non-Goals

The gateway is explicitly **not** designed for the following purposes. These boundaries protect the project from scope creep and clarify what the system must not become.

- **Not a DAQ replacement or supervisory control system.** The gateway does not run experiment control loops, sequence acquisitions, or manage data streams.
- **Not intended for routine experiment operation.** Technicians use it for monitoring and bounded maintenance adjustments, not for running experiments.
- **Not a multi-user simultaneous tuning interface.** At most one human operator holds write access at a time.
- **Not a full instrument programming front-end.** The standard UI deliberately hides the majority of the Arroyo command set. Only the subset relevant to maintenance is exposed.
- **Not a remote-access portal.** The default deployment binds to localhost. VLAN exposure is an optional hardening step, not an architectural goal.


## 2  Design Principles

1. **Low threshold for users.** A technician who can read a temperature display and press a button should be able to use the interface without training beyond a one-page quickstart.
2. **Gateway as sole trust boundary.** The Arroyo TCP interface (port 10001) is unauthenticated and offers no access control of its own. The gateway is therefore not a convenience layer; it is the real control surface and the only component that enforces access policy, parameter limits, and audit.
3. **Read by default, write by exception.** The UI opens in read-only mode. Write access requires an explicit unlock action.
4. **Single-writer discipline.** At any moment, exactly one controller (DAQ or maintenance gateway) owns write access to a device. Conflict is prevented architecturally, not by convention. See §5.2 for lock strength classification.
5. **Audit everything that changes state.** Every write command is logged with timestamp, user identity, device, channel, parameter, old value, new value, and the raw command string sent to the instrument.
6. **Fail safe.** On communication loss the UI freezes, disables writes, and displays a clear alert. No silent degradation.


## 3  System Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Service Laptop                      │
│                                                      │
│  ┌────────────┐       ┌──────────────────────────┐  │
│  │  Browser    │◄─────►│  Gateway Service          │  │
│  │  (localhost)│  HTTP │  (FastAPI, Python)         │  │
│  └────────────┘       │                            │  │
│                       │  ┌──────────────────────┐  │  │
│                       │  │  Policy Layer         │  │  │
│                       │  │  • auth / roles       │  │  │
│                       │  │  • parameter classes   │  │  │
│                       │  │  • software limits     │  │  │
│                       │  │  • maintenance lock    │  │  │
│                       │  └──────────────────────┘  │  │
│                       │                            │  │
│                       │  ┌──────────────────────┐  │  │
│                       │  │  Instrument Adapter   │  │  │
│                       │  │  • TCP client per unit│  │  │
│                       │  │  • command builder    │  │  │
│                       │  │  • readback verifier  │  │  │
│                       │  └──────────────────────┘  │  │
│                       │                            │  │
│                       │  ┌──────────────────────┐  │  │
│                       │  │  Audit Store          │  │  │
│                       │  │  (SQLite)             │  │  │
│                       │  └──────────────────────┘  │  │
│                       └──────────────────────────────┘  │
│                              │ TCP :10001               │
└──────────────────────────────┼──────────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
        ┌─────┴─────┐   ┌─────┴─────┐   ┌─────┴─────┐
        │ Arroyo     │   │ Arroyo     │   │ Arroyo     │
        │ 7154 #1    │   │ 7154 #2    │   │ 7154 #N    │
        │ (4 ch)     │   │ (4 ch)     │   │ (4 ch)     │
        └───────────┘   └───────────┘   └───────────┘
```

### 3.1  Component Responsibilities

| Component | Responsibility |
|---|---|
| **Browser UI** | Display status, accept bounded user input. No instrument knowledge. |
| **Gateway Service** | HTTP API, authentication, session management, request routing. |
| **Policy Layer** | Enforce parameter classes, software limits, maintenance lock, role checks. |
| **Instrument Adapter** | Translate API calls into Arroyo TCP commands; manage connections; verify readback. |
| **Audit Store** | Append-only log of all state-changing operations and connection events. |

### 3.2  Source-of-Truth Hierarchy

When values differ between layers, the following precedence applies:

**For instrument parameters:**

1. Instrument readback (authoritative)
2. Gateway cache (reflection of last successful poll)
3. UI display (presentation of cache)

If the cache is stale (device degraded or disconnected), the UI must indicate this visually. It must never silently present a cached value as live.

**For lock state:**

1. Shared lock source (flag file, API, or manual acknowledgement — see §5.2)
2. Gateway session state (internal representation)
3. UI banner (display)

If the shared lock source becomes unreachable, the gateway must treat the lock as unresolvable and fall back to `DAQ_OWNS` (safe default).


## 4  Parameter Classification

All instrument parameters are classified into exactly one of three tiers.

**Conceptual boundary:** Tiers 1 and 2 contain *operational* parameters — values that a technician expects to read or adjust during routine maintenance. Tier 3 contains *structural* parameters — settings that modify controller behaviour, measurement interpretation, or communication identity rather than routine operating targets.

> **Provisional command mnemonics.** All Arroyo command strings shown in this section are provisional placeholders based on published Arroyo documentation and ILX/Newport command compatibility. They must be verified against the 7154-05-12 programming manual and bench-tested on the installed units before implementation.

### Tier 1a — Primary Observables (main dashboard, all users)

These are directly legible and action-relevant for any operator at a glance.

| Parameter | Arroyo query | Unit | Notes |
|---|---|---|---|
| Actual temperature | `TEC:T?` | °C | Primary reading |
| Setpoint temperature | `TEC:SET:T?` | °C | |
| Output current | `TEC:ITE?` | A | |
| TEC voltage | `TEC:V?` | V | |
| Output state | `TEC:OUT?` | on/off | |
| Alarm summary | `TEC:ALARM?` | human-readable | Decoded from bitmask; see §4.4 |

### Tier 1b — Diagnostic Observables (detail view, all users)

Available in the channel detail panel. These require more context to interpret correctly and should not clutter the main dashboard.

| Parameter | Arroyo query | Unit | Notes |
|---|---|---|---|
| Current limit (active) | `TEC:LIM:ITE?` | A | |
| Voltage limit (active) | `TEC:LIM:V?` | V | |
| Alarm bitmask (raw) | `TEC:ALARM?` | hex/int | Raw word for diagnostics |
| Sensor reading (raw) | `TEC:SEN?` | varies | Meaning depends on sensor type |
| Fan state | device-level query | on/off | |
| Control mode | `TEC:MODE?` | enumeration | Read-only in standard view |

### Tier 2 — Tunable (requires maintenance lock)

| Parameter | Arroyo command | Constraint |
|---|---|---|
| Temperature setpoint | `TEC:T <value>` | Within software window: T_min ≤ value ≤ T_max |
| Current limit | `TEC:LIM:ITE <value>` | ≤ hardware max × safety factor |
| Voltage limit | `TEC:LIM:V <value>` | ≤ hardware max × safety factor |
| Output enable/disable | `TEC:OUT <0\|1>` | Allowed only when setpoint is within window |

Output enable is gated on the setpoint window to prevent energising a channel with an out-of-policy target still loaded in the controller. Without this check, a technician could inadvertently drive a channel to a setpoint that was set before the gateway was aware of it.

Software limits are defined per device and channel in a configuration file and are **stricter** than the hardware limits programmed into the controller.

### Tier 3 — Protected (admin only; hidden from standard UI)

Tier 3 parameters are *structural*: they modify controller behaviour, measurement interpretation, or communication identity rather than routine operating targets.

| Parameter | Examples |
|---|---|
| PID coefficients | `TEC:PID:P`, `TEC:PID:I`, `TEC:PID:D` |
| Sensor type / calibration | `TEC:SEN:TYP`, calibration tables |
| Control mode (write) | `TEC:MODE` |
| AutoTune | `TEC:ATUNE` |
| Network settings | `TEC:COMM:*` |
| Shutdown behaviour | `TEC:SHTDN:*` |

Tier 3 parameters are excluded from the standard UI entirely. They are accessible only through a dedicated admin page that requires a separate authentication step and logs every command — including the raw command string — individually. See §8.4 for additional constraints on the admin command console.

### 4.4  Alarm Decoding

The gateway decodes the alarm bitmask into human-readable summary text for Tier 1a display (e.g. "NONE", "OVER-TEMP", "OPEN SENSOR"). The raw bitmask is available in Tier 1b for diagnostic purposes. The exact bit definitions must be taken from the 7154 programming manual.


## 5  Maintenance Lock Protocol

### 5.1  Problem Statement

The Arroyo TCP interface accepts commands from any connected client without authentication or arbitration. If both DAQ and gateway send writes simultaneously, the resulting behaviour is undefined and potentially destructive.

The specific risk is not only undefined behaviour. It is also **false operator confidence**: a technician changes a setpoint, the UI reports success, but the DAQ loop writes the old value back 300 ms later. The support staff then wrongly assumes the controller runs at the technician's target. This failure mode must be addressed explicitly (see §5.5).

### 5.2  Lock Strength Classification

The maintenance lock is only considered safety-relevant if the DAQ actively consumes and obeys the same lock state. Manual acknowledgement is operationally useful but does not constitute architectural exclusion.

**Lock scope:** Lock ownership is per device, not global. A technician may hold locks on multiple devices simultaneously, but each device has exactly one lock state. This matters when several 7154 units are deployed.

Three lock strengths are defined:

| Level | Name | Mechanism | Guarantee |
|---|---|---|---|
| **A** | Manual advisory | Technician confirms DAQ writes are paused via checkbox | None — relies on human discipline |
| **B** | Cooperative software lock | DAQ and gateway both read/write the same lock source (flag file, shared key, or API) | Mutual exclusion if both sides are correctly implemented |
| **C** | Enforced network lock | Network path or control routing ensures only one writer can physically reach the device (e.g. managed switch, VLAN reassignment, proxy with exclusive session) | Hardware-enforced exclusion |

**Level B requirements.** For the cooperative lock to qualify as Level B, the shared lock source must support: (i) atomic acquisition (no race between DAQ and gateway), (ii) unambiguous ownership (lock holder identity is part of the record), and (iii) stale-lock detection (expiry timestamp or heartbeat, so that a crashed holder does not block indefinitely). A plain flag file can satisfy these requirements if it is written atomically (e.g. write-to-temp-then-rename) and includes a timestamp, but this must be verified for each DAQ integration.

**Phase 1** operates at Level A (read-only dashboard; no writes of any kind).  
**Phase 2** must operate at Level B minimum before bounded setpoint writes are enabled.  
**Phase 3** should design toward Level C where the network infrastructure permits.

### 5.3  Lock States

```
States:
  DAQ_OWNS     — default. Gateway is read-only.
  MAINT_LOCKED — technician has acquired write access.
                  DAQ must refrain from writes (enforcement
                  level per §5.2).
  CONTESTED    — lock request denied or timed out. Gateway
                  remains read-only. Alert displayed.
```

### 5.4  Lock Lifecycle

1. Technician clicks **Request Maintenance Access** on the UI.
2. Gateway checks DAQ ownership signal (implementation-specific: flag file, shared Redis key, DAQ API call, or manual confirmation).
3. If DAQ releases: state → `MAINT_LOCKED`. UI enables Tier 2 controls. A visible banner shows lock holder, lock level (A/B/C), and countdown.
4. Lock expires after a configurable timeout (default: 15 minutes). Technician can extend.
5. On release or timeout: state → `DAQ_OWNS`. All Tier 2 controls disable. Pending writes are discarded.

### 5.5  Post-Write Stability Verification

To detect the "silent overwrite" failure mode (§5.1), the gateway performs a two-stage readback for setpoints and output state:

1. **Immediate readback** — within the same command exchange, directly after the write.
2. **Stability check** — after 2 polling cycles (i.e. ~2 s at 1 Hz), the gateway re-reads the parameter.

If the value has reverted between stage 1 and stage 2, the parameter is marked as **contested** in the UI with a specific warning: "Value accepted but may have been overwritten by another controller." This does not automatically roll back the write (the gateway cannot know which value is correct), but it alerts the operator to a coordination failure.

### 5.6  Crash Recovery

If the gateway service restarts (crash, manual restart, or laptop reboot), it must:

1. Default to `DAQ_OWNS` on startup.
2. Query the shared lock source immediately.
3. Never inherit a stale `MAINT_LOCKED` state from a previous session.

Lock state is not persisted in SQLite. It exists only in process memory and the shared lock source.

### 5.7  Lock Fallback (Level A)

When Tier 2 writes are enabled (Phase 2+) but DAQ integration is not yet implemented, the lock can operate as a manual confirmation: the technician must acknowledge that DAQ writes are paused before the gateway enables Tier 2 access. The UI displays a prominent banner: **"Advisory lock only — DAQ exclusion is not enforced."** This fallback does not apply to Phase 1, which is strictly read-only regardless of lock state.


## 6  HTTP API Specification

Base URL: `http://localhost:8400/api/v1`

### 6.1  Device and Channel Status

```
GET  /devices
     → [{ id, name, ip, port, channels: int, connected: bool,
          connection_quality: "ok"|"degraded"|"disconnected",
          lock_state, lock_level }]

GET  /devices/{device_id}/status
     → { id, connected, connection_quality, lock_state, lock_level,
         lock_holder, lock_expires, channels: [...] }

GET  /devices/{device_id}/channels/{ch}/status
     → { device_id, channel,
         primary: { actual_temp, setpoint, current, voltage,
                    output_state, alarm_summary },
         diagnostic: { current_limit, voltage_limit, alarm_raw,
                       sensor_raw, fan_state, control_mode },
         software_limits: { temp_min, temp_max, current_max, voltage_max },
         contested_params: [...],
         timestamp, cache_age_ms }
```

### 6.2  Writes (require MAINT_LOCKED state)

Each limit parameter has its own endpoint to avoid partial-failure ambiguity.

```
POST /devices/{device_id}/channels/{ch}/setpoint
     Body: { "value": 22.5 }
     → { ok, old_value, new_value, readback_verified,
         stability_check: "pending"|"confirmed"|"contested" }

POST /devices/{device_id}/channels/{ch}/output
     Body: { "state": true }
     → { ok, old_state, new_state, readback_verified,
         stability_check: "pending"|"confirmed"|"contested" }

POST /devices/{device_id}/channels/{ch}/current-limit
     Body: { "value": 2.0 }
     → { ok, old_value, new_value, readback_verified }

POST /devices/{device_id}/channels/{ch}/voltage-limit
     Body: { "value": 8.0 }
     → { ok, old_value, new_value, readback_verified }
```

**Partial-failure rule:** Each write is atomic and independently verified. If a technician needs to change both limits, they issue two requests. Each succeeds or fails on its own terms. There is no transaction or rollback across endpoints.

**Readback verification response:** The `readback_verified` field reflects immediate readback (stage 1 per §5.5). The `stability_check` field is initially `"pending"` and updated asynchronously via the status endpoint after the stability window elapses.

### 6.3  Lock Management

```
POST /lock/acquire
     Body: { "device_id": "arroyo-1" }
     → { ok, lock_state, lock_level, expires_at }

POST /lock/release
     Body: { "device_id": "arroyo-1" }
     → { ok, lock_state }

POST /lock/extend
     Body: { "device_id": "arroyo-1" }
     → { ok, expires_at }
```

The user identity is taken from the authenticated session, not from the request body.

### 6.4  Audit

```
GET  /audit/log?device_id=...&since=...&until=...&limit=100
     → [{ timestamp, user, device_id, channel, action, parameter,
          old_value, new_value, raw_command, readback_ok }]
```

Audit log access is **role-restricted:**
- `viewer`: no audit access.
- `technician`: may see all Tier 1–2 events; raw command strings are redacted.
- `admin`: full access including raw command strings.

```
GET  /audit/export?format=csv
     → CSV download (admin role required)
```

### 6.5  Authentication

```
POST /auth/login
     Body: { "username": "...", "password": "..." }
     → { token, role, expires_at }
```

Roles: `viewer` (Tier 1 only), `technician` (Tier 1 + Tier 2 with lock), `admin` (all tiers).

Tokens are short-lived (default: 4 hours). Implementation: HTTP-only cookie over localhost.

The audit model assumes **individual logins rather than shared technician accounts**. Each person who uses the gateway has a distinct credential. Shared accounts degrade audit trail integrity and are not supported.


## 7  User Interface Design

### 7.1  Design Philosophy

The UI must be **immediately legible to a technician standing at a rack** — potentially on a laptop screen at arm's length. This means:

- Large, high-contrast temperature readings (≥ 28 px font for primary values).
- Colour-coded status with unambiguous semantics (see §7.5).
- No hidden menus for primary information. Everything a viewer needs is on the main screen.
- Write controls are visually distinct (bordered panel, different background shade) and only appear when maintenance lock is held.
- No jargon beyond what appears on the physical instrument display.

### 7.2  Screen Map — Main Dashboard

The main dashboard shows only Tier 1a (primary observable) data.

```
┌──────────────────────────────────────────────────────────┐
│  ARROYO TEC GATEWAY                    [user] [logout]   │
│  ─────────────────────────────────────────────────────── │
│  Lock: DAQ owns │ or │ MAINTENANCE (B) — user (12:04)    │
│  ─────────────────────────────────────────────────────── │
│                                                          │
│  ┌─── Cryostat East (192.168.1.10) ───── connected ───┐ │
│  │                                                      │ │
│  │  CH 1          CH 2          CH 3          CH 4      │ │
│  │  ┌──────┐     ┌──────┐     ┌──────┐     ┌──────┐   │ │
│  │  │22.00°│     │25.13°│     │      │     │19.87°│   │ │
│  │  │set 22│     │set 25│     │set 20│     │set 20│   │ │
│  │  │0.8 A │     │1.2 A │     │      │     │0.5 A │   │ │
│  │  │3.1 V │     │4.8 V │     │      │     │2.1 V │   │ │
│  │  │ ● ON │     │ ● ON │     │ ◆ OFF│     │ ● ON │   │ │
│  │  └──────┘     └──────┘     └──────┘     └──────┘   │ │
│  │                                                      │ │
│  └──────────────────────────────────────────────────────┘ │
│                                                          │
│  [Request Maintenance Access]         [Audit Log ↗]      │
└──────────────────────────────────────────────────────────┘
```

### 7.3  Channel Detail View

Clicking a channel card opens a detail panel showing both Tier 1a and Tier 1b data, plus maintenance controls when the lock is held.

```
┌────────────────────────────────────────┐
│  Cryostat East — Channel 2            │
│  ──────────────────────────────────── │
│                                        │
│  Actual temperature      25.13 °C      │
│  Setpoint                25.00 °C      │
│  Output current           1.21 A       │
│  TEC voltage              4.82 V       │
│  Output                   ON           │
│  Alarm                    NONE         │
│                                        │
│  ┌──────────────────────────────────┐  │
│  │  Temperature trend (last 10 min) │  │
│  │  ~~~~~~~~~~~─────────────────~~  │  │
│  └──────────────────────────────────┘  │
│                                        │
│  Diagnostics (Tier 1b)                │
│  Current limit   2.00 A  (hw: 4.00)   │
│  Voltage limit   8.00 V  (hw: 12.0)   │
│  Temp window     15–30 °C             │
│  Sensor raw      10.24 kΩ             │
│  Control mode    T (temperature)       │
│  Fan             ON                    │
│                                        │
│  ── Maintenance Controls ───────────  │
│  (visible only when lock is held)      │
│                                        │
│  Setpoint  [ 25.00 ] °C   [Apply]     │
│  Output    [Enable] [Disable]          │
│  Current limit [ 2.00 ] A [Apply]      │
│  Voltage limit [ 8.00 ] V [Apply]      │
│                                        │
│  [Close]                               │
└────────────────────────────────────────┘
```

### 7.4  Interaction Rules

| Action | Precondition | Feedback |
|---|---|---|
| View any parameter | Logged in as viewer+ | Live update at poll rate |
| Click channel card | Any role | Detail panel opens |
| Request maintenance lock | Technician+ role | Confirmation dialog; lock state and level shown in banner |
| Change setpoint | Lock held + value in window | Input validated client-side and server-side; readback displayed; stability check pending |
| Enable/disable output | Lock held | Confirmation dialog ("You are about to disable output on CH 2") |
| Change current limit | Lock held + within software bounds | Same as setpoint |
| Change voltage limit | Lock held + within software bounds | Same as setpoint |
| Access admin page | Admin role | Separate authentication step |
| Export audit log | Admin role | CSV download |

### 7.5  Colour and State Semantics

| Visual | Meaning |
|---|---|
| **Green** (●) | Output on, within nominal range |
| **Amber** (▲) | Warning: value approaching software limit, or readback unverified |
| **Red** (■) | Alarm active or fault condition |
| **Grey** (◆) | Output intentionally off (expected state) |
| **Grey overlay** | Device disconnected — all values stale |
| **Striped/hatched** | Contested parameter — value may have been overwritten (§5.5) |

The distinction between grey (intentional off) and red (fault/alarm) resolves the ambiguity in v0.1 where "output off unexpectedly" was conflated with alarms.

### 7.6  Error and Safety States

| Condition | UI Behaviour |
|---|---|
| Communication degraded (1 failed poll) | Channel cards show subtle "degraded" indicator; values still displayed from cache with age warning |
| Communication lost (N consecutive failures) | Channel cards turn grey; "DISCONNECTED" overlay; all writes disabled |
| Readback mismatch after write | Amber warning banner; value shown with "⚠ unverified" |
| Stability check failed (value reverted) | Striped indicator on parameter; warning: "Value may have been overwritten by another controller" |
| Alarm active on channel | Channel card border turns red; alarm summary shown |
| Lock expired during session | Write controls disable immediately; banner updates; browser notification |
| Value outside software window | Input field rejects; red border; tooltip shows allowed range |
| Setpoint change > 5 °C from current | Extra confirmation dialog ("Large setpoint change — confirm?") |
| Inactivity timeout | UI locks; requires re-authentication to resume (protects shared-laptop scenarios) |

### 7.7  Session and Identity

- Automatic UI lock after 10 minutes of inactivity (configurable).
- On lock, the screen shows a re-authentication prompt — not a full logout, so the technician can resume quickly.
- If a different user logs in, the previous session is terminated and the maintenance lock (if held) is released.
- The UI header always shows the currently authenticated user name.


## 8  Instrument Adapter

### 8.1  Connection Management

Each Arroyo device is managed by a dedicated `ArroyoDriver` instance that:

- Opens a persistent TCP connection to port 10001.
- Sends commands as newline-terminated ASCII strings.
- Reads responses with a configurable timeout (default: 2 s).
- Reconnects automatically on connection loss (with backoff: 1, 2, 4, 8, 16 s).
- Serialises all commands through an asyncio lock (one command at a time per device).

**Connection watchdog:** Each driver instance runs its own asyncio task. A hung socket read on one device must not block the event loop or polling of other devices. The watchdog cancels any command that exceeds the response timeout and marks the device as degraded.

### 8.2  Command Safety

- The adapter exposes only named methods (e.g. `get_temperature(ch)`, `set_setpoint(ch, value)`), never a raw command passthrough for standard API use.
- Every `set_*` method performs an immediate readback query after writing and returns both the commanded and readback values.
- The adapter validates channel numbers (1–4) and basic value types before sending.

### 8.3  Polling and Degradation

Status polling runs as a background task at 1 Hz per device (configurable). Poll results are cached; HTTP GET requests read from cache, not from the instrument directly.

**Graceful degradation model:**

| Consecutive poll failures | Device state | UI effect |
|---|---|---|
| 1 | `degraded` | Subtle warning indicator; values shown from cache with age |
| 2 | `degraded` | Same; reconnection attempt begins |
| 3+ | `disconnected` | Grey overlay; all writes disabled; active reconnection with backoff |
| Poll succeeds after failures | `ok` | Normal display resumes; connection event logged |

This avoids UI flapping on transient network glitches while still surfacing genuine connection loss promptly.

### 8.4  Admin Command Console (Tier 3)

The raw command console is the highest-risk feature in the system. It partially bypasses the architecture's main safety advantage (named, validated methods). The following constraints apply:

1. The console is only accessible when the device is in `MAINT_LOCKED` state.
2. Each raw command is logged individually, including the exact command string, in a separate audit category (`action: 'raw_command'`).
3. Commands matching sensitive prefixes (`TEC:COMM:*`, `TEC:SHTDN:*`, `TEC:ATUNE`) require a second confirmation step.
4. An optional allowlist mode can restrict the console to a predefined set of command prefixes, rejecting anything else.
5. The console is hidden by default and must be explicitly enabled in `config.yaml` (`admin_console.enabled: true`).

### 8.5  Readback Tolerance Model

Different parameter types require different verification logic:

| Parameter | Readback method | Tolerance | Notes |
|---|---|---|---|
| Setpoint | Query `TEC:SET:T?` | Exact match or ≤ 1 display LSB (0.01 °C) | Command-level quantity; should match precisely |
| Current limit | Query `TEC:LIM:ITE?` | Exact match or ≤ 1 display LSB | |
| Voltage limit | Query `TEC:LIM:V?` | Exact match or ≤ 1 display LSB | |
| Output state | Query `TEC:OUT?` | Exact match (0 or 1) | Binary; no tolerance |
| Actual temperature | N/A | **Not a write-readback quantity** | Dynamic; verified only against setpoint proximity, never as write confirmation |

The exact display resolution (number of decimal places returned by the instrument) must be determined from the 7154 programming manual. The tolerance is defined as "1 unit in the least significant digit of the instrument's response format."

**Response format note:** The implementation must determine whether the 7154 returns bare numeric values (e.g. `25.00`) or values with unit suffixes (e.g. `25.00C`). This affects parsing logic and must be bench-tested.


## 9  Alarm Philosophy

The gateway is **alarm-visible, not alarm-authoritative**. It surfaces alarm information but does not act as an alarm manager: it displays, but does not arbitrate, clear, or own alarm handling. The following policy applies:

| Condition | Gateway response |
|---|---|
| Alarm active, gateway in read-only mode | Display alarm in Tier 1a summary (red indicator). No further action — the DAQ or operator handles the alarm. |
| Alarm active, maintenance lock held | Display alarm prominently. Tier 2 writes remain enabled (the technician may need to adjust setpoints to resolve the alarm condition). However, the alarm state is shown alongside any write confirmation so the operator is always aware. |
| Alarm active, output enable requested | Additional confirmation: "Channel is in alarm state. Confirm output enable?" |

The gateway does **not** automatically disable writes on alarm, because an alarm may be the reason the technician needs write access (e.g. to lower a setpoint that is causing over-temperature). Suppressing writes during alarm would create a paradox.

The gateway does **not** acknowledge or clear alarms. Alarm management remains with the DAQ or the instrument's front panel.


## 10  Audit and Logging

### 10.1  Audit Log Schema (SQLite)

```sql
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,          -- ISO 8601 UTC
    user        TEXT NOT NULL,
    role        TEXT NOT NULL,
    device_id   TEXT NOT NULL,
    channel     INTEGER,                -- NULL for device-level actions
    action      TEXT NOT NULL,          -- e.g. 'set_setpoint', 'enable_output',
                                        --      'stability_confirmed',
                                        --      'stability_contested',
                                        --      'raw_command', 'lock_acquire',
                                        --      'connection_lost'
    parameter   TEXT,
    old_value   TEXT,
    new_value   TEXT,
    raw_command TEXT,                   -- exact string sent to instrument
    readback_ok BOOLEAN,
    ref_event   INTEGER,               -- NULL for original events; set to the id
                                        -- of the originating write for stability
                                        -- follow-up events. Logical reference only,
                                        -- not a SQLite foreign key.
    notes       TEXT
);
```

### 10.2  Logging Rules

- Every Tier 2 and Tier 3 write is logged before the response is sent to the browser.
- Stability check outcomes (§5.5) are logged as **separate events** referencing the original write via `ref_event`. The two possible actions are `stability_confirmed` and `stability_contested`. This keeps the audit table strictly append-only.
- Lock acquisitions, releases, expirations, denied requests, and level changes are logged.
- Connection events (connect, degrade, disconnect, reconnect) are logged.
- Session events (login, logout, inactivity lock, forced session termination) are logged.
- Read-only queries are **not** logged individually (they are too frequent and not safety-relevant).
- The audit table is **strictly append-only**. No UPDATE or DELETE operations exist in the schema or the API.


## 11  Security

### 11.1  Network

- The gateway binds to `127.0.0.1:8400` by default (localhost only).
- If access from other machines on the lab VLAN is required, bind to the VLAN interface only — never to `0.0.0.0`.
- The Arroyo devices should be on a dedicated subnet or VLAN. Direct access from general lab machines to port 10001 should be blocked at the switch or firewall level.
- The instrument protocol itself is unauthenticated (Telnet-style TCP). The gateway is therefore the entire trust boundary, not a convenience layer.

### 11.2  Authentication and Identity

- Local user database (hashed passwords, bcrypt) stored in SQLite.
- Three roles: `viewer`, `technician`, `admin`.
- Session tokens with configurable expiry (default: 4 hours).
- No default passwords. Initial setup creates an admin account interactively.
- **Individual accounts only.** Shared technician accounts are not supported. The audit trail depends on individual identity.
- Automatic UI lock after inactivity (default: 10 minutes). Quick re-authentication to resume.
- Explicit operator switching: if a different user logs in on the same browser, the previous session terminates and any held maintenance lock is released.

### 11.3  Transport

- Since traffic is localhost-only in the default configuration, HTTPS is optional but recommended if the gateway is ever exposed on the VLAN.
- The Arroyo TCP connection is plaintext. This is a fixed constraint of the hardware.


## 12  Configuration

All site-specific settings live in a single YAML file (`config.yaml`):

```yaml
gateway:
  host: "127.0.0.1"
  port: 8400
  poll_rate_hz: 1.0
  poll_failure_threshold: 3        # consecutive failures before 'disconnected'
  lock_timeout_minutes: 15
  large_setpoint_change_threshold: 5.0  # °C, triggers extra confirmation
  stability_check_cycles: 2        # polling cycles before stability verdict
  inactivity_lock_minutes: 10

devices:
  - id: "arroyo-1"
    name: "Cryostat East"
    ip: "192.168.1.10"
    port: 10001
    channels: 4
    software_limits:
      ch1: { temp_min: 15.0, temp_max: 30.0, current_max: 2.0, voltage_max: 8.0 }
      ch2: { temp_min: 10.0, temp_max: 35.0, current_max: 3.0, voltage_max: 10.0 }
      ch3: { temp_min: 15.0, temp_max: 30.0, current_max: 2.0, voltage_max: 8.0 }
      ch4: { temp_min: 15.0, temp_max: 30.0, current_max: 2.0, voltage_max: 8.0 }

  - id: "arroyo-2"
    name: "Cryostat West"
    ip: "192.168.1.11"
    port: 10001
    channels: 4
    software_limits:
      # ...

auth:
  token_expiry_hours: 4
  max_failed_logins: 5

daq_integration:
  mode: "manual"          # "manual" | "flag_file" | "api"
  flag_file_path: null    # used if mode == "flag_file"
  api_url: null           # used if mode == "api"

admin_console:
  enabled: false          # must be explicitly enabled
  allowlist_mode: false   # if true, only allowlisted command prefixes are accepted
  allowlisted_prefixes: []
```


## 13  Technology Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python ≥ 3.10 | Already in lab ecosystem; async support |
| Web framework | FastAPI | Async, automatic OpenAPI docs, lightweight |
| Instrument transport | asyncio TCP (raw sockets) | Matches Arroyo Telnet protocol; per-device task isolation |
| Database | SQLite | Zero-config; sufficient for audit log at this scale |
| Frontend | Single-page HTML + vanilla JS | Lowest deployment friction; no build step |
| Charts | Lightweight JS library (e.g. uPlot or Chart.js) | Trend display for channel detail |
| Deployment | Python venv or single Docker container | Minimal ops burden on service laptop |


## 14  Phased Implementation Plan

### Phase 1 — Read-Only Dashboard

**Lock level:** A (manual advisory).

**Deliverables:**
- `ArroyoDriver` class: connect, poll, parse status for all four channels; connection watchdog; graceful degradation.
- FastAPI backend with `/devices` and `/status` endpoints.
- Static HTML dashboard showing all devices and channels with live polling (1 Hz via periodic fetch or SSE).
- SQLite audit table created; logging connection events and logins.
- Login with viewer role.
- Inactivity lock.

**Acceptance criteria:**
- Technician can open `http://localhost:8400` and see live temperatures, currents, voltages, and output states for all channels of all configured devices.
- Communication degradation is shown after 1 failed poll; disconnection after 3.
- UI locks after 10 minutes of inactivity.

### Phase 2 — Bounded Writes

**Lock level:** B (cooperative software lock) required before this phase ships.

**Deliverables:**
- Maintenance lock protocol with Level B integration.
- Tier 2 write endpoints: setpoint, output enable/disable, current limit, voltage limit (separate endpoints).
- Policy enforcement: software limits, range validation, readback verification, stability check.
- Audit logging for all writes including stability outcomes.
- Technician role; login required for write access.
- Channel detail view with Tier 1b diagnostics and maintenance controls.

**Acceptance criteria:**
- Technician can acquire lock, change a setpoint within the software window, and see readback confirmation.
- Stability check detects and flags a value overwritten by a simulated competing writer.
- Out-of-range values are rejected with a clear message.
- Every write is recorded in the audit store with old/new values, raw command string, and stability outcome. Role-restricted views apply per §6.4 (technicians see redacted commands; admins see full detail).

### Phase 3 — Admin and Hardening

**Lock level:** Design toward C where infrastructure permits.

**Deliverables:**
- Admin page for Tier 3 parameters (PID, sensor config, etc.).
- Raw command console with per-command audit logging, confirmation for sensitive prefixes, optional allowlist mode.
- DAQ integration mode upgrade (flag file or API, depending on DAQ architecture).
- Lock timeout enforcement and automatic release.
- Role-restricted audit log access and CSV export (admin only).
- Optional: HTTPS, VLAN binding.

**Acceptance criteria:**
- Admin can access Tier 3 controls after secondary authentication.
- Every raw command is individually logged with the exact command string.
- DAQ and gateway never write simultaneously (verified by test scenario).
- Sensitive command prefixes require second confirmation.


## 15  Acceptance Test Plan

This is a minimal acceptance-oriented test matrix. Each scenario should be verified manually during Phase 1–3 development and, where feasible, automated as integration tests.

| # | Scenario | Expected behaviour | Phase |
|---|---|---|---|
| T1 | Connection loss during status poll | Device degrades after 1 failure; disconnects after N; UI updates accordingly | 1 |
| T2 | Connection loss during active write | Write returns error; UI shows failure; audit logs the attempt | 2 |
| T3 | Lock timeout expires during active session | All write controls disable; banner updates; browser notification; lock released | 2 |
| T4 | Lock request while DAQ is active (Level B) | Request denied or queued; UI shows `CONTESTED`; gateway remains read-only | 2 |
| T5 | Readback mismatch after setpoint write | Response includes `readback_verified: false`; UI shows amber warning | 2 |
| T6 | Stability check detects overwritten value | Parameter marked as contested; striped indicator; specific warning text | 2 |
| T7 | Device reboot during polling | Connection lost → reconnect with backoff → poll resumes → UI recovers | 1 |
| T8 | Invalid channel index in API call | 400 error with clear message; no command sent to instrument | 1 |
| T9 | Setpoint change exceeding 5 °C threshold | Extra confirmation dialog before write proceeds | 2 |
| T10 | Setpoint outside software window | Rejected client-side and server-side; red border; tooltip shows range | 2 |
| T11 | Two browser tabs attempt simultaneous lock | Second request denied; only one lock holder at a time | 2 |
| T12 | Gateway crash and restart | Defaults to `DAQ_OWNS`; no phantom lock persists | 2 |
| T13 | Raw command with sensitive prefix (admin) | Second confirmation required; command and confirmation logged | 3 |
| T14 | Inactivity timeout on shared laptop | UI locks; re-authentication required; maintenance lock released | 1 |
| T15 | Concurrent poll of N devices, one hangs | Hung device times out independently; other devices continue polling normally | 1 |
| T16 | Alarm active during maintenance lock | Alarm displayed; writes remain enabled; output enable shows extra confirmation | 2 |
| T17 | Stale cache presented after reconnect race | Once fresh polling resumes, stale markers clear only after successful readback; no stale values are silently shown as live | 1 |


## 16  Open Questions

These items require decisions before or during implementation:

1. **DAQ integration mechanism.** What interface does the DAQ expose for signalling write ownership? (Flag file, shared memory, REST API, EPICS PV, manual?) This determines the lock protocol implementation and whether Level B can be achieved for Phase 2.

2. **Exact Arroyo command set.** The blueprint uses provisional command mnemonics based on Arroyo documentation and ILX/Newport compatibility. The actual command strings, response formats (bare float vs. unit-suffixed), and display resolution must be verified against the 7154-05-12 programming manual and bench-tested on the installed units.

3. **Number of devices.** The architecture supports N devices. The initial deployment target (number of 7154 units, network topology) should be confirmed. If N is large enough that serial 1 Hz polling introduces latency, the polling architecture may need adjustment.

4. **Sensor types in use.** The Tier 1b readback parsing depends on whether channels use thermistors, RTDs, or other sensor types. This affects unit display, raw value interpretation, and calibration visibility.

5. **Physical access context.** Is the service laptop always at the rack, or sometimes remote? This affects whether localhost binding is sufficient or VLAN access is needed from day one.

6. **Alarm bit definitions.** The 7154 alarm bitmask must be decoded from the programming manual. The Tier 1a summary text depends on this.

---

*End of blueprint.*
