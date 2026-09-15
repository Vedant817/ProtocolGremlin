# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Cocotb tests for JTAG (IEEE 1149.1) TAP Controller protocol engine.

Verifies:
1. JTAG TAP Reset & 32-bit IDCODE Readout:
   - Standard IDCODE (0x149511C3) read into R0..R3 with bit-level accuracy.
2. JTAG IDCODE Sweep:
   - Sweep across multiple 32-bit IDs (0x00000001, 0xDEADBEEF, 0x12345679, 0xCAFEBABF).
3. JTAG BYPASS Instruction & 1-bit Latency Propagation:
   - Shifts BYPASS instruction (0b1111) into IR.
   - Shifts test patterns through 1-bit BYPASS register, verifying exact 1-TCK delay.
4. JTAG TAP Recovery from Arbitrary States:
   - Verifies 5-pulse TMS=1 reset sequence recovers TAP from any state.
5. JTAG Bus Pin Isolation:
   - Verifies TCK/TMS/TDI are configured as outputs and TDO is strictly an input.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from jtag_model import (  # noqa: E402
    JtagTarget,
    build_jtag_read_idcode_asm,
    build_jtag_bypass_verify_asm,
    PAUSE_DR,
    SHIFT_IR,
)

TCK_PIN = 4
TMS_PIN = 5
TDI_PIN = 6
TDO_PIN = 7


async def _init_dut_and_bootload(dut, words: list[int]):
    """Fresh hardware reset and bootload."""
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = (1 << TDO_PIN)  # TDO pull-up
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = (1 << TDO_PIN)


@cocotb.test()
async def test_jtag_read_idcode_standard(dut):
    """Test JTAG 32-bit IDCODE readout into registers R0..R3 against JtagTarget."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    expected_idcode = 0x149511C3
    dut._log.info(f"Testing JTAG IDCODE Readout: expected = 0x{expected_idcode:08X}")

    asm = build_jtag_read_idcode_asm(
        tck_pin=TCK_PIN,
        tms_pin=TMS_PIN,
        tdi_pin=TDI_PIN,
        tdo_pin=TDO_PIN,
        half_period=2,
    )
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    target = JtagTarget(idcode=expected_idcode)
    core = dut.user_project.u_core

    tdo_val = 1
    for cycle in range(600):
        await FallingEdge(dut.clk)
        dut.uio_in.value = tdo_val << TDO_PIN

        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_out = int(dut.uio_out.value)
        tck = (uio_out >> TCK_PIN) & 1
        tms = (uio_out >> TMS_PIN) & 1
        tdi = (uio_out >> TDI_PIN) & 1

        tdo_val = target.step(tck, tms, tdi)

        if bool(core.halted.value):
            break
    else:
        raise AssertionError("JTAG IDCODE test timed out")

    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)
    r3 = int(core.r3.value)
    read_idcode = r0 | (r1 << 8) | (r2 << 16) | (r3 << 24)

    dut._log.info(
        f"JTAG IDCODE read complete: 0x{read_idcode:08X} "
        f"(R0=0x{r0:02X}, R1=0x{r1:02X}, R2=0x{r2:02X}, R3=0x{r3:02X})"
    )
    assert read_idcode == expected_idcode, (
        f"IDCODE mismatch: expected 0x{expected_idcode:08X}, got 0x{read_idcode:08X}"
    )


@cocotb.test()
async def test_jtag_read_idcode_sweep(dut):
    """Test JTAG IDCODE readout across multiple 32-bit device identities."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    test_ids = [0x00000001, 0xDEADBEEF, 0x12345679, 0xCAFEBABF]

    for expected_idcode in test_ids:
        dut._log.info(f"Testing JTAG IDCODE Sweep: 0x{expected_idcode:08X}")
        asm = build_jtag_read_idcode_asm(
            tck_pin=TCK_PIN,
            tms_pin=TMS_PIN,
            tdi_pin=TDI_PIN,
            tdo_pin=TDO_PIN,
            half_period=2,
        )
        words = assemble(asm)
        await _init_dut_and_bootload(dut, words)

        target = JtagTarget(idcode=expected_idcode)
        core = dut.user_project.u_core

        tdo_val = 1
        for cycle in range(600):
            await FallingEdge(dut.clk)
            dut.uio_in.value = tdo_val << TDO_PIN

            await RisingEdge(dut.clk)
            await ReadOnly()

            uio_out = int(dut.uio_out.value)
            tck = (uio_out >> TCK_PIN) & 1
            tms = (uio_out >> TMS_PIN) & 1
            tdi = (uio_out >> TDI_PIN) & 1

            tdo_val = target.step(tck, tms, tdi)

            if bool(core.halted.value):
                break
        else:
            raise AssertionError(f"JTAG IDCODE sweep timed out for 0x{expected_idcode:08X}")

        r0 = int(core.r0.value)
        r1 = int(core.r1.value)
        r2 = int(core.r2.value)
        r3 = int(core.r3.value)
        read_idcode = r0 | (r1 << 8) | (r2 << 16) | (r3 << 24)
        assert read_idcode == expected_idcode, (
            f"Sweep IDCODE mismatch: expected 0x{expected_idcode:08X}, got 0x{read_idcode:08X}"
        )


@cocotb.test()
async def test_jtag_bypass_register(dut):
    """Test JTAG BYPASS instruction programming and 1-cycle shift propagation."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    test_bytes = [0xA5, 0x5A, 0xFF, 0x00]

    for byte_val in test_bytes:
        expected_output = ((byte_val << 1) & 0xFF)  # 1 TCK cycle latency through 1-bit bypass
        dut._log.info(
            f"Testing JTAG BYPASS: input=0x{byte_val:02X}, expected output=0x{expected_output:02X}"
        )

        asm = build_jtag_bypass_verify_asm(
            test_byte=byte_val,
            tck_pin=TCK_PIN,
            tms_pin=TMS_PIN,
            tdi_pin=TDI_PIN,
            tdo_pin=TDO_PIN,
            half_period=2,
        )
        words = assemble(asm)
        await _init_dut_and_bootload(dut, words)

        target = JtagTarget()
        core = dut.user_project.u_core

        tdo_val = 1
        for cycle in range(600):
            await FallingEdge(dut.clk)
            dut.uio_in.value = tdo_val << TDO_PIN

            await RisingEdge(dut.clk)
            await ReadOnly()

            uio_out = int(dut.uio_out.value)
            tck = (uio_out >> TCK_PIN) & 1
            tms = (uio_out >> TMS_PIN) & 1
            tdi = (uio_out >> TDI_PIN) & 1

            tdo_val = target.step(tck, tms, tdi)

            if bool(core.halted.value):
                break
        else:
            raise AssertionError(f"JTAG BYPASS test timed out for byte 0x{byte_val:02X}")

        received_byte = int(core.r0.value)
        dut._log.info(
            f"JTAG BYPASS complete: input=0x{byte_val:02X}, received R0=0x{received_byte:02X}"
        )
        assert received_byte == expected_output, (
            f"BYPASS mismatch: expected 0x{expected_output:02X}, got 0x{received_byte:02X}"
        )


@cocotb.test()
async def test_jtag_tap_reset_recovery(dut):
    """Test JTAG reset recovery from arbitrary TAP states (PAUSE_DR, SHIFT_IR)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    arbitrary_states = [PAUSE_DR, SHIFT_IR]
    expected_idcode = 0x20261149

    for init_state in arbitrary_states:
        dut._log.info(f"Testing JTAG TAP Reset Recovery from state {init_state}")
        asm = build_jtag_read_idcode_asm(
            tck_pin=TCK_PIN,
            tms_pin=TMS_PIN,
            tdi_pin=TDI_PIN,
            tdo_pin=TDO_PIN,
            half_period=2,
        )
        words = assemble(asm)
        await _init_dut_and_bootload(dut, words)

        target = JtagTarget(idcode=expected_idcode)
        target.tap_state = init_state  # Force target into non-reset state
        core = dut.user_project.u_core

        tdo_val = 1
        for cycle in range(600):
            await FallingEdge(dut.clk)
            dut.uio_in.value = tdo_val << TDO_PIN

            await RisingEdge(dut.clk)
            await ReadOnly()

            uio_out = int(dut.uio_out.value)
            tck = (uio_out >> TCK_PIN) & 1
            tms = (uio_out >> TMS_PIN) & 1
            tdi = (uio_out >> TDI_PIN) & 1

            tdo_val = target.step(tck, tms, tdi)

            if bool(core.halted.value):
                break
        else:
            raise AssertionError(f"JTAG reset recovery timed out for initial state {init_state}")

        r0 = int(core.r0.value)
        r1 = int(core.r1.value)
        r2 = int(core.r2.value)
        r3 = int(core.r3.value)
        read_idcode = r0 | (r1 << 8) | (r2 << 16) | (r3 << 24)
        assert read_idcode == expected_idcode, (
            f"Reset recovery mismatch: expected 0x{expected_idcode:08X}, got 0x{read_idcode:08X}"
        )


@cocotb.test()
async def test_jtag_pin_isolation(dut):
    """Verify that TCK/TMS/TDI are configured as outputs and TDO is strictly input."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = build_jtag_read_idcode_asm(
        tck_pin=TCK_PIN,
        tms_pin=TMS_PIN,
        tdi_pin=TDI_PIN,
        tdo_pin=TDO_PIN,
        half_period=2,
    )
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core

    for cycle in range(50):
        await FallingEdge(dut.clk)
        dut.uio_in.value = 0

        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_oe = int(dut.uio_oe.value)
        # Check that TCK (pin 4), TMS (pin 5), TDI (pin 6) are outputs after setup
        if cycle > 10:
            assert (uio_oe & (1 << TCK_PIN)), "TCK is not output"
            assert (uio_oe & (1 << TMS_PIN)), "TMS is not output"
            assert (uio_oe & (1 << TDI_PIN)), "TDI is not output"
            # TDO (pin 7) MUST be an input (uio_oe bit 7 == 0)
            assert not (uio_oe & (1 << TDO_PIN)), "TDO was driven as output (bus contention risk!)"

        if bool(core.halted.value):
            break

    dut._log.info("JTAG pin direction isolation verified: TCK/TMS/TDI output, TDO strictly input.")
