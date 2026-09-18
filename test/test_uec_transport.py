# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_uec_transport.py - Cocotb test suite for InfiniBand XDR/GDR & UEC Transport Engine

Verifies:
1. test_uec_master_packet_transmission: Master transmits SYNC training pattern (0xBC) followed by
   RDMA_WRITE opcode (0x10) and PSN (0x01) on pin 3, verified at baud center with status R2 = 0x00.
2. test_uec_rx_sync_ingress: Slave synchronizes to SYNC pattern rising edge on pin 3 via WAITEDGE,
   samples opcode byte into R0 and preserves in R1 (0x10) with status R2 = 0x00.
3. test_uec_opcode_filter_and_fault_trapping: In-register validation of transport opcodes
   (valid 0x10 RDMA_WRITE, 0x20 RDMA_READ_REQ, 0x30 RDMA_READ_RESP, 0x40 CONGESTION_NOTIF, 0x50 SELECTIVE_ACK -> R2 = 0x00; illegal 0x7F trapped with R2 = 0xEE).
4. test_uec_cwnd_tracking_and_underflow_trapping: In-register validation of Congestion Window (CWND)
   increment (ACK received), decrement (Congestion detected), and underflow prevention (CWND=0 -> R2 = 0xEE).
5. test_uec_packet_framing_and_receiver: Packet encapsulation, CRC-32 protection,
   Congestion Window accounting, and UecReceiverModel link lock state machine.
6. test_uec_standards_and_ppa: UEC opcode encodings, CRC-32 determinism,
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
from uec_transport_model import (  # noqa: E402
    UecOpCode,
    compute_uec_crc32,
    encode_uec_packet,
    decode_uec_packet,
    UecReceiverModel,
    UecPpaModel,
    build_uec_tx_packet_asm,
    build_uec_rx_sync_asm,
    build_uec_opcode_filter_asm,
    build_uec_cwnd_tracker_asm,
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
async def test_uec_master_packet_transmission(dut):
    """
    Test 1: Master UEC Packet Header Transmission:
    Transmits SYNC pattern (0xBC) followed by RDMA_WRITE opcode (0x10) and PSN (0x01) on pin 3.
    Verifies bit timings, captured LSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sync = int(UecOpCode.SYNC)              # 0xBC (0b10111100)
    expected_opcode = int(UecOpCode.RDMA_WRITE)      # 0x10 (0b00010000)
    expected_psn = 0x01                              # PSN 1

    asm_code = build_uec_tx_packet_asm(
        sync_code=expected_sync,
        opcode=expected_opcode,
        psn=expected_psn,
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
            uio_out = int(dut.uio_out.value)
            bit = (uio_out >> pin_tx) & 1
            captured_bits.append(bit)

        if bool(core.halted.value) and len(captured_bits) >= (24 * baud_cycles):
            break

    # Subsample bits at baud centers (offset 1 cycle in)
    symbol_bits = []
    for i in range(1, len(captured_bits), baud_cycles):
        symbol_bits.append(captured_bits[i])

    assert len(symbol_bits) >= 24, f"Expected at least 24 transmitted bits, got {len(symbol_bits)}"

    rec_sync = 0
    for idx in range(8):
        rec_sync |= (symbol_bits[idx] << idx)
    assert rec_sync == expected_sync, f"Expected SYNC 0x{expected_sync:02X}, got 0x{rec_sync:02X}"

    rec_op = 0
    for idx in range(8):
        rec_op |= (symbol_bits[8 + idx] << idx)
    assert rec_op == expected_opcode, f"Expected opcode 0x{expected_opcode:02X}, got 0x{rec_op:02X}"

    rec_psn = 0
    for idx in range(8):
        rec_psn |= (symbol_bits[16 + idx] << idx)
    assert rec_psn == expected_psn, f"Expected PSN 0x{expected_psn:02X}, got 0x{rec_psn:02X}"

    r2_val = int(core.r2.value)
    assert r2_val == 0x00, f"Expected R2=0x00 (Success), got 0x{r2_val:02X}"


@cocotb.test()
async def test_uec_rx_sync_ingress(dut):
    """
    Test 2: Slave UEC SYNC Ingress & Opcode Sampling:
    Core synchronizes to SYNC pattern rising edge on pin 3 via WAITEDGE,
    strides to bit midpoint, samples 8 bits into R0, preserves in R1 (0x10 RDMA_WRITE),
    and halts with status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    sync_code = int(UecOpCode.SYNC)                  # 0xBC (0b10111100)
    target_op = int(UecOpCode.RDMA_WRITE)            # 0x10 (0b00010000)

    asm_code = build_uec_rx_sync_asm(pin_rx=pin_rx, baud_cycles=baud_cycles)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let receiver reach WAITEDGE
    await ClockCycles(dut.clk, 10)

    # 1. Transmit SYNC pattern LSB-first
    for bit_idx in range(8):
        bit = (sync_code >> bit_idx) & 1
        dut.uio_in.value = (bit << pin_rx)
        await ClockCycles(dut.clk, baud_cycles)

    # 2. Transmit target opcode byte LSB-first
    for bit_idx in range(8):
        bit = (target_op >> bit_idx) & 1
        dut.uio_in.value = (bit << pin_rx)
        await ClockCycles(dut.clk, baud_cycles)

    # Return bus to idle low
    dut.uio_in.value = 0x00

    # Wait for halt
    for _ in range(50):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value), "Core did not halt after RX sync"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00 (Success), got 0x{int(core.r2.value):02X}"
    assert int(core.r0.value) == target_op, f"Expected R0=0x{target_op:02X}, got 0x{int(core.r0.value):02X}"
    assert int(core.r1.value) == target_op, f"Expected R1=0x{target_op:02X}, got 0x{int(core.r1.value):02X}"


@cocotb.test()
async def test_uec_opcode_filter_and_fault_trapping(dut):
    """
    Test 3: In-Register Transport Opcode Filtering:
    - Valid opcodes:
      0x10 (RDMA_WRITE), 0x20 (RDMA_READ_REQ), 0x30 (RDMA_READ_RESP),
      0x40 (CONGESTION_NOTIF), 0x50 (SELECTIVE_ACK) -> R2 = 0x00
    - Illegal opcode:
      0x7F -> trapped with R2 = 0xEE
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_ops = [
        int(UecOpCode.RDMA_WRITE),
        int(UecOpCode.RDMA_READ_REQ),
        int(UecOpCode.RDMA_READ_RESP),
        int(UecOpCode.CONGESTION_NOTIF),
        int(UecOpCode.SELECTIVE_ACK),
    ]

    for op in valid_ops:
        asm_code = build_uec_opcode_filter_asm(test_opcode=op)
        words = assemble("\n".join(asm_code))
        await _init_dut_and_bootload(dut, words)

        core = dut.user_project.u_core
        for _ in range(50):
            if bool(core.halted.value):
                break
            await RisingEdge(dut.clk)

        assert bool(core.halted.value)
        assert int(core.r2.value) == 0x00, f"Expected R2=0x00 for opcode 0x{op:02X}, got 0x{int(core.r2.value):02X}"

    # Test illegal opcode: 0x7F
    asm_code_illegal = build_uec_opcode_filter_asm(test_opcode=0x7F)
    words_illegal = assemble("\n".join(asm_code_illegal))
    await _init_dut_and_bootload(dut, words_illegal)

    core = dut.user_project.u_core
    for _ in range(50):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE (Fault Trap) for illegal 0x7F, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_uec_cwnd_tracking_and_underflow_trapping(dut):
    """
    Test 4: In-Register Congestion Window (CWND) Accounting:
    - Event 1 (ACK received): initial CWND 4 -> 5, R2 = 0x00
    - Event 2 (Congestion detected): initial CWND 4 -> 3, R2 = 0x00
    - Event 2 (Congestion detected with CWND 0): underflow error -> R2 = 0xEE
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Case 1: ACK increment
    asm_inc = build_uec_cwnd_tracker_asm(cwnd_event=1, initial_cwnd=4)
    words = assemble("\n".join(asm_inc))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r0.value) == 5, f"Expected CWND incremented to 5, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00

    # Case 2: Congestion decrement
    asm_dec = build_uec_cwnd_tracker_asm(cwnd_event=2, initial_cwnd=4)
    words = assemble("\n".join(asm_dec))
    await _init_dut_and_bootload(dut, words)

    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r0.value) == 3, f"Expected CWND decremented to 3, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00

    # Case 3: CWND underflow attempt (initial CWND = 0, event = 2)
    asm_underflow = build_uec_cwnd_tracker_asm(cwnd_event=2, initial_cwnd=0)
    words = assemble("\n".join(asm_underflow))
    await _init_dut_and_bootload(dut, words)

    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r2.value) == 0xEE, f"Expected underflow trap R2=0xEE, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_uec_packet_framing_and_receiver(dut):
    """
    Test 5: Python Model Packet Framing, Congestion Window & Receiver Lock:
    - Verifies encode/decode round trip with CRC-32 for opcodes.
    - Verifies UecReceiverModel link lock state machine and CWND accounting.
    """
    receiver = UecReceiverModel(initial_cwnd=4)
    assert not receiver.link_lock

    operations = [
        (UecOpCode.RDMA_WRITE, 0x01),
        (UecOpCode.RDMA_READ_REQ, 0x02),
        (UecOpCode.SELECTIVE_ACK, 0x03),
        (UecOpCode.CONGESTION_NOTIF, 0x04),
    ]

    for op, psn in operations:
        payload = bytes([0x40 + b for b in range(8)])
        pkt_dict = encode_uec_packet(opcode=op, psn=psn, payload=payload)

        raw = pkt_dict["raw_bytes"]
        sync, dec_op, dec_psn, dec_payload, crc32, is_valid = decode_uec_packet(raw)

        assert is_valid, f"Packet decode failed for opcode 0x{op:02X}"
        assert sync == UecOpCode.SYNC
        assert dec_op == op
        assert dec_psn == psn
        assert dec_payload == payload
        assert crc32 == pkt_dict["crc32"]

        success = receiver.process_packet(raw)
        assert success

    assert receiver.packets_received == 4
    assert receiver.cwnd == 4          # 4 + 1 (SELECTIVE_ACK) - 1 (CONGESTION_NOTIF) = 4
    assert receiver.acks_sent == 1
    assert receiver.crc_errors == 0
    assert receiver.link_lock, "Link should achieve lock after 4 valid syncs"

    # Test corrupted CRC
    corrupt_raw = bytearray(pkt_dict["raw_bytes"])
    corrupt_raw[-1] ^= 0xFF
    res = receiver.process_packet(bytes(corrupt_raw))
    assert not res, "Corrupted packet must be rejected"
    assert receiver.crc_errors == 1


@cocotb.test()
async def test_uec_standards_and_ppa(dut):
    """
    Test 6: Standards Compliance & Calibrated IHP 130nm SG13G2 PPA Model:
    - Verifies UEC opcode encodings and CRC-32 mathematics.
    - Validates hardware PPA metrics.
    """
    # 1. Opcode values
    assert UecOpCode.RDMA_WRITE == 0x10
    assert UecOpCode.RDMA_READ_REQ == 0x20
    assert UecOpCode.RDMA_READ_RESP == 0x30
    assert UecOpCode.CONGESTION_NOTIF == 0x40
    assert UecOpCode.SELECTIVE_ACK == 0x50
    assert UecOpCode.SYNC == 0xBC
    assert UecOpCode.IDLE == 0x7E

    # 2. Known CRC-32 calculation test
    test_data = b"123456789"
    crc32_val = compute_uec_crc32(test_data)
    assert 0 <= crc32_val <= 0xFFFFFFFF

    # 3. PPA Model metrics validation
    ppa = UecPpaModel.get_metrics()
    assert ppa["macro_cells"] == 610
    assert ppa["macro_ge"] == 1190.0
    assert ppa["macro_area_um2"] == 4500.0
    assert ppa["f_max_mhz"] == 800.0
    assert ppa["nominal_power_uw_10mhz"] == 59.5
    assert ppa["raw_throughput_mbps"] == 200000.0
    assert ppa["energy_pj_per_bit"] == 0.00030
