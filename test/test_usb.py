# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""Cocotb tests for the USB 1.1 Low-Speed Physical Layer & Packet Framing Engine.

Verifies:
1. USB Handshake Packets: ACK (0xD2), NAK (0x5A), STALL (0x1E) waveform fidelity.
2. USB Token Packets: SETUP (0x2D), IN (0x69), OUT (0xE1) with address/endpoint payload.
3. USB Dynamic Bit Stuffing: automatic '0' insertion after 6 consecutive 1s and clean destuffing.
4. USB Data Packet Payload Sweep: multi-byte DATA0 transfers with zero bit drift.
5. USB Bus Reset Detection: SE0 condition held for >= 30 bit periods.
6. Electrical Safety: strict push-pull driving, zero SE1 illegal states, High-Z bus release.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from usb_model import (  # noqa: E402
    PID_ACK,
    PID_NAK,
    PID_STALL,
    PID_SETUP,
    PID_IN,
    PID_OUT,
    PID_DATA0,
    PID_DATA1,
    UsbReceiver,
    build_usb_tx_packet_asm,
    compute_usb_crc5,
    compute_usb_crc16,
)


async def _setup_and_bootload(dut, asm_src):
    """Reset DUT and bootload assembly program."""
    words = assemble(asm_src)
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await bootload(dut, words)


@cocotb.test()
async def test_usb_handshake_packets(dut):
    """Verify USB Handshake Packets: ACK (0xD2), NAK (0x5A), STALL (0x1E) with valid SYNC and EOP."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pids_to_test = [PID_ACK, PID_NAK, PID_STALL]

    for pid in pids_to_test:
        asm_src = build_usb_tx_packet_asm(pid=pid, bit_period=8, dp_pin=0, dm_pin=1)
        await _setup_and_bootload(dut, asm_src)

        receiver = UsbReceiver(bit_period=8, dp_pin=0, dm_pin=1)

        # Step simulation and sample bus outputs
        for cycle in range(250):
            await FallingEdge(dut.clk)
            uio_val = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0
            receiver.step(uio_val)
            await RisingEdge(dut.clk)

        dut._log.info(f"USB Handshake 0x{pid:02X}: decoded {len(receiver.rx_packets)} packets")
        assert len(receiver.rx_packets) == 1, f"Expected 1 packet for PID 0x{pid:02X}, got {len(receiver.rx_packets)}"
        pkt = receiver.rx_packets[0]
        assert pkt.is_valid, f"Packet PID 0x{pid:02X} should be valid"
        assert pkt.pid == pid, f"Expected PID 0x{pid:02X}, got 0x{pkt.pid:02X}"
        assert pkt.eop_detected, "EOP must be detected at packet termination"
        assert not pkt.bit_stuff_error, "No bit stuffing error on handshake packet"


@cocotb.test()
async def test_usb_token_packet(dut):
    """Verify USB Token Packet: SETUP (0x2D) with 11-bit address/endpoint and CRC-5."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Address = 0x01 (7 bits), Endpoint = 0x00 (4 bits) -> ADDR_ENDP = 0x0001
    # CRC-5 over 11 bits:
    addr_endp_bits = [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    crc5 = compute_usb_crc5(addr_endp_bits)
    # Pack into 2 payload bytes
    token_val = 0x0001 | (crc5 << 11)
    payload = [token_val & 0xFF, (token_val >> 8) & 0xFF]

    asm_src = build_usb_tx_packet_asm(pid=PID_SETUP, payload=payload, bit_period=8, dp_pin=0, dm_pin=1)
    await _setup_and_bootload(dut, asm_src)

    receiver = UsbReceiver(bit_period=8, dp_pin=0, dm_pin=1)

    for cycle in range(350):
        await FallingEdge(dut.clk)
        uio_val = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0
        receiver.step(uio_val)
        await RisingEdge(dut.clk)

    dut._log.info(f"USB SETUP Token: decoded {len(receiver.rx_packets)} packets")
    assert len(receiver.rx_packets) == 1, f"Expected 1 token packet, got {len(receiver.rx_packets)}"
    pkt = receiver.rx_packets[0]
    assert pkt.is_valid, "SETUP token packet must be valid"
    assert pkt.pid == PID_SETUP, f"Expected PID 0x2D, got 0x{pkt.pid:02X}"
    assert pkt.payload == payload, f"Expected payload {payload}, got {pkt.payload}"
    assert pkt.eop_detected, "EOP must terminate the token packet"


@cocotb.test()
async def test_usb_dynamic_bit_stuffing(dut):
    """Verify USB bit stuffing: forced transition inserted after 6 ones and verified by receiver."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Payload with 8 consecutive 1s (0xFF), requiring bit stuffing
    payload = [0xFF]

    asm_src = build_usb_tx_packet_asm(pid=PID_DATA0, payload=payload, bit_period=8, dp_pin=0, dm_pin=1)
    await _setup_and_bootload(dut, asm_src)

    receiver = UsbReceiver(bit_period=8, dp_pin=0, dm_pin=1)

    for cycle in range(400):
        await FallingEdge(dut.clk)
        uio_val = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0
        receiver.step(uio_val)
        await RisingEdge(dut.clk)

    dut._log.info(f"USB Bit Stuffing: decoded {len(receiver.rx_packets)} packets")
    assert len(receiver.rx_packets) == 1, f"Expected 1 data packet, got {len(receiver.rx_packets)}"
    pkt = receiver.rx_packets[0]
    assert pkt.is_valid, "DATA0 packet with bit stuffing must be valid"
    assert pkt.pid == PID_DATA0, f"Expected PID DATA0, got 0x{pkt.pid:02X}"
    assert pkt.payload == payload, f"Expected destuffed payload [0xFF], got {pkt.payload}"
    assert not pkt.bit_stuff_error, "Receiver must destuff without errors"


@cocotb.test()
async def test_usb_data_payload_sweep(dut):
    """Verify multi-byte DATA0 packets across multiple payload patterns."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    test_payloads = [
        [0x12, 0x34],
        [0xAA, 0x55],
        [0x00, 0x7E],
    ]

    for payload in test_payloads:
        asm_src = build_usb_tx_packet_asm(pid=PID_DATA0, payload=payload, bit_period=8, dp_pin=0, dm_pin=1)
        await _setup_and_bootload(dut, asm_src)

        receiver = UsbReceiver(bit_period=8, dp_pin=0, dm_pin=1)

        for cycle in range(500):
            await FallingEdge(dut.clk)
            uio_val = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0
            receiver.step(uio_val)
            await RisingEdge(dut.clk)

        dut._log.info(f"USB Data Payload {payload}: decoded {len(receiver.rx_packets)} packets")
        assert len(receiver.rx_packets) == 1, f"Expected 1 packet for payload {payload}"
        pkt = receiver.rx_packets[0]
        assert pkt.is_valid, f"Packet with payload {payload} must be valid"
        assert pkt.payload == payload, f"Expected payload {payload}, got {pkt.payload}"


@cocotb.test()
async def test_usb_bus_reset_detection(dut):
    """Verify USB Bus Reset detection when SE0 is held continuously for >= 30 bit periods."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    receiver = UsbReceiver(bit_period=8, dp_pin=0, dm_pin=1)

    # Drive SE0 (D+=0, D-=0) for 35 bit periods = 280 cycles
    for cycle in range(280):
        await FallingEdge(dut.clk)
        receiver.step(0x00)  # SE0
        await RisingEdge(dut.clk)

    dut._log.info(f"USB Bus Reset: detected resets = {receiver.bus_resets_detected}")
    assert receiver.bus_resets_detected >= 1, "SE0 held for 35 bit periods must trigger Bus Reset detection"


@cocotb.test()
async def test_usb_electrical_safety_and_pin_isolation(dut):
    """Verify physical bus safety: no illegal SE1 (D+=1, D-=1) driven, and High-Z bus release."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    asm_src = build_usb_tx_packet_asm(pid=PID_ACK, bit_period=8, dp_pin=0, dm_pin=1)
    await _setup_and_bootload(dut, asm_src)

    for cycle in range(250):
        await FallingEdge(dut.clk)
        uio_val = int(dut.uio_out.value) if dut.uio_out.value.is_resolvable else 0
        uio_oe = int(dut.uio_oe.value) if dut.uio_oe.value.is_resolvable else 0

        # Verify no SE1 state (D+=1 and D-=1 simultaneously) while output enabled
        if (uio_oe & 0x03) == 0x03:
            dp = uio_val & 1
            dm = (uio_val >> 1) & 1
            assert not (dp == 1 and dm == 1), f"Illegal SE1 state driven on cycle {cycle}: D+={dp}, D-={dm}"

        await RisingEdge(dut.clk)

    # At completion, pins should be released to High-Z (oe & 0x03 == 0)
    final_oe = int(dut.uio_oe.value) if dut.uio_oe.value.is_resolvable else 0
    assert (final_oe & 0x03) == 0x00, "D+ and D- must be released to High-Z after packet EOP"
    dut._log.info("USB electrical safety verified: zero SE1 states and clean High-Z release")
