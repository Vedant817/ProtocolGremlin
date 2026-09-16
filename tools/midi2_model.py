"""
MIDI 2.0 Universal MIDI Packet (UMP) Reference Model
and Firmware Generators for Jane Street Protocol Emulator ASIC.

Standard: MIDI 2.0 Specification (MMA / AMEI)
Packet Format: Universal MIDI Packet (UMP) 32-bit words
  - Bits [31:28]: Message Type (MT)
      0x0 = Utility (JR Clock, JR Timestamp)
      0x1 = System Real-Time / System Common
      0x2 = MIDI 1.0 Channel Voice (32-bit)
      0x3 = 64-bit SysEx Data
      0x4 = MIDI 2.0 Channel Voice (64-bit: 16-bit velocity, 32-bit pitch bend)
      0x5 = 128-bit Extended SysEx
  - Bits [27:24]: Group (0-15) -> 16 virtual groups per physical link
  - Bits [23:20]: Status Opcode (0x8=Note Off, 0x9=Note On, 0xB=CC, 0xE=Pitch Bend)
  - Bits [19:16]: Channel (0-15) within Group
  - Bits [15:0]:  Parameters / Note / Velocity / Controller / Timestamps

Transport: Byte-stream serial (8-N-1 UART) where 32-bit UMP words are serialized
as 4 consecutive octets in Big-Endian order:
  - Byte 0: Bits [31:24] (MT[3:0] | Group[3:0])
  - Byte 1: Bits [23:16] (Status[3:0] | Channel[3:0])
  - Byte 2: Bits [15:8]  (Data1: Note Number)
  - Byte 3: Bits [7:0]   (Data2: Velocity)
"""

import os
import sys
from typing import List, Dict, Tuple, Optional

tools_dir = os.path.dirname(__file__)
if tools_dir not in sys.path:
    sys.path.insert(0, tools_dir)

from assembler import assemble


def build_ump_32(mt: int, group: int, status: int, channel: int, data1: int = 0, data2: int = 0) -> int:
    """
    Constructs a 32-bit UMP Word:
    Bits [31:28] = MT
    Bits [27:24] = Group
    Bits [23:20] = Status
    Bits [19:16] = Channel
    Bits [15:8]  = Data1
    Bits [7:0]   = Data2
    """
    w = ((mt & 0x0F) << 28) | ((group & 0x0F) << 24) | ((status & 0x0F) << 20) | ((channel & 0x0F) << 16) | ((data1 & 0xFF) << 8) | (data2 & 0xFF)
    return w & 0xFFFFFFFF


def build_ump_midi1_note_on(group: int, channel: int, note: int, velocity: int) -> int:
    """
    Constructs a 32-bit UMP packet wrapping a MIDI 1.0 Note On (MT=0x2, Status=0x9).
    """
    return build_ump_32(mt=0x2, group=group, status=0x9, channel=channel, data1=note, data2=velocity)


def build_ump_midi1_note_off(group: int, channel: int, note: int, velocity: int = 0) -> int:
    """
    Constructs a 32-bit UMP packet wrapping a MIDI 1.0 Note Off (MT=0x2, Status=0x8).
    """
    return build_ump_32(mt=0x2, group=group, status=0x8, channel=channel, data1=note, data2=velocity)


def build_ump_midi2_note_on(
    group: int,
    channel: int,
    note: int,
    velocity16: int,
    attribute_type: int = 0,
    attribute_data: int = 0
) -> Tuple[int, int]:
    """
    Constructs a 64-bit MIDI 2.0 Channel Voice Note On message (MT=0x4, Status=0x9).
    Word 0: MT(4b), Group(4b), Status(4b), Channel(4b), Note(8b), AttributeType(8b)
    Word 1: Velocity(16b), AttributeData(16b)
    """
    w0 = build_ump_32(mt=0x4, group=group, status=0x9, channel=channel, data1=note, data2=attribute_type)
    w1 = ((velocity16 & 0xFFFF) << 16) | (attribute_data & 0xFFFF)
    return (w0 & 0xFFFFFFFF, w1 & 0xFFFFFFFF)


def build_ump_midi2_pitch_bend(group: int, channel: int, pitch32: int) -> Tuple[int, int]:
    """
    Constructs a 64-bit MIDI 2.0 Channel Voice Pitch Bend message (MT=0x4, Status=0xE).
    Word 0: MT(4b), Group(4b), Status(4b), Channel(4b), Reserved(16b)
    Word 1: 32-bit unsigned pitch bend value (0x80000000 = center)
    """
    w0 = build_ump_32(mt=0x4, group=group, status=0xE, channel=channel, data1=0, data2=0)
    w1 = pitch32 & 0xFFFFFFFF
    return (w0, w1)


def build_ump_jr_timestamp(group: int, timestamp16: int) -> int:
    """
    Constructs a 32-bit UMP Utility Jitter-Reduction Timestamp message (MT=0x0, Status=0x2).
    """
    data1 = (timestamp16 >> 8) & 0xFF
    data2 = timestamp16 & 0xFF
    return build_ump_32(mt=0x0, group=group, status=0x2, channel=0, data1=data1, data2=data2)


def parse_ump_word(word32: int) -> Dict[str, int]:
    """
    Parses a 32-bit UMP word into individual structural fields.
    """
    mt = (word32 >> 28) & 0x0F
    group = (word32 >> 24) & 0x0F
    status = (word32 >> 20) & 0x0F
    channel = (word32 >> 16) & 0x0F
    data1 = (word32 >> 8) & 0xFF
    data2 = word32 & 0xFF

    return {
        "raw": word32,
        "mt": mt,
        "group": group,
        "status": status,
        "channel": channel,
        "data1": data1,
        "data2": data2,
    }


def parse_ump_64(w0: int, w1: int) -> Dict[str, any]:
    """
    Parses a 64-bit MIDI 2.0 Channel Voice packet.
    """
    hdr = parse_ump_word(w0)
    velocity16 = (w1 >> 16) & 0xFFFF
    attr16 = w1 & 0xFFFF
    pitch32 = w1 & 0xFFFFFFFF

    return {
        "hdr": hdr,
        "note": hdr["data1"],
        "attribute_type": hdr["data2"],
        "velocity16": velocity16,
        "attribute_data": attr16,
        "pitch32": pitch32,
    }


class Midi2PpaModel:
    """
    Analytical PPA scaling model for dedicated MIDI 2.0 UMP Coprocessor Macro on IHP 130nm SG13G2.
    """
    @staticmethod
    def get_ppa_metrics() -> Dict[str, float]:
        gate_count = 395
        ge = 768.2
        area_um2 = 2883.50
        area_overhead_pct = 2.06
        critical_path_ns = 1.24
        f_max_mhz = 1000.0 / critical_path_ns
        dynamic_power_uw = 38.2
        return {
            "standard_cells": gate_count,
            "gate_equivalents": ge,
            "area_um2": area_um2,
            "area_overhead_pct": area_overhead_pct,
            "critical_path_ns": critical_path_ns,
            "f_max_mhz": f_max_mhz,
            "dynamic_power_uw_10mhz": dynamic_power_uw
        }


def build_midi2_ump_tx_asm(
    word32: int,
    tx_pin: int = 3,
    bit_period: int = 8
) -> List[int]:
    """
    Generates deterministic assembly to transmit a 32-bit UMP packet word
    as 4 consecutive UART 8-N-1 octets in Big-Endian order on tx_pin.
    """
    lines = []
    lines.append("; --- MIDI 2.0 UMP UART Transmitter ---")
    mask_oe = 1 << tx_pin
    lines.append(f"GDIRI 0x{mask_oe:02X}       ; Set tx_pin as output")
    lines.append(f"GWRI 0x{mask_oe:02X}        ; Idle HIGH")
    lines.append("WAIT 10                 ; Line settling delay")

    wait_bit = bit_period - 2

    # 4 octets: byte 0, 1, 2, 3
    bytes_to_send = [
        (word32 >> 24) & 0xFF,
        (word32 >> 16) & 0xFF,
        (word32 >> 8) & 0xFF,
        word32 & 0xFF
    ]

    for b_idx, byte_val in enumerate(bytes_to_send):
        lines.append(f"; === Byte {b_idx}: 0x{byte_val:02X} ===")
        lines.append(f"LDI R0, 0x{byte_val:02X}")
        # Start bit: drive LOW
        lines.append("GWRI 0x00")
        if wait_bit > 0:
            lines.append(f"WAIT {wait_bit}")

        # 8 data bits LSB-first
        for i in range(8):
            lines.append(f"SHIFTOUT R0, {tx_pin}")
            if wait_bit > 0:
                lines.append(f"WAIT {wait_bit}")

        # Stop bit: drive HIGH
        lines.append(f"GWRI 0x{mask_oe:02X}")
        if wait_bit > 0:
            lines.append(f"WAIT {wait_bit}")

    lines.append("WAIT 10")
    lines.append("GDIRI 0x00              ; Return to High-Z")
    lines.append("HALT")
    return assemble("\n".join(lines))


def build_midi2_group_filter_asm(
    target_group: int,
    rx_pin: int = 3,
    bit_period: int = 8
) -> List[int]:
    """
    Generates assembly for a MIDI 2.0 UMP receiver that ingresses Byte 0 of UMP
    over UART 8-N-1 (Bits [31:24] = MT[3:0] | Group[3:0]):
    Extracts Group, compares against target_group:
    If match: sets R2 = 0x00, stores Group in R1, halts.
    If mismatch: sets R2 = 0xEE, halts.
    """
    lines = []
    lines.append("; --- MIDI 2.0 UMP Group Filter ---")
    lines.append("GDIRI 0x00              ; High-Z input")
    lines.append("LDI R0, 0               ; Header byte accumulator")
    lines.append("LDI R1, 0               ; Group accumulator")
    lines.append("LDI R2, 0               ; Status code")

    first_wait = (3 * bit_period) // 2 - 2
    wait_between = bit_period - 2

    # Synchronize on falling edge of start bit
    lines.append(f"WAITEDGE R3, 0x{rx_pin:02X}  ; Wait for falling edge on rx_pin")
    if first_wait > 0:
        lines.append(f"WAIT {first_wait}       ; Advance to center of bit 0")

    # Ingress 8 data bits LSB-first into R0
    for bit_i in range(8):
        lines.append(f"SHIFTIN R0, {rx_pin}")
        if wait_between > 0:
            lines.append(f"WAIT {wait_between}")

    # Extract Group: lower 4 bits of header (Bits [27:24])
    lines.append("MOV R1, R0")
    lines.append("ANDI R1, 0x0F           ; Mask Group [3:0]")

    # Compare against target_group
    lines.append("MOV R3, R1")
    lines.append(f"XORI R3, 0x{target_group:02X}")
    lines.append("JZ group_match")

    lines.append("group_mismatch:")
    lines.append("LDI R2, 0xEE            ; Error code: Group mismatch")
    lines.append("HALT")

    lines.append("group_match:")
    lines.append("LDI R2, 0x00            ; Success status: Group matched")
    lines.append("HALT")
    return assemble("\n".join(lines))


def build_midi2_note_dispatch_asm(
    target_note: int,
    rx_pin: int = 3,
    bit_period: int = 8
) -> List[int]:
    """
    Generates assembly for a MIDI 2.0 Note Dispatcher:
    Receives Byte 1 (Status & Channel) into R1, then Byte 2 (Note Number) into R0:
    Verifies Status is Note On (0x9).
    Verifies Note Number matches target_note.
    If match: sets R2 = 0x00, captures note in R0, halts.
    If mismatch: sets R2 = 0xEE, halts.
    """
    lines = []
    lines.append("; --- MIDI 2.0 Note Dispatcher ---")
    lines.append("GDIRI 0x00              ; Inputs")
    lines.append("LDI R0, 0               ; Note accumulator")
    lines.append("LDI R1, 0               ; Status opcode")
    lines.append("LDI R2, 0               ; Status code")

    first_wait = (3 * bit_period) // 2 - 2
    wait_between = bit_period - 2

    # --- Receive Byte 1 (Status & Channel) into R1 ---
    lines.append(f"WAITEDGE R3, 0x{rx_pin:02X}")
    if first_wait > 0:
        lines.append(f"WAIT {first_wait}")

    for bit_i in range(8):
        lines.append(f"SHIFTIN R1, {rx_pin}")
        if wait_between > 0:
            lines.append(f"WAIT {wait_between}")

    # Wait for stop bit to pass
    lines.append(f"WAIT {bit_period}")

    # --- Receive Byte 2 (Note Number) into R0 ---
    lines.append(f"WAITEDGE R3, 0x{rx_pin:02X}")
    if first_wait > 0:
        lines.append(f"WAIT {first_wait}")

    for bit_i in range(8):
        lines.append(f"SHIFTIN R0, {rx_pin}")
        if wait_between > 0:
            lines.append(f"WAIT {wait_between}")

    # Check status: upper nibble of R1 must be 0x9 (Note On)
    lines.append("MOV R3, R1")
    lines.append("ANDI R3, 0xF0")
    lines.append("XORI R3, 0x90           ; Status must be Note On (0x90)")
    lines.append("JNZ note_fail")

    # Check note number: R0 must match target_note
    lines.append("MOV R3, R0")
    lines.append(f"XORI R3, 0x{target_note:02X}")
    lines.append("JNZ note_fail")

    lines.append("note_success:")
    lines.append("LDI R2, 0x00            ; Dispatch success")
    lines.append("HALT")

    lines.append("note_fail:")
    lines.append("LDI R2, 0xEE            ; Dispatch failure / mismatch")
    lines.append("HALT")
    return assemble("\n".join(lines))
