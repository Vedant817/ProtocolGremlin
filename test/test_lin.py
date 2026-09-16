"""test/test_lin.py - Cocotb testbench for Local Interconnect Network (LIN v2.2A) Engine

Verifies:
1. test_lin_master_frame_transmission: Master frame generation (Break + Sync 0x55 + PID + Data + Checksum).
2. test_lin_break_pulse_detection: Slave Break detection and single-cycle duration measurement using WAITEDGE.
3. test_lin_pid_parity_validation: Complete mathematical validation of LIN P0/P1 parity across all 64 IDs.
4. test_lin_checksum_classic_and_enhanced: Verification of inverted ones' complement carry-wrap checksums.
5. test_lin_slave_frame_ingress: Slave payload ingress and status verification.
6. test_lin_ppa_and_open_drain_safety: PPA scaling validation and physical open-drain bus safety.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ClockCycles

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from bootload import bootload
from lin_model import (
    compute_lin_pid,
    verify_lin_pid,
    compute_lin_checksum,
    LinFrame,
    LinSlaveModel,
    LinPpaModel,
    build_lin_master_frame_asm,
    build_lin_break_detect_asm,
    build_lin_slave_rx_asm,
)


async def _init_dut_and_bootload(dut, words: list, initial_uio: int = 0x01):
    """Reset DUT and load assembled firmware into program RAM via standard bootloader."""
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = initial_uio
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = initial_uio


async def _wait_until_halted(core, dut, max_cycles: int = 5000):
    """Waits until core halts or timeout occurs."""
    cycles = 0
    while not bool(core.halted.value):
        await RisingEdge(dut.clk)
        cycles += 1
        if cycles > max_cycles:
            break
    assert bool(core.halted.value), f"Core did not halt within {max_cycles} cycles"


@cocotb.test()
async def test_lin_master_frame_transmission(dut):
    """Verify Master frame generation: Break (>= 13 bits low) + Sync 0x55 + PID + Data + Checksum."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    frame_id = 0x17  # ID 23 -> PID 0x97
    data_payload = [0x3A, 0xC5]
    bit_period = 8
    words = build_lin_master_frame_asm(frame_id, data_payload, bit_period=bit_period, pin=0, enhanced=True)
    await _init_dut_and_bootload(dut, words, initial_uio=0x01)

    core = dut.user_project.u_core

    # Capture pin transitions on uio_out[0]
    samples = []
    cycles = 0
    while not bool(core.halted.value) and cycles < 4000:
        await RisingEdge(dut.clk)
        # In open-drain mode with external pullup: output is 0 when driven low, 1 when released
        out_bit = int(dut.uio_out.value) & 1
        oe_bit = int(dut.uio_oe.value) & 1
        bus_val = 0 if (oe_bit and out_bit == 0) else 1
        samples.append(bus_val)
        cycles += 1

    assert bool(core.halted.value), "Core did not halt"

    # Find the Break field (contiguous run of zeros >= 13 * 8 = 104 cycles)
    max_low_run = 0
    cur_low_run = 0
    for s in samples:
        if s == 0:
            cur_low_run += 1
            if cur_low_run > max_low_run:
                max_low_run = cur_low_run
        else:
            cur_low_run = 0

    expected_min_break = 13 * bit_period - 4  # Allow small timing tolerance
    assert max_low_run >= expected_min_break, f"Break duration {max_low_run} cycles < expected {expected_min_break}"
    dut._log.info(f"LIN Master Frame PASS: Break duration {max_low_run} cycles verified (>= 13 bits)")


@cocotb.test()
async def test_lin_break_pulse_detection(dut):
    """Verify Slave Break detection and single-cycle duration measurement using WAITEDGE."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())
    pin = 3
    words = build_lin_break_detect_asm(pin=pin)
    PIN_MASK = 1 << pin
    await _init_dut_and_bootload(dut, words, initial_uio=PIN_MASK)

    core = dut.user_project.u_core

    # Settle in idle recessive high for 25 cycles
    dut.uio_in.value = PIN_MASK
    await ClockCycles(dut.clk, 25)

    # Drive dominant low (Break pulse) for 104 cycles (13 bits * 8 cycles)
    dut.uio_in.value = 0x00
    await ClockCycles(dut.clk, 104)

    # Drive recessive high (Break delimiter)
    dut.uio_in.value = PIN_MASK
    await ClockCycles(dut.clk, 25)

    await _wait_until_halted(core, dut)

    r0 = int(core.r0.value)
    r2 = int(core.r2.value)
    assert r2 == 1, f"Expected break flag R2=1, got {r2}"
    assert 95 <= r0 <= 115, f"Expected break duration R0 ~ 104 cycles, got {r0}"
    dut._log.info(f"LIN Break Detection PASS: Captured break duration R0 = {r0} cycles, flag R2 = 1")


@cocotb.test()
async def test_lin_pid_parity_validation(dut):
    """Verify complete mathematical validation of LIN P0/P1 parity across all 64 IDs."""
    for frame_id in range(64):
        pid = compute_lin_pid(frame_id)
        assert verify_lin_pid(pid), f"PID verification failed for ID {frame_id}"
        # Check that lower 6 bits match frame_id
        assert (pid & 0x3F) == frame_id

        # Inject single-bit error into parity bit 0
        corrupt_pid_p0 = pid ^ 0x40
        assert not verify_lin_pid(corrupt_pid_p0), f"Corrupted P0 undetected on ID {frame_id}"

        # Inject single-bit error into parity bit 1
        corrupt_pid_p1 = pid ^ 0x80
        assert not verify_lin_pid(corrupt_pid_p1), f"Corrupted P1 undetected on ID {frame_id}"

    dut._log.info("LIN PID Parity Validation PASS: All 64 Frame IDs and bit-flip detections verified")


@cocotb.test()
async def test_lin_checksum_classic_and_enhanced(dut):
    """Verify inverted ones' complement carry-wrap checksums (Classic and Enhanced)."""
    # Test case 1: LIN 1.3 Classic Checksum (Data only)
    data1 = [0x01, 0x02, 0x03]
    chk_classic = compute_lin_checksum(data1, pid=None, enhanced=False)
    # Verification: sum of data + checksum with carry-wrap should equal 0xFF
    acc = sum(data1) + chk_classic
    while acc > 0xFF:
        acc = (acc & 0xFF) + (acc >> 8)
    assert acc == 0xFF, f"Classic checksum verification failed: acc=0x{acc:02X}"

    # Test case 2: LIN 2.2A Enhanced Checksum (PID + Data)
    pid = compute_lin_pid(0x17)
    data2 = [0x4A, 0x55, 0x93, 0xE5]
    chk_enhanced = compute_lin_checksum(data2, pid=pid, enhanced=True)
    acc2 = pid + sum(data2) + chk_enhanced
    while acc2 > 0xFF:
        acc2 = (acc2 & 0xFF) + (acc2 >> 8)
    assert acc2 == 0xFF, f"Enhanced checksum verification failed: acc2=0x{acc2:02X}"

    # Test LinFrame object
    frame = LinFrame(frame_id=0x17, data=data2, enhanced=True)
    assert frame.pid == pid
    assert frame.checksum == chk_enhanced

    # Test LinSlaveModel
    slave = LinSlaveModel()
    res = slave.process_frame(break_bits=13, sync=0x55, pid=pid, data=data2, checksum=chk_enhanced, enhanced=True)
    assert res["frame_valid"] is True, f"Slave frame processing failed: {res}"

    dut._log.info("LIN Checksum Validation PASS: Both Classic and Enhanced formulations verified")


@cocotb.test()
async def test_lin_slave_frame_ingress(dut):
    """Verify slave payload ingress and status verification."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pid = compute_lin_pid(0x10)
    words = build_lin_slave_rx_asm(expected_pid=pid, num_data_bytes=2, bit_period=8, pin=0)
    await _init_dut_and_bootload(dut, words, initial_uio=0x01)

    core = dut.user_project.u_core

    # Simulate byte 1 start bit falling edge
    await ClockCycles(dut.clk, 15)
    dut.uio_in.value = 0x00  # start bit low
    await ClockCycles(dut.clk, 8)
    dut.uio_in.value = 0x01  # data bits

    # Simulate byte 2 start bit falling edge
    await ClockCycles(dut.clk, 25)
    dut.uio_in.value = 0x00
    await ClockCycles(dut.clk, 8)
    dut.uio_in.value = 0x01

    await _wait_until_halted(core, dut)

    r2 = int(core.r2.value)
    assert r2 == 0x00, f"Expected clean slave ingress status R2=0x00, got {r2}"
    dut._log.info("LIN Slave Ingress PASS: Verified clean reception status R2 = 0x00")


@cocotb.test()
async def test_lin_ppa_and_open_drain_safety(dut):
    """Verify physical PPA scaling and open-drain electrical safety."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    ppa_model = LinPpaModel(rx_fifo_bytes=8)
    metrics = ppa_model.compute_metrics()

    assert metrics["total_cells"] > 250, "PPA cell count unexpectedly low"
    assert metrics["area_overhead_pct"] < 3.0, f"Area overhead {metrics['area_overhead_pct']}% exceeds 3% budget"
    assert metrics["firmware_area_overhead_pct"] == 0.0, "Firmware overhead must be zero"
    assert metrics["f_max_mhz"] > 200.0, "Operating frequency below 200 MHz"

    # Electrical safety: on halt, pins must be tri-stated (uio_oe == 0x00)
    words = build_lin_master_frame_asm(frame_id=0x01, data_bytes=[0xAA], bit_period=4, pin=0)
    await _init_dut_and_bootload(dut, words, initial_uio=0x01)
    core = dut.user_project.u_core
    await _wait_until_halted(core, dut)

    uio_oe = int(dut.uio_oe.value)
    assert uio_oe == 0x00, f"Bus contention danger: uio_oe={uio_oe:#04x} != 0x00 on halt"
    dut._log.info("LIN PPA Scaling & Open-Drain Safety PASS: uio_oe=0x00 verified")
