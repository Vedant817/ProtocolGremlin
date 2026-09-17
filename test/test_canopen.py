# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
test/test_canopen.py
====================
Cocotb testbench for CANopen (CiA 301 / EN 50325-4) & SAE J1939
Higher-Layer Automotive/Industrial Protocol Engine.

Test suite covers:
  1. CANopen NMT Network Management State Machine Transitions:
     Verifies Start Node (CS 0x01 -> Operational 0x05), Stop Node (CS 0x02 -> Stopped 0x04),
     and Pre-operational (CS 0x80 -> Pre-op 0x7F).
  2. CANopen NMT Node Address Discrimination & Bypass:
     Telegram addressed to mismatched Node-ID (0x09 vs 0x05) is bypassed without state change (R2 = 0xAA).
  3. CANopen Heartbeat Frame Production:
     ASIC serializes Heartbeat frame ([Node-ID 0x05, State 0x05]) on pin 3, verified by UartReceiver.
  4. CANopen SDO Expedited Upload (Read) Transactions:
     Matches Object Dictionary Index 0x1017 / Sub 0x00 returning 0x64 (R2 = 0x00);
     mismatched index returns SDO Abort 0x80 with error code R2 = 0xEE.
  5. SAE J1939 29-bit CAN-ID Parsing & PDU1/PDU2 Addressing:
     Differentiates PDU1 peer-to-peer (DA match R2=0x00, mismatch R2=0xAA) from PDU2 broadcast (R2=0x01).
  6. CANopen & J1939 Standards Validation & Synthesizable PPA Model.
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
from canopen_model import (
    CanOpenNmtCommand,
    CanOpenNmtState,
    CanOpenSdoCs,
    CanOpenFrame,
    J1939Frame,
    CanOpenNodeModel,
    CanOpenPpaModel,
    build_j1939_id,
    parse_j1939_id,
    build_canopen_nmt_state_machine_asm,
    build_canopen_heartbeat_generator_asm,
    build_canopen_sdo_expedited_transfer_asm,
    build_j1939_pgn_extractor_asm,
    build_j1939_bam_reassembly_asm,
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
async def test_canopen_nmt_state_transitions(dut):
    """
    Test 1: CANopen NMT Network Management State Machine Transitions:
    ASIC node (configured for Node-ID 0x05) processes NMT command on pin 4:
      - CS = 0x01 (Start Node) -> Transitions to Operational (R3 = 0x05), R2 = 0x00.
      - CS = 0x02 (Stop Node) -> Transitions to Stopped (R3 = 0x04), R2 = 0x00.
      - CS = 0x80 (Pre-Operational) -> Transitions to Pre-op (R3 = 0x7F), R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rx_pin = 4
    bit_period = 8
    node_id = 0x05

    words = build_canopen_nmt_state_machine_asm(
        configured_node_id=node_id,
        rx_pin=rx_pin,
        bit_period=bit_period
    )

    transitions = [
        (CanOpenNmtCommand.START_NODE, CanOpenNmtState.OPERATIONAL),
        (CanOpenNmtCommand.STOP_NODE, CanOpenNmtState.STOPPED),
        (CanOpenNmtCommand.ENTER_PRE_OPERATIONAL, CanOpenNmtState.PRE_OPERATIONAL),
    ]

    pin_mask = 1 << rx_pin

    for cmd_cs, expected_state in transitions:
        await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)
        core = dut.user_project.u_core
        await ClockCycles(dut.clk, 8)

        tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
        stream = []
        # Send CS and Node-ID
        stream.extend(tx.generate_bit_stream(int(cmd_cs), idle_before=4, idle_after=6))
        stream.extend(tx.generate_bit_stream(node_id, idle_before=4, idle_after=6))

        for bit in stream:
            await RisingEdge(dut.clk)
            dut.uio_in.value = (bit << rx_pin)
            if bool(core.halted.value):
                break

        for _ in range(50):
            await RisingEdge(dut.clk)
            if bool(core.halted.value):
                break

        assert bool(core.halted.value), f"Core should halt after processing NMT command 0x{cmd_cs:02X}"
        r3 = int(core.r3.value)
        r2 = int(core.r2.value)

        assert r3 == int(expected_state), f"Expected NMT state 0x{expected_state:02X}, got 0x{r3:02X}"
        assert r2 == 0x00, f"Expected Status R2=0x00, got 0x{r2:02X}"
        dut._log.info(f"CANopen NMT CS 0x{cmd_cs:02X} -> State 0x{r3:02X} PASS")


@cocotb.test()
async def test_canopen_nmt_node_filtering(dut):
    """
    Test 2: CANopen NMT Node Address Discrimination & Bypass:
    Node configured for Node-ID 0x05 receives NMT command addressed to Node-ID 0x09.
    ASIC detects node address mismatch, bypasses state update, and halts with R2 = 0xAA.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rx_pin = 4
    bit_period = 8
    node_id = 0x05

    words = build_canopen_nmt_state_machine_asm(
        configured_node_id=node_id,
        rx_pin=rx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    # Send command addressed to node 0x09 (mismatch!)
    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = []
    stream.extend(tx.generate_bit_stream(int(CanOpenNmtCommand.START_NODE), idle_before=4, idle_after=6))
    stream.extend(tx.generate_bit_stream(0x09, idle_before=4, idle_after=6))

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)
        if bool(core.halted.value):
            break

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt on Node-ID mismatch"
    r2 = int(core.r2.value)
    r3 = int(core.r3.value)

    assert r2 == 0xAA, f"Expected Bypass Status R2=0xAA, got 0x{r2:02X}"
    assert r3 == int(CanOpenNmtState.PRE_OPERATIONAL), f"NMT State should remain unchanged (0x7F), got 0x{r3:02X}"
    dut._log.info(f"CANopen NMT Node Mismatch PASS: Status=0x{r2:02X}, State=0x{r3:02X}")


@cocotb.test()
async def test_canopen_heartbeat_production(dut):
    """
    Test 3: CANopen Heartbeat Frame Production:
    Node 5 transmits Heartbeat frame: [Node_ID=0x05, State=0x05 (Operational)]
    over pin 3, verified by independent UartReceiver.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    tx_pin = 3
    bit_period = 8
    node_id = 0x05
    nmt_state = int(CanOpenNmtState.OPERATIONAL)

    words = build_canopen_heartbeat_generator_asm(
        node_id=node_id,
        nmt_state=nmt_state,
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
    assert decoded_bytes == [node_id, nmt_state], f"Expected {[node_id, nmt_state]}, got {decoded_bytes}"

    r2 = int(core.r2.value)
    assert r2 == 0x00, f"Expected R2=0x00, got 0x{r2:02X}"
    dut._log.info(f"CANopen Heartbeat Production PASS: Node={decoded_bytes[0]}, State=0x{decoded_bytes[1]:02X}")


@cocotb.test()
async def test_canopen_sdo_expedited_transfer(dut):
    """
    Test 4: CANopen SDO Expedited Upload (Read) Transactions:
    1. Matching Index 0x1017, Sub-index 0x00: Returns parameter value 0x64 (R0) with status R2 = 0x00.
    2. Mismatched Index 0x2000, Sub-index 0x00: Returns SDO Abort 0x80 (R0) with status R2 = 0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rx_pin = 4
    bit_period = 8
    target_index = 0x1017
    target_subindex = 0x00
    od_value = 0x64

    words = build_canopen_sdo_expedited_transfer_asm(
        target_index=target_index,
        target_subindex=target_subindex,
        od_value=od_value,
        rx_pin=rx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << rx_pin

    # 1. Matching SDO Read
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)
    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = []
    # CS (0x40 Upload Request), Index_L (0x17), Index_H (0x10), Sub (0x00)
    for b in [int(CanOpenSdoCs.UPLOAD_REQUEST), target_index & 0xFF, (target_index >> 8) & 0xFF, target_subindex]:
        stream.extend(tx.generate_bit_stream(b, idle_before=4, idle_after=6))

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)
        if bool(core.halted.value):
            break

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after SDO read"
    assert int(core.r0.value) == od_value, f"Expected OD value 0x{od_value:02X}, got 0x{int(core.r0.value):02X}"
    assert int(core.r2.value) == 0x00, f"Expected Status R2=0x00, got 0x{int(core.r2.value):02X}"

    # 2. Mismatched SDO Read (Abort)
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)
    await ClockCycles(dut.clk, 8)

    stream_mismatch = []
    # Index 0x2000 (mismatch!)
    for b in [int(CanOpenSdoCs.UPLOAD_REQUEST), 0x00, 0x20, 0x00]:
        stream_mismatch.extend(tx.generate_bit_stream(b, idle_before=4, idle_after=6))

    for bit in stream_mismatch:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)
        if bool(core.halted.value):
            break

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after SDO abort"
    assert int(core.r0.value) == int(CanOpenSdoCs.ABORT_TRANSFER), f"Expected Abort CS 0x80, got 0x{int(core.r0.value):02X}"
    assert int(core.r2.value) == 0xEE, f"Expected Status R2=0xEE, got 0x{int(core.r2.value):02X}"
    dut._log.info("CANopen SDO Expedited Transfer Match & Abort PASS")


@cocotb.test()
async def test_j1939_pgn_extraction_and_addressing(dut):
    """
    Test 5: SAE J1939 29-bit CAN-ID Parsing & PDU1/PDU2 Addressing:
    ASIC parses 4 identifier octets:
      1. PDU1 format with matching DA (0x20): returns R2 = 0x00.
      2. PDU1 format with mismatched DA (0x35): returns R2 = 0xAA (bypassed).
      3. PDU2 format (PF = 0xFE >= 240): returns R2 = 0x01 (broadcast accepted).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rx_pin = 4
    bit_period = 8
    configured_da = 0x20

    words = build_j1939_pgn_extractor_asm(
        configured_da=configured_da,
        rx_pin=rx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << rx_pin

    cases = [
        # (Byte 0 Prio/DP, Byte 1 PF, Byte 2 PS/DA, Byte 3 SA, Expected Status R2)
        (0x06, 0x00, configured_da, 0x80, 0x00), # PDU1 Match
        (0x06, 0x00, 0x35,          0x80, 0xAA), # PDU1 Mismatch
        (0x06, 0xFE, 0x01,          0x80, 0x01), # PDU2 Broadcast
    ]

    for b0, b1, b2, b3, expected_r2 in cases:
        await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)
        core = dut.user_project.u_core
        await ClockCycles(dut.clk, 8)

        tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
        stream = []
        for b in [b0, b1, b2, b3]:
            stream.extend(tx.generate_bit_stream(b, idle_before=4, idle_after=6))

        for bit in stream:
            await RisingEdge(dut.clk)
            dut.uio_in.value = (bit << rx_pin)
            if bool(core.halted.value):
                break

        for _ in range(50):
            await RisingEdge(dut.clk)
            if bool(core.halted.value):
                break

        assert bool(core.halted.value), "Core should halt after J1939 ID parsing"
        actual_r2 = int(core.r2.value)
        assert actual_r2 == expected_r2, f"Expected R2=0x{expected_r2:02X}, got 0x{actual_r2:02X}"
        dut._log.info(f"SAE J1939 ID PF=0x{b1:02X}, PS=0x{b2:02X} -> R2=0x{actual_r2:02X} PASS")


@cocotb.test()
async def test_canopen_standards_and_ppa(dut):
    """
    Test 6: CANopen & J1939 Standards Validation & Synthesizable PPA Model:
    1. Validates J1939 BAM reassembly microcode (sequence continuity 1 -> 2).
    2. Validates CanOpenNodeModel and J1939Frame roundtrip.
    3. Confirms IHP 130nm SG13G2 PPA scaling metrics.
    4. Confirms pin electrical safety (uio_oe == 0x00).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. J1939 BAM Reassembly Microcode
    rx_pin = 4
    bit_period = 8
    words_bam = build_j1939_bam_reassembly_asm(rx_pin=rx_pin, bit_period=bit_period)
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words_bam, initial_uio=pin_mask)

    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = []
    # Packet 1 (Seq 1, Data 0x11), Packet 2 (Seq 2, Data 0x22)
    for b in [0x01, 0x11, 0x02, 0x22]:
        stream.extend(tx.generate_bit_stream(b, idle_before=4, idle_after=6))

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)
        if bool(core.halted.value):
            break

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after BAM reassembly"
    assert int(core.r2.value) == 0x00, f"Expected Status R2=0x00, got 0x{int(core.r2.value):02X}"

    # 2. Python Reference Model Checks
    # PDU1 Peer-to-Peer frame (PGN 59904 / 0xEA00 Request, DA = 0x20)
    frame_pdu1 = J1939Frame(priority=3, pgn=59904, da=0x20, sa=0x00, data=[1, 2, 3, 4, 5, 6, 7, 8])
    raw_1 = frame_pdu1.to_bytes()
    parsed_1 = J1939Frame.from_bytes(raw_1)
    assert parsed_1.priority == 3
    assert parsed_1.pgn == 59904
    assert parsed_1.da == 0x20
    assert parsed_1.data == [1, 2, 3, 4, 5, 6, 7, 8]

    # PDU2 Broadcast frame (PGN 61444 / 0xF004 EEC1, DA = 0xFF broadcast)
    frame_pdu2 = J1939Frame(priority=6, pgn=61444, sa=0x00, data=[1, 2, 3, 4, 5, 6, 7, 8])
    raw_2 = frame_pdu2.to_bytes()
    parsed_2 = J1939Frame.from_bytes(raw_2)
    assert parsed_2.priority == 6
    assert parsed_2.pgn == 61444
    assert parsed_2.da == 0xFF
    assert parsed_2.data == [1, 2, 3, 4, 5, 6, 7, 8]

    # 3. PPA Model Validation
    ppa = CanOpenPpaModel.get_ppa_metrics()
    assert ppa["standard_cells"] == 498
    assert ppa["gate_equivalents"] == 938.0
    assert ppa["f_max_mhz"] > 700.0
    assert ppa["area_overhead_pct"] < 3.0

    # 4. Electrical Safety
    assert int(dut.uio_oe.value) == 0x00, "Unused bidirectional IOs must remain High-Z on reset"
    dut._log.info(f"CANopen & J1939 PPA Validation PASS: PPA GE={ppa['gate_equivalents']}, f_max={ppa['f_max_mhz']:.1f} MHz")
