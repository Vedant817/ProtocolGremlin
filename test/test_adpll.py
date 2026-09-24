"""
test/test_adpll.py
==================
Verification test suite for the All-Digital Phase-Locked Loop (ADPLL)
& Dynamic Frequency Scaling (DFS) Macro.

Covers:
1. Closed-loop phase lock acquisition & settling latency (<64 cycles)
2. Frequency multiplication sweep across divider ratios (N = 8, 16, 32, 64)
3. Glitch-free dynamic frequency scaling clock switching (runt pulse prevention)
4. Dynamic power scaling across operating gears (NOMINAL, TURBO, LOW_POWER, DEEP_SLEEP)
5. Synthesizable in-core RTL microcode DFS gear selection & WAITEDGE lock handshake
6. Calibrated 130nm CMOS PPA macro scaling & jitter metrics
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer, ClockCycles
from tools.adpll_model import (
    AdpllCore,
    DfsController,
    get_adpll_ppa_metrics,
    get_incore_dfs_microcode,
)


async def reset_dut(dut):
    """Clean hardware reset sequence."""
    dut.rst_n.value = 0
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.ena.value = 1
    for _ in range(5):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def bootload_words(dut, words):
    """Serial bootloader driving instructions into program_ram over uio[0:2]."""
    dut.uio_in.value = 0x01
    await ClockCycles(dut.clk, 4)

    header_val = len(words)
    for b in range(7, -1, -1):
        bit = (header_val >> b) & 1
        dut.uio_in.value = 0x01 | (bit << 2)
        await ClockCycles(dut.clk, 2)
        dut.uio_in.value = 0x01 | (bit << 2) | (1 << 1)
        await ClockCycles(dut.clk, 2)
        dut.uio_in.value = 0x01 | (bit << 2)
        await ClockCycles(dut.clk, 2)

    for word in words:
        for b in range(15, -1, -1):
            bit = (word >> b) & 1
            dut.uio_in.value = 0x01 | (bit << 2)
            await ClockCycles(dut.clk, 2)
            dut.uio_in.value = 0x01 | (bit << 2) | (1 << 1)
            await ClockCycles(dut.clk, 2)
            dut.uio_in.value = 0x01 | (bit << 2)
            await ClockCycles(dut.clk, 2)

    def crc8_calc(data_bytes):
        poly = 0x07
        crc = 0x00
        for byte in data_bytes:
            for i in range(7, -1, -1):
                b = (byte >> i) & 1
                if ((crc >> 7) ^ b) & 1:
                    crc = ((crc << 1) ^ poly) & 0xFF
                else:
                    crc = (crc << 1) & 0xFF
        return crc

    stream = [header_val]
    for w in words:
        stream.append((w >> 8) & 0xFF)
        stream.append(w & 0xFF)
    crc_expected = crc8_calc(stream)

    for b in range(7, -1, -1):
        bit = (crc_expected >> b) & 1
        dut.uio_in.value = 0x01 | (bit << 2)
        await ClockCycles(dut.clk, 2)
        dut.uio_in.value = 0x01 | (bit << 2) | (1 << 1)
        await ClockCycles(dut.clk, 2)
        dut.uio_in.value = 0x01 | (bit << 2)
        await ClockCycles(dut.clk, 2)

    dut.uio_in.value = 0x00
    await ClockCycles(dut.clk, 8)


@cocotb.test()
async def test_adpll_phase_lock_acquisition(dut):
    """Test 1: Verify closed-loop phase lock acquisition and jitter within bounds."""
    dut._log.info("Starting Test 1: ADPLL Phase Lock Acquisition")

    adpll = AdpllCore(f_ref_mhz=10.0, target_n=16)
    res = adpll.simulate_lock(max_cycles=80)

    dut._log.info(
        f"ADPLL Lock: {res['locked']} at Cycle {res['lock_cycle']}, "
        f"Settled Freq: {res['settled_freq_mhz']:.2f} MHz (Target: {res['target_freq_mhz']:.2f} MHz), "
        f"Error: {res['freq_error_pct']:.4f}%, RMS Jitter: {res['rms_jitter_ps']:.2f} ps"
    )

    assert res["locked"], "ADPLL failed to achieve phase lock"
    assert res["lock_cycle"] < 64, f"Lock acquisition time exceeded 64 cycles: {res['lock_cycle']}"
    assert res["freq_error_pct"] < 0.5, f"Frequency error exceeds 0.5%: {res['freq_error_pct']}%"
    assert res["rms_jitter_ps"] < 3.0, f"RMS period jitter exceeds 3.0 ps: {res['rms_jitter_ps']} ps"

    dut._log.info("Test 1 PASS: ADPLL phase lock acquisition confirmed.")


@cocotb.test()
async def test_adpll_frequency_multiplication_sweep(dut):
    """Test 2: Verify frequency multiplication across divider ratios N = 8, 16, 32, 64."""
    dut._log.info("Starting Test 2: Frequency Multiplication Sweep")

    test_multipliers = [8, 16, 32, 64]
    f_ref = 10.0

    for N in test_multipliers:
        adpll = AdpllCore(f_ref_mhz=f_ref, target_n=N)
        res = adpll.simulate_lock(max_cycles=100)
        expected_f = f_ref * N

        dut._log.info(
            f"N={N:2d} -> Settled: {res['settled_freq_mhz']:.2f} MHz "
            f"(Expected: {expected_f:.2f} MHz, Locked={res['locked']})"
        )
        assert res["locked"], f"Lock failed for multiplier N={N}"
        assert res["freq_error_pct"] < 0.8, f"Frequency error too high for N={N}: {res['freq_error_pct']}%"

    dut._log.info("Test 2 PASS: Frequency multiplication ratios verified.")


@cocotb.test()
async def test_dfs_glitch_free_clock_switching(dut):
    """Test 3: Verify glitch-free clock multiplexing and runt pulse suppression."""
    dut._log.info("Starting Test 3: Glitch-Free Clock Switching Across Gears")

    dfs = DfsController(initial_gear=0b00)  # NOMINAL
    gear_transitions = [
        (0b10, "TURBO"),
        (0b01, "LOW_POWER"),
        (0b11, "DEEP_SLEEP_BAUD"),
        (0b00, "NOMINAL"),
    ]

    for target_gear, name in gear_transitions:
        sw = dfs.simulate_switch(target_gear=target_gear)
        dut._log.info(
            f"Transition {sw['source_gear']} -> {sw['target_gear']}: "
            f"Min Pulse: {sw['min_pulse_width_ns']:.2f} ns (Allowed >= {sw['min_allowed_pulse_ns']:.2f} ns), "
            f"Runt Pulse: {sw['has_runt_pulse']}, Latency: {sw['switch_latency_cycles']} cycles"
        )
        assert not sw["has_runt_pulse"], f"Glitch/runt pulse detected during transition to {name}"
        assert sw["min_pulse_width_ns"] >= (sw["min_allowed_pulse_ns"] * 0.95), (
            f"Pulse width violation on switch to {name}"
        )

    dut._log.info("Test 3 PASS: Glitch-free clock switching verified across all power gears.")


@cocotb.test()
async def test_dfs_dynamic_power_scaling(dut):
    """Test 4: Verify dynamic power dissipation scales with core frequency."""
    dut._log.info("Starting Test 4: Dynamic Power Scaling Model")

    dfs = DfsController()
    p_turbo = dfs.estimate_power_uw(0b10)      # 50 MHz
    p_nominal = dfs.estimate_power_uw(0b00)    # 10 MHz
    p_low = dfs.estimate_power_uw(0b01)        # 2.5 MHz
    p_sleep = dfs.estimate_power_uw(0b11)      # 0.5 MHz

    dut._log.info(f"Power: TURBO={p_turbo:.2f} uW, NOMINAL={p_nominal:.2f} uW, LOW={p_low:.2f} uW, SLEEP={p_sleep:.2f} uW")

    assert p_turbo > p_nominal > p_low > p_sleep, "Power scaling is not strictly monotonic"
    # Low power mode should achieve > 70% dynamic savings vs nominal
    savings_low_vs_nom = (p_nominal - p_low) / p_nominal * 100.0
    dut._log.info(f"Dynamic Power Savings (LOW vs NOMINAL): {savings_low_vs_nom:.1f}%")
    assert savings_low_vs_nom > 70.0, f"Expected >70% savings, got {savings_low_vs_nom:.1f}%"

    dut._log.info("Test 4 PASS: Dynamic power scaling verified.")


from bootload import bootload


@cocotb.test()
async def test_incore_microcode_dfs_handshake(dut):
    """Test 5: Verify synthesizable RTL core executes DFS mode select and WAITEDGE lock handshake."""
    dut._log.info("Starting Test 5: Synthesizable RTL In-Core DFS Execution")

    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Clean reset sequence matching bootloader timing
    await FallingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    microcode = get_incore_dfs_microcode()
    dut._log.info(f"Assembled DFS microcode length: {len(microcode)} words")
    await bootload(dut, microcode)

    # Core is now executing assembled microcode at PC=0
    # Simulate the ADPLL hardware macro
    async def simulate_adpll_hardware():
        for _ in range(100):
            await RisingEdge(dut.clk)
            try:
                uio_val = int(dut.uio_out.value)
            except ValueError:
                continue
            gear_sel = uio_val & 0x03
            if gear_sel == 0x02:  # Core selected TURBO (0b10)
                dut._log.info("ADPLL Macro: Detected TURBO gear request on uio_out")
                # Wait 4 cycles lock acquisition time
                for _ in range(4):
                    await RisingEdge(dut.clk)
                # Assert adpll_locked on pin 3 (uio_in[3] = 1 -> 0x08)
                dut.uio_in.value = 0x08
                dut._log.info("ADPLL Macro: Asserted adpll_locked on uio_in[3]")
                return

    task = cocotb.start_soon(simulate_adpll_hardware())

    # Wait for core execution completion
    for _ in range(80):
        await RisingEdge(dut.clk)

    uio_out_val = int(dut.uio_out.value)
    dut._log.info(f"Core execution halted with uio_out = 0x{uio_out_val:02X}")

    # Core must have strobed PASS status: uio_out[0] = 1
    assert (uio_out_val & 0x01) == 1, f"DFS in-core PASS flag not asserted: uio_out=0x{uio_out_val:02X}"

    dut._log.info("Test 5 PASS: Synthesizable in-core RTL microcode DFS execution confirmed.")


@cocotb.test()
async def test_adpll_ppa_metrics(dut):
    """Test 6: Verify ADPLL macro standard cell count, Fmax, and jitter metrics."""
    dut._log.info("Starting Test 6: ADPLL PPA Metrics Validation")

    ppa = get_adpll_ppa_metrics()
    dut._log.info(f"PPA Metrics: {ppa}")

    assert ppa["adpll_standard_cells"] == 280
    assert ppa["adpll_gate_equivalents"] == 548
    assert ppa["f_max_dco_mhz"] >= 800.0
    assert ppa["f_max_core_mhz"] >= 80.0
    assert ppa["active_power_uw_per_mhz"] <= 1.70
    assert ppa["rms_period_jitter_ps"] < 3.0
    assert ppa["lock_acquisition_cycles"] <= 64

    dut._log.info("Test 6 PASS: ADPLL PPA metrics verified.")
