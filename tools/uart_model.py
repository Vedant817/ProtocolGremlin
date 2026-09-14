# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Independent UART reference model and firmware generator.

This module provides:
1. build_uart_tx_asm: Parameterized assembly generator for UART TX on a
   chosen GPIO pin and bit period (in clock cycles).
2. UartReceiver: An independent cycle-accurate software-UART receiver
   algorithm that samples an RX line, detects falling edges (start bit),
   samples each bit at its mid-period point, and verifies the stop bit.

This decoder does NOT share timing logic with the transmitter; it acts
as a real-world asynchronous UART receiver peripheral.
"""
from __future__ import annotations


class UartFramingError(Exception):
    """Raised when a start bit is false or stop bit is not high."""
    pass


def build_uart_tx_asm(
    byte_value: int,
    bit_period_cycles: int,
    pin: int = 0,
) -> str:
    """Generate protocol-engine assembly for transmitting one byte over UART.

    Format: 8-N-1 (1 start bit low, 8 data bits LSB-first, 1 stop bit high).
    Line idles high.

    Args:
        byte_value: 0-255 integer to transmit.
        bit_period_cycles: bit duration in clock cycles (must be >= 2).
        pin: GPIO pin index (0-7), default 0.

    Returns:
        Assembly source code as a string.
    """
    if not (0 <= byte_value <= 255):
        raise ValueError(f"byte_value must be 0-255, got {byte_value}")
    if bit_period_cycles < 2:
        raise ValueError(f"bit_period_cycles must be >= 2, got {bit_period_cycles}")
    if not (0 <= pin <= 7):
        raise ValueError(f"pin must be 0-7, got {pin}")

    pin_mask = 1 << pin
    wait_cycles = bit_period_cycles - 2

    # Instructions:
    # 1. Configure pin as output
    # 2. Drive pin high (idle)
    # 3. Load byte into R0
    # 4. Wait 1 full bit period of idle
    # 5. Start bit: drive pin low, wait bit period
    # 6. 8 data bits: SHIFTOUT R0, pin + wait bit period
    # 7. Stop bit: drive pin high, wait bit period
    # 8. HALT
    lines = [
        f"; UART TX firmware for byte 0x{byte_value:02X} at {bit_period_cycles} cycles/bit on pin {pin}",
        f"GDIRI 0x{pin_mask:02X}",
        f"GWRI  0x{pin_mask:02X}",
        f"LDI   R0, 0x{byte_value:02X}",
        f"WAIT  {bit_period_cycles}",
        "; --- Start bit (0) ---",
        "GWRI  0x00",
        f"WAIT  {wait_cycles}",
    ]

    for bit_i in range(8):
        lines.append(f"; --- Data bit {bit_i} ---")
        lines.append(f"SHIFTOUT R0, {pin}")
        lines.append(f"WAIT     {wait_cycles}")

    lines.extend([
        "; --- Stop bit (1) ---",
        f"GWRI  0x{pin_mask:02X}",
        f"WAIT  {wait_cycles}",
        "HALT",
        "",
    ])

    return "\n".join(lines)


class UartReceiver:
    """Independent cycle-by-cycle UART receiver decoder.

    Detects the falling edge of the start bit, waits to mid-bit to verify
    the start bit is 0, then mid-bit-samples each of the 8 data bits (LSB first),
    and finally verifies the stop bit is 1.
    """

    STATE_IDLE = 0
    STATE_START_CHECK = 1
    STATE_DATA = 2
    STATE_STOP_CHECK = 3

    def __init__(self, bit_period_cycles: int):
        if bit_period_cycles < 2:
            raise ValueError("bit_period_cycles must be >= 2")
        self.bit_period = bit_period_cycles
        self.reset()

    def reset(self) -> None:
        self.state = self.STATE_IDLE
        self.prev_line = 1
        self.cycle_cnt = 0
        self.target_cycle = 0
        self.bit_idx = 0
        self.shift_reg = 0
        self.received_bytes: list[int] = []

    def step(self, rx_bit: int) -> int | None:
        """Feed one sampled cycle bit into the receiver.

        Returns the decoded byte (0-255) if a valid frame completed on this
        cycle, or None.
        """
        rx = 1 if rx_bit else 0
        decoded_byte = None

        if self.state == self.STATE_IDLE:
            # Look for falling edge from 1 to 0
            if self.prev_line == 1 and rx == 0:
                self.state = self.STATE_START_CHECK
                self.cycle_cnt = 0
                # Sample start bit in the middle
                self.target_cycle = self.bit_period // 2

        elif self.state == self.STATE_START_CHECK:
            self.cycle_cnt += 1
            if self.cycle_cnt == self.target_cycle:
                if rx != 0:
                    # False start bit / glitch
                    self.state = self.STATE_IDLE
                else:
                    self.state = self.STATE_DATA
                    self.bit_idx = 0
                    self.shift_reg = 0
                    self.cycle_cnt = 0
                    self.target_cycle = self.bit_period

        elif self.state == self.STATE_DATA:
            self.cycle_cnt += 1
            if self.cycle_cnt == self.target_cycle:
                self.shift_reg |= (rx << self.bit_idx)
                self.bit_idx += 1
                self.cycle_cnt = 0
                if self.bit_idx == 8:
                    self.state = self.STATE_STOP_CHECK
                    self.target_cycle = self.bit_period

        elif self.state == self.STATE_STOP_CHECK:
            self.cycle_cnt += 1
            if self.cycle_cnt == self.target_cycle:
                if rx != 1:
                    self.state = self.STATE_IDLE
                    raise UartFramingError(f"Framing error: stop bit was {rx}, expected 1")
                decoded_byte = self.shift_reg
                self.received_bytes.append(decoded_byte)
                self.state = self.STATE_IDLE

        self.prev_line = rx
        return decoded_byte

    def receive(self, bit_stream: list[int]) -> list[int]:
        """Convenience method to feed a list of cycle-by-cycle bit values."""
        results = []
        for bit in bit_stream:
            byte = self.step(bit)
            if byte is not None:
                results.append(byte)
        return results
