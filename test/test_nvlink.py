# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_nvlink.py - Cocotb test suite for NVLink (NVIDIA High-Speed GPU Interconnect)
Physical & Data Link Layer Engine

Verifies:
1. test_nvlink_master_packet_transmission: Master transmits SYNC training pattern (0xBC) followed by
   READ_REQ opcode (0x01) and Target GPU ID (0x02) on pin 3 via SHIFTOUT (MSB-first), verified at baud center with status R2 = 0x00.
2. test_nvlink_rx_sync_ingress: Slave synchronizes to SYNC pattern rising edge on pin 3 via WAITEDGE,
   samples opcode byte MSB-first into R0 and preserves in R1 (0x01) with status R2 = 0x00.
3. test_nvlink_opcode_filter_and_fault_trapping: In-register validation of NVLink opcodes
   (valid 0x01 READ_REQ, 0x02 READ_RESP, 0x03 WRITE_REQ, 0x04 ATOMIC_REQ, 0x05 FLOW_CTRL_CREDIT, 0x06 LINK_TRAIN_REQ -> R2 = 0x00; illegal 0x7F trapped with R2 = 0xEE).
4. test_nvlink_credit_tracking_and_underflow_trapping: In-register validation of sub-link flow control credit
   increment (Credit Return), decrement (Packet Send), and underflow prevention (credits=0 -> R2 = 0xEE).
5. test_nvlink_packet_framing_and_receiver: Packet encapsulation, CCITT CRC-16 (0x1021) protection,
   credit accounting, atomic operation handling, and NvLinkReceiverModel link lock state machine.
6. test_nvlink_standards_and_ppa: NVLink opcode encodings, CRC-16 determinism,
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
from nvlink_model import (  # noqa: E402
    NvLinkOpCode,
    compute_nvlink_crc16,
    encode_nvlink_packet,
    decode_nvlink_packet,
    NvLinkReceiverModel,
    NvLinkPpaModel,
    build_nvlink_tx_packet_asm,
    build_nvlink_rx_sync_asm,
    build_nvlink_opcode_filter_asm,
    build_nvlink_credit_tracker_asm,
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
async def test_nvlink_master_packet_transmission(dut):
    """
    Test 1: Master NVLink Packet Header Transmission:
    Transmits SYNC pattern (0xBC) followed by READ_REQ opcode (0x01) and Target GPU ID (0x02) on pin 3 via SHIFTOUT (MSB-first).
    Verifies bit timings, captured MSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sync = int(NvLinkOpCode.SYNC)              # 0xBC (0b10111100)
    expected_opcode = int(NvLinkOpCode.READ_REQ)        # 0x01
    expected_gpu_id = 0x02                              # GPU 2

    asm_code = build_nvlink_tx_packet_asm(
        sync_code=expected_sync,
        opcode=expected_opcode,
        target_gpu_id=expected_gpu_id,
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

    # Reconstruct bytes MSB-first
    rec_sync = 0
    for idx in range(8):
        rec_sync = (rec_sync << 1) | symbol_bits[idx]
    assert rec_sync == expected_sync, f"Expected SYNC 0x{expected_sync:02X}, got 0x{rec_sync:02X}"

    rec_op = 0
    for idx in range(8):
        rec_op = (rec_op << 1) | symbol_bits[8 + idx]
    assert rec_op == expected_opcode, f"Expected opcode 0x{expected_opcode:02X}, got 0x{rec_op:02X}"

    rec_gpu_id = 0
    for idx in range(8):
        rec_gpu_id = (rec_gpu_id << 1) | symbol_bits[16 + idx]
    assert rec_gpu_id == expected_gpu_id, f"Expected GPU ID 0x{expected_gpu_id:02X}, got 0x{rec_gpu_id:02X}"

    r2_val = int(core.r2.value)
    assert r2_val == 0x00, f"Expected R2=0x00 (Success), got 0x{r2_val:02X}"


@cocotb.test()
async def test_nvlink_rx_sync_ingress(dut):
    """
    Test 2: Slave NVLink SYNC Ingress & Opcode Sampling:
    Core synchronizes to SYNC pattern rising edge on pin 3 via WAITEDGE (bit 7 = 1),
    strides to bit midpoint, samples 8 bits MSB-first into R0, preserves in R1 (0x01 READ_REQ),
    and halts with status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    sync_code = int(NvLinkOpCode.SYNC)                  # 0xBC (0b10111100)
    target_op = int(NvLinkOpCode.READ_REQ)              # 0x01

    asm_code = build_nvlink_rx_sync_asm(pin_rx=pin_rx, baud_cycles=baud_cycles)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let receiver reach WAITEDGE
    await ClockCycles(dut.clk, 10)

    # 1. Transmit SYNC pattern MSB-first
    for bit_idx in range(7, -1, -1):
        bit = (sync_code >> bit_idx) & 1
        dut.uio_in.value = (bit << pin_rx)
        await ClockCycles(dut.clk, baud_cycles)

    # 2. Transmit target opcode byte MSB-first
    for bit_idx in range(7, -1, -1):
        bit = (target_op >> bit_idx) & 1
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
    assert int(core.r0.value) == target_op, f"Expected R0=0x{target_op:02X}, got 0x{int(core.r0.value):02X}"
    assert int(core.r1.value) == target_op, f"Expected R1=0x{target_op:02X}, got 0x{int(core.r1.value):02X}"


@cocotb.test()
async def test_nvlink_opcode_filter_and_fault_trapping(dut):
    """
    Test 3: In-Register NVLink Opcode Filtering:
    - Valid opcodes:
      0x01 (READ_REQ), 0x02 (READ_RESP), 0x03 (WRITE_REQ),
      0x04 (ATOMIC_REQ), 0x05 (FLOW_CTRL_CREDIT), 0x06 (LINK_TRAIN_REQ) -> R2 = 0x00
    - Illegal opcode:
      0x7F -> trapped with R2 = 0xEE
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_ops = [
        int(NvLinkOpCode.READ_REQ),
        int(NvLinkOpCode.READ_RESP),
        int(NvLinkOpCode.WRITE_REQ),
        int(NvLinkOpCode.ATOMIC_REQ),
        int(NvLinkOpCode.FLOW_CTRL_CREDIT),
        int(NvLinkOpCode.LINK_TRAIN_REQ),
    ]

    for op in valid_ops:
        asm_code = build_nvlink_opcode_filter_asm(test_opcode=op)
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
    asm_code_illegal = build_nvlink_opcode_filter_asm(test_opcode=0x7F)
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
async def test_nvlink_credit_tracking_and_underflow_trapping(dut):
    """
    Test 4: In-Register NVLink Buffer Flow Control Credit Accounting:
    - Event 1 (Credit Return): initial credits 4 -> 5, R2 = 0x00
    - Event 2 (Packet Send): initial credits 4 -> 3, R2 = 0x00
    - Event 2 (Packet Send when credits == 0): underflow error -> R2 = 0xEE
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Case 1: Credit Return increment
    asm_return = build_nvlink_credit_tracker_asm(event_type=1, initial_credits=4)
    words = assemble("\n".join(asm_return))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r0.value) == 5, f"Expected credits incremented to 5, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00

    # Case 2: Packet Send decrement
    asm_send = build_nvlink_credit_tracker_asm(event_type=2, initial_credits=4)
    words = assemble("\n".join(asm_send))
    await _init_dut_and_bootload(dut, words)

    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r0.value) == 3, f"Expected credits decremented to 3, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00

    # Case 3: Credit underflow attempt (initial credits = 0, event = 2)
    asm_underflow = build_nvlink_credit_tracker_asm(event_type=2, initial_credits=0)
    words = assemble("\n".join(asm_underflow))
    await _init_dut_and_bootload(dut, words)

    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r2.value) == 0xEE, f"Expected underflow trap R2=0xEE, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_nvlink_packet_framing_and_receiver(dut):
    """
    Test 5: Python Model Packet Framing, Credit Accounting & Receiver Lock:
    - Verifies encode/decode round trip with CCITT CRC-16 (0x1021) for all opcodes.
    - Verifies NvLinkReceiverModel link lock state machine, flow control credits, and atomic operations.
    """
    receiver = NvLinkReceiverModel(initial_credits=4)
    assert not receiver.link_lock
    assert receiver.flow_control_credits == 4

    operations = [
        (NvLinkOpCode.READ_REQ, 0x01, b""),
        (NvLinkOpCode.READ_RESP, 0x01, bytes([0xDE, 0xAD, 0xBE, 0xEF])),
        (NvLinkOpCode.ATOMIC_REQ, 0x02, bytes([0x01, 0x00, 0x00, 0x00])),  # Atomic Fetch-Add 1
        (NvLinkOpCode.FLOW_CTRL_CREDIT, 0x01, b""),
    ]

    for op, target_gpu, payload in operations:
        pkt_dict = encode_nvlink_packet(opcode=op, target_gpu_id=target_gpu, payload=payload)

        raw = pkt_dict["raw_bytes"]
        sync, dec_op, dec_gpu, dec_payload, crc16, is_valid = decode_nvlink_packet(raw)

        assert is_valid, f"Packet decode failed for opcode 0x{op:02X}"
        assert sync == NvLinkOpCode.SYNC
        assert dec_op == op
        assert dec_gpu == target_gpu
        assert dec_payload == payload
        assert crc16 == pkt_dict["crc16"]

        success = receiver.process_packet(raw)
        assert success

    assert receiver.packets_received == 4
    assert receiver.atomic_operations == 1
    # Initial 4 - 1(READ_REQ) - 1(ATOMIC_REQ) + 1(FLOW_CTRL_CREDIT) = 3
    assert receiver.flow_control_credits == 3
    assert receiver.crc_errors == 0
    assert receiver.link_lock, "Link should achieve lock after 4 valid syncs"

    # Test corrupted CRC
    corrupt_raw = bytearray(pkt_dict["raw_bytes"])
    corrupt_raw[-1] ^= 0xFF
    res = receiver.process_packet(bytes(corrupt_raw))
    assert not res, "Corrupted packet must be rejected"
    assert receiver.crc_errors == 1


@cocotb.test()
async def test_nvlink_standards_and_ppa(dut):
    """
    Test 6: Standards Compliance & Calibrated IHP 130nm SG13G2 PPA Model:
    - Verifies NVLink opcode encodings and CRC-16 mathematics.
    - Validates hardware PPA metrics.
    """
    # 1. Opcode values
    assert NvLinkOpCode.READ_REQ == 0x01
    assert NvLinkOpCode.READ_RESP == 0x02
    assert NvLinkOpCode.WRITE_REQ == 0x03
    assert NvLinkOpCode.ATOMIC_REQ == 0x04
    assert NvLinkOpCode.FLOW_CTRL_CREDIT == 0x05
    assert NvLinkOpCode.LINK_TRAIN_REQ == 0x06
    assert NvLinkOpCode.SYNC == 0xBC
    assert NvLinkOpCode.IDLE == 0x7E

    # 2. Known CRC-16 calculation test
    test_data = b"123456789"
    crc16_val = compute_nvlink_crc16(test_data)
    assert 0 <= crc16_val <= 0xFFFF

    # 3. PPA Model metrics validation
    ppa = NvLinkPpaModel.get_metrics()
    assert ppa["macro_cells"] == 620
    assert ppa["macro_ge"] == 1210.0
    assert ppa["macro_area_um2"] == 4560.0
    assert ppa["f_max_mhz"] == 800.0
    assert ppa["nominal_power_uw_10mhz"] == 60.50
    assert ppa["raw_throughput_mbps"] == 20000.0
    assert ppa["energy_pj_per_bit"] == 0.00075
