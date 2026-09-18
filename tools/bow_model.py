# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
tools/bow_model.py - Python Reference Model & Microcode Generators for Bunch of Wires (BoW / OpenHBI) Die-to-Die Physical Layer Engine

Provides:
- Open Compute Project (OCP) ODSA Bunch of Wires (BoW) & OpenHBI Die-to-Die Opcodes & Delimiters
- 16-bit CRC (CRC-16-IBM / ANSI polynomial 0x8005) calculation and BoW packet framing/decoding
- Receiver verification model tracking link lock, calibration state, and slice wire sparing/remapping
- Calibrated hardware PPA model for IHP 130nm SG13G2 platform
- Assembly microcode generators for master packet transmission via SHIFTOUT (MSB-first), slave sync ingress,
  opcode filtering/trapping, and slice spare wire allocation/underflow tracking.
"""

from enum import IntEnum
from typing import Dict, List, Tuple, Union


class BowOpCode(IntEnum):
    """Bunch of Wires (BoW / OpenHBI) Die-to-Die Interface Opcodes & Delimiters."""
    CALIB_REQ = 0x01          # Impedance calibration / termination matching request
    CALIB_RESP = 0x02         # Calibration response with drive strength / ODT settings
    TRAIN_STROBE_REQ = 0x03   # Forwarded strobe alignment & phase deskew training
    DATA_TRANSFER = 0x04      # Raw payload data flit transfer
    LANE_REMAP = 0x05         # Remap bad wire to spare wire within slice
    POWER_DOWN_REQ = 0x06     # Low-power sleep state transition request
    SYNC = 0xBC               # Bit-time training delimiter (K28.5 comma 0b10111100)
    IDLE = 0x7E               # Quiescent line keep-alive delimiter


def compute_bow_crc16(data: bytes, init: int = 0xFFFF) -> int:
    """
    Compute 16-bit BoW Packet CRC.
    Polynomial: CRC-16-IBM / ANSI standard (0x8005: x^16 + x^15 + x^2 + 1).
    Initial seed: 0xFFFF.
    """
    crc = init
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x8005) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc & 0xFFFF


def encode_bow_packet(opcode: int, slice_id: int, payload: bytes = b"") -> dict:
    """
    Encapsulates a BoW / OpenHBI Packet:
    SYNC (1 byte 0xBC) + OpCode (1 byte) + SliceID (1 byte) + Payload + CRC-16 (2 bytes, big-endian).
    """
    header_and_payload = bytes([opcode, slice_id & 0xFF]) + payload
    crc16 = compute_bow_crc16(header_and_payload)
    crc_bytes = crc16.to_bytes(2, byteorder="big")

    full_pkt = bytes([BowOpCode.SYNC]) + header_and_payload + crc_bytes
    return {
        "sync": BowOpCode.SYNC,
        "opcode": opcode,
        "slice_id": slice_id,
        "payload": payload,
        "crc16": crc16,
        "raw_bytes": full_pkt,
        "valid": True,
    }


def decode_bow_packet(raw: bytes) -> Tuple[int, int, int, bytes, int, bool]:
    """
    Decodes a BoW / OpenHBI Packet.
    Returns (sync, opcode, slice_id, payload, crc16, is_valid).
    """
    if len(raw) < 5:  # SYNC(1) + OpCode(1) + SliceID(1) + Payload(0+) + CRC(2)
        return (0, 0, 0, b"", 0, False)

    sync = raw[0]
    opcode = raw[1]
    slice_id = raw[2]
    payload = raw[3:-2]
    received_crc = int.from_bytes(raw[-2:], byteorder="big")

    expected_crc = compute_bow_crc16(raw[1:-2])
    is_valid = (sync == BowOpCode.SYNC) and (received_crc == expected_crc)
    return (sync, opcode, slice_id, payload, received_crc, is_valid)


class BowReceiverModel:
    """
    Independent behavioral verification model for BoW / OpenHBI receiver.
    Tracks received packets, valid CRC packets, link lock, and spare wire allocation.
    """

    def __init__(self, initial_spares: int = 1, wires_per_slice: int = 16):
        self.spare_wires = initial_spares
        self.wires_per_slice = wires_per_slice
        self.packets_received = 0
        self.crc_errors = 0
        self.sync_count = 0
        self.link_lock = False
        self.last_slice_id = 0
        self.remapped_wires = 0
        self.calibrated = False

    def process_packet(self, raw: bytes) -> bool:
        """Process incoming raw BoW packet bytes."""
        sync, opcode, slice_id, payload, crc16, is_valid = decode_bow_packet(raw)
        if not is_valid:
            self.crc_errors += 1
            return False

        self.packets_received += 1
        self.last_slice_id = slice_id

        if sync == BowOpCode.SYNC:
            self.sync_count += 1
            if self.sync_count >= 4:
                self.link_lock = True

        # Handle specific BoW opcodes
        if opcode == BowOpCode.CALIB_RESP:
            self.calibrated = True
        elif opcode == BowOpCode.LANE_REMAP:
            if self.spare_wires > 0:
                self.spare_wires -= 1
                self.remapped_wires += 1
            else:
                return False  # Sparing exhausted

        return True


class BowPpaModel:
    """
    Calibrated physical PPA scaling model for dedicated Bunch of Wires (BoW) & OpenHBI macro
    on IHP 130nm SG13G2 platform.
    """

    @staticmethod
    def get_metrics() -> Dict[str, Union[int, float]]:
        return {
            "macro_cells": 610,
            "macro_ge": 1190.0,
            "macro_area_um2": 4520.0,
            "f_max_mhz": 800.0,
            "nominal_power_uw_10mhz": 59.0,
            "raw_throughput_mbps": 32000.0,  # 16 wires @ 2 Gbps (BoW-Base standard package)
            "energy_pj_per_bit": 0.00045,
        }


def build_bow_tx_packet_asm(
    sync_code: int = 0xBC,
    opcode: int = 0x01,
    slice_id: int = 0x00,
    baud_cycles: int = 4,
    pin_tx: int = 3,
) -> List[str]:
    """
    Generates microcode to transmit a BoW / OpenHBI packet header using OP_SHIFTOUT (MSB-first):
    - Sets pin_tx as output
    - Transmits SYNC training byte (0xBC) MSB-first via SHIFTOUT (operand[3]=1)
    - Transmits OpCode header byte (e.g. 0x01 CALIB_REQ) MSB-first via SHIFTOUT
    - Transmits SliceID byte (e.g. 0x00) MSB-first via SHIFTOUT
    - Preloads next byte during bit 7 wait to ensure strict baud_cycles timing per bit
    - Asserts status R2 = 0x00 and halts
    """
    asm: List[str] = []
    oe_mask = (1 << pin_tx)
    asm.append(f"GDIRI 0x{oe_mask:02X}        ; Configure TX pin as output")
    asm.append("GWRI 0x00             ; Idle bus low")

    # For SHIFTOUT with MSB-first on pin_tx: operand = (1 << 3) | (pin_tx & 0x07)
    shift_operand = (1 << 3) | (pin_tx & 0x07)
    wait_delay = max(0, baud_cycles - 2)
    wait_delay_transition = max(0, baud_cycles - 3)

    bytes_to_send = [sync_code, opcode, slice_id]

    for byte_idx, val in enumerate(bytes_to_send):
        if byte_idx == 0:
            asm.append(f"LDI R0, 0x{val:02X}        ; Load initial byte into R0")

        for bit_idx in range(8):
            asm.append(f"SHIFTOUT R0, 0x{shift_operand:02X}   ; Byte {byte_idx} bit {bit_idx} (MSB-first)")
            if bit_idx < 7:
                if wait_delay > 0:
                    asm.append(f"WAIT {wait_delay}")
            else:
                if byte_idx < len(bytes_to_send) - 1:
                    next_val = bytes_to_send[byte_idx + 1]
                    if wait_delay_transition > 0:
                        asm.append(f"WAIT {wait_delay_transition}")
                    asm.append(f"LDI R0, 0x{next_val:02X}        ; Preload next byte into R0")
                else:
                    if wait_delay > 0:
                        asm.append(f"WAIT {wait_delay}")

    asm.append("GWRI 0x00             ; Idle bus low")
    asm.append("LDI R2, 0x00           ; Status: TX Complete (R2 = 0x00)")
    asm.append("HALT                   ; Halt execution")
    return asm


def build_bow_rx_sync_asm(
    pin_rx: int = 3, baud_cycles: int = 4
) -> List[str]:
    """
    Generates microcode to synchronize to a BoW SYNC delimiter (MSB-first):
    - SYNC = 0xBC (0b10111100). The first bit (bit 7) is 1.
    - If line is idle low, bit 7 causes an immediate rising edge on pin_rx.
    - Waits for rising edge on pin_rx via WAITEDGE (operand 0x0B).
    - Strides past remaining 7 bits of delimiter to opcode bit 7.
    - Samples 8 subsequent bits MSB-first into R0 using SHIFTIN with operand[3]=1.
    - Preserves sampled byte in R1.
    - Halts with status R2 = 0x00.
    """
    asm: List[str] = []
    asm.append("GDIRI 0x00             ; Configure all pins as inputs")
    asm.append("LDI R0, 0x00           ; Clear R0")
    asm.append("LDI R1, 0x00           ; Clear R1")
    asm.append("LDI R2, 0x00           ; Clear R2 (Status)")

    # Wait for rising edge on pin_rx (mode 01 = rising edge)
    operand_rise = (0x01 << 3) | (pin_rx & 0x07)
    asm.append(f"WAITEDGE R3, 0x{operand_rise:02X} ; Wait for SYNC delimiter rising edge (bit 7)")

    # Stride past remaining 7 bits of delimiter to opcode byte bit 7 midpoint
    mid_wait = max(0, (baud_cycles * 7) + 2)
    if mid_wait > 0:
        asm.append(f"WAIT {mid_wait}           ; Stride past delimiter to opcode MSB midpoint")

    wait_step = max(0, baud_cycles - 2)
    # SHIFTIN MSB-first: operand = (1 << 3) | (pin_rx & 0x07)
    operand_sample = (1 << 3) | (pin_rx & 0x07)
    for _ in range(8):
        asm.append(f"SHIFTIN R0, 0x{operand_sample:02X}  ; Sample data bit MSB-first into R0")
        if wait_step > 0:
            asm.append(f"WAIT {wait_step}")

    asm.append("MOV R1, R0             ; Preserve received byte in R1")
    asm.append("LDI R2, 0x00           ; Status: Sync & Ingress Success (R2 = 0x00)")
    asm.append("HALT                   ; Halt execution")
    return asm


def build_bow_opcode_filter_asm(test_opcode: int) -> List[str]:
    """
    Validates in-register BoW / OpenHBI opcode:
    - Valid opcodes:
      0x01 (CALIB_REQ), 0x02 (CALIB_RESP), 0x03 (TRAIN_STROBE_REQ),
      0x04 (DATA_TRANSFER), 0x05 (LANE_REMAP), 0x06 (POWER_DOWN_REQ)
    - If valid: R2 = 0x00
    - If invalid: R2 = 0xEE (Fault Trap)
    """
    asm: List[str] = []
    asm.append(f"LDI R0, 0x{test_opcode:02X}     ; Load test opcode into R0")
    asm.append("LDI R2, 0xEE           ; Default status = Fault Trap (0xEE)")

    # Test CALIB_REQ (0x01)
    asm.append("MOV R3, R0             ; Copy opcode to R3")
    asm.append("XORI R3, 0x01          ; Test CALIB_REQ")
    asm.append("JZ MATCH               ; If match, jump to success")

    # Test CALIB_RESP (0x02)
    asm.append("MOV R3, R0             ; Copy opcode to R3")
    asm.append("XORI R3, 0x02          ; Test CALIB_RESP")
    asm.append("JZ MATCH               ; If match, jump to success")

    # Test TRAIN_STROBE_REQ (0x03)
    asm.append("MOV R3, R0             ; Copy opcode to R3")
    asm.append("XORI R3, 0x03          ; Test TRAIN_STROBE_REQ")
    asm.append("JZ MATCH               ; If match, jump to success")

    # Test DATA_TRANSFER (0x04)
    asm.append("MOV R3, R0             ; Copy opcode to R3")
    asm.append("XORI R3, 0x04          ; Test DATA_TRANSFER")
    asm.append("JZ MATCH               ; If match, jump to success")

    # Test LANE_REMAP (0x05)
    asm.append("MOV R3, R0             ; Copy opcode to R3")
    asm.append("XORI R3, 0x05          ; Test LANE_REMAP")
    asm.append("JZ MATCH               ; If match, jump to success")

    # Test POWER_DOWN_REQ (0x06)
    asm.append("MOV R3, R0             ; Copy opcode to R3")
    asm.append("XORI R3, 0x06          ; Test POWER_DOWN_REQ")
    asm.append("JZ MATCH               ; If match, jump to success")

    # No match -> halt with R2 = 0xEE
    asm.append("HALT")

    # Match target
    asm.append("MATCH:")
    asm.append("LDI R2, 0x00           ; Status: Valid Opcode Match (R2 = 0x00)")
    asm.append("HALT")
    return asm


def build_bow_spare_tracker_asm(
    remap_event: int = 1, initial_spares: int = 1
) -> List[str]:
    """
    Maintains spare wire allocation tracking for a BoW / OpenHBI slice:
    - R0: Current spare wire count (default 1 per slice)
    - R1: Event (0x01: Remap bad wire using spare -> spare -= 1; 0x02: Restore spare -> spare += 1)
    - If event == 1:
        If R0 == 0: underflow error (no spares available) -> R2 = 0xEE
        Else: R0 -= 1, R2 = 0x00
    - If event == 2:
        R0 += 1, R2 = 0x00
    - If unknown event: R2 = 0xEE
    """
    asm: List[str] = []
    asm.append(f"LDI R0, 0x{initial_spares:02X}   ; Initial spare wire count in R0")
    asm.append(f"LDI R1, 0x{remap_event:02X}   ; Event in R1 (1=Remap, 2=Restore)")
    asm.append("LDI R2, 0x00           ; Default status = 0x00")

    # Check if event == 1 (Remap wire using spare)
    asm.append("MOV R3, R1             ; Copy event to R3")
    asm.append("XORI R3, 0x01          ; Test event == 1")
    asm.append("JZ DO_REMAP            ; Jump to remap")

    # Check if event == 2 (Restore spare wire)
    asm.append("MOV R3, R1             ; Copy event to R3")
    asm.append("XORI R3, 0x02          ; Test event == 2")
    asm.append("JZ DO_RESTORE          ; Jump to restore")

    # Unknown event
    asm.append("LDI R2, 0xEE           ; Error: Unknown event")
    asm.append("HALT")

    # Remap branch (consume spare)
    asm.append("DO_REMAP:")
    asm.append("MOV R3, R0             ; Test current spare count")
    asm.append("XORI R3, 0x00          ; Check if spares == 0")
    asm.append("JZ UNDERFLOW           ; If 0, trap underflow")
    asm.append("SUBI R0, 0x01          ; Decrement spare count by 1")
    asm.append("LDI R2, 0x00           ; Status = 0x00")
    asm.append("HALT")

    # Restore branch
    asm.append("DO_RESTORE:")
    asm.append("ADDI R0, 0x01          ; Increment spare count by 1")
    asm.append("LDI R2, 0x00           ; Status = 0x00")
    asm.append("HALT")

    # Underflow trap
    asm.append("UNDERFLOW:")
    asm.append("LDI R2, 0xEE           ; Status: Sparing Exhaustion Error (0xEE)")
    asm.append("HALT")
    return asm
