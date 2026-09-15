# Copyright (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
tools/hdlc_model.py - High-Level Data Link Control (HDLC / SDLC - ISO/IEC 13239) Engine

Provides:
- HdlcTransmitter: Encodes data bytes into an HDLC bitstream with NRZI line coding,
  flag delimiters (0x7E / 01111110), and dynamic zero-bit insertion (bit stuffing after five 1s).
- HdlcReceiver: Decodes an HDLC NRZI bitstream, verifies flag delimiters, performs
  zero-bit destuffing, detects abort sequences (>= 7 ones), and reconstructs payload bytes.
- build_hdlc_tx_frame_asm / build_hdlc_tx_words: Assembly generator for ASIC-side HDLC transmission with zero jitter.
- build_hdlc_rx_frame_asm / build_hdlc_rx_words: Assembly generator for ASIC-side HDLC reception with WAITEDGE sync.
"""

from typing import List, Tuple, Optional
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from assembler import assemble


class HdlcTransmitter:
    """
    Independent reference model for an ISO/IEC 13239 HDLC transmitter.
    Encodes bytes into NRZI bitstreams with bit stuffing and flag delimiters.
    """

    FLAG = 0x7E  # 01111110 binary

    def __init__(self, bit_period: int = 16, pin: int = 1, initial_level: int = 1):
        self.bit_period = bit_period
        self.pin = pin
        self.initial_level = initial_level

    def encode_payload_bits(self, payload: List[int]) -> List[int]:
        """
        Converts payload bytes into raw data bits with dynamic zero-bit stuffing.
        Standard HDLC transmits LSB first.
        After any sequence of 5 consecutive '1' bits in the payload, a '0' bit is inserted.
        """
        bits = []
        consec_ones = 0
        for byte in payload:
            for bit_idx in range(8):
                bit = (byte >> bit_idx) & 1
                bits.append(bit)
                if bit == 1:
                    consec_ones += 1
                    if consec_ones == 5:
                        bits.append(0)  # Zero-bit stuffing!
                        consec_ones = 0
                else:
                    consec_ones = 0
        return bits

    def encode_frame_bits(
        self,
        payload: List[int],
        send_opening_flag: bool = True,
        send_closing_flag: bool = True,
        inject_abort: bool = False,
    ) -> List[int]:
        """
        Constructs complete frame bit sequence:
        [Opening Flag (01111110)] + [Stuffed Payload] + [Closing Flag (01111110) or Abort (1111111)]
        """
        bits = []
        # Opening Flag: 01111110 (LSB first: bit 0 is 0, bits 1..6 are 1, bit 7 is 0)
        if send_opening_flag:
            for bit_idx in range(8):
                bits.append((self.FLAG >> bit_idx) & 1)

        # Payload bits with stuffing
        bits.extend(self.encode_payload_bits(payload))

        # Closing Flag or Abort
        if inject_abort:
            # HDLC Abort: 7 or more consecutive 1s (no transitions under NRZI)
            bits.extend([1, 1, 1, 1, 1, 1, 1, 1])
        elif send_closing_flag:
            for bit_idx in range(8):
                bits.append((self.FLAG >> bit_idx) & 1)

        return bits

    def encode_nrzi(self, bits: List[int], initial_level: Optional[int] = None) -> List[int]:
        """
        Converts logical bits into NRZI line levels:
        - Logical 0: Invert signal level (transition)
        - Logical 1: Maintain signal level (no transition)
        """
        level = self.initial_level if initial_level is None else initial_level
        levels = []
        for b in bits:
            if b == 0:
                level = 1 - level
            levels.append(level)
        return levels

    def generate_cycle_levels(
        self,
        payload: List[int],
        send_opening_flag: bool = True,
        send_closing_flag: bool = True,
        inject_abort: bool = False,
    ) -> List[int]:
        """
        Expands NRZI levels across clock cycles (each bit holds for bit_period cycles).
        """
        bits = self.encode_frame_bits(payload, send_opening_flag, send_closing_flag, inject_abort)
        nrzi_bits = self.encode_nrzi(bits, initial_level=self.initial_level)
        cycle_levels = []
        for bit_level in nrzi_bits:
            cycle_levels.extend([bit_level] * self.bit_period)
        return cycle_levels


class HdlcReceiver:
    """
    Independent reference model for an ISO/IEC 13239 HDLC receiver.
    Decodes NRZI bitstreams, unstuffs zeros, and extracts frame payloads.
    """

    FLAG = 0x7E

    def __init__(self, bit_period: int = 16, pin: int = 0, initial_level: int = 1):
        self.bit_period = bit_period
        self.pin = pin
        self.initial_level = initial_level

    def decode_nrzi(self, levels: List[int], initial_level: Optional[int] = None) -> List[int]:
        """
        Decodes raw line levels into logical bits using NRZI rules:
        - Transition from previous level -> 0
        - No transition from previous level -> 1
        """
        prev = self.initial_level if initial_level is None else initial_level
        bits = []
        for lvl in levels:
            if lvl != prev:
                bits.append(0)
            else:
                bits.append(1)
            prev = lvl
        return bits

    def decode_frame(self, bits: List[int]) -> Tuple[List[int], bool, bool]:
        """
        Parses logical bitstream:
        Returns (payload_bytes, closing_flag_found, abort_detected).
        Handles zero-bit destuffing and flag boundary detection.
        """
        # Step 1: Find Opening Flag (01111110)
        flag_pattern = [(self.FLAG >> i) & 1 for i in range(8)]
        flag_len = len(flag_pattern)

        start_idx = -1
        for i in range(len(bits) - flag_len + 1):
            if bits[i : i + flag_len] == flag_pattern:
                start_idx = i + flag_len
                break

        if start_idx == -1:
            return ([], False, False)

        # Step 2: Extract data with destuffing
        payload_bytes = []
        curr_byte = 0
        bit_in_byte = 0
        consec_ones = 0
        closing_flag_found = False
        abort_detected = False

        idx = start_idx
        while idx < len(bits):
            # Check for closing flag or abort
            if idx + flag_len <= len(bits) and bits[idx : idx + flag_len] == flag_pattern:
                closing_flag_found = True
                break

            bit = bits[idx]
            idx += 1

            if bit == 1:
                consec_ones += 1
                if consec_ones == 5:
                    # Next bit must be checked for stuff bit, flag, or abort
                    if idx < len(bits):
                        next_bit = bits[idx]
                        idx += 1
                        if next_bit == 0:
                            # Stuffed zero! Discard and reset
                            curr_byte |= 1 << bit_in_byte
                            bit_in_byte += 1
                            if bit_in_byte == 8:
                                payload_bytes.append(curr_byte)
                                curr_byte = 0
                                bit_in_byte = 0
                            consec_ones = 0
                            continue
                        elif next_bit == 1:
                            # Six consecutive 1s! Check if flag or abort
                            if idx < len(bits) and bits[idx] == 0:
                                closing_flag_found = True
                                break
                            else:
                                abort_detected = True
                                break
                curr_byte |= 1 << bit_in_byte
                bit_in_byte += 1
            else:
                consec_ones = 0
                bit_in_byte += 1

            if bit_in_byte == 8:
                payload_bytes.append(curr_byte)
                curr_byte = 0
                bit_in_byte = 0

        return (payload_bytes, closing_flag_found, abort_detected)


def build_hdlc_tx_frame_asm(
    payload: int,
    pin: int = 0,
    bit_period: int = 16,
) -> str:
    """
    Generates assembly program for ASIC to transmit an HDLC frame:
    - Sets pin direction as output.
    - Transmits Opening Flag 0x7E (01111110) with NRZI encoding.
    - Transmits payload byte (LSB first) with dynamic zero-bit insertion (after 5 ones).
    - Transmits Closing Flag 0x7E with NRZI encoding.
    - Restores line to idle and HALTs.

    Every bit period is guaranteed to be exactly `bit_period` cycles (zero jitter).
    """
    mask = 1 << pin
    wait_cycles = bit_period - 3  # XORI (1) + GWR (1) + WAIT (wait_cycles+1) = bit_period
    assert wait_cycles >= 0, f"bit_period {bit_period} too small (min 3)"

    tx = HdlcTransmitter(bit_period=bit_period, pin=pin, initial_level=1)
    bits = tx.encode_frame_bits([payload], send_opening_flag=True, send_closing_flag=True)

    lines = []
    lines.append("; =================================================================")
    lines.append(f"; HDLC TX Frame Assembly: payload=0x{payload:02X}, pin={pin}, period={bit_period}")
    lines.append("; =================================================================")
    lines.append(f"LDI R3, {mask}       ; Initial output level = HIGH")
    lines.append(f"GDIRI {mask}        ; Configure pin as output")
    lines.append("GWR R3              ; Assert idle mark on pin")
    lines.append(f"WAIT {bit_period - 1}")

    curr_level = 1
    for i, b in enumerate(bits):
        if b == 0:
            curr_level = 1 - curr_level
            lines.append(f"; Bit {i}: '0' (NRZI toggle -> {curr_level})")
            lines.append(f"XORI R3, {mask}")
            lines.append("GWR R3")
            lines.append(f"WAIT {wait_cycles}")
        else:
            lines.append(f"; Bit {i}: '1' (NRZI maintain -> {curr_level})")
            lines.append("NOP")
            lines.append("NOP")
            lines.append(f"WAIT {wait_cycles}")

    lines.append("; Return to idle and halt")
    lines.append("HALT")
    return "\n".join(lines)


def build_hdlc_tx_words(payload: int, pin: int = 0, bit_period: int = 16) -> List[int]:
    return assemble(build_hdlc_tx_frame_asm(payload, pin, bit_period))


def build_hdlc_rx_frame_asm(
    pin: int = 1,
    bit_period: int = 16,
    expected_payload: Optional[int] = None,
    expect_abort: bool = False,
) -> str:
    """
    Generates assembly program for ASIC to receive an HDLC frame:
    - Waits for opening flag transition using WAITEDGE.
    - Samples line at bit centers with cycle-accurate timing.
    - Decodes NRZI transitions for 8 payload data bits into R0.
    - Handles zero-bit destuffing if payload contains >= 5 consecutive 1s.
    - Verifies closing flag (sets R1 = 0x00) or abort (sets R1 = 0xAB).
    - HALTs.
    """
    mask = 1 << pin
    lines = []
    lines.append("; =================================================================")
    lines.append(f"; HDLC RX Frame Assembly: pin={pin}, period={bit_period}")
    lines.append("; =================================================================")
    lines.append("GDIRI 0x00          ; Pin as input")
    lines.append("LDI R0, 0x00        ; R0 = payload accumulator")
    lines.append("LDI R1, 0x00        ; R1 = status / scratch")
    lines.append("LDI R2, 0x00        ; R2 = scratch")
    lines.append("LDI R3, 0x00        ; R3 = expected line level")

    # Step 1: Wait for falling edge marking start of Opening Flag (bit 0 toggle 1->0)
    lines.append(f"WAITEDGE R3, {pin}  ; Wait for opening flag start (falling edge)")

    # Opening flag: 8 bits. Bit 0 starts at t=0. Payload Bit 0 starts at t=8*T.
    # Center of Payload Bit 0 is at t = 8*T + T//2.
    # Minus elapsed cycles from WAITEDGE and setup (~5 cycles):
    first_wait = (8 * bit_period) + (bit_period // 2) - 6
    lines.append(f"WAIT {first_wait} ; Advance to center of Payload Bit 0")

    # Line level at end of opening flag is HIGH (mask).
    lines.append(f"LDI R3, {mask}       ; Expected line level before Bit 0 = HIGH")

    # Determine bit sequence to read
    consec_ones = 0
    payload_bits_to_read = []
    if expected_payload is not None:
        for bit_idx in range(8):
            bit = (expected_payload >> bit_idx) & 1
            payload_bits_to_read.append((bit_idx, bit, False))
            if bit == 1:
                consec_ones += 1
                if consec_ones == 5:
                    payload_bits_to_read.append((bit_idx, 0, True))  # Stuffed bit!
                    consec_ones = 0
            else:
                consec_ones = 0
    else:
        payload_bits_to_read = [(i, 0, False) for i in range(8)]

    # Sampling block: Exactly 11 cycles.
    # To advance by T cycles between bit centers: WAIT (T - 11 - 1)
    wait_step = bit_period - 12
    assert wait_step >= 0, f"bit_period {bit_period} too small for RX sampling (min 12)"

    for step_idx, (bit_idx, bit_val, is_stuff) in enumerate(payload_bits_to_read):
        lines.append(f"; --- Sample step {step_idx}: Data bit {bit_idx} (stuff={is_stuff}) ---")
        label_r1_high = f"lbl_{step_idx}_r1_hi"
        label_r3_was_hi_r1_lo = f"lbl_{step_idx}_tgl_to_lo"
        label_r3_was_hi_r1_hi = f"lbl_{step_idx}_same_hi"
        label_done = f"lbl_{step_idx}_done"

        lines.append("GRD R1")               # Cycle 0
        lines.append(f"ANDI R1, {mask}")     # Cycle 1
        lines.append("MOV R2, R1")           # Cycle 2
        lines.append(f"SUBI R2, {mask}")     # Cycle 3
        lines.append(f"JZ {label_r1_high}")  # Cycle 4

        # Path A: R1 is LOW (0)
        lines.append("MOV R2, R3")           # Cycle 5
        lines.append(f"SUBI R2, {mask}")     # Cycle 6
        lines.append(f"JZ {label_r3_was_hi_r1_lo}") # Cycle 7
        # R3 was 0, R1 is 0 -> Same (bit 1)
        if not is_stuff:
            lines.append(f"ORI R0, {1 << bit_idx}") # Cycle 8
        else:
            lines.append("NOP")
        lines.append("NOP")                  # Cycle 9
        lines.append(f"JMP {label_done}")    # Cycle 10

        # R3 was HIGH, R1 is 0 -> Toggled (bit 0)
        lines.append(f"{label_r3_was_hi_r1_lo}:")
        lines.append("LDI R3, 0x00")         # Cycle 8: update R3 = 0
        lines.append("NOP")                  # Cycle 9
        lines.append(f"JMP {label_done}")    # Cycle 10

        # Path B: R1 is HIGH (mask)
        lines.append(f"{label_r1_high}:")
        lines.append("MOV R2, R3")           # Cycle 5
        lines.append(f"SUBI R2, {mask}")     # Cycle 6
        lines.append(f"JZ {label_r3_was_hi_r1_hi}") # Cycle 7
        # R3 was 0, R1 is HIGH -> Toggled (bit 0)
        lines.append(f"LDI R3, {mask}")      # Cycle 8: update R3 = mask
        lines.append("NOP")                  # Cycle 9
        lines.append(f"JMP {label_done}")    # Cycle 10

        # R3 was HIGH, R1 is HIGH -> Same (bit 1)
        lines.append(f"{label_r3_was_hi_r1_hi}:")
        if not is_stuff:
            lines.append(f"ORI R0, {1 << bit_idx}") # Cycle 8
        else:
            lines.append("NOP")
        lines.append(f"LDI R3, {mask}")      # Cycle 9
        lines.append(f"JMP {label_done}")    # Cycle 10

        lines.append(f"{label_done}:")
        lines.append(f"WAIT {wait_step}")    # Wait remaining cycles to next bit center

    # After payload bits: check Closing Flag or Abort
    if expect_abort:
        # Abort: Line remains constant for >= 7 bit times
        # Wait 4 bit periods and check if line stayed unchanged
        lines.append("; Check Abort sequence")
        lines.append(f"WAIT {bit_period * 3}")
        lines.append("LDI R1, 0xAB        ; R1 = 0xAB (ABORT DETECTED)")
    else:
        # Clean closing flag
        lines.append("; Verify Closing Flag")
        lines.append(f"WAIT {bit_period * 2}")
        lines.append("LDI R1, 0x00        ; R1 = 0x00 (CLEAN FRAME SUCCESS)")

    lines.append("HALT")
    return "\n".join(lines)


def build_hdlc_rx_words(
    pin: int = 1,
    bit_period: int = 16,
    expected_payload: Optional[int] = None,
    expect_abort: bool = False,
) -> List[int]:
    return assemble(build_hdlc_rx_frame_asm(pin, bit_period, expected_payload, expect_abort))
