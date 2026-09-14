# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
test/test_bootload.py - Comprehensive verification of on-chip bootloader integrity checking.

Verifies:
1. Valid CRC-8 load frame execution and clean boot status on uo_out (0x01).
2. Inverted CRC-8 detection: assertion of boot_err (uo_out=0x03) and permanent halt.
3. Single-bit corrupted code payload detection via CRC mismatch.
4. Truncated frame detection upon early LOAD_REQ deassertion.
5. Warm boot / skip load bypass when LOAD_REQ=0 at reset.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import _send_bit, bootload, compute_crc8  # noqa: E402


@cocotb.test()
async def test_bootload_clean_crc(dut):
    """Verify that a valid bootloader frame with matching CRC-8 executes to HALT with uo_out=0x01."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm_src = """
        LDI R0, 0x42
        LDI R1, 0x99
        HALT
    """
    words = assemble(asm_src)

    # Reset DUT
    await FallingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    # Bootload with correct CRC-8
    await bootload(dut, words)

    core = dut.user_project.u_core

    # Let program execute to HALT
    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    else:
        raise AssertionError("Clean program did not halt within 50 cycles")

    await ReadOnly()
    uo = int(dut.uo_out.value)
    # uo_out[0] = boot_done (1), uo_out[1] = boot_err (0) -> 0x01
    assert uo == 0x01, f"Expected uo_out=0x01 (done=1, err=0), got 0x{uo:02X}"
    assert int(core.r0.value) == 0x42, f"Expected R0=0x42, got 0x{int(core.r0.value):02X}"
    assert int(core.r1.value) == 0x99, f"Expected R1=0x99, got 0x{int(core.r1.value):02X}"
    dut._log.info("Bootloader Valid CRC-8: PASS (uo_out=0x%02X, R0=0x42, R1=0x99)", uo)


@cocotb.test()
async def test_bootload_corrupted_crc(dut):
    """Verify that a frame with an inverted CRC-8 asserts boot_err (0x03) and permanently halts."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm_src = """
        LDI R0, 0xFF
        HALT
    """
    words = assemble(asm_src)

    await FallingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    # Bootload with corrupted (inverted) CRC
    await bootload(dut, words, corrupt_crc=True)

    core = dut.user_project.u_core

    # Allow a few cycles post-bootload
    for _ in range(10):
        await RisingEdge(dut.clk)

    await ReadOnly()
    uo = int(dut.uo_out.value)
    # uo_out[0] = boot_done (1), uo_out[1] = boot_err (1) -> 0x03
    assert uo == 0x03, f"Expected uo_out=0x03 (done=1, err=1), got 0x{uo:02X}"
    assert bool(core.halted.value), "Core did not halt on CRC mismatch"
    assert int(core.pc.value) == 0, f"Core advanced PC ({int(core.pc.value)}) despite CRC error"
    assert int(core.r0.value) == 0, f"Core executed code (R0=0x{int(core.r0.value):02X}) despite CRC error"
    dut._log.info("Bootloader Corrupted CRC Rejection: PASS (uo_out=0x%02X, execution aborted)", uo)


@cocotb.test()
async def test_bootload_corrupted_code_payload(dut):
    """Verify single-bit corrupted payload bit triggers CRC mismatch and aborts execution."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm_src = """
        LDI R0, 0x12
        LDI R1, 0x34
        HALT
    """
    words = assemble(asm_src)
    # Compute CRC over true words
    true_crc = compute_crc8(words)

    # Corrupt 1 bit in words transmitted to DUT
    corrupted_words = list(words)
    corrupted_words[0] ^= 0x0001

    await FallingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    # Send corrupted words with true_crc (which now mismatches!)
    uio_val = 1
    dut.uio_in.value = uio_val
    for _ in range(3):
        await RisingEdge(dut.clk)

    count = len(corrupted_words)
    for bit_index in range(7, -1, -1):
        uio_val = await _send_bit(dut, uio_val, (count >> bit_index) & 1)

    for word in corrupted_words:
        for bit_index in range(15, -1, -1):
            uio_val = await _send_bit(dut, uio_val, (word >> bit_index) & 1)

    # Send original true_crc
    for bit_index in range(7, -1, -1):
        uio_val = await _send_bit(dut, uio_val, (true_crc >> bit_index) & 1)

    uio_val &= ~1
    dut.uio_in.value = uio_val
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)

    core = dut.user_project.u_core
    for _ in range(10):
        await RisingEdge(dut.clk)

    await ReadOnly()
    uo = int(dut.uo_out.value)
    assert uo == 0x03, f"Expected uo_out=0x03 (err=1), got 0x{uo:02X}"
    assert bool(core.halted.value), "Core did not halt on payload CRC corruption"
    assert int(core.r0.value) == 0, "Corrupted payload executed"
    dut._log.info("Bootloader Payload Bit Corruption Detection: PASS (uo_out=0x%02X)", uo)


@cocotb.test()
async def test_bootload_truncated_frame(dut):
    """Verify that deasserting LOAD_REQ prematurely aborts with boot_err=1."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm_src = """
        LDI R0, 0x11
        LDI R1, 0x22
        LDI R2, 0x33
        HALT
    """
    words = assemble(asm_src)

    await FallingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    # Abort load after only 1 word transmitted
    await bootload(dut, words, abort_early_after_words=1)

    core = dut.user_project.u_core
    for _ in range(10):
        await RisingEdge(dut.clk)

    await ReadOnly()
    uo = int(dut.uo_out.value)
    assert uo == 0x03, f"Expected uo_out=0x03 on truncation, got 0x{uo:02X}"
    assert bool(core.halted.value), "Core did not halt on truncated frame"
    assert int(core.pc.value) == 0, "Core advanced PC on truncated frame"
    dut._log.info("Bootloader Truncated Frame Detection: PASS (uo_out=0x%02X, aborted)", uo)


@cocotb.test()
async def test_bootload_warm_boot_skip(dut):
    """Verify that releasing reset with LOAD_REQ=0 enters LD_DONE with boot_err=0."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    await FallingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0  # LOAD_REQ is 0!
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    # Wait for settle counter
    for _ in range(10):
        await RisingEdge(dut.clk)

    await ReadOnly()
    uo = int(dut.uo_out.value)
    # boot_done = 1, boot_err = 0 -> 0x01
    assert uo == 0x01, f"Expected uo_out=0x01 for warm boot, got 0x{uo:02X}"
    dut._log.info("Bootloader Warm Boot Skip: PASS (uo_out=0x%02X)", uo)
