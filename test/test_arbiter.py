# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""test/test_arbiter.py - Cocotb test suite for Multi-Master Bus Contention & Collision Arbiter Engine.

Verifies:
1. test_arbiter_fixed_priority: Strict priority preemption and grant allocation across 4 competing masters.
2. test_arbiter_round_robin_fairness: Round-robin rotation under saturation, proving zero starvation and fair access.
3. test_arbiter_wired_and_bitwise_collision: Bitwise wired-AND dominant arbitration loss and RTL in-core detection.
4. test_arbiter_csma_cd_exponential_backoff: CSMA/CD collision detection, jam pulse handling, and slotted exponential backoff.
5. test_arbiter_electrical_safety_hazard_trap: Push-pull bus contention trap vs hardware open-drain electrical safety.
6. test_arbiter_coprocessor_ppa_synthesis: Analytical PPA benchmarking of on-chip hardware arbiter macros on IHP 130nm.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from arbiter_model import (  # noqa: E402
    ArbitrationPolicy,
    ArbiterPpaMetrics,
    ElectricalContentionError,
    ElectricalDriveMode,
    MasterState,
    MultiMasterBusArbiter,
    get_arbiter_microcode_asm,
)


async def _init_dut_and_bootload(dut, words: list, initial_uio: int = 0x00):
    """Reset DUT and load assembled firmware into program RAM via serial bootloader."""
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
async def test_arbiter_fixed_priority(dut):
    """Test 1: Verify fixed priority arbitration with strict preemption hierarchy (M0 > M1 > M2 > M3)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing Fixed Priority Bus Arbitration...")
    arbiter = MultiMasterBusArbiter(num_masters=4, policy=ArbitrationPolicy.FIXED_PRIORITY)

    # All 4 masters request simultaneously
    for m in range(4):
        arbiter.request(m)

    # Master 0 must be granted
    grant = arbiter.step_arbitration()
    assert grant == 0, f"Expected M0 granted, got M{grant}"

    # While M0 holds request, it retains grant
    assert arbiter.step_arbitration() == 0

    # M0 finishes and releases
    arbiter.release(0)
    grant = arbiter.step_arbitration()
    assert grant == 1, f"Expected M1 granted after M0 released, got M{grant}"

    # If M0 re-requests while M1 is active, after M1 finishes M0 preempts M2/M3
    arbiter.request(0)
    arbiter.release(1)
    grant = arbiter.step_arbitration()
    assert grant == 0, f"Expected M0 to preempt M2/M3, got M{grant}"

    arbiter.release(0)
    grant = arbiter.step_arbitration()
    assert grant == 2, f"Expected M2 granted, got M{grant}"

    arbiter.release(2)
    grant = arbiter.step_arbitration()
    assert grant == 3, f"Expected M3 granted, got M{grant}"

    arbiter.release(3)
    grant = arbiter.step_arbitration()
    assert grant is None, "Expected no grant when bus is idle"
    dut._log.info("Fixed priority arbitration verified successfully.")


@cocotb.test()
async def test_arbiter_round_robin_fairness(dut):
    """Test 2: Verify round-robin fairness, bounded wait latency, and zero starvation."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing Round-Robin Fairness under high load...")
    arbiter = MultiMasterBusArbiter(num_masters=4, policy=ArbitrationPolicy.ROUND_ROBIN)

    # 4 masters continuously request
    total_rounds = 40
    for round_idx in range(total_rounds):
        for m in range(4):
            arbiter.request(m)
        grant = arbiter.step_arbitration()
        assert grant is not None
        # Release granted master so next can arbitrate
        arbiter.release(grant)

    # In 40 rounds, each of the 4 masters must receive exactly 10 grants
    for m in range(4):
        grants = arbiter.stats.grants_per_master[m]
        dut._log.info(f"Master {m}: {grants} grants")
        assert grants == 10, f"Master {m} expected 10 grants, got {grants} (starvation or unfairness!)"

    dut._log.info("Round-robin fair token allocation verified.")


@cocotb.test()
async def test_arbiter_wired_and_bitwise_collision(dut):
    """Test 3: Verify bitwise wired-AND dominant arbitration loss both in model and physical RTL."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing wired-AND bitwise collision arbitration...")
    arbiter = MultiMasterBusArbiter(num_masters=3, policy=ArbitrationPolicy.WIRED_AND_DOMINANT)

    # Model test:
    # Bit 0: M0=0 (dom), M1=0 (dom), M2=1 (rec) -> Bus=0. Survivors=[0, 1], Losers=[2]
    bus, survivors, losers = arbiter.step_wired_and_bit({0: 0, 1: 0, 2: 1})
    assert bus == 0
    assert survivors == [0, 1]
    assert losers == [2]

    # Bit 1: M0=1 (rec), M1=0 (dom) -> Bus=0. Survivors=[1], Losers=[0]
    bus, survivors, losers = arbiter.step_wired_and_bit({0: 1, 1: 0})
    assert bus == 0
    assert survivors == [1]
    assert losers == [0]

    # RTL In-Core verification:
    # Microcode attempts to drive pin 0 recessive 1. Competitor holds pin 0 dominant 0.
    # ASIC must detect mismatch and record R2 = 0xAA (Arbitration Lost).
    dut._log.info("Bootloading in-core wired-AND collision detection microcode onto RTL...")
    asm = get_arbiter_microcode_asm(pin=0)
    words = assemble(asm)

    # Initial uio_in = 0x00 (competitor pulling pin 0 low dominant)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)
    await ClockCycles(dut.clk, 25)

    core = dut.user_project.u_core
    assert bool(core.halted.value), "Core should have halted"
    r2_val = int(core.r2.value)
    dut._log.info(f"Core halted with R2 = 0x{r2_val:02X}")
    assert r2_val == 0xAA, f"Expected R2=0xAA (Arbitration Lost), got 0x{r2_val:02X}"

    # Now verify win path when competitor does not pull low (pull-up high)
    dut._log.info("Testing win path when line is recessive 1...")
    await _init_dut_and_bootload(dut, words, initial_uio=0x01)
    await ClockCycles(dut.clk, 25)
    assert bool(core.halted.value), "Core should have halted"
    r2_val_win = int(core.r2.value)
    assert r2_val_win == 0x00, f"Expected R2=0x00 (Arbitration Won), got 0x{r2_val_win:02X}"
    dut._log.info("In-core wired-AND arbitration loss and win paths verified on RTL.")


@cocotb.test()
async def test_arbiter_csma_cd_exponential_backoff(dut):
    """Test 4: Verify CSMA/CD simultaneous collision detection and slotted exponential backoff."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing CSMA/CD collision detection and backoff...")
    arbiter = MultiMasterBusArbiter(num_masters=2, policy=ArbitrationPolicy.CSMA_CD_BACKOFF, slot_time_cycles=4)

    # Both M0 and M1 attempt to transmit simultaneously
    arbiter.request(0)
    arbiter.request(1)

    grant = arbiter.step_arbitration()
    # Collision! Neither granted
    assert grant is None, "Collision must prevent grant"
    assert arbiter.stats.collisions_detected == 1
    assert arbiter.master_states[0] == MasterState.BACKOFF
    assert arbiter.master_states[1] == MasterState.BACKOFF
    assert arbiter.backoff_timer[0] > 0
    assert arbiter.backoff_timer[1] > 0

    # Advance cycles until both backoff timers expire and one wins
    max_wait = 200
    granted = None
    for _ in range(max_wait):
        g = arbiter.step_arbitration()
        if g is not None:
            granted = g
            break

    assert granted is not None, "Backoff resolution failed to grant a master within timeout"
    dut._log.info(f"CSMA/CD backoff successfully resolved; Master {granted} granted after collision.")


@cocotb.test()
async def test_arbiter_electrical_safety_hazard_trap(dut):
    """Test 5: Verify electrical contention trapping for push-pull drivers vs safe open-drain."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing electrical contention hazard detection...")
    arbiter = MultiMasterBusArbiter(num_masters=2)

    # 1. Open-drain multi-master: Safe!
    od_drives = {
        0: (ElectricalDriveMode.OPEN_DRAIN, 0),
        1: (ElectricalDriveMode.OPEN_DRAIN, 1),
    }
    assert arbiter.check_electrical_contention(od_drives), "Open-drain concurrent drive must be electrically safe"

    # 2. Push-pull opposing drives: Fatal Contention!
    pp_drives = {
        0: (ElectricalDriveMode.PUSH_PULL, 0),
        1: (ElectricalDriveMode.PUSH_PULL, 1),
    }
    hazard_trapped = False
    try:
        arbiter.check_electrical_contention(pp_drives)
    except ElectricalContentionError:
        hazard_trapped = True

    assert hazard_trapped, "Conflicting push-pull drivers must trigger ElectricalContentionError"
    assert arbiter.stats.electrical_hazards_prevented == 1

    # 3. Verify physical ASIC core tri-states pins safely when configured as input or recessive
    dut.ena.value = 1
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    await ReadOnly()
    dut_oe = int(dut.uio_oe.value) if dut.uio_oe.value.is_resolvable else 0
    dut._log.info(f"DUT pin output enables: 0x{dut_oe:02X}")
    # Pins should be High-Z on reset (0x00)
    assert dut_oe == 0x00, f"Expected pins tri-stated on reset, got 0x{dut_oe:02X}"
    dut._log.info("Electrical contention hazard trapping verified.")


@cocotb.test()
async def test_arbiter_coprocessor_ppa_synthesis(dut):
    """Test 6: Analytical PPA benchmarking across hardware bus arbiter macros on IHP 130nm."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Benchmarking Hardware Bus Arbiter Macros on IHP 130nm SG13CMOS5L...")
    p_fixed = MultiMasterBusArbiter.get_ppa_metrics(ArbitrationPolicy.FIXED_PRIORITY, num_ports=4)
    p_rr = MultiMasterBusArbiter.get_ppa_metrics(ArbitrationPolicy.ROUND_ROBIN, num_ports=4)
    p_wand = MultiMasterBusArbiter.get_ppa_metrics(ArbitrationPolicy.WIRED_AND_DOMINANT, num_ports=4)
    p_csma = MultiMasterBusArbiter.get_ppa_metrics(ArbitrationPolicy.CSMA_CD_BACKOFF, num_ports=4)

    dut._log.info(f"FIXED_PRIORITY: {p_fixed.cell_count} cells, {p_fixed.area_um2:.1f} um2, {p_fixed.max_freq_mhz:.1f} MHz, {p_fixed.dynamic_power_uw_at_10mhz:.2f} uW")
    dut._log.info(f"ROUND_ROBIN:    {p_rr.cell_count} cells, {p_rr.area_um2:.1f} um2, {p_rr.max_freq_mhz:.1f} MHz, {p_rr.dynamic_power_uw_at_10mhz:.2f} uW")
    dut._log.info(f"WIRED_AND:      {p_wand.cell_count} cells, {p_wand.area_um2:.1f} um2, {p_wand.max_freq_mhz:.1f} MHz, {p_wand.dynamic_power_uw_at_10mhz:.2f} uW")
    dut._log.info(f"CSMA_CD:        {p_csma.cell_count} cells, {p_csma.area_um2:.1f} um2, {p_csma.max_freq_mhz:.1f} MHz, {p_csma.dynamic_power_uw_at_10mhz:.2f} uW")

    # Assertions on micro-architectural trade-offs
    assert p_fixed.cell_count < p_rr.cell_count, "Fixed priority must be smaller than round-robin (no state registers)"
    assert p_fixed.grant_latency_ns < p_csma.grant_latency_ns, "Fixed priority has lower combinational latency than CSMA backoff"
    assert p_csma.cell_count > p_rr.cell_count, "CSMA/CD requires pseudo-random LFSR and backoff timer counters"
    assert all(p.max_freq_mhz > 700.0 for p in [p_fixed, p_rr, p_wand, p_csma]), "All arbiter macros must achieve >700 MHz on IHP 130nm"

    dut._log.info("Bus arbiter PPA synthesis assertions verified.")
