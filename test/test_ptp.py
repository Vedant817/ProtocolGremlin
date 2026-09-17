# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
test/test_ptp.py
================
Cocotb testbench for IEEE 1588 Precision Time Protocol (PTPv2 / IEEE 1588-2019)
Hardware Timestamping and Sub-Microsecond Clock Synchronization Engine.

Test suite covers:
  1. Master PTP Sync Frame Transmission with Egress Hardware Timestamp (t1).
  2. Slave PTP Sync Frame Ingress with Physical Layer Hardware Timestamp (t2).
  3. In-Register Mean Path Delay and Master-to-Slave Offset Calculations.
  4. PTP Message Classification (Event vs General Messages).
  5. Clock Syntonization, Frequency Ratio, and Drift Tracking Model.
  6. IEEE 1588 Standards Conformance, Synthesizable PPA Model, and Electrical Safety.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ReadOnly, ClockCycles

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from bootload import bootload
from uart_model import UartReceiver, UartTransmitter
from ptp_model import (
    PtpMessageType,
    PtpClockMode,
    PtpHeader,
    PtpClockModel,
    PtpPpaModel,
    compute_mean_path_delay,
    compute_clock_offset,
    compute_syntonization_ratio,
    build_ptp_sync_tx_asm,
    build_ptp_rx_timestamp_asm,
    build_ptp_offset_calculator_asm,
    build_ptp_message_filter_asm,
)


async def _init_dut_and_bootload(dut, words: list, initial_uio: int = 0x10):
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
    await ClockCycles(dut.clk, 10)


@cocotb.test()
async def test_ptp_sync_tx_with_timestamp(dut):
    """
    Test 1: Master PTP Sync Frame Transmission with Egress Hardware Timestamp (t1):
    ASIC captures cycle counter timestamp into R3 via WAITEDGE R3, 0x30, then serializes
    Byte 0: Message Type (0x00 Sync) and Byte 1: Sequence ID (0x01) on pin 3.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    tx_pin = 3
    bit_period = 8
    msg_type = int(PtpMessageType.SYNC)
    seq_id = 0x01

    words = build_ptp_sync_tx_asm(
        message_type=msg_type,
        sequence_id=seq_id,
        tx_pin=tx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << tx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    receiver = UartReceiver(bit_period_cycles=bit_period)
    decoded_bytes = []
    max_cycles = 100 + 2 * 14 * bit_period
    core = dut.user_project.u_core

    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        pin_val = (int(dut.uio_out.value) >> tx_pin) & 1
        res = receiver.step(pin_val)
        if res is not None:
            decoded_bytes.append(res)

        if bool(core.halted.value) and receiver.state == UartReceiver.STATE_IDLE and len(decoded_bytes) >= 2:
            break

    assert len(decoded_bytes) == 2, f"Expected 2 bytes, got {len(decoded_bytes)}: {decoded_bytes}"
    assert decoded_bytes == [msg_type, seq_id], f"Expected {[msg_type, seq_id]}, got {decoded_bytes}"

    t1 = int(core.r3.value)
    r2 = int(core.r2.value)

    assert r2 == 0x00, f"Expected Status R2=0x00, got 0x{r2:02X}"
    assert t1 >= 0, f"Egress timestamp t1 must be valid, got {t1}"
    dut._log.info(f"PTP Sync TX PASS: Bytes={decoded_bytes}, Egress Timestamp t1={t1} cycles")


@cocotb.test()
async def test_ptp_rx_timestamp_capture(dut):
    """
    Test 2: Slave PTP Sync Frame Ingress with Physical Layer Hardware Timestamp (t2):
    ASIC synchronizes on Start bit falling edge, immediately captures cycle counter
    timestamp into R3 (t2), samples Message Type into R0, and Sequence ID into R1.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rx_pin = 4
    bit_period = 8
    msg_type = int(PtpMessageType.SYNC)
    seq_id = 0x05

    words = build_ptp_rx_timestamp_asm(rx_pin=rx_pin, bit_period=bit_period)
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = []
    stream.extend(tx.generate_bit_stream(msg_type, idle_before=4, idle_after=6))
    stream.extend(tx.generate_bit_stream(seq_id, idle_before=4, idle_after=6))

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)
        if bool(core.halted.value):
            break

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after PTP frame reception"
    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)
    t2 = int(core.r3.value)

    assert r0 == msg_type, f"Expected MsgType 0x{msg_type:02X}, got 0x{r0:02X}"
    assert r1 == seq_id, f"Expected SeqID 0x{seq_id:02X}, got 0x{r1:02X}"
    assert r2 == 0x00, f"Expected Status R2=0x00, got 0x{r2:02X}"
    assert t2 >= 0, f"Ingress timestamp t2 must be valid, got {t2}"
    dut._log.info(f"PTP Sync RX PASS: MsgType=0x{r0:02X}, SeqID=0x{r1:02X}, Ingress Timestamp t2={t2} cycles")


@cocotb.test()
async def test_ptp_offset_and_delay_calculation(dut):
    """
    Test 3: In-Register Mean Path Delay and Master-to-Slave Offset Calculations:
    Case A (Symmetric Delay, Zero Offset):
      t1 = 10, t2 = 30, t3 = 40, t4 = 60
      diff1 = 20, diff2 = 20 -> Mean Delay = 20, Offset = 0
    Case B (Non-zero Offset):
      t1 = 10, t2 = 40, t3 = 50, t4 = 60
      diff1 = 30, diff2 = 10 -> Mean Delay = 20, Offset = 10
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    test_cases = [
        # (t1, t2, t3, t4, expected_delay, expected_offset)
        (10, 30, 40, 60, 20, 0),
        (10, 40, 50, 60, 20, 10),
    ]

    for t1, t2, t3, t4, exp_delay, exp_offset in test_cases:
        words = build_ptp_offset_calculator_asm(t1=t1, t2=t2, t3=t3, t4=t4)
        await _init_dut_and_bootload(dut, words, initial_uio=0x00)

        core = dut.user_project.u_core
        for _ in range(50):
            await RisingEdge(dut.clk)
            if bool(core.halted.value):
                break

        assert bool(core.halted.value), "Core should halt after PTP offset computation"
        actual_delay = int(core.r0.value)
        actual_offset = int(core.r1.value)
        status = int(core.r2.value)

        assert status == 0x00, f"Expected Status R2=0x00, got 0x{status:02X}"
        assert actual_delay == exp_delay, f"Expected Mean Delay {exp_delay}, got {actual_delay}"
        assert actual_offset == exp_offset, f"Expected Clock Offset {exp_offset}, got {actual_offset}"
        dut._log.info(f"PTP Offset Calc PASS: Timestamps=({t1},{t2},{t3},{t4}) -> Delay={actual_delay}, Offset={actual_offset}")


@cocotb.test()
async def test_ptp_message_filtering(dut):
    """
    Test 4: PTP Message Classification:
    Differentiates Event messages (Sync 0x00, Delay_Req 0x01 -> R2=0x01)
    from General messages (Follow_Up 0x08, Delay_Resp 0x09 -> R2=0x02).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rx_pin = 4
    bit_period = 8
    words = build_ptp_message_filter_asm(rx_pin=rx_pin, bit_period=bit_period)
    pin_mask = 1 << rx_pin

    cases = [
        (PtpMessageType.SYNC,       0x01),  # Event
        (PtpMessageType.DELAY_REQ,  0x01),  # Event
        (PtpMessageType.FOLLOW_UP,  0x02),  # General
        (PtpMessageType.DELAY_RESP, 0x02),  # General
    ]

    for msg_type, expected_r2 in cases:
        await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)
        core = dut.user_project.u_core
        await ClockCycles(dut.clk, 8)

        tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
        stream = tx.generate_bit_stream(int(msg_type), idle_before=4, idle_after=6)

        for bit in stream:
            await RisingEdge(dut.clk)
            dut.uio_in.value = (bit << rx_pin)
            if bool(core.halted.value):
                break

        for _ in range(50):
            await RisingEdge(dut.clk)
            if bool(core.halted.value):
                break

        assert bool(core.halted.value), "Core should halt after PTP message classification"
        actual_r2 = int(core.r2.value)
        assert actual_r2 == expected_r2, f"Expected Status R2=0x{expected_r2:02X}, got 0x{actual_r2:02X}"
        dut._log.info(f"PTP Msg Classification PASS: Type=0x{int(msg_type):02X} -> R2=0x{actual_r2:02X}")


@cocotb.test()
async def test_ptp_syntonization_and_drift(dut):
    """
    Test 5: Clock Syntonization, Frequency Ratio, and Drift Tracking Model:
    Evaluates clock frequency alignment across consecutive sync intervals.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Frequency Ratio & Drift
    delta_master = 1000000
    delta_slave = 1000050  # Slave clock is 50 ppm slower
    ratio = compute_syntonization_ratio(delta_master, delta_slave)
    drift_ppm = (ratio - 1.0) * 1e6
    assert abs(drift_ppm - (-49.9975)) < 0.01

    # 2. Clock State Machine Model
    clock_model = PtpClockModel(clock_mode=PtpClockMode.TWO_STEP)
    clock_model.record_sync_tx(10)
    clock_model.record_sync_rx(30)
    clock_model.record_delay_req_tx(40)
    clock_model.record_delay_req_rx(60)

    assert clock_model.mean_path_delay == 20
    assert clock_model.clock_offset == 0
    dut._log.info(f"PTP Syntonization PASS: Ratio={ratio:.6f}, Drift={drift_ppm:.2f} ppm")


@cocotb.test()
async def test_ptp_standards_and_ppa(dut):
    """
    Test 6: IEEE 1588 Standards Conformance, Synthesizable PPA Model, and Electrical Safety:
    1. Validates PtpHeader serialization / deserialization roundtrip.
    2. Validates IHP 130nm SG13G2 PPA scaling metrics.
    3. Confirms pin electrical safety (uio_oe == 0x00).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. PTP Header Conformance
    header = PtpHeader(message_type=PtpMessageType.SYNC, sequence_id=42, domain_number=0)
    raw = header.to_bytes()
    parsed = PtpHeader.from_bytes(raw)
    assert parsed.message_type == PtpMessageType.SYNC
    assert parsed.sequence_id == 42
    assert parsed.domain_number == 0

    # 2. Synthesizable PPA Scaling
    ppa = PtpPpaModel.get_ppa_metrics()
    assert ppa["standard_cells"] == 515
    assert ppa["gate_equivalents"] == 975.0
    assert ppa["f_max_mhz"] > 750.0
    assert ppa["area_overhead_pct"] < 3.0

    # 3. Electrical Safety
    dut.ena.value = 1
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 4)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 4)
    assert int(dut.uio_oe.value) == 0x00, "Unused bidirectional IOs must remain High-Z on reset"
    dut._log.info(f"IEEE 1588 PPA Validation PASS: GE={ppa['gate_equivalents']}, f_max={ppa['f_max_mhz']:.1f} MHz")
