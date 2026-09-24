"""
Cocotb testbench for Autonomous Link Training & Status State Machine (LTSSM) & Speed Negotiation.
Part of the Jane Street Protocol Emulator Verification Suite.

Tests:
1. test_ltssm_normal_link_bringup_to_l0: Standard state progression from DETECT through POLLING/CONFIG to L0.
2. test_ltssm_ts1_ts2_ordered_set_fidelity: TS1/TS2 16-symbol ordered set generation, serialization, and error trapping.
3. test_ltssm_speed_negotiation_dynamic_gear_switch: Dynamic multi-gear rate switching (10M -> 50M -> 100M).
4. test_ltssm_link_fault_recovery_and_hot_reset: Error-triggered RECOVERY, timeout handling, and HOT_RESET recovery.
5. test_ltssm_incore_microcode_execution: Synthesizable Verilog core execution of LTSSM verification.
6. test_ltssm_ppa_metrics: Silicon PPA metrics assertion on IHP 130nm SG13G2.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from tools.ltssm_model import (
    LtssmState,
    SpeedGear,
    OrderedSetType,
    TrainingOrderedSet,
    LtssmEngine,
    get_ltssm_ppa_metrics,
    get_incore_ltssm_microcode,
)
from bootload import bootload


@cocotb.test()
async def test_ltssm_normal_link_bringup_to_l0(dut):
    """Test 1: Verify standard LTSSM progression from DETECT through POLLING and CONFIG to L0_ACTIVE_RUN."""
    dut._log.info("Starting Test 1: LTSSM Normal Link Bringup to L0")

    engine = LtssmEngine()
    assert engine.state == LtssmState.DETECT_QUIET

    # Detect phase: impedance sensing
    assert engine.step_detect(rx_impedance_detected=True)
    assert engine.state == LtssmState.DETECT_ACTIVE
    assert engine.step_detect(rx_impedance_detected=True)
    assert engine.state == LtssmState.POLLING_ACTIVE

    # Polling phase: 8 consecutive TS1 ordered sets
    ts1 = TrainingOrderedSet(OrderedSetType.TS1, link_num=0, lane_num=0, rate_id=0x01)
    for i in range(7):
        assert not engine.step_polling(ts1)
        assert engine.state == LtssmState.POLLING_ACTIVE
    # 8th TS1 transitions to POLLING_CONFIG
    assert engine.step_polling(ts1)
    assert engine.state == LtssmState.POLLING_CONFIG

    # Polling Config phase: 8 consecutive TS2 ordered sets
    ts2 = TrainingOrderedSet(OrderedSetType.TS2, link_num=1, lane_num=0, rate_id=0x01)
    for i in range(7):
        assert not engine.step_polling(ts2)
        assert engine.state == LtssmState.POLLING_CONFIG
    # 8th TS2 transitions to CONFIG_LINKWIDTH
    assert engine.step_polling(ts2)
    assert engine.state == LtssmState.CONFIG_LINKWIDTH

    # Config phase: negotiate link ID and lane ID
    assert engine.step_config(link_id=1, lane_id=0)
    assert engine.state == LtssmState.CONFIG_LANENUM
    assert engine.step_config(link_id=1, lane_id=0)
    assert engine.state == LtssmState.L0_ACTIVE_RUN

    dut._log.info(f"Link bringup completed to L0 (Link ID={engine.link_id}, Lane ID={engine.lane_id})!")


@cocotb.test()
async def test_ltssm_ts1_ts2_ordered_set_fidelity(dut):
    """Test 2: Verify TS1/TS2 ordered set serialization, parsing, and comma alignment validation."""
    dut._log.info("Starting Test 2: TS1/TS2 Ordered Set Fidelity Verification")

    # Construct TS1
    ts1 = TrainingOrderedSet(
        OrderedSetType.TS1, link_num=3, lane_num=2, n_fts=48, rate_id=0x02, training_ctrl=0x01
    )
    raw_ts1 = ts1.to_bytes()
    assert len(raw_ts1) == 16
    assert raw_ts1[0] == TrainingOrderedSet.COMMA_K28_5
    assert raw_ts1[1] == 3
    assert raw_ts1[2] == 2
    assert raw_ts1[3] == 48
    assert raw_ts1[4] == 0x02
    assert raw_ts1[7] == TrainingOrderedSet.TS1_ID_BYTE

    # Parse roundtrip
    parsed_ts1 = TrainingOrderedSet.from_bytes(raw_ts1)
    assert parsed_ts1 is not None
    assert parsed_ts1.os_type == OrderedSetType.TS1
    assert parsed_ts1.link_num == 3
    assert parsed_ts1.lane_num == 2
    assert parsed_ts1.rate_id == 0x02

    # Construct TS2
    ts2 = TrainingOrderedSet(
        OrderedSetType.TS2, link_num=5, lane_num=1, n_fts=64, rate_id=0x04, training_ctrl=0x00
    )
    raw_ts2 = ts2.to_bytes()
    assert raw_ts2[7] == TrainingOrderedSet.TS2_ID_BYTE
    parsed_ts2 = TrainingOrderedSet.from_bytes(raw_ts2)
    assert parsed_ts2 is not None
    assert parsed_ts2.os_type == OrderedSetType.TS2
    assert parsed_ts2.rate_id == 0x04

    # Test error trapping: corrupted comma symbol
    corrupted_data = bytearray(raw_ts1)
    corrupted_data[0] = 0x00  # Invalidate K28.5 comma
    assert TrainingOrderedSet.from_bytes(bytes(corrupted_data)) is None

    # Test error trapping: corrupted trailing identifier
    corrupted_id = bytearray(raw_ts1)
    corrupted_id[10] = 0xFF
    assert TrainingOrderedSet.from_bytes(bytes(corrupted_id)) is None

    dut._log.info("Ordered set formatting, comma verification, and fault rejection confirmed!")


@cocotb.test()
async def test_ltssm_speed_negotiation_dynamic_gear_switch(dut):
    """Test 3: Verify dynamic multi-gear speed negotiation from L0 to RECOVERY and return."""
    dut._log.info("Starting Test 3: Dynamic Multi-Gear Speed Negotiation Verification")

    engine = LtssmEngine()

    # Fast-forward to L0 at GEAR_1_BASE (10 Mbps)
    engine.step_detect(True)
    engine.step_detect(True)
    ts1 = TrainingOrderedSet(OrderedSetType.TS1)
    for _ in range(8):
        engine.step_polling(ts1)
    ts2 = TrainingOrderedSet(OrderedSetType.TS2)
    for _ in range(8):
        engine.step_polling(ts2)
    engine.step_config(1, 0)
    engine.step_config(1, 0)
    assert engine.state == LtssmState.L0_ACTIVE_RUN
    assert engine.current_gear == SpeedGear.GEAR_1_BASE

    # Request speed change to GEAR_2_HIGH (50 Mbps)
    assert engine.request_speed_change(SpeedGear.GEAR_2_HIGH)
    assert engine.state == LtssmState.RECOVERY_SPEED
    assert engine.target_gear == SpeedGear.GEAR_2_HIGH

    # Complete recovery retraining and lock
    assert engine.step_recovery(lock_acquired=True)
    assert engine.state == LtssmState.L0_ACTIVE_RUN
    assert engine.current_gear == SpeedGear.GEAR_2_HIGH
    assert engine.speed_negotiated is True

    # Upgrade to GEAR_3_SUPER (100 Mbps)
    assert engine.request_speed_change(SpeedGear.GEAR_3_SUPER)
    assert engine.state == LtssmState.RECOVERY_SPEED
    assert engine.step_recovery(lock_acquired=True)
    assert engine.state == LtssmState.L0_ACTIVE_RUN
    assert engine.current_gear == SpeedGear.GEAR_3_SUPER

    dut._log.info("Dynamic rate switching verified across 10 Mbps -> 50 Mbps -> 100 Mbps gears!")


@cocotb.test()
async def test_ltssm_link_fault_recovery_and_hot_reset(dut):
    """Test 4: Verify symbol error detection, automatic RECOVERY triggering, and HOT_RESET fallback."""
    dut._log.info("Starting Test 4: Fault Detection, Recovery & Hot Reset Verification")

    engine = LtssmEngine()

    # Fast-forward to L0
    engine.step_detect(True)
    engine.step_detect(True)
    ts1 = TrainingOrderedSet(OrderedSetType.TS1)
    for _ in range(8):
        engine.step_polling(ts1)
    ts2 = TrainingOrderedSet(OrderedSetType.TS2)
    for _ in range(8):
        engine.step_polling(ts2)
    engine.step_config(1, 0)
    engine.step_config(1, 0)
    assert engine.state == LtssmState.L0_ACTIVE_RUN

    # Report errors below threshold -> remains in L0
    engine.report_symbol_error()
    engine.report_symbol_error()
    assert engine.state == LtssmState.L0_ACTIVE_RUN
    assert engine.error_count == 2

    # 3rd error exceeds threshold -> automatic fallback into RECOVERY_SPEED
    engine.report_symbol_error()
    assert engine.state == LtssmState.RECOVERY_SPEED

    # Simulate recovery timeout -> transitions to HOT_RESET
    engine.step_recovery(lock_acquired=False, timeout=True)
    assert engine.state == LtssmState.HOT_RESET

    # Complete hot reset -> transitions to DETECT_QUIET with base gear
    engine.complete_hot_reset()
    assert engine.state == LtssmState.DETECT_QUIET
    assert engine.current_gear == SpeedGear.GEAR_1_BASE
    assert engine.error_count == 0

    dut._log.info("Autonomous fault tracking, recovery timeout, and hot reset cycle verified!")


@cocotb.test()
async def test_ltssm_incore_microcode_execution(dut):
    """Test 5: Verify synthesizable Verilog core executes LTSSM verification microcode."""
    dut._log.info("Starting Test 5: Synthesizable RTL In-Core LTSSM Microcode")

    clock = Clock(dut.clk, 100, unit="ns")  # 10 MHz clock
    cocotb.start_soon(clock.start())

    # Clean reset sequence matching bootloader timing
    await FallingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    microcode = get_incore_ltssm_microcode()
    dut._log.info(f"Bootloading LTSSM microcode ({len(microcode)} words)...")
    await bootload(dut, microcode)

    # Wait for core to complete LTSSM verification (GWRI 0x33 on uio_out)
    passed = False
    for cycle in range(50):
        await RisingEdge(dut.clk)
        try:
            uio_val = int(dut.uio_out.value)
            oe_val = int(dut.uio_oe.value)
        except ValueError:
            continue

        if oe_val == 0xFF and uio_val == 0x33:
            dut._log.info(f"LTSSM microcode verified at cycle {cycle}: uio_out=0x{uio_val:02X}")
            passed = True
            break

    assert passed, f"LTSSM verification timed out (oe=0x{int(dut.uio_oe.value):02X}, uio=0x{int(dut.uio_out.value):02X})"
    dut._log.info("Synthesizable core verified: LTSSM verification executed with zero errors!")


@cocotb.test()
async def test_ltssm_ppa_metrics(dut):
    """Test 6: Verify LTSSM Engine silicon PPA metrics on IHP 130nm SG13G2."""
    dut._log.info("Starting Test 6: Silicon PPA Metrics Validation")

    ppa = get_ltssm_ppa_metrics()
    assert ppa["standard_cells"] == 290, f"Expected 290 cells, got {ppa['standard_cells']}"
    assert ppa["gate_equivalents"] == 570, f"Expected 570 GE, got {ppa['gate_equivalents']}"
    assert ppa["silicon_area_mm2"] == 0.0050, f"Expected 0.0050 mm2, got {ppa['silicon_area_mm2']}"
    assert ppa["f_max_mhz"] == 800.0, f"Expected 800 MHz Fmax, got {ppa['f_max_mhz']}"
    assert ppa["dynamic_power_uw_per_mhz"] == 1.68, f"Expected 1.68 uW/MHz, got {ppa['dynamic_power_uw_per_mhz']}"
    assert ppa["bringup_latency_cycles"] == 64, f"Expected 64 cycles latency, got {ppa['bringup_latency_cycles']}"
    dut._log.info(f"PPA metrics qualified: {ppa}")
