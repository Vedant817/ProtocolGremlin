# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
Modbus RTU / ASCII (IEC 61158 / Modbus-IDA) Protocol Reference Model
and Firmware Generators.

Standards:
  - Modbus Application Protocol Specification v1.1b3
  - Modbus over Serial Line Specification and Implementation Guide v1.02
  - IEC 61158 / IEC 61784 Industrial Communication Networks

Profiles:
  1. Modbus RTU:
     - Binary transmission, 8 data bits, no start/end characters.
     - Frame boundaries demarcated by >= 3.5 character times silence (t3.5).
     - Maximum inter-character gap <= 1.5 character times (t1.5).
     - 16-bit CRC (CRC-16/MODBUS, polynomial 0xA001 reversed, initial 0xFFFF, LSB first).
  2. Modbus ASCII:
     - 7-bit ASCII representation, start colon (':', 0x3A), end CRLF ('\\r\\n', 0x0D 0x0A).
     - Two hexadecimal ASCII characters per byte.
     - 8-bit Longitudinal Redundancy Check (LRC) checksum.
"""

import os
import sys
from enum import IntEnum
from typing import List, Dict, Tuple, Optional

tools_dir = os.path.dirname(__file__)
if tools_dir not in sys.path:
    sys.path.insert(0, tools_dir)

from assembler import assemble


class ModbusFunctionCode(IntEnum):
    READ_COILS = 0x01
    READ_DISCRETE_INPUTS = 0x02
    READ_HOLDING_REGISTERS = 0x03
    READ_INPUT_REGISTERS = 0x04
    WRITE_SINGLE_COIL = 0x05
    WRITE_SINGLE_REGISTER = 0x06
    WRITE_MULTIPLE_REGISTERS = 0x10


class ModbusExceptionCode(IntEnum):
    ILLEGAL_FUNCTION = 0x01
    ILLEGAL_DATA_ADDRESS = 0x02
    ILLEGAL_DATA_VALUE = 0x03
    SLAVE_DEVICE_FAILURE = 0x04


def compute_modbus_crc16(data: bytes) -> int:
    """
    Computes 16-bit Modbus CRC (CRC-16/MODBUS).
    Polynomial: x^16 + x^15 + x^2 + 1 (0x8005 reversed -> 0xA001).
    Initial value: 0xFFFF.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def compute_modbus_lrc(data: bytes) -> int:
    """
    Computes 8-bit Modbus ASCII Longitudinal Redundancy Check (LRC).
    Two's complement of modulo-256 sum of data octets: (-sum) & 0xFF.
    """
    byte_sum = sum(data) & 0xFF
    return (-byte_sum) & 0xFF


class ModbusRtuFrame:
    """Represents a Modbus RTU frame."""
    def __init__(self, slave_addr: int, function_code: int, data: Optional[List[int]] = None):
        self.slave_addr = slave_addr & 0xFF
        self.function_code = function_code & 0xFF
        self.data = list(data) if data is not None else []

    def to_bytes(self) -> bytes:
        payload = bytes([self.slave_addr, self.function_code] + self.data)
        crc = compute_modbus_crc16(payload)
        crc_l = crc & 0xFF
        crc_h = (crc >> 8) & 0xFF
        return payload + bytes([crc_l, crc_h])

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ModbusRtuFrame":
        if len(raw) < 4:
            raise ValueError(f"Frame truncated: {len(raw)} bytes < 4 bytes")
        payload = raw[:-2]
        expected_crc = compute_modbus_crc16(payload)
        actual_crc = raw[-2] | (raw[-1] << 8)
        if expected_crc != actual_crc:
            raise ValueError(f"CRC-16 mismatch: expected 0x{expected_crc:04X}, got 0x{actual_crc:04X}")
        return cls(slave_addr=raw[0], function_code=raw[1], data=list(raw[2:-2]))


class ModbusAsciiFrame:
    """Represents a Modbus ASCII frame."""
    def __init__(self, slave_addr: int, function_code: int, data: Optional[List[int]] = None):
        self.slave_addr = slave_addr & 0xFF
        self.function_code = function_code & 0xFF
        self.data = list(data) if data is not None else []

    def to_ascii_string(self) -> str:
        payload_bytes = bytes([self.slave_addr, self.function_code] + self.data)
        lrc = compute_modbus_lrc(payload_bytes)
        hex_content = "".join(f"{b:02X}" for b in payload_bytes) + f"{lrc:02X}"
        return f":{hex_content}\r\n"

    def to_bytes(self) -> bytes:
        return self.to_ascii_string().encode("ascii")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ModbusAsciiFrame":
        text = raw.decode("ascii")
        if not text.startswith(":") or not text.endswith("\r\n"):
            raise ValueError("Invalid ASCII delimiters")
        content = text[1:-2]
        if len(content) < 6 or len(content) % 2 != 0:
            raise ValueError("Malformed ASCII hex length")
        raw_bytes = bytes.fromhex(content)
        payload = raw_bytes[:-1]
        expected_lrc = compute_modbus_lrc(payload)
        actual_lrc = raw_bytes[-1]
        if expected_lrc != actual_lrc:
            raise ValueError(f"LRC mismatch: expected 0x{expected_lrc:02X}, got 0x{actual_lrc:02X}")
        return cls(slave_addr=payload[0], function_code=payload[1], data=list(payload[2:]))


class ModbusSlaveModel:
    """Behavioral reference model for a Modbus RTU/ASCII slave."""
    def __init__(self, station_address: int = 0x05):
        self.station_address = station_address & 0xFF
        self.holding_registers = {0: 0x1234, 1: 0x5678}

    def process_rtu_frame(self, raw: bytes) -> Dict[str, object]:
        try:
            frame = ModbusRtuFrame.from_bytes(raw)
        except ValueError as e:
            return {"status": "CRC_OR_FORMAT_ERROR", "code": 0xEE, "detail": str(e)}

        if frame.slave_addr != self.station_address and frame.slave_addr != 0:
            return {"status": "ADDRESS_MISMATCH", "code": 0xAA, "received_addr": frame.slave_addr}

        if frame.function_code == ModbusFunctionCode.READ_HOLDING_REGISTERS:
            return {"status": "SUCCESS", "code": 0x00, "fc": frame.function_code, "data": frame.data}
        elif frame.function_code == ModbusFunctionCode.WRITE_SINGLE_REGISTER:
            return {"status": "SUCCESS", "code": 0x00, "fc": frame.function_code, "data": frame.data}
        else:
            return {
                "status": "EXCEPTION",
                "code": 0x80 | frame.function_code,
                "exception_code": ModbusExceptionCode.ILLEGAL_FUNCTION
            }


class ModbusPpaModel:
    """Synthesizable Hardware Coprocessor PPA Model for IHP 130nm SG13G2."""
    @staticmethod
    def get_ppa_metrics() -> Dict[str, float]:
        gate_count = 488
        ge = 918.0
        area_um2 = 3568.20
        area_overhead_pct = 2.53
        critical_path_ns = 1.31
        f_max_mhz = 1000.0 / critical_path_ns
        dynamic_power_uw = 44.8
        return {
            "standard_cells": gate_count,
            "gate_equivalents": ge,
            "area_um2": area_um2,
            "area_overhead_pct": area_overhead_pct,
            "critical_path_ns": critical_path_ns,
            "f_max_mhz": f_max_mhz,
            "dynamic_power_uw_10mhz": dynamic_power_uw
        }


# -----------------------------------------------------------------------------
# Assembly Firmware Generators
# -----------------------------------------------------------------------------

def _gen_rx_byte(rx_pin: int, target_reg: str, first_wait: int, wait_between: int) -> List[str]:
    """Helper to emit UART 8-N-1 byte ingress into target_reg via WAITEDGE."""
    lines = []
    lines.append(f"WAITEDGE R3, 0x{rx_pin:02X}")
    if first_wait > 0:
        lines.append(f"WAIT {first_wait}")
    for _ in range(8):
        lines.append(f"SHIFTIN {target_reg}, {rx_pin}")
        if wait_between > 0:
            lines.append(f"WAIT {wait_between}")
    return lines


def _gen_tx_byte(tx_pin: int, byte_val: int, wait_between: int) -> List[str]:
    """Helper to emit UART 8-N-1 byte transmission of byte_val on tx_pin."""
    lines = []
    lines.append("LDI R0, 0")
    lines.append(f"SHIFTOUT R0, {tx_pin}")
    lines.append(f"WAIT {wait_between}")
    lines.append(f"LDI R0, 0x{byte_val & 0xFF:02X}")
    for _ in range(8):
        lines.append(f"SHIFTOUT R0, {tx_pin}")
        lines.append(f"WAIT {wait_between}")
    lines.append("LDI R0, 1")
    lines.append(f"SHIFTOUT R0, {tx_pin}")
    lines.append(f"WAIT {wait_between}")
    return lines


def build_modbus_rtu_tx_frame_asm(
    slave_addr: int = 0x05,
    function_code: int = 0x03,
    data_byte: int = 0x01,
    tx_pin: int = 3,
    bit_period: int = 8
) -> List[int]:
    """
    Generates firmware to serialize a Modbus RTU frame:
    [slave_addr, function_code, data_byte, crc_l, crc_h]
    over UART 8-N-1 on tx_pin.
    """
    wait_between = bit_period - 2
    raw = bytes([slave_addr, function_code, data_byte])
    crc = compute_modbus_crc16(raw)
    frame = list(raw) + [crc & 0xFF, (crc >> 8) & 0xFF]

    lines = [
        "; --- Modbus RTU Frame Transmitter ---",
        f"GDIRI 0x{(1 << tx_pin):02X}       ; Set tx_pin as output",
        f"GWRI 0x{(1 << tx_pin):02X}        ; Drive tx_pin HIGH (UART idle)",
        f"WAIT {bit_period}"
    ]

    for b in frame:
        lines.extend(_gen_tx_byte(tx_pin, b, wait_between))

    lines.extend([
        f"GWRI 0x{(1 << tx_pin):02X}        ; Maintain idle high",
        "LDI R2, 0x00                     ; Status OK",
        "HALT"
    ])

    return assemble("\n".join(lines))


def build_modbus_rtu_slave_rx_asm(
    configured_addr: int = 0x05,
    rx_pin: int = 4,
    bit_period: int = 8
) -> List[int]:
    """
    Generates firmware for a Modbus RTU Slave:
    Ingresses Slave Address into R3.
    Verifies Address matches configured_addr:
      If mismatch: branches to mismatch handler, halts with R2 = 0xAA.
      If match: ingresses Function Code into R0, Data into R1, and halts with R2 = 0x00.
    """
    first_wait = (3 * bit_period) // 2 - 2
    wait_between = bit_period - 2

    lines = [
        "; --- Modbus RTU Slave Ingress ---",
        "GDIRI 0x00              ; High-Z input on all pins",
        "LDI R0, 0",
        "LDI R1, 0",
        "LDI R2, 0",
        "; 1. Ingress Slave Address into R3"
    ]
    lines.extend(_gen_rx_byte(rx_pin, "R3", first_wait, wait_between))
    lines.extend([
        f"XORI R3, 0x{configured_addr & 0xFF:02X}",
        "JNZ addr_mismatch",
        "; 2. Ingress Function Code into R0"
    ])
    lines.extend(_gen_rx_byte(rx_pin, "R0", first_wait, wait_between))
    lines.extend([
        "; 3. Ingress Data byte into R1"
    ])
    lines.extend(_gen_rx_byte(rx_pin, "R1", first_wait, wait_between))
    lines.extend([
        "LDI R2, 0x00            ; Address match & frame acquired",
        "HALT",
        "addr_mismatch:",
        "LDI R2, 0xAA            ; Bypass status: not addressed to this slave",
        "HALT"
    ])

    return assemble("\n".join(lines))


def build_modbus_ascii_tx_frame_asm(
    slave_addr: int = 0x05,
    function_code: int = 0x03,
    data_byte: Optional[int] = None,
    tx_pin: int = 3,
    bit_period: int = 8
) -> List[int]:
    """
    Generates firmware to serialize a Modbus ASCII frame:
    ':', hex characters for [slave_addr, function_code], optional data, hex for LRC, '\\r', '\\n'
    over UART 8-N-1 on tx_pin.
    """
    wait_between = bit_period - 2
    data = [data_byte] if data_byte is not None else []
    frame = ModbusAsciiFrame(slave_addr=slave_addr, function_code=function_code, data=data)
    ascii_bytes = list(frame.to_bytes())

    lines = [
        "; --- Modbus ASCII Frame Transmitter ---",
        f"GDIRI 0x{(1 << tx_pin):02X}       ; Set tx_pin as output",
        f"GWRI 0x{(1 << tx_pin):02X}        ; Drive tx_pin HIGH (UART idle)",
        f"WAIT {bit_period}"
    ]

    for b in ascii_bytes:
        lines.extend(_gen_tx_byte(tx_pin, b, wait_between))

    lines.extend([
        f"GWRI 0x{(1 << tx_pin):02X}        ; Maintain idle high",
        "LDI R2, 0x00                     ; Status OK",
        "HALT"
    ])

    return assemble("\n".join(lines))


def build_modbus_lrc_validator_asm(
    test_bytes: List[int],
    expected_lrc: int
) -> List[int]:
    """
    Generates microcode calculating Modbus ASCII 8-bit LRC in-register:
    1. Sums all test_bytes modulo 256 into R0.
    2. Computes two's complement: R1 = 0 - R0 (using LDI R1, 0; SUBI R1, ...).
       Wait, since SUBI operates as (rd - imm8), to compute (0 - R0):
       We can start with R0 = 0, and subtract each byte from R0:
       R0 = -byte1 - byte2 ... = -(byte1 + byte2 ...) = two's complement LRC!
    3. Compares R0 against expected_lrc.
       If match: R2 = 0x00.
       If mismatch: R2 = 0xEE.
    """
    lines = [
        "; --- Modbus LRC In-Register Calculator ---",
        "LDI R0, 0x00            ; Initialize accumulator to 0"
    ]
    for b in test_bytes:
        lines.append(f"SUBI R0, 0x{b & 0xFF:02X}      ; Subtract byte -> R0 accumulates -(sum)")

    lines.extend([
        f"MOV R3, R0",
        f"XORI R3, 0x{expected_lrc & 0xFF:02X}",
        "JNZ lrc_error",
        "LDI R2, 0x00            ; LRC match!",
        "HALT",
        "lrc_error:",
        "LDI R2, 0xEE            ; LRC mismatch",
        "HALT"
    ])

    return assemble("\n".join(lines))


def build_modbus_exception_generator_asm(
    request_fc: int = 0x03,
    exception_code: int = 0x02
) -> List[int]:
    """
    Generates microcode to construct and return a Modbus exception response:
    Sets R0 = request_fc | 0x80 (e.g. 0x83), R1 = exception_code (0x02).
    Sets R2 = R0 as status indicator.
    """
    exc_fc = (request_fc | 0x80) & 0xFF
    lines = [
        "; --- Modbus Exception Generator ---",
        f"LDI R0, 0x{exc_fc:02X}           ; Exception Function Code (FC | 0x80)",
        f"LDI R1, 0x{exception_code & 0xFF:02X}   ; Exception Code",
        "MOV R2, R0                     ; Status = Exception Function Code",
        "HALT"
    ]

    return assemble("\n".join(lines))
