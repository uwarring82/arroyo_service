"""Policy layer for Arroyo TEC Gateway.

Validates write requests against software limits before they reach
the instrument adapter.  See blueprint §4 (Tier 2 constraints)
and §8.5 (readback tolerance).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import ChannelLimits


@dataclass
class WriteResult:
    """Result of a validated write operation."""
    ok: bool
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    readback_verified: bool = False
    raw_command: Optional[str] = None
    error: Optional[str] = None


READBACK_TOLERANCE = 0.01  # 1 display LSB, §8.5


def validate_setpoint(
    value: float,
    limits: Optional[ChannelLimits],
    current_setpoint: float,
    large_change_threshold: float = 5.0,
) -> tuple[bool, Optional[str], bool]:
    """Validate a setpoint change.

    Returns (ok, error_message, requires_confirmation).
    """
    if limits is None:
        return True, None, False

    if value < limits.temp_min or value > limits.temp_max:
        return False, (
            f"Setpoint {value:.2f}°C is outside software window "
            f"[{limits.temp_min:.1f}, {limits.temp_max:.1f}]°C"
        ), False

    requires_confirmation = abs(value - current_setpoint) > large_change_threshold
    return True, None, requires_confirmation


def validate_current_limit(
    value: float,
    limits: Optional[ChannelLimits],
) -> tuple[bool, Optional[str]]:
    """Validate a current limit change."""
    if limits is None:
        return True, None
    if value <= 0:
        return False, "Current limit must be positive"
    if value > limits.current_max:
        return False, (
            f"Current limit {value:.2f}A exceeds software maximum "
            f"{limits.current_max:.2f}A"
        )
    return True, None


def validate_voltage_limit(
    value: float,
    limits: Optional[ChannelLimits],
) -> tuple[bool, Optional[str]]:
    """Validate a voltage limit change."""
    if limits is None:
        return True, None
    if value <= 0:
        return False, "Voltage limit must be positive"
    if value > limits.voltage_max:
        return False, (
            f"Voltage limit {value:.2f}V exceeds software maximum "
            f"{limits.voltage_max:.2f}V"
        )
    return True, None


def validate_output_enable(
    setpoint: float,
    limits: Optional[ChannelLimits],
) -> tuple[bool, Optional[str]]:
    """Gate output enable on setpoint being within software window.

    Prevents energising a channel with an out-of-policy target
    still loaded in the controller (blueprint §4, Tier 2).
    """
    if limits is None:
        return True, None
    if setpoint < limits.temp_min or setpoint > limits.temp_max:
        return False, (
            f"Cannot enable output: current setpoint {setpoint:.2f}°C "
            f"is outside software window [{limits.temp_min:.1f}, "
            f"{limits.temp_max:.1f}]°C"
        )
    return True, None


def check_readback(commanded: float, readback: float) -> bool:
    """Check if readback matches commanded value within tolerance (§8.5)."""
    return abs(commanded - readback) <= READBACK_TOLERANCE


def check_readback_bool(commanded: bool, readback: bool) -> bool:
    """Check binary readback (output state)."""
    return commanded == readback
