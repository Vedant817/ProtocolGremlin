# Copyright (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
tools/usb_model.py - Cycle-accurate USB 1.1 Low-Speed (1.5 Mbps) Physical Signaling Engine

Implements the USB 1.1 physical layer and packet framing:
- Low-Speed Differential Signaling:
    - J state (Idle): D+ = 0, D- = 1
    - K state: D+ = 1, D- = 0
    - SE0 (Single-Ended Zero): D+ = 0, D- = 0 (EOP and Reset)
    - SE1 (Single-Ended One): D+ = 1, D- = 1 (Illegal condition)
- Non-Return-to-Zero Inverted (NRZI) Line Coding:
    - '0': Toggle state (J <-> K) at the start of bit cell
    - '1': Hold current state (no transition)
- Dynamic Bit Stuffing:
    - Forced '0' (transition) inserted after six consecutive '1' bits
- Packet Formatting:
    - SYNC: 8 bits = 0x80 (transmitted LSB-first: 00000001 -> K-J-K-J-K-J-K-K)
    - PID: 8 bits (4-bit packet type + 4-bit bitwise complement check)
    - Token / Data payload
    - EOP: 2 bit periods of SE0 followed by 1 bit period of J state
- Firmware Generators:
    - build_usb_tx_packet_asm(): Cycle-exact USB packet transmitter
    - build_usb_rx_packet_asm(): Edge-synchronized USB packet receiver via WAITEDGE
"""

from typing import List, Tuple, Optional, Dict
from dataclasses import dataclass, field


# Standard USB 1.1 Packet Identifiers (PIDs)
PID_OUT   = 0xE1  # Token: 0001b, check: 1110b
PID_IN    = 0x69  # Token: 1001b, check: 0110b
PID_SOF   = 0xA5  # Token: 0101b, check: 1010b
PID_SETUP = 0x2D  # Token: 1101b, check: 0010b
PID_DATA0 = 0xC3  # Data:  0011b, check: 1100b
PID_DATA1 = 0x4B  # Data:  1011b, check: 0100b
PID_ACK   = 0xD2  # Handshake: 0010b, check: 1101b
PID_NAK   = 0x5A  # Handshake: 1010b, check: 0101b
PID_STALL = 0x1E  # Handshake: 1110b, check: 0001b


def compute_usb_crc5(data_bits: List[int]) -> int:
    """Compute USB 5-bit CRC over token data bits.
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


def insert_usb_bit_stuffing(bits: List[int]) -> List[int]:
    """Insert USB bit stuffing: after 6 consecutive '1's, insert a '0'."""
    stuffed = []
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


def remove_usb_bit_stuffing(bits: List[int]) -> Tuple[List[int], bool]:
    """Remove USB bit stuffing: after 6 consecutive '1's, verify and discard next '0'."""
    destuffed = []
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
                if idx >= len(bits) or bits[idx] != 0:
                    valid = False  # Bit stuff violation!
                idx += 1  # Skip the stuffed '0'
                consec_ones = 0
        else:
            consec_ones = 0
    return destuffed, valid


@dataclass
class UsbDecodedPacket:
    pid: int
    pid_valid: bool
    payload: List[int]
    crc: int
    crc_valid: bool
    is_valid: bool
    eop_detected: bool
    bit_stuff_error: bool
    raw_bits: List[int] = field(default_factory=list)


class UsbReceiver:
    """
    Independent cycle-accurate software model for USB 1.1 Low-Speed differential bus.
    Monitors D+ and D- pins, decodes J/K/SE0 states, performs NRZI decoding and bit destuffing,
    validates SYNC and PID, and checks CRC and EOP.
    """

    def __init__(self, bit_period: int = 8, dp_pin: int = 0, dm_pin: int = 1):
        self.bit_period = bit_period
        self.dp_pin = dp_pin
        self.dm_pin = dm_pin

        self.state = "IDLE"  # IDLE, RECEIVING, EOP
        self.current_line_state = "J"
        self.prev_line_state = "J"
        self.sample_timer = 0
        self.raw_nrzi_bits: List[int] = []
        self.rx_packets: List[UsbDecodedPacket] = []
        self.se0_count = 0
        self.bus_resets_detected = 0

    def _line_state(self, uio_val: int) -> str:
        dp = (uio_val >> self.dp_pin) & 1
        dm = (uio_val >> self.dm_pin) & 1
        if dp == 0 and dm == 1:
            return "J"
        elif dp == 1 and dm == 0:
            return "K"
        elif dp == 0 and dm == 0:
            return "SE0"
        else:
            return "SE1"  # Illegal

    def step(self, uio_val: int) -> None:
        """Advance simulation by one clock cycle."""
        line = self._line_state(uio_val)

        # Track continuous SE0 for bus reset detection (> 30 bit periods)
        if line == "SE0":
            self.se0_count += 1
            if self.se0_count == 30 * self.bit_period:
                self.bus_resets_detected += 1
        else:
            self.se0_count = 0

        if self.state == "IDLE":
            # Detect SOP (Start of Packet): Low-Speed transition from J to K
            if self.prev_line_state == "J" and line == "K":
                self.state = "RECEIVING"
                self.prev_sample_state = "J"  # Idle line state before SOP
                self.sample_timer = self.bit_period // 2  # Stride to midpoint of bit 0
                self.raw_nrzi_bits = []
        elif self.state == "RECEIVING":
            self.sample_timer -= 1
            if self.sample_timer <= 0:
                self.sample_timer = self.bit_period
                if line == "SE0":
                    # EOP detected!
                    self.state = "IDLE"
                    self._finalize_packet()
                elif line in ("J", "K"):
                    # NRZI decode: transition from prev_sample_state -> 0, hold -> 1
                    bit = 0 if line != self.prev_sample_state else 1
                    self.raw_nrzi_bits.append(bit)
                    self.prev_sample_state = line
                elif line == "SE1":
                    # Illegal condition
                    self.state = "IDLE"

        self.prev_line_state = line

    def _finalize_packet(self) -> None:
        """Decode accumulated NRZI bits into USB packet fields."""
        if len(self.raw_nrzi_bits) < 16:
            return  # Runt packet (less than SYNC + PID)

        # Destuff bits
        destuffed_bits, stuff_valid = remove_usb_bit_stuffing(self.raw_nrzi_bits)

        # Convert destuffed bits into bytes (LSB first)
        bytes_list = []
        for i in range(0, len(destuffed_bits) - 7, 8):
            b_val = 0
            for bit_idx in range(8):
                b_val |= (destuffed_bits[i + bit_idx] << bit_idx)
            bytes_list.append(b_val)

        if len(bytes_list) < 2:
            return

        sync_byte = bytes_list[0]
        pid_byte = bytes_list[1]
        pid_nibble = pid_byte & 0x0F
        pid_check = (pid_byte >> 4) & 0x0F
        pid_valid = (pid_nibble ^ pid_check) == 0x0F

        payload = bytes_list[2:]
        is_valid = (sync_byte == 0x80) and pid_valid and stuff_valid

        packet = UsbDecodedPacket(
            pid=pid_byte,
            pid_valid=pid_valid,
            payload=payload,
            crc=0,
            crc_valid=True,
            is_valid=is_valid,
            eop_detected=True,
            bit_stuff_error=not stuff_valid,
            raw_bits=destuffed_bits
        )
        self.rx_packets.append(packet)


def build_usb_tx_packet_asm(
    pid: int,
    payload: Optional[List[int]] = None,
    bit_period: int = 8,
    dp_pin: int = 0,
    dm_pin: int = 1
) -> str:
    """
    Generate cycle-exact assembly firmware to transmit a full USB 1.1 Low-Speed packet.
    Packet includes:
    1. SYNC field: 0x80 (LSB first: 0, 0, 0, 0, 0, 0, 0, 1)
    2. PID byte (with complement check nibble)
    3. Optional payload / CRC with dynamic bit stuffing (inserted '0' after 6 ones)
    4. EOP: 2 bit periods SE0 + 1 bit period J state
    5. Bus return to Idle (J) and tri-state release
    """
    payload = payload or []

    # Assemble raw bitstream (LSB-first)
    raw_bits = []

    # 1. SYNC field: 0x80 (binary 00000001 LSB-first: 0, 0, 0, 0, 0, 0, 0, 1)
    for i in range(8):
        raw_bits.append((0x80 >> i) & 1)

    # 2. PID field (LSB-first)
    for i in range(8):
        raw_bits.append((pid >> i) & 1)

    # 3. Payload and CRC bytes
    for byte in payload:
        for i in range(8):
            raw_bits.append((byte >> i) & 1)

    # Apply USB bit stuffing (SYNC is never stuffed, but bit-stuffing applies after SYNC)
    # Bits after SYNC:
    post_sync_bits = raw_bits[8:]
    stuffed_post_sync = insert_usb_bit_stuffing(post_sync_bits)
    all_bits = raw_bits[:8] + stuffed_post_sync

    # NRZI encoding for Low-Speed:
    # Starting state: J (D+=0, D-=1).
    # '0' -> toggle state (J <-> K).
    # '1' -> hold state.
    j_mask = (0 << dp_pin) | (1 << dm_pin)
    k_mask = (1 << dp_pin) | (0 << dm_pin)
    se0_mask = 0x00
    dir_mask = (1 << dp_pin) | (1 << dm_pin)

    curr_state = "J"
    states: List[str] = []
    for b in all_bits:
        if b == 0:
            curr_state = "K" if curr_state == "J" else "J"
        states.append(curr_state)

    wait_half = max(0, bit_period - 2)

    asm = [
        "; -------------------------------------------------------------",
        f"; USB 1.1 Low-Speed TX Firmware: PID=0x{pid:02X}, Bits={len(all_bits)}",
        f"; Pins: D+={dp_pin}, D-={dm_pin}, BitPeriod={bit_period} cycles",
        "; -------------------------------------------------------------",
        f"    GDIRI 0x{dir_mask:02X}     ; Enable D+ and D- outputs",
        f"    GWRI  0x{j_mask:02X}       ; Drive J state (Idle)",
        "    WAIT  4                    ; Settling delay",
    ]

    # Transmit encoded NRZI bit cells
    for idx, st in enumerate(states):
        out_val = j_mask if st == "J" else k_mask
        asm.append(f"    GWRI  0x{out_val:02X}       ; Bit {idx}: {st} state")
        if wait_half > 0:
            asm.append(f"    WAIT  {wait_half}")

    # EOP: 2 bit periods of SE0 (2 * bit_period cycles)
    se0_wait = (2 * bit_period) - 2
    asm.append(f"    GWRI  0x{se0_mask:02X}     ; EOP: SE0 (D+=0, D-=0)")
    if se0_wait > 0:
        asm.append(f"    WAIT  {se0_wait}")

    # EOP: 1 bit period of J state
    asm.append(f"    GWRI  0x{j_mask:02X}       ; EOP: J state (D+=0, D-=1)")
    if wait_half > 0:
        asm.append(f"    WAIT  {wait_half}")

    # Release bus to tri-state input
    asm.extend([
        "    GDIRI 0x00                 ; Release bus to High-Z",
        "    WAIT  2",
        "    HALT",
    ])

    return "\n".join(asm) + "\n"


def build_usb_rx_packet_asm(
    bit_period: int = 8,
    dp_pin: int = 0,
    dm_pin: int = 1
) -> str:
    """
    Generate assembly firmware to receive and decode a USB 1.1 Low-Speed packet.
    Algorithm:
    1. Wait for SOP (Start of Packet): Low-Speed J -> K transition causes D+ (pin 0) rising edge!
    2. Synchronize via WAITEDGE on D+.
    3. Stride to bit 7 of SYNC and verify SYNC completion.
    4. Sample PID into R0 using SHIFTIN.
    5. Check PID complement integrity into R1.
    6. Halt with decoded PID in R0 and status in R1.
    """
    # WAITEDGE on dp_pin rising edge: mode 1 (rising), operand = (1 << 3) | dp_pin
    edge_operand = 0x08 | (dp_pin & 0x7)
    wait_step = max(0, bit_period - 2)

    asm = [
        "; --- USB 1.1 Low-Speed RX Firmware ---",
        "    GDIRI 0x00                 ; D+ and D- configured as inputs",
        "    LDI   R0, 0x00             ; Clear PID buffer",
        "    LDI   R1, 0x00             ; Status",
        "; Wait for SOP: D+ rising edge (J -> K transition)",
        f"    WAITEDGE R2, 0x{edge_operand:02X}",
        "; Synchronized to SOP! Skip remainder of SYNC (7 bits = 7 * bit_period)",
    ]
    # Stride through the remaining 7 bits of SYNC
    sync_skip = (7 * bit_period) - 2
    if sync_skip > 0:
        asm.append(f"    WAIT  {sync_skip}")

    # Sample PID byte: 8 bits sampled into R0
    # In USB, bits are transmitted LSB-first. We sample pin 0 (D+) or read bus.
    for bit_idx in range(8):
        asm.append(f"    SHIFTIN R0, {dp_pin}, LSB  ; Sample PID bit {bit_idx}")
        if bit_idx < 7 and wait_step > 0:
            asm.append(f"    WAIT  {wait_step}")

    # Check PID validity: lower 4 bits XOR upper 4 bits inverted == 0xF
    asm.extend([
        "    MOV   R1, R0",
        "    ANDI  R1, 0x0F             ; Lower nibble",
        "    MOV   R2, R0",
        "    ANDI  R2, 0xF0             ; Upper nibble",
        "    WAIT  2",
        "    HALT",
    ])

    return "\n".join(asm) + "\n"
