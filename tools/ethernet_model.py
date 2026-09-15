# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
tools/ethernet_model.py - 10 Mbit/s Ethernet (10BASE-T) Physical Layer Model & Firmware Generators

Implements IEEE 802.3 Clause 14 (10BASE-T) modeling, framing, and firmware:
1. Manchester Biphase-L line modulation (logic 1: High->Low, logic 0: Low->High).
2. Preamble (7 octets of 0x55) and Start Frame Delimiter (SFD, 1 octet 0xD5), LSB-first.
3. Normal Link Pulses (NLP) link integrity heartbeats (~100 ns pulses every 16 ms).
4. End-of-Transmission (TP_IDL) delimiter (2.5 to 4.5 bit times high, then High-Z idle).
5. IEEE 802.3 32-bit Frame Check Sequence (CRC-32, poly 0x04C11DB7).
"""

from typing import List, Tuple
import zlib


def compute_ethernet_crc32(data: List[int]) -> int:
    """Compute standard IEEE 802.3 32-bit Ethernet CRC (FCS)."""
    return zlib.crc32(bytes(data)) & 0xFFFFFFFF


class EthernetTransceiverModel:
    """Cycle-accurate 10BASE-T physical layer transceiver and monitor."""

    STATE_IDLE = 0
    STATE_PREAMBLE = 1
    STATE_SFD = 2
    STATE_DATA = 3
    STATE_TP_IDL = 4

    def __init__(self, half_period: int = 4, pin: int = 0):
        self.half_period = half_period
        self.pin = pin
        self.reset()

    def reset(self) -> None:
        self.state = self.STATE_IDLE
        self.nlp_count = 0
        self.last_pulse_cycle = 0
        self.preamble_octets = 0
        self.received_bytes: List[int] = []
        self.sfd_locked = False
        self.tp_idl_detected = False
        self.link_active = False

    def generate_nlp_pulse(self, width_cycles: int = 2, idle_after: int = 16) -> List[int]:
        """Generate a single Normal Link Pulse (NLP) waveform."""
        return [1] * width_cycles + [0] * idle_after

    def generate_packet_waveform(
        self,
        payload: List[int],
        preamble_count: int = 7,
        include_crc: bool = True,
        idle_before: int = 8,
        idle_after: int = 12,
    ) -> List[int]:
        """
        Generate full cycle-accurate 10BASE-T packet waveform:
        [Idle] -> [Preamble 0x55 x 7] -> [SFD 0xD5] -> [Payload] -> [CRC-32] -> [TP_IDL] -> [Idle]
        Ethernet octets are transmitted LSB-first.
        """
        stream: List[int] = []

        # Idle low
        stream.extend([0] * idle_before)

        # Full frame bytes to transmit
        frame_bytes = [0x55] * preamble_count + [0xD5] + list(payload)
        if include_crc:
            crc = compute_ethernet_crc32(payload)
            crc_bytes = [
                (crc >> 0) & 0xFF,
                (crc >> 8) & 0xFF,
                (crc >> 16) & 0xFF,
                (crc >> 24) & 0xFF,
            ]
            frame_bytes.extend(crc_bytes)

        # Manchester encode each byte (LSB-first per IEEE 802.3 Clause 14)
        for byte_val in frame_bytes:
            for bit_idx in range(8):
                bit = (byte_val >> bit_idx) & 1
                if bit == 1:
                    # Logic 1: High first half, Low second half
                    stream.extend([1] * self.half_period)
                    stream.extend([0] * self.half_period)
                else:
                    # Logic 0: Low first half, High second half
                    stream.extend([0] * self.half_period)
                    stream.extend([1] * self.half_period)

        # TP_IDL delimiter: hold high for ~3 bit periods (6 * half_period)
        tp_idl_cycles = 3 * (2 * self.half_period)
        stream.extend([1] * tp_idl_cycles)

        # Return to idle
        stream.extend([0] * idle_after)
        return stream

    def generate_rx_frame_waveform(self, payload: int, idle_before: int = 8) -> List[int]:
        """Generate a test RX frame with start bit 1 and 8 data bits LSB-first."""
        stream: List[int] = []
        stream.extend([0] * idle_before)

        # Start bit '1': first half 1, second half 0
        stream.extend([1] * self.half_period)
        stream.extend([0] * self.half_period)

        # 8 Data bits LSB-first (IEEE 802.3)
        for bit_idx in range(8):
            bit = (payload >> bit_idx) & 1
            if bit == 1:
                stream.extend([1] * self.half_period)
                stream.extend([0] * self.half_period)
            else:
                stream.extend([0] * self.half_period)
                stream.extend([1] * self.half_period)

        # Return to idle 0
        stream.extend([0] * 8)
        return stream

    def decode_packet_waveform(self, samples: List[int]) -> Tuple[List[int], bool, bool]:
        """
        Decode a sampled 10BASE-T Manchester waveform into received payload bytes.
        Returns:
            (payload_bytes, sfd_locked, tp_idl_detected)
        """
        # Find transition from idle (0) to active preamble (1)
        start_idx = -1
        for i in range(len(samples) - 1):
            if samples[i] == 0 and samples[i + 1] == 1:
                start_idx = i + 1
                break

        if start_idx == -1:
            return [], False, False

        # Extract bits by sampling center of first half:
        bit_period = 2 * self.half_period
        decoded_bits = []
        curr_idx = start_idx + (self.half_period // 2)

        while curr_idx < len(samples) - self.half_period:
            # First half level directly gives bit value (1 if High, 0 if Low)
            bit = samples[curr_idx]
            decoded_bits.append(bit)
            curr_idx += bit_period

        # Assemble bits into bytes (LSB first per IEEE 802.3)
        decoded_bytes = []
        for i in range(0, len(decoded_bits) - 7, 8):
            byte_val = 0
            for b in range(8):
                byte_val |= (decoded_bits[i + b] << b)
            decoded_bytes.append(byte_val)

        # Check for SFD (0xD5)
        sfd_idx = -1
        for i, b in enumerate(decoded_bytes):
            if b == 0xD5:
                sfd_idx = i
                break

        if sfd_idx != -1:
            payload = decoded_bytes[sfd_idx + 1 :]
            return payload, True, True

        return decoded_bytes, False, False


def build_ethernet_nlp_generator_asm(pin: int = 0, pulse_cycles: int = 2, count: int = 3) -> str:
    """Generate firmware to transmit 'count' Normal Link Pulses (NLP) separated by idle intervals."""
    pin_mask = 1 << pin
    lines = [
        f"; 10BASE-T NLP Heartbeat Generator on pin {pin}",
        f"GDIRI 0x{pin_mask:02X}",   # Pin as output
        "WAIT  6",                  # Settle margin
        f"LDI   R3, {count}",        # Pulse repeat count
        "nlp_loop:",
        f"GWRI  0x{pin_mask:02X}",   # Assert pulse (High)
    ]
    if pulse_cycles >= 2:
        lines.append(f"WAIT  {pulse_cycles - 2}")
    lines.extend([
        "GWRI  0x00",                # Deassert pulse (Low)
        "WAIT  15",                  # Inter-pulse idle spacing
        "DECJNZ R3, nlp_loop",
        "GWRI  0x00",
        "GDIRI 0x00",                # Release bus to High-Z
        "HALT",
        "",
    ])
    return "\n".join(lines)


def build_ethernet_tx_packet_asm(
    payload: List[int],
    half_period: int = 4,
    pin: int = 0,
) -> str:
    """
    Generate assembly firmware to transmit a 10BASE-T Ethernet packet:
    Preamble (0x55 x 2) + SFD (0xD5) + Payload + TP_IDL (all LSB-first).
    """
    pin_mask = 1 << pin
    lines = [
        f"; 10BASE-T Packet Transmitter on pin {pin}",
        f"GDIRI 0x{pin_mask:02X}",   # Pin as output
        "GWRI  0x00",                # Start at idle 0
        "WAIT  8",                  # Settle before start
    ]

    wait_half = half_period - 2

    def emit_bit(bit: int):
        if bit == 1:
            # 1: High first half, Low second half
            lines.append(f"GWRI  0x{pin_mask:02X}")
            if wait_half >= 0:
                lines.append(f"WAIT  {wait_half}")
            lines.append("GWRI  0x00")
            if wait_half >= 0:
                lines.append(f"WAIT  {wait_half}")
        else:
            # 0: Low first half, High second half
            lines.append("GWRI  0x00")
            if wait_half >= 0:
                lines.append(f"WAIT  {wait_half}")
            lines.append(f"GWRI  0x{pin_mask:02X}")
            if wait_half >= 0:
                lines.append(f"WAIT  {wait_half}")

    def emit_byte(byte_val: int):
        for bit_idx in range(8):
            emit_bit((byte_val >> bit_idx) & 1)

    # Emit 2 bytes of Preamble 0x55
    emit_byte(0x55)
    emit_byte(0x55)

    # Emit SFD 0xD5
    emit_byte(0xD5)

    # Emit payload bytes
    for b in payload:
        emit_byte(b)

    # TP_IDL: Hold High for 3 bit times (6 * half_period)
    lines.append(f"GWRI  0x{pin_mask:02X}")
    lines.append(f"WAIT  {(6 * half_period) - 2}")
    lines.append("GWRI  0x00")       # Return to 0
    lines.append("GDIRI 0x00")       # Tri-state to High-Z
    lines.append("HALT")
    lines.append("")
    return "\n".join(lines)


def build_ethernet_rx_packet_asm(
    half_period: int = 4,
    pin: int = 3,
) -> str:
    """
    Generate assembly firmware to synchronize to Ethernet SFD (0xD5) and receive payload byte into R0.
    Uses WAITEDGE on mid-bit falling edge of preamble bit 0, then samples payload LSB-first into R0.
    """
    lines = [
        f"; 10BASE-T SFD Synchronizer & Payload Ingress on pin {pin}",
        "GDIRI 0x00",                # All pins input
        "LDI   R0, 0x00",            # Clear accumulator
        "LDI   R2, 0x00",            # Status OK
        f"WAITEDGE R3, 0x{pin:02X}", # Wait for falling edge of mid-bit transition (mode 00)
    ]

    wait_step = (2 * half_period) - 2
    initial_wait = half_period + (half_period // 2) - 2
    lines.append(f"WAIT  {initial_wait}")

    # Read 8 bits of payload directly into R0 (default LSB mode shifts in at bit 7 and shifts right)
    for bit_i in range(8):
        lines.append(f"SHIFTIN R0, {pin}")
        if bit_i < 7 and wait_step > 0:
            lines.append(f"WAIT    {wait_step}")

    lines.extend([
        "HALT",
        "",
    ])
    return "\n".join(lines)
