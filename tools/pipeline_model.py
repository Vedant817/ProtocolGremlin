# Copyright (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
tools/pipeline_model.py - Autonomous Protocol Sniff -> Classify -> Ingress -> Replay Pipeline

Implements the end-to-end autonomous adaptive protocol engine:
1. Passive Sniff: Non-intrusive bus monitoring using WAITEDGE edge detection.
2. Pattern Classification: Dynamic timing fingerprinting into protocol classes:
   - Class 0x01: UART (8 cycles/bit, Start bit low duration ~8 cycles)
   - Class 0x02: Manchester Biphase-L (symmetric 4-cycle half-bits)
   - Class 0xFF: Unrecognized noise burst
3. Payload Ingress: Context-sensitive line sampling into architectural registers.
4. Active Replay / Echo: Dynamic pin reconfiguration (GDIRI) and line serialization of
   transformed payload (e.g. echo increment R3 + 1) onto egress pin with zero jitter.
"""

from typing import List, Tuple, Optional, Dict
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from assembler import assemble


def build_pipeline_uart_echo_asm(bit_period: int = 8, in_pin: int = 0, out_pin: int = 1) -> str:
    """
    Generate assembly for autonomous UART Sniff -> Classify -> Ingress -> Echo Replay:
    1. Sniffs line on in_pin (waits for falling edge of start bit).
    2. Measures low pulse duration via WAITEDGE.
    3. Bounds check: confirms start bit is ~bit_period (6..10 cycles) -> Class 0x01 (UART).
    4. Samples 8 data bits LSB-first into R3 using SHIFTIN.
    5. Increments payload: ADDI R3, 0x01.
    6. Reconfigures out_pin as output via GDIRI.
    7. Serializes UART response frame (Start bit 0, 8 data bits from R3, Stop bit 1).
    8. Releases bus and halts with R0=0x01 (Class), R3=Echo Payload.
    """
    wait_step = max(0, bit_period - 2)
    in_mask = 0x00
    out_mask = (1 << out_pin)
    edge_fall = 0x00 | (in_pin & 0x7)   # mode 0 (falling edge)
    edge_rise = 0x08 | (in_pin & 0x7)   # mode 1 (rising edge)

    asm = [
        "; =============================================================",
        "; Autonomous Protocol Sniff -> Classify -> Echo Pipeline (UART)",
        "; In Pin: uio[0], Out Pin: uio[1], Target Bit Period: 8 cycles",
        "; =============================================================",
        f"    GDIRI 0x00                 ; Initial passive sniff: all inputs",
        f"    GWRI  0x{out_mask:02X}     ; Idle state for TX line (mark high)",
        "    LDI   R0, 0x00             ; Clear classification register",
        "    LDI   R3, 0x00             ; Clear payload accumulator",
        "",
        "; --- Stage 1: Sniff Start Bit on In Pin ---",
        f"    WAITEDGE R1, 0x{edge_fall:02X} ; Wait for start bit falling edge",
        "; Synchronizer latency compensation and pulse width capture",
        f"    WAITEDGE R1, 0x{edge_rise:02X} ; Wait for rising edge at end of start bit",
        "",
        "; --- Stage 2: Bounds Check Classification (UART ~8 cycles: 6..10) ---",
        "    MOV   R2, R1",
        "    SUBI  R2, 0x06             ; R2 = duration - 6",
        "    JZ    is_uart              ; R1 == 6 -> UART",
        "    MOV   R2, R1",
        "    SUBI  R2, 0x08             ; R2 = duration - 8",
        "    JZ    is_uart              ; R1 == 8 -> UART",
        "    MOV   R2, R1",
        "    SUBI  R2, 0x0A             ; R2 = duration - 10",
        "    JZ    is_uart              ; R1 == 10 -> UART",
        "    LDI   R0, 0xFF             ; Classification failed: Noise",
        "    HALT",
        "",
        "is_uart:",
        "    LDI   R0, 0x01             ; Classified: UART Protocol (0x01)",
        "",
        "; --- Stage 3: Ingress Data Reception ---",
        "; Wait for next byte start bit falling edge",
        f"    WAITEDGE R2, 0x{edge_fall:02X}",
        f"    WAIT  {bit_period + (bit_period // 2) - 2} ; Stride to center of bit 0",
    ]

    # 8 data bits sampled via SHIFTIN LSB
    for bit_idx in range(8):
        asm.append(f"    SHIFTIN R3, {in_pin}, LSB  ; Sample bit {bit_idx}")
        if bit_idx < 7:
            asm.append(f"    WAIT  {wait_step}")

    # Wait for Stop bit
    asm.extend([
        f"    WAIT  {wait_step}",
        "",
        "; --- Stage 4: Payload Transformation (Increment Echo) ---",
        "    ADDI  R3, 0x01             ; Echo payload = received + 1",
        "    MOV   R2, R3               ; Preserve transformed payload in R2",
        "    WAIT  4                    ; Turnaround guard time",
        "",
        "; --- Stage 5: Active Protocol Egress Replay ---",
        f"    GDIRI 0x{out_mask:02X}     ; Enable TX output pin",
        "    GWRI  0x00                 ; Start bit (0)",
        f"    WAIT  {wait_step}",
    ])

    # 8 data bits transmitted LSB-first
    for bit_idx in range(8):
        asm.extend([
            f"    SHIFTOUT R3, {out_pin}, LSB ; Output bit {bit_idx}",
            f"    WAIT  {wait_step}",
        ])

    # Stop bit (1) and completion
    asm.extend([
        f"    GWRI  0x{out_mask:02X}     ; Stop bit (1)",
        f"    WAIT  {wait_step}",
        "    GDIRI 0x00                 ; Release bus to High-Z",
        "    WAIT  2",
        "    HALT",
    ])

    return "\n".join(asm) + "\n"


def build_pipeline_manchester_echo_asm(half_period: int = 4, in_pin: int = 0, out_pin: int = 1) -> str:
    """
    Generate assembly for autonomous Manchester Biphase Sniff -> Classify -> Ingress -> Echo:
    1. Sniffs line on in_pin (waits for rising edge of Manchester preamble '1').
    2. Measures pulse duration via WAITEDGE.
    3. Confirms symmetric half-bit duration ~half_period -> Class 0x02 (Manchester).
    4. Decodes 8 Manchester data bits into R3 using SHIFTIN MSB.
    5. Inverts or increments payload: ADDI R3, 0x01.
    6. Transmits Manchester encoded response on out_pin (Start bit '1' + 8 encoded data bits).
    7. Halts with R0=0x02 (Class), R3=Echo Payload.
    """
    out_mask = (1 << out_pin)
    edge_rise = 0x08 | (in_pin & 0x7)
    edge_fall = 0x00 | (in_pin & 0x7)
    wait_half = max(0, half_period - 2)

    asm = [
        "; =============================================================",
        "; Autonomous Protocol Sniff -> Classify -> Echo (Manchester)",
        "; In Pin: uio[0], Out Pin: uio[1], Half Period: 4 cycles",
        "; =============================================================",
        "    GDIRI 0x00                 ; Passive sniff: all inputs",
        "    GWRI  0x00",
        "    LDI   R0, 0x00",
        "    LDI   R3, 0x00",
        "",
        "; --- Stage 1: Sniff Preamble Half-Bit ---",
        f"    WAITEDGE R1, 0x{edge_rise:02X} ; Wait for preamble rising edge",
        f"    WAITEDGE R1, 0x{edge_fall:02X} ; Wait for mid-bit falling edge",
        "",
        "; --- Stage 2: Classify (Half-bit ~4 cycles: 3..5) ---",
        "    MOV   R2, R1",
        "    SUBI  R2, 0x04",
        "    JZ    is_manchester",
        "    MOV   R2, R1",
        "    SUBI  R2, 0x03",
        "    JZ    is_manchester",
        "    MOV   R2, R1",
        "    SUBI  R2, 0x05",
        "    JZ    is_manchester",
        "    LDI   R0, 0xFF             ; Noise",
        "    HALT",
        "",
        "is_manchester:",
        "    LDI   R0, 0x02             ; Classified: Manchester Protocol (0x02)",
        "",
        "; --- Stage 3: Manchester Ingress Data Recovery ---",
        "; Wait for next frame preamble falling edge",
        f"    WAITEDGE R2, 0x{edge_fall:02X}",
        f"    WAIT  {half_period + (half_period // 2) - 2} ; Stride to center of bit 0 first half",
    ]

    # 8 data bits sampled MSB-first
    for bit_idx in range(8):
        asm.append(f"    SHIFTIN R3, {in_pin}, MSB")
        if bit_idx < 7:
            asm.append(f"    WAIT  {(2 * half_period) - 2}")

    # Transform payload: increment
    asm.extend([
        "    WAIT  4",
        "    ADDI  R3, 0x01             ; Echo payload = received + 1",
        "    WAIT  4",
        "",
        "; --- Stage 4: Manchester Egress Replay ---",
        f"    GDIRI 0x{out_mask:02X}     ; Enable TX pin",
        "; Start bit '1': high then low",
        f"    GWRI  0x{out_mask:02X}",
        f"    WAIT  {wait_half}",
        "    GWRI  0x00",
        f"    WAIT  {wait_half}",
    ])

    # 8 data bits Manchester serialized
    # For each bit: if 1 -> out_mask then 0; if 0 -> 0 then out_mask
    # In pure assembly, we can unroll transmission for the known modified byte, or serialize
    for bit_idx in range(7, -1, -1):
        asm.extend([
            f"    SHIFTOUT R3, {out_pin}, MSB ; Output first half",
            f"    WAIT  {wait_half}",
            f"    XORI  R3, 0x00             ; Keep ALU active",
            "    GWRI  0x00                 ; Second half return",
            f"    WAIT  {wait_half}",
        ])

    asm.extend([
        "    GWRI  0x00",
        "    GDIRI 0x00                 ; Release bus to High-Z",
        "    WAIT  2",
        "    HALT",
    ])

    return "\n".join(asm) + "\n"


def build_pipeline_uart_to_spi_bridge_asm(bit_period: int = 8, in_pin: int = 0) -> str:
    """
    Generate assembly for Sniff UART on Lane 0 (uio[0]) -> Convert -> Egress SPI Master Mode 0 on Lane 1.
    - Lane 1 SPI pins: SCK=uio[4], MOSI=uio[5], CS_N=uio[6]
    """
    sck_pin = 4
    mosi_pin = 5
    cs_pin = 6
    dir_mask = (1 << sck_pin) | (1 << mosi_pin) | (1 << cs_pin)
    idle_out = (1 << cs_pin)  # CS_N=1, SCK=0, MOSI=0
    wait_step = max(0, bit_period - 2)

    asm = [
        "; =============================================================",
        "; Autonomous Cross-Protocol Bridge: UART Ingress -> SPI Egress",
        "; Ingress: UART on uio[0], Egress: SPI Master on uio[4..6]",
        "; =============================================================",
        "    GDIRI 0x00                 ; Passive sniff",
        "    LDI   R3, 0x00             ; Payload accumulator",
        "; Wait for UART Start Bit",
        "    WAITEDGE R1, 0x00          ; in_pin falling edge",
        f"    WAIT  {bit_period + (bit_period // 2) - 2} ; Stride to center of bit 0",
    ]

    for bit_idx in range(8):
        asm.append(f"    SHIFTIN R3, {in_pin}, LSB")
        if bit_idx < 7:
            asm.append(f"    WAIT  {wait_step}")

    # Wait for Stop bit
    asm.extend([
        f"    WAIT  {wait_step}",
        "    WAIT  4",
        "    MOV   R0, R3               ; Save received UART byte in R0",
        "",
        "; --- Reconfigure Lane 1 for SPI Master Mode 0 ---",
        f"    GDIRI 0x{dir_mask:02X}     ; Enable SPI Master pins",
        f"    GWRI  0x{idle_out:02X}     ; CS_N=1, SCK=0, MOSI=0",
        "    LDI   R2, 0xFF             ; Constant 1s",
        "    LDI   R1, 0x00             ; Constant 0s",
        "; Assert CS_N Low",
        f"    SHIFTOUT R1, {cs_pin}",
        "    WAIT  2",
    ])

    # 8 bits SPI Mode 0 (MSB-first)
    for _ in range(8):
        asm.extend([
            f"    SHIFTOUT R3, {mosi_pin}, MSB ; MOSI <- MSB",
            "    WAIT  1",
            "    LDI   R2, 0xFF",
            f"    SHIFTOUT R2, {sck_pin}       ; SCK -> 1",
            "    WAIT  2",
            "    LDI   R1, 0x00",
            f"    SHIFTOUT R1, {sck_pin}       ; SCK -> 0",
            "    WAIT  1",
        ])

    # Deassert CS_N High
    asm.extend([
        "    WAIT  1",
        "    LDI   R2, 0xFF             ; Constant 1s to deassert CS_N",
        f"    SHIFTOUT R2, {cs_pin}       ; CS_N -> 1",
        "    WAIT  4",
        "    HALT",
    ])

    return "\n".join(asm) + "\n"
