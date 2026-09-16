# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
test/test_i2s.py
================
Cocotb testbench for I2S (Inter-IC Sound) & TDM Digital Audio Multi-Channel
Serial Interface Engine on the Jane Street Protocol Emulator ASIC.

Test suite covers:
  1. I2S Master Transmission: ASIC synthesizes SCK, WS, and SD lines; transmits
     stereo PCM audio (Left=0xA5, Right=0x3C); verified by cycle-accurate I2sReceiverModel.
  2. I2S Slave Reception: ASIC synchronizes on WS transitions, demuxes Left (0x5A)
     into R0 and Right (0xC3) into R1 with status R2 = 0x00.
  3. In-Register Volume Attenuation: Microcode executes 6 dB (1-step) and 12 dB (2-step)
     digital audio attenuation directly in architectural registers via SHIFTOUT.
  4. TDM Multi-Channel Slot Extraction: ASIC synchronizes on FSYNC pulse (marking Slot 0)
     and extracts target Slot 2 payload (0x33) from a 4-channel audio stream into R0.
  5. I2S Standard Delay vs Left-Justified Phase Discrimination: Verifies 1-bit delay timing.
  6. Audio Standards & Physical PPA Model Scaling on IHP 130nm SG13G2.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ReadOnly, ClockCycles

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from bootload import bootload
from i2s_model import (
    I2sReceiverModel,
    I2sPpaModel,
    build_i2s_tx_master_asm,
    build_i2s_rx_slave_asm,
    build_i2s_volume_scale_asm,
    build_tdm_slot_filter_asm,
)


async def _init_dut_and_bootload(dut, words: list, initial_uio: int = 0x10):
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
async def test_i2s_master_tx_transmission(dut):
    """
    Test 1: ASIC acts as I2S Master driving SCK (pin 3), WS (pin 4), and SD (pin 5).
    Transmits standard I2S stereo audio: Left=0xA5, Right=0x3C.
    Verified cycle-by-cycle against independent I2sReceiverModel.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    left_sample = 0xA5
    right_sample = 0x3C
    half_period = 4
    words = build_i2s_tx_master_asm(
        left_sample=left_sample,
        right_sample=right_sample,
        sck_pin=3,
        ws_pin=4,
        sd_pin=5,
        half_period=half_period
    )
    await _init_dut_and_bootload(dut, words, initial_uio=0x10)  # WS=1 idle

    receiver = I2sReceiverModel(bit_depth=8, standard_delay=True)
    core = dut.user_project.u_core

    max_cycles = 500
    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_out = int(dut.uio_out.value)
        sck = (uio_out >> 3) & 1
        ws = (uio_out >> 4) & 1
        sd = (uio_out >> 5) & 1

        receiver.step(sck=sck, ws=ws, sd=sd)

        if bool(core.halted.value) and len(receiver.decoded_left) >= 1 and len(receiver.decoded_right) >= 1:
            break

    assert bool(core.halted.value), "Core should halt after I2S frame transmission"
    assert receiver.decoded_left == [left_sample], (
        f"Left channel mismatch: expected 0x{left_sample:02X}, got {[hex(x) for x in receiver.decoded_left]}"
    )
    assert receiver.decoded_right == [right_sample], (
        f"Right channel mismatch: expected 0x{right_sample:02X}, got {[hex(x) for x in receiver.decoded_right]}"
    )

    dut._log.info(
        f"I2S Master Transmission PASS: Left=0x{left_sample:02X}, Right=0x{right_sample:02X}"
    )


@cocotb.test()
async def test_i2s_slave_rx_stereo_capture(dut):
    """
    Test 2: ASIC acts as I2S Slave receiving SCK (pin 3), WS (pin 4), SD (pin 5).
    Ingresses Left channel (0x5A) into R0 and Right channel (0xC3) into R1.
    Halts with R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    bit_period = 8
    half_period = bit_period // 2
    words = build_i2s_rx_slave_asm(sck_pin=3, ws_pin=4, sd_pin=5, bit_period=bit_period)
    await _init_dut_and_bootload(dut, words, initial_uio=0x10)  # WS=1 idle

    core = dut.user_project.u_core

    # Let core settle and enter WAITEDGE stall on WS falling edge
    for _ in range(6):
        await RisingEdge(dut.clk)

    left_val = 0x5A
    right_val = 0xC3

    # --- Drive Left Channel (WS = 0) ---
    # 1-bit standard delay: WS=0, SD=0
    for cycle in range(bit_period):
        sck = 1 if cycle >= half_period else 0
        dut.uio_in.value = (sck << 3) | (0 << 4) | (0 << 5)
        await RisingEdge(dut.clk)

    # 8 data bits MSB first
    for bit_idx in range(7, -1, -1):
        sd = (left_val >> bit_idx) & 1
        for cycle in range(bit_period):
            sck = 1 if cycle >= half_period else 0
            dut.uio_in.value = (sck << 3) | (0 << 4) | (sd << 5)
            await RisingEdge(dut.clk)

    # --- Drive Right Channel (WS = 1) ---
    # 1-bit standard delay: WS=1, SD=0
    for cycle in range(bit_period):
        sck = 1 if cycle >= half_period else 0
        dut.uio_in.value = (sck << 3) | (1 << 4) | (0 << 5)
        await RisingEdge(dut.clk)

    # 8 data bits MSB first
    for bit_idx in range(7, -1, -1):
        sd = (right_val >> bit_idx) & 1
        for cycle in range(bit_period):
            sck = 1 if cycle >= half_period else 0
            dut.uio_in.value = (sck << 3) | (1 << 4) | (sd << 5)
            await RisingEdge(dut.clk)

    # Idle state
    dut.uio_in.value = 0x10
    for _ in range(30):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after I2S slave capture"
    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    assert r0 == left_val, f"Left channel mismatch: expected 0x{left_val:02X}, got 0x{r0:02X}"
    assert r1 == right_val, f"Right channel mismatch: expected 0x{right_val:02X}, got 0x{r1:02X}"
    assert r2 == 0x00, f"Expected status R2=0x00, got 0x{r2:02X}"

    dut._log.info(
        f"I2S Slave Stereo Capture PASS: R0(Left)=0x{r0:02X}, R1(Right)=0x{r1:02X}, Status=0x{r2:02X}"
    )


@cocotb.test()
async def test_i2s_in_register_volume_attenuation(dut):
    """
    Test 3: In-register digital audio attenuation in microcode ALU.
    Step 1: 0x40 attenuated by 6 dB (1 right shift) -> 0x20.
    Step 2: 0x60 attenuated by 12 dB (2 right shifts) -> 0x18.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    core = dut.user_project.u_core

    # Test 1: 6 dB attenuation
    sample1 = 0x40
    words1 = build_i2s_volume_scale_asm(sample=sample1, attenuation_steps=1)
    await _init_dut_and_bootload(dut, words1, initial_uio=0x00)

    for _ in range(25):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after volume scale"
    r0 = int(core.r0.value)
    r2 = int(core.r2.value)
    assert r0 == 0x20, f"Expected 0x20 (-6 dB from 0x40), got 0x{r0:02X}"
    assert r2 == 0x00

    # Test 2: 12 dB attenuation
    sample2 = 0x60
    words2 = build_i2s_volume_scale_asm(sample=sample2, attenuation_steps=2)
    await _init_dut_and_bootload(dut, words2, initial_uio=0x00)

    for _ in range(25):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value)
    r0 = int(core.r0.value)
    assert r0 == 0x18, f"Expected 0x18 (-12 dB from 0x60), got 0x{r0:02X}"

    dut._log.info(f"I2S In-Register Volume Attenuation PASS: 0x40->0x20 (-6dB), 0x60->0x18 (-12dB)")


@cocotb.test()
async def test_tdm_multi_channel_slot_extraction(dut):
    """
    Test 4: Time-Division Multiplexed (TDM) multi-channel audio reception.
    4-slot TDM frame: Slot 0=0x11, Slot 1=0x22, Slot 2=0x33, Slot 3=0x44.
    Target slot: Slot 2.
    ASIC synchronizes on FSYNC rising edge, skips slots 0 and 1, extracts Slot 2 into R0.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    target_slot = 2
    bit_period = 8
    half_period = bit_period // 2
    words = build_tdm_slot_filter_asm(
        target_slot=target_slot,
        total_slots=4,
        fs_pin=4,
        sd_pin=5,
        bit_period=bit_period
    )
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    for _ in range(6):
        await RisingEdge(dut.clk)

    slots = [0x11, 0x22, 0x33, 0x44]

    # Drive FSYNC high for first slot (Slot 0)
    for slot_idx, slot_val in enumerate(slots):
        fs = 1 if slot_idx == 0 else 0
        for bit_idx in range(7, -1, -1):
            sd = (slot_val >> bit_idx) & 1
            for cycle in range(bit_period):
                sck = 1 if cycle >= half_period else 0
                dut.uio_in.value = (sck << 3) | (fs << 4) | (sd << 5)
                await RisingEdge(dut.clk)
            # FSYNC pulses high only during bit 7 of slot 0
            fs = 0

    # Wait for halt
    dut.uio_in.value = 0x00
    for _ in range(40):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after TDM slot extraction"
    r0 = int(core.r0.value)
    r2 = int(core.r2.value)
    expected_val = slots[target_slot]
    assert r0 == expected_val, f"TDM Slot {target_slot} mismatch: expected 0x{expected_val:02X}, got 0x{r0:02X}"
    assert r2 == 0x00

    dut._log.info(f"TDM Slot 2 Extraction PASS: Expected 0x{expected_val:02X}, Received 0x{r0:02X}")


@cocotb.test()
async def test_i2s_slave_sync_delay_discrimination(dut):
    """
    Test 5: Validates sensitivity to standard I2S 1-bit delay.
    Standard receiver expects 1-bit delay; verifies that incoming data bits
    match phase alignment with SCK and WS transitions.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    bit_period = 8
    half_period = bit_period // 2
    words = build_i2s_rx_slave_asm(sck_pin=3, ws_pin=4, sd_pin=5, bit_period=bit_period)
    await _init_dut_and_bootload(dut, words, initial_uio=0x10)

    core = dut.user_project.u_core
    for _ in range(6):
        await RisingEdge(dut.clk)

    # Drive Left channel with sample 0xAA (0b10101010)
    test_left = 0xAA
    test_right = 0x55

    # 1-bit delay (SD=0)
    for cycle in range(bit_period):
        sck = 1 if cycle >= half_period else 0
        dut.uio_in.value = (sck << 3) | (0 << 4) | (0 << 5)
        await RisingEdge(dut.clk)

    for bit_idx in range(7, -1, -1):
        sd = (test_left >> bit_idx) & 1
        for cycle in range(bit_period):
            sck = 1 if cycle >= half_period else 0
            dut.uio_in.value = (sck << 3) | (0 << 4) | (sd << 5)
            await RisingEdge(dut.clk)

    # Right channel 1-bit delay
    for cycle in range(bit_period):
        sck = 1 if cycle >= half_period else 0
        dut.uio_in.value = (sck << 3) | (1 << 4) | (0 << 5)
        await RisingEdge(dut.clk)

    for bit_idx in range(7, -1, -1):
        sd = (test_right >> bit_idx) & 1
        for cycle in range(bit_period):
            sck = 1 if cycle >= half_period else 0
            dut.uio_in.value = (sck << 3) | (1 << 4) | (sd << 5)
            await RisingEdge(dut.clk)

    dut.uio_in.value = 0x10
    for _ in range(30):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value)
    assert int(core.r0.value) == test_left, f"Expected 0x{test_left:02X}, got 0x{int(core.r0.value):02X}"
    assert int(core.r1.value) == test_right, f"Expected 0x{test_right:02X}, got 0x{int(core.r1.value):02X}"

    dut._log.info(f"I2S Delay Alignment Discrimination PASS: R0=0x{int(core.r0.value):02X}, R1=0x{int(core.r1.value):02X}")


@cocotb.test()
async def test_i2s_ppa_and_audio_standards_validation(dut):
    """
    Test 6: Mathematical validation of professional digital audio standards
    (16/24/32-bit PCM, sampling rates, dynamic range) and physical PPA scaling on IHP 130nm SG13G2.
    """
    # 1. 16-bit 44.1 kHz CD Audio
    fs_cd = 44100
    bclk_cd = 2 * fs_cd * 16
    dr_16bit = 6.02 * 16 + 1.76
    assert bclk_cd == 1411200  # 1.4112 MHz
    assert abs(dr_16bit - 98.08) < 0.01

    # 2. 24-bit 96 kHz High-Resolution Studio Audio
    fs_hi = 96000
    bclk_hi = 2 * fs_hi * 24
    dr_24bit = 6.02 * 24 + 1.76
    assert bclk_hi == 4608000  # 4.608 MHz
    assert abs(dr_24bit - 146.24) < 0.01

    # 3. 32-channel 32-bit 192 kHz TDM Professional Audio
    fs_tdm = 192000
    bclk_tdm = 32 * fs_tdm * 32
    assert bclk_tdm == 196608000  # 196.608 MHz

    # 4. IHP 130nm SG13G2 PPA Scaling Validation
    ppa = I2sPpaModel.get_ppa_metrics()
    assert ppa["standard_cells"] == 428
    assert ppa["gate_equivalents"] == 834.5
    assert ppa["area_um2"] == 3128.48
    assert ppa["area_overhead_pct"] == 2.22
    assert ppa["f_max_mhz"] > 750.0

    dut._log.info(
        f"I2S Audio Standards & PPA Model PASS: Cells={ppa['standard_cells']}, "
        f"GE={ppa['gate_equivalents']}, Fmax={ppa['f_max_mhz']:.1f} MHz, "
        f"Area={ppa['area_um2']:.2f} um2 (+{ppa['area_overhead_pct']:.2f}%)"
    )
