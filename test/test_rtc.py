"""
Cocotb testbench for Real-Time Clock (RTC) & Sub-Nanosecond Fractional Hardware Timestamping Engine.
Part of the Jane Street Protocol Emulator Verification Suite.

Tests:
1. test_rtc_fractional_accumulator_accuracy: Fractional accumulator step and second rollover.
2. test_rtc_frequency_syntonization_ppb: Frequency syntonization drift compensation across ppb sweep.
3. test_rtc_sub_nanosecond_timestamp_capture: Sub-nanosecond TSU ingress/egress phase interpolation.
4. test_rtc_alarm_compare_match_trigger: Programmable alarm comparator matching and trigger status.
5. test_rtc_incore_microcode_execution: Synthesizable Verilog core microcode execution of timestamp capture.
6. test_rtc_ppa_metrics: Silicon PPA metrics assertion on IHP 130nm SG13G2.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer
import sys
from pathlib import Path

# Add repo root to import tools and test helpers
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from tools.rtc_model import (
    RtcEngine,
    FractionalAccumulator,
    TimestampingUnit,
    RtcTimestamp,
    get_rtc_ppa_metrics,
    get_incore_rtc_microcode,
)
from bootload import bootload


@cocotb.test()
async def test_rtc_fractional_accumulator_accuracy(dut):
    """Test 1: Verify fractional accumulator accuracy and second rollover."""
    dut._log.info("Starting Test 1: Fractional Accumulator Accuracy & Rollover")

    rtc = RtcEngine(f_sys_mhz=50.0)
    rtc.set_time(seconds=1700000000, nanoseconds=999_999_900)

    # 50 MHz clock has a nominal period of 20 ns
    # 5 cycles = 100 ns -> reaches exactly 1,000,000,000 ns -> rollover to 1700000001s, 0ns
    rtc.step_cycles(5)
    ts = rtc.get_current_timestamp()
    assert ts.seconds == 1700000001, f"Expected second rollover to 1700000001, got {ts.seconds}"
    assert ts.nanoseconds == 0, f"Expected 0 ns, got {ts.nanoseconds}"

    # Step another 15 cycles (300 ns)
    rtc.step_cycles(15)
    ts2 = rtc.get_current_timestamp()
    assert ts2.seconds == 1700000001, f"Expected second 1700000001, got {ts2.seconds}"
    assert ts2.nanoseconds == 300, f"Expected 300 ns, got {ts2.nanoseconds}"
    dut._log.info(f"Rollover test passed: {ts2}")


@cocotb.test()
async def test_rtc_frequency_syntonization_ppb(dut):
    """Test 2: Verify frequency syntonization drift compensation across ppb sweep."""
    dut._log.info("Starting Test 2: Frequency Syntonization Drift Compensation")

    nominal_rtc = RtcEngine(f_sys_mhz=50.0)
    nominal_rtc.set_frequency_tuning(0.0)

    # +100 ppm offset (+100,000 ppb)
    fast_rtc = RtcEngine(f_sys_mhz=50.0)
    fast_rtc.set_frequency_tuning(100_000.0)

    # -100 ppm offset (-100,000 ppb)
    slow_rtc = RtcEngine(f_sys_mhz=50.0)
    slow_rtc.set_frequency_tuning(-100_000.0)

    cycles = 50_000  # 1 ms of nominal time at 50 MHz (1,000,000 ns)
    nominal_rtc.step_cycles(cycles)
    fast_rtc.step_cycles(cycles)
    slow_rtc.step_cycles(cycles)

    nom_ns = nominal_rtc.get_current_timestamp().total_nanoseconds()
    fast_ns = fast_rtc.get_current_timestamp().total_nanoseconds()
    slow_ns = slow_rtc.get_current_timestamp().total_nanoseconds()

    # Nominal time should be exactly 1,000,000 ns (1 ms)
    assert abs(nom_ns - 1_000_000.0) < 1.0, f"Nominal time error: {nom_ns}"

    # Fast RTC should gain ~100 ns (+100 ppm of 1 ms)
    drift_fast = fast_ns - nom_ns
    assert 95.0 <= drift_fast <= 105.0, f"Expected ~100 ns drift, got {drift_fast} ns"

    # Slow RTC should lose ~100 ns (-100 ppm of 1 ms)
    drift_slow = nom_ns - slow_ns
    assert 95.0 <= drift_slow <= 105.0, f"Expected ~100 ns loss, got {drift_slow} ns"

    dut._log.info(f"Syntonization passed: nominal={nom_ns}ns, +100ppm={fast_ns}ns, -100ppm={slow_ns}ns")


@cocotb.test()
async def test_rtc_sub_nanosecond_timestamp_capture(dut):
    """Test 3: Verify sub-nanosecond TSU ingress/egress phase interpolation."""
    dut._log.info("Starting Test 3: Sub-Nanosecond TSU Phase Interpolation")

    rtc = RtcEngine(f_sys_mhz=50.0)
    rtc.set_time(seconds=1700000000, nanoseconds=500_000)
    tsu = TimestampingUnit(vernier_resolution_ps=156.25)

    test_phases = [0.0, 156.25, 312.5, 468.75, 625.0, 781.25, 937.5]

    for phase in test_phases:
        ts_rx = tsu.capture_rx(rtc, phase_offset_ps=phase)
        expected_ps = int(round(phase / 156.25) * 156.25) % 1000
        assert ts_rx.picoseconds == expected_ps, f"Phase {phase}ps mapped to {ts_rx.picoseconds}ps, expected {expected_ps}ps"

    # Verify egress capture
    ts_tx = tsu.capture_tx(rtc, phase_offset_ps=312.5)
    assert ts_tx.picoseconds == 312, f"Tx timestamp picoseconds error: {ts_tx.picoseconds}"
    assert tsu.rx_count == len(test_phases)
    assert tsu.tx_count == 1
    dut._log.info("TSU sub-nanosecond vernier interpolation verified across all test phases.")


@cocotb.test()
async def test_rtc_alarm_compare_match_trigger(dut):
    """Test 4: Verify programmable alarm comparator matching and trigger status."""
    dut._log.info("Starting Test 4: Programmable Alarm Compare Match")

    rtc = RtcEngine(f_sys_mhz=50.0)
    rtc.set_time(seconds=1700000000, nanoseconds=1000)

    # Configure alarm for 1700000000s, 1400ns
    rtc.set_alarm(seconds=1700000000, nanoseconds=1400)
    assert not rtc.alarm_triggered, "Alarm should not be triggered initially"

    # Step 10 cycles (200 ns) -> time becomes 1200 ns (still < 1400 ns)
    rtc.step_cycles(10)
    assert not rtc.alarm_triggered, "Alarm triggered prematurely at 1200 ns"

    # Step another 15 cycles (300 ns) -> time becomes 1500 ns (> 1400 ns)
    rtc.step_cycles(15)
    assert rtc.alarm_triggered, "Alarm failed to trigger at 1500 ns"
    dut._log.info("Alarm compare match trigger verified successfully.")


@cocotb.test()
async def test_rtc_incore_microcode_execution(dut):
    """Test 5: Verify synthesizable Verilog core executes timestamp capture microcode."""
    dut._log.info("Starting Test 5: Synthesizable RTL In-Core RTC Timestamp Execution")

    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Reset sequence
    await FallingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    microcode = get_incore_rtc_microcode()
    dut._log.info(f"Bootloading RTC microcode ({len(microcode)} words)...")
    await bootload(dut, microcode)

    # Run core execution until completion or timeout
    passed = False
    for cycle in range(60):
        await RisingEdge(dut.clk)
        try:
            uo_val = int(dut.uo_out.value)
            uio_val = int(dut.uio_out.value)
            oe_val = int(dut.uio_oe.value)
        except ValueError:
            continue

        # Microcode drives GDIRI 0xFF (oe=0xFF) and GWR R2 (uio_out=T1 > 0)
        if oe_val == 0xFF and uio_val > 0:
            dut._log.info(f"RTC microcode execution completed at cycle {cycle}: uio_out=0x{uio_val:02X}")
            passed = True
            break

    assert passed, f"RTC microcode execution timed out (oe=0x{int(dut.uio_oe.value):02X}, uio=0x{int(dut.uio_out.value):02X})"

    # Verify architectural registers in RTL
    core = dut.user_project.u_core
    r1_val = int(core.r1.value)
    r2_val = int(core.r2.value)
    r3_val = int(core.r3.value)

    dut._log.info(f"Core registers: R1(T0)={r1_val} (0x{r1_val:02X}), R2(T1)={r2_val} (0x{r2_val:02X}), R3={r3_val}")

    # R1 holds T0 (initial timestamp capture)
    # R2 and R3 hold T1 (timestamp capture after WAIT 8)
    # The elapsed cycles between WAITEDGE 0x18 and second WAITEDGE 0x18 must be strictly positive
    # (WAIT 8 + MOV + WAITEDGE = ~10-12 cycles)
    cycle_diff = (r2_val - r1_val) & 0xFF
    dut._log.info(f"Measured cycle difference between T1 and T0: {cycle_diff} cycles")
    assert r1_val > 0, f"Expected non-zero initial timestamp, got {r1_val}"
    assert 8 <= cycle_diff <= 14, f"Expected cycle diff ~10-12 (between 8 and 14), got {cycle_diff}"
    assert int(dut.uio_out.value) == r2_val, f"Pin uio_out (0x{int(dut.uio_out.value):02X}) must match R2 (0x{r2_val:02X})"
    dut._log.info("Synthesizable core verified: RTC timestamping captured real cycle counter progression!")


@cocotb.test()
async def test_rtc_ppa_metrics(dut):
    """Test 6: Verify RTC & Timestamping Engine silicon PPA metrics."""
    dut._log.info("Starting Test 6: Silicon PPA Metrics Validation")

    ppa = get_rtc_ppa_metrics()
    assert ppa["rtc_standard_cells"] <= 350, f"Excessive cells: {ppa['rtc_standard_cells']}"
    assert ppa["f_max_mhz"] >= 800.0, f"Insufficient Fmax: {ppa['f_max_mhz']} MHz"
    assert ppa["active_power_uw_per_mhz"] <= 2.5, f"Excessive power: {ppa['active_power_uw_per_mhz']} uW/MHz"
    assert ppa["timestamp_resolution_ps"] <= 250.0, f"Insufficient resolution: {ppa['timestamp_resolution_ps']} ps"
    assert ppa["tuning_resolution_ppb"] <= 0.5, f"Insufficient tuning: {ppa['tuning_resolution_ppb']} ppb"
    dut._log.info(f"PPA metrics qualified: {ppa}")
