# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_amba_chi.py - Cocotb test suite for ARM AMBA CHI (Coherent Hub Interface)
and ACE (AXI Coherency Extensions) Cache-Coherent Interconnect Engine.

Verifies:
1. test_amba_chi_master_packet_transmission: Master transmits SYNC_SOF delimiter (0xA5)
   followed by READ_SHARED opcode byte (0x01) and Target Address (0x40) on pin 3 via SHIFTOUT (MSB-first),
   verified at baud center with status R2 = 0x00.
2. test_amba_chi_rx_beat_ingress: Slave synchronizes to SYNC_SOF rising edge on pin 3 via WAITEDGE,
   samples opcode byte MSB-first into R0 and preserves in R1 (0x01 READ_SHARED) with status R2 = 0x00.
3. test_amba_chi_opcode_filter_and_fault_trapping: In-register validation of AMBA CHI opcodes
   (valid 0x01 READ_SHARED, 0x02 READ_CLEAN, 0x03 READ_ONCE, 0x04 CLEAN_UNIQUE, 0x05 MAKE_UNIQUE,
    0x06 WRITE_BACK_PTL, 0x07 SNOOP_RESP, 0x08 COMP_ACK -> R2 = 0x00; illegal 0x7F trapped with R2 = 0xEE).
4. test_amba_chi_credit_tracking_and_underflow_trapping: In-register validation of coherent transaction
   credit increment (CompAck returned), decrement (request issued), and underflow prevention (credits=0 -> R2 = 0xEE).
5. test_amba_chi_packet_framing_moesi_and_receiver: Packet flit encapsulation, MOESI state machine transitions,
   CCITT CRC-16 (0x1021) protection, and AmbaChiReceiverModel directory tracking and link lock acquisition.
6. test_amba_chi_standards_and_ppa: MOESI states, flit channels, opcodes, CRC-16 determinism,
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
from amba_chi_model import (  # noqa: E402
    MoesiState,
    AmbaChiChannel,
    AmbaChiOpCode,
    transition_moesi,
    compute_amba_chi_crc16,
    encode_amba_chi_flit,
    decode_amba_chi_flit,
    AmbaChiReceiverModel,
    AmbaChiPpaModel,
    build_amba_chi_tx_beat_asm,
    build_amba_chi_rx_beat_asm,
    build_amba_chi_opcode_filter_asm,
    build_amba_chi_credit_tracker_asm,
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
async def test_amba_chi_master_packet_transmission(dut):
    """
    Test 1: Master AMBA CHI Flit Header Transmission:
    Transmits SYNC_SOF delimiter (0xA5) followed by READ_SHARED opcode (0x01) and Target Address (0x40)
    on pin 3 via SHIFTOUT (MSB-first).
    Verifies bit timings, captured MSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sync = int(AmbaChiOpCode.SYNC_SOF)       # 0xA5 (0b10100101)
    expected_opcode = int(AmbaChiOpCode.READ_SHARED)  # 0x01
    expected_addr = 0x40                              # Target Cache Line Address 0x40

    asm_code = build_amba_chi_tx_beat_asm(
        sync_code=expected_sync,
        opcode_val=expected_opcode,
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
    assert byte1 == expected_opcode, f"Byte 1 (READ_SHARED): expected 0x{expected_opcode:02X}, got 0x{byte1:02X}"
    assert byte2 == expected_addr, f"Byte 2 (Target Address): expected 0x{expected_addr:02X}, got 0x{byte2:02X}"


@cocotb.test()
async def test_amba_chi_rx_beat_ingress(dut):
    """
    Test 2: Slave AMBA CHI SYNC_SOF Ingress & OpCode Sampling:
    Core synchronizes to SYNC_SOF delimiter rising edge on pin 3 via WAITEDGE (bit 7 = 1),
    strides to bit midpoint, samples 8 bits MSB-first into R0, preserves in R1 (0x01 READ_SHARED),
    and halts with status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    sync_delimiter = int(AmbaChiOpCode.SYNC_SOF)       # 0xA5 = 0b10100101
    test_opcode = int(AmbaChiOpCode.READ_SHARED)      # 0x01 = 0b00000001

    asm_code = build_amba_chi_rx_beat_asm(
        pin_rx=pin_rx,
        baud_cycles=baud_cycles,
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let core start and enter WAITEDGE
    await ClockCycles(dut.clk, 10)

    # Deliver SYNC_SOF (0xA5) MSB-first: bit 7 is 1, causing rising edge
    for bit_i in range(8):
        bit_val = (sync_delimiter >> (7 - bit_i)) & 1
        dut.uio_in.value = bit_val << pin_rx
        await ClockCycles(dut.clk, baud_cycles)

    # Deliver test_opcode (0x01) MSB-first
    for bit_i in range(8):
        bit_val = (test_opcode >> (7 - bit_i)) & 1
        dut.uio_in.value = bit_val << pin_rx
        await ClockCycles(dut.clk, baud_cycles)

    dut.uio_in.value = 0x00

    # Wait for completion
    for _ in range(200):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt after RX beat ingress"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00 (Success), got 0x{int(core.r2.value):02X}"
    assert int(core.r1.value) == test_opcode, f"Expected R1=0x{test_opcode:02X} (READ_SHARED), got 0x{int(core.r1.value):02X}"


@cocotb.test()
async def test_amba_chi_opcode_filter_and_fault_trapping(dut):
    """
    Test 3: In-Register AMBA CHI OpCode Filter & Fault Trapping:
    Verifies valid opcodes:
      0x01 (READ_SHARED)    -> R2 = 0x00
      0x02 (READ_CLEAN)     -> R2 = 0x00
      0x03 (READ_ONCE)      -> R2 = 0x00
      0x04 (CLEAN_UNIQUE)   -> R2 = 0x00
      0x05 (MAKE_UNIQUE)    -> R2 = 0x00
      0x06 (WRITE_BACK_PTL) -> R2 = 0x00
      0x07 (SNOOP_RESP)     -> R2 = 0x00
      0x08 (COMP_ACK)       -> R2 = 0x00
    Verifies illegal opcode:
      0x7F -> trapped with R2 = 0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_test_cases = [
        (int(AmbaChiOpCode.READ_SHARED), "READ_SHARED"),
        (int(AmbaChiOpCode.READ_CLEAN), "READ_CLEAN"),
        (int(AmbaChiOpCode.READ_ONCE), "READ_ONCE"),
        (int(AmbaChiOpCode.CLEAN_UNIQUE), "CLEAN_UNIQUE"),
        (int(AmbaChiOpCode.MAKE_UNIQUE), "MAKE_UNIQUE"),
        (int(AmbaChiOpCode.WRITE_BACK_PTL), "WRITE_BACK_PTL"),
        (int(AmbaChiOpCode.SNOOP_RESP), "SNOOP_RESP"),
        (int(AmbaChiOpCode.COMP_ACK), "COMP_ACK"),
    ]

    for op_val, name in valid_test_cases:
        asm_code = build_amba_chi_opcode_filter_asm(op_val)
        words = assemble("\n".join(asm_code))
        await _init_dut_and_bootload(dut, words)

        core = dut.user_project.u_core
        for _ in range(200):
            await RisingEdge(dut.clk)
            if bool(core.halted.value):
                break

        assert bool(core.halted.value), f"Core did not halt for valid opcode {name}"
        assert int(core.r2.value) == 0x00, f"Expected R2=0x00 for {name}, got 0x{int(core.r2.value):02X}"

    # Test illegal opcode: 0x7F
    illegal_op = 0x7F
    asm_code = build_amba_chi_opcode_filter_asm(illegal_op)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    for _ in range(200):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt for illegal opcode"
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE (Fault Trap), got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_amba_chi_credit_tracking_and_underflow_trapping(dut):
    """
    Test 4: In-Register Coherent Transaction Credit Tracking & Underflow Trapping:
    - Event 1: CompAck returned -> credit increment (4 -> 5), R2 = 0x00.
    - Event 2: Request issued with credits > 0 -> credit decrement (4 -> 3), R2 = 0x00.
    - Event 2: Request issued with credits == 0 -> underflow trap (R2 = 0xEE).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Case A: Event 1 (Credit Return / CompAck): 4 + 1 = 5
    asm_code_return = build_amba_chi_credit_tracker_asm(event_type=1, initial_credits=4)
    words_return = assemble("\n".join(asm_code_return))
    await _init_dut_and_bootload(dut, words_return)

    core = dut.user_project.u_core
    for _ in range(200):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt on credit return"
    assert int(core.r0.value) == 5, f"Expected R0=5 after credit return, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00, got 0x{int(core.r2.value):02X}"

    # Case B: Event 2 (Credit Consume / Request): 4 - 1 = 3
    asm_code_send = build_amba_chi_credit_tracker_asm(event_type=2, initial_credits=4)
    words_send = assemble("\n".join(asm_code_send))
    await _init_dut_and_bootload(dut, words_send)

    for _ in range(200):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt on request issue"
    assert int(core.r0.value) == 3, f"Expected R0=3 after request issue, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00, got 0x{int(core.r2.value):02X}"

    # Case C: Event 2 with 0 credits -> Underflow Trap (R2 = 0xEE)
    asm_code_underflow = build_amba_chi_credit_tracker_asm(event_type=2, initial_credits=0)
    words_underflow = assemble("\n".join(asm_code_underflow))
    await _init_dut_and_bootload(dut, words_underflow)

    for _ in range(200):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core did not halt on underflow condition"
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE (Underflow Trap), got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_amba_chi_packet_framing_moesi_and_receiver(dut):
    """
    Test 5: AMBA CHI / ACE Framing, MOESI State Transitions & Receiver Model:
    - Tests MOESI transitions:
      - Invalid + ReadShared -> Shared Clean
      - Invalid + ReadClean -> Unique Clean
      - Shared Clean + CleanUnique -> Unique Clean
      - Unique Clean + MakeUnique -> Unique Dirty
      - Unique Dirty + WriteBackPtl -> Invalid
    - Tests flit encapsulation, CRC-16 generation, and decoding.
    - Tests CRC corrupt flit detection.
    - Tests AmbaChiReceiverModel state transitions and link lock acquisition after 4 flits.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # MOESI Transition Tests
    s1, _ = transition_moesi(MoesiState.INVALID, AmbaChiOpCode.READ_SHARED)
    assert s1 == MoesiState.SHARED_CLEAN, f"Expected SHARED_CLEAN, got {s1}"

    s2, _ = transition_moesi(MoesiState.INVALID, AmbaChiOpCode.READ_CLEAN)
    assert s2 == MoesiState.UNIQUE_CLEAN, f"Expected UNIQUE_CLEAN, got {s2}"

    s3, _ = transition_moesi(MoesiState.SHARED_CLEAN, AmbaChiOpCode.CLEAN_UNIQUE)
    assert s3 == MoesiState.UNIQUE_CLEAN, f"Expected UNIQUE_CLEAN, got {s3}"

    s4, _ = transition_moesi(MoesiState.UNIQUE_CLEAN, AmbaChiOpCode.MAKE_UNIQUE)
    assert s4 == MoesiState.UNIQUE_DIRTY, f"Expected UNIQUE_DIRTY, got {s4}"

    s5, _ = transition_moesi(MoesiState.UNIQUE_DIRTY, AmbaChiOpCode.WRITE_BACK_PTL)
    assert s5 == MoesiState.INVALID, f"Expected INVALID, got {s5}"

    # Flit Encoding & Decoding Tests
    flit = encode_amba_chi_flit(
        opcode=AmbaChiOpCode.READ_SHARED,
        node_id=2,
        txn_id=5,
        addr=0x40,
        moesi_state=MoesiState.SHARED_CLEAN,
        resp_code=0x01,
        payload=b"\x12\x34",
    )
    raw_bytes = flit["raw_bytes"]
    opcode, node_id, txn_id, addr, moesi_state, resp_code, payload, crc16, is_valid = decode_amba_chi_flit(raw_bytes)

    assert is_valid is True, "Valid flit failed decoding"
    assert opcode == AmbaChiOpCode.READ_SHARED, f"Expected READ_SHARED, got {opcode}"
    assert node_id == 2, f"Expected node_id=2, got {node_id}"
    assert txn_id == 5, f"Expected txn_id=5, got {txn_id}"
    assert addr == 0x40, f"Expected addr=0x40, got 0x{addr:02X}"
    assert moesi_state == MoesiState.SHARED_CLEAN, f"Expected SHARED_CLEAN, got {moesi_state}"
    assert resp_code == 0x01, f"Expected resp_code=0x01, got {resp_code}"
    assert payload == b"\x12\x34", f"Expected b'\\x12\\x34', got {payload}"

    # Corrupt flit test
    corrupted_bytes = bytearray(raw_bytes)
    corrupted_bytes[-1] ^= 0xFF
    _, _, _, _, _, _, _, _, is_valid_corrupt = decode_amba_chi_flit(bytes(corrupted_bytes))
    assert is_valid_corrupt is False, "Corrupted flit incorrectly passed decoding"

    # Receiver Model Link Lock Test
    receiver = AmbaChiReceiverModel(initial_credits=4)
    assert receiver.link_lock is False, "Initial link lock should be False"

    # Feed 4 consecutive valid flits
    for i in range(4):
        test_flit = encode_amba_chi_flit(
            opcode=AmbaChiOpCode.READ_SHARED,
            node_id=1,
            txn_id=i,
            addr=0x40 + i * 4,
            moesi_state=MoesiState.SHARED_CLEAN,
        )
        success = receiver.process_flit(test_flit["raw_bytes"])
        assert success is True, f"Failed processing flit {i}"

    assert receiver.link_lock is True, "Receiver should achieve link lock after 4 flits"
    assert receiver.flits_received == 4, f"Expected 4 flits received, got {receiver.flits_received}"
    assert receiver.crc_errors == 0, "Expected 0 CRC errors"

    # Feed CompAck to return credit
    ack_flit = encode_amba_chi_flit(
        opcode=AmbaChiOpCode.COMP_ACK,
        node_id=1,
        txn_id=0,
        addr=0x40,
        moesi_state=MoesiState.SHARED_CLEAN,
    )
    receiver.process_flit(ack_flit["raw_bytes"])
    assert receiver.comp_ack_count == 1, f"Expected 1 CompAck, got {receiver.comp_ack_count}"
    assert receiver.outstanding_credits == 1, f"Expected 1 credit remaining, got {receiver.outstanding_credits}"


@cocotb.test()
async def test_amba_chi_standards_and_ppa(dut):
    """
    Test 6: Standards Compliance and PPA Model Validation:
    - Verifies MOESI state values and CHI channels.
    - Verifies CHI transaction opcodes.
    - Verifies CCITT CRC-16 polynomial determinism.
    - Verifies IHP 130nm SG13G2 calibrated PPA model metrics.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # MOESI states
    assert MoesiState.INVALID == 0x00
    assert MoesiState.UNIQUE_CLEAN == 0x01
    assert MoesiState.UNIQUE_DIRTY == 0x02
    assert MoesiState.SHARED_CLEAN == 0x03
    assert MoesiState.SHARED_DIRTY == 0x04

    # Flit Channels
    assert AmbaChiChannel.REQ == 0x00
    assert AmbaChiChannel.RSP == 0x01
    assert AmbaChiChannel.DAT == 0x02
    assert AmbaChiChannel.SNP == 0x03

    # OpCodes
    assert AmbaChiOpCode.READ_SHARED == 0x01
    assert AmbaChiOpCode.READ_CLEAN == 0x02
    assert AmbaChiOpCode.READ_ONCE == 0x03
    assert AmbaChiOpCode.CLEAN_UNIQUE == 0x04
    assert AmbaChiOpCode.MAKE_UNIQUE == 0x05
    assert AmbaChiOpCode.WRITE_BACK_PTL == 0x06
    assert AmbaChiOpCode.SNOOP_RESP == 0x07
    assert AmbaChiOpCode.COMP_ACK == 0x08
    assert AmbaChiOpCode.IDLE == 0x7E
    assert AmbaChiOpCode.SYNC_SOF == 0xA5

    # CRC-16 determinism test
    test_data = b"ARM AMBA CHI and ACE Cache-Coherent Interconnect"
    crc1 = compute_amba_chi_crc16(test_data)
    crc2 = compute_amba_chi_crc16(test_data)
    assert crc1 == crc2, "CRC-16 non-deterministic"
    assert isinstance(crc1, int) and 0 <= crc1 <= 0xFFFF, f"CRC-16 out of range: {crc1}"

    # PPA Model validation
    ppa = AmbaChiPpaModel.get_metrics()
    assert ppa["macro_cells"] == 645, f"PPA macro cells mismatch: {ppa['macro_cells']}"
    assert ppa["macro_ge"] == 1265.0, f"PPA GE mismatch: {ppa['macro_ge']}"
    assert ppa["macro_area_um2"] == 4745.0, f"PPA area mismatch: {ppa['macro_area_um2']}"
    assert ppa["f_max_mhz"] == 800.0, f"PPA f_max mismatch: {ppa['f_max_mhz']}"
    assert ppa["nominal_power_uw_10mhz"] == 63.00, f"PPA power mismatch: {ppa['nominal_power_uw_10mhz']}"
    assert ppa["raw_throughput_mbps"] == 32000.0, f"PPA throughput mismatch: {ppa['raw_throughput_mbps']}"
    assert ppa["energy_pj_per_bit"] == 0.00098, f"PPA energy mismatch: {ppa['energy_pj_per_bit']}"
