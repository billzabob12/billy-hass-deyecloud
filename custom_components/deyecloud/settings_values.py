"""Decode settings reported by DeyeCloud, without assuming command success."""

import json
import math

from .battery_parameters import extract_battery_parameter


def decode_settings(battery: dict, order: dict) -> dict:
    """Combine decoded battery fields and supported dynamic-read registers.

    Support the two observed Deye hybrid layouts. Require layout markers;
    leave unknown or ambiguous layouts unknown rather than guessing addresses.
    """
    settings = {}
    # The decoded endpoint can lag behind a completed live register read.
    for key, parameter in (
        ("max_battery_charge_current", "MAX_CHARGE_CURRENT"),
        ("max_battery_discharge_current", "MAX_DISCHARGE_CURRENT"),
        ("grid_charge_current", "GRID_CHARGE_AMPERE"),
    ):
        value = extract_battery_parameter(battery, (parameter,))
        if value is not None and math.isfinite(value) and value >= 0:
            settings[key] = value
    registers = order.get("analysisResult")
    if isinstance(registers, str):
        try:
            registers = json.loads(registers)
        except ValueError:
            registers = None
    if isinstance(registers, dict):
        registers = {str(k).upper().zfill(4): v for k, v in registers.items()}
        layouts = [
            (("00F4", "00F8"), ("00D2", "00D3", "00E6", "00E8")),
            (("008E", "0091", "006C", "006D"), ("006C", "006D", "0080", "0082")),
        ]
        matches = [
            addresses for markers, addresses in layouts if all(m in registers for m in markers)
        ]
        if len(matches) == 1:
            for key, address in zip(
                (
                    "max_battery_charge_current",
                    "max_battery_discharge_current",
                    "grid_charge_current",
                    "grid_charge",
                ),
                matches[0],
                strict=True,
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

    return settings
