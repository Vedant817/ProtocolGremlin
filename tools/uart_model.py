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


def build_uart_rx_asm(
    bit_period_cycles: int,
    pin: int = 3,
    check_false_start: bool = False,
) -> str:
    """Generate protocol-engine assembly for receiving one byte over UART.

    Format: 8-N-1 (1 start bit low, 8 data bits LSB-first, 1 stop bit high).
    Line idles high. Uses WAITEDGE for 0-jitter hardware edge synchronization.

    Register assignment on completion:
      R0: received 8-bit data
      R1: scratchpad / stop bit state
      R2: status code (0x00 = OK, 0xFE = framing error, 0xFF = false start glitch)
      R3: WAITEDGE start-bit latency

    Args:
        bit_period_cycles: bit duration in clock cycles (must be >= 4, or >= 6 if check_false_start).
        pin: GPIO pin index (0-7), default 3.
        check_false_start: if True, samples at 0.5P to verify valid start bit.

    Returns:
        Assembly source code as a string.
    """
    if pin < 0 or pin > 7:
        raise ValueError(f"pin must be 0-7, got {pin}")
    if check_false_start and bit_period_cycles < 6:
        raise ValueError(f"bit_period_cycles must be >= 6 when check_false_start is True, got {bit_period_cycles}")
    if not check_false_start and bit_period_cycles < 4:
        raise ValueError(f"bit_period_cycles must be >= 4, got {bit_period_cycles}")

    pin_mask = 1 << pin
    lines = [
        f"; UART RX firmware for {bit_period_cycles} cycles/bit on pin {pin}",
        "GDIRI 0x00",            # All pins input
        "LDI   R2, 0x00",        # Status OK (0x00)
        f"WAITEDGE R3, 0x{pin:02X}",  # Mode 0 (falling edge) on pin. Stalls until falling edge at cycle T0
    ]

    if check_false_start:
        wait_start = (bit_period_cycles // 2) - 2
        wait_to_bit0 = bit_period_cycles - 5
        if wait_start > 0:
            lines.append(f"WAIT  {wait_start}")
        lines.extend([
            "; --- False start bit check (mid-start at 0.5P) ---",
            "GRD   R1",
            f"ANDI  R1, 0x{pin_mask:02X}",
            "JZ    start_ok",
            "LDI   R2, 0xFF",        # 0xFF = false start glitch error
            "HALT",
            "start_ok:",
        ])
        if wait_to_bit0 > 0:
            lines.append(f"WAIT  {wait_to_bit0}")
    else:
        first_wait = (3 * bit_period_cycles) // 2 - 2
        if first_wait > 0:
            lines.append(f"WAIT  {first_wait}")

    # Center of Bit 0
    wait_between = bit_period_cycles - 2
    for bit_i in range(8):
        lines.append(f"; --- Sample bit {bit_i} ---")
        lines.append(f"SHIFTIN R0, {pin}")
        if wait_between > 0:
            lines.append(f"WAIT    {wait_between}")

    # After 8th bit and wait_between, we are at center of stop bit
    lines.extend([
        "; --- Stop bit check (mid-stop) ---",
        "GRD   R1",
        f"ANDI  R1, 0x{pin_mask:02X}",
        "JNZ   rx_success",
        "LDI   R2, 0xFE",        # 0xFE = framing error (stop bit low)
        "rx_success:",
        "HALT",
        "",
    ])

    return "\n".join(lines)


class UartTransmitter:
    """Cycle-accurate UART bitstream generator for testbenches and simulations.

    Transmits 8-N-1 UART frames (1 start bit low, 8 data bits LSB-first, 1 stop bit high).
    """

    def __init__(self, bit_period_cycles: int, pin: int = 3):
        if bit_period_cycles < 2:
            raise ValueError("bit_period_cycles must be >= 2")
        self.bit_period = bit_period_cycles
        self.pin = pin

    def generate_bit_stream(
        self,
        byte_value: int,
        stop_bit: int = 1,
        idle_before: int = 4,
        idle_after: int = 4,
    ) -> list[int]:
        """Generate a cycle-by-cycle list of pin values (0 or 1) for a UART frame."""
        stream = [1] * idle_before
        # Start bit
        stream.extend([0] * self.bit_period)
        # 8 Data bits (LSB first)
        for i in range(8):
            bit = (byte_value >> i) & 1
            stream.extend([bit] * self.bit_period)
        # Stop bit
        stream.extend([stop_bit] * self.bit_period)
        # Idle after
        stream.extend([1] * idle_after)
        return stream

