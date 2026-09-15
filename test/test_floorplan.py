# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb verification test suite for Physical Die Floorplan, Pad Placement & Package Pinout.

Test cases:
1. test_floorplan_sso_ground_bounce: Verifies simultaneous 8-pin switching stability.
2. test_floorplan_pin_isolation_crosstalk: Verifies adjacent pin drive isolation.
3. test_floorplan_sso_analytical_model: Verifies analytical SSO bounce against noise margins.
4. test_floorplan_ir_drop_budget: Verifies PDN IR drop budget across power mesh.
5. test_floorplan_cross_talk_budget: Verifies adjacent pin cross-talk coupling margins.
6. test_floorplan_electrical_safety_and_highz: Confirms complete High-Z bus return on halt.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ReadOnly

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from assembler import assemble
from bootload import bootload
from floorplan_model import (
    SsoGroundBounceModel,
    IrDropModel,
    CrossTalkModel,
    build_sso_stress_test_asm,
    build_pin_isolation_matrix_asm,
)


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
async def test_floorplan_sso_ground_bounce(dut):
    """Verify simultaneous switching output across all 8 pins without core disruption."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = build_sso_stress_test_asm(iterations=4)
    await _init_dut_and_bootload(dut, asm)

    core = dut.user_project.u_core

    sso_high_seen = False
    sso_low_seen = False

    for _ in range(80):
        await RisingEdge(dut.clk)
        await ReadOnly()
        val = int(dut.uio_out.value)
        oe = int(dut.uio_oe.value)
        if oe == 0xFF:
            if val == 0xFF:
                sso_high_seen = True
            elif val == 0x00:
                sso_low_seen = True
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not reach HALT"
    assert sso_high_seen, "Did not observe simultaneous 8-pin HIGH drive"
    assert sso_low_seen, "Did not observe simultaneous 8-pin LOW drive"
    assert int(dut.uio_oe.value) == 0x00, "Pins did not release to High-Z on exit"
    cocotb.log.info("SSO stress test verified: 8-pin simultaneous switching passed with zero lockup.")


@cocotb.test()
async def test_floorplan_pin_isolation_crosstalk(dut):
    """Verify adjacent pin drive isolation (aggressor pin 0 vs victim pin 1)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    aggressor = 0
    victim = 1
    asm = build_pin_isolation_matrix_asm(aggressor_pin=aggressor, victim_pin=victim)
    await _init_dut_and_bootload(dut, asm, initial_uio=0x02)

    core = dut.user_project.u_core

    for _ in range(60):
        await RisingEdge(dut.clk)
        await ReadOnly()
        oe = int(dut.uio_oe.value)
        # Victim pin (bit 1) must NEVER be enabled as output
        assert (oe & (1 << victim)) == 0, f"Victim pin {victim} was inadvertently driven: oe=0x{oe:02X}"
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not reach HALT"
    cocotb.log.info("Adjacent pin drive isolation confirmed.")


@cocotb.test()
async def test_floorplan_sso_analytical_model(dut):
    """Verify analytical SSO bounce model results against noise margin specs."""
    bounce_report = SsoGroundBounceModel.calculate_bounce(
        n_switching_pins=8,
        drive_ma=8.0,
        trise_ns=2.0,
        l_eff_nh=2.0,
    )
    cocotb.log.info(f"SSO Ground Bounce Report: {bounce_report}")

    assert bounce_report["margin_preserved"] is True, "SSO bounce exceeds noise margin!"
    assert bounce_report["v_bounce_mv"] <= 100.0, f"Bounce too high: {bounce_report['v_bounce_mv']} mV"
    assert bounce_report["pct_vdd"] < 5.0, f"Bounce exceeds 5% VDD: {bounce_report['pct_vdd']}%"


@cocotb.test()
async def test_floorplan_ir_drop_budget(dut):
    """Verify Power Distribution Network (PDN) IR drop budget."""
    ir_report = IrDropModel.calculate_ir_drop(
        peak_current_ma=1.85,
        grid_resistance_ohms=4.2,
        vdd_v=1.8,
    )
    cocotb.log.info(f"IR Drop Report: {ir_report}")

    assert ir_report["budget_pass"] is True, "IR drop exceeds 3% VDD budget!"
    assert ir_report["v_drop_mv"] < 15.0, f"IR drop {ir_report['v_drop_mv']} mV exceeds threshold"


@cocotb.test()
async def test_floorplan_cross_talk_budget(dut):
    """Verify adjacent pin cross-talk capacitive coupling margins."""
    crosstalk_report = CrossTalkModel.calculate_coupling(
        v_swing_v=3.3,
        c_mutual_pf=0.15,
        c_load_pf=30.0,
    )
    cocotb.log.info(f"Cross-Talk Report: {crosstalk_report}")

    assert crosstalk_report["cross_talk_safe"] is True, "Cross-talk voltage exceeds safe margin!"
    assert crosstalk_report["v_coupled_mv"] < 25.0, f"Coupled voltage {crosstalk_report['v_coupled_mv']} mV too high"


@cocotb.test()
async def test_floorplan_electrical_safety_and_highz(dut):
    """Confirm complete High-Z bus return on halt."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm = build_sso_stress_test_asm(iterations=2)
    await _init_dut_and_bootload(dut, asm)

    core = dut.user_project.u_core

    for _ in range(60):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not reach HALT"
    assert int(dut.uio_oe.value) == 0x00, "uio_oe must be 0x00 after safe_exit"
    cocotb.log.info("Floorplan electrical High-Z safety confirmed.")
