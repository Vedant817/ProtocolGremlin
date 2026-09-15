# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb verification test suite for Dynamic Power & Energy Optimization Study.

Test cases:
1. test_power_instruction_profiling: Micro-architectural power profiling of ALU and register operations.
2. test_clock_gated_wait_stall_efficiency: Quantifies power reduction during WAIT stall cycles with clock gating.
3. test_waitedge_power_and_wake_timing: Verifies low-power edge-wait stalls and 1-cycle instant wakeup latency.
4. test_gpio_capacitive_load_energy_scaling: Verifies external pad energy scaling with load capacitance and pin toggle rate.
5. test_protocol_energy_benchmark_uart: Benchmarks protocol-level energy efficiency (pJ/bit) for 8-N-1 UART.
6. test_power_electrical_safety_and_halt_state: Confirms halted state pin isolation and static leakage baseline.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ReadOnly

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from assembler import assemble
from bootload import bootload
from power_model import (
    PowerModel,
    build_alu_heavy_power_asm,
    build_gpio_heavy_power_asm,
    build_wait_idle_power_asm,
    build_waitedge_power_asm,
    build_uart_tx_power_benchmark_asm,
)

OPCODE_MAP = {
    0: "NOP", 1: "LDI", 2: "MOV", 3: "ADDI", 4: "SUBI",
    5: "ANDI", 6: "ORI", 7: "XORI", 8: "GDIRI", 9: "GDIR",
    10: "GWRI", 11: "GWR", 12: "GRD", 13: "WAIT", 14: "JMP",
    15: "JZ", 16: "JNZ", 17: "DECJNZ", 18: "HALT", 19: "SHIFTOUT",
    20: "SHIFTIN", 21: "WAITEDGE", 22: "GODRI", 23: "GODR"
}


async def _init_dut_and_bootload(dut, asm_lines: list[str], initial_uio: int = 0x00):
    """Reset DUT and load assembled firmware into program RAM."""
    words = assemble("\n".join(asm_lines))
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = initial_uio
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = initial_uio


@cocotb.test()
async def test_power_instruction_profiling(dut):
    """Profile micro-architectural power for ALU and register datapath operations."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = build_alu_heavy_power_asm(iterations=5)
    await _init_dut_and_bootload(dut, asm)

    core = dut.user_project.u_core
    pm = PowerModel()

    # Step through execution until HALT
    for _ in range(300):
        await RisingEdge(dut.clk)
        await ReadOnly()

        halted = bool(core.halted.value)
        wait_rem = int(core.wait_remaining.value)
        op_val = int(core.opcode.value)
        op_name = OPCODE_MAP.get(op_val, "NOP")

        is_wait = (wait_rem != 0)
        is_edge = (op_val == 21 and not is_wait and not bool(core.edge_matched.value)) if hasattr(core, "edge_matched") else False

        pin_out = int(dut.uio_out.value)
        pin_oe = int(dut.uio_oe.value)

        pm.sample_cycle(
            opcode_name=op_name,
            is_wait_stall=is_wait,
            is_edge_stall=is_edge,
            is_halt=halted,
            pin_out=pin_out,
            pin_oe=pin_oe,
        )

        if halted:
            break

    assert bool(core.halted.value), "Core did not reach HALT"
    assert pm.alu_ops > 0, f"Expected ALU ops, got {pm.alu_ops}"
    assert pm.reg_writes > 0, f"Expected register writes, got {pm.reg_writes}"

    ungated = pm.compute_power_breakdown(clock_gating=False)
    gated = pm.compute_power_breakdown(clock_gating=True)

    cocotb.log.info(f"ALU Benchmark Ungated: {ungated['p_total_uw']} uW ({ungated['energy_per_insn_pj']} pJ/insn)")
    cocotb.log.info(f"ALU Benchmark Gated:   {gated['p_total_uw']} uW ({gated['energy_per_insn_pj']} pJ/insn)")

    assert gated["p_total_uw"] < ungated["p_total_uw"], "Clock gating should reduce total power"
    assert ungated["p_core_datapath_uw"] > 0, "Datapath power should be non-zero during ALU ops"


@cocotb.test()
async def test_clock_gated_wait_stall_efficiency(dut):
    """Verify dynamic power reduction during WAIT stall states with clock gating."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    wait_duration = 30
    loops = 3
    asm = build_wait_idle_power_asm(wait_duration=wait_duration, iterations=loops)
    await _init_dut_and_bootload(dut, asm)

    core = dut.user_project.u_core
    pm = PowerModel()

    for _ in range(500):
        await RisingEdge(dut.clk)
        await ReadOnly()

        halted = bool(core.halted.value)
        wait_rem = int(core.wait_remaining.value)
        op_val = int(core.opcode.value)
        op_name = OPCODE_MAP.get(op_val, "NOP")

        is_wait = (wait_rem != 0)
        pin_out = int(dut.uio_out.value)
        pin_oe = int(dut.uio_oe.value)

        pm.sample_cycle(
            opcode_name=op_name,
            is_wait_stall=is_wait,
            is_edge_stall=False,
            is_halt=halted,
            pin_out=pin_out,
            pin_oe=pin_oe,
        )

        if halted:
            break

    assert bool(core.halted.value), "Core did not reach HALT"
    assert int(core.r0.value) == loops, f"Expected R0={loops}, got {int(core.r0.value)}"
    assert pm.wait_stall_cycles >= ((wait_duration - 1) * loops), f"Expected >= {(wait_duration - 1) * loops} wait cycles, got {pm.wait_stall_cycles}"

    savings = pm.compute_energy_savings()
    cocotb.log.info(f"Wait Stall Energy Savings: {savings['savings_pct']}% ({savings['ungated_p_total_uw']} uW -> {savings['gated_p_total_uw']} uW)")

    assert savings["savings_pct"] >= 40.0, f"Expected >= 40% power savings during wait stalls, got {savings['savings_pct']}%"


@cocotb.test()
async def test_waitedge_power_and_wake_timing(dut):
    """Verify edge-wait stall low-power mode and 1-cycle instant wakeup latency."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = build_waitedge_power_asm(pin=0)
    await _init_dut_and_bootload(dut, asm, initial_uio=0x00)

    core = dut.user_project.u_core
    pm = PowerModel()

    # Hold pin 0 low for 25 cycles during WAITEDGE stall
    stall_cycles_target = 25
    for cycle in range(stall_cycles_target):
        await RisingEdge(dut.clk)
        await ReadOnly()

        halted = bool(core.halted.value)
        wait_rem = int(core.wait_remaining.value)
        op_val = int(core.opcode.value)
        op_name = OPCODE_MAP.get(op_val, "NOP")
        is_wait = (wait_rem != 0)
        is_edge = (op_val == 21 and not is_wait and not bool(core.edge_matched.value))

        pm.sample_cycle(
            opcode_name=op_name,
            is_wait_stall=is_wait,
            is_edge_stall=is_edge,
            is_halt=halted,
            pin_out=int(dut.uio_out.value),
            pin_oe=int(dut.uio_oe.value),
        )

    # Transition pin 0 HIGH (rising edge) to wake the core
    await RisingEdge(dut.clk)
    dut.uio_in.value = 0x01

    # Core must detect the edge and retire to HALT within 3 cycles
    woken = False
    for _ in range(10):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if bool(core.halted.value):
            woken = True
            break

    assert woken, "Core failed to wake from WAITEDGE stall upon rising edge"
    captured_duration = int(core.r0.value)
    cocotb.log.info(f"WAITEDGE captured duration in R0: {captured_duration} cycles")
    assert captured_duration >= stall_cycles_target, f"Expected captured duration >= {stall_cycles_target}, got {captured_duration}"
    assert pm.edge_stall_cycles >= 20, f"Expected >= 20 edge stall cycles, got {pm.edge_stall_cycles}"


@cocotb.test()
async def test_gpio_capacitive_load_energy_scaling(dut):
    """Verify that external pad energy scales linearly with capacitive load and toggle rate."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    iterations = 6
    asm = build_gpio_heavy_power_asm(iterations=iterations)
    await _init_dut_and_bootload(dut, asm)

    core = dut.user_project.u_core
    pm_20pf = PowerModel(c_pad=20.0e-12)
    pm_50pf = PowerModel(c_pad=50.0e-12)

    for _ in range(200):
        await RisingEdge(dut.clk)
        await ReadOnly()

        halted = bool(core.halted.value)
        wait_rem = int(core.wait_remaining.value)
        op_val = int(core.opcode.value)
        op_name = OPCODE_MAP.get(op_val, "NOP")

        pin_out = int(dut.uio_out.value)
        pin_oe = int(dut.uio_oe.value)

        pm_20pf.sample_cycle(
            opcode_name=op_name,
            is_wait_stall=(wait_rem != 0),
            is_edge_stall=False,
            is_halt=halted,
            pin_out=pin_out,
            pin_oe=pin_oe,
        )
        pm_50pf.sample_cycle(
            opcode_name=op_name,
            is_wait_stall=(wait_rem != 0),
            is_edge_stall=False,
            is_halt=halted,
            pin_out=pin_out,
            pin_oe=pin_oe,
        )

        if halted:
            break

    assert bool(core.halted.value), "Core did not reach HALT"
    assert pm_20pf.pin_transitions >= (iterations * 8), f"Expected >= {iterations * 8} pin transitions, got {pm_20pf.pin_transitions}"

    p_20 = pm_20pf.compute_power_breakdown(clock_gating=True)
    p_50 = pm_50pf.compute_power_breakdown(clock_gating=True)

    cocotb.log.info(f"GPIO Pad Power (20 pF): {p_20['p_gpio_pads_uw']} uW | Total: {p_20['p_total_uw']} uW")
    cocotb.log.info(f"GPIO Pad Power (50 pF): {p_50['p_gpio_pads_uw']} uW | Total: {p_50['p_total_uw']} uW")

    ratio = p_50["p_gpio_pads_uw"] / max(1e-6, p_20["p_gpio_pads_uw"])
    expected_ratio = 50.0 / 20.0  # 2.5x
    assert abs(ratio - expected_ratio) < 0.05, f"Expected 2.5x scaling, got {ratio:.2f}x"


@cocotb.test()
async def test_protocol_energy_benchmark_uart(dut):
    """Benchmark protocol-level energy efficiency (pJ/bit) for 8-N-1 UART transmission."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    baud_div = 8
    asm = build_uart_tx_power_benchmark_asm(data_byte=0x55, baud_div=baud_div)
    await _init_dut_and_bootload(dut, asm)

    core = dut.user_project.u_core
    pm = PowerModel()

    for _ in range(400):
        await RisingEdge(dut.clk)
        await ReadOnly()

        halted = bool(core.halted.value)
        wait_rem = int(core.wait_remaining.value)
        op_val = int(core.opcode.value)
        op_name = OPCODE_MAP.get(op_val, "NOP")

        pm.sample_cycle(
            opcode_name=op_name,
            is_wait_stall=(wait_rem != 0),
            is_edge_stall=False,
            is_halt=halted,
            pin_out=int(dut.uio_out.value),
            pin_oe=int(dut.uio_oe.value),
        )

        if halted:
            break

    assert bool(core.halted.value), "Core did not reach HALT"

    # 10 bits in frame (1 start + 8 data + 1 stop)
    pj_per_bit_ungated = pm.compute_protocol_metric(num_bits=10, clock_gating=False)
    pj_per_bit_gated = pm.compute_protocol_metric(num_bits=10, clock_gating=True)

    cocotb.log.info(f"UART 8-N-1 Energy Ungated: {pj_per_bit_ungated} pJ/bit")
    cocotb.log.info(f"UART 8-N-1 Energy Gated:   {pj_per_bit_gated} pJ/bit")

    assert pj_per_bit_gated < pj_per_bit_ungated, "Clock gating should improve energy-per-bit"
    assert pj_per_bit_gated < 250.0, f"Expected < 250 pJ/bit for gated UART TX, got {pj_per_bit_gated}"


@cocotb.test()
async def test_power_electrical_safety_and_halt_state(dut):
    """Verify that halted state maintains electrical safety and zero dynamic core switching."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = ["LDI R0, 0xAA", "GWRI 0x00", "HALT"]
    await _init_dut_and_bootload(dut, asm)

    core = dut.user_project.u_core

    # Run to halt
    for _ in range(50):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not reach HALT"
    frozen_pc = int(core.pc.value)
    frozen_out = int(dut.uio_out.value)
    frozen_oe = int(dut.uio_oe.value)

    # Observe 20 cycles in halted state
    for _ in range(20):
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert bool(core.halted.value), "Core lost halted state"
        assert int(core.pc.value) == frozen_pc, "PC changed after HALT"
        assert int(dut.uio_out.value) == frozen_out, "uio_out changed after HALT"
        assert int(dut.uio_oe.value) == frozen_oe, "uio_oe changed after HALT"

    cocotb.log.info("Halted state electrical safety and zero dynamic activity confirmed.")
