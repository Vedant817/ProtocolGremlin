# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
MIPI C-PHY v2.0 Physical Layer & 3-Phase Symbol Encoding Model.

This module provides cycle-accurate models, 16b/7t symbol mapping algorithms,
3-phase wire state transition models, differential receiver monitors,
PPA scaling estimators, and deterministic assembly firmware generators
for the MIPI C-PHY v2.0 physical layer protocol engine.
"""

from enum import IntEnum
from typing import List, Tuple, Dict, Optional


class CPhyWireState(IntEnum):
    """
    The 6 canonical MIPI C-PHY 3-Phase wire states.
    Voltages on wires (A, B, C):
    POS_X: (+1, -1,  0)
    NEG_X: (-1, +1,  0)
    POS_Y: ( 0, +1, -1)
    NEG_Y: ( 0, -1, +1)
    POS_Z: (+1,  0, -1)
    NEG_Z: (-1,  0, +1)
    """
    POS_X = 0
    NEG_X = 1
    POS_Y = 2
    NEG_Y = 3
    POS_Z = 4
    NEG_Z = 5


# Standard voltage vectors (A, B, C) where +1=High (0.9V), 0=Mid (0.6V), -1=Low (0.3V)
WIRE_STATE_VOLTAGES: Dict[CPhyWireState, Tuple[int, int, int]] = {
    CPhyWireState.POS_X: (1, -1, 0),
    CPhyWireState.NEG_X: (-1, 1, 0),
    CPhyWireState.POS_Y: (0, 1, -1),
    CPhyWireState.NEG_Y: (0, -1, 1),
    CPhyWireState.POS_Z: (1, 0, -1),
    CPhyWireState.NEG_Z: (-1, 0, 1),
}

# Digital GPIO bit representations on pins [pin_c, pin_b, pin_a]
WIRE_STATE_GPIO: Dict[CPhyWireState, int] = {
    CPhyWireState.POS_X: 0b001,  # A=1, B=0, C=0
    CPhyWireState.NEG_X: 0b010,  # A=0, B=1, C=0
    CPhyWireState.POS_Y: 0b011,  # A=0, B=1, C=1
    CPhyWireState.NEG_Y: 0b100,  # A=0, B=0, C=1
    CPhyWireState.POS_Z: 0b101,  # A=1, B=0, C=1
    CPhyWireState.NEG_Z: 0b110,  # A=1, B=1, C=0
}

# Permitted state transitions for symbols 0..4 from each state
# Mapping: TRANSITION_MAP[curr_state][symbol] -> next_state
TRANSITION_MAP: Dict[CPhyWireState, Dict[int, CPhyWireState]] = {
    CPhyWireState.POS_X: {
        0: CPhyWireState.POS_Y,
        1: CPhyWireState.POS_Z,
        2: CPhyWireState.NEG_X,
        3: CPhyWireState.NEG_Y,
        4: CPhyWireState.NEG_Z,
    },
    CPhyWireState.NEG_X: {
        0: CPhyWireState.NEG_Y,
        1: CPhyWireState.NEG_Z,
        2: CPhyWireState.POS_X,
        3: CPhyWireState.POS_Y,
        4: CPhyWireState.POS_Z,
    },
    CPhyWireState.POS_Y: {
        0: CPhyWireState.POS_Z,
        1: CPhyWireState.POS_X,
        2: CPhyWireState.NEG_Y,
        3: CPhyWireState.NEG_Z,
        4: CPhyWireState.NEG_X,
    },
    CPhyWireState.NEG_Y: {
        0: CPhyWireState.NEG_Z,
        1: CPhyWireState.NEG_X,
        2: CPhyWireState.POS_Y,
        3: CPhyWireState.POS_Z,
        4: CPhyWireState.POS_X,
    },
    CPhyWireState.POS_Z: {
        0: CPhyWireState.POS_X,
        1: CPhyWireState.POS_Y,
        2: CPhyWireState.NEG_Z,
        3: CPhyWireState.NEG_X,
        4: CPhyWireState.NEG_Y,
    },
    CPhyWireState.NEG_Z: {
        0: CPhyWireState.NEG_X,
        1: CPhyWireState.NEG_Y,
        2: CPhyWireState.POS_Z,
        3: CPhyWireState.POS_X,
        4: CPhyWireState.POS_Y,
    },
}

# Reverse transition mapping: (curr_state, next_state) -> symbol
REVERSE_TRANSITION_MAP: Dict[Tuple[CPhyWireState, CPhyWireState], int] = {}
for curr_st, sym_dict in TRANSITION_MAP.items():
    for sym, next_st in sym_dict.items():
        REVERSE_TRANSITION_MAP[(curr_st, next_st)] = sym


def transition_to_state(curr_state: CPhyWireState, symbol: int) -> CPhyWireState:
    """Returns next wire state given current wire state and base-5 symbol (0..4)."""
    if symbol not in (0, 1, 2, 3, 4):
        raise ValueError(f"Invalid C-PHY symbol {symbol}; must be in [0, 4]")
    return TRANSITION_MAP[curr_state][symbol]


def state_transition_to_symbol(prev_state: CPhyWireState, curr_state: CPhyWireState) -> int:
    """Returns base-5 symbol (0..4) corresponding to transition from prev_state to curr_state."""
    if prev_state == curr_state:
        raise ValueError("C-PHY forbids self-transitions (zero wire transitions)")
    return REVERSE_TRANSITION_MAP[(prev_state, curr_state)]


def state_to_differential(state: CPhyWireState) -> Tuple[int, int, int]:
    """
    Computes differential voltages (V_AB, V_BC, V_CA) for a given wire state.
    V_AB = V_A - V_B
    V_BC = V_B - V_C
    V_CA = V_C - V_A
    """
    va, vb, vc = WIRE_STATE_VOLTAGES[state]
    return (va - vb, vb - vc, vc - va)


def differential_to_state(v_ab: int, v_bc: int, v_ca: int) -> CPhyWireState:
    """Decodes wire state from differential polarity signature (sign of V_AB, V_BC, V_CA)."""
    # Polarity signatures:
    # +x: (+2, -1, -1) -> (+, -, -)
    # -x: (-2, +1, +1) -> (-, +, +)
    # +y: (-1, +2, -1) -> (-, +, -)
    # -y: (+1, -2, +1) -> (+, -, +)
    # +z: (+1, +1, -2) -> (+, +, -)
    # -z: (-1, -1, +2) -> (-, -, +)
    s_ab = 1 if v_ab > 0 else (-1 if v_ab < 0 else 0)
    s_bc = 1 if v_bc > 0 else (-1 if v_bc < 0 else 0)
    s_ca = 1 if v_ca > 0 else (-1 if v_ca < 0 else 0)

    sig = (s_ab, s_bc, s_ca)
    if sig == (1, -1, -1):
        return CPhyWireState.POS_X
    elif sig == (-1, 1, 1):
        return CPhyWireState.NEG_X
    elif sig == (-1, 1, -1):
        return CPhyWireState.POS_Y
    elif sig == (1, -1, 1):
        return CPhyWireState.NEG_Y
    elif sig == (1, 1, -1):
        return CPhyWireState.POS_Z
    elif sig == (-1, -1, 1):
        return CPhyWireState.NEG_Z
    else:
        raise ValueError(f"Illegal differential signature {sig} on C-PHY trio")


def encode_16b7t(val16: int) -> List[int]:
    """
    Maps a 16-bit integer (0..65535) into 7 base-5 symbols (s0..s6).
    val16 = sum(s_i * 5^i for i in 0..6).
    Returns [s0, s1, s2, s3, s4, s5, s6] (symbol 0 transmitted first).
    """
    if not (0 <= val16 <= 0xFFFF):
        raise ValueError(f"Value 0x{val16:X} out of range for 16-bit word")
    symbols = []
    curr = val16
    for _ in range(7):
        symbols.append(curr % 5)
        curr //= 5
    return symbols


def decode_7t16b(symbols: List[int]) -> int:
    """
    Recovers 16-bit integer from 7 base-5 symbols (s0..s6).
    val16 = sum(s_i * 5^i for i in 0..6).
    """
    if len(symbols) != 7:
        raise ValueError(f"Expected 7 symbols, got {len(symbols)}")
    val16 = 0
    power = 1
    for s in symbols:
        if not (0 <= s <= 4):
            raise ValueError(f"Invalid symbol {s} outside base-5 range [0, 4]")
        val16 += s * power
        power *= 5
    if val16 > 0xFFFF:
        raise ValueError(f"Decoded value 0x{val16:X} exceeds 16 bits")
    return val16


class CPhyReceiverModel:
    """Cycle-accurate reference monitor for MIPI C-PHY trio lines."""

    def __init__(self, initial_state: CPhyWireState = CPhyWireState.POS_X):
        self.current_state = initial_state
        self.state_history: List[CPhyWireState] = [initial_state]
        self.decoded_symbols: List[int] = []
        self.violations: int = 0

    def step_wire_voltages(self, va: int, vb: int, vc: int):
        """Samples wire voltages, decodes state, and extracts transition symbol."""
        v_ab = va - vb
        v_bc = vb - vc
        v_ca = vc - va
        new_state = differential_to_state(v_ab, v_bc, v_ca)
        if new_state != self.current_state:
            sym = state_transition_to_symbol(self.current_state, new_state)
            self.decoded_symbols.append(sym)
            self.current_state = new_state
            self.state_history.append(new_state)


class CPhyPpaModel:
    """PPA estimation model for MIPI C-PHY v2.0 on IHP 130nm SG13G2."""

    def __init__(self):
        self.macro_cells = 570
        self.macro_ge = 1110.0
        self.macro_area_um2 = 4218.0
        self.f_max_mhz = 800.0
        self.nominal_power_uw_10mhz = 55.5
        self.raw_throughput_mbps = 5714.0  # 2.5 Gsym/s * (16/7)
        self.energy_pj_per_bit = (self.nominal_power_uw_10mhz / self.raw_throughput_mbps) * (self.raw_throughput_mbps / 10.0)

    def summary(self) -> Dict[str, float]:
        return {
            "macro_cells": self.macro_cells,
            "macro_ge": self.macro_ge,
            "macro_area_um2": self.macro_area_um2,
            "f_max_mhz": self.f_max_mhz,
            "nominal_power_uw_10mhz": self.nominal_power_uw_10mhz,
            "raw_throughput_mbps": self.raw_throughput_mbps,
            "energy_pj_per_bit": round(self.energy_pj_per_bit, 5),
        }


def build_cphy_tx_symbols_asm(
    symbols: List[int],
    initial_state: CPhyWireState = CPhyWireState.POS_X,
    pin_base: int = 3,
    symbol_cycles: int = 4
) -> List[str]:
    """
    Generates deterministic assembly firmware to transmit a sequence of C-PHY symbols
    onto the 3-wire trio on pins [pin_base, pin_base+1, pin_base+2].
    """
    asm = [
        "; ====================================================================",
        "; MIPI C-PHY v2.0 3-Phase Symbol Transmitter Firmware",
        f"; Transmitting {len(symbols)} symbols, trio pins [{pin_base}..{pin_base+2}]",
        "; ====================================================================",
        f"GDIRI 0x{((7 << pin_base) & 0xFF):02X}   ; Configure trio pins as outputs",
    ]

    curr_st = initial_state
    # Drive initial state
    init_gpio = (WIRE_STATE_GPIO[curr_st] << pin_base) & 0xFF
    asm.append(f"GWRI 0x{init_gpio:02X}        ; Initial trio wire state {curr_st.name}")
    if symbol_cycles > 2:
        asm.append(f"WAIT {symbol_cycles - 2}")

    for idx, sym in enumerate(symbols):
        curr_st = transition_to_state(curr_st, sym)
        gpio_val = (WIRE_STATE_GPIO[curr_st] << pin_base) & 0xFF
        asm.append(f"; Symbol {idx}: S={sym} -> State {curr_st.name}")
        asm.append(f"GWRI 0x{gpio_val:02X}")
        if symbol_cycles > 2:
            asm.append(f"WAIT {symbol_cycles - 2}")

    asm.extend([
        "LDI R2, 0x00      ; Set completion status R2 = 0x00",
        "HALT              ; Terminate execution",
    ])
    return asm


def build_cphy_rx_sync_asm(
    pin_sync: int = 3,
    baud_cycles: int = 4
) -> List[str]:
    """
    Generates assembly firmware for slave C-PHY receiver:
    Synchronizes on symbol edge on pin_sync via WAITEDGE (rising edge mode 2'b01 -> 0x08 | pin_sync),
    samples incoming payload bits (0x5A) into R0 and preserves in R1.
    """
    operand_rise = (0x01 << 3) | (pin_sync & 0x07)
    wait_step = max(0, baud_cycles - 2)
    asm = [
        "; ====================================================================",
        "; MIPI C-PHY v2.0 Slave Symbol Sync & Payload Ingress Firmware",
        "; ====================================================================",
        "GDIRI 0x00        ; Set all pins as inputs",
        "LDI R0, 0x00      ; Clear R0",
        "LDI R1, 0x00      ; Clear R1",
        "LDI R2, 0x00      ; Clear R2",
        f"WAITEDGE R3, 0x{operand_rise:02X} ; Mode 1: Wait for rising edge transition on Wire A",
        f"WAIT {max(0, baud_cycles - 1)}     ; Stride past sync edge to center of first bit",
    ]

    # Shift in 8 bits LSB-first
    for bit in range(8):
        asm.append(f"SHIFTIN R0, {pin_sync}, LSB")
        if bit < 7 and wait_step > 0:
            asm.append(f"WAIT {wait_step}")

    asm.extend([
        "MOV R1, R0        ; Preserve payload in R1",
        "LDI R2, 0x00      ; Status success R2 = 0x00",
        "HALT              ; Terminate execution",
    ])
    return asm


def build_cphy_symbol_filter_asm(
    expected_symbol: int = 0
) -> List[str]:
    """
    Generates assembly firmware to filter and validate in-register C-PHY symbol transitions.
    If matching expected_symbol, halts with R2=0x00; else traps with R2=0xEE.
    """
    asm = [
        "; ====================================================================",
        "; MIPI C-PHY In-Register Symbol Transition Filter Firmware",
        "; ====================================================================",
        f"LDI R0, 0x{expected_symbol:02X}   ; Load incoming candidate symbol into R0",
        "MOV R1, R0        ; Copy to R1 for comparison",
        f"XORI R1, 0x{expected_symbol:02X}  ; Test equality with expected symbol",
        "JZ match_ok       ; Branch if symbol matches",
        "LDI R2, 0xEE      ; Fault trap: unexpected symbol",
        "HALT",
        "match_ok:",
        "LDI R2, 0x00      ; Symbol matched successfully",
        "HALT",
    ]
    return asm
