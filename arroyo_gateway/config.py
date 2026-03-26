"""Configuration loader for Arroyo TEC Gateway."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class ChannelLimits:
    temp_min: float
    temp_max: float
    current_max: float
    voltage_max: float


@dataclass
class DeviceConfig:
    id: str
    name: str
    ip: str
    port: int
    channels: int
    software_limits: dict[int, ChannelLimits]  # keyed by 1-based channel number


@dataclass
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8400
    poll_rate_hz: float = 1.0
    poll_failure_threshold: int = 3
    inactivity_lock_minutes: int = 10
    driver_mode: str = "simulator"  # "simulator" | "hardware"


@dataclass
class Config:
    gateway: GatewayConfig
    devices: list[DeviceConfig]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _parse_limits(limits_dict: dict[str, Any]) -> dict[int, ChannelLimits]:
    """Parse 'ch1', 'ch2', … keys into {1: ChannelLimits, …}."""
    result: dict[int, ChannelLimits] = {}
    for key, val in limits_dict.items():
        if not key.startswith("ch"):
            continue
        ch_num = int(key[2:])
        result[ch_num] = ChannelLimits(
            temp_min=float(val["temp_min"]),
            temp_max=float(val["temp_max"]),
            current_max=float(val["current_max"]),
            voltage_max=float(val["voltage_max"]),
        )
    return result


def load_config(path: str | pathlib.Path = "config.yaml") -> Config:
    """Load and validate configuration from YAML file."""
    path = pathlib.Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    gw_raw = raw.get("gateway", {})
    gw = GatewayConfig(
        host=gw_raw.get("host", "127.0.0.1"),
        port=int(gw_raw.get("port", 8400)),
        poll_rate_hz=float(gw_raw.get("poll_rate_hz", 1.0)),
        poll_failure_threshold=int(gw_raw.get("poll_failure_threshold", 3)),
        inactivity_lock_minutes=int(gw_raw.get("inactivity_lock_minutes", 10)),
        driver_mode=str(gw_raw.get("driver_mode", "simulator")),
    )

    devices: list[DeviceConfig] = []
    for dev_raw in raw.get("devices", []):
        devices.append(
            DeviceConfig(
                id=dev_raw["id"],
                name=dev_raw["name"],
                ip=dev_raw["ip"],
                port=int(dev_raw.get("port", 10001)),
                channels=int(dev_raw.get("channels", 4)),
                software_limits=_parse_limits(dev_raw.get("software_limits", {})),
            )
        )

    return Config(gateway=gw, devices=devices, raw=raw)
