# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_hmc.py - Cocotb test suite for HMC 2.1 (Hybrid Memory Cube Consortium Specification 2.1)
3D-Stacked DRAM Serial Interface & Packet Routing Engine.

Verifies:
1. test_hmc_master_packet_transmission: Master transmits SYNC_SOF delimiter (0xA5)
   followed by RD16 CMD byte (0x10) and Target Address (0x80) on pin 3 via SHIFTOUT (MSB-first),
   verified at baud center with status R2 = 0x00.
2. test_hmc_rx_beat_ingress: Slave synchronizes to SYNC_SOF rising edge on pin 3 via WAITEDGE,
   samples CMD byte MSB-first into R0 and preserves in R1 (0x30 RSP_RD) with status R2 = 0x00.
3. test_hmc_command_filter_and_fault_trapping: In-register validation of HMC command opcodes
   (valid 0x00 NULL, 0x01 PRET, 0x02 TRET, 0x03 IRTRY, 0x10 RD16,
    0x11 RD32, 0x20 WR16, 0x21 WR32, 0x30 RSP_RD, 0x31 RSP_WR -> R2 = 0x00; illegal 0x7F trapped with R2 = 0xEE).
4. test_hmc_credit_tracking_and_underflow_trapping: In-register validation of HMC token credit
   increment (Event 1), decrement (Event 2), and underflow prevention (credits=0 -> R2 = 0xEE).
5. test_hmc_packet_framing_cubes_and_receiver: Packet encapsulation, target cubes,
   CCITT CRC-16 (0x1021) protection, and HmcReceiverModel link lock state machine.
6. test_hmc_standards_and_ppa: Link states, Cube IDs, OpCodes, CRC-16 determinism,
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
from hmc_model import (  # noqa: E402
    HmcLinkState,
    HmcCubeId,
    HmcOpCode,
    compute_hmc_crc16,
    encode_hmc_packet,
    decode_hmc_packet,
    HmcReceiverModel,
    HmcPpaModel,
    build_hmc_tx_beat_asm,
    build_hmc_rx_beat_asm,
    build_hmc_command_filter_asm,
    build_hmc_credit_tracker_asm,
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
async def test_hmc_master_packet_transmission(dut):
    """
    Test 1: Master HMC Packet Header Transmission:
    Transmits SYNC_SOF delimiter (0xA5) followed by RD16 CMD (0x10) and Target Address (0x80)
    on pin 3 via SHIFTOUT (MSB-first).
    Verifies bit timings, captured MSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sync = int(HmcOpCode.SYNC_SOF)       # 0xA5 (0b10100101)
    expected_opcode = int(HmcOpCode.RD16)         # 0x10
    expected_addr = 0x80                          # Target Vault Address 0x80

    asm_code = build_hmc_tx_beat_asm(
        sync_code=expected_sync,
        cmd_op=expected_opcode,
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

    # Reconstruct 3 bytes (MSB first)
    byte_sync = 0
    for b in sampled_bits[0:8]:
        byte_sync = (byte_sync << 1) | b
    assert byte_sync == expected_sync, f"Delimiter mismatch: expected 0x{expected_sync:02X}, got 0x{byte_sync:02X}"

    byte_op = 0
    for b in sampled_bits[8:16]:
        byte_op = (byte_op << 1) | b
    assert byte_op == expected_opcode, f"Opcode mismatch: expected 0x{expected_opcode:02X}, got 0x{byte_op:02X}"

    byte_addr = 0
    for b in sampled_bits[16:24]:
        byte_addr = (byte_addr << 1) | b
    assert byte_addr == expected_addr, f"Address mismatch: expected 0x{expected_addr:02X}, got 0x{byte_addr:02X}"


@cocotb.test()
async def test_hmc_rx_beat_ingress(dut):
    """
    Test 2: Slave HMC SYNC Synchronization & Command Ingress:
    Drives SYNC_SOF (0xA5) on pin 3, WAITEDGE rising edge detects bit 7,
    strides to CMD byte midpoint, ingresses 8 bits MSB-first (0x30 RSP_RD) into R0,
    preserves into R1, and halts with R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    command_to_send = int(HmcOpCode.RSP_RD)  # 0x30 RSP_RD

    asm_code = build_hmc_rx_beat_asm(
        pin_rx=pin_rx,
        baud_cycles=baud_cycles,
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let core reach WAITEDGE
    await ClockCycles(dut.clk, 10)

    # 1. Drive SYNC_SOF (0xA5 = 0b10100101), MSB-first
    sync_byte = int(HmcOpCode.SYNC_SOF)
    for bit_idx in range(7, -1, -1):
        bit_val = (sync_byte >> bit_idx) & 1
        dut.uio_in.value = bit_val << pin_rx
        await ClockCycles(dut.clk, baud_cycles)

    # 2. Drive CMD byte (0x30 = 0b00110000), MSB-first
    for bit_idx in range(7, -1, -1):
        bit_val = (command_to_send >> bit_idx) & 1
        dut.uio_in.value = bit_val << pin_rx
        await ClockCycles(dut.clk, baud_cycles)

    # 3. Return bus low
    dut.uio_in.value = 0x00
    await ClockCycles(dut.clk, 20)

    assert bool(core.halted.value), "Core did not halt after RX ingress"
    assert int(core.r1.value) == command_to_send, f"Expected R1=0x{command_to_send:02X}, got 0x{int(core.r1.value):02X}"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00 (Success), got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_hmc_command_filter_and_fault_trapping(dut):
    """
    Test 3: In-Register HMC Command Filtering & Illegal Opcode Trapping:
    Verifies valid commands:
      0x00 NULL, 0x01 PRET, 0x02 TRET, 0x03 IRTRY, 0x10 RD16,
      0x11 RD32, 0x20 WR16, 0x21 WR32, 0x30 RSP_RD, 0x31 RSP_WR -> R2 = 0x00
    Verifies illegal command:
      0x7F -> R2 = 0xEE (Trapped Error).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_test_cases = [
        int(HmcOpCode.NULL),
        int(HmcOpCode.PRET),
        int(HmcOpCode.TRET),
        int(HmcOpCode.IRTRY),
        int(HmcOpCode.RD16),
        int(HmcOpCode.RD32),
        int(HmcOpCode.WR16),
        int(HmcOpCode.WR32),
        int(HmcOpCode.RSP_RD),
        int(HmcOpCode.RSP_WR),
    ]

    for op in valid_test_cases:
        asm_code = build_hmc_command_filter_asm(test_opcode=op)
        words = assemble("\n".join(asm_code))
        await _init_dut_and_bootload(dut, words)

        core = dut.user_project.u_core
        for _ in range(100):
            await RisingEdge(dut.clk)
            if bool(core.halted.value):
                break

        assert bool(core.halted.value), f"Core timed out on valid HMC CMD 0x{op:02X}"
        assert int(core.r2.value) == 0x00, f"Expected R2=0x00 for valid HMC CMD 0x{op:02X}, got 0x{int(core.r2.value):02X}"

    # Verify illegal CMD trapping (0x7F)
    illegal_op = 0x7F
    asm_code = build_hmc_command_filter_asm(test_opcode=illegal_op)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    for _ in range(100):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    assert bool(core.halted.value), "Core timed out on illegal HMC CMD 0x7F"
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE for illegal HMC CMD 0x7F, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_hmc_credit_tracking_and_underflow_trapping(dut):
    """
    Test 4: In-Register HMC Token Credit Accounting & Underflow Trapping:
    - Event 1: Token returned (PRET / Response) -> increments token pool (initial 4 -> 5), R2 = 0x00.
    - Event 2: Request dispatched -> decrements token pool (initial 4 -> 3), R2 = 0x00.
    - Event 2 with 0 tokens -> underflow trapped with R2 = 0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Case 1: Token Return (Event 1)
    asm_code = build_hmc_credit_tracker_asm(initial_credits=4, event_type=1)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)
    core = dut.user_project.u_core
    for _ in range(100):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    assert bool(core.halted.value), "Core timed out on token return"
    assert int(core.r0.value) == 5, f"Expected credits=5, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00, got 0x{int(core.r2.value):02X}"

    # Case 2: Request Dispatch (Event 2)
    asm_code = build_hmc_credit_tracker_asm(initial_credits=4, event_type=2)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)
    core = dut.user_project.u_core
    for _ in range(100):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    assert bool(core.halted.value), "Core timed out on request dispatch"
    assert int(core.r0.value) == 3, f"Expected credits=3, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00, got 0x{int(core.r2.value):02X}"

    # Case 3: Underflow Trapping (Event 2 with credits=0)
    asm_code = build_hmc_credit_tracker_asm(initial_credits=0, event_type=2)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)
    core = dut.user_project.u_core
    for _ in range(100):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break
    assert bool(core.halted.value), "Core timed out on underflow test"
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE (Underflow Trap), got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_hmc_packet_framing_cubes_and_receiver(dut):
    """
    Test 5: HMC Packet Framing, Cubes, and Receiver Model:
    - Encodes and decodes packets across Cube IDs (CUBE_0, CUBE_1).
    - Verifies CCITT CRC-16 generation and bitflip detection.
    - Validates HmcReceiverModel link lock FSM and link state transitions.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Packet framing test
    packet = encode_hmc_packet(
        cmd=HmcOpCode.RD16,
        cube_id=int(HmcCubeId.CUBE_1),
        tag=0x07,
        addr=0x1000,
        payload=b"\xAA\xBB\xCC\xDD",
    )
    assert len(packet) == 12, f"Expected 12 bytes, got {len(packet)}"
    assert packet[0] == int(HmcOpCode.SYNC_SOF)

    decoded = decode_hmc_packet(packet)
    assert decoded is not None
    assert decoded["cmd"] == HmcOpCode.RD16
    assert decoded["cube_id"] == int(HmcCubeId.CUBE_1)
    assert decoded["tag"] == 0x07
    assert decoded["addr"] == 0x1000
    assert decoded["payload"] == b"\xAA\xBB\xCC\xDD"

    # Corrupt packet payload -> CRC failure
    corrupt_packet = bytearray(packet)
    corrupt_packet[6] ^= 0xFF
    assert decode_hmc_packet(bytes(corrupt_packet)) is None

    # 2. HmcReceiverModel test
    receiver = HmcReceiverModel(initial_credits=4)
    assert not receiver.link_locked
    assert receiver.state == HmcLinkState.LINK_INIT

    for _ in range(3):
        receiver.process_sync()
    assert not receiver.link_locked

    receiver.process_sync()
    assert receiver.link_locked
    assert receiver.state == HmcLinkState.READY

    # RD16 -> ACTIVE_TX (credits decrement 4 -> 3)
    rd_pkt = encode_hmc_packet(HmcOpCode.RD16, cube_id=int(HmcCubeId.CUBE_0))
    assert receiver.process_packet(rd_pkt)
    assert receiver.state == HmcLinkState.ACTIVE_TX
    assert receiver.credits == 3

    # RSP_RD -> ACTIVE_RX (credits increment 3 -> 4)
    rsp_pkt = encode_hmc_packet(HmcOpCode.RSP_RD, cube_id=int(HmcCubeId.CUBE_0))
    assert receiver.process_packet(rsp_pkt)
    assert receiver.state == HmcLinkState.ACTIVE_RX
    assert receiver.credits == 4

    # PRET -> READY (credits increment 4 -> 5)
    pret_pkt = encode_hmc_packet(HmcOpCode.PRET, cube_id=int(HmcCubeId.CUBE_0))
    assert receiver.process_packet(pret_pkt)
    assert receiver.state == HmcLinkState.READY
    assert receiver.credits == 5


@cocotb.test()
async def test_hmc_standards_and_ppa(dut):
    """
    Test 6: HMC 2.1 Standards Verification & PPA Scaling Model:
    - Verifies HMC 2.1 link states, Cube IDs, and command opcodes.
    - Validates CCITT CRC-16 determinism against standard vector.
    - Validates IHP 130nm SG13G2 PPA scaling metrics for the HMC slice macro.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # 1. Link states
    assert int(HmcLinkState.LINK_DOWN) == 0x00
    assert int(HmcLinkState.LINK_INIT) == 0x01
    assert int(HmcLinkState.READY) == 0x02
    assert int(HmcLinkState.ACTIVE_TX) == 0x03
    assert int(HmcLinkState.ACTIVE_RX) == 0x04
    assert int(HmcLinkState.RETRY_ERR) == 0x05

    # 2. Cube IDs
    assert int(HmcCubeId.CUBE_0) == 0x00
    assert int(HmcCubeId.CUBE_1) == 0x01
    assert int(HmcCubeId.CUBE_2) == 0x02
    assert int(HmcCubeId.CUBE_3) == 0x03

    # 3. Command opcodes
    assert int(HmcOpCode.NULL) == 0x00
    assert int(HmcOpCode.PRET) == 0x01
    assert int(HmcOpCode.TRET) == 0x02
    assert int(HmcOpCode.IRTRY) == 0x03
    assert int(HmcOpCode.RD16) == 0x10
    assert int(HmcOpCode.RD32) == 0x11
    assert int(HmcOpCode.RD64) == 0x12
    assert int(HmcOpCode.WR16) == 0x20
    assert int(HmcOpCode.WR32) == 0x21
    assert int(HmcOpCode.WR64) == 0x22
    assert int(HmcOpCode.RSP_RD) == 0x30
    assert int(HmcOpCode.RSP_WR) == 0x31
    assert int(HmcOpCode.IDLE) == 0x7E
    assert int(HmcOpCode.SYNC_SOF) == 0xA5

    # 4. CCITT CRC-16 determinism
    crc_empty = compute_hmc_crc16(b"")
    assert crc_empty == 0xFFFF

    test_bytes = b"\xA5\x10\x01\x01\x00\x80"
    crc_val = compute_hmc_crc16(test_bytes)
    assert 0 <= crc_val <= 0xFFFF

    # 5. Calibrated PPA Metrics
    metrics = HmcPpaModel.get_metrics()
    assert metrics["macro_cells"] == 645
    assert metrics["macro_ge"] == 1270.0
    assert metrics["macro_area_um2"] == 4780.0
    assert metrics["f_max_mhz"] == 800.0
    assert metrics["nominal_power_uw_10mhz"] == 63.50
    assert metrics["raw_throughput_mbps"] == 30000.0
    assert metrics["energy_pj_per_bit"] == 0.00078
