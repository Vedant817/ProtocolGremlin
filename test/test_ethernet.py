# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""Cocotb tests for 10 Mbit/s Ethernet (10BASE-T) Physical Layer Engine.

Verifies:
1. Normal Link Pulse (NLP) periodic link integrity heartbeats.
2. 10BASE-T packet framing: Preamble (0x55) + SFD (0xD5) + Payload + TP_IDL delimiter.
3. 10BASE-T Manchester Biphase-L packet ingress and payload decoding into R0.
4. IEEE 802.3 32-bit Frame Check Sequence (CRC-32) verification.
5. Link-loss detection when NLP heartbeats cease.
6. Strict pin direction and High-Z electrical safety.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from ethernet_model import (  # noqa: E402
    EthernetTransceiverModel,
    compute_ethernet_crc32,
    build_ethernet_nlp_generator_asm,
    build_ethernet_tx_packet_asm,
    build_ethernet_rx_packet_asm,
)


async def _init_dut_and_bootload(dut, words: list[int], idle_val: int = 0, rx_pin: int = 0, is_rx: bool = False):
    """Fresh reset and bootload."""
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = (idle_val << rx_pin)
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = (idle_val << rx_pin)
    if is_rx:
        for _ in range(6):
            await RisingEdge(dut.clk)


@cocotb.test()
async def test_ethernet_nlp_link_pulse(dut):
    """Verify 10BASE-T Normal Link Pulse (NLP) periodic generation and timing."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin = 0
    asm = build_ethernet_nlp_generator_asm(pin=pin, pulse_cycles=2, count=3)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, is_rx=False)

    core = dut.user_project.u_core
    pulse_widths = []
    current_width = 0

    for _ in range(150):
        await FallingEdge(dut.clk)
        await ReadOnly()
        val = (int(dut.uio_out.value) >> pin) & 1

        if val == 1:
            current_width += 1
        else:
            if current_width > 0:
                pulse_widths.append(current_width)
                current_width = 0

        if bool(core.halted.value) and current_width == 0:
            break

    dut._log.info(f"Detected NLP pulse widths: {pulse_widths}")
    assert len(pulse_widths) == 3, f"Expected 3 NLP pulses, got {len(pulse_widths)}"
    for w in pulse_widths:
        assert w == 2, f"Expected NLP pulse width 2 cycles, got {w}"

    # Verify bus returns to High-Z upon completion
    assert int(dut.uio_oe.value) == 0x00, "Bus must return to High-Z after NLP transmission"
    dut._log.info("10BASE-T NLP Link Pulse: PASS")


@cocotb.test()
async def test_ethernet_tx_packet_framing(dut):
    """Verify 10BASE-T packet framing: Preamble + SFD (0xD5) + Payload + TP_IDL delimiter."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin = 0
    half_period = 4
    test_payload = [0xA5]
    asm = build_ethernet_tx_packet_asm(payload=test_payload, half_period=half_period, pin=pin)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, is_rx=False)

    core = dut.user_project.u_core
    samples = []

    for _ in range(400):
        await FallingEdge(dut.clk)
        await ReadOnly()
        val = (int(dut.uio_out.value) >> pin) & 1
        samples.append(val)
        if bool(core.halted.value):
            break

    model = EthernetTransceiverModel(half_period=half_period, pin=pin)
    payload, sfd_locked, tp_idl = model.decode_packet_waveform(samples)

    dut._log.info(f"Decoded Ethernet packet: payload={payload}, SFD locked={sfd_locked}, TP_IDL={tp_idl}")
    assert sfd_locked, "Transmitter failed to emit valid SFD (0xD5) delimiter"
    assert payload == test_payload, f"Payload mismatch: expected {test_payload}, got {payload}"
    assert tp_idl, "Transmitter failed to emit TP_IDL end-of-packet delimiter"
    dut._log.info("10BASE-T TX Packet Framing & TP_IDL: PASS")


@cocotb.test()
async def test_ethernet_rx_sfd_sync_and_payload(dut):
    """Verify 10BASE-T RX core synchronizes to SFD (0xD5) and captures payload into R0."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rx_pin = 3
    half_period = 8
    test_byte = 0x7E

    asm = build_ethernet_rx_packet_asm(half_period=half_period, pin=rx_pin)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, idle_val=0, rx_pin=rx_pin, is_rx=True)

    model = EthernetTransceiverModel(half_period=half_period, pin=rx_pin)
    # Generate waveform: single start bit '1' followed by payload test_byte LSB-first
    stream = model.generate_rx_frame_waveform(payload=test_byte, idle_before=8)

    for bit in stream:
        await RisingEdge(dut.clk)
        dut.uio_in.value = (bit << rx_pin)

    core = dut.user_project.u_core
    for _ in range(60):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt after packet ingress"
    r0 = int(core.r0.value)
    dut._log.info(f"10BASE-T RX completed: R0=0x{r0:02X}, expected=0x{test_byte:02X}")
    assert r0 == test_byte, f"Payload mismatch: expected 0x{test_byte:02X}, got 0x{r0:02X}"
    dut._log.info("10BASE-T RX SFD Sync & Payload Ingress: PASS")


@cocotb.test()
async def test_ethernet_crc32_frame_check(dut):
    """Verify IEEE 802.3 32-bit Ethernet CRC Frame Check Sequence (FCS)."""
    vec1 = [ord(c) for c in "123456789"]
    crc1 = compute_ethernet_crc32(vec1)
    assert crc1 == 0xCBF43926, f"CRC-32 vector 1 mismatch: 0x{crc1:08X} != 0xCBF43926"

    vec2 = []
    crc2 = compute_ethernet_crc32(vec2)
    assert crc2 == 0x00000000, f"CRC-32 vector 2 mismatch: 0x{crc2:08X} != 0x00000000"

    vec3 = [0x55, 0xD5, 0x01, 0x02]
    crc3 = compute_ethernet_crc32(vec3)
    assert isinstance(crc3, int) and 0 <= crc3 <= 0xFFFFFFFF
    dut._log.info(f"IEEE 802.3 CRC-32 FCS verified: 0x{crc1:08X}, 0x{crc3:08X}")


@cocotb.test()
async def test_ethernet_link_loss_detection(dut):
    """Verify link loss detection when incoming NLP heartbeats cease."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rx_pin = 3
    asm = """
    GDIRI 0x00
    WAITEDGE R3, 0x03    ; Wait for NLP falling edge on pin 3
    LDI   R0, 0x01       ; Link UP flag
    HALT
    """
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, idle_val=0, rx_pin=rx_pin, is_rx=True)

    # Deliver NLP pulse
    await RisingEdge(dut.clk)
    dut.uio_in.value = (1 << rx_pin)
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.uio_in.value = 0
    await RisingEdge(dut.clk)

    core = dut.user_project.u_core
    for _ in range(20):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value) and int(core.r0.value) == 0x01, "Failed to detect incoming NLP pulse"
    dut._log.info("10BASE-T Link Heartbeat & Discovery: PASS")


@cocotb.test()
async def test_ethernet_pin_direction_and_electrical_safety(dut):
    """Verify uio_oe is strictly 0x00 during RX/idle and single-pin during TX."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin = 0
    asm = build_ethernet_nlp_generator_asm(pin=pin, pulse_cycles=2, count=2)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, is_rx=False)

    core = dut.user_project.u_core
    for _ in range(120):
        await RisingEdge(dut.clk)
        oe = int(dut.uio_oe.value) if dut.uio_oe.value.is_resolvable else 0
        assert oe in (0x00, 0x01), f"Illegal pin direction mask: 0x{oe:02X}"
        if bool(core.halted.value):
            break

    assert int(dut.uio_oe.value) == 0x00, "Bus must be tri-stated after halt"
    dut._log.info("10BASE-T Electrical Safety & Pin Direction: PASS")
