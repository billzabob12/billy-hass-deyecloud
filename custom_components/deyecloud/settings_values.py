"""Decode settings reported by DeyeCloud, without assuming command success."""

import json
import math

from .battery_parameters import extract_battery_parameter


def decode_settings(battery: dict, order: dict) -> dict:
    """Combine decoded battery fields and supported dynamic-read registers.

    Dynamic register addresses are for single-phase low-voltage Deye hybrids.
    Require both layout markers; leave other layouts unknown.
    """
    settings = {}
    registers = order.get("analysisResult")
    if isinstance(registers, str):
        try:
            registers = json.loads(registers)
        except ValueError:
            registers = None
    if isinstance(registers, dict):
        registers = {str(k).upper().zfill(4): v for k, v in registers.items()}
        if all(marker in registers for marker in ("00F4", "00F8")):
            for key, address in (
                ("max_battery_charge_current", "00D2"),
                ("max_battery_discharge_current", "00D3"),
                ("grid_charge_current", "00E6"),
                ("grid_charge", "00E8"),
            ):
                raw = registers.get(address)
                try:
                    value = float(raw) if not isinstance(raw, bool) else None
                except (ValueError, TypeError):
                    value = None
                if value is None or not math.isfinite(value):
                    continue
                if key == "grid_charge":
                    if value in (0, 1):
                        settings[key] = bool(value)
                elif value >= 0:
                    settings[key] = value

    # The documented decoded endpoint is authoritative where it supplies data.
    for key, parameter in (
        ("max_battery_charge_current", "MAX_CHARGE_CURRENT"),
        ("max_battery_discharge_current", "MAX_DISCHARGE_CURRENT"),
        ("grid_charge_current", "GRID_CHARGE_AMPERE"),
    ):
        value = extract_battery_parameter(battery, (parameter,))
        if value is not None and math.isfinite(value) and value >= 0:
            settings[key] = value
    return settings
