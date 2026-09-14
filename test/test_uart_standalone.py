# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Standalone validation of uart_model against isa_model without cocotb."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from uart_model import build_uart_tx_asm, UartReceiver  # noqa: E402
from assembler import assemble  # noqa: E402
from isa_model import CoreModel  # noqa: E402


def test_uart_standalone():
    test_bytes = [0x00, 0xFF, 0x55, 0xAA, 0x42, 0x13, 0x7E, 0xC3]
    test_periods = [4, 8, 16]

    for P in test_periods:
        for b in test_bytes:
            asm = build_uart_tx_asm(b, bit_period_cycles=P, pin=0)
            words = assemble(asm)
            model = CoreModel(words)
            model.reset()
            rx = UartReceiver(bit_period_cycles=P)
            decoded = []
            max_cycles = 50 + 12 * P
            for _ in range(max_cycles):
                model.step()
                pin_val = model.state.gpio_out & 1
                res = rx.step(pin_val)
                if res is not None:
                    decoded.append(res)
                if model.state.halted and rx.state == UartReceiver.STATE_IDLE:
                    break
            assert decoded == [b], f"Period {P}, byte 0x{b:02X} failed: decoded {decoded}"

    print(f"All {len(test_periods) * len(test_bytes)} UART standalone tests passed successfully!")


if __name__ == "__main__":
    test_uart_standalone()
