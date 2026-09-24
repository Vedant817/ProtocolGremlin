"""
Cocotb testbench for Hardware PRBS Bit Error Rate Tester (BERT) & Eye Margin Diagnostic Engine.
Part of the Jane Street Protocol Emulator Verification Suite.

Tests:
1. test_bert_pattern_generation_and_periodicity: PRBS-7/9/15/23/31 sequence generation, cyclic period verification, anti-lockup.
2. test_bert_autonomous_lock_and_synchronization: Detector LFSR seed ingestion, acquisition, and zero-error locked state.
3. test_bert_bit_error_injection_and_counting: Controlled single and multi-bit error injection, error count, and BER calculation.
4. test_bert_bit_slip_loss_of_lock_recovery: Burst noise error threshold exceeding, BIT_SLIP trigger, and auto-reacquisition.
5. test_bert_eye_margin_and_statistical_confidence: 2D bathtub curves, Dual-Dirac jitter, EOW/EOH metrics, Poisson confidence.
6. test_bert_incore_microcode_execution: Synthesizable Verilog core execution of in-core BERT verification microcode.
7. test_bert_ppa_metrics: Silicon PPA metrics assertion on IHP 130nm SG13G2.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from tools.bert_model import (
    PrbsPattern,
    BertState,
    PrbsGenerator,
    BertDetector,
    EyeMarginProfiler,
    poisson_confidence_required_bits,
    get_bert_ppa_metrics,
    get_incore_bert_microcode,
)
from bootload import bootload


@cocotb.test()
async def test_bert_pattern_generation_and_periodicity(dut):
    """Test 1: Verify PRBS pattern generation, maximal LFSR cycle periodicity, and anti-lockup."""
    dut._log.info("Starting Test 1: PRBS Generation and Periodicity Verification")

    # 1. PRBS-7 periodicity: period must be 2^7 - 1 = 127 bits
    gen7 = PrbsGenerator(PrbsPattern.PRBS7, seed=0x7F)
    first_period = gen7.generate_bits(127)
    second_period = gen7.generate_bits(127)
    assert first_period == second_period, "PRBS-7 failed maximal-length sequence periodicity test (127 bits)"
    assert sum(first_period) == 64, f"PRBS-7 should contain 64 ones, got {sum(first_period)}"

    # 2. PRBS-9 periodicity: period must be 2^9 - 1 = 511 bits
    gen9 = PrbsGenerator(PrbsPattern.PRBS9, seed=0x1FF)
    p9_first = gen9.generate_bits(511)
    p9_second = gen9.generate_bits(511)
    assert p9_first == p9_second, "PRBS-9 failed maximal-length sequence periodicity test (511 bits)"

    # 3. Anti-lockup test: all-zero seed must be automatically converted to non-zero seed
    gen_zero = PrbsGenerator(PrbsPattern.PRBS7, seed=0x00)
    assert gen_zero.state != 0, "PRBS generator absorbed into all-zero state"
    bits = gen_zero.generate_bits(32)
    assert sum(bits) > 0, "PRBS output remained all zeros under zero seed"

    dut._log.info("PRBS generation and cyclic periodicity verified across patterns!")


@cocotb.test()
async def test_bert_autonomous_lock_and_synchronization(dut):
    """Test 2: Verify autonomous LFSR seed ingestion, acquisition, and zero-error locked state."""
    dut._log.info("Starting Test 2: Autonomous BERT Pattern Lock and Synchronization")

    gen = PrbsGenerator(PrbsPattern.PRBS7, seed=0x5A)
    det = BertDetector(PrbsPattern.PRBS7, acquisition_bits=32)
    assert det.state == BertState.UNLOCKED

    # Stream bits until locked without skipping
    bits_sent = 0
    while det.state != BertState.LOCKED and bits_sent < 100:
        det.process_bit(gen.next_bit())
        bits_sent += 1

    assert det.state == BertState.LOCKED, "Detector failed to acquire lock on clean PRBS stream"
    assert det.error_count == 0, f"Expected 0 errors during clean lock acquisition, got {det.error_count}"

    # Process subsequent 300 bits and verify zero errors
    for _ in range(300):
        det.process_bit(gen.next_bit())

    assert det.error_count == 0, f"Expected 0 bit errors on clean channel, got {det.error_count}"
    assert det.get_ber() == 0.0, f"Expected BER 0.0, got {det.get_ber()}"
    dut._log.info("Autonomous pattern lock and zero-error tracking verified!")


@cocotb.test()
async def test_bert_bit_error_injection_and_counting(dut):
    """Test 3: Verify controlled single-bit and multi-bit error injection, counting, and BER."""
    dut._log.info("Starting Test 3: Bit Error Injection and Counting Verification")

    gen = PrbsGenerator(PrbsPattern.PRBS7, seed=0x3F)
    det = BertDetector(PrbsPattern.PRBS7, acquisition_bits=32)

    # Establish initial lock
    for b in gen.generate_bits(50):
        det.process_bit(b)
    assert det.state == BertState.LOCKED

    # Inject 5 isolated single-bit errors spaced 20 bits apart
    injected_errors = 0
    for i in range(100):
        b = gen.next_bit()
        if i in [10, 30, 50, 70, 90]:
            b ^= 1  # Invert bit
            injected_errors += 1
        det.process_bit(b)

    assert det.error_count == injected_errors, f"Expected {injected_errors} errors, got {det.error_count}"
    assert det.state == BertState.LOCKED, "Detector should remain locked under isolated single-bit errors"
    assert det.get_ber() > 0.0, "BER must be greater than zero when errors are injected"
    dut._log.info(f"Verified {det.error_count} bit errors accurately counted (BER={det.get_ber():.6e})!")


@cocotb.test()
async def test_bert_bit_slip_loss_of_lock_recovery(dut):
    """Test 4: Verify burst noise causes BIT_SLIP and triggers auto-reacquisition upon recovery."""
    dut._log.info("Starting Test 4: Bit Slip Loss-of-Lock and Recovery")

    gen = PrbsGenerator(PrbsPattern.PRBS7, seed=0x4D)
    det = BertDetector(PrbsPattern.PRBS7, acquisition_bits=32)

    # Lock detector
    for b in gen.generate_bits(50):
        det.process_bit(b)
    assert det.state == BertState.LOCKED

    # Inject heavy burst error (25 consecutive inverted bits)
    for _ in range(25):
        b = gen.next_bit() ^ 1
        det.process_bit(b)

    # Detector should have dropped lock due to high error density
    assert det.state in [BertState.BIT_SLIP, BertState.UNLOCKED, BertState.ACQUIRING], (
        f"Detector failed to drop lock under burst corruption (state={det.state})"
    )

    # Now restore clean PRBS-7 stream for 100 bits; detector must re-acquire lock
    for b in gen.generate_bits(100):
        det.process_bit(b)

    assert det.state == BertState.LOCKED, f"Detector failed to re-lock after channel recovery (state={det.state})"
    dut._log.info("Bit-slip detection and autonomous re-synchronization successfully verified!")


@cocotb.test()
async def test_bert_eye_margin_and_statistical_confidence(dut):
    """Test 5: Verify 2D bathtub curve generation, Dual-Dirac jitter decomposition, and Poisson confidence."""
    dut._log.info("Starting Test 5: Eye Margin Bath-Tub Profiling & Poisson Confidence")

    profiler = EyeMarginProfiler(dj_ui=0.15, rj_rms_ui=0.02, v_peak_mv=400.0, v_noise_rms_mv=15.0)

    # Bath-tub horizontal BER calculation: center of eye should have low BER
    ber_center = profiler.compute_horizontal_ber(0.0)
    ber_edge = profiler.compute_horizontal_ber(0.45)
    assert ber_center < 1e-10, f"Expected low BER at eye center, got {ber_center}"
    assert ber_edge > 1e-4, f"Expected high BER near eye edge, got {ber_edge}"

    # Eye Opening Width & Height
    eow = profiler.calculate_eye_opening_width(target_ber=1e-12)
    eoh = profiler.calculate_eye_opening_height(target_ber=1e-12)
    assert 0.40 < eow < 0.70, f"Expected EOW between 0.40 and 0.70 UI, got {eow}"
    assert 400.0 < eoh < 700.0, f"Expected EOH between 400 and 700 mV, got {eoh}"

    # Poisson statistical confidence
    req_bits_95 = poisson_confidence_required_bits(1e-6, confidence=0.95)
    req_bits_99 = poisson_confidence_required_bits(1e-6, confidence=0.99)
    assert 2.9e6 < req_bits_95 < 3.1e6, f"Expected ~3.0e6 bits for 95% confidence, got {req_bits_95}"
    assert 4.5e6 < req_bits_99 < 4.7e6, f"Expected ~4.61e6 bits for 99% confidence, got {req_bits_99}"

    dut._log.info(f"Eye diagnostics verified: EOW={eow:.3f} UI, EOH={eoh:.1f} mV, Req Bits 95%={req_bits_95}")


@cocotb.test()
async def test_bert_incore_microcode_execution(dut):
    """Test 6: Verify synthesizable Verilog core executes BERT in-core verification microcode."""
    dut._log.info("Starting Test 6: Synthesizable RTL In-Core BERT Microcode Execution")

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

    microcode = get_incore_bert_microcode()
    dut._log.info(f"Bootloading BERT microcode ({len(microcode)} words)...")
    await bootload(dut, microcode)

    # Wait for core to complete BERT verification (outputs 0x77 on uio_out)
    passed = False
    for cycle in range(50):
        await RisingEdge(dut.clk)
        try:
            uio_val = int(dut.uio_out.value)
            oe_val = int(dut.uio_oe.value)
        except ValueError:
            continue

        if oe_val == 0xFF and uio_val == 0x77:
            dut._log.info(f"BERT microcode verified at cycle {cycle}: uio_out=0x{uio_val:02X}")
            passed = True
            break

    assert passed, f"BERT verification timed out (oe=0x{int(dut.uio_oe.value):02X}, uio=0x{int(dut.uio_out.value):02X})"
    dut._log.info("Synthesizable core verified: BERT verification microcode executed with zero errors!")


@cocotb.test()
async def test_bert_ppa_metrics(dut):
    """Test 7: Verify BERT Engine silicon PPA metrics on IHP 130nm SG13G2."""
    dut._log.info("Starting Test 7: Silicon PPA Metrics Validation")

    ppa = get_bert_ppa_metrics()
    assert ppa["standard_cells"] == 265, f"Expected 265 cells, got {ppa['standard_cells']}"
    assert ppa["gate_equivalents"] == 520, f"Expected 520 GE, got {ppa['gate_equivalents']}"
    assert ppa["silicon_area_mm2"] == 0.0046, f"Expected 0.0046 mm2, got {ppa['silicon_area_mm2']}"
    assert ppa["f_max_mhz"] == 800.0, f"Expected 800 MHz Fmax, got {ppa['f_max_mhz']}"
    assert ppa["dynamic_power_uw_per_mhz"] == 1.55, f"Expected 1.55 uW/MHz, got {ppa['dynamic_power_uw_per_mhz']}"
    assert ppa["max_throughput_mbps"] == 800.0, f"Expected 800.0 Mbps, got {ppa['max_throughput_mbps']}"
    dut._log.info(f"PPA metrics qualified: {ppa}")
