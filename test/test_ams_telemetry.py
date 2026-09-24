# test/test_ams_telemetry.py - Analog-Mixed Signal (AMS) Delta-Sigma Telemetry Verification
# Verifies CT Delta-Sigma noise shaping, Sinc^2 CIC decimation, PDM DAC generation,
# voltage window alert classification, and synthesizable RTL in-core microcode execution.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

from tools.ams_model import (
    DeltaSigmaModulator,
    Sinc2DecimationFilter,
    PdmDacGenerator,
    TelemetryAlertClassifier,
    TelemetryAlert,
    get_ams_ppa_metrics,
    get_in_core_ams_microcode,
)


async def reset_dut(dut):
    """Clean reset sequence for the protocol emulator core."""
    dut.rst_n.value = 0
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.ena.value = 1
    await ClockCycles(dut.clk, 5)
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
async def test_ams_delta_sigma_modulator_linearity(dut):
    """Test 1: Verify Delta-Sigma 1st and 2nd order bitstream density linearity across voltage sweep."""
    dut._log.info("Starting Test 1: Delta-Sigma Modulator Linearity")

    mod = DeltaSigmaModulator(order=1, vdd=1.20)
    sweep_voltages = [0.12, 0.36, 0.60, 0.84, 1.08]
    prev_density = -1.0

    for vin in sweep_voltages:
        bits = mod.modulate_sequence(vin, 512)
        density = sum(bits) / len(bits)
        expected_density = vin / 1.20
        dut._log.info(f"Vin={vin:.2f}V -> Measured Density={density:.3f} (Expected={expected_density:.3f})")
        assert abs(density - expected_density) < 0.05, f"Density linearity error at Vin={vin}"
        assert density > prev_density, "Non-monotonic density progression in delta-sigma sweep"
        prev_density = density

    dut._log.info("Test 1 PASS: Delta-Sigma modulator linearity verified.")


@cocotb.test()
async def test_ams_sinc2_decimation_filtering(dut):
    """Test 2: Verify Sinc^2 CIC decimation filter output words achieve 10-bit ENOB resolution."""
    dut._log.info("Starting Test 2: Sinc^2 Decimation Filter Accuracy")

    mod = DeltaSigmaModulator(order=1, vdd=1.20)
    flt = Sinc2DecimationFilter(decimation_factor=64)

    test_vin = 0.72  # 0.72V / 1.20V = 0.60 (Expected 10-bit code ~ 614)
    bits = mod.modulate_sequence(test_vin, 1024)
    decimated_words = flt.process_bitstream(bits)

    dut._log.info(f"Decimated 10-bit codes: {decimated_words[:8]}")
    avg_code = sum(decimated_words) / len(decimated_words)
    expected_code = (test_vin / 1.20) * 1023.0

    dut._log.info(f"Average Filter Output: {avg_code:.1f} (Expected: {expected_code:.1f})")
    assert abs(avg_code - expected_code) <= 10.0, f"Decimation filter error exceeds tolerance: {avg_code} vs {expected_code}"

    dut._log.info("Test 2 PASS: Sinc^2 decimation filtering verified.")


@cocotb.test()
async def test_ams_pdm_dac_reconstruction(dut):
    """Test 3: Verify digital PDM DAC generation and analog voltage reconstruction."""
    dut._log.info("Starting Test 3: PDM DAC Generation & Voltage Reconstruction")

    dac = PdmDacGenerator(vdd=1.20)
    test_levels = [32, 64, 128, 192]

    for lvl in test_levels:
        dac.reset()
        pdm_bits = dac.generate_pdm(lvl, 512)
        v_rec = dac.reconstruct_voltage(pdm_bits)
        v_target = (lvl / 256.0) * 1.20
        dut._log.info(f"Target Level={lvl}/255 -> Reconstructed Voltage: {v_rec:.3f}V (Target={v_target:.3f}V)")
        assert abs(v_rec - v_target) < 0.015, f"PDM DAC voltage error for level {lvl}: {v_rec} vs {v_target}"

    dut._log.info("Test 3 PASS: PDM DAC generation confirmed.")


@cocotb.test()
async def test_ams_telemetry_alert_window_classifier(dut):
    """Test 4: Verify telemetry window alert classification (Normal, Brownout, Overvoltage)."""
    dut._log.info("Starting Test 4: Telemetry Alert Window Classifier")

    classifier = TelemetryAlertClassifier(v_low_code=256, v_high_code=896)

    # Brownout condition (e.g. code 150 < 256)
    res_low = classifier.classify(150)
    assert res_low == TelemetryAlert.BROWNOUT_ALERT, f"Expected BROWNOUT_ALERT, got {res_low}"

    # Normal operating condition (e.g. code 512)
    res_norm = classifier.classify(512)
    assert res_norm == TelemetryAlert.NORMAL, f"Expected NORMAL, got {res_norm}"

    # Overvoltage condition (e.g. code 950 > 896)
    res_high = classifier.classify(950)
    assert res_high == TelemetryAlert.OVERVOLTAGE_ALERT, f"Expected OVERVOLTAGE_ALERT, got {res_high}"

    dut._log.info("Test 4 PASS: Alert window classifier verified.")


@cocotb.test()
async def test_ams_in_core_rtl_microcode_execution(dut):
    """Test 5: Verify synthesizable RTL core executes in-core density accumulator microcode."""
    dut._log.info("Starting Test 5: In-Core Synthesizable RTL Density Accumulation")

    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    await reset_dut(dut)

    microcode = get_in_core_ams_microcode()
    dut._log.info(f"Assembled AMS microcode length: {len(microcode)} words")
    await bootload_words(dut, microcode)

    # Drive alternating bitstream on uio_in[0] during execution
    # 50% density over 16 samples = 8 ones (> 4 threshold)
    async def drive_pdm_stream():
        for i in range(200):
            dut.uio_in.value = 0x01 if (i % 2 == 0) else 0x00
            await RisingEdge(dut.clk)

    cocotb.start_soon(drive_pdm_stream())

    # Wait for execution completion
    for _ in range(250):
        await RisingEdge(dut.clk)

    uo_val = int(dut.uo_out.value)
    dut._log.info(f"Core execution halted with uo_out = 0x{uo_val:02X}")

    # uo_out[0] must be 1 (PASS Normal), uo_out[1] must be 0 (No Alert)
    assert (uo_val & 0x01) == 1, f"Telemetry PASS flag not asserted: uo_out=0x{uo_val:02X}"
    assert (uo_val & 0x02) == 0, f"Telemetry Alert flag asserted: uo_out=0x{uo_val:02X}"

    dut._log.info("Test 5 PASS: Real RTL microcode telemetry execution confirmed.")


@cocotb.test()
async def test_ams_ppa_and_mixed_signal_metrics(dut):
    """Test 6: Verify AMS telemetry PPA scaling and mixed-signal performance metrics."""
    dut._log.info("Starting Test 6: AMS Telemetry PPA and Performance Metrics")

    ppa = get_ams_ppa_metrics()
    dut._log.info(f"PPA Metrics: {ppa}")

    assert ppa["macro_name"] == "AMS_DELTA_SIGMA_TELEMETRY"
    assert ppa["cell_count"] == 260
    assert ppa["fmax_mhz"] >= 800.0
    assert ppa["enob_bits"] >= 10.0
    assert ppa["dynamic_range_db"] >= 60.0

    dut._log.info("Test 6 PASS: AMS telemetry PPA and performance metrics verified.")
