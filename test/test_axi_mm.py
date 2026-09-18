# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_axi_mm.py - Cocotb test suite for AXI4/AXI5 Memory-Mapped (AXI4-MM)
On-Chip Interconnect & Burst Controller Engine.

Verifies:
1. test_axi_mm_master_packet_transmission: Master transmits SYNC_SOF delimiter (0xA5)
   followed by AR_REQ channel byte (0x01) and Target Address (0x40) on pin 3 via SHIFTOUT (MSB-first),
   verified at baud center with status R2 = 0x00.
2. test_axi_mm_rx_beat_ingress: Slave synchronizes to SYNC_SOF rising edge on pin 3 via WAITEDGE,
   samples channel beat byte MSB-first into R0 and preserves in R1 (0x02 R_DATA) with status R2 = 0x00.
3. test_axi_mm_channel_filter_and_fault_trapping: In-register validation of AXI4-MM channel commands
   (valid 0x01 AR_REQ, 0x02 R_DATA, 0x03 AW_REQ, 0x04 W_DATA, 0x05 B_RESP, 0x06 ATOMIC_REQ -> R2 = 0x00;
   illegal 0x7F trapped with R2 = 0xEE).
4. test_axi_mm_credit_tracking_and_underflow_trapping: In-register validation of outstanding transaction
   credit increment (Response beat completed), decrement (Request issued), and underflow prevention (credits=0 -> R2 = 0xEE).
5. test_axi_mm_packet_framing_burst_and_receiver: Packet encapsulation, burst address computation
   (FIXED, INCR, WRAP with wrap boundary wrapping), CCITT CRC-16 (0x1021) protection, and AxiMmReceiverModel link lock state machine.
6. test_axi_mm_standards_and_ppa: Burst types, response codes, channel opcodes, CRC-16 determinism,
   and calibrated IHP 130nm SG13G2 PPA model.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from axi_mm_model import (  # noqa: E402
    AxBurst,
    AxResp,
    AxiMmChannel,
    compute_axi_mm_burst_addresses,
    compute_axi_mm_crc16,
    encode_axi_mm_packet,
    decode_axi_mm_packet,
    AxiMmReceiverModel,
    AxiMmPpaModel,
    build_axi_mm_tx_beat_asm,
    build_axi_mm_rx_beat_asm,
    build_axi_mm_channel_filter_asm,
    build_axi_mm_credit_tracker_asm,
)


async def _init_dut_and_bootload(dut, words: list, initial_uio: int = 0x00):
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


@cocotb.test()
async def test_axi_mm_master_packet_transmission(dut):
    """
    Test 1: Master AXI4-MM Packet Header Transmission:
    Transmits SYNC_SOF delimiter (0xA5) followed by AR_REQ channel byte (0x01) and Target Address (0x40)
    on pin 3 via SHIFTOUT (MSB-first).
    Verifies bit timings, captured MSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sync = int(AxiMmChannel.SYNC_SOF)        # 0xA5 (0b10100101)
    expected_channel = int(AxiMmChannel.AR_REQ)      # 0x01
    expected_addr = 0x40                             # Target Address 0x40

    asm_code = build_axi_mm_tx_beat_asm(
        sync_code=expected_sync,
        channel_cmd=expected_channel,
        target_addr=expected_addr,
        pin_tx=pin_tx,
        baud_cycles=baud_cycles,
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    max_cycles = 400

    captured_bits = []

    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_oe = int(dut.uio_oe.value)
        if (uio_oe & (1 << pin_tx)) != 0:
            bit_val = (int(dut.uio_out.value) >> pin_tx) & 1
            captured_bits.append(bit_val)

        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt within timeout"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00 (Success), got 0x{int(core.r2.value):02X}"

    # Sample each bit at baud midpoint (cycles: 2, 6, 10, 14, ...)
    sampled_bits = []
    idx = 2
    step = baud_cycles
    while idx < len(captured_bits):
        sampled_bits.append(captured_bits[idx])
        idx += step

    assert len(sampled_bits) >= 24, f"Expected >= 24 bits (3 bytes), got {len(sampled_bits)}"

    # Reconstruct bytes MSB-first
    byte0 = sum(sampled_bits[b] << (7 - b) for b in range(8))
    byte1 = sum(sampled_bits[8 + b] << (7 - b) for b in range(8))
    byte2 = sum(sampled_bits[16 + b] << (7 - b) for b in range(8))

    assert byte0 == expected_sync, f"Byte 0 (SYNC_SOF): expected 0x{expected_sync:02X}, got 0x{byte0:02X}"
    assert byte1 == expected_channel, f"Byte 1 (Channel AR_REQ): expected 0x{expected_channel:02X}, got 0x{byte1:02X}"
    assert byte2 == expected_addr, f"Byte 2 (Target Address): expected 0x{expected_addr:02X}, got 0x{byte2:02X}"


@cocotb.test()
async def test_axi_mm_rx_beat_ingress(dut):
    """
    Test 2: Slave AXI4-MM SYNC_SOF Ingress & Channel Beat Sampling:
    Core synchronizes to SYNC_SOF delimiter rising edge on pin 3 via WAITEDGE (bit 7 = 1),
    strides to bit midpoint, samples 8 bits MSB-first into R0, preserves in R1 (0x02 R_DATA),
    and halts with status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    sync_code = int(AxiMmChannel.SYNC_SOF)        # 0xA5 (0b10100101)
    target_channel = int(AxiMmChannel.R_DATA)     # 0x02

    asm_code = build_axi_mm_rx_beat_asm(pin_rx=pin_rx, baud_cycles=baud_cycles)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let receiver reach WAITEDGE
    await ClockCycles(dut.clk, 10)

    # 1. Transmit SYNC_SOF delimiter MSB-first
    for bit_idx in range(7, -1, -1):
        bit = (sync_code >> bit_idx) & 1
        dut.uio_in.value = (bit << pin_rx)
        await ClockCycles(dut.clk, baud_cycles)

    # 2. Transmit target channel byte MSB-first
    for bit_idx in range(7, -1, -1):
        bit = (target_channel >> bit_idx) & 1
        dut.uio_in.value = (bit << pin_rx)
        await ClockCycles(dut.clk, baud_cycles)

    # Hold last bit for another baud period before bus goes idle low to ensure stable sampling
    await ClockCycles(dut.clk, baud_cycles)
    dut.uio_in.value = 0x00

    # Wait for halt
    for _ in range(50):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value), "Core did not halt after RX sync"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00 (Success), got 0x{int(core.r2.value):02X}"
    assert int(core.r0.value) == target_channel, f"Expected R0=0x{target_channel:02X}, got 0x{int(core.r0.value):02X}"
    assert int(core.r1.value) == target_channel, f"Expected R1=0x{target_channel:02X}, got 0x{int(core.r1.value):02X}"


@cocotb.test()
async def test_axi_mm_channel_filter_and_fault_trapping(dut):
    """
    Test 3: In-Register AXI4-MM Channel Filtering:
    - Valid channels:
      0x01 (AR_REQ), 0x02 (R_DATA), 0x03 (AW_REQ), 0x04 (W_DATA),
      0x05 (B_RESP), 0x06 (ATOMIC_REQ) -> R2 = 0x00
    - Invalid channel:
      0x7F -> Trapped with R2 = 0xEE
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    core = dut.user_project.u_core

    valid_channels = [
        int(AxiMmChannel.AR_REQ),
        int(AxiMmChannel.R_DATA),
        int(AxiMmChannel.AW_REQ),
        int(AxiMmChannel.W_DATA),
        int(AxiMmChannel.B_RESP),
        int(AxiMmChannel.ATOMIC_REQ),
    ]

    for ch in valid_channels:
        asm_code = build_axi_mm_channel_filter_asm(test_channel=ch)
        words = assemble("\n".join(asm_code))
        await _init_dut_and_bootload(dut, words)

        for _ in range(50):
            if bool(core.halted.value):
                break
            await RisingEdge(dut.clk)

        assert bool(core.halted.value), f"Core did not halt on channel 0x{ch:02X}"
        assert int(core.r2.value) == 0x00, f"Channel 0x{ch:02X} failed validation, R2=0x{int(core.r2.value):02X}"

    # Test illegal channel 0x7F -> must trap with R2 = 0xEE
    asm_code_fault = build_axi_mm_channel_filter_asm(test_channel=0x7F)
    words_fault = assemble("\n".join(asm_code_fault))
    await _init_dut_and_bootload(dut, words_fault)

    for _ in range(50):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value), "Core did not halt on illegal channel 0x7F"
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE (Fault Trap), got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_axi_mm_credit_tracking_and_underflow_trapping(dut):
    """
    Test 4: In-Register Outstanding Transaction Credit Accounting:
    - Event 1: Response completed -> increments credit (initial 4 -> 5), R2 = 0x00
    - Event 2: Request sent -> decrements credit (initial 4 -> 3), R2 = 0x00
    - Event 2 with initial credits = 0 -> underflow trapped with R2 = 0xEE
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    core = dut.user_project.u_core

    # Case A: Response completed (credit return)
    asm_return = build_axi_mm_credit_tracker_asm(event_type=1, initial_credits=4)
    words_return = assemble("\n".join(asm_return))
    await _init_dut_and_bootload(dut, words_return)

    for _ in range(40):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value), "Core did not halt on credit return"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00, got 0x{int(core.r2.value):02X}"
    assert int(core.r0.value) == 5, f"Expected credits=5, got {int(core.r0.value)}"

    # Case B: Request sent (credit consume)
    asm_send = build_axi_mm_credit_tracker_asm(event_type=2, initial_credits=4)
    words_send = assemble("\n".join(asm_send))
    await _init_dut_and_bootload(dut, words_send)

    for _ in range(40):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value), "Core did not halt on credit consume"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00, got 0x{int(core.r2.value):02X}"
    assert int(core.r0.value) == 3, f"Expected credits=3, got {int(core.r0.value)}"

    # Case C: Request sent with 0 credits -> Underflow trap
    asm_underflow = build_axi_mm_credit_tracker_asm(event_type=2, initial_credits=0)
    words_underflow = assemble("\n".join(asm_underflow))
    await _init_dut_and_bootload(dut, words_underflow)

    for _ in range(40):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value), "Core did not halt on credit underflow"
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE (Underflow Trap), got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_axi_mm_packet_framing_burst_and_receiver(dut):
    """
    Test 5: AXI4-MM Packet Framing, Burst Address Calculation & Receiver Link Model:
    - Encodes and decodes packets across channels.
    - Validates FIXED, INCR, and WRAP burst address calculations.
    - Validates 16-bit CCITT CRC integrity and corruption rejection.
    - Verifies AxiMmReceiverModel link lock acquisition upon 4 consecutive valid frames.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Burst Address Verification
    # FIXED: 4 beats at address 0x10 -> all 0x10
    fixed_addrs = compute_axi_mm_burst_addresses(start_addr=0x10, axsize=0, axlen=3, axburst=AxBurst.FIXED)
    assert fixed_addrs == [0x10, 0x10, 0x10, 0x10], f"Fixed burst mismatch: {fixed_addrs}"

    # INCR: 4 beats of 2 bytes starting at 0x10 -> 0x10, 0x12, 0x14, 0x16
    incr_addrs = compute_axi_mm_burst_addresses(start_addr=0x10, axsize=1, axlen=3, axburst=AxBurst.INCR)
    assert incr_addrs == [0x10, 0x12, 0x14, 0x16], f"Incr burst mismatch: {incr_addrs}"

    # WRAP: 4 beats of 4 bytes (total 16-byte wrap container e.g. 0x20..0x2F), start at 0x2C
    # container boundary = 0x20. Sequence: 0x2C, 0x20, 0x24, 0x28
    wrap_addrs = compute_axi_mm_burst_addresses(start_addr=0x2C, axsize=2, axlen=3, axburst=AxBurst.WRAP)
    assert wrap_addrs == [0x2C, 0x20, 0x24, 0x28], f"Wrap burst mismatch: {wrap_addrs}"

    # 2. Receiver Model Verification
    receiver = AxiMmReceiverModel(initial_credits=4)
    assert not receiver.link_lock

    # Send 4 valid frames
    channels_to_send = [
        AxiMmChannel.AR_REQ,
        AxiMmChannel.R_DATA,
        AxiMmChannel.AW_REQ,
        AxiMmChannel.B_RESP,
    ]

    for ch in channels_to_send:
        pkt = encode_axi_mm_packet(
            channel=ch,
            axid=1,
            addr_or_data=0x40,
            burst_type=AxBurst.INCR,
            resp=AxResp.OKAY,
            payload=b"\x12\x34",
        )
        ok = receiver.process_packet(pkt["raw_bytes"])
        assert ok, f"Failed to process packet for channel {ch}"

    assert receiver.packets_received == 4
    assert receiver.link_lock, "Receiver should have acquired link lock after 4 consecutive syncs"
    assert receiver.read_requests == 1
    assert receiver.write_requests == 1
    assert receiver.responses_completed == 2
    assert receiver.outstanding_credits == 4

    # Corrupted frame rejection
    bad_pkt = bytearray(encode_axi_mm_packet(AxiMmChannel.AR_REQ, 1, 0x40)["raw_bytes"])
    bad_pkt[-1] ^= 0xFF  # Corrupt CRC
    ok_bad = receiver.process_packet(bytes(bad_pkt))
    assert not ok_bad, "Corrupted packet was incorrectly accepted"
    assert receiver.crc_errors == 1


@cocotb.test()
async def test_axi_mm_standards_and_ppa(dut):
    """
    Test 6: AXI4/AXI5 Standards Compliance & Calibrated IHP 130nm SG13G2 PPA Model:
    - Verifies burst types, response codes, and CRC polynomial determinism.
    - Validates PPA scaling metrics for the synthesizable interconnect macro.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Burst Types and Response Codes
    assert AxBurst.FIXED == 0x00
    assert AxBurst.INCR == 0x01
    assert AxBurst.WRAP == 0x02
    assert AxBurst.RESERVED == 0x03

    assert AxResp.OKAY == 0x00
    assert AxResp.EXOKAY == 0x01
    assert AxResp.SLVERR == 0x02
    assert AxResp.DECERR == 0x03

    # 2. CRC-16 Determinism
    test_vec = b"\x01\x01\x40\x10\x12\x34"
    crc1 = compute_axi_mm_crc16(test_vec)
    crc2 = compute_axi_mm_crc16(test_vec)
    assert crc1 == crc2
    assert 0 <= crc1 <= 0xFFFF

    # 3. PPA Model Validation
    metrics = AxiMmPpaModel.get_metrics()
    assert metrics["macro_cells"] == 630
    assert metrics["macro_ge"] == 1235.0
    assert metrics["macro_area_um2"] == 4630.0
    assert metrics["f_max_mhz"] == 800.0
    assert metrics["nominal_power_uw_10mhz"] == 61.50
    assert metrics["raw_throughput_mbps"] == 32000.0
    assert metrics["energy_pj_per_bit"] == 0.00077
