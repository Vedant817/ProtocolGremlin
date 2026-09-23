# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Isolated unit tests for individual ISA opcodes.

Directly exercises each instruction in isolation with known stimuli and assertions,
closing the gap where opcodes were previously only tested indirectly via loop_demo.asm.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402


async def _setup_and_load(dut, asm_source: str, external_uio_in: int = 0):
    words = assemble(asm_source)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    assert int(dut.uio_oe.value) == 0, f"GPIO must be high-Z on reset, got {dut.uio_oe.value}"
    dut.rst_n.value = 1

    await bootload(dut, words)
    dut.uio_in.value = external_uio_in
    await ReadOnly()
    return dut.user_project.u_core


async def _run_to_halt(dut, max_cycles: int = 200):
    core = dut.user_project.u_core
    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if bool(core.halted.value):
            return
    raise AssertionError(f"Core did not halt within {max_cycles} cycles")


@cocotb.test()
async def test_opcode_alu_and_regs(dut):
    """Test LDI, MOV, ADDI, SUBI, ANDI, ORI, XORI, NOP and Z flag."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = """
        NOP
        LDI R0, 0x10
        LDI R1, 0x20
        MOV R2, R0      ; R2 = 0x10
        ADDI R2, 0x05   ; R2 = 0x15
        SUBI R2, 0x15   ; R2 = 0x00, Z should be 1
        JZ zero_ok
        HALT            ; fail if branch not taken
    zero_ok:
        LDI R3, 0xAA
        ANDI R3, 0x0F   ; R3 = 0x0A
        ORI  R3, 0x50   ; R3 = 0x5A
        XORI R3, 0x5A   ; R3 = 0x00, Z should be 1
        HALT
    """
    core = await _setup_and_load(dut, asm)
    await _run_to_halt(dut)

    assert int(core.r0.value) == 0x10, f"R0: {int(core.r0.value)}"
    assert int(core.r1.value) == 0x20, f"R1: {int(core.r1.value)}"
    assert int(core.r2.value) == 0x00, f"R2: {int(core.r2.value)}"
    assert int(core.r3.value) == 0x00, f"R3: {int(core.r3.value)}"
    assert bool(core.z.value) is True, "Z flag should be set"


@cocotb.test()
async def test_opcode_branches_and_loop(dut):
    """Test JMP, JZ, JNZ, DECJNZ."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = """
        LDI R0, 5
        LDI R1, 0
    loop:
        ADDI R1, 2
        DECJNZ R0, loop  ; loop 5 times -> R1 should become 10 (0x0A)
        
        ; Test JNZ taken
        LDI R2, 1        ; Z=0
        JNZ jnz_target
        HALT
    jnz_target:
        ; Test JMP
        JMP finish
        LDI R1, 0xFF     ; should be skipped
    finish:
        HALT
    """
    core = await _setup_and_load(dut, asm)
    await _run_to_halt(dut)

    assert int(core.r0.value) == 0, f"R0 should be 0, got {int(core.r0.value)}"
    assert int(core.r1.value) == 10, f"R1 should be 10, got {int(core.r1.value)}"


@cocotb.test()
async def test_opcode_gpio_and_shifts(dut):
    """Test GDIRI, GDIR, GWRI, GWR, GRD, SHIFTOUT, SHIFTIN, WAIT."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = """
        ; Test GDIRI and GWRI
        GDIRI 0x0F
        GWRI  0x05
        
        ; Test GDIR and GWR
        LDI R0, 0x33
        GDIR R0
        LDI R1, 0x12
        GWR R1
        
        ; Test GRD from external input (set to 0x84)
        GRD R2
        
        ; Test SHIFTOUT pin 0: transmit LSB of 0x55 (1)
        LDI R3, 0x55
        SHIFTOUT R3, 0  ; pin 0 gets 1, R3 becomes 0x2A
        
        ; Test SHIFTIN pin 7: shift in bit 7 of external input (which is 1)
        SHIFTIN R3, 7   ; R3 becomes (1 << 7) | (0x2A >> 1) = 0x80 | 0x15 = 0x95
        
        WAIT 3
        HALT
    """
    core = await _setup_and_load(dut, asm, external_uio_in=0x84)
    await _run_to_halt(dut)

    assert int(dut.uio_oe.value) == 0x33, f"uio_oe: {int(dut.uio_oe.value)}"
    assert int(core.r2.value) == 0x84, f"R2 (GRD): {hex(int(core.r2.value))}"
    assert int(core.r3.value) == 0x95, f"R3 (SHIFTIN): {hex(int(core.r3.value))}"
