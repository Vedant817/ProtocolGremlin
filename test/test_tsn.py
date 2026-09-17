# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
test/test_tsn.py
================
Cocotb testbench for Ethernet AVB / TSN (IEEE 802.1Qav / IEEE 802.1Qbv / IEEE 1722)
Protocol Engine and Credit-Based Shaper.

Test suite covers:
  1. IEEE 802.1Q VLAN Tagged Frame Transmission:
     ASIC serializes a 7-byte 802.1Q tagged frame:
     [TPID_H=0x81, TPID_L=0x00, TCI_H=0xA0, TCI_L=0x02, EtherType_H=0x22, EtherType_L=0xF0, Payload=0x5A]
     over UART 8-N-1 on pin 3, verified by independent UartReceiver and TsnFrame parser.
  2. Ingress Priority Classification - SR Class A (PCP=5):
     ASIC ingresses 802.1Q TPID/TCI on pin 4, extracts PCP=5 from TCI_H,
     latches TCI into R0/R1, and asserts Class A status R2 = 0x01.
  3. Ingress Priority Classification - SR Class B (PCP=4):
     ASIC ingresses 802.1Q frame with PCP=4, extracts Class B,
     latches TCI into R0/R1, and asserts Class B status R2 = 0x02.
  4. Ingress Priority Classification - Best Effort (PCP=0):
     ASIC ingresses 802.1Q frame with PCP=0, extracts Best Effort,
     latches TCI into R0/R1, and asserts Best Effort status R2 = 0x00.
  5. In-Register Credit-Based Shaper (CBS) & Time-Aware Gate Control:
     ASIC microcode executes CBS credit depletion on frame send, confirms queue gating (R1 = 0xFF),
     recovers via idleSlope, asserts transmission permitted (R2 = 0x00), and validates TAS gate microcode.
  6. IEEE 802.1Qav / 802.1Qbv Standards Behavioral Model & Synthesizable PPA Validation.
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
from tsn_model import (
    TsnTrafficClass,
    TsnEtherType,
    TsnVlanTag,
    TsnFrame,
    CreditBasedShaperModel,
    TsnPpaModel,
    build_tsn_tx_vlan_frame_asm,
    build_tsn_rx_priority_classifier_asm,
    build_tsn_cbs_credit_shaper_asm,
    build_tsn_gate_control_asm,
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
async def test_tsn_tx_vlan_tagged_frame(dut):
    """
    Test 1: IEEE 802.1Q VLAN Tagged Frame Transmission on pin 3:
    Transmits [0x81, 0x00, 0xA0, 0x02, 0x22, 0xF0, 0x5A] (PCP=5, VID=2, AVTP, Payload=0x5A).
    Verified by independent UartReceiver on pin 3 and TsnFrame parser.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    tx_pin = 3
    bit_period = 8
    pcp = 5
    vid = 2
    payload = 0x5A

    words = build_tsn_tx_vlan_frame_asm(
        pcp=pcp,
        vid=vid,
        payload_byte=payload,
        tx_pin=tx_pin,
        bit_period=bit_period
    )
    pin_mask = 1 << tx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    receiver = UartReceiver(bit_period_cycles=bit_period)
    decoded_bytes = []
    max_cycles = 100 + 7 * 14 * bit_period
    core = dut.user_project.u_core

    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        pin_val = (int(dut.uio_out.value) >> tx_pin) & 1
        res = receiver.step(pin_val)
        if res is not None:
            decoded_bytes.append(res)

        if bool(core.halted.value) and receiver.state == UartReceiver.STATE_IDLE and len(decoded_bytes) >= 7:
            break

    assert len(decoded_bytes) == 7, f"Expected 7 bytes, got {len(decoded_bytes)}: {decoded_bytes}"
    expected_bytes = [
        0x81, 0x00,  # TPID (IEEE 802.1Q)
        0xA0, 0x02,  # TCI (PCP=5, DEI=0, VID=2)
        0x22, 0xF0,  # EtherType (IEEE 1722 AVTP)
        0x5A         # Payload
    ]
    assert decoded_bytes == expected_bytes, f"Frame mismatch: expected {expected_bytes}, got {decoded_bytes}"

    parsed = TsnFrame.from_bytes(bytes(decoded_bytes))
    assert parsed.vlan_tag is not None
    assert parsed.vlan_tag.pcp == pcp
    assert parsed.vlan_tag.vid == vid
    assert parsed.ethertype == TsnEtherType.AVTP
    assert parsed.payload == [payload]
    dut._log.info(f"TSN TX 802.1Q Tagged Frame PASS: {[hex(b) for b in decoded_bytes]}")


@cocotb.test()
async def test_tsn_rx_priority_classification_class_a(dut):
    """
    Test 2: Ingress Priority Classification - SR Class A (PCP=5):
    ASIC ingresses 802.1Q TPID/TCI on pin 4 with PCP=5 (TCI_H = 0xA0, TCI_L = 0x02).
    Extracts PCP=5, latches TCI into R0/R1, and asserts Class A return code R2 = 0x01.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rx_pin = 4
    bit_period = 8

    words = build_tsn_rx_priority_classifier_asm(rx_pin=rx_pin, bit_period=bit_period)
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    # Ingress TPID=0x8100, TCI=0xA002 (PCP=5, Class A)
    tx_bytes = [0x81, 0x00, 0xA0, 0x02]

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = []
    for b in tx_bytes:
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

    assert bool(core.halted.value), "Core should halt after priority classification"
    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    assert r0 == 0xA0, f"Expected TCI_H 0xA0 in R0, got 0x{r0:02X}"
    assert r1 == 0x02, f"Expected TCI_L 0x02 in R1, got 0x{r1:02X}"
    assert r2 == 0x01, f"Expected Class A Status R2=0x01, got 0x{r2:02X}"
    dut._log.info(f"TSN Priority Classifier Class A PASS: TCI=0x{r0:02X}{r1:02X}, Status=0x{r2:02X}")


@cocotb.test()
async def test_tsn_rx_priority_classification_class_b(dut):
    """
    Test 3: Ingress Priority Classification - SR Class B (PCP=4):
    ASIC ingresses 802.1Q frame with PCP=4 (TCI_H = 0x80, TCI_L = 0x02).
    Extracts PCP=4, latches TCI into R0/R1, and asserts Class B return code R2 = 0x02.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rx_pin = 4
    bit_period = 8

    words = build_tsn_rx_priority_classifier_asm(rx_pin=rx_pin, bit_period=bit_period)
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    # Ingress TPID=0x8100, TCI=0x8002 (PCP=4, Class B)
    tx_bytes = [0x81, 0x00, 0x80, 0x02]

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = []
    for b in tx_bytes:
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

    assert bool(core.halted.value), "Core should halt after priority classification"
    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    assert r0 == 0x80, f"Expected TCI_H 0x80 in R0, got 0x{r0:02X}"
    assert r1 == 0x02, f"Expected TCI_L 0x02 in R1, got 0x{r1:02X}"
    assert r2 == 0x02, f"Expected Class B Status R2=0x02, got 0x{r2:02X}"
    dut._log.info(f"TSN Priority Classifier Class B PASS: TCI=0x{r0:02X}{r1:02X}, Status=0x{r2:02X}")


@cocotb.test()
async def test_tsn_rx_priority_classification_best_effort(dut):
    """
    Test 4: Ingress Priority Classification - Best Effort (PCP=0):
    ASIC ingresses 802.1Q frame with PCP=0 (TCI_H = 0x00, TCI_L = 0x02).
    Extracts PCP=0, latches TCI into R0/R1, and asserts Best Effort return code R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rx_pin = 4
    bit_period = 8

    words = build_tsn_rx_priority_classifier_asm(rx_pin=rx_pin, bit_period=bit_period)
    pin_mask = 1 << rx_pin
    await _init_dut_and_bootload(dut, words, initial_uio=pin_mask)

    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 8)

    # Ingress TPID=0x8100, TCI=0x0002 (PCP=0, Best Effort)
    tx_bytes = [0x81, 0x00, 0x00, 0x02]

    tx = UartTransmitter(bit_period_cycles=bit_period, pin=rx_pin)
    stream = []
    for b in tx_bytes:
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

    assert bool(core.halted.value), "Core should halt after priority classification"
    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    assert r0 == 0x00, f"Expected TCI_H 0x00 in R0, got 0x{r0:02X}"
    assert r1 == 0x02, f"Expected TCI_L 0x02 in R1, got 0x{r1:02X}"
    assert r2 == 0x00, f"Expected Best Effort Status R2=0x00, got 0x{r2:02X}"
    dut._log.info(f"TSN Priority Classifier Best Effort PASS: TCI=0x{r0:02X}{r1:02X}, Status=0x{r2:02X}")


@cocotb.test()
async def test_tsn_cbs_credit_depletion_and_recovery(dut):
    """
    Test 5: In-Register Credit-Based Shaper (CBS) & Time-Aware Gate Control:
    Executes CBS credit depletion microcode:
    - Initial credit 10, frame cost 25 -> credit drops to -15 (negative).
    - Traps negative credit, asserts queue gating (R1 = 0xFF).
    - Simulates idleSlope recovery (+15) -> credit reaches 0.
    - Confirms non-negative state, asserts transmission permitted (R2 = 0x00).
    - Also verifies IEEE 802.1Qbv Time-Aware Shaper gate control microcode (OPEN -> 1, CLOSED -> 0).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. CBS Credit Depletion and Recovery Microcode
    words_cbs = build_tsn_cbs_credit_shaper_asm(initial_credit=10, frame_cost=25, recover_steps=15)
    await _init_dut_and_bootload(dut, words_cbs)

    core = dut.user_project.u_core
    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after CBS microcode"
    r0 = int(core.r0.value)
    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    assert r0 == 0x00, f"Expected recovered credit 0x00 in R0, got 0x{r0:02X}"
    assert r1 == 0xFF, f"Expected gated flag 0xFF in R1, got 0x{r1:02X}"
    assert r2 == 0x00, f"Expected success status 0x00 in R2, got 0x{r2:02X}"

    # 2. Time-Aware Shaper Gate Open Microcode
    words_open = build_tsn_gate_control_asm(gate_state=1)
    await _init_dut_and_bootload(dut, words_open)

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after TAS gate open"
    assert int(core.r2.value) == 0x01, f"Expected TAS Gate OPEN R2=0x01, got 0x{int(core.r2.value):02X}"

    # 3. Time-Aware Shaper Gate Closed Microcode
    words_closed = build_tsn_gate_control_asm(gate_state=0)
    await _init_dut_and_bootload(dut, words_closed)

    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core should halt after TAS gate closed"
    assert int(core.r2.value) == 0x00, f"Expected TAS Gate CLOSED R2=0x00, got 0x{int(core.r2.value):02X}"
    dut._log.info("TSN CBS Credit Shaper & TAS Gate Control PASS")


@cocotb.test()
async def test_tsn_standards_and_ppa(dut):
    """
    Test 6: IEEE 802.1Qav / 802.1Qbv Standards Behavioral Model & Synthesizable PPA Validation:
    Exercises CreditBasedShaperModel mathematical rate limits, TsnFrame serialization,
    and validates synthesizable hardware coprocessor PPA scaling metrics for IHP 130nm SG13G2.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Credit-Based Shaper Mathematical Behavioral Model
    cbs = CreditBasedShaperModel(port_rate_mbps=100.0, reserved_bw_pct=75.0, max_frame_bytes=1500)
    assert cbs.idle_slope == 75.0
    assert cbs.send_slope == -25.0
    assert cbs.can_transmit() is True

    # Transmit 1500-byte frame: tx_time = 1500 * 8 / 100 = 120 us
    # Credit delta = -25 * 120 = -3000 bits
    recovery_us = cbs.transmit_frame(1500)
    assert cbs.credit < 0.0
    assert cbs.can_transmit() is False
    assert abs(recovery_us - (3000.0 / 75.0)) < 1e-4  # 40 us recovery time

    # Step idle by 40 us: credit should replenish back to 0
    cbs.step_idle(40.0)
    assert cbs.credit == 0.0
    assert cbs.can_transmit() is True

    # 2. VLAN Frame & Serialization Model Validation
    tag = TsnVlanTag(pcp=5, dei=0, vid=100)
    assert tag.pcp == 5
    assert tag.vid == 100
    frame = TsnFrame(vlan_tag=tag, ethertype=TsnEtherType.AVTP, payload=[0x01, 0x02, 0x03])
    raw = frame.to_bytes()
    parsed = TsnFrame.from_bytes(raw)
    assert parsed.vlan_tag.pcp == 5
    assert parsed.vlan_tag.vid == 100
    assert parsed.ethertype == TsnEtherType.AVTP
    assert parsed.payload == [0x01, 0x02, 0x03]

    # 3. PPA Scaling Validation for IHP 130nm SG13G2
    ppa = TsnPpaModel.get_ppa_metrics()
    assert ppa["standard_cells"] == 492
    assert ppa["gate_equivalents"] == 925.0
    assert ppa["area_um2"] == 3596.52
    assert ppa["area_overhead_pct"] == 2.55
    assert ppa["f_max_mhz"] > 700.0
    assert ppa["dynamic_power_uw_10mhz"] < 50.0
    dut._log.info(f"TSN Standards & PPA Model PASS: {ppa}")
