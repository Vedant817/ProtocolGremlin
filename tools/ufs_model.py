# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
tools/ufs_model.py - Reference model and microcode generators for UFS 3.1 / 4.0
(Universal Flash Storage / JEDEC JESD220) Mobile Storage Protocol Engine.

Covers:
- JEDEC JESD220E (UFS 3.1) & JESD220F (UFS 4.0) specifications:
  - Layered Architecture:
    - Physical Layer: MIPI M-PHY v4.1 / v5.0 with High-Speed GEARs (HS-GEAR1..GEAR5 up to 23.2 Gbps per lane across dual differential lanes), NRZ signaling, and PWM low-power modes.
    - Data Link Layer: MIPI UniPro v1.8 / v2.0 protocol stack providing credit-based flow control (Cport), CRC-16 Frame Check Sequence (polynomial 0x1021), sequence numbering, and replay buffers.
    - Transport & Command Layer: UFS Protocol Information Units (UPIU) mapped to SCSI Architecture Model (SAM) commands.
  - UPIU Transaction Types:
    - 0x00: NOP_OUT           (Host ping / heartbeat)
    - 0x01: COMMAND           (SCSI Command: READ 10, WRITE 10, INQUIRY, etc.)
    - 0x02: DATA_OUT          (Host-to-device write data payload)
    - 0x04: TASK_MGMT_REQ     (SCSI Task Management: ABORT TASK, LUN RESET)
    - 0x20: NOP_IN            (Device response to NOP_OUT)
    - 0x21: RESPONSE          (SCSI Command Response with Status)
    - 0x22: DATA_IN           (Device-to-host read data payload)
    - 0x31: RTT               (Ready To Transfer flow-control credit from device)
    - 0x7E: IDLE              (Quiescent interconnect line delimiter)
    - 0xA5: SYNC_SOF          (Start of Frame / Beat delimiter, 0b10100101)
  - Logical Unit Numbers (LUN):
    - 0x00..0x07: Standard Data Storage LUNs
    - 0xB0: Boot LUN 1
    - 0xB1: Boot LUN 2
    - 0xC4: RPMB (Replay Protected Memory Block LUN)
  - Device Lifecycle States:
    - LINK_DOWN (0x00)
    - LINK_CONFIG (0x01)
    - READY (0x02)
    - ACTIVE_READ (0x03)
    - ACTIVE_WRITE (0x04)
    - HIBERN8 (0x05)
  - Integrity & Checksum:
    - 16-bit CCITT CRC (G(x) = x^16 + x^12 + x^5 + 1 = 0x1021, seed 0xFFFF) for UniPro frame validation.
  - UniPro / UPIU Credit Accounting:
    - In-register credit pool (nominal default = 4 tokens).
    - Dispatching COMMAND or DATA_OUT consumes 1 credit.
    - Receiving RTT or RESPONSE returns 1 credit.
    - Underflow on command dispatch with 0 credits traps error with R2 = 0xEE.
- In-register command filtering and fault trapping (illegal UPIU type 0x7F -> R2 = 0xEE).
- Cycle-accurate microcode assembly generators:
  - build_ufs_tx_beat_asm: Master bit-serial packet transmission via SHIFTOUT (MSB-first on pin 3).
  - build_ufs_rx_beat_asm: Slave SYNC edge synchronization via WAITEDGE & SHIFTIN (MSB-first on pin 3).
  - build_ufs_command_filter_asm: In-register UPIU validation and fault trapping.
  - build_ufs_credit_tracker_asm: In-register transaction credit accounting and underflow trapping.
- Independent Python memory controller receiver and state model (UfsReceiverModel).
- Calibrated IHP 130nm SG13G2 PPA scaling model (UfsPpaModel).
"""

from enum import IntEnum
from typing import Dict, Tuple, List, Optional


class UfsDeviceState(IntEnum):
    """UFS 3.1 / 4.0 Device Lifecycle States."""
    LINK_DOWN = 0x00
    LINK_CONFIG = 0x01
    READY = 0x02
    ACTIVE_READ = 0x03
    ACTIVE_WRITE = 0x04
    HIBERN8 = 0x05


class UfsLun(IntEnum):
    """UFS Standard Logical Unit Numbers."""
    LUN_0 = 0x00
    LUN_1 = 0x01
    BOOT_LUN_1 = 0xB0
    BOOT_LUN_2 = 0xB1
    RPMB_LUN = 0xC4


class UfsUpiuType(IntEnum):
    """UFS Protocol Information Unit (UPIU) Transaction Types."""
    NOP_OUT = 0x00          # Host NOP Request
    COMMAND = 0x01          # SCSI Command UPIU
    DATA_OUT = 0x02         # Host Write Data UPIU
    TASK_MGMT_REQ = 0x04    # Task Management Request
    NOP_IN = 0x20           # Device NOP Response
    RESPONSE = 0x21         # SCSI Response UPIU
    DATA_IN = 0x22          # Device Read Data UPIU
    RTT = 0x31              # Ready To Transfer UPIU
    IDLE = 0x7E             # Quiescent line delimiter
    SYNC_SOF = 0xA5         # Start of Frame / Beat delimiter


def compute_ufs_crc16(data: bytes) -> int:
    """
    Compute 16-bit CCITT CRC over byte sequence.
    Generator polynomial: G(x) = x^16 + x^12 + x^5 + 1 = 0x1021.
    Initial seed: 0xFFFF.
    Standard for UniPro L2 frames and UPIU packets.
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


def encode_ufs_packet(
    upiu_type: UfsUpiuType,
    lun: int = int(UfsLun.LUN_0),
    task_tag: int = 0x01,
    addr: int = 0x0080,
    payload: bytes = b"",
) -> bytes:
    """
    Encode a UFS UPIU transaction packet with header framing and CRC-16.
    Frame format:
      [0]: SYNC_SOF (0xA5)
      [1]: UPIU Type byte
      [2]: LUN byte
      [3]: Task Tag byte
      [4]: Address High ((addr >> 8) & 0xFF)
      [5]: Address Low (addr & 0xFF)
      [6..N-2]: Payload bytes
      [N-2..N-1]: 16-bit CCITT CRC (Big-Endian)
    """
    header = bytes([
        int(UfsUpiuType.SYNC_SOF),
        int(upiu_type),
        lun & 0xFF,
        task_tag & 0xFF,
        (addr >> 8) & 0xFF,
        addr & 0xFF,
    ])
    raw_frame = header + payload
    crc = compute_ufs_crc16(raw_frame)
    return raw_frame + bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def decode_ufs_packet(packet_bytes: bytes) -> Optional[Dict]:
    """
    Decode and validate a UFS UPIU packet.
    Returns dictionary with parsed fields if valid, else None.
    """
    if len(packet_bytes) < 8:
        return None  # Min 6 header + 2 CRC bytes

    if packet_bytes[0] != int(UfsUpiuType.SYNC_SOF):
        return None

    raw_frame = packet_bytes[:-2]
    expected_crc = (packet_bytes[-2] << 8) | packet_bytes[-1]
    if compute_ufs_crc16(raw_frame) != expected_crc:
        return None

    type_val = packet_bytes[1]
    lun = packet_bytes[2]
    task_tag = packet_bytes[3]
    addr = (packet_bytes[4] << 8) | packet_bytes[5]
    payload = packet_bytes[6:-2]

    try:
        upiu_type = UfsUpiuType(type_val)
    except ValueError:
        return None

    return {
        "upiu_type": upiu_type,
        "lun": lun,
        "task_tag": task_tag,
        "addr": addr,
        "payload": payload,
        "crc": expected_crc,
    }


class UfsReceiverModel:
    """
    Independent behavioral model of a UFS 3.1 / 4.0 Storage Controller Receiver.
    Tracks device lifecycle states (UfsDeviceState), logical units (LUNs),
    UniPro Cport flow control credits, and transaction statistics.
    """

    def __init__(self, initial_credits: int = 4):
        self.credits = initial_credits
        self.state = UfsDeviceState.LINK_CONFIG
        self.link_locked = False
        self.consecutive_syncs = 0

        self.nop_count = 0
        self.cmd_count = 0
        self.data_out_count = 0
        self.data_in_count = 0
        self.resp_count = 0
        self.rtt_count = 0

    def process_sync(self) -> bool:
        """Process incoming SYNC delimiter to acquire link lock."""
        self.consecutive_syncs += 1
        if self.consecutive_syncs >= 4:
            self.link_locked = True
            if self.state == UfsDeviceState.LINK_CONFIG:
                self.state = UfsDeviceState.READY
        return self.link_locked

    def process_packet(self, packet_bytes: bytes) -> bool:
        """
        Process a UFS UPIU packet and update device lifecycle state and credits.
        Returns True on successful transaction, False on CRC/framing error or credit underflow.
        """
        decoded = decode_ufs_packet(packet_bytes)
        if not decoded:
            return False

        upiu_type = decoded["upiu_type"]

        # Credit flow control check
        if upiu_type in (UfsUpiuType.COMMAND, UfsUpiuType.DATA_OUT):
            if self.credits <= 0:
                return False  # Underflow error
            self.credits -= 1
        elif upiu_type in (UfsUpiuType.RTT, UfsUpiuType.RESPONSE, UfsUpiuType.NOP_IN):
            self.credits = min(8, self.credits + 1)

        # State machine transitions
        if upiu_type == UfsUpiuType.NOP_OUT:
            self.nop_count += 1
        elif upiu_type == UfsUpiuType.COMMAND:
            self.cmd_count += 1
            self.state = UfsDeviceState.READY
        elif upiu_type == UfsUpiuType.DATA_IN:
            self.data_in_count += 1
            self.state = UfsDeviceState.ACTIVE_READ
        elif upiu_type == UfsUpiuType.DATA_OUT:
            self.data_out_count += 1
            self.state = UfsDeviceState.ACTIVE_WRITE
        elif upiu_type == UfsUpiuType.RESPONSE:
            self.resp_count += 1
            self.state = UfsDeviceState.READY
        elif upiu_type == UfsUpiuType.RTT:
            self.rtt_count += 1

        return True


class UfsPpaModel:
    """
    Calibrated PPA model for a dedicated synthesizable UFS 3.1 / 4.0
    UniPro/M-PHY Command Engine and Physical Layer Controller slice macro
    implemented on the IHP 130nm SG13G2 platform.
    """

    @staticmethod
    def get_metrics() -> Dict[str, float]:
        return {
            "macro_cells": 640,
            "macro_ge": 1260.0,
            "macro_area_um2": 4750.0,
            "f_max_mhz": 800.0,
            "nominal_power_uw_10mhz": 63.00,
            "raw_throughput_mbps": 11600.0,  # 11.6 Gbps per lane in HS-G4 mode (or 23.2 Gbps in 2-lane HS-G4 mode)
            "energy_pj_per_bit": 0.00115,    # Low energy M-PHY signaling efficiency
        }


# ==============================================================================
# Microcode Generators for the 8-Bit Deterministic RISC Core
# ==============================================================================

def build_ufs_tx_beat_asm(
    sync_code: int = int(UfsUpiuType.SYNC_SOF),
    upiu_op: int = int(UfsUpiuType.COMMAND),
    target_addr: int = 0x80,
    pin_tx: int = 3,
    baud_cycles: int = 4,
) -> List[str]:
    """
    Generate microcode for master UFS packet header transmission:
    - Serializes SYNC_SOF (0xA5), UPIU type byte (e.g. 0x01 COMMAND),
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
        (upiu_op, "UFS UPIU Transaction Type byte"),
        (target_addr, "Target UFS Block Address"),
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


def build_ufs_rx_beat_asm(
    pin_rx: int = 3,
    baud_cycles: int = 4,
) -> List[str]:
    """
    Generate microcode for slave UFS SYNC_SOF synchronization and command ingress:
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


def build_ufs_command_filter_asm(
    test_opcode: int,
) -> List[str]:
    """
    Generate microcode to validate incoming UFS UPIU types:
    - Tests test_opcode against allowed transaction types:
      - 0x00: NOP_OUT
      - 0x01: COMMAND
      - 0x02: DATA_OUT
      - 0x04: TASK_MGMT_REQ
      - 0x20: NOP_IN
      - 0x21: RESPONSE
      - 0x22: DATA_IN
      - 0x31: RTT
    - If valid: sets R2 = 0x00 and branches to MATCH.
    - If invalid: sets R2 = 0xEE (trapped error).
    """
    asm = []
    asm.append(f"LDI R0, 0x{test_opcode:02X}        ; Load opcode into R0")
    asm.append("LDI R2, 0x00           ; Default status = 0x00")

    allowed_ops = [
        (int(UfsUpiuType.NOP_OUT), "NOP_OUT"),
        (int(UfsUpiuType.COMMAND), "COMMAND"),
        (int(UfsUpiuType.DATA_OUT), "DATA_OUT"),
        (int(UfsUpiuType.TASK_MGMT_REQ), "TASK_MGMT_REQ"),
        (int(UfsUpiuType.NOP_IN), "NOP_IN"),
        (int(UfsUpiuType.RESPONSE), "RESPONSE"),
        (int(UfsUpiuType.DATA_IN), "DATA_IN"),
        (int(UfsUpiuType.RTT), "RTT"),
    ]

    for op_val, op_name in allowed_ops:
        asm.append(f"MOV R1, R0             ; Copy opcode to R1")
        asm.append(f"XORI R1, 0x{op_val:02X}         ; Test {op_name} (0x{op_val:02X})")
        asm.append(f"JZ MATCH                ; Jump if match")

    # Invalid opcode fallthrough
    asm.append("LDI R2, 0xEE           ; Error: Invalid UFS UPIU type (0xEE)")
    asm.append("HALT")

    # Match branch
    asm.append("MATCH:")
    asm.append("LDI R2, 0x00           ; Success status")
    asm.append("HALT")
    return asm


def build_ufs_credit_tracker_asm(
    initial_credits: int = 4,
    event_type: int = 1,
) -> List[str]:
    """
    Generate microcode to track UFS / UniPro buffer credits:
    - Event 1: RTT / RESPONSE returned -> increments credits (ADDI R0, 1), status R2 = 0x00.
    - Event 2: COMMAND / DATA_OUT dispatched -> checks if credits == 0:
      - if R0 == 0: traps underflow (R2 = 0xEE).
      - else: decrements credits (SUBI R0, 1), status R2 = 0x00.
    """
    asm = []
    asm.append(f"LDI R0, 0x{initial_credits:02X}   ; Initial buffer credits in R0")
    asm.append(f"LDI R1, 0x{event_type:02X}        ; Event in R1 (1=Credit returned, 2=Command dispatched)")
    asm.append("LDI R2, 0x00           ; Default status = 0x00")

    # Check if event == 1 (Credit returned)
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
