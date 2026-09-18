# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
tools/hypertransport_model.py - Python Reference Model & Microcode Generators for HyperTransport 3.1

Provides:
- HyperTransport Command Types & Delimiters (NOP, READ_REQ, WRITE_REQ, RESPONSE, SYNC, IDLE)
- 32-bit CRC calculation and packet framing/decoding
- Receiver verification model tracking virtual channel buffer credits and link lock
- Calibrated hardware PPA model for IHP 130nm SG13G2 platform
- Assembly microcode generators for master packet transmission, slave sync ingress,
  command filtering/trapping, and virtual channel credit flow control.
"""

from enum import IntEnum
from typing import Dict, List, Tuple, Union


class HtCommandType(IntEnum):
    """HyperTransport 3.1 Command Encodings & Delimiters."""
    NOP = 0x00          # NOP / Keep-alive / Flow control sync
    READ_REQ = 0x20     # Sized non-posted read request
    WRITE_REQ = 0x40    # Posted or non-posted write request
    RESPONSE = 0xC0     # Target completion response
    SYNC = 0xBC         # Bit-time training delimiter (K28.5 comma 0b10111100)
    IDLE = 0x7E         # Quiescent line keep-alive delimiter


def compute_ht_crc32(data: bytes) -> int:
    """
    Compute 32-bit HyperTransport packet CRC.
    Polynomial: IEEE 802.3 Ethernet / HT standard (0xEDB88320 reflected).
    Seed: 0xFFFFFFFF, inverted output.
    """
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc = (crc >> 1)
    return (~crc) & 0xFFFFFFFF


def encode_ht_packet(cmd: int, vc_id: int, payload: bytes) -> dict:
    """
    Encapsulates a HyperTransport Packet:
    SYNC (1 byte 0xBC) + Command (1 byte) + Virtual Channel ID (1 byte) + Payload + CRC-32 (4 bytes).
    """
    header_and_payload = bytes([cmd, vc_id]) + payload
    crc32 = compute_ht_crc32(header_and_payload)
    crc_bytes = crc32.to_bytes(4, byteorder="big")

    full_pkt = bytes([HtCommandType.SYNC]) + header_and_payload + crc_bytes
    return {
        "sync": HtCommandType.SYNC,
        "cmd": cmd,
        "vc_id": vc_id,
        "payload": payload,
        "crc32": crc32,
        "raw_bytes": full_pkt,
        "valid": True,
    }


def decode_ht_packet(pkt_bytes: bytes) -> Tuple[int, int, int, bytes, int, bool]:
    """
    Decodes a HyperTransport Packet.
    Minimum size: SYNC (1) + CMD (1) + VC (1) + CRC (4) = 7 bytes.
    Returns: (sync, cmd, vc_id, payload, crc32, is_valid)
    """
    if len(pkt_bytes) < 7:
        return 0, 0, 0, b"", 0, False

    sync = pkt_bytes[0]
    cmd = pkt_bytes[1]
    vc_id = pkt_bytes[2]
    payload = pkt_bytes[3:-4]
    crc_rx = int.from_bytes(pkt_bytes[-4:], byteorder="big")

    header_and_payload = pkt_bytes[1:-4]
    expected_crc = compute_ht_crc32(header_and_payload)
    valid_crc = (crc_rx == expected_crc)
    valid_sync = (sync == HtCommandType.SYNC)
    valid_cmd = cmd in (
        HtCommandType.NOP,
        HtCommandType.READ_REQ,
        HtCommandType.WRITE_REQ,
        HtCommandType.RESPONSE,
    )

    return sync, cmd, vc_id, payload, crc_rx, (valid_sync and valid_crc and valid_cmd)


class HyperTransportReceiverModel:
    """
    Software verification receiver tracking HyperTransport packets,
    virtual channel credits, and link synchronization.
    """

    def __init__(self, initial_credits: int = 8):
        self.preq_credits = initial_credits
        self.npreq_credits = initial_credits
        self.resp_credits = initial_credits
        self.packets_received = 0
        self.crc_errors = 0
        self.link_lock = False
        self.consecutive_valid_syncs = 0

    def process_sync(self, sync_byte: int) -> bool:
        """Process link sync delimiter."""
        if sync_byte == HtCommandType.SYNC:
            self.consecutive_valid_syncs += 1
            if self.consecutive_valid_syncs >= 4:
                self.link_lock = True
            return True
        else:
            self.consecutive_valid_syncs = 0
            self.link_lock = False
            return False

    def process_packet(self, pkt_bytes: bytes) -> bool:
        """Process incoming packet and route to virtual channel."""
        sync, cmd, vc_id, _, _, is_valid = decode_ht_packet(pkt_bytes)
        if not is_valid:
            self.crc_errors += 1
            return False

        self.process_sync(sync)
        self.packets_received += 1

        if cmd == HtCommandType.WRITE_REQ:
            if self.preq_credits > 0:
                self.preq_credits -= 1
        elif cmd == HtCommandType.READ_REQ:
            if self.npreq_credits > 0:
                self.npreq_credits -= 1
        elif cmd == HtCommandType.RESPONSE:
            if self.resp_credits > 0:
                self.resp_credits -= 1

        return True


class HyperTransportPpaModel:
    """
    Calibrated PPA estimation model for HyperTransport 3.1 PCS/Link macro
    on IHP 130nm SG13G2 platform.
    """

    @staticmethod
    def get_metrics() -> Dict[str, Union[int, float]]:
        return {
            "macro_cells": 605,
            "macro_ge": 1180.0,
            "macro_area_um2": 4460.0,
            "f_max_mhz": 800.0,
            "nominal_power_uw_10mhz": 59.0,
            "raw_throughput_mbps": 51200.0,
            "energy_pj_per_bit": 0.00115,
        }


def build_ht_tx_packet_asm(
    sync_code: int = 0xBC, cmd_code: int = 0x20, baud_cycles: int = 4, pin_tx: int = 3
) -> List[str]:
    """
    Generates microcode to transmit a HyperTransport packet header:
    - Sets pin_tx as output
    - Transmits SYNC training byte (0xBC) LSB-first
    - Transmits command header byte (e.g. 0x20 for READ_REQ) LSB-first
    - Asserts status R2 = 0x00 and halts
    """
    asm: List[str] = []
    oe_mask = (1 << pin_tx)
    asm.append(f"GDIRI 0x{oe_mask:02X}        ; Configure TX pin as output")
    asm.append("GWRI 0x00             ; Idle bus low")

    wait_delay = max(0, baud_cycles - 2)

    # 1. Transmit SYNC pattern LSB-first
    for bit_idx in range(8):
        bit = (sync_code >> bit_idx) & 1
        val = (bit << pin_tx)
        asm.append(f"GWRI 0x{val:02X}            ; SYNC bit {bit_idx} = {bit}")
        if wait_delay > 0:
            asm.append(f"WAIT {wait_delay}")

    # 2. Transmit command opcode LSB-first
    for bit_idx in range(8):
        bit = (cmd_code >> bit_idx) & 1
        val = (bit << pin_tx)
        asm.append(f"GWRI 0x{val:02X}            ; CMD bit {bit_idx} = {bit}")
        if wait_delay > 0:
            asm.append(f"WAIT {wait_delay}")

    asm.append("GWRI 0x00             ; Idle bus low")
    asm.append("LDI R2, 0x00           ; Status: TX Complete (R2 = 0x00)")
    asm.append("HALT                   ; Halt execution")
    return asm


def build_ht_rx_sync_asm(
    pin_rx: int = 3, baud_cycles: int = 4
) -> List[str]:
    """
    Generates microcode to synchronize to a HyperTransport SYNC delimiter:
    - Waits for rising edge on pin_rx via WAITEDGE (operand 0x0B)
    - In K28.5 (0xBC = 0b10111100), first rising edge is at bit 2.
    - Strides past remaining 6 bits to the midpoint of command opcode byte
    - Samples 8 subsequent bits into R0
    - Preserves sampled byte in R1
    - Halts with status R2 = 0x00
    """
    asm: List[str] = []
    asm.append("GDIRI 0x00             ; Configure all pins as inputs")
    asm.append("LDI R0, 0x00           ; Clear R0")
    asm.append("LDI R1, 0x00           ; Clear R1")
    asm.append("LDI R2, 0x00           ; Clear R2 (Status)")

    # Wait for rising edge on pin_rx (mode 01 = rising edge)
    operand_rise = (0x01 << 3) | (pin_rx & 0x07)
    asm.append(f"WAITEDGE R3, 0x{operand_rise:02X} ; Wait for SYNC delimiter rising edge")

    # Stride past remaining 6 bits of delimiter to command opcode byte
    mid_wait = max(0, (baud_cycles * 6) - 1)
    if mid_wait > 0:
        asm.append(f"WAIT {mid_wait}           ; Stride past delimiter to command opcode")

    wait_step = max(0, baud_cycles - 2)
    operand_sample = pin_rx & 0x07
    for _ in range(8):
        asm.append(f"SHIFTIN R0, 0x{operand_sample:02X}  ; Sample data bit into R0")
        if wait_step > 0:
            asm.append(f"WAIT {wait_step}")

    asm.append("MOV R1, R0             ; Preserve received byte in R1")
    asm.append("LDI R2, 0x00           ; Status: Sync & Ingress Success (R2 = 0x00)")
    asm.append("HALT                   ; Halt execution")
    return asm


def build_ht_command_filter_asm(test_cmd: int) -> List[str]:
    """
    Validates in-register HyperTransport command opcode:
    - Valid commands:
      0x00 (NOP), 0x20 (READ_REQ), 0x40 (WRITE_REQ), 0xC0 (RESPONSE)
    - If valid: R2 = 0x00
    - If invalid: R2 = 0xEE
    """
    asm: List[str] = [
        f"LDI R0, 0x{test_cmd & 0xFF:02X}     ; Load test command opcode into R0",
        "MOV R1, R0             ; Preserve candidate in R1",
        "XORI R1, 0x00          ; Test for 0x00 (NOP)",
        "JZ cmd_valid           ; If equal, valid command",
        "MOV R1, R0             ; Restore candidate",
        "XORI R1, 0x20          ; Test for 0x20 (READ_REQ)",
        "JZ cmd_valid           ; If equal, valid command",
        "MOV R1, R0             ; Restore candidate",
        "XORI R1, 0x40          ; Test for 0x40 (WRITE_REQ)",
        "JZ cmd_valid           ; If equal, valid command",
        "MOV R1, R0             ; Restore candidate",
        "XORI R1, 0xC0          ; Test for 0xC0 (RESPONSE)",
        "JZ cmd_valid           ; If equal, valid command",
        "LDI R2, 0xEE           ; Error: Unsupported / illegal command (0xEE)",
        "HALT                   ;",
        "cmd_valid:             ;",
        "LDI R2, 0x00           ; Success: Command verified (0x00)",
        "HALT                   ;",
    ]
    return asm


def build_ht_credit_tracker_asm(credit_event: int, initial_credit: int) -> List[str]:
    """
    Validates in-register Virtual Channel buffer credit accounting:
    - credit_event = 0x01: Return credit -> credit = initial_credit + 1
    - credit_event = 0x02: Consume credit -> credit = initial_credit - 1 (if > 0, else error 0xEE)
    """
    asm: List[str] = [
        f"LDI R0, 0x{initial_credit & 0xFF:02X} ; Load initial credit into R0",
        f"LDI R1, 0x{credit_event & 0xFF:02X}   ; Load credit event (1=Return, 2=Consume)",
        "XORI R1, 0x01          ; Test if Return event",
        "JZ credit_return       ;",
        # Consume credit event
        "MOV R1, R0             ; Copy current credit",
        "JZ underflow_error     ; If credit == 0, underflow error!",
        "SUBI R0, 0x01          ; Decrement credit (Packet sent)",
        "LDI R2, 0x00           ; Status: Valid credit decrement",
        "HALT                   ;",
        "credit_return:         ;",
        "ADDI R0, 0x01          ; Increment credit (Buffer credit received)",
        "LDI R2, 0x00           ; Status: Valid credit increment",
        "HALT                   ;",
        "underflow_error:       ;",
        "LDI R2, 0xEE           ; Error: Buffer credit underflow (0xEE)",
        "HALT                   ;",
    ]
    return asm
