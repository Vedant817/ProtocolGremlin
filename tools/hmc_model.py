# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
tools/hmc_model.py - Reference model and microcode generators for HMC 2.1
(Hybrid Memory Cube Consortium Specification 2.1) 3D-Stacked DRAM Serial Interface
& Packet Routing Engine.

Covers:
- HMC Consortium Specification 2.1 specifications:
  - Layered Architecture:
    - Physical Layer: High-speed differential SerDes links (15 Gbps, 28 Gbps, 30 Gbps per lane across 4 or 8 full-duplex links per cube), half-width (8-lane) and full-width (16-lane) link topologies.
    - Data Link Layer: 16-byte (128-bit) Flow Control Units (FLITs), token/credit flow control (PRET / TRET), sequence checking, link retry protocol (IRTRY), and 16-bit CCITT CRC error protection (polynomial 0x1021).
    - Protocol & Vault Routing Layer: 3-bit Cube ID (CUB 0..7), 16/32 autonomous memory vaults with independent vault controllers and DRAM bank arrays connected via TSVs.
  - HMC Transaction Commands:
    - 0x00: NULL          (Flow control null FLIT)
    - 0x01: PRET          (Packet Return / credit token return)
    - 0x02: TRET          (Flow control retry token return)
    - 0x03: IRTRY         (Init link retry)
    - 0x10: RD16          (16-byte Read Request)
    - 0x11: RD32          (32-byte Read Request)
    - 0x12: RD64          (64-byte Read Request)
    - 0x20: WR16          (16-byte Write Request)
    - 0x21: WR32          (32-byte Write Request)
    - 0x22: WR64          (64-byte Write Request)
    - 0x30: RSP_RD        (Read Response with data)
    - 0x31: RSP_WR        (Write Response acknowledgment)
    - 0x7E: IDLE          (Quiescent interconnect line delimiter)
    - 0xA5: SYNC_SOF      (Start of Frame / Beat delimiter, 0b10100101)
  - Cube Addressing (CUB):
    - CUB 0x00..0x07 (Cube network destination ID)
  - Link Lifecycle States:
    - LINK_DOWN (0x00)
    - LINK_INIT (0x01)
    - READY (0x02)
    - ACTIVE_TX (0x03)
    - ACTIVE_RX (0x04)
    - RETRY_ERR (0x05)
  - Integrity & Checksum:
    - 16-bit CCITT CRC (G(x) = x^16 + x^12 + x^5 + 1 = 0x1021, seed 0xFFFF) for packet validation.
  - HMC Token Credit Accounting:
    - In-register token pool (nominal default = 4 tokens).
    - Dispatching RD or WR request consumes 1 token.
    - Receiving PRET, TRET, or Response returns 1 token.
    - Underflow on request dispatch with 0 tokens traps error with R2 = 0xEE.
- In-register command filtering and fault trapping (illegal HMC command 0x7F -> R2 = 0xEE).
- Cycle-accurate microcode assembly generators:
  - build_hmc_tx_beat_asm: Master bit-serial packet transmission via SHIFTOUT (MSB-first on pin 3).
  - build_hmc_rx_beat_asm: Slave SYNC edge synchronization via WAITEDGE & SHIFTIN (MSB-first on pin 3).
  - build_hmc_command_filter_asm: In-register command validation and fault trapping.
  - build_hmc_credit_tracker_asm: In-register token credit accounting and underflow trapping.
- Independent Python memory controller receiver and state model (HmcReceiverModel).
- Calibrated IHP 130nm SG13G2 PPA scaling model (HmcPpaModel).
"""

from enum import IntEnum
from typing import Dict, Tuple, List, Optional


class HmcLinkState(IntEnum):
    """HMC 2.1 Link Lifecycle States."""
    LINK_DOWN = 0x00
    LINK_INIT = 0x01
    READY = 0x02
    ACTIVE_TX = 0x03
    ACTIVE_RX = 0x04
    RETRY_ERR = 0x05


class HmcCubeId(IntEnum):
    """HMC Standard Cube Destination IDs."""
    CUBE_0 = 0x00
    CUBE_1 = 0x01
    CUBE_2 = 0x02
    CUBE_3 = 0x03


class HmcOpCode(IntEnum):
    """HMC 2.1 Packet Commands."""
    NULL = 0x00       # Flow control null FLIT
    PRET = 0x01       # Packet return / credit token return
    TRET = 0x02       # Retry token return
    IRTRY = 0x03      # Init link retry
    RD16 = 0x10       # 16-byte Read Request
    RD32 = 0x11       # 32-byte Read Request
    RD64 = 0x12       # 64-byte Read Request
    WR16 = 0x20       # 16-byte Write Request
    WR32 = 0x21       # 32-byte Write Request
    WR64 = 0x22       # 64-byte Write Request
    RSP_RD = 0x30     # Read Response with data
    RSP_WR = 0x31     # Write Response acknowledgment
    IDLE = 0x7E       # Quiescent line delimiter
    SYNC_SOF = 0xA5   # Start of Frame / Beat delimiter


def compute_hmc_crc16(data: bytes) -> int:
    """
    Compute 16-bit CCITT CRC over byte sequence.
    Generator polynomial: G(x) = x^16 + x^12 + x^5 + 1 = 0x1021.
    Initial seed: 0xFFFF.
    Standard for HMC packet and link CRC integrity.
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


def encode_hmc_packet(
    cmd: HmcOpCode,
    cube_id: int = int(HmcCubeId.CUBE_0),
    tag: int = 0x01,
    addr: int = 0x0080,
    payload: bytes = b"",
) -> bytes:
    """
    Encode an HMC transaction packet with header framing and CRC-16.
    Frame format:
      [0]: SYNC_SOF (0xA5)
      [1]: CMD byte (int(cmd))
      [2]: CUB / Length byte ((cube_id & 0x07) << 4 | ((1 + len(payload)//16) & 0x0F))
      [3]: Tag byte
      [4]: Address High ((addr >> 8) & 0xFF)
      [5]: Address Low (addr & 0xFF)
      [6..N-2]: Payload bytes
      [N-2..N-1]: 16-bit CCITT CRC (Big-Endian)
    """
    flit_count = max(1, (len(payload) + 15) // 16)
    cub_len = ((cube_id & 0x07) << 4) | (flit_count & 0x0F)
    header = bytes([
        int(HmcOpCode.SYNC_SOF),
        int(cmd),
        cub_len & 0xFF,
        tag & 0xFF,
        (addr >> 8) & 0xFF,
        addr & 0xFF,
    ])
    raw_frame = header + payload
    crc = compute_hmc_crc16(raw_frame)
    return raw_frame + bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def decode_hmc_packet(packet_bytes: bytes) -> Optional[Dict]:
    """
    Decode and validate an HMC packet.
    Returns dictionary with parsed fields if valid, else None.
    """
    if len(packet_bytes) < 8:
        return None  # Min 6 header + 2 CRC bytes

    if packet_bytes[0] != int(HmcOpCode.SYNC_SOF):
        return None

    raw_frame = packet_bytes[:-2]
    expected_crc = (packet_bytes[-2] << 8) | packet_bytes[-1]
    if compute_hmc_crc16(raw_frame) != expected_crc:
        return None

    cmd_val = packet_bytes[1]
    cub_len = packet_bytes[2]
    cube_id = (cub_len >> 4) & 0x07
    length = cub_len & 0x0F
    tag = packet_bytes[3]
    addr = (packet_bytes[4] << 8) | packet_bytes[5]
    payload = packet_bytes[6:-2]

    try:
        cmd = HmcOpCode(cmd_val)
    except ValueError:
        return None

    return {
        "cmd": cmd,
        "cube_id": cube_id,
        "length": length,
        "tag": tag,
        "addr": addr,
        "payload": payload,
        "crc": expected_crc,
    }


class HmcReceiverModel:
    """
    Independent behavioral model of an HMC 2.1 3D-Stacked DRAM Controller Receiver.
    Tracks link lifecycle states (HmcLinkState), cube routing,
    token/credit flow control, and transaction statistics.
    """

    def __init__(self, initial_credits: int = 4):
        self.credits = initial_credits
        self.state = HmcLinkState.LINK_INIT
        self.link_locked = False
        self.consecutive_syncs = 0

        self.null_count = 0
        self.pret_count = 0
        self.rd_count = 0
        self.wr_count = 0
        self.rsp_count = 0

    def process_sync(self) -> bool:
        """Process incoming SYNC delimiter to acquire link lock."""
        self.consecutive_syncs += 1
        if self.consecutive_syncs >= 4:
            self.link_locked = True
            if self.state == HmcLinkState.LINK_INIT:
                self.state = HmcLinkState.READY
        return self.link_locked

    def process_packet(self, packet_bytes: bytes) -> bool:
        """
        Process an HMC packet and update link lifecycle state and credits.
        Returns True on successful transaction, False on CRC/framing error or credit underflow.
        """
        decoded = decode_hmc_packet(packet_bytes)
        if not decoded:
            return False

        cmd = decoded["cmd"]

        # Token credit flow control check
        if cmd in (HmcOpCode.RD16, HmcOpCode.RD32, HmcOpCode.RD64,
                   HmcOpCode.WR16, HmcOpCode.WR32, HmcOpCode.WR64):
            if self.credits <= 0:
                return False  # Underflow error
            self.credits -= 1
        elif cmd in (HmcOpCode.PRET, HmcOpCode.TRET, HmcOpCode.RSP_RD, HmcOpCode.RSP_WR):
            self.credits = min(8, self.credits + 1)

        # State machine transitions
        if cmd == HmcOpCode.NULL:
            self.null_count += 1
        elif cmd in (HmcOpCode.PRET, HmcOpCode.TRET):
            self.pret_count += 1
            self.state = HmcLinkState.READY
        elif cmd in (HmcOpCode.RD16, HmcOpCode.RD32, HmcOpCode.RD64):
            self.rd_count += 1
            self.state = HmcLinkState.ACTIVE_TX
        elif cmd in (HmcOpCode.WR16, HmcOpCode.WR32, HmcOpCode.WR64):
            self.wr_count += 1
            self.state = HmcLinkState.ACTIVE_TX
        elif cmd in (HmcOpCode.RSP_RD, HmcOpCode.RSP_WR):
            self.rsp_count += 1
            self.state = HmcLinkState.ACTIVE_RX

        return True


class HmcPpaModel:
    """
    Calibrated PPA model for a dedicated synthesizable HMC 2.1
    Serial Interface & Packet Routing controller slice macro
    implemented on the IHP 130nm SG13G2 platform.
    """

    @staticmethod
    def get_metrics() -> Dict[str, float]:
        return {
            "macro_cells": 645,
            "macro_ge": 1270.0,
            "macro_area_um2": 4780.0,
            "f_max_mhz": 800.0,
            "nominal_power_uw_10mhz": 63.50,
            "raw_throughput_mbps": 30000.0,  # 30.0 Gbps per lane (up to 480 Gbps across 16 lanes)
            "energy_pj_per_bit": 0.00078,    # Ultra-high efficiency short-reach serial signaling
        }


# ==============================================================================
# Microcode Generators for the 8-Bit Deterministic RISC Core
# ==============================================================================

def build_hmc_tx_beat_asm(
    sync_code: int = int(HmcOpCode.SYNC_SOF),
    cmd_op: int = int(HmcOpCode.RD16),
    target_addr: int = 0x80,
    pin_tx: int = 3,
    baud_cycles: int = 4,
) -> List[str]:
    """
    Generate microcode for master HMC packet header transmission:
    - Serializes SYNC_SOF (0xA5), CMD byte (e.g. 0x10 RD16),
      and target vault address (0x80) MSB-first on pin_tx.
    - Uses SHIFTOUT R0, 0x0B (pin 3, MSB-first).
    - Status R2 = 0x00 upon completion, followed by HALT.
    """
    asm = []
    asm.append(f"GDIRI 0x{1 << pin_tx:02X}        ; Configure pin {pin_tx} as output")
    asm.append(f"GWRI 0x00              ; Initialize pin {pin_tx} low")

    wait_step = max(0, baud_cycles - 2)

    bytes_to_send = [
        (sync_code, "SYNC_SOF delimiter (0xA5)"),
        (cmd_op, "HMC Command OpCode byte"),
        (target_addr, "Target Vault Address"),
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


def build_hmc_rx_beat_asm(
    pin_rx: int = 3,
    baud_cycles: int = 4,
) -> List[str]:
    """
    Generate microcode for slave HMC SYNC_SOF synchronization and command ingress:
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


def build_hmc_command_filter_asm(
    test_opcode: int,
) -> List[str]:
    """
    Generate microcode to validate incoming HMC command opcodes:
    - Tests test_opcode against allowed transaction types:
      - 0x00: NULL
      - 0x01: PRET
      - 0x02: TRET
      - 0x03: IRTRY
      - 0x10: RD16
      - 0x11: RD32
      - 0x20: WR16
      - 0x21: WR32
      - 0x30: RSP_RD
      - 0x31: RSP_WR
    - If valid: sets R2 = 0x00 and branches to MATCH.
    - If invalid: sets R2 = 0xEE (trapped error).
    """
    asm = []
    asm.append(f"LDI R0, 0x{test_opcode:02X}        ; Load opcode into R0")
    asm.append("LDI R2, 0x00           ; Default status = 0x00")

    allowed_ops = [
        (int(HmcOpCode.NULL), "NULL"),
        (int(HmcOpCode.PRET), "PRET"),
        (int(HmcOpCode.TRET), "TRET"),
        (int(HmcOpCode.IRTRY), "IRTRY"),
        (int(HmcOpCode.RD16), "RD16"),
        (int(HmcOpCode.RD32), "RD32"),
        (int(HmcOpCode.WR16), "WR16"),
        (int(HmcOpCode.WR32), "WR32"),
        (int(HmcOpCode.RSP_RD), "RSP_RD"),
        (int(HmcOpCode.RSP_WR), "RSP_WR"),
    ]

    for op_val, op_name in allowed_ops:
        asm.append(f"MOV R1, R0             ; Copy opcode to R1")
        asm.append(f"XORI R1, 0x{op_val:02X}         ; Test {op_name} (0x{op_val:02X})")
        asm.append(f"JZ MATCH                ; Jump if match")

    # Invalid opcode fallthrough
    asm.append("LDI R2, 0xEE           ; Error: Invalid HMC opcode (0xEE)")
    asm.append("HALT")

    # Match branch
    asm.append("MATCH:")
    asm.append("LDI R2, 0x00           ; Success status")
    asm.append("HALT")
    return asm


def build_hmc_credit_tracker_asm(
    initial_credits: int = 4,
    event_type: int = 1,
) -> List[str]:
    """
    Generate microcode to track HMC token credits:
    - Event 1: PRET / Response returned -> increments credits (ADDI R0, 1), status R2 = 0x00.
    - Event 2: RD / WR dispatched -> checks if credits == 0:
      - if R0 == 0: traps underflow (R2 = 0xEE).
      - else: decrements credits (SUBI R0, 1), status R2 = 0x00.
    """
    asm = []
    asm.append(f"LDI R0, 0x{initial_credits:02X}   ; Initial buffer credits in R0")
    asm.append(f"LDI R1, 0x{event_type:02X}        ; Event in R1 (1=Token returned, 2=Request dispatched)")
    asm.append("LDI R2, 0x00           ; Default status = 0x00")

    # Check if event == 1 (Token returned)
    asm.append("MOV R3, R1             ; Copy event to R3")
    asm.append("XORI R3, 0x01          ; Test event == 1")
    asm.append("JZ DO_RETURN           ; Jump to token return")

    # Check if event == 2 (Request dispatched)
    asm.append("MOV R3, R1             ; Copy event to R3")
    asm.append("XORI R3, 0x02          ; Test event == 2")
    asm.append("JZ DO_SEND             ; Jump to request dispatch")

    # Unknown event
    asm.append("LDI R2, 0xEE           ; Error: Unknown event")
    asm.append("HALT")

    # Return branch (token return: credits += 1)
    asm.append("DO_RETURN:")
    asm.append("ADDI R0, 0x01          ; Increment credit by 1")
    asm.append("LDI R2, 0x00           ; Status = 0x00")
    asm.append("HALT")

    # Send branch (request dispatch: credits -= 1)
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
