"""
tools/wiegand_model.py
======================
Wiegand Access Control Protocol & Pulse Width Discovery Model.

Implements:
  1. Standard 26-bit Wiegand (H10301) parity generation and verification
     (Leading Even Parity over bits [24:13], Trailing Odd Parity over bits [12:1]).
  2. Wiegand credential parsing and raw bitstream encoding.
  3. Cycle-accurate WiegandReaderModel simulating an access control panel receiver.
  4. Physical PPA model for synthesizable Wiegand coprocessor macros on IHP 130nm SG13G2.
  5. Firmware generators for:
     - 26-bit Wiegand credential writer/transmitter (pulsed DATA0/DATA1).
     - Pulse width & pulse interval timing discovery via WAITEDGE.
     - Wiegand stream reader with edge polling.
     - Line short / physical cable tamper detection.
"""

import os
import sys
from dataclasses import dataclass
from typing import List, Tuple, Optional

# Support importing assembler whether run from repo root, test/, or tools/
sys.path.insert(0, os.path.dirname(__file__))
from assembler import assemble


def compute_wiegand26_parity(facility_code: int, card_id: int) -> Tuple[int, int]:
    """
    Computes standard 26-bit Wiegand leading even parity and trailing odd parity.

    Bit structure:
      Bit 25: Even Parity over bits [24:13]
      Bits [24:17]: 8-bit Facility Code
      Bits [16:1]:  16-bit Card ID
      Bit 0:  Odd Parity over bits [12:1]

    Bits [24:13]:
      Facility Code [7:0] (8 bits) + Card ID [15:12] (4 bits) = 12 bits
    Bits [12:1]:
      Card ID [11:0] (12 bits)
    """
    fc = facility_code & 0xFF
    cid = card_id & 0xFFFF

    # Upper 12 data bits: FC (8 bits) and upper 4 bits of Card ID
    upper_12 = (fc << 4) | ((cid >> 12) & 0x0F)
    ones_upper = bin(upper_12).count('1')
    # Even parity makes total ones in [25:13] even
    even_parity = ones_upper % 2

    # Lower 12 data bits: lower 12 bits of Card ID
    lower_12 = cid & 0x0FFF
    ones_lower = bin(lower_12).count('1')
    # Odd parity makes total ones in [12:0] odd
    odd_parity = 1 - (ones_lower % 2)

    return even_parity, odd_parity


def build_wiegand26_raw(facility_code: int, card_id: int) -> int:
    """
    Packages Facility Code and Card ID into a 26-bit raw integer word.
    """
    even_p, odd_p = compute_wiegand26_parity(facility_code, card_id)
    raw26 = (even_p << 25) | ((facility_code & 0xFF) << 17) | ((card_id & 0xFFFF) << 1) | odd_p
    return raw26


def verify_wiegand26(raw26: int) -> Tuple[bool, int, int]:
    """
    Validates a 26-bit Wiegand raw word.
    Returns:
      (is_valid, facility_code, card_id)
    """
    even_p = (raw26 >> 25) & 1
    fc = (raw26 >> 17) & 0xFF
    cid = (raw26 >> 1) & 0xFFFF
    odd_p = raw26 & 1

    exp_even_p, exp_odd_p = compute_wiegand26_parity(fc, cid)
    is_valid = (even_p == exp_even_p) and (odd_p == exp_odd_p)
    return is_valid, fc, cid


@dataclass
class WiegandCredential:
    facility_code: int
    card_id: int
    raw26: int
    even_parity: int
    odd_parity: int
    valid: bool


class WiegandReaderModel:
    """
    Cycle-accurate model of a Wiegand access control panel receiver.
    Monitors DATA0 and DATA1 lines and ingresses 26-bit credentials.
    """
    def __init__(self, d0_pin: int = 3, d1_pin: int = 4):
        self.d0_pin = d0_pin
        self.d1_pin = d1_pin
        self.bits: List[int] = []
        self.last_d0 = 1
        self.last_d1 = 1
        self.tamper_detected = False
        self.pulse_widths: List[int] = []
        self.pulse_intervals: List[int] = []
        self.current_pulse_width = 0
        self.cycles_since_last_pulse = 0

    def step(self, uio_out: int, uio_oe: int) -> Optional[WiegandCredential]:
        """
        Steps the receiver model with the current GPIO output state.
        Returns WiegandCredential when a 26-bit frame is completed.
        """
        d0 = (uio_out >> self.d0_pin) & 1 if (uio_oe >> self.d0_pin) & 1 else 1
        d1 = (uio_out >> self.d1_pin) & 1 if (uio_oe >> self.d1_pin) & 1 else 1

        # Check for tamper condition: simultaneous low
        if d0 == 0 and d1 == 0:
            self.tamper_detected = True

        # Edge detection on DATA0 (bit 0)
        if self.last_d0 == 1 and d0 == 0:
            self.bits.append(0)
            if self.cycles_since_last_pulse > 0:
                self.pulse_intervals.append(self.cycles_since_last_pulse)
            self.cycles_since_last_pulse = 0
            self.current_pulse_width = 1
        elif d0 == 0:
            self.current_pulse_width += 1
        elif self.last_d0 == 0 and d0 == 1:
            self.pulse_widths.append(self.current_pulse_width)

        # Edge detection on DATA1 (bit 1)
        if self.last_d1 == 1 and d1 == 0:
            self.bits.append(1)
            if self.cycles_since_last_pulse > 0:
                self.pulse_intervals.append(self.cycles_since_last_pulse)
            self.cycles_since_last_pulse = 0
            self.current_pulse_width = 1
        elif d1 == 0:
            self.current_pulse_width += 1
        elif self.last_d1 == 0 and d1 == 1:
            self.pulse_widths.append(self.current_pulse_width)

        self.cycles_since_last_pulse += 1
        self.last_d0 = d0
        self.last_d1 = d1

        if len(self.bits) == 26:
            raw26 = 0
            for b in self.bits:
                raw26 = (raw26 << 1) | b
            valid, fc, cid = verify_wiegand26(raw26)
            cred = WiegandCredential(
                facility_code=fc,
                card_id=cid,
                raw26=raw26,
                even_parity=(raw26 >> 25) & 1,
                odd_parity=raw26 & 1,
                valid=valid
            )
            return cred

        return None


class WiegandPpaModel:
    """
    Physical PPA Model for synthesizable Wiegand coprocessor macros on IHP 130nm SG13G2.
    """
    GATE_AREA_UM2 = 7.30
    GE_PER_CELL = 1.95

    MACRO_CONFIGS = {
        "1ch_reader_writer": {
            "cells": 285,
            "dffs": 68,
            "delay_ns": 1.22,
            "fmax_mhz": 819.6,
        },
        "2ch_dual_door": {
            "cells": 480,
            "dffs": 124,
            "delay_ns": 1.25,
            "fmax_mhz": 800.0,
        },
        "4ch_pacs_controller": {
            "cells": 890,
            "dffs": 240,
            "delay_ns": 1.28,
            "fmax_mhz": 781.2,
        },
    }

    @classmethod
    def get_ppa(cls, config: str = "1ch_reader_writer") -> dict:
        info = cls.MACRO_CONFIGS.get(config, cls.MACRO_CONFIGS["1ch_reader_writer"])
        cells = info["cells"]
        ge = cells * cls.GE_PER_CELL
        area_um2 = cells * cls.GATE_AREA_UM2
        return {
            "config": config,
            "cells": cells,
            "ge": ge,
            "area_um2": area_um2,
            "fmax_mhz": info["fmax_mhz"],
            "delay_ns": info["delay_ns"],
        }


# ==============================================================================
# Firmware Generators (Returning assembled hex words)
# ==============================================================================

def build_wiegand_writer_asm(
    facility_code: int,
    card_id: int,
    pulse_width_cycles: int = 8,
    pulse_interval_cycles: int = 24,
    d0_pin: int = 3,
    d1_pin: int = 4
) -> List[int]:
    """
    Generates assembled firmware for a 26-bit Wiegand credential transmitter (card emulator).
    Generates active-low pulses on d0_pin (bit 0) or d1_pin (bit 1).
    """
    raw26 = build_wiegand26_raw(facility_code, card_id)
    idle_mask = (1 << d0_pin) | (1 << d1_pin)
    d0_active = (1 << d1_pin)  # d0 is LOW (0), d1 is HIGH (1)
    d1_active = (1 << d0_pin)  # d1 is LOW (0), d0 is HIGH (1)

    low_wait = max(1, pulse_width_cycles - 3)
    high_wait = max(1, pulse_interval_cycles - pulse_width_cycles - 3)

    lines = [
        "; -------------------------------------------------------------",
        f"; 26-bit Wiegand Writer: FC={facility_code}, ID={card_id}, raw=0x{raw26:07X}",
        "; -------------------------------------------------------------",
        f"GDIRI {idle_mask}          ; Set DATA0 and DATA1 as outputs",
        f"GWRI {idle_mask}           ; Initialize both lines HIGH (idle)",
        "WAIT 10                   ; Inter-frame settling delay",
    ]

    for bit_idx in range(25, -1, -1):
        bit_val = (raw26 >> bit_idx) & 1
        lines.append(f"; Bit {bit_idx} = {bit_val}")
        if bit_val == 0:
            lines.append(f"GWRI {d0_active}           ; Pulse DATA0 LOW")
            lines.append(f"WAIT {low_wait}")
            lines.append(f"GWRI {idle_mask}           ; Return DATA0 HIGH")
            lines.append(f"WAIT {high_wait}")
        else:
            lines.append(f"GWRI {d1_active}           ; Pulse DATA1 LOW")
            lines.append(f"WAIT {low_wait}")
            lines.append(f"GWRI {idle_mask}           ; Return DATA1 HIGH")
            lines.append(f"WAIT {high_wait}")

    lines.extend([
        f"LDI R0, {facility_code}     ; Record transmitted Facility Code",
        f"LDI R1, {(card_id >> 8) & 0xFF}  ; Record Card ID High",
        f"LDI R2, {card_id & 0xFF}         ; Record Card ID Low",
        "HALT                      ; Transmission complete",
    ])
    return assemble("\n".join(lines))


def build_wiegand_pulse_discovery_asm(d0_pin: int = 3) -> List[int]:
    """
    Generates assembled firmware for Wiegand physical pulse width & interval discovery via WAITEDGE.
    R0: Idle duration before first pulse (falling edge, mode 0).
    R1: Active-low pulse width (rising edge, mode 1 = 0x08 | d0_pin).
    R2: Pulse interval (falling edge, mode 0 = d0_pin).
    """
    mode0_fall = d0_pin & 0x7
    mode1_rise = 0x08 | (d0_pin & 0x7)

    lines = [
        "; -------------------------------------------------------------",
        f"; Wiegand Pulse Timing Discovery on Pin {d0_pin}",
        "; -------------------------------------------------------------",
        "GDIRI 0x00                ; Configure all pins as inputs",
        f"WAITEDGE R0, {mode0_fall} ; Wait for 1st falling edge",
        f"WAITEDGE R1, {mode1_rise} ; Wait for rising edge (pulse width in R1)",
        f"WAITEDGE R2, {mode0_fall} ; Wait for 2nd falling edge (inter-pulse in R2)",
        "HALT                      ; Timing discovery complete",
    ]
    return assemble("\n".join(lines))


def build_wiegand_tamper_detector_asm(d0_pin: int = 3, d1_pin: int = 4) -> List[int]:
    """
    Generates assembled firmware for Wiegand physical line short / cable tamper detection.
    Normal pulse: records R2 = 0x01.
    Tamper condition (simultaneous low on DATA0 and DATA1): records R2 = 0xAA.
    """
    lines = [
        "; -------------------------------------------------------------",
        "; Wiegand Line Short & Tamper Detection",
        "; -------------------------------------------------------------",
        "GDIRI 0x00                ; Configure pins as inputs",
        "; Wait for bus to settle idle HIGH initially",
        "tamper_init_wait:",
        "GRD R0",
        "ANDI R0, 0x18",
        "MOV R3, R0",
        "XORI R3, 0x18",
        "JNZ tamper_init_wait",
        "poll_loop:",
        "GRD R0                    ; Read GPIO bus",
        "ANDI R0, 0x18             ; Mask pin 3 (DATA0) and pin 4 (DATA1)",
        "JZ tamper_trap            ; Both lines LOW -> Tamper short-circuit detected!",
        "MOV R3, R0",
        "XORI R3, 0x18             ; Both lines HIGH (idle)?",
        "JZ poll_loop              ; Yes -> continue polling",
        "; Single line pulse detected (normal operation):",
        "LDI R2, 1                 ; Status code 0x01 = Normal pulse",
        "HALT",
        "tamper_trap:",
        "LDI R2, 170               ; Status code 0xAA (170) = Tamper alarm",
        "HALT",
    ]
    return assemble("\n".join(lines))


def build_wiegand_reader_byte_asm(d0_pin: int = 3, d1_pin: int = 4) -> List[int]:
    """
    Generates assembled firmware to read 8 bits of Wiegand stream into R0.
    Waits for line to be idle HIGH initially, then unrolled bit loop.
    """
    lines = [
        "; -------------------------------------------------------------",
        "; 8-bit Wiegand Stream Ingress",
        "; -------------------------------------------------------------",
        "GDIRI 0x00                ; Configure pins as inputs",
        "; Wait for bus to settle idle HIGH initially",
        "init_wait:",
        "GRD R2",
        "ANDI R2, 0x18",
        "MOV R3, R2",
        "XORI R3, 0x18",
        "JNZ init_wait",
        "LDI R0, 0                 ; Clear accumulator R0",
    ]

    for bit_idx in range(7, -1, -1):
        bit_mask = 1 << bit_idx
        lines.extend([
            f"poll_bit_{bit_idx}:",
            "GRD R2                    ; Read GPIO state",
            "ANDI R2, 0x18             ; Mask pins 3 and 4",
            "MOV R3, R2",
            "XORI R3, 0x18             ; Both high?",
            f"JZ poll_bit_{bit_idx}     ; Still idling high -> keep polling",
            "; Edge detected!",
            "MOV R3, R2",
            "ANDI R3, 0x10             ; Check pin 4 (DATA1)",
            f"JNZ bit_{bit_idx}_zero   ; If pin 4 is 1 (pin 3 is 0), bit is 0",
            f"ORI R0, {bit_mask}        ; Pin 4 is 0 (DATA1 pulse) -> bit is 1",
            f"bit_{bit_idx}_zero:",
            f"wait_high_{bit_idx}:",
            "GRD R2                    ; Read GPIO state",
            "ANDI R2, 0x18             ; Mask pins 3 and 4",
            "MOV R3, R2",
            "XORI R3, 0x18             ; Returned to high?",
            f"JNZ wait_high_{bit_idx}   ; Wait until lines return high",
        ])

    lines.extend([
        "LDI R2, 0                 ; Status code 0x00 = Clean reception",
        "HALT",
    ])
    return assemble("\n".join(lines))
