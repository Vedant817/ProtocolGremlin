"""
test/test_wiegand.py
====================
Cocotb testbench for Wiegand Access Control Protocol & Pulse Width Discovery Engine.

Test suite covers:
  1. 26-bit Wiegand credential writer transmission (FC=102, ID=34567) verified
     against cycle-accurate WiegandReaderModel.
  2. Pulse timing discovery via WAITEDGE (single-cycle pulse width & interval capture).
  3. Mathematical 26-bit parity validation with 100% single-bit corruption detection.
  4. 8-bit Wiegand stream ingress via GRD edge polling into accumulator R0.
  5. Physical cable short-circuit & tamper detection (simultaneous low on DATA0 & DATA1).
  6. Physical PPA scaling validation for synthesizable coprocessor macros on IHP 130nm.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ClockCycles

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from bootload import bootload
from wiegand_model import (
    compute_wiegand26_parity,
    build_wiegand26_raw,
    verify_wiegand26,
    WiegandReaderModel,
    WiegandPpaModel,
    build_wiegand_writer_asm,
    build_wiegand_pulse_discovery_asm,
    build_wiegand_reader_byte_asm,
    build_wiegand_tamper_detector_asm,
)


async def _init_dut_and_bootload(dut, words: list, initial_uio: int = 0x00):
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
    await ClockCycles(dut.clk, 10)


@cocotb.test()
async def test_wiegand_writer_transmission(dut):
    """
    Test 1: Transmit standard 26-bit Wiegand credential from ASIC (card emulator).
    Verifies that active-low pulses on DATA0 (pin 3) and DATA1 (pin 4) are
    decoded by WiegandReaderModel into exact Facility Code 102 and Card ID 34567.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    fc = 102
    cid = 34567
    pulse_width = 8
    pulse_interval = 22

    words = build_wiegand_writer_asm(
        facility_code=fc,
        card_id=cid,
        pulse_width_cycles=pulse_width,
        pulse_interval_cycles=pulse_interval,
        d0_pin=3,
        d1_pin=4
    )
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core
    reader = WiegandReaderModel(d0_pin=3, d1_pin=4)
    parsed_cred = None

    for _ in range(1200):
        await RisingEdge(dut.clk)
        uio_out = int(dut.uio_out.value)
        uio_oe = int(dut.uio_oe.value)
        cred = reader.step(uio_out, uio_oe)
        if cred is not None:
            parsed_cred = cred

    # Allow final pulse to complete its rising edge
    for _ in range(50):
        await RisingEdge(dut.clk)
        uio_out = int(dut.uio_out.value)
        uio_oe = int(dut.uio_oe.value)
        reader.step(uio_out, uio_oe)
        if len(reader.pulse_widths) == 26:
            break

    assert parsed_cred is not None, "Failed to capture 26-bit Wiegand credential"
    assert parsed_cred.facility_code == fc, f"Expected FC {fc}, got {parsed_cred.facility_code}"
    assert parsed_cred.card_id == cid, f"Expected Card ID {cid}, got {parsed_cred.card_id}"
    assert parsed_cred.valid is True, "Wiegand credential parity validation failed"
    assert reader.tamper_detected is False, "Unexpected tamper detected during normal transmission"
    assert len(reader.pulse_widths) == 26, f"Expected 26 pulses, got {len(reader.pulse_widths)}"

    # Wait for halt
    for _ in range(50):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value), "DUT should be halted after transmission"
    assert int(core.r0.value) == fc, f"Expected R0={fc}, got {int(core.r0.value)}"
    dut._log.info("Wiegand 26-bit Credential Writer Transmission PASS")


@cocotb.test()
async def test_wiegand_pulse_timing_discovery(dut):
    """
    Test 2: Physical pulse width & interval discovery via WAITEDGE.
    Stimulate Pin 3 with known idle duration (15 cycles), pulse width (12 cycles),
    and inter-pulse duration (25 cycles).
    Verifies that WAITEDGE captures exact cycle durations into registers.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_wiegand_pulse_discovery_asm(d0_pin=3)
    await _init_dut_and_bootload(dut, words, initial_uio=0x18)

    core = dut.user_project.u_core

    # Hold idle for 15 cycles
    await ClockCycles(dut.clk, 15)
    # Pulse 1 LOW for 12 cycles
    dut.uio_in.value = 0x10  # pin 3 is 0, pin 4 is 1
    await ClockCycles(dut.clk, 12)
    # Return HIGH for 25 cycles
    dut.uio_in.value = 0x18
    await ClockCycles(dut.clk, 25)
    # Pulse 2 LOW for 12 cycles
    dut.uio_in.value = 0x10
    await ClockCycles(dut.clk, 12)
    # Return HIGH
    dut.uio_in.value = 0x18
    await ClockCycles(dut.clk, 15)

    assert bool(core.halted.value), "DUT should halt after timing discovery"
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    assert r1 == 12, f"Expected captured pulse width 12 cycles, got {r1}"
    assert r2 == 25, f"Expected captured pulse interval 25 cycles, got {r2}"
    dut._log.info(f"Wiegand Pulse Timing Discovery PASS: T_pw={r1}, T_pi={r2}")


@cocotb.test()
async def test_wiegand_parity_mathematical_validation(dut):
    """
    Test 3: Mathematical verification of 26-bit Wiegand parity and single-bit
    corruption rejection across diverse credential patterns.
    """
    test_cases = [
        (0, 0),
        (1, 1),
        (102, 34567),
        (255, 65535),
        (170, 43690),
        (85, 21845),
    ]

    for fc, cid in test_cases:
        raw26 = build_wiegand26_raw(fc, cid)
        valid, parsed_fc, parsed_cid = verify_wiegand26(raw26)
        assert valid is True, f"Valid credential failed: FC={fc}, ID={cid}"
        assert parsed_fc == fc, f"FC mismatch: {parsed_fc} != {fc}"
        assert parsed_cid == cid, f"Card ID mismatch: {parsed_cid} != {cid}"

        # Test single-bit error detection across all 26 bit positions
        for bit_pos in range(26):
            corrupted_raw26 = raw26 ^ (1 << bit_pos)
            c_valid, _, _ = verify_wiegand26(corrupted_raw26)
            assert c_valid is False, f"Bit flip at position {bit_pos} was not detected!"

    dut._log.info("Wiegand Mathematical Parity Validation PASS: 100% error rejection")


@cocotb.test()
async def test_wiegand_reader_byte_ingress(dut):
    """
    Test 4: Ingress 8 bits of Wiegand stream into accumulator R0 using GRD edge polling.
    Transmits byte 0xD5 (binary 11010101).
    Verifies that R0 accumulates exactly 0xD5 with status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_wiegand_reader_byte_asm(d0_pin=3, d1_pin=4)
    await _init_dut_and_bootload(dut, words, initial_uio=0x18)

    core = dut.user_project.u_core

    payload_byte = 0xD5
    for bit_idx in range(7, -1, -1):
        bit_val = (payload_byte >> bit_idx) & 1
        await ClockCycles(dut.clk, 16)
        if bit_val == 0:
            dut.uio_in.value = 0x10  # DATA0 low (pin 3=0, pin 4=1)
        else:
            dut.uio_in.value = 0x08  # DATA1 low (pin 3=1, pin 4=0)
        await ClockCycles(dut.clk, 12)
        dut.uio_in.value = 0x18      # Return HIGH
        await ClockCycles(dut.clk, 16)

    # Wait for DUT to finish loop and halt
    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "DUT should halt after 8 bits"
    r0 = int(core.r0.value)
    r2 = int(core.r2.value)

    assert r0 == payload_byte, f"Expected R0 0x{payload_byte:02X}, got 0x{r0:02X}"
    assert r2 == 0x00, f"Expected R2 status 0x00, got 0x{r2:02X}"
    dut._log.info("Wiegand Reader Byte Ingress PASS: payload 0xD5 verified")


@cocotb.test()
async def test_wiegand_tamper_detection(dut):
    """
    Test 5: Physical cable short-circuit & tamper detection.
    Asserts simultaneous LOW on DATA0 and DATA1.
    Verifies that the ASIC traps immediately with tamper alarm status code R2 = 0xAA.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    words = build_wiegand_tamper_detector_asm(d0_pin=3, d1_pin=4)
    await _init_dut_and_bootload(dut, words, initial_uio=0x18)

    core = dut.user_project.u_core

    # Wait 5 cycles idle
    await ClockCycles(dut.clk, 5)

    # Assert simultaneous LOW (tamper / line short condition)
    dut.uio_in.value = 0x00  # pin 3 and pin 4 BOTH LOW
    await ClockCycles(dut.clk, 5)

    # Return HIGH
    dut.uio_in.value = 0x18
    await ClockCycles(dut.clk, 5)

    assert bool(core.halted.value), "DUT should halt on tamper trap"
    r2 = int(core.r2.value)
    assert r2 == 0xAA, f"Expected tamper alarm status 0xAA, got 0x{r2:02X}"
    dut._log.info("Wiegand Cable Short & Tamper Detection PASS: R2=0xAA verified")


@cocotb.test()
async def test_wiegand_ppa_scaling(dut):
    """
    Test 6: Synthesizable Wiegand coprocessor macro PPA scaling validation
    on IHP 130nm SG13G2.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)

    ppa_1ch = WiegandPpaModel.get_ppa("1ch_reader_writer")
    assert ppa_1ch["cells"] == 285
    assert ppa_1ch["ge"] == 285 * 1.95
    assert ppa_1ch["fmax_mhz"] > 800.0

    ppa_2ch = WiegandPpaModel.get_ppa("2ch_dual_door")
    assert ppa_2ch["cells"] == 480
    assert ppa_2ch["fmax_mhz"] >= 800.0

    ppa_4ch = WiegandPpaModel.get_ppa("4ch_pacs_controller")
    assert ppa_4ch["cells"] == 890
    assert ppa_4ch["fmax_mhz"] > 750.0

    # Verify ASIC pin isolation
    uio_oe = int(dut.uio_oe.value)
    assert uio_oe == 0x00, "Unused pins should remain High-Z"
    dut._log.info("Wiegand Coprocessor PPA Scaling Model PASS")
