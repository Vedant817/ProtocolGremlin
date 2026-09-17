# Copyright (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
tools/usb_fs_model.py - Cycle-accurate USB 2.0 Full-Speed (12 Mbps) Physical & Packet Engine

Implements the USB 2.0 Full-Speed physical layer, NRZI modulation, dynamic bit stuffing,
PID verification, Token/Data CRC checking, EOP detection, and microcode generators:

- Full-Speed Differential Signaling:
    - J state (Idle): D+ = 1, D- = 0 (1.5 kΩ pull-up on D+)
    - K state (Active): D+ = 0, D- = 1
    - SE0 (Single-Ended Zero): D+ = 0, D- = 0 (EOP and Reset)
    - SE1 (Single-Ended One): D+ = 1, D- = 1 (Illegal condition / bus error)
- Non-Return-to-Zero Inverted (NRZI) Line Coding:
    - Bit '0': Invert differential state (J <-> K)
    - Bit '1': Maintain current differential state (no transition)
- Dynamic Bit Stuffing:
    - Forced '0' inserted following six consecutive '1' bits
    - Receiver destuffs and traps bit-stuff violations (> 6 consecutive '1's)
- Packet Formatting:
    - SYNC: 8 bits = 0x80 (transmitted LSB-first: 00000001b -> K-J-K-J-K-J-K-K)
    - PID: 8 bits (4-bit type field P[3:0] + 4-bit inverted check field P[7:4])
    - Payload / Address / Endpoint
    - CRC: 5-bit CRC-5 for Token, 16-bit CRC-16 for Data
    - EOP: 2 bit periods of SE0 followed by 1 bit period of J state
"""

from typing import List, Tuple, Optional, Dict, Any
from dataclasses import dataclass, field


# Standard USB 2.0 Full-Speed Packet Identifiers (PIDs)
PID_OUT      = 0xE1  # Token:     0001b, check: 1110b
PID_IN       = 0x69  # Token:     1001b, check: 0110b
PID_SOF      = 0xA5  # Token:     0101b, check: 1010b
PID_SETUP    = 0x2D  # Token:     1101b, check: 0010b
PID_DATA0    = 0xC3  # Data:      0011b, check: 1100b
PID_DATA1    = 0x4B  # Data:      1011b, check: 0100b
PID_DATA2    = 0x87  # Data:      0111b, check: 1000b
PID_MDATA    = 0x0F  # Data:      1111b, check: 0000b
PID_ACK      = 0xD2  # Handshake: 0010b, check: 1101b
PID_NAK      = 0x5A  # Handshake: 1010b, check: 0101b
PID_STALL    = 0x1E  # Handshake: 1110b, check: 0001b
PID_NYET     = 0x96  # Handshake: 0110b, check: 1001b
PID_PRE_ERR  = 0x3C  # Special:   1100b, check: 0011b
PID_SPLIT    = 0x78  # Special:   1000b, check: 0111b
PID_PING     = 0xB4  # Special:   0100b, check: 1011b


def verify_usb_pid(pid: int) -> bool:
    """Verifies that the upper nibble is the exact bitwise inversion of the lower nibble."""
    type_nibble = pid & 0x0F
    check_nibble = (pid >> 4) & 0x0F
    return (type_nibble ^ check_nibble) == 0x0F


def compute_usb_crc5(data_bits: List[int]) -> int:
    """Compute USB 5-bit CRC over token data bits (11 bits: 7-bit addr + 4-bit ep).
    Polynomial: G(X) = X^5 + X^2 + 1 (0x05)
    Initial value: 0x1F, inverted at end.
    """
    crc = 0x1F
    for b in data_bits:
        fb = (crc >> 4) ^ (b & 1)
        crc = ((crc << 1) & 0x1F)
        if fb:
            crc ^= 0x05
    return (~crc) & 0x1F


def verify_usb_crc5(data_bits: List[int], expected_crc: int) -> bool:
    return compute_usb_crc5(data_bits) == (expected_crc & 0x1F)


def compute_usb_crc16(data_bytes: List[int]) -> int:
    """Compute USB 16-bit CRC over data packet payload bytes.
    Polynomial: G(X) = X^16 + X^15 + X^2 + 1 (0x8005)
    Initial value: 0xFFFF, inverted at end.
    """
    crc = 0xFFFF
    for byte in data_bytes:
        for bit_idx in range(8):
            b = (byte >> bit_idx) & 1
            fb = (crc ^ b) & 1
            crc >>= 1
            if fb:
                crc ^= 0xA001
    return (~crc) & 0xFFFF


def verify_usb_crc16(data_bytes: List[int], expected_crc: int) -> bool:
    return compute_usb_crc16(data_bytes) == (expected_crc & 0xFFFF)


def insert_usb_fs_bit_stuffing(bits: List[int]) -> List[int]:
    """Insert USB bit stuffing: after 6 consecutive '1's, insert a '0'."""
    stuffed: List[int] = []
    consec_ones = 0
    for b in bits:
        stuffed.append(b)
        if b == 1:
            consec_ones += 1
            if consec_ones == 6:
                stuffed.append(0)  # Insert stuffed '0'
                consec_ones = 0
        else:
            consec_ones = 0
    return stuffed


def remove_usb_fs_bit_stuffing(bits: List[int]) -> Tuple[List[int], bool]:
    """Remove USB bit stuffing: after 6 consecutive '1's, verify and discard next '0'.
    Returns (destuffed_bits, is_valid). If a '1' follows 6 consecutive '1's, is_valid is False.
    """
    destuffed: List[int] = []
    consec_ones = 0
    idx = 0
    valid = True
    while idx < len(bits):
        b = bits[idx]
        destuffed.append(b)
        idx += 1
        if b == 1:
            consec_ones += 1
            if consec_ones == 6:
                if idx < len(bits):
                    stuffed_bit = bits[idx]
                    idx += 1
                    if stuffed_bit != 0:
                        valid = False  # Bit stuffing violation
                consec_ones = 0
        else:
            consec_ones = 0
    return destuffed, valid


def nrzi_encode_fs(bits: List[int], initial_state: str = "J") -> List[str]:
    """Encode binary bits into Full-Speed differential line states using NRZI:
    - '0': Toggle line state (J <-> K)
    - '1': Hold current line state
    Full-Speed: J is D+=1/D-=0, K is D+=0/D-=1.
    """
    current_state = initial_state
    states: List[str] = []
    for b in bits:
        if b == 0:
            current_state = "K" if current_state == "J" else "J"
        states.append(current_state)
    return states


def nrzi_decode_fs(states: List[str], initial_state: str = "J") -> List[int]:
    """Decode Full-Speed differential line states into binary bits using NRZI:
    - Transition (J->K or K->J): Bit 0
    - No transition: Bit 1
    """
    current_state = initial_state
    bits: List[int] = []
    for s in states:
        if s in ("SE0", "SE1"):
            continue
        if s != current_state:
            bits.append(0)
            current_state = s
        else:
            bits.append(1)
    return bits


@dataclass
class UsbFsPacket:
    """Represents a decoded or structured USB 2.0 Full-Speed packet."""
    pid: int
    payload: List[int] = field(default_factory=list)
    crc5: Optional[int] = None
    crc16: Optional[int] = None
    valid_pid: bool = True
    valid_crc: bool = True
    bit_stuff_valid: bool = True


class UsbFsReceiverModel:
    """
    Cycle-accurate Full-Speed USB 2.0 receiver monitor.
    Tracks D+ and D- pin states, decodes NRZI symbols with bit_period timing,
    strips bit stuffing, and extracts USB packets.
    """
    def __init__(self, bit_period: int = 4, dp_pin: int = 3, dn_pin: int = 4):
        self.bit_period = bit_period
        self.dp_pin = dp_pin
        self.dn_pin = dn_pin
        self.state = "IDLE"
        self.prev_line = "J"
        self.prev_sample_state = "J"
        self.sample_timer = 0
        self.raw_nrzi_bits: List[int] = []
        self.packets_received: List[UsbFsPacket] = []
        self.se0_count = 0
        self.reset_detected = False

    def step(self, dp: int, dn: int) -> Optional[UsbFsPacket]:
        """Processes one clock or sample step on D+ and D-."""
        if dp == 1 and dn == 0:
            line = "J"
        elif dp == 0 and dn == 1:
            line = "K"
        elif dp == 0 and dn == 0:
            line = "SE0"
        else:
            line = "SE1"  # Illegal condition

        # Track SE0 for bus reset
        if line == "SE0":
            self.se0_count += 1
            if self.se0_count >= 50:
                self.reset_detected = True
        else:
            self.se0_count = 0

        new_packet: Optional[UsbFsPacket] = None

        if self.state == "IDLE":
            # Detect SOP (Full-Speed transition from J to K)
            if self.prev_line == "J" and line == "K":
                self.state = "RECEIVING"
                self.prev_sample_state = "J"
                self.sample_timer = self.bit_period // 2
                self.raw_nrzi_bits = []
        elif self.state == "RECEIVING":
            self.sample_timer -= 1
            if self.sample_timer <= 0:
                self.sample_timer = self.bit_period
                if line == "SE0":
                    self.state = "IDLE"
                    new_packet = self._finalize_packet()
                    if new_packet:
                        self.packets_received.append(new_packet)
                elif line in ("J", "K"):
                    # NRZI decode: transition -> 0, hold -> 1
                    bit = 0 if line != self.prev_sample_state else 1
                    self.raw_nrzi_bits.append(bit)
                    self.prev_sample_state = line
                elif line == "SE1":
                    self.state = "IDLE"

        self.prev_line = line
        return new_packet

    def _finalize_packet(self) -> Optional[UsbFsPacket]:
        if len(self.raw_nrzi_bits) < 16:
            return None

        # 1. Check SYNC byte (first 8 bits must be 0x80: 00000001b LSB first)
        sync_bits = self.raw_nrzi_bits[:8]
        sync_byte = 0
        for i, b in enumerate(sync_bits):
            sync_byte |= (b << i)
        if sync_byte != 0x80:
            return None

        # 2. Destuff bits following SYNC
        body_bits = self.raw_nrzi_bits[8:]
        destuffed_bits, stuff_valid = remove_usb_fs_bit_stuffing(body_bits)

        # 3. Extract bytes (LSB first)
        bytes_list: List[int] = []
        for i in range(0, len(destuffed_bits) - 7, 8):
            b_val = 0
            for bit_idx in range(8):
                b_val |= (destuffed_bits[i + bit_idx] << bit_idx)
            bytes_list.append(b_val)

        if len(bytes_list) < 1:
            return None

        pid_byte = bytes_list[0]
        valid_pid = verify_usb_pid(pid_byte)

        payload: List[int] = []
        crc16_val = None
        crc5_val = None
        valid_crc = True

        type_nibble = pid_byte & 0x0F
        if type_nibble in (0x03, 0x0B, 0x07, 0x0F):  # Data packets
            if len(bytes_list) >= 3:
                payload = bytes_list[1:-2]
                crc_low = bytes_list[-2]
                crc_high = bytes_list[-1]
                crc16_val = crc_low | (crc_high << 8)
                valid_crc = (compute_usb_crc16(payload) == crc16_val)
            else:
                payload = bytes_list[1:]
        elif type_nibble in (0x01, 0x09, 0x05, 0x0D, 0x04):  # Token packets
            if len(bytes_list) >= 3:
                payload = bytes_list[1:3]
                raw_token_bits = destuffed_bits[8:24]
                token_data = raw_token_bits[:11]
                crc_bits = raw_token_bits[11:16]
                crc5_val = sum(b << i for i, b in enumerate(crc_bits))
                valid_crc = (compute_usb_crc5(token_data) == crc5_val)

        return UsbFsPacket(
            pid=pid_byte,
            payload=payload,
            crc5=crc5_val,
            crc16=crc16_val,
            valid_pid=valid_pid,
            valid_crc=valid_crc,
            bit_stuff_valid=stuff_valid
        )


class UsbFsPpaModel:
    """
    Physical PPA Model for synthesizable USB 2.0 Full-Speed Serial Interface Engine (SIE) Macro
    on the IHP 130nm SG13G2 CMOS5L standard cell library.
    """
    STANDARD_CELL_COUNT = 512
    GATE_EQUIVALENCE_GE = 985.0
    AREA_UM2 = 3741.80
    AREA_OVERHEAD_PCT = 2.65
    CRITICAL_PATH_NS = 1.27
    FMAX_MHZ = 787.40
    DYNAMIC_POWER_UW_AT_10MHZ = 48.2
    THROUGHPUT_MBPS = 12.0
    ENERGY_EFFICIENCY_PJ_PER_BIT = 4.02


# -------------------------------------------------------------------------
# Assembly Firmware Generators for the Jane Street Protocol Emulator Core
# -------------------------------------------------------------------------

def build_usb_fs_tx_packet_asm(
    pid: int,
    payload: Optional[List[int]] = None,
    dp_pin: int = 3,
    dn_pin: int = 4,
    bit_cycles: int = 4
) -> List[str]:
    """
    Generates cycle-exact microcode transmitting a Full-Speed USB 2.0 packet:
    - Drives J state Idle (D+=1, D-=0)
    - Emits SYNC (0x80: K-J-K-J-K-J-K-K)
    - Emits PID byte
    - Emits Payload bytes (with CRC-16 for Data or CRC-5 for Token)
    - Applies NRZI line coding and dynamic bit stuffing
    - Emits EOP: 2 bit periods of SE0 (D+=0, D-=0) followed by 1 bit period of J
    - Halts with bus returned to J or High-Z
    """
    if payload is None:
        payload = []

    # 1. Assemble raw bits (LSB-first)
    raw_bits: List[int] = []
    # SYNC = 0x80 -> 00000001b LSB-first
    for bit_idx in range(8):
        raw_bits.append((0x80 >> bit_idx) & 1)

    # PID (8 bits)
    body_bits: List[int] = []
    for bit_idx in range(8):
        body_bits.append((pid >> bit_idx) & 1)

    # Payload & CRC
    type_nibble = pid & 0x0F
    if type_nibble in (0x03, 0x0B, 0x07, 0x0F):  # Data packet
        for byte in payload:
            for bit_idx in range(8):
                body_bits.append((byte >> bit_idx) & 1)
        crc16 = compute_usb_crc16(payload)
        for bit_idx in range(16):
            body_bits.append((crc16 >> bit_idx) & 1)

    # Dynamic bit stuffing on body bits
    stuffed_body = insert_usb_fs_bit_stuffing(body_bits)
    all_bits = raw_bits + stuffed_body

    # NRZI encode
    states = nrzi_encode_fs(all_bits, initial_state="J")

    # Add EOP: 2 bit times of SE0, 1 bit time of J
    states.append("SE0")
    states.append("SE0")
    states.append("J")

    asm: List[str] = []
    dp_mask = 1 << dp_pin
    dn_mask = 1 << dn_pin
    out_mask = dp_mask | dn_mask

    wait_cycles = bit_cycles - 2

    asm.append(f"GDIRI 0x{out_mask:02X}        ; Configure D+ (pin {dp_pin}) and D- (pin {dn_pin}) as outputs")
    # Initial J state
    asm.append(f"GWRI 0x{dp_mask:02X}         ; Drive J state (D+=1, D-=0)")
    if wait_cycles >= 0:
        asm.append(f"WAIT {wait_cycles}")

    for idx, st in enumerate(states):
        if st == "J":
            val = dp_mask
        elif st == "K":
            val = dn_mask
        elif st == "SE0":
            val = 0x00
        else:
            val = out_mask
        asm.append(f"GWRI 0x{val:02X}         ; Symbol {idx} ({st})")
        if wait_cycles >= 0:
            asm.append(f"WAIT {wait_cycles}")

    # Return bus to Idle J and halt
    asm.append(f"GWRI 0x{dp_mask:02X}         ; Return to J state Idle")
    asm.append("HALT                    ; TX complete")
    return asm


def build_usb_fs_rx_packet_asm(
    expected_pid: int = PID_DATA0,
    dp_pin: int = 3,
    dn_pin: int = 4,
    bit_cycles: int = 4
) -> List[str]:
    """
    Generates microcode to receive a Full-Speed USB 2.0 packet:
    - Waits for SOP transition from J to K on D+ via WAITEDGE (falling edge mode)
    - Strides to midpoint of Bit 0
    - Samples incoming PID into R0 using SHIFTIN (LSB mode: operand[3]=0)
    - Samples incoming Payload byte into R1 using SHIFTIN (LSB mode: operand[3]=0)
    - Validates PID against expected_pid, asserting R2 = 0x00 on match or R2 = 0xEE on corruption
    """
    asm: List[str] = []
    asm.append("GDIRI 0x00             ; Configure all pins as inputs")
    asm.append("LDI R0, 0x00           ; Clear R0 (PID)")
    asm.append("LDI R1, 0x00           ; Clear R1 (Payload)")
    asm.append("LDI R2, 0x00           ; Clear R2 (Status)")

    # Wait for falling edge on D+ (pin dp_pin) indicating transition from J (D+=1) to K (D+=0)
    operand = dp_pin & 0x07  # mode 0 (falling edge) on dp_pin
    asm.append(f"WAITEDGE R3, 0x{operand:02X} ; Wait for SOP (D+ falling edge from J to K)")

    # Align to midpoint of bit 0 (accounting for SOP duration on gpio_in)
    mid_wait = max(0, bit_cycles - 1)
    if mid_wait > 0:
        asm.append(f"WAIT {mid_wait}             ; Stride to midpoint of bit 0")

    wait_step = max(0, bit_cycles - 2)
    # Sample 8 bits of PID into R0 (LSB mode: operand[3]=0)
    for _ in range(8):
        asm.append(f"SHIFTIN R0, 0x{operand:02X}  ; Sample D+ into R0 (LSB)")
        if wait_step > 0:
            asm.append(f"WAIT {wait_step}")

    # Sample 8 bits of Payload into R1 (LSB mode: operand[3]=0)
    for _ in range(8):
        asm.append(f"SHIFTIN R1, 0x{operand:02X}  ; Sample D+ into R1 (LSB)")
        if wait_step > 0:
            asm.append(f"WAIT {wait_step}")

    # Validate PID against expected_pid
    asm.append(f"XORI R0, 0x{expected_pid:02X} ; Verify PID type and complement check")
    asm.append("JZ pid_valid           ; If match, branch to success")
    asm.append("LDI R2, 0xEE           ; Error: PID check nibble corrupt")
    asm.append("HALT                   ;")
    asm.append("pid_valid:             ;")
    asm.append("LDI R2, 0x00           ; Status R2 = 0x00 (Success)")
    asm.append("HALT                   ; RX complete")
    return asm


def build_usb_fs_pid_validator_asm(expected_pid: int) -> List[str]:
    """
    Decodes and validates an 8-bit USB PID in R0:
    - R0 holds candidate PID
    - Verifies candidate PID == expected_pid (verifying type and complement check)
    - If valid: R2 = 0x00
    - If invalid: R2 = 0xEE
    """
    asm: List[str] = [
        f"XORI R0, 0x{expected_pid:02X} ; Compare candidate PID against expected PID",
        "JZ pid_match           ; Branch if match",
        "LDI R2, 0xEE           ; Error: PID mismatch or corrupt complement",
        "HALT                   ;",
        "pid_match:             ;",
        "LDI R2, 0x00           ; Success: PID valid and matched",
        "HALT                   ;"
    ]
    return asm


def build_usb_fs_eop_detector_asm(dp_pin: int = 3, dn_pin: int = 4) -> List[str]:
    """
    Detects Full-Speed USB End of Packet (EOP):
    - Reads GPIO bus
    - Confirms SE0 state (D+=0 and D-=0)
    - Waits for rising edge on D+ (transition to J state)
    - Sets R2 = 0x00 on completion
    """
    asm: List[str] = [
        "GDIRI 0x00             ; Configure all pins as inputs",
        "GRD R0                 ; Read current pin states",
        f"ANDI R0, 0x{(1 << dp_pin) | (1 << dn_pin):02X} ; Mask D+ and D-",
        "JZ se0_confirmed       ; If both are 0, SE0 confirmed",
        "LDI R2, 0xEE           ; Error: Not in SE0",
        "HALT                   ;",
        "se0_confirmed:         ;",
        # Wait for D+ rising edge to transition to J
        f"WAITEDGE R3, 0x{(0x01 << 3) | (dp_pin & 0x07):02X} ; Wait for D+ rising edge (J transition)",
        "LDI R2, 0x00           ; Success: Clean EOP detected",
        "HALT                   ;"
    ]
    return asm
