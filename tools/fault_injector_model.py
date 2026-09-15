# Copyright (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
tools/fault_injector_model.py - Deterministic Fault Injection & Protocol Stress Engine

Provides cycle-accurate firmware generators and test models to intentionally inject
protocol-level violations across CAN, HDLC, UART, and Manchester buses:
1. CAN 2.0A Faults:
   - bit stuff violation (6 consecutive identical bits without complementary stuff bit)
   - CRC-15 checksum corruption
   - End-of-Frame (EOF) dominant bit violation
2. HDLC / SDLC Faults:
   - premature abort sequence (7 consecutive 1s inside active frame)
   - zero-bit stuff omission on payload with >= 6 consecutive 1s
   - corrupted closing delimiter
3. UART Faults:
   - framing error (stop bit forced LOW)
   - sub-baud noise glitch (narrow 1-cycle runt pulse on idle line)
   - break condition (continuous LOW for > 12 bit periods)
4. Manchester Biphase-L Faults:
   - biphase violation (omission of mid-bit transition)
"""

from typing import List, Tuple, Optional
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from assembler import assemble
from can_model import compute_can_crc15, insert_can_bit_stuffing
from hdlc_model import HdlcTransmitter


def build_can_fault_tx_asm(
    can_id: int,
    data_byte: int,
    fault_type: str,
    bit_period: int = 8,
    pin: int = 4,
) -> str:
    """
    Generate assembly firmware to transmit a CAN frame with deliberate protocol faults.

    Supported fault_type values:
    - 'stuff_error': Transmits payload data without inserting complementary stuff bits,
      resulting in >= 6 consecutive identical bits.
    - 'crc_error': Inverts bits in the 15-bit CRC field before transmission.
    - 'eof_error': Drives Dominant (0) on the 3rd bit of the 7-bit Recessive End of Frame.
    """
    pin_mask = 1 << pin
    normal_wait = max(0, bit_period - 2)

    raw_bits = []
    # SOF
    raw_bits.append(0)
    # 11-bit Identifier
    for b in range(10, -1, -1):
        raw_bits.append((can_id >> b) & 1)
    # RTR, IDE, r0
    raw_bits.extend([0, 0, 0])
    # DLC = 1
    raw_bits.extend([0, 0, 0, 1])
    # Data byte
    for b in range(7, -1, -1):
        raw_bits.append((data_byte >> b) & 1)

    # Compute CRC-15
    crc15 = compute_can_crc15(raw_bits)

    if fault_type == "crc_error":
        # Corrupt CRC-15 by inverting bits
        crc15 ^= 0x5555

    for b in range(14, -1, -1):
        raw_bits.append((crc15 >> b) & 1)

    if fault_type == "stuff_error":
        # Do NOT apply bit stuffing: raw bits directly violate the 5-consecutive rule
        # e.g., for data_byte=0x00, DLC=0001 followed by 8 zeros produces 8 consecutive 0s!
        tx_bits = list(raw_bits)
    else:
        # Standard bit stuffing
        tx_bits = insert_can_bit_stuffing(raw_bits)

    asm = []
    asm.append(f"; --- CAN Fault Injector: {fault_type} ---")
    asm.append(f"GDIRI 0x{pin_mask:02X}")
    asm.append(f"GODRI 0x{pin_mask:02X}")
    asm.append(f"GWRI 0x{pin_mask:02X}")  # Recessive idle
    asm.append("WAIT 4")

    # Transmit pre-ACK bits
    for bit in tx_bits:
        if bit == 0:
            asm.append("GWRI 0x00")
        else:
            asm.append(f"GWRI 0x{pin_mask:02X}")
        if normal_wait > 0:
            asm.append(f"WAIT {normal_wait}")

    # CRC Delimiter (recessive)
    asm.append(f"GWRI 0x{pin_mask:02X}")
    if normal_wait > 0:
        asm.append(f"WAIT {normal_wait}")

    # ACK Slot (recessive from TX)
    asm.append(f"GWRI 0x{pin_mask:02X}")
    if normal_wait > 0:
        asm.append(f"WAIT {normal_wait}")

    # ACK Delimiter (recessive)
    asm.append(f"GWRI 0x{pin_mask:02X}")
    if normal_wait > 0:
        asm.append(f"WAIT {normal_wait}")

    # End of Frame: 7 bits recessive (unless eof_error fault)
    for eof_idx in range(7):
        if fault_type == "eof_error" and eof_idx == 2:
            # Inject dominant bit during EOF
            asm.append("GWRI 0x00")
        else:
            asm.append(f"GWRI 0x{pin_mask:02X}")
        if normal_wait > 0:
            asm.append(f"WAIT {normal_wait}")

    # Return bus to recessive and halt
    asm.append(f"GWRI 0x{pin_mask:02X}")
    asm.append("WAIT 4")
    asm.append("HALT")

    return "\n".join(asm)


def build_hdlc_fault_tx_asm(
    payload: List[int],
    fault_type: str,
    bit_period: int = 16,
    pin: int = 1,
) -> str:
    """
    Generate assembly firmware to transmit HDLC frames with deliberate protocol faults.

    Supported fault_type values:
    - 'abort_sequence': Injects 7 consecutive 1s (0b01111111) inside active frame.
    - 'stuff_omission': Sends payload with >= 5 consecutive 1s without zero-bit stuffing.
    - 'corrupted_flag': Ends frame with corrupted closing delimiter (e.g. 0x7B instead of 0x7E).
    """
    pin_mask = 1 << pin
    normal_wait = max(0, bit_period - 2)
    FLAG = 0x7E

    # Construct logical bit sequence based on fault
    bits = []
    # Opening Flag
    for b in range(8):
        bits.append((FLAG >> b) & 1)

    if fault_type == "abort_sequence":
        # Send first 4 bits of payload
        for b in range(4):
            bits.append((payload[0] >> b) & 1)
        # Abort sequence: 7 consecutive 1s
        bits.extend([1] * 7)
    elif fault_type == "stuff_omission":
        # Send payload bytes with NO zero-bit stuffing
        for byte_val in payload:
            for b in range(8):
                bits.append((byte_val >> b) & 1)
        # Closing flag
        for b in range(8):
            bits.append((FLAG >> b) & 1)
    elif fault_type == "corrupted_flag":
        # Send stuffed payload normally
        tx = HdlcTransmitter(bit_period=bit_period, pin=pin)
        stuffed_payload = tx.encode_payload_bits(payload)
        bits.extend(stuffed_payload)
        # Corrupted closing flag: 0x7B (01111011)
        bad_flag = 0x7B
        for b in range(8):
            bits.append((bad_flag >> b) & 1)
    else:
        raise ValueError(f"Unknown HDLC fault_type: {fault_type}")

    # Convert logical bits to NRZI levels
    # Initial level is HIGH (1)
    current_level = 1
    nrzi_levels = []
    for bit in bits:
        if bit == 0:
            current_level = 1 - current_level
        nrzi_levels.append(current_level)

    asm = []
    asm.append(f"; --- HDLC Fault Injector: {fault_type} ---")
    asm.append(f"GDIRI 0x{pin_mask:02X}")
    asm.append(f"GWRI 0x{pin_mask:02X}")  # Idle high
    asm.append("WAIT 4")

    for lvl in nrzi_levels:
        val = pin_mask if lvl == 1 else 0
        asm.append(f"GWRI 0x{val:02X}")
        if normal_wait > 0:
            asm.append(f"WAIT {normal_wait}")

    # Return to idle HIGH and halt
    asm.append(f"GWRI 0x{pin_mask:02X}")
    asm.append("WAIT 4")
    asm.append("HALT")

    return "\n".join(asm)


def build_uart_fault_tx_asm(
    byte_val: int,
    fault_type: str,
    bit_period: int = 8,
    pin: int = 0,
) -> str:
    """
    Generate assembly firmware to transmit UART data with deliberate protocol faults.

    Supported fault_type values:
    - 'framing_error': Drives stop bit LOW (0) instead of HIGH (1).
    - 'noise_glitch': Injects a single-cycle runt pulse (0) on an idle line.
    - 'break_condition': Drives line LOW continuously for 14 bit periods.
    """
    pin_mask = 1 << pin
    normal_wait = max(0, bit_period - 2)

    asm = []
    asm.append(f"; --- UART Fault Injector: {fault_type} ---")
    asm.append(f"GDIRI 0x{pin_mask:02X}")
    asm.append(f"GWRI 0x{pin_mask:02X}")  # Idle HIGH
    asm.append("WAIT 4")

    if fault_type == "noise_glitch":
        # Drive LOW for exactly 1 cycle, then return HIGH
        asm.append("GWRI 0x00")
        asm.append(f"GWRI 0x{pin_mask:02X}")
        asm.append(f"WAIT {bit_period * 2}")
        asm.append("HALT")
        return "\n".join(asm)

    if fault_type == "break_condition":
        # Drive LOW for 14 bit periods
        asm.append("GWRI 0x00")
        for _ in range(14):
            asm.append(f"WAIT {bit_period - 1}")
        asm.append(f"GWRI 0x{pin_mask:02X}")
        asm.append("WAIT 4")
        asm.append("HALT")
        return "\n".join(asm)

    if fault_type == "framing_error":
        # Start bit (0)
        asm.append("GWRI 0x00")
        if normal_wait > 0:
            asm.append(f"WAIT {normal_wait}")

        # 8 data bits (LSB first)
        for b in range(8):
            bit = (byte_val >> b) & 1
            val = pin_mask if bit else 0
            asm.append(f"GWRI 0x{val:02X}")
            if normal_wait > 0:
                asm.append(f"WAIT {normal_wait}")

        # Corrupted Stop bit: drive LOW instead of HIGH!
        asm.append("GWRI 0x00")
        if normal_wait > 0:
            asm.append(f"WAIT {normal_wait}")

        # Return to idle and halt
        asm.append(f"GWRI 0x{pin_mask:02X}")
        asm.append("WAIT 4")
        asm.append("HALT")
        return "\n".join(asm)

    raise ValueError(f"Unknown UART fault_type: {fault_type}")


def build_manchester_fault_tx_asm(
    data_byte: int,
    violation_bit_index: int = 3,
    half_period: int = 4,
    pin: int = 2,
) -> str:
    """
    Generate assembly firmware to transmit Manchester Biphase-L data with an intentional
    biphase violation (no mid-bit transition) at violation_bit_index.

    IEEE 802.3 Manchester:
    - Logic 0: [0, 1] (rising edge at mid-bit)
    - Logic 1: [1, 0] (falling edge at mid-bit)
    - Violation: [0, 0] or [1, 1] (level held constant across entire bit cell)
    """
    pin_mask = 1 << pin
    half_wait = max(0, half_period - 2)

    asm = []
    asm.append("; --- Manchester Biphase-L Fault Injector ---")
    asm.append(f"GDIRI 0x{pin_mask:02X}")
    asm.append("GWRI 0x00")  # Idle low
    asm.append("WAIT 4")

    # Preamble Start Bit: Logic 1 -> [1, 0]
    asm.append(f"GWRI 0x{pin_mask:02X}")
    if half_wait > 0:
        asm.append(f"WAIT {half_wait}")
    asm.append("GWRI 0x00")
    if half_wait > 0:
        asm.append(f"WAIT {half_wait}")

    # 8 data bits (MSB first)
    for bit_idx in range(7, -1, -1):
        bit = (data_byte >> bit_idx) & 1

        if bit_idx == violation_bit_index:
            # Inject biphase violation: hold line at 0 for entire bit cell (2 * half_period)
            asm.append("GWRI 0x00")
            full_wait = (2 * half_period) - 2
            if full_wait > 0:
                asm.append(f"WAIT {full_wait}")
        else:
            if bit == 1:
                # [1, 0]
                asm.append(f"GWRI 0x{pin_mask:02X}")
                if half_wait > 0:
                    asm.append(f"WAIT {half_wait}")
                asm.append("GWRI 0x00")
                if half_wait > 0:
                    asm.append(f"WAIT {half_wait}")
            else:
                # [0, 1]
                asm.append("GWRI 0x00")
                if half_wait > 0:
                    asm.append(f"WAIT {half_wait}")
                asm.append(f"GWRI 0x{pin_mask:02X}")
                if half_wait > 0:
                    asm.append(f"WAIT {half_wait}")

    # Return to idle and halt
    asm.append("GWRI 0x00")
    asm.append("WAIT 4")
    asm.append("HALT")

    return "\n".join(asm)
