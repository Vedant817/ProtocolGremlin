#!/usr/bin/env python3
"""Autobaud Rate Auto-Discovery Engine Model and Firmware Generator.

Demonstrates the core's unique capability to measure unknown external signal
pulse durations with single-cycle precision using WAITEDGE, validate pulse
symmetry to reject noise glitches, classify the discovered baud rate into
calibrated profiles, and dynamically decode subsequent data frames at that
auto-discovered rate with zero bit jitter.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from assembler import assemble


def build_autobaud_rx_asm(pin: int = 3) -> list[int]:
    """Generate firmware that measures sync pulse duration, classifies baud rate,
    and receives the subsequent data byte at the discovered baud rate.

    Pin: GPIO pin on uio bus (default pin 3).

    Register ABI:
      R0: Decoded data byte payload on success
      R1: Discovered baud profile ID:
          0x01: Rate 8  cycles/bit (e.g. 1.25 Mbps at 10 MHz)
          0x02: Rate 16 cycles/bit (e.g. 625 kbps at 10 MHz)
          0x03: Rate 32 cycles/bit (e.g. 312.5 kbps at 10 MHz)
      R2: Status code:
          0x00: SUCCESS
          0xEE: NOISE / SYMMETRY ERROR (T0 != T1)
          0xBF: BAUD FAULT (unsupported baud rate)
          0xFE: FRAMING ERROR (stop bit was 0)
      R3: Scratch / measured period T1
    """
    pin_fall = 0x00 | (pin & 0x07)
    pin_rise = 0x08 | (pin & 0x07)

    asm = f"""
        ; --- Initialization ---
        GDIRI 0x00               ; All pins input
        LDI R0, 0x00
        LDI R1, 0x00
        LDI R2, 0x00
        LDI R3, 0x00

        ; --- Step 1: Measure Start Bit (falling -> rising edge) ---
        WAITEDGE R0, {pin_fall}    ; Wait for falling edge of sync start bit
        WAITEDGE R1, {pin_rise}    ; Wait for rising edge of start bit -> R1 = T0

        ; --- Step 2: Measure Bit 1 (falling -> rising edge) ---
        WAITEDGE R2, {pin_fall}    ; Wait for falling edge of Bit 1
        WAITEDGE R3, {pin_rise}    ; Wait for rising edge of Bit 1 -> R3 = T1

        ; --- Step 3: Symmetry & Noise Check (T0 == T1) ---
        MOV R2, R1
        SUBI R2, 0
        XORI R2, 0               ; Refresh flags
        MOV R2, R1
        SUBI R2, 0
        ; Compare R1 and R3
        MOV R2, R1
        XORI R2, 0               ; R2 = R1
        ; Since ALU doesn't have direct CMP reg-reg, compute R2 = R1 - R3:
        ; Wait: SUBI is immediate. To subtract R3 from R2, we use MOV and loop or check known rates.
        ; Let's check rate classification for R1 first!
    """

    # Better approach for ISA v1:
    # Check if R1 == 8, 16, or 32:
    # If R1 == 8: check if R3 == 8. If match, rate is confirmed!
    # If R1 == 16: check if R3 == 16. If match, rate is confirmed!
    # If R1 == 32: check if R3 == 32. If match, rate is confirmed!
    # If neither matches or R1 != R3, jump to error handler!

    asm_code = f"""
        ; --- Step 1: Initialize ---
        GDIRI 0x00               ; input mode
        LDI R0, 0x00
        LDI R1, 0x00
        LDI R2, 0x00
        LDI R3, 0x00

        ; --- Step 2: Measure Sync Bit Durations ---
        WAITEDGE R0, {pin_fall}    ; wait for start bit falling edge
        WAITEDGE R1, {pin_rise}    ; measure start bit low duration -> R1 = T0
        WAITEDGE R2, {pin_fall}    ; wait for bit 1 falling edge
        WAITEDGE R3, {pin_rise}    ; measure bit 1 low duration -> R3 = T1

        ; --- Step 3: Rate 8 Classification & Symmetry Check ---
        MOV R2, R1
        SUBI R2, 8
        JZ test_sym_8
        MOV R2, R1
        SUBI R2, 16
        JZ test_sym_16
        MOV R2, R1
        SUBI R2, 32
        JZ test_sym_32
        JMP err_baud

    test_sym_8:
        MOV R2, R3
        SUBI R2, 8
        JZ match_rate_8
        JMP err_noise

    test_sym_16:
        MOV R2, R3
        SUBI R2, 16
        JZ match_rate_16
        JMP err_noise

    test_sym_32:
        MOV R2, R3
        SUBI R2, 32
        JZ match_rate_32
        JMP err_noise

    ; =========================================================================
    ; Rate 8 Handler (T = 8 cycles/bit)
    ; =========================================================================
    match_rate_8:
        ; Consume remaining edges of sync byte (0x55):
        ; Currently at rising edge of bit 1 (bit 2 is high).
        ; Remaining low pulses: Bit 3, Bit 5, Bit 7, then Stop bit goes high.
        WAITEDGE R2, {pin_fall}    ; bit 3 low
        WAITEDGE R2, {pin_fall}    ; bit 5 low
        WAITEDGE R2, {pin_fall}    ; bit 7 low
        WAITEDGE R2, {pin_rise}    ; stop bit high

        ; Wait for start bit of data byte
        WAITEDGE R2, {pin_fall}

        ; Sample mid-bit: 1.5 * 8 = 12 cycles from edge.
        ; WAIT 10 takes 11 cycles + SHIFTIN 1 cycle = 12 cycles.
        WAIT 10
        SHIFTIN R0, {pin}, LSB
        WAIT 6
        SHIFTIN R0, {pin}, LSB
        WAIT 6
        SHIFTIN R0, {pin}, LSB
        WAIT 6
        SHIFTIN R0, {pin}, LSB
        WAIT 6
        SHIFTIN R0, {pin}, LSB
        WAIT 6
        SHIFTIN R0, {pin}, LSB
        WAIT 6
        SHIFTIN R0, {pin}, LSB
        WAIT 6
        SHIFTIN R0, {pin}, LSB

        ; Check stop bit (high)
        WAIT 4
        GRD R2
        ANDI R2, {1 << pin}
        JZ err_framing

        LDI R1, 0x01             ; Profile ID = 1 (Rate 8)
        LDI R2, 0x00             ; Status = SUCCESS
        HALT

    ; =========================================================================
    ; Rate 16 Handler (T = 16 cycles/bit)
    ; =========================================================================
    match_rate_16:
        WAITEDGE R2, {pin_fall}    ; bit 3 low
        WAITEDGE R2, {pin_fall}    ; bit 5 low
        WAITEDGE R2, {pin_fall}    ; bit 7 low
        WAITEDGE R2, {pin_rise}    ; stop bit high

        ; Wait for start bit of data byte
        WAITEDGE R2, {pin_fall}

        ; Sample mid-bit: 1.5 * 16 = 24 cycles from edge.
        ; WAIT 22 takes 23 cycles + SHIFTIN 1 cycle = 24 cycles.
        WAIT 22
        SHIFTIN R0, {pin}, LSB
        WAIT 14
        SHIFTIN R0, {pin}, LSB
        WAIT 14
        SHIFTIN R0, {pin}, LSB
        WAIT 14
        SHIFTIN R0, {pin}, LSB
        WAIT 14
        SHIFTIN R0, {pin}, LSB
        WAIT 14
        SHIFTIN R0, {pin}, LSB
        WAIT 14
        SHIFTIN R0, {pin}, LSB
        WAIT 14
        SHIFTIN R0, {pin}, LSB

        ; Check stop bit
        WAIT 10
        GRD R2
        ANDI R2, {1 << pin}
        JZ err_framing

        LDI R1, 0x02             ; Profile ID = 2 (Rate 16)
        LDI R2, 0x00             ; Status = SUCCESS
        HALT

    ; =========================================================================
    ; Rate 32 Handler (T = 32 cycles/bit)
    ; =========================================================================
    match_rate_32:
        WAITEDGE R2, {pin_fall}    ; bit 3 low
        WAITEDGE R2, {pin_fall}    ; bit 5 low
        WAITEDGE R2, {pin_fall}    ; bit 7 low
        WAITEDGE R2, {pin_rise}    ; stop bit high

        ; Wait for start bit of data byte
        WAITEDGE R2, {pin_fall}

        ; Sample mid-bit: 1.5 * 32 = 48 cycles from edge.
        ; WAIT 46 takes 47 cycles + SHIFTIN 1 cycle = 48 cycles.
        WAIT 46
        SHIFTIN R0, {pin}, LSB
        WAIT 30
        SHIFTIN R0, {pin}, LSB
        WAIT 30
        SHIFTIN R0, {pin}, LSB
        WAIT 30
        SHIFTIN R0, {pin}, LSB
        WAIT 30
        SHIFTIN R0, {pin}, LSB
        WAIT 30
        SHIFTIN R0, {pin}, LSB
        WAIT 30
        SHIFTIN R0, {pin}, LSB
        WAIT 30
        SHIFTIN R0, {pin}, LSB

        ; Check stop bit
        WAIT 20
        GRD R2
        ANDI R2, {1 << pin}
        JZ err_framing

        LDI R1, 0x03             ; Profile ID = 3 (Rate 32)
        LDI R2, 0x00             ; Status = SUCCESS
        HALT

    ; =========================================================================
    ; Error Handlers
    ; =========================================================================
    err_noise:
        LDI R2, 0xEE             ; Status = NOISE / ASYMMETRY ERROR
        HALT

    err_baud:
        LDI R2, 0xBF             ; Status = BAUD FAULT (unsupported rate)
        HALT

    err_framing:
        LDI R2, 0xFE             ; Status = FRAMING ERROR
        HALT
    """
    return assemble(asm_code)


class AutobaudTransmitterModel:
    """Simulation model for driving autobaud training and data byte sequences."""

    def __init__(self, pin: int = 3):
        self.pin = pin

    def generate_sync_and_data(
        self,
        bit_period: int,
        data_byte: int,
        corrupt_symmetry: bool = False,
        framing_error: bool = False,
        interbyte_stop_cycles: int | None = None,
    ) -> list[int]:
        """Generate cycle-by-cycle bit levels for an Autobaud session.
        Returns a list of 0s and 1s representing pin levels per clock cycle.
        """
        if interbyte_stop_cycles is None:
            interbyte_stop_cycles = bit_period * 2

        levels = []

        # 1. Idle High before sync
        levels.extend([1] * 16)

        # 2. Sync Byte: 0x55 (0b01010101) in 8-N-1 UART
        # Start bit: 0 (keep nominal so rate classification succeeds)
        levels.extend([0] * bit_period)

        # 8 Data bits for 0x55: alternating 1, 0, 1, 0, 1, 0, 1, 0
        # If corrupt_symmetry is True, distort Bit 1 (low pulse) to trigger asymmetry detection
        for bit_idx in range(8):
            bit_val = (0x55 >> bit_idx) & 1
            t_bit = (bit_period + 4) if (corrupt_symmetry and bit_idx == 1) else bit_period
            levels.extend([bit_val] * t_bit)

        # Stop bit(s) for sync byte
        levels.extend([1] * interbyte_stop_cycles)

        # 3. Data Byte
        # Start bit: 0
        levels.extend([0] * bit_period)

        # 8 Data bits (LSB first)
        for bit_idx in range(8):
            bit_val = (data_byte >> bit_idx) & 1
            levels.extend([bit_val] * bit_period)

        # Stop bit: 1 (or 0 if intentional framing error)
        stop_val = 0 if framing_error else 1
        levels.extend([stop_val] * (bit_period * 2))

        # Trailing idle high
        levels.extend([1] * 16)

        return levels


if __name__ == "__main__":
    words = build_autobaud_rx_asm(pin=3)
    print(f"Autobaud RX firmware assembled: {len(words)} instruction words (max 256)")
    for i, w in enumerate(words):
        if i < 15:
            print(f"  [{i:02d}] 0x{w:04X}")
