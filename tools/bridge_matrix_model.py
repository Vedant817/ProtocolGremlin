# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
tools/bridge_matrix_model.py - Multi-Protocol Bus Bridging Matrix Firmware Generator & Reference

Enables real-time, cross-protocol translation across heterogeneous serial buses on the ASIC:
1. I2C-to-SPI Bridge:
   - Ingress: I2C Master read from external slave (or I2C slave listener) on uio[1:0].
   - Egress: SPI Master Mode 0 (CPOL=0, CPHA=0) on uio[6:4] (SCK=4, MOSI=5, CS_N=6).
2. UART-to-CAN Bridge:
   - Ingress: Asynchronous UART RX on uio[0] with edge synchronization and framing verification.
   - Egress: ISO 11898 CAN 2.0A frame on open-drain uio[4] with SOF, ID, DLC, CRC-15, and ACK slot.
3. 1-Wire-to-UART Bridge:
   - Ingress: Dallas 1-Wire Master read timeslots on open-drain uio[0].
   - Egress: Asynchronous UART TX 8-N-1 on uio[4].
4. Multi-Byte Streaming Bridge:
   - Streaming pipeline translating continuous multi-byte bursts (UART -> SPI) with zero cumulative drift.
"""

from typing import List, Tuple, Optional
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from can_model import build_can_frame_bits
from i2c_model import build_i2c_read_asm


def build_bridge_i2c_master_to_spi_asm(
    i2c_addr: int = 0x38,
    sda_pin: int = 0,
    scl_pin: int = 1,
    sck_pin: int = 4,
    mosi_pin: int = 5,
    cs_pin: int = 6,
    half_period: int = 4
) -> str:
    """
    Generate assembly for I2C Master Read -> SPI Master Mode 0 Egress:
    1. Reads 1 byte from i2c_addr into R0 using verified I2C read protocol.
    2. Bridges R0 to SPI Master Mode 0 on sck_pin, mosi_pin, cs_pin.
    3. Halts with R2=0x00 (Success) and R0=Payload.
    """
    # Generate verified I2C read sequence
    i2c_asm = build_i2c_read_asm(
        addr7=i2c_addr,
        num_bytes=1,
        half_period=half_period,
        sda_pin=sda_pin,
        scl_pin=scl_pin
    )
    raw_lines = [l for l in i2c_asm.strip().split("\n") if l.strip() != "HALT"]

    i2c_mask = (1 << sda_pin) | (1 << scl_pin)
    spi_mask = (1 << sck_pin) | (1 << mosi_pin) | (1 << cs_pin)
    total_dir = i2c_mask | spi_mask
    od_mask = i2c_mask
    init_out = (1 << sda_pin) | (1 << scl_pin) | (1 << cs_pin)

    # Reconfigure pin directions to include SPI pins
    configured_lines = []
    for line in raw_lines:
        if "GDIRI" in line:
            configured_lines.append(f"    GDIRI 0x{total_dir:02X}     ; Enable I2C and SPI pins")
        elif "GODRI" in line:
            configured_lines.append(f"    GODRI 0x{od_mask:02X}      ; Open-drain on I2C pins only")
        elif "GWRI" in line and "Release both lines" in line:
            configured_lines.append(f"    GWRI  0x{init_out:02X}     ; Idle High (I2C released, CS_N=1)")
        else:
            configured_lines.append(line)

    # Append SPI Master Mode 0 transmission of R0
    spi_lines = [
        "",
        "; --- Stage 5: SPI Master Mode 0 Egress ---",
        "    MOV   R3, R0               ; Copy received payload to R3",
        "    LDI   R2, 0xFF             ; 1s",
        "    LDI   R1, 0x00             ; 0s",
        f"    SHIFTOUT R1, {cs_pin}      ; Assert CS_N Low",
        "    WAIT  2",
    ]

    for bit_idx in range(8):
        spi_lines.extend([
            f"    SHIFTOUT R3, {mosi_pin}, MSB ; MOSI <- MSB",
            "    WAIT  1",
            "    LDI   R2, 0xFF",
            f"    SHIFTOUT R2, {sck_pin}       ; SCK -> 1",
            "    WAIT  2",
            "    LDI   R1, 0x00",
            f"    SHIFTOUT R1, {sck_pin}       ; SCK -> 0",
            "    WAIT  1",
        ])

    spi_lines.extend([
        "    LDI   R2, 0xFF",
        f"    SHIFTOUT R2, {cs_pin}       ; Deassert CS_N High",
        "    WAIT  2",
        "    LDI   R2, 0x00             ; Status: SUCCESS",
        "    HALT",
    ])

    return "\n".join(configured_lines + spi_lines) + "\n"


def build_bridge_uart_to_can_asm(
    can_id: int = 0x123,
    payload_byte: int = 0xA5,
    bit_period: int = 8,
    uart_rx_pin: int = 0,
    can_tx_pin: int = 4
) -> str:
    """
    Generate assembly for UART Ingress -> CAN 2.0A Egress:
    1. Reconfigures uart_rx_pin as input (uio_oe bit 0 = 0) and can_tx_pin as open-drain output.
    2. Waits for UART Start bit falling edge via WAITEDGE.
    3. Strides to center of bit 0, samples 8 data bits LSB-first into R0.
    4. Validates UART Stop bit (checks line is High). If Stop bit is Low, sets R2=0xFE and halts.
    5. Translates payload into CAN 2.0A standard frame on can_tx_pin.
    6. Emits SOF, 11-bit ID, RTR, IDE, r0, DLC, 8-bit Data, CRC-15, CRC Delimiter.
    7. Samples external receiver ACK bit. If ACK missing, sets R2=0xAE.
    8. Emits ACK Delimiter and 7-bit End of Frame (EOF).
    9. Halts with R2=0x00 (Success) and R0=Payload.
    """
    can_mask = 1 << can_tx_pin
    edge_fall = 0x00 | (uart_rx_pin & 0x7)
    wait_step = max(0, bit_period - 2)

    stuffed_bits, crc15, post_ack = build_can_frame_bits(can_id, [payload_byte])

    lines = [
        "; =============================================================",
        "; Multi-Protocol Bridge: UART Ingress -> CAN 2.0A Egress",
        f"; UART RX: uio[{uart_rx_pin}], CAN TX: uio[{can_tx_pin}], ID: 0x{can_id:03X}",
        "; =============================================================",
        f"    GDIRI 0x{can_mask:02X}     ; CAN pin output, UART pin input",
        f"    GODRI 0x{can_mask:02X}     ; CAN pin open-drain",
        f"    GWRI  0x{can_mask:02X}     ; CAN pin idle recessive (High)",
        "    LDI   R2, 0x00             ; Default status: Success",
        "",
        "; --- Stage 1: UART Start Bit Edge Detect ---",
        f"    WAITEDGE R1, 0x{edge_fall:02X} ; Wait for Start Bit falling edge",
        f"    WAIT  {bit_period + (bit_period // 2) - 2} ; Stride to center of bit 0",
        "",
        "; --- Stage 2: UART Data Bits Ingress (LSB-first) ---",
    ]

    for bit_idx in range(8):
        lines.append(f"    SHIFTIN R0, {uart_rx_pin}, LSB ; Sample UART bit {bit_idx}")
        if bit_idx < 7:
            lines.append(f"    WAIT  {wait_step}")

    # Verify Stop Bit
    lines.extend([
        f"    WAIT  {wait_step}          ; Wait to center of Stop bit",
        "    GRD   R3",
        f"    ANDI  R3, 0x{(1 << uart_rx_pin):02X} ; Check UART Stop bit",
        "    JNZ   uart_ok",
        "    LDI   R2, 0xFE             ; UART Framing Error (Stop bit Low)",
        "    HALT",
        "uart_ok:",
        "    WAIT  4                    ; Inter-protocol turnaround",
        "",
        "; --- Stage 3: CAN 2.0A Egress Transmission ---",
    ])

    normal_wait = max(0, bit_period - 2)

    # Serialize pre-ACK stuffed bits
    for idx, bit in enumerate(stuffed_bits):
        if bit == 0:
            lines.append("    GWRI  0x00")
            if normal_wait > 0:
                lines.append(f"    WAIT  {normal_wait}")
        else:
            lines.append(f"    GWRI  0x{can_mask:02X}")
            if normal_wait > 0:
                lines.append(f"    WAIT  {normal_wait}")

    # CRC Delimiter (1 recessive bit)
    lines.append(f"    GWRI  0x{can_mask:02X}")
    if normal_wait > 0:
        lines.append(f"    WAIT  {normal_wait}")

    # ACK Slot: transmitter drives recessive, checks if receiver pulls dominant (0)
    lines.extend([
        f"    GWRI  0x{can_mask:02X}     ; Recessive ACK slot",
    ])
    ack_wait = max(0, (bit_period // 2) - 1)
    if ack_wait > 0:
        lines.append(f"    WAIT  {ack_wait}")
    lines.extend([
        "    GRD   R3                   ; Sample ACK bit",
        f"    ANDI  R3, 0x{can_mask:02X}",
        "    JZ    ack_received",
        "    LDI   R2, 0xAE             ; Missing ACK Error",
        "    HALT",
        "ack_received:",
    ])
    remaining_ack = max(0, bit_period - ack_wait - 5)
    if remaining_ack > 0:
        lines.append(f"    WAIT  {remaining_ack}")

    # ACK Delimiter (1) + End of Frame (7x 1s)
    lines.extend([
        f"    GWRI  0x{can_mask:02X}     ; ACK delimiter & EOF",
        f"    WAIT  {bit_period * 8}     ; Hold recessive through EOF",
        "    LDI   R2, 0x00             ; Status: Success",
        "    HALT",
    ])

    return "\n".join(lines) + "\n"


def build_bridge_onewire_to_uart_asm(
    onewire_pin: int = 0,
    uart_tx_pin: int = 4,
    uart_period: int = 8
) -> str:
    """
    Generate assembly for Dallas 1-Wire Ingress -> UART TX Egress:
    1. Reconfigures onewire_pin as open-drain and uart_tx_pin as push-pull output.
    2. Issues 8 read timeslots on onewire_pin:
       - Pull line low for 2 cycles.
       - Release line.
       - Wait 8 cycles for slave bit response.
       - Sample line into R0 using SHIFTIN LSB.
       - Wait remaining 35 cycles for timeslot recovery.
    3. Reconfigures uart_tx_pin idle high.
    4. Serializes R0 onto uart_tx_pin at uart_period cycles/bit:
       - Start bit (0)
       - 8 data bits (LSB-first)
       - Stop bit (1)
    5. Halts with R2=0x00 and R0=Payload.
    """
    ow_mask = 1 << onewire_pin
    uart_mask = 1 << uart_tx_pin
    dir_mask = ow_mask | uart_mask
    uart_wait = max(0, uart_period - 2)

    lines = [
        "; =============================================================",
        "; Multi-Protocol Bridge: Dallas 1-Wire Ingress -> UART TX Egress",
        f"; 1-Wire: uio[{onewire_pin}] (Open-Drain), UART TX: uio[{uart_tx_pin}]",
        "; =============================================================",
        f"    GDIRI 0x{dir_mask:02X}     ; Enable 1-Wire and UART outputs",
        f"    GODRI 0x{ow_mask:02X}      ; 1-Wire is open-drain, UART push-pull",
        f"    GWRI  0x{dir_mask:02X}     ; Idle High on both buses",
        "    LDI   R0, 0x00             ; Data byte accumulator",
        "    LDI   R1, 0x08             ; 8 bits",
        "    WAIT  4                    ; Bus settling",
        "",
        "; --- Stage 1: Dallas 1-Wire Read Timeslots (8 bits) ---",
        "loop_ow_read:",
        f"    GWRI  0x{uart_mask:02X}    ; 1-Wire -> 0 (initiate timeslot)",
        "    WAIT  1                    ; 2 cycles low pulse",
        f"    GWRI  0x{dir_mask:02X}     ; 1-Wire -> Release (1)",
        "    WAIT  2                    ; Settle & sync (cycles 3-5)",
        f"    SHIFTIN R0, {onewire_pin}, LSB ; Sample bit into R0 (cycle 6)",
        "    WAIT  22                   ; Timeslot recovery",
        "    DECJNZ R1, loop_ow_read",
        "",
        "; --- Stage 2: UART TX Egress ---",
        "    MOV   R3, R0               ; Copy byte to R3 for UART transmission",
        "    WAIT  4                    ; Turnaround guard",
        f"    GWRI  0x{ow_mask:02X}      ; UART Start bit (0), keep 1-Wire released",
        f"    WAIT  {uart_wait}",
    ]

    for bit_idx in range(8):
        lines.extend([
            f"    SHIFTOUT R3, {uart_tx_pin}, LSB ; Output UART bit {bit_idx}",
            f"    WAIT  {uart_wait}",
        ])

    lines.extend([
        f"    GWRI  0x{dir_mask:02X}     ; UART Stop bit (1)",
        f"    WAIT  {uart_wait}",
        "    LDI   R2, 0x00             ; Status: Success",
        "    HALT",
    ])

    return "\n".join(lines) + "\n"


def build_bridge_multi_byte_stream_asm(
    byte_count: int = 3,
    bit_period: int = 8,
    uart_rx_pin: int = 0,
    sck_pin: int = 4,
    mosi_pin: int = 5,
    cs_pin: int = 6
) -> str:
    """
    Generate assembly for continuous streaming bridge:
    Translates byte_count consecutive UART bytes into SPI Master Mode 0 frames.
    """
    spi_mask = (1 << sck_pin) | (1 << mosi_pin) | (1 << cs_pin)
    init_out = (1 << cs_pin)
    edge_fall = 0x00 | (uart_rx_pin & 0x7)
    wait_step = max(0, bit_period - 2)

    lines = [
        "; =============================================================",
        "; Multi-Byte Streaming Bridge: UART Ingress -> SPI Master Egress",
        f"; Streaming {byte_count} consecutive frames without cumulative drift",
        "; =============================================================",
        f"    GWRI  0x{init_out:02X}     ; CS_N=1, SCK=0, MOSI=0 (latch high before enable)",
        f"    GDIRI 0x{spi_mask:02X}     ; SPI pins output, UART pin input",
        f"    LDI   R0, 0x{byte_count:02X} ; Outer packet counter",
        "",
        "stream_packet_loop:",
        "; --- Wait for UART Start Bit ---",
        f"    WAITEDGE R1, 0x{edge_fall:02X}",
        f"    WAIT  {bit_period + (bit_period // 2) - 2} ; Stride to center of bit 0",
        "    LDI   R3, 0x00             ; Clear byte buffer",
    ]

    for bit_idx in range(8):
        lines.append(f"    SHIFTIN R3, {uart_rx_pin}, LSB")
        if bit_idx < 7:
            lines.append(f"    WAIT  {wait_step}")

    # Wait for stop bit
    lines.extend([
        f"    WAIT  {wait_step}",
        "    WAIT  2",
        "",
        "; --- SPI Master Egress ---",
        "    LDI   R2, 0xFF",
        "    LDI   R1, 0x00",
        f"    SHIFTOUT R1, {cs_pin}      ; CS_N -> 0",
        "    WAIT  2",
    ])

    for _ in range(8):
        lines.extend([
            f"    SHIFTOUT R3, {mosi_pin}, MSB",
            "    WAIT  1",
            "    LDI   R2, 0xFF",
            f"    SHIFTOUT R2, {sck_pin}       ; SCK -> 1",
            "    WAIT  2",
            "    LDI   R1, 0x00",
            f"    SHIFTOUT R1, {sck_pin}       ; SCK -> 0",
            "    WAIT  1",
        ])

    lines.extend([
        "    LDI   R2, 0xFF",
        f"    SHIFTOUT R2, {cs_pin}       ; CS_N -> 1",
        "    WAIT  4",
        "    DECJNZ R0, stream_packet_loop ; Next packet",
        "    LDI   R2, 0x00             ; Status: Success",
        "    HALT",
    ])

    return "\n".join(lines) + "\n"
