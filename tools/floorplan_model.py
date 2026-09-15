# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
Physical Die Floorplan, Pad Placement & Package Pinout Co-Design Models.

Provides:
- PhysicalPad: Representation of an ASIC pad, bondwire, and package lead.
- SsoGroundBounceModel: Analytical model for simultaneous switching output ground bounce.
- IrDropModel: Power distribution network (PDN) IR drop estimator.
- CrossTalkModel: Capacitive mutual coupling estimator across adjacent leadframe pins.
- Assembly generators for physical SSO stress testing and adjacent pin isolation validation.
"""

from dataclasses import dataclass
from typing import Dict, List, Any


@dataclass
class PhysicalPad:
    package_pin: int
    signal_name: str
    pad_cell_type: str
    drive_strength_ma: float = 8.0
    slew_rate_ns: float = 2.0
    bondwire_inductance_nh: float = 0.8
    package_lead_inductance_nh: float = 1.2
    pad_capacitance_pf: float = 2.5


class SsoGroundBounceModel:
    """Analytical model for Simultaneous Switching Output (SSO) ground bounce."""

    @classmethod
    def calculate_bounce(
        cls,
        n_switching_pins: int = 8,
        drive_ma: float = 8.0,
        trise_ns: float = 2.0,
        l_eff_nh: float = 2.0,
    ) -> Dict[str, Any]:
        """
        Calculate ground bounce voltage:
        V_bounce = N * L_eff * (di / dt)
        """
        di_dt = (drive_ma * 1e-3) / (trise_ns * 1e-9)  # Amperes / second
        v_bounce_v = n_switching_pins * (l_eff_nh * 1e-9) * di_dt
        v_bounce_mv = v_bounce_v * 1e3
        noise_margin_mv = 200.0  # 200 mV noise margin on 1.8V core

        return {
            "n_pins": n_switching_pins,
            "v_bounce_mv": round(v_bounce_mv, 2),
            "noise_margin_mv": noise_margin_mv,
            "margin_preserved": v_bounce_mv < noise_margin_mv,
            "pct_vdd": round((v_bounce_v / 1.8) * 100, 2),
        }


class IrDropModel:
    """Power Distribution Network (PDN) IR drop estimator."""

    @classmethod
    def calculate_ir_drop(
        cls,
        peak_current_ma: float = 1.85,
        grid_resistance_ohms: float = 4.2,
        vdd_v: float = 1.8,
    ) -> Dict[str, Any]:
        """
        Calculate worst-case resistive voltage drop across M4/M5 power mesh.
        """
        v_drop_v = (peak_current_ma * 1e-3) * grid_resistance_ohms
        v_drop_mv = v_drop_v * 1e3
        pct_vdd = (v_drop_v / vdd_v) * 100.0

        return {
            "v_drop_mv": round(v_drop_mv, 2),
            "pct_vdd": round(pct_vdd, 2),
            "budget_pass": pct_vdd < 3.0,  # Must be < 3% VDD
        }


class CrossTalkModel:
    """Adjacent pin capacitive cross-talk model."""

    @classmethod
    def calculate_coupling(
        cls,
        v_swing_v: float = 3.3,
        c_mutual_pf: float = 0.15,
        c_load_pf: float = 30.0,
    ) -> Dict[str, Any]:
        """
        Calculate coupled voltage on victim pin:
        V_coupled = V_swing * (C_mutual / (C_mutual + C_load))
        """
        v_coupled_v = v_swing_v * (c_mutual_pf / (c_mutual_pf + c_load_pf))
        v_coupled_mv = v_coupled_v * 1e3
        isolation_db = 20.0 * -1.0 * (c_mutual_pf / (c_mutual_pf + c_load_pf))

        return {
            "v_coupled_mv": round(v_coupled_mv, 2),
            "isolation_db": round(abs(isolation_db), 2),
            "cross_talk_safe": v_coupled_mv < 100.0,  # Below Schmitt trigger threshold
        }


# =========================================================================
# Assembly Firmware Generators for Physical Layout & SSO Testing
# =========================================================================

def build_sso_stress_test_asm(iterations: int = 4) -> List[str]:
    """
    Firmware asserting simultaneous switching across all 8 uio pins (0x00 <-> 0xFF)
    to maximize ground bounce and test core stability under peak di/dt.
    """
    return [
        "init:",
        "    GDIRI 0xFF          ; All 8 pins configured as outputs",
        f"    LDI   R3, {iterations}  ; Number of stress toggles",
        "",
        "sso_loop:",
        "    GWRI  0xFF          ; Drive all 8 pins high simultaneously",
        "    NOP",
        "    GWRI  0x00          ; Drive all 8 pins low simultaneously (max di/dt pull-down)",
        "    NOP",
        "    DECJNZ R3, sso_loop ; Repeat loop",
        "",
        "safe_exit:",
        "    GDIRI 0x00          ; Restore all pins to High-Z input",
        "    HALT",
    ]


def build_pin_isolation_matrix_asm(aggressor_pin: int = 0, victim_pin: int = 1) -> List[str]:
    """
    Firmware toggling aggressor_pin rapidly while victim_pin is monitored.
    """
    aggressor_mask = 1 << aggressor_pin
    return [
        "init:",
        f"    GDIRI 0x{aggressor_mask:02X} ; Aggressor is output, victim is input",
        "    LDI   R3, 0x03      ; 3 toggle iterations",
        "",
        "toggle_loop:",
        f"    GWRI  0x{aggressor_mask:02X} ; Aggressor High",
        "    GRD   R0            ; Sample bus (verify victim remains unaffected)",
        "    GWRI  0x00          ; Aggressor Low",
        "    GRD   R1            ; Sample bus again",
        "    DECJNZ R3, toggle_loop",
        "",
        "done:",
        "    GDIRI 0x00          ; High-Z safe state",
        "    HALT",
    ]
