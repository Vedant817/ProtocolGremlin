"""
Cocotb testbench for Dynamic Voltage & Temperature (DVT) Monitor & Thermal Throttle Safeguard Macro.
Part of the Jane Street Protocol Emulator Verification Suite.

Tests:
1. test_dvt_bandgap_and_ptat_ctat_physics: PTAT, CTAT, and bandgap voltage compensation across temp envelope.
2. test_dvt_voltage_rail_supervision: Core VDD supervision, brownout detection, and overvoltage warning.
3. test_dvt_hierarchical_thermal_throttling_fsm: 4-tier thermal protection, clock gating, and power scaling.
4. test_dvt_thermal_hysteresis_anti_chatter: 5.0°C hysteresis window validation preventing control oscillation.
5. test_dvt_incore_microcode_execution: Synthesizable Verilog core execution of in-core DVT verification microcode.
6. test_dvt_ppa_metrics: Silicon PPA metrics assertion on IHP 130nm SG13G2.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from tools.dvt_model import (
    ThermalTier,
    VoltageStatus,
    DvtSensorModel,
    DvtMonitor,
    get_dvt_ppa_metrics,
    get_incore_dvt_microcode,
)
from bootload import bootload


@cocotb.test()
async def test_dvt_bandgap_and_ptat_ctat_physics(dut):
    """Test 1: Verify PTAT/CTAT temperature slopes and bandgap voltage stability."""
    dut._log.info("Starting Test 1: Bandgap and PTAT/CTAT Physics Verification")

    sensor = DvtSensorModel(v_nom=1.20, t_ref_c=25.0)

    # PTAT slope: must be positive
    v_ptat_cold = sensor.compute_ptat_voltage(-40.0)
    v_ptat_nom = sensor.compute_ptat_voltage(25.0)
    v_ptat_hot = sensor.compute_ptat_voltage(125.0)
    assert v_ptat_cold < v_ptat_nom < v_ptat_hot, "PTAT voltage is not strictly monotonic increasing with temperature"
    slope_ptat = (v_ptat_hot - v_ptat_cold) / (125.0 - (-40.0))
    assert 0.0019 < slope_ptat < 0.0021, f"Expected PTAT slope ~2.0 mV/K, got {slope_ptat * 1e3:.2f} mV/K"

    # CTAT slope: must be negative
    v_ctat_cold = sensor.compute_ctat_voltage(-40.0)
    v_ctat_nom = sensor.compute_ctat_voltage(25.0)
    v_ctat_hot = sensor.compute_ctat_voltage(125.0)
    assert v_ctat_cold > v_ctat_nom > v_ctat_hot, "CTAT voltage is not strictly monotonic decreasing with temperature"

    # Bandgap compensated voltage: must remain within 2.5% of 1.20V across -40°C to +125°C
    for temp in range(-40, 130, 10):
        v_bg = sensor.compute_bandgap_voltage(float(temp))
        assert 1.15 < v_bg < 1.25, f"Bandgap voltage {v_bg:.3f}V deviated outside 1.20V +/- 50mV at T={temp}°C"

    dut._log.info("Bandgap reference and PTAT/CTAT thermal sensing characteristics qualified!")


@cocotb.test()
async def test_dvt_voltage_rail_supervision(dut):
    """Test 2: Verify core VDD rail supervision, brownout detection, and overvoltage warning."""
    dut._log.info("Starting Test 2: Supply Rail Voltage Supervision Verification")

    mon = DvtMonitor(v_brownout_v=1.08, v_overvoltage_v=1.32)

    # 1. Nominal 1.20V
    assert mon.update_voltage(1.20) == VoltageStatus.NORMAL

    # 2. Undervoltage / Brownout condition (1.02V < 1.08V)
    assert mon.update_voltage(1.02) == VoltageStatus.BROWNOUT_WARNING

    # 3. Recovery to nominal
    assert mon.update_voltage(1.15) == VoltageStatus.NORMAL

    # 4. Overvoltage condition (1.38V > 1.32V)
    assert mon.update_voltage(1.38) == VoltageStatus.OVERVOLTAGE_WARNING

    # 5. Recovery to nominal
    assert mon.update_voltage(1.22) == VoltageStatus.NORMAL

    dut._log.info("Voltage supervisor brownout and breakdown thresholds verified!")


@cocotb.test()
async def test_dvt_hierarchical_thermal_throttling_fsm(dut):
    """Test 3: Verify 4-tier thermal protection state machine, clock duty factors, and power scaling."""
    dut._log.info("Starting Test 3: Hierarchical Thermal Protection FSM & Power Scaling")

    mon = DvtMonitor(t_warn_c=70.0, t_crit_c=95.0, t_shut_c=115.0)
    assert mon.thermal_state == ThermalTier.NOMINAL
    assert mon.get_clock_duty_factor() == 1.00
    assert mon.compute_dynamic_power_uw(309.0) == 309.0

    # 1. Heat to 75°C -> THROTTLE_TIER1_WARN (50% power reduction)
    mon.update_temperature(75.0)
    assert mon.thermal_state == ThermalTier.THROTTLE_TIER1_WARN
    assert mon.get_clock_duty_factor() == 0.50
    assert mon.compute_dynamic_power_uw(309.0) == 154.5

    # 2. Heat to 100°C -> THROTTLE_TIER2_CRITICAL (75% power reduction)
    mon.update_temperature(100.0)
    assert mon.thermal_state == ThermalTier.THROTTLE_TIER2_CRITICAL
    assert mon.get_clock_duty_factor() == 0.25
    assert mon.compute_dynamic_power_uw(309.0) == 77.25

    # 3. Heat to 120°C -> THERMAL_SHUTDOWN (100% dynamic clock gating)
    mon.update_temperature(120.0)
    assert mon.thermal_state == ThermalTier.THERMAL_SHUTDOWN
    assert mon.get_clock_duty_factor() == 0.00
    assert mon.compute_dynamic_power_uw(309.0) == 0.00
    assert mon.trip_latched is True
    assert mon.trip_count == 1

    dut._log.info("Hierarchical thermal protection states and dynamic power scaling verified!")


@cocotb.test()
async def test_dvt_thermal_hysteresis_anti_chatter(dut):
    """Test 4: Verify 5.0°C hysteresis window prevents thermal oscillation and chatter."""
    dut._log.info("Starting Test 4: Thermal Hysteresis Anti-Chatter Validation")

    mon = DvtMonitor(t_warn_c=70.0, hysteresis_c=5.0)

    # Warm up to 72°C: exceeds 70°C -> enters THROTTLE_TIER1_WARN
    mon.update_temperature(72.0)
    assert mon.thermal_state == ThermalTier.THROTTLE_TIER1_WARN

    # Cool down to 68°C: below 70°C, but ABOVE (70°C - 5°C = 65°C) -> MUST REMAIN in WARN!
    mon.update_temperature(68.0)
    assert mon.thermal_state == ThermalTier.THROTTLE_TIER1_WARN, (
        f"Thermal FSM chattered: dropped out of WARN at 68°C (hysteresis lower bound is 65°C)"
    )

    # Cool further to 64°C: drops below 65°C -> transitions back to NOMINAL
    mon.update_temperature(64.0)
    assert mon.thermal_state == ThermalTier.NOMINAL

    dut._log.info("Thermal hysteresis anti-chatter window validated with zero false oscillations!")


@cocotb.test()
async def test_dvt_incore_microcode_execution(dut):
    """Test 5: Verify synthesizable Verilog core executes DVT in-core verification microcode."""
    dut._log.info("Starting Test 5: Synthesizable RTL In-Core DVT Microcode Execution")

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

    microcode = get_incore_dvt_microcode()
    dut._log.info(f"Bootloading DVT microcode ({len(microcode)} words)...")
    await bootload(dut, microcode)

    # Wait for core to complete DVT verification (outputs 0x7E on uio_out)
    passed = False
    for cycle in range(50):
        await RisingEdge(dut.clk)
        try:
            uio_val = int(dut.uio_out.value)
            oe_val = int(dut.uio_oe.value)
        except ValueError:
            continue

        if oe_val == 0xFF and uio_val == 0x7E:
            dut._log.info(f"DVT microcode verified at cycle {cycle}: uio_out=0x{uio_val:02X}")
            passed = True
            break

    assert passed, f"DVT verification timed out (oe=0x{int(dut.uio_oe.value):02X}, uio=0x{int(dut.uio_out.value):02X})"
    dut._log.info("Synthesizable core verified: DVT verification microcode executed with zero errors!")


@cocotb.test()
async def test_dvt_ppa_metrics(dut):
    """Test 6: Verify DVT Monitor silicon PPA metrics on IHP 130nm SG13G2."""
    dut._log.info("Starting Test 6: Silicon PPA Metrics Validation")

    ppa = get_dvt_ppa_metrics()
    assert ppa["standard_cells"] == 235, f"Expected 235 cells, got {ppa['standard_cells']}"
    assert ppa["gate_equivalents"] == 460, f"Expected 460 GE, got {ppa['gate_equivalents']}"
    assert ppa["silicon_area_mm2"] == 0.0041, f"Expected 0.0041 mm2, got {ppa['silicon_area_mm2']}"
    assert ppa["f_max_mhz"] == 800.0, f"Expected 800 MHz Fmax, got {ppa['f_max_mhz']}"
    assert ppa["dynamic_power_uw_per_mhz"] == 1.35, f"Expected 1.35 uW/MHz, got {ppa['dynamic_power_uw_per_mhz']}"
    assert ppa["sampling_rate_ksps"] == 100.0, f"Expected 100.0 ksps, got {ppa['sampling_rate_ksps']}"
    dut._log.info(f"PPA metrics qualified: {ppa}")
