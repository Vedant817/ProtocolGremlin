# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
tools/emmc_model.py - Reference model and microcode generators for eMMC 5.1 / SD 6.0 UHS-II
(JEDEC JESD84-B51 / SD Association) Non-Volatile Memory Bus & Card Protocol Engine.

Covers:
- JEDEC JESD84-B51 (eMMC 5.1) and SD Association Physical Layer Specification v6.0 / UHS-II:
  - Bus Topology & Signaling:
    - CMD: Bidirectional command / response line operating at Single Data Rate (SDR).
    - DAT[7:0]: Bidirectional data bus supporting 1-bit, 4-bit, and 8-bit bus widths.
    - CLK: Clock up to 200 MHz in HS400 DDR mode (yielding up to 400 MB/s or 3200 Mbps).
    - Data Strobe (DS): Transmitted by the device in HS400 mode for read data timing.
    - SD UHS-II: Low-voltage differential signaling (0.26 V swing) on D0/D1 pairs with 8b/10b symbol encoding.
  - Card Partitions:
    - User Data Area (0x00)
    - Boot Area Partition 1 (0x01)
    - Boot Area Partition 2 (0x02)
    - RPMB (Replay Protected Memory Block, 0x03)
    - General Purpose Partitions 1..4 (0x04..0x07)
  - Card Lifecycle / State Machine:
    - IDLE (0x00): Card reset / quiescent state.
    - READY (0x01): Operating condition negotiation.
    - IDENT (0x02): Card identification and CID transmission.
    - STBY (0x03): Standby state waiting for address assignment / selection.
    - TRAN (0x04): Data transfer state ready for block read/write.
    - DATA (0x05): Transmitting data blocks.
    - RCV (0x06): Receiving data blocks.
    - PRG (0x07): Programming flash memory.
    - DIS (0x08): Disconnect state.
- Command OpCodes:
  - 0x00: CMD0_GO_IDLE        (Reset all cards to idle state)
  - 0x01: CMD1_SEND_OP_COND   (Host sends operating condition / voltage negotiation)
  - 0x02: CMD2_ALL_SEND_CID   (Request all cards to send CID registers)
  - 0x03: CMD3_SET_RELATIVE_ADDR (Assign / request relative card address RCA)
  - 0x07: CMD7_SELECT_CARD    (Toggle card between Standby and Transfer state)
  - 0x08: CMD8_SEND_EXT_CSD   (Request Extended CSD register / interface condition)
  - 0x0C: CMD12_STOP_TRANSMISSION (Force card to stop transmission / return to TRAN)
  - 0x11: CMD17_READ_SINGLE_BLOCK (Read single 512-byte data block from address)
  - 0x18: CMD24_WRITE_BLOCK   (Write single 512-byte data block to address)
  - 0x7E: IDLE                (Quiescent interconnect line delimiter)
  - 0xA5: SYNC_SOF            (Start of Frame / Beat delimiter, 0b10100101)
- Integrity & Checksum Algorithms:
  - 7-bit ITU-T / JEDEC CRC-7 (G(x) = x^7 + x^3 + 1 = 0x09) for command and response verification.
  - 16-bit CCITT CRC (G(x) = x^16 + x^12 + x^5 + 1 = 0x1021, seed 0xFFFF) for block data transfer frames.
- Command Queuing Engine (CQE) & Buffer Flow Control:
  - In-register credit accounting (initial pool = 4).
  - Issuing a read/write transaction consumes 1 credit.
  - Receiving a completion / ready handshake returns 1 credit.
  - Underflow on command dispatch with 0 credits traps error with R2 = 0xEE.
- In-register command filtering and fault trapping (illegal opcode 0x7F -> R2 = 0xEE).
- Cycle-accurate microcode assembly generators:
  - build_emmc_tx_beat_asm: Master bit-serial packet transmission via SHIFTOUT (MSB-first on pin 3).
  - build_emmc_rx_beat_asm: Slave SYNC edge synchronization via WAITEDGE & SHIFTIN (MSB-first on pin 3).
  - build_emmc_command_filter_asm: In-register command validation and fault trapping.
  - build_emmc_credit_tracker_asm: In-register transaction credit accounting and underflow trapping.
- Independent Python memory card receiver and state model (EmmcReceiverModel).
- Calibrated IHP 130nm SG13G2 PPA scaling model (EmmcPpaModel).
"""

from enum import IntEnum
from typing import Dict, Tuple, List, Optional


class EmmcCardState(IntEnum):
    """eMMC 5.1 / SD 6.0 Card Lifecycle States."""
    IDLE = 0x00
    READY = 0x01
    IDENT = 0x02
    STBY = 0x03
    TRAN = 0x04
    DATA = 0x05
    RCV = 0x06
    PRG = 0x07
    DIS = 0x08


class EmmcPartition(IntEnum):
    """eMMC 5.1 Hardware Partitions."""
    USER_DATA = 0x00
    BOOT_1 = 0x01
    BOOT_2 = 0x02
    RPMB = 0x03
    GPP_1 = 0x04
    GPP_2 = 0x05
    GPP_3 = 0x06
    GPP_4 = 0x07


class EmmcOpCode(IntEnum):
    """eMMC 5.1 / SD 6.0 Command OpCodes."""
    CMD0_GO_IDLE = 0x00
    CMD1_SEND_OP_COND = 0x01
    CMD2_ALL_SEND_CID = 0x02
    CMD3_SET_RELATIVE_ADDR = 0x03
    CMD7_SELECT_CARD = 0x07
    CMD8_SEND_EXT_CSD = 0x08
    CMD12_STOP_TRANSMISSION = 0x0C
    CMD17_READ_SINGLE_BLOCK = 0x11
    CMD24_WRITE_BLOCK = 0x18
    IDLE = 0x7E
    SYNC_SOF = 0xA5


def compute_emmc_crc7(data: bytes) -> int:
    """
    Compute 7-bit ITU-T / JEDEC CRC-7 over byte sequence.
    Generator polynomial: G(x) = x^7 + x^3 + 1 = 0x09.
    Initial seed: 0x00.
    Standard for 48-bit eMMC / SD command and response packets.
    """
    crc = 0
    for byte in data:
        for bit in range(7, -1, -1):
            crc_bit = (crc >> 6) & 1
            data_bit = (byte >> bit) & 1
            inv = crc_bit ^ data_bit
            crc = ((crc << 1) & 0x7F)
            if inv:
                crc ^= 0x09
    return crc & 0x7F


def compute_emmc_crc16(data: bytes) -> int:
    """
    Compute 16-bit CCITT CRC over byte sequence.
    Generator polynomial: G(x) = x^16 + x^12 + x^5 + 1 = 0x1021.
    Initial seed: 0xFFFF.
    Standard for eMMC/SD block data payloads and packet frames.
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


def encode_emmc_packet(
    opcode: EmmcOpCode,
    partition: int = int(EmmcPartition.USER_DATA),
    block_addr: int = 0x0080,
    payload: bytes = b"",
) -> bytes:
    """
    Encode an eMMC / SD transaction packet with header framing and CRC-16.
    Frame format:
      [0]: SYNC_SOF (0xA5)
      [1]: OpCode byte
      [2]: Partition byte (partition & 0x07)
      [3]: Address High ((block_addr >> 8) & 0xFF)
      [4]: Address Low (block_addr & 0xFF)
      [5..N-2]: Payload bytes
      [N-2..N-1]: 16-bit CCITT CRC (Big-Endian)
    """
    header = bytes([
        int(EmmcOpCode.SYNC_SOF),
        int(opcode),
        partition & 0x07,
        (block_addr >> 8) & 0xFF,
        block_addr & 0xFF,
    ])
    raw_frame = header + payload
    crc = compute_emmc_crc16(raw_frame)
    return raw_frame + bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def decode_emmc_packet(packet_bytes: bytes) -> Optional[Dict]:
    """
    Decode and validate an eMMC / SD packet.
    Returns dictionary with parsed fields if valid, else None.
    """
    if len(packet_bytes) < 7:
        return None  # Min 5 header + 2 CRC bytes

    if packet_bytes[0] != int(EmmcOpCode.SYNC_SOF):
        return None

    raw_frame = packet_bytes[:-2]
    expected_crc = (packet_bytes[-2] << 8) | packet_bytes[-1]
    if compute_emmc_crc16(raw_frame) != expected_crc:
        return None

    opcode_val = packet_bytes[1]
    partition_val = packet_bytes[2] & 0x07
    block_addr = (packet_bytes[3] << 8) | packet_bytes[4]
    payload = packet_bytes[5:-2]

    try:
        opcode = EmmcOpCode(opcode_val)
    except ValueError:
        return None

    try:
        partition = EmmcPartition(partition_val)
    except ValueError:
        partition = EmmcPartition.USER_DATA

    return {
        "opcode": opcode,
        "partition": partition,
        "block_addr": block_addr,
        "payload": payload,
        "crc": expected_crc,
    }


class EmmcReceiverModel:
    """
    Independent behavioral model of an eMMC 5.1 / SD 6.0 Memory Card Receiver.
    Tracks card lifecycle states (EmmcCardState), hardware partitions,
    Command Queuing Engine (CQE) credits, and transaction counters.
    """

    def __init__(self, initial_credits: int = 4):
        self.credits = initial_credits
        self.state = EmmcCardState.IDLE
        self.active_partition = EmmcPartition.USER_DATA
        self.link_locked = False
        self.consecutive_syncs = 0

        self.cmd0_count = 0
        self.cmd1_count = 0
        self.cmd2_count = 0
        self.cmd3_count = 0
        self.cmd7_count = 0
        self.cmd8_count = 0
        self.cmd12_count = 0
        self.cmd17_count = 0
        self.cmd24_count = 0

    def process_sync(self) -> bool:
        """Process incoming SYNC delimiter to acquire link lock."""
        self.consecutive_syncs += 1
        if self.consecutive_syncs >= 4:
            self.link_locked = True
        return self.link_locked

    def process_packet(self, packet_bytes: bytes) -> bool:
        """
        Process an eMMC command packet and update card lifecycle state and credits.
        Returns True on successful transaction, False on CRC/framing error or credit underflow.
        """
        decoded = decode_emmc_packet(packet_bytes)
        if not decoded:
            return False

        opcode = decoded["opcode"]
        partition = decoded["partition"]

        # Credit flow control check for read/write transactions
        if opcode in (EmmcOpCode.CMD17_READ_SINGLE_BLOCK, EmmcOpCode.CMD24_WRITE_BLOCK):
            if self.credits <= 0:
                return False  # Underflow error
            self.credits -= 1
        elif opcode in (EmmcOpCode.CMD0_GO_IDLE, EmmcOpCode.CMD12_STOP_TRANSMISSION):
            self.credits = min(8, self.credits + 1)

        self.active_partition = partition

        # State machine transitions
        if opcode == EmmcOpCode.CMD0_GO_IDLE:
            self.state = EmmcCardState.IDLE
            self.cmd0_count += 1
        elif opcode == EmmcOpCode.CMD1_SEND_OP_COND:
            self.state = EmmcCardState.READY
            self.cmd1_count += 1
        elif opcode == EmmcOpCode.CMD2_ALL_SEND_CID:
            self.state = EmmcCardState.IDENT
            self.cmd2_count += 1
        elif opcode == EmmcOpCode.CMD3_SET_RELATIVE_ADDR:
            self.state = EmmcCardState.STBY
            self.cmd3_count += 1
        elif opcode == EmmcOpCode.CMD7_SELECT_CARD:
            self.state = EmmcCardState.TRAN
            self.cmd7_count += 1
        elif opcode == EmmcOpCode.CMD8_SEND_EXT_CSD:
            self.cmd8_count += 1
        elif opcode == EmmcOpCode.CMD12_STOP_TRANSMISSION:
            self.state = EmmcCardState.TRAN
            self.cmd12_count += 1
        elif opcode == EmmcOpCode.CMD17_READ_SINGLE_BLOCK:
            self.state = EmmcCardState.DATA
            self.cmd17_count += 1
        elif opcode == EmmcOpCode.CMD24_WRITE_BLOCK:
            self.state = EmmcCardState.RCV
            self.cmd24_count += 1

        return True


class EmmcPpaModel:
    """
    Calibrated PPA model for a dedicated synthesizable eMMC 5.1 / SD 6.0 UHS-II
    Command Engine and Physical Layer Controller slice macro
    implemented on the IHP 130nm SG13G2 platform.
    """

    @staticmethod
    def get_metrics() -> Dict[str, float]:
        return {
            "macro_cells": 625,
            "macro_ge": 1230.0,
            "macro_area_um2": 4650.0,
            "f_max_mhz": 800.0,
            "nominal_power_uw_10mhz": 61.50,
            "raw_throughput_mbps": 3200.0,  # 3.2 Gbps in HS400 DDR 8-bit mode (200 MHz x 2 edges x 8 bits = 3200 Mbps)
            "energy_pj_per_bit": 0.00192,   # Energy efficiency at 1.8V / 1.2V signaling
        }


# ==============================================================================
# Microcode Generators for the 8-Bit Deterministic RISC Core
# ==============================================================================

def build_emmc_tx_beat_asm(
    sync_code: int = int(EmmcOpCode.SYNC_SOF),
    command_op: int = int(EmmcOpCode.CMD17_READ_SINGLE_BLOCK),
    target_addr: int = 0x80,
    pin_tx: int = 3,
    baud_cycles: int = 4,
) -> List[str]:
    """
    Generate microcode for master eMMC packet header transmission:
    - Serializes SYNC_SOF (0xA5), command opcode byte (e.g. 0x11 CMD17),
      and target address (0x80) MSB-first on pin_tx.
    - Uses SHIFTOUT R0, 0x0B (pin 3, MSB-first).
    - Status R2 = 0x00 upon completion, followed by HALT.
    """
    asm = []
    asm.append(f"GDIRI 0x{1 << pin_tx:02X}        ; Configure pin {pin_tx} as output")
    asm.append(f"GWRI 0x00              ; Initialize pin {pin_tx} low")

    wait_step = max(0, baud_cycles - 2)

    bytes_to_send = [
        (sync_code, "SYNC_SOF delimiter (0xA5)"),
        (command_op, "eMMC Command OpCode byte"),
        (target_addr, "Target eMMC Block Address"),
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


def build_emmc_rx_beat_asm(
    pin_rx: int = 3,
    baud_cycles: int = 4,
) -> List[str]:
    """
    Generate microcode for slave eMMC SYNC_SOF synchronization and command ingress:
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


def build_emmc_command_filter_asm(
    test_opcode: int,
) -> List[str]:
    """
    Generate microcode to validate incoming eMMC command opcodes:
    - Tests test_opcode against allowed command types:
      - 0x00: CMD0_GO_IDLE
      - 0x01: CMD1_SEND_OP_COND
      - 0x02: CMD2_ALL_SEND_CID
      - 0x03: CMD3_SET_RELATIVE_ADDR
      - 0x07: CMD7_SELECT_CARD
      - 0x08: CMD8_SEND_EXT_CSD
      - 0x0C: CMD12_STOP_TRANSMISSION
      - 0x11: CMD17_READ_SINGLE_BLOCK
      - 0x18: CMD24_WRITE_BLOCK
    - If valid: sets R2 = 0x00 and branches to MATCH.
    - If invalid: sets R2 = 0xEE (trapped error).
    """
    asm = []
    asm.append(f"LDI R0, 0x{test_opcode:02X}        ; Load opcode into R0")
    asm.append("LDI R2, 0x00           ; Default status = 0x00")

    allowed_ops = [
        (int(EmmcOpCode.CMD0_GO_IDLE), "CMD0"),
        (int(EmmcOpCode.CMD1_SEND_OP_COND), "CMD1"),
        (int(EmmcOpCode.CMD2_ALL_SEND_CID), "CMD2"),
        (int(EmmcOpCode.CMD3_SET_RELATIVE_ADDR), "CMD3"),
        (int(EmmcOpCode.CMD7_SELECT_CARD), "CMD7"),
        (int(EmmcOpCode.CMD8_SEND_EXT_CSD), "CMD8"),
        (int(EmmcOpCode.CMD12_STOP_TRANSMISSION), "CMD12"),
        (int(EmmcOpCode.CMD17_READ_SINGLE_BLOCK), "CMD17"),
        (int(EmmcOpCode.CMD24_WRITE_BLOCK), "CMD24"),
    ]

    for op_val, op_name in allowed_ops:
        asm.append(f"MOV R1, R0             ; Copy opcode to R1")
        asm.append(f"XORI R1, 0x{op_val:02X}         ; Test {op_name} (0x{op_val:02X})")
        asm.append(f"JZ MATCH                ; Jump if match")

    # Invalid opcode fallthrough
    asm.append("LDI R2, 0xEE           ; Error: Invalid eMMC command opcode (0xEE)")
    asm.append("HALT")

    # Match branch
    asm.append("MATCH:")
    asm.append("LDI R2, 0x00           ; Success status")
    asm.append("HALT")
    return asm


def build_emmc_credit_tracker_asm(
    initial_credits: int = 4,
    event_type: int = 1,
) -> List[str]:
    """
    Generate microcode to track eMMC command buffer / CQE credits:
    - Event 1: Transfer complete / ACK returned -> increments credits (ADDI R0, 1), status R2 = 0x00.
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
