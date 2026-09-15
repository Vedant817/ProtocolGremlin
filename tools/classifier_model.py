# Copyright (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
tools/classifier_model.py - Autonomous Hardware Protocol Sniffer & Dynamic Pattern Classifier Engine

Leverages the core's WAITEDGE single-cycle pulse measurement primitive and 32-bit cycle
counter to passively sniff unknown communication lines, measure pulse widths and duty cycles,
and autonomously classify the active protocol on the wire.

Protocol Classifications:
  0x01: UART (Asynchronous serial: start bit low ~16 cycles)
  0x02: Manchester Biphase-L (IEEE 802.3 / MIL-STD-1553: symmetric half-bits ~4 cycles)
  0x03: Dallas 1-Wire (Long reset pulse >= 80 cycles + presence delay >= 35 cycles)
  0x04: DMX512 (ANSI E1.11: Break pulse >= 80 cycles + short MAB <= 24 cycles)
  0x05: HDLC / SDLC (ISO/IEC 13239: NRZI line coding with 56-cycle flag hold at T=8)
  0xFF: Unrecognized / Noise
"""

import os
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(__file__))
from assembler import assemble

# Protocol IDs
PROTO_UART       = 0x01
PROTO_MANCHESTER = 0x02
PROTO_ONEWIRE    = 0x03
PROTO_DMX512     = 0x04
PROTO_HDLC       = 0x05
PROTO_UNKNOWN    = 0xFF


def build_protocol_sniffer_asm(pin: int = 2) -> list[int]:
    """
    Generates assembly for the autonomous protocol sniffer & classifier engine.
    Listens on `pin`, measures pulse durations via WAITEDGE, and classifies the protocol into R0.

    Register ABI:
      R0: Classified Protocol ID (0x01..0x05, or 0xFF unknown)
      R1: Measured first low pulse duration T_low
      R2: Measured high pulse duration T_high
      R3: Scratch / metric register
    """
    pin_fall = 0x00 | (pin & 0x07)
    pin_rise = 0x08 | (pin & 0x07)

    asm = f"""
    ; =================================================================
    ; Autonomous Protocol Sniffer & Classifier Firmware
    ; Pin: {pin}
    ; =================================================================
    GDIRI 0x00               ; Configure all pins as inputs
    LDI R0, 0xFF             ; Default: PROTO_UNKNOWN
    LDI R1, 0x00
    LDI R2, 0x00
    LDI R3, 0x00

    ; --- Step 1: Wait for first falling edge and measure low pulse ---
    WAITEDGE R1, {pin_fall}   ; Stall until falling edge
    WAITEDGE R1, {pin_rise}   ; Stall until rising edge -> R1 = T_low (cycles)

    ; --- Step 2: Measure subsequent high pulse ---
    WAITEDGE R2, {pin_fall}   ; Stall until next falling edge -> R2 = T_high (cycles)

    ; --- Step 3: Decision Tree Classification ---
    ; Check if R1 < 7 (Manchester candidate: T1 ~ 4)
    MOV R3, R1
    SUBI R3, 7
    ANDI R3, 0x80
    JNZ check_manchester

    ; Check if R1 < 30 (UART candidate: T1 ~ 16)
    MOV R3, R1
    SUBI R3, 30
    ANDI R3, 0x80
    JNZ check_uart

    ; Check if R1 < 68 (HDLC candidate at T=8: T1 ~ 56)
    MOV R3, R1
    SUBI R3, 68
    ANDI R3, 0x80
    JNZ check_hdlc

    ; Check if R1 >= 70 (Long pulse: DMX512 or 1-Wire)
    MOV R3, R1
    SUBI R3, 70
    ANDI R3, 0x80
    JZ check_long_pulse

    ; Otherwise: Unknown / Noise
    JMP classify_done

check_manchester:
    ; Check if R2 <= 8
    MOV R3, R2
    SUBI R3, 9
    ANDI R3, 0x80
    JZ classify_done          ; R2 > 8 -> noise
    LDI R0, {PROTO_MANCHESTER}
    JMP classify_done

check_uart:
    ; Check if R1 >= 10 (reject noise < 10)
    MOV R3, R1
    SUBI R3, 10
    ANDI R3, 0x80
    JNZ classify_done         ; R1 < 10 -> noise
    LDI R0, {PROTO_UART}
    JMP classify_done

check_hdlc:
    ; Check if R1 >= 45 (HDLC flag 56 cycles)
    MOV R3, R1
    SUBI R3, 45
    ANDI R3, 0x80
    JNZ classify_done         ; R1 < 45 -> noise
    LDI R0, {PROTO_HDLC}
    JMP classify_done

check_long_pulse:
    ; Distinguish DMX512 vs 1-Wire based on T_high (R2):
    ; DMX512 MAB is short: T_high <= 24 cycles (e.g. 16)
    ; 1-Wire recovery time is longer: T_high >= 32 cycles (e.g. 40)
    MOV R3, R2
    SUBI R3, 26
    ANDI R3, 0x80
    JNZ is_dmx

    ; Check if R2 >= 30 (1-Wire)
    MOV R3, R2
    SUBI R3, 30
    ANDI R3, 0x80
    JZ is_onewire
    JMP classify_done

is_dmx:
    LDI R0, {PROTO_DMX512}
    JMP classify_done

is_onewire:
    LDI R0, {PROTO_ONEWIRE}
    JMP classify_done

classify_done:
    HALT
    """
    return assemble(asm)
