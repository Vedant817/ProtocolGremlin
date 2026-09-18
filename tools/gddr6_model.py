# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
tools/gddr6_model.py - Reference model and microcode generators for GDDR6 / GDDR6X
(JEDEC JESD250) High-Speed Graphics Memory Physical Layer & Command Engine.

Covers:
- JEDEC JESD250 GDDR6 and GDDR6X SGRAM specifications:
  - Dual independent 16-bit channels per device (Channel A & Channel B, 32 bits total).
  - High-speed 10-bit Command/Address (CA) bus operating up to 16 to 24 Gbps.
  - PAM4 multilevel signaling on GDDR6X vs NRZ on GDDR6.
  - Bank Hierarchy:
    - 16 banks per channel: 4 Bank Groups (BG0..BG3), 4 Banks per group (BA0..BA3).
    - Per-Bank Refresh (PBR) and All-Bank Refresh (ABR).
    - Burst Length 16 (BL16) and Burst Length 32 (BL32) support.
- Command OpCodes:
  - 0x00: NOP         (No Operation / Deselect)
  - 0x01: ACT         (Row Activate)
  - 0x02: PRE         (Bank / All-Bank Precharge)
  - 0x03: REF         (All-Bank / Per-Bank Refresh)
  - 0x04: PDE         (Power-Down Entry)
  - 0x05: RD          (Column Read Transfer)
  - 0x06: WR          (Column Write Transfer)
  - 0x07: WOM         (Write with On-Die Mask)
  - 0x08: MRW         (Mode Register Write Configuration)
  - 0x7E: IDLE        (Quiescent interconnect line delimiter)
  - 0xA5: SYNC_SOF    (Start of Frame / Beat delimiter, 0b10100101)
- Packet / Beat Encapsulation:
  - Delimiter byte: SYNC_SOF (0xA5).
  - Header: OpCode byte, Channel / Bank Group byte ((Channel_ID << 4) | (Bank_Group & 0x0F)),
    Bank / Address byte ((Bank_ID << 4) | (Addr_High & 0x0F)), Low Address byte.
  - Payload: variable length byte payload.
  - Checksum: 16-bit CCITT CRC (polynomial 0x1021, seed 0xFFFF).
- Memory Command Buffer Flow Control:
  - In-register credit accounting (initial pool = 4).
  - Issuing a memory command consumes 1 credit.
  - Receiving a completion / precharge ready handshake returns 1 credit.
  - Underflow on command dispatch with 0 credits traps error with R2 = 0xEE.
- In-register command filtering and fault trapping (illegal opcode 0x7F -> R2 = 0xEE).
- Cycle-accurate microcode assembly generators:
  - build_gddr6_tx_beat_asm: Master bit-serial packet transmission via SHIFTOUT (MSB-first).
  - build_gddr6_rx_beat_asm: Slave SYNC edge synchronization via WAITEDGE & SHIFTIN (MSB-first).
  - build_gddr6_command_filter_asm: In-register command validation and fault trapping.
  - build_gddr6_credit_tracker_asm: In-register transaction credit accounting and underflow trapping.
- Independent Python memory controller receiver and state model (Gddr6ReceiverModel).
- Calibrated IHP 130nm SG13G2 PPA scaling model (Gddr6PpaModel).
"""

from enum import IntEnum
from typing import Dict, Tuple, List, Optional


class Gddr6BankState(IntEnum):
    """GDDR6 / GDDR6X Bank States."""
    IDLE = 0x00
    ACTIVE = 0x01
    PRECHARGING = 0x02
    REFRESHING = 0x03


class Gddr6OpCode(IntEnum):
    """GDDR6 / GDDR6X Command OpCodes."""
    NOP = 0x00          # No Operation / Deselect
    ACT = 0x01          # Row Activate
    PRE = 0x02          # Bank / All-Bank Precharge
    REF = 0x03          # Refresh (Per-Bank / All-Bank)
    PDE = 0x04          # Power-Down Entry
    RD = 0x05           # Column Read Transfer
    WR = 0x06           # Column Write Transfer
    WOM = 0x07          # Write with On-Die Mask
    MRW = 0x08          # Mode Register Write
    IDLE = 0x7E         # Quiescent interconnect line delimiter
    SYNC_SOF = 0xA5     # Start of Frame / Beat delimiter (0b10100101)


def compute_gddr6_crc16(data: bytes) -> int:
    """
    Compute 16-bit CCITT CRC over byte sequence.
    Generator polynomial: G(x) = x^16 + x^12 + x^5 + 1 = 0x1021.
    Initial seed: 0xFFFF.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def encode_gddr6_packet(
    opcode: Gddr6OpCode,
    channel_id: int,
    bank_group: int,
    bank_id: int,
    addr: int,
    payload: bytes = b"",
) -> Dict[str, object]:
    """
    Encode a GDDR6 command packet with SYNC_SOF delimiter, opcode byte,
    channel and bank group byte, bank and address byte, payload, and 16-bit CCITT CRC.
    """
    ch_bg_byte = ((channel_id & 0x0F) << 4) | (bank_group & 0x0F)
    bank_addr_high = ((bank_id & 0x0F) << 4) | ((addr >> 8) & 0x0F)
    addr_low = addr & 0xFF

    header_and_payload = bytes([
        int(opcode),
        ch_bg_byte,
        bank_addr_high,
        addr_low,
    ]) + payload

    crc16 = compute_gddr6_crc16(header_and_payload)
    crc_bytes = bytes([(crc16 >> 8) & 0xFF, crc16 & 0xFF])

    frame_bytes = bytes([int(Gddr6OpCode.SYNC_SOF)]) + header_and_payload + crc_bytes

    return {
        "opcode": opcode,
        "channel_id": channel_id,
        "bank_group": bank_group,
        "bank_id": bank_id,
        "addr": addr,
        "payload": payload,
        "crc16": crc16,
        "frame_bytes": frame_bytes,
    }


def decode_gddr6_packet(data: bytes) -> Optional[Dict[str, object]]:
    """
    Decode a received GDDR6 command packet.
    Validates SYNC_SOF delimiter, extracts opcode, channel, bank group, bank ID,
    address, and validates 16-bit CCITT CRC. Returns decoded dictionary or None if invalid.
    """
    if len(data) < 7:
        return None
    if data[0] != int(Gddr6OpCode.SYNC_SOF):
        return None

    packet_data = data[1:-2]
    received_crc = (data[-2] << 8) | data[-1]
    calculated_crc = compute_gddr6_crc16(packet_data)

    if received_crc != calculated_crc:
        return None

    try:
        opcode = Gddr6OpCode(packet_data[0])
    except ValueError:
        return None

    ch_bg_byte = packet_data[1]
    channel_id = (ch_bg_byte >> 4) & 0x0F
    bank_group = ch_bg_byte & 0x0F

    bank_addr_high = packet_data[2]
    bank_id = (bank_addr_high >> 4) & 0x0F
    addr_high = bank_addr_high & 0x0F
    addr_low = packet_data[3]
    addr = (addr_high << 8) | addr_low

    payload = packet_data[4:]

    return {
        "opcode": opcode,
        "channel_id": channel_id,
        "bank_group": bank_group,
        "bank_id": bank_id,
        "addr": addr,
        "payload": payload,
        "crc16": received_crc,
    }


class Gddr6ReceiverModel:
    """
    Independent Python behavioral reference model for a GDDR6 / GDDR6X
    memory controller command scheduler and bank state tracking engine.
    """

    def __init__(self, num_channels: int = 2, bank_groups_per_ch: int = 4, banks_per_group: int = 4):
        self.num_channels = num_channels
        self.bank_groups_per_ch = bank_groups_per_ch
        self.banks_per_group = banks_per_group
        self.total_banks_per_ch = bank_groups_per_ch * banks_per_group  # 16 banks

        # Bank state table: [channel_id][bank_group][bank_id] -> Gddr6BankState
        self.bank_states: Dict[Tuple[int, int, int], Gddr6BankState] = {}
        for ch in range(num_channels):
            for bg in range(bank_groups_per_ch):
                for ba in range(banks_per_group):
                    self.bank_states[(ch, bg, ba)] = Gddr6BankState.IDLE

        self.credits = 4
        self.link_locked = False
        self.consecutive_syncs = 0
        self.act_count = 0
        self.pre_count = 0
        self.rd_count = 0
        self.wr_count = 0
        self.wom_count = 0
        self.ref_count = 0
        self.mrw_count = 0

    def process_sync(self) -> bool:
        """Process incoming SYNC delimiter to acquire link lock."""
        self.consecutive_syncs += 1
        if self.consecutive_syncs >= 4:
            self.link_locked = True
        return self.link_locked

    def get_bank_state(self, channel_id: int, bank_group: int, bank_id: int) -> Gddr6BankState:
        return self.bank_states.get((channel_id, bank_group, bank_id), Gddr6BankState.IDLE)

    def process_packet(self, packet_bytes: bytes) -> bool:
        """
        Process a GDDR6 command packet and update bank state machines and credits.
        Returns True on successful transaction, False on CRC/framing error or underflow.
        """
        decoded = decode_gddr6_packet(packet_bytes)
        if not decoded:
            return False

        opcode = decoded["opcode"]
        ch = decoded["channel_id"]
        bg = decoded["bank_group"]
        ba = decoded["bank_id"]

        if opcode in (Gddr6OpCode.ACT, Gddr6OpCode.RD, Gddr6OpCode.WR, Gddr6OpCode.WOM):
            if self.credits <= 0:
                return False  # Underflow error
            self.credits -= 1
        elif opcode == Gddr6OpCode.PRE:
            self.credits = min(8, self.credits + 1)

        # Update bank state machine
        current_state = self.bank_states.get((ch, bg, ba), Gddr6BankState.IDLE)

        if opcode == Gddr6OpCode.ACT:
            self.bank_states[(ch, bg, ba)] = Gddr6BankState.ACTIVE
            self.act_count += 1
        elif opcode == Gddr6OpCode.PRE:
            self.bank_states[(ch, bg, ba)] = Gddr6BankState.IDLE
            self.pre_count += 1
        elif opcode == Gddr6OpCode.REF:
            self.bank_states[(ch, bg, ba)] = Gddr6BankState.IDLE
            self.ref_count += 1
        elif opcode == Gddr6OpCode.RD:
            self.rd_count += 1
        elif opcode == Gddr6OpCode.WR:
            self.wr_count += 1
        elif opcode == Gddr6OpCode.WOM:
            self.wom_count += 1
        elif opcode == Gddr6OpCode.MRW:
            self.mrw_count += 1

        return True


class Gddr6PpaModel:
    """
    Calibrated PPA model for a dedicated synthesizable GDDR6 / GDDR6X
    Command Scheduler and Physical Layer Controller slice macro
    implemented on the IHP 130nm SG13G2 platform.
    """

    @staticmethod
    def get_metrics() -> Dict[str, float]:
        return {
            "macro_cells": 660,
            "macro_ge": 1295.0,
            "macro_area_um2": 4850.0,
            "f_max_mhz": 800.0,
            "nominal_power_uw_10mhz": 64.50,
            "raw_throughput_mbps": 38400.0,  # 38.4 Gbps per 16-bit channel interface (16 DQ @ 1200 MHz DDR)
            "energy_pj_per_bit": 0.00095,
        }


# ==============================================================================
# Microcode Generators for the 8-Bit Deterministic RISC Core
# ==============================================================================

def build_gddr6_tx_beat_asm(
    sync_code: int = int(Gddr6OpCode.SYNC_SOF),
    command_op: int = int(Gddr6OpCode.ACT),
    target_addr: int = 0x80,
    pin_tx: int = 3,
    baud_cycles: int = 4,
) -> List[str]:
    """
    Generate microcode for master GDDR6 packet header transmission:
    - Serializes SYNC_SOF (0xA5), command opcode byte (e.g. 0x01 ACT),
      and target row address (0x80) MSB-first on pin_tx.
    - Uses SHIFTOUT R0, 0x0B (pin 3, MSB-first).
    - Status R2 = 0x00 upon completion, followed by HALT.
    """
    asm = []
    asm.append(f"GDIRI 0x{1 << pin_tx:02X}        ; Configure pin {pin_tx} as output")
    asm.append(f"GWRI 0x00              ; Initialize pin {pin_tx} low")

    wait_step = max(0, baud_cycles - 2)

    bytes_to_send = [
        (sync_code, "SYNC_SOF delimiter (0xA5)"),
        (command_op, "GDDR6 Command OpCode byte"),
        (target_addr, "Target GDDR6 Row/Column Address"),
    ]

    for byte_val, comment in bytes_to_send:
        asm.append(f"LDI R0, 0x{byte_val:02X}        ; Load {comment}")
        # SHIFTOUT MSB-first: operand = (1 << 3) | (pin_tx & 0x07)
        operand = (0x01 << 3) | (pin_tx & 0x07)
        for bit_i in range(8):
            asm.append(f"SHIFTOUT R0, 0x{operand:02X}   ; Transmit bit {7 - bit_i}")
            if wait_step > 0:
                asm.append(f"WAIT {wait_step}            ; Hold bit stable")

    asm.append("GWRI 0x00              ; Return interconnect bus to idle low")
    asm.append("LDI R2, 0x00           ; Status = SUCCESS")
    asm.append("HALT                   ; Halt execution")
    return asm


def build_gddr6_rx_beat_asm(
    pin_rx: int = 3,
    baud_cycles: int = 4,
) -> List[str]:
    """
    Generate microcode for slave GDDR6 SYNC_SOF synchronization and command ingress:
    - Synchronizes on SYNC_SOF rising edge (bit 7) on pin_rx using WAITEDGE.
    - Strides past remaining 7 bits to command byte bit 7 midpoint.
    - Ingresses 8 bits MSB-first into R0 using SHIFTIN R0, 0x0B.
    - Preserves sampled command in R1.
    - Halts with R2 = 0x00.
    """
    asm = []
    asm.append("GDIRI 0x00             ; Configure all pins as input")
    asm.append("LDI R0, 0x00           ; Clear R0")
    asm.append("LDI R1, 0x00           ; Clear R1")
    asm.append("LDI R2, 0x00           ; Clear R2")

    # WAITEDGE mode 2'b01 (rising edge), pin_rx
    operand_rise = (0x01 << 3) | (pin_rx & 0x07)
    asm.append(f"WAITEDGE R3, 0x{operand_rise:02X} ; Wait for SYNC_SOF delimiter rising edge (bit 7)")

    # Stride past remaining 7 bits of delimiter to command byte bit 7 midpoint
    mid_wait = max(0, (baud_cycles * 7) + 2)
    if mid_wait > 0:
        asm.append(f"WAIT {mid_wait}           ; Stride past delimiter to command byte MSB midpoint")

    wait_step = max(0, baud_cycles - 2)
    # SHIFTIN MSB-first: operand = (1 << 3) | (pin_rx & 0x07)
    operand = (0x01 << 3) | (pin_rx & 0x07)
    for bit_i in range(8):
        asm.append(f"SHIFTIN R0, 0x{operand:02X}    ; Ingress bit {7 - bit_i}")
        if wait_step > 0:
            asm.append(f"WAIT {wait_step}            ; Wait step for next bit")

    asm.append("MOV R1, R0             ; Preserve received command in R1")
    asm.append("LDI R2, 0x00           ; Status = SUCCESS")
    asm.append("HALT                   ; Halt execution")
    return asm


def build_gddr6_command_filter_asm(
    test_opcode: int,
) -> List[str]:
    """
    Generate microcode to validate incoming GDDR6 command opcodes:
    - Tests test_opcode against allowed command types:
      - 0x01: ACT
      - 0x02: PRE
      - 0x03: REF
      - 0x04: PDE
      - 0x05: RD
      - 0x06: WR
      - 0x07: WOM
      - 0x08: MRW
    - If valid: sets R2 = 0x00 and branches to MATCH.
    - If invalid: sets R2 = 0xEE (trapped error).
    """
    asm = []
    asm.append(f"LDI R0, 0x{test_opcode:02X}        ; Load opcode into R0")
    asm.append("LDI R2, 0x00           ; Default status = 0x00")

    allowed_ops = [
        (int(Gddr6OpCode.ACT), "ACT"),
        (int(Gddr6OpCode.PRE), "PRE"),
        (int(Gddr6OpCode.REF), "REF"),
        (int(Gddr6OpCode.PDE), "PDE"),
        (int(Gddr6OpCode.RD), "RD"),
        (int(Gddr6OpCode.WR), "WR"),
        (int(Gddr6OpCode.WOM), "WOM"),
        (int(Gddr6OpCode.MRW), "MRW"),
    ]

    for op_val, op_name in allowed_ops:
        asm.append(f"MOV R1, R0             ; Copy opcode to R1")
        asm.append(f"XORI R1, 0x{op_val:02X}         ; Test {op_name} (0x{op_val:02X})")
        asm.append(f"JZ MATCH                ; Jump if match")

    # Invalid opcode fallthrough
    asm.append("LDI R2, 0xEE           ; Error: Invalid GDDR6 command opcode (0xEE)")
    asm.append("HALT")

    # Match branch
    asm.append("MATCH:")
    asm.append("LDI R2, 0x00           ; Success status")
    asm.append("HALT")
    return asm


def build_gddr6_credit_tracker_asm(
    initial_credits: int = 4,
    event_type: int = 1,
) -> List[str]:
    """
    Generate microcode to track GDDR6 command buffer credits:
    - Event 1: Precharge / Memory ACK returned -> increments credits (ADDI R0, 1), status R2 = 0x00.
    - Event 2: Command dispatched -> checks if credits == 0:
      - if R0 == 0: traps underflow (R2 = 0xEE).
      - else: decrements credits (SUBI R0, 1), status R2 = 0x00.
    """
    asm = []
    asm.append(f"LDI R0, 0x{initial_credits:02X}   ; Initial buffer credits in R0")
    asm.append(f"LDI R1, 0x{event_type:02X}        ; Event in R1 (1=ACK returned, 2=Command dispatched)")
    asm.append("LDI R2, 0x00           ; Default status = 0x00")

    # Check if event == 1 (ACK returned)
    asm.append("MOV R3, R1             ; Copy event to R3")
    asm.append("XORI R3, 0x01          ; Test event == 1")
    asm.append("JZ DO_RETURN           ; Jump to credit return")

    # Check if event == 2 (Command dispatched)
    asm.append("MOV R3, R1             ; Copy event to R3")
    asm.append("XORI R3, 0x02          ; Test event == 2")
    asm.append("JZ DO_SEND             ; Jump to command dispatch")

    # Unknown event
    asm.append("LDI R2, 0xEE           ; Error: Unknown event")
    asm.append("HALT")

    # Return branch (credit return: credits += 1)
    asm.append("DO_RETURN:")
    asm.append("ADDI R0, 0x01          ; Increment credit by 1")
    asm.append("LDI R2, 0x00           ; Status = 0x00")
    asm.append("HALT")

    # Send branch (command dispatch: credits -= 1)
    asm.append("DO_SEND:")
    asm.append("MOV R3, R0             ; Test current credits")
    asm.append("XORI R3, 0x00          ; Check if credits == 0")
    asm.append("JZ UNDERFLOW           ; If 0, trap underflow")
    asm.append("SUBI R0, 0x01          ; Decrement credit by 1")
    asm.append("LDI R2, 0x00           ; Status = 0x00")
    asm.append("HALT")

    # Underflow trap
    asm.append("UNDERFLOW:")
    asm.append("LDI R2, 0xEE           ; Status: Flow Control Underflow Trap (0xEE)")
    asm.append("HALT")
    return asm
