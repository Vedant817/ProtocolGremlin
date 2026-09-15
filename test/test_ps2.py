# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Cocotb tests for PS/2 Bidirectional Host Controller protocol engine.

Verifies:
1. PS/2 Device-to-Host Reception:
   - Scan codes received into R0 with dynamic odd-parity verification.
   - Tested on standard scan codes (0x1C 'A', 0x32 'B', 0xF0 Break, 0xAA BAT, 0x00, 0xFF).
2. PS/2 Parity Error Detection:
   - Injected parity fault is caught and sets R2 = 0xFD.
3. PS/2 Framing Error Detection:
   - Corrupted stop bit (0 instead of 1) is caught and sets R2 = 0xFE.
4. PS/2 Host-to-Device Transmission:
   - Host inhibit (CLK low >= 30 cycles), RTS (DATA low), clock edge synchronization.
   - Device samples 8 data bits and odd parity, returns ACK on 12th clock cycle.
   - Verified on standard commands (0xED Set LEDs, 0xF4 Enable, 0xFF Reset).
5. PS/2 Device NACK Detection:
   - Unresponsive device leaving DATA high during ACK cycle is caught and sets R2 = 0xFC.
6. Open-drain electrical safety:
   - Both lines verified for zero bus contention under forced external pull-downs.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from ps2_model import (  # noqa: E402
    PS2Device,
    build_ps2_rx_asm,
    build_ps2_tx_asm,
)

CLK_PIN = 4
DATA_PIN = 5
CLK_MASK = 1 << CLK_PIN
DATA_MASK = 1 << DATA_PIN
PULLUP_MASK = CLK_MASK | DATA_MASK


async def _init_dut_and_bootload(dut, words: list[int]):
    """Fresh hardware reset and bootload."""
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = PULLUP_MASK  # External pull-ups hold idle bus high
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = PULLUP_MASK


@cocotb.test()
async def test_ps2_rx_scan_codes(dut):
    """Test PS/2 Host receiving standard scan codes with odd-parity verification."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    test_codes = [0x1C, 0x32, 0xF0, 0xAA, 0x00, 0xFF]

    for code in test_codes:
        dut._log.info(f"Testing PS/2 RX: scan code = 0x{code:02X}")
        asm = build_ps2_rx_asm(clk_pin=CLK_PIN, data_pin=DATA_PIN)
        words = assemble(asm)
        await _init_dut_and_bootload(dut, words)

        device = PS2Device(clk_half_period=15, tx_bytes=[code])
        core = dut.user_project.u_core

        bus_clk_val = 1
        bus_data_val = 1

        for cycle in range(600):
            await FallingEdge(dut.clk)
            dut.uio_in.value = (bus_clk_val << CLK_PIN) | (bus_data_val << DATA_PIN)

            await RisingEdge(dut.clk)
            await ReadOnly()

            uio_oe = int(dut.uio_oe.value)
            uio_out = int(dut.uio_out.value)
            master_clk_low = bool((uio_oe & CLK_MASK) and not (uio_out & CLK_MASK))
            master_data_low = bool((uio_oe & DATA_MASK) and not (uio_out & DATA_MASK))

            # Electrical safety check
            assert not ((uio_oe & CLK_MASK) and (uio_out & CLK_MASK)), "Master drove active high on CLK"
            assert not ((uio_oe & DATA_MASK) and (uio_out & DATA_MASK)), "Master drove active high on DATA"

            dev_clk_low, dev_data_low = device.step(master_clk_low, master_data_low)
            bus_clk_val = 0 if (master_clk_low or dev_clk_low) else 1
            bus_data_val = 0 if (master_data_low or dev_data_low) else 1

            if bool(core.halted.value):
                break
        else:
            raise AssertionError(f"PS/2 RX timed out for scan code 0x{code:02X}")

        received = int(core.r0.value)
        status = int(core.r2.value)
        dut._log.info(f"PS/2 RX complete: expected=0x{code:02X}, got R0=0x{received:02X}, status R2=0x{status:02X}")
        assert status == 0x00, f"Expected status 0x00 (OK), got 0x{status:02X}"
        assert received == code, f"Data mismatch: expected 0x{code:02X}, got 0x{received:02X}"


@cocotb.test()
async def test_ps2_rx_parity_error(dut):
    """Test PS/2 Host detects parity mismatch and flags R2 = 0xFD."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    code = 0x1C  # Parity error injected
    dut._log.info("Testing PS/2 RX parity error detection")
    asm = build_ps2_rx_asm(clk_pin=CLK_PIN, data_pin=DATA_PIN)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    device = PS2Device(clk_half_period=15, tx_bytes=[code], inject_parity_error=True)
    core = dut.user_project.u_core

    bus_clk_val = 1
    bus_data_val = 1

    for cycle in range(600):
        await FallingEdge(dut.clk)
        dut.uio_in.value = (bus_clk_val << CLK_PIN) | (bus_data_val << DATA_PIN)

        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_oe = int(dut.uio_oe.value)
        uio_out = int(dut.uio_out.value)
        master_clk_low = bool((uio_oe & CLK_MASK) and not (uio_out & CLK_MASK))
        master_data_low = bool((uio_oe & DATA_MASK) and not (uio_out & DATA_MASK))

        dev_clk_low, dev_data_low = device.step(master_clk_low, master_data_low)
        bus_clk_val = 0 if (master_clk_low or dev_clk_low) else 1
        bus_data_val = 0 if (master_data_low or dev_data_low) else 1

        if bool(core.halted.value):
            break
    else:
        raise AssertionError("PS/2 parity error test timed out")

    status = int(core.r2.value)
    dut._log.info(f"PS/2 Parity Error status: R2=0x{status:02X}")
    assert status == 0xFD, f"Expected status 0xFD (Parity Error), got 0x{status:02X}"


@cocotb.test()
async def test_ps2_rx_framing_error(dut):
    """Test PS/2 Host detects corrupted stop bit (0) and flags R2 = 0xFE."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    code = 0x32  # Stop bit corrupted to 0
    dut._log.info("Testing PS/2 RX framing error detection")
    asm = build_ps2_rx_asm(clk_pin=CLK_PIN, data_pin=DATA_PIN)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    device = PS2Device(clk_half_period=15, tx_bytes=[code], inject_framing_error=True)
    core = dut.user_project.u_core

    bus_clk_val = 1
    bus_data_val = 1

    for cycle in range(600):
        await FallingEdge(dut.clk)
        dut.uio_in.value = (bus_clk_val << CLK_PIN) | (bus_data_val << DATA_PIN)

        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_oe = int(dut.uio_oe.value)
        uio_out = int(dut.uio_out.value)
        master_clk_low = bool((uio_oe & CLK_MASK) and not (uio_out & CLK_MASK))
        master_data_low = bool((uio_oe & DATA_MASK) and not (uio_out & DATA_MASK))

        dev_clk_low, dev_data_low = device.step(master_clk_low, master_data_low)
        bus_clk_val = 0 if (master_clk_low or dev_clk_low) else 1
        bus_data_val = 0 if (master_data_low or dev_data_low) else 1

        if bool(core.halted.value):
            break
    else:
        raise AssertionError("PS/2 framing error test timed out")

    status = int(core.r2.value)
    dut._log.info(f"PS/2 Framing Error status: R2=0x{status:02X}")
    assert status == 0xFE, f"Expected status 0xFE (Framing Error), got 0x{status:02X}"


@cocotb.test()
async def test_ps2_tx_command(dut):
    """Test PS/2 Host transmits commands to device with RTS and receives ACK."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    commands = [0xED, 0xF4, 0xFF]

    for cmd in commands:
        dut._log.info(f"Testing PS/2 TX: command = 0x{cmd:02X}")
        asm = build_ps2_tx_asm(cmd_byte=cmd, clk_pin=CLK_PIN, data_pin=DATA_PIN, inhibit_cycles=30)
        words = assemble(asm)
        await _init_dut_and_bootload(dut, words)

        device = PS2Device(clk_half_period=15, ack_host=True)
        core = dut.user_project.u_core

        bus_clk_val = 1
        bus_data_val = 1

        for cycle in range(600):
            await FallingEdge(dut.clk)
            dut.uio_in.value = (bus_clk_val << CLK_PIN) | (bus_data_val << DATA_PIN)

            await RisingEdge(dut.clk)
            await ReadOnly()

            uio_oe = int(dut.uio_oe.value)
            uio_out = int(dut.uio_out.value)
            master_clk_low = bool((uio_oe & CLK_MASK) and not (uio_out & CLK_MASK))
            master_data_low = bool((uio_oe & DATA_MASK) and not (uio_out & DATA_MASK))

            # Contention check
            assert not ((uio_oe & CLK_MASK) and (uio_out & CLK_MASK)), "Master drove active high on CLK"
            assert not ((uio_oe & DATA_MASK) and (uio_out & DATA_MASK)), "Master drove active high on DATA"

            dev_clk_low, dev_data_low = device.step(master_clk_low, master_data_low)
            bus_clk_val = 0 if (master_clk_low or dev_clk_low) else 1
            bus_data_val = 0 if (master_data_low or dev_data_low) else 1

            if bool(core.halted.value):
                break
        else:
            raise AssertionError(f"PS/2 TX timed out for command 0x{cmd:02X}")

        status = int(core.r2.value)
        dut._log.info(f"PS/2 TX complete: cmd=0x{cmd:02X}, device received={device.rx_bytes}, status R2=0x{status:02X}")
        assert device.rx_bytes == [cmd], f"Device received {device.rx_bytes}, expected {[cmd]}"
        assert status == 0x00, f"Expected ACK status 0x00, got 0x{status:02X}"


@cocotb.test()
async def test_ps2_tx_nack(dut):
    """Test PS/2 Host detects device NACK (data line stays high) and flags R2 = 0xFC."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    cmd = 0xED
    dut._log.info("Testing PS/2 TX NACK detection")
    asm = build_ps2_tx_asm(cmd_byte=cmd, clk_pin=CLK_PIN, data_pin=DATA_PIN, inhibit_cycles=30)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    device = PS2Device(clk_half_period=15, ack_host=False)  # Device will not ACK
    core = dut.user_project.u_core

    bus_clk_val = 1
    bus_data_val = 1

    for cycle in range(600):
        await FallingEdge(dut.clk)
        dut.uio_in.value = (bus_clk_val << CLK_PIN) | (bus_data_val << DATA_PIN)

        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_oe = int(dut.uio_oe.value)
        uio_out = int(dut.uio_out.value)
        master_clk_low = bool((uio_oe & CLK_MASK) and not (uio_out & CLK_MASK))
        master_data_low = bool((uio_oe & DATA_MASK) and not (uio_out & DATA_MASK))

        dev_clk_low, dev_data_low = device.step(master_clk_low, master_data_low)
        bus_clk_val = 0 if (master_clk_low or dev_clk_low) else 1
        bus_data_val = 0 if (master_data_low or dev_data_low) else 1

        if bool(core.halted.value):
            break
    else:
        raise AssertionError("PS/2 TX NACK test timed out")

    status = int(core.r2.value)
    dut._log.info(f"PS/2 TX NACK status: R2=0x{status:02X}")
    assert status == 0xFC, f"Expected status 0xFC (NACK), got 0x{status:02X}"


@cocotb.test()
async def test_ps2_electrical_safety(dut):
    """Verify cycle-by-cycle that PS/2 host never drives active HIGH under forced external pull-downs."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = build_ps2_rx_asm(clk_pin=CLK_PIN, data_pin=DATA_PIN)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core

    # External device forces both clock and data low throughout the entire execution
    for cycle in range(250):
        await FallingEdge(dut.clk)
        dut.uio_in.value = 0  # Forced low externally

        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_oe = int(dut.uio_oe.value)
        uio_out = int(dut.uio_out.value)
        assert not ((uio_oe & CLK_MASK) and ((uio_out >> CLK_PIN) & 1)), (
            f"Contention bug at cycle {cycle}: host actively drove 1 on CLK against external pull-down!"
        )
        assert not ((uio_oe & DATA_MASK) and ((uio_out >> DATA_PIN) & 1)), (
            f"Contention bug at cycle {cycle}: host actively drove 1 on DATA against external pull-down!"
        )

        if bool(core.halted.value):
            break

    dut._log.info("PS/2 open-drain electrical safety verified: zero bus contention under forced external pull-down.")

