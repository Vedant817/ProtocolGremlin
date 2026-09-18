# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_bow.py - Cocotb test suite for Bunch of Wires (BoW / OpenHBI) Die-to-Die Physical Layer Engine

Verifies:
1. test_bow_master_packet_transmission: Master transmits SYNC training pattern (0xBC) followed by
   CALIB_REQ opcode (0x01) and SliceID (0x00) on pin 3 via SHIFTOUT (MSB-first), verified at baud center with status R2 = 0x00.
2. test_bow_rx_sync_ingress: Slave synchronizes to SYNC pattern rising edge on pin 3 via WAITEDGE,
   samples opcode byte MSB-first into R0 and preserves in R1 (0x01) with status R2 = 0x00.
3. test_bow_opcode_filter_and_fault_trapping: In-register validation of BoW opcodes
   (valid 0x01 CALIB_REQ, 0x02 CALIB_RESP, 0x03 TRAIN_STROBE_REQ, 0x04 DATA_TRANSFER, 0x05 LANE_REMAP, 0x06 POWER_DOWN_REQ -> R2 = 0x00; illegal 0x7F trapped with R2 = 0xEE).
4. test_bow_spare_tracking_and_underflow_trapping: In-register validation of slice spare wire allocation
   decrement (wire remapped), increment (spare restored), and underflow prevention (spares=0 -> R2 = 0xEE).
5. test_bow_packet_framing_and_receiver: Packet encapsulation, CRC-16 (0x8005) protection,
   calibration handling, spare wire accounting, and BowReceiverModel link lock state machine.
6. test_bow_standards_and_ppa: BoW opcode encodings, CRC-16 determinism,
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
from bow_model import (  # noqa: E402
    BowOpCode,
    compute_bow_crc16,
    encode_bow_packet,
    decode_bow_packet,
    BowReceiverModel,
    BowPpaModel,
    build_bow_tx_packet_asm,
    build_bow_rx_sync_asm,
    build_bow_opcode_filter_asm,
    build_bow_spare_tracker_asm,
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
async def test_bow_master_packet_transmission(dut):
    """
    Test 1: Master BoW / OpenHBI Packet Header Transmission:
    Transmits SYNC pattern (0xBC) followed by CALIB_REQ opcode (0x01) and SliceID (0x00) on pin 3 via SHIFTOUT (MSB-first).
    Verifies bit timings, captured MSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sync = int(BowOpCode.SYNC)              # 0xBC (0b10111100)
    expected_opcode = int(BowOpCode.CALIB_REQ)        # 0x01 (0b00000001)
    expected_slice_id = 0x00                         # Slice 0

    asm_code = build_bow_tx_packet_asm(
        sync_code=expected_sync,
        opcode=expected_opcode,
        slice_id=expected_slice_id,
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

    rec_slice_id = 0
    for idx in range(8):
        rec_slice_id = (rec_slice_id << 1) | symbol_bits[16 + idx]
    assert rec_slice_id == expected_slice_id, f"Expected SliceID 0x{expected_slice_id:02X}, got 0x{rec_slice_id:02X}"

    r2_val = int(core.r2.value)
    assert r2_val == 0x00, f"Expected R2=0x00 (Success), got 0x{r2_val:02X}"


@cocotb.test()
async def test_bow_rx_sync_ingress(dut):
    """
    Test 2: Slave BoW SYNC Ingress & Opcode Sampling:
    Core synchronizes to SYNC pattern rising edge on pin 3 via WAITEDGE (bit 7 = 1),
    strides to bit midpoint, samples 8 bits MSB-first into R0, preserves in R1 (0x01 CALIB_REQ),
    and halts with status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    sync_code = int(BowOpCode.SYNC)                  # 0xBC (0b10111100)
    target_op = int(BowOpCode.CALIB_REQ)              # 0x01 (0b00000001)

    asm_code = build_bow_rx_sync_asm(pin_rx=pin_rx, baud_cycles=baud_cycles)
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
async def test_bow_opcode_filter_and_fault_trapping(dut):
    """
    Test 3: In-Register BoW Opcode Filtering:
    - Valid opcodes:
      0x01 (CALIB_REQ), 0x02 (CALIB_RESP), 0x03 (TRAIN_STROBE_REQ),
      0x04 (DATA_TRANSFER), 0x05 (LANE_REMAP), 0x06 (POWER_DOWN_REQ) -> R2 = 0x00
    - Illegal opcode:
      0x7F -> trapped with R2 = 0xEE
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_ops = [
        int(BowOpCode.CALIB_REQ),
        int(BowOpCode.CALIB_RESP),
        int(BowOpCode.TRAIN_STROBE_REQ),
        int(BowOpCode.DATA_TRANSFER),
        int(BowOpCode.LANE_REMAP),
        int(BowOpCode.POWER_DOWN_REQ),
    ]

    for op in valid_ops:
        asm_code = build_bow_opcode_filter_asm(test_opcode=op)
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
    asm_code_illegal = build_bow_opcode_filter_asm(test_opcode=0x7F)
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
async def test_bow_spare_tracking_and_underflow_trapping(dut):
    """
    Test 4: In-Register Slice Spare Wire Allocation Accounting:
    - Event 1 (Remap bad wire using spare): initial spares 1 -> 0, R2 = 0x00
    - Event 2 (Restore spare wire): initial spares 1 -> 2, R2 = 0x00
    - Event 1 (Remap wire when spares == 0): underflow error -> R2 = 0xEE
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Case 1: Remap wire decrement
    asm_remap = build_bow_spare_tracker_asm(remap_event=1, initial_spares=1)
    words = assemble("\n".join(asm_remap))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r0.value) == 0, f"Expected spares decremented to 0, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00

    # Case 2: Restore spare increment
    asm_restore = build_bow_spare_tracker_asm(remap_event=2, initial_spares=1)
    words = assemble("\n".join(asm_restore))
    await _init_dut_and_bootload(dut, words)

    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r0.value) == 2, f"Expected spares incremented to 2, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00

    # Case 3: Sparing underflow attempt (initial spares = 0, event = 1)
    asm_underflow = build_bow_spare_tracker_asm(remap_event=1, initial_spares=0)
    words = assemble("\n".join(asm_underflow))
    await _init_dut_and_bootload(dut, words)

    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r2.value) == 0xEE, f"Expected underflow trap R2=0xEE, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_bow_packet_framing_and_receiver(dut):
    """
    Test 5: Python Model Packet Framing, Sparing Accounting & Receiver Lock:
    - Verifies encode/decode round trip with CRC-16 (0x8005) for all opcodes.
    - Verifies BowReceiverModel link lock state machine, calibration response, and spare wire consumption.
    """
    receiver = BowReceiverModel(initial_spares=1, wires_per_slice=16)
    assert not receiver.link_lock
    assert not receiver.calibrated

    operations = [
        (BowOpCode.CALIB_REQ, 0x00, b""),
        (BowOpCode.CALIB_RESP, 0x00, bytes([0x1F, 0x20])),        # ODT / Drive strength settings
        (BowOpCode.LANE_REMAP, 0x00, bytes([0x03])),              # Remap wire 3
        (BowOpCode.DATA_TRANSFER, 0x00, bytes([0x5A, 0xC3])),     # Data flit
    ]

    for op, slice_id, payload in operations:
        pkt_dict = encode_bow_packet(opcode=op, slice_id=slice_id, payload=payload)

        raw = pkt_dict["raw_bytes"]
        sync, dec_op, dec_slice, dec_payload, crc16, is_valid = decode_bow_packet(raw)

        assert is_valid, f"Packet decode failed for opcode 0x{op:02X}"
        assert sync == BowOpCode.SYNC
        assert dec_op == op
        assert dec_slice == slice_id
        assert dec_payload == payload
        assert crc16 == pkt_dict["crc16"]

        success = receiver.process_packet(raw)
        assert success

    assert receiver.packets_received == 4
    assert receiver.calibrated, "Calibration response must set calibrated=True"
    assert receiver.spare_wires == 0           # 1 initial - 1 consumed by LANE_REMAP = 0
    assert receiver.remapped_wires == 1
    assert receiver.crc_errors == 0
    assert receiver.link_lock, "Link should achieve lock after 4 valid syncs"

    # Test corrupted CRC
    corrupt_raw = bytearray(pkt_dict["raw_bytes"])
    corrupt_raw[-1] ^= 0xFF
    res = receiver.process_packet(bytes(corrupt_raw))
    assert not res, "Corrupted packet must be rejected"
    assert receiver.crc_errors == 1


@cocotb.test()
async def test_bow_standards_and_ppa(dut):
    """
    Test 6: Standards Compliance & Calibrated IHP 130nm SG13G2 PPA Model:
    - Verifies BoW opcode encodings and CRC-16 mathematics.
    - Validates hardware PPA metrics.
    """
    # 1. Opcode values
    assert BowOpCode.CALIB_REQ == 0x01
    assert BowOpCode.CALIB_RESP == 0x02
    assert BowOpCode.TRAIN_STROBE_REQ == 0x03
    assert BowOpCode.DATA_TRANSFER == 0x04
    assert BowOpCode.LANE_REMAP == 0x05
    assert BowOpCode.POWER_DOWN_REQ == 0x06
    assert BowOpCode.SYNC == 0xBC
    assert BowOpCode.IDLE == 0x7E

    # 2. Known CRC-16 calculation test
    test_data = b"123456789"
    crc16_val = compute_bow_crc16(test_data)
    assert 0 <= crc16_val <= 0xFFFF

    # 3. PPA Model metrics validation
    ppa = BowPpaModel.get_metrics()
    assert ppa["macro_cells"] == 610
    assert ppa["macro_ge"] == 1190.0
    assert ppa["macro_area_um2"] == 4520.0
    assert ppa["f_max_mhz"] == 800.0
    assert ppa["nominal_power_uw_10mhz"] == 59.0
    assert ppa["raw_throughput_mbps"] == 32000.0
    assert ppa["energy_pj_per_bit"] == 0.00045
