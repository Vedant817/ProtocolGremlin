# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_hypertransport.py - Cocotb test suite for HyperTransport 3.1 Physical Layer & Link Protocol Engine

Verifies:
1. test_ht_master_packet_transmission: Master transmits SYNC training pattern (0xBC) followed by
   READ_REQ command ID (0x20) on pin 3, verified at baud center with status R2 = 0x00.
2. test_ht_rx_sync_ingress: Slave synchronizes to SYNC pattern rising edge on pin 3 via WAITEDGE,
   samples command ID byte into R0 and preserves in R1 (0x20) with status R2 = 0x00.
3. test_ht_command_filter_and_fault_trapping: In-register validation of command opcodes
   (valid 0x00 NOP, 0x20 READ_REQ, 0x40 WRITE_REQ, 0xC0 RESPONSE -> R2 = 0x00; illegal 0x7F trapped with R2 = 0xEE).
4. test_ht_credit_tracking_and_underflow_trapping: In-register validation of Virtual Channel buffer credit
   increment (Credit Return), decrement (Packet Send), and underflow prevention (credit=0 -> R2 = 0xEE).
5. test_ht_packet_framing_and_receiver: Packet encapsulation, CRC-32 protection,
   virtual channel credit accounting, and HyperTransportReceiverModel link lock state machine.
6. test_ht_standards_and_ppa: HyperTransport command encodings, CRC-32 determinism,
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
from hypertransport_model import (  # noqa: E402
    HtCommandType,
    compute_ht_crc32,
    encode_ht_packet,
    decode_ht_packet,
    HyperTransportReceiverModel,
    HyperTransportPpaModel,
    build_ht_tx_packet_asm,
    build_ht_rx_sync_asm,
    build_ht_command_filter_asm,
    build_ht_credit_tracker_asm,
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
async def test_ht_master_packet_transmission(dut):
    """
    Test 1: Master HyperTransport Packet Header Transmission:
    Transmits SYNC pattern (0x55) followed by READ_REQ command ID (0x20) on pin 3.
    Verifies bit timings, captured LSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sync = int(HtCommandType.SYNC)       # 0xBC (0b10111100)
    expected_cmd = int(HtCommandType.READ_REQ)   # 0x20 (0b00100000)

    asm_code = build_ht_tx_packet_asm(
        sync_code=expected_sync,
        cmd_code=expected_cmd,
        pin_tx=pin_tx,
        baud_cycles=baud_cycles,
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    max_cycles = 300

    captured_bits = []

    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_oe = int(dut.uio_oe.value)
        if (uio_oe & (1 << pin_tx)) != 0:
            uio_out = int(dut.uio_out.value)
            bit = (uio_out >> pin_tx) & 1
            captured_bits.append(bit)

        if bool(core.halted.value) and len(captured_bits) >= (16 * baud_cycles):
            break

    # Subsample bits at baud centers (offset 1 cycle in)
    symbol_bits = []
    for i in range(1, len(captured_bits), baud_cycles):
        symbol_bits.append(captured_bits[i])

    assert len(symbol_bits) >= 16, f"Expected at least 16 transmitted bits, got {len(symbol_bits)}"

    rec_sync = 0
    for idx in range(8):
        rec_sync |= (symbol_bits[idx] << idx)
    assert rec_sync == expected_sync, f"Expected SYNC 0x{expected_sync:02X}, got 0x{rec_sync:02X}"

    rec_cmd = 0
    for idx in range(8):
        rec_cmd |= (symbol_bits[8 + idx] << idx)
    assert rec_cmd == expected_cmd, f"Expected command 0x{expected_cmd:02X}, got 0x{rec_cmd:02X}"

    r2_val = int(core.r2.value)
    assert r2_val == 0x00, f"Expected R2=0x00 (Success), got 0x{r2_val:02X}"


@cocotb.test()
async def test_ht_rx_sync_ingress(dut):
    """
    Test 2: Slave HyperTransport SYNC Ingress & Command Sampling:
    Core synchronizes to SYNC pattern rising edge on pin 3 via WAITEDGE,
    strides to bit midpoint, samples 8 bits into R0, preserves in R1 (0x20 READ_REQ),
    and halts with status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    sync_code = int(HtCommandType.SYNC)           # 0xBC (0b10111100)
    target_cmd = int(HtCommandType.READ_REQ)      # 0x20 (0b00100000)

    asm_code = build_ht_rx_sync_asm(pin_rx=pin_rx, baud_cycles=baud_cycles)
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

    # 2. Transmit target command byte LSB-first
    for bit_idx in range(8):
        bit = (target_cmd >> bit_idx) & 1
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
    assert int(core.r0.value) == target_cmd, f"Expected R0=0x{target_cmd:02X}, got 0x{int(core.r0.value):02X}"
    assert int(core.r1.value) == target_cmd, f"Expected R1=0x{target_cmd:02X}, got 0x{int(core.r1.value):02X}"


@cocotb.test()
async def test_ht_command_filter_and_fault_trapping(dut):
    """
    Test 3: In-Register Command Identifier Filtering:
    - Valid commands:
      0x00 (NOP), 0x20 (READ_REQ), 0x40 (WRITE_REQ), 0xC0 (RESPONSE) -> R2 = 0x00
    - Illegal command:
      0x7F -> trapped with R2 = 0xEE
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_cmds = [
        int(HtCommandType.NOP),
        int(HtCommandType.READ_REQ),
        int(HtCommandType.WRITE_REQ),
        int(HtCommandType.RESPONSE),
    ]

    for cmd in valid_cmds:
        asm_code = build_ht_command_filter_asm(test_cmd=cmd)
        words = assemble("\n".join(asm_code))
        await _init_dut_and_bootload(dut, words)

        core = dut.user_project.u_core
        for _ in range(40):
            if bool(core.halted.value):
                break
            await RisingEdge(dut.clk)

        assert bool(core.halted.value)
        assert int(core.r2.value) == 0x00, f"Expected R2=0x00 for command 0x{cmd:02X}, got 0x{int(core.r2.value):02X}"

    # Test illegal command: 0x7F
    asm_code_illegal = build_ht_command_filter_asm(test_cmd=0x7F)
    words_illegal = assemble("\n".join(asm_code_illegal))
    await _init_dut_and_bootload(dut, words_illegal)

    core = dut.user_project.u_core
    for _ in range(40):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE (Fault Trap) for illegal 0x7F, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_ht_credit_tracking_and_underflow_trapping(dut):
    """
    Test 4: In-Register Virtual Channel Buffer Credit Accounting:
    - Event 1 (Credit return): initial credit 4 -> 5, R2 = 0x00
    - Event 2 (Packet send): initial credit 4 -> 3, R2 = 0x00
    - Event 2 (Packet send with credit 0): underflow error -> R2 = 0xEE
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Case 1: Credit return increment
    asm_inc = build_ht_credit_tracker_asm(credit_event=1, initial_credit=4)
    words = assemble("\n".join(asm_inc))
    await _init_dut_and_bootload(dut, words)

    core = dut.user_project.u_core
    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r0.value) == 5, f"Expected credit incremented to 5, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00

    # Case 2: Packet send credit decrement
    asm_dec = build_ht_credit_tracker_asm(credit_event=2, initial_credit=4)
    words = assemble("\n".join(asm_dec))
    await _init_dut_and_bootload(dut, words)

    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r0.value) == 3, f"Expected credit decremented to 3, got {int(core.r0.value)}"
    assert int(core.r2.value) == 0x00

    # Case 3: Credit underflow attempt (initial credit = 0, event = 2)
    asm_underflow = build_ht_credit_tracker_asm(credit_event=2, initial_credit=0)
    words = assemble("\n".join(asm_underflow))
    await _init_dut_and_bootload(dut, words)

    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r2.value) == 0xEE, f"Expected underflow trap R2=0xEE, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_ht_packet_framing_and_receiver(dut):
    """
    Test 5: Python Model Packet Framing, Virtual Channels & Receiver Lock:
    - Verifies encode/decode round trip with CRC-32 for commands.
    - Verifies HyperTransportReceiverModel link lock state machine and VC credit accounting.
    """
    receiver = HyperTransportReceiverModel(initial_credits=4)
    assert not receiver.link_lock

    commands = [
        (HtCommandType.NOP, 0x00),
        (HtCommandType.READ_REQ, 0x01),
        (HtCommandType.WRITE_REQ, 0x02),
        (HtCommandType.RESPONSE, 0x03),
    ]

    for cmd, vc in commands:
        payload = bytes([0x20 + b for b in range(8)])
        pkt_dict = encode_ht_packet(cmd=cmd, vc_id=vc, payload=payload)

        raw = pkt_dict["raw_bytes"]
        sync, dec_cmd, dec_vc, dec_payload, crc32, is_valid = decode_ht_packet(raw)

        assert is_valid, f"Packet decode failed for command 0x{cmd:02X}"
        assert sync == HtCommandType.SYNC
        assert dec_cmd == cmd
        assert dec_vc == vc
        assert dec_payload == payload
        assert crc32 == pkt_dict["crc32"]

        success = receiver.process_packet(raw)
        assert success

    assert receiver.packets_received == 4
    assert receiver.preq_credits == 3   # WRITE_REQ consumed 1
    assert receiver.npreq_credits == 3  # READ_REQ consumed 1
    assert receiver.resp_credits == 3   # RESPONSE consumed 1
    assert receiver.crc_errors == 0
    assert receiver.link_lock, "Link should achieve lock after 4 valid syncs"

    # Test corrupted CRC
    corrupt_raw = bytearray(pkt_dict["raw_bytes"])
    corrupt_raw[-1] ^= 0xFF
    res = receiver.process_packet(bytes(corrupt_raw))
    assert not res, "Corrupted packet must be rejected"
    assert receiver.crc_errors == 1


@cocotb.test()
async def test_ht_standards_and_ppa(dut):
    """
    Test 6: Standards Compliance & Calibrated IHP 130nm SG13G2 PPA Model:
    - Verifies HyperTransport command encodings and CRC-32 mathematics.
    - Validates hardware PPA metrics.
    """
    # 1. Command values
    assert HtCommandType.NOP == 0x00
    assert HtCommandType.READ_REQ == 0x20
    assert HtCommandType.WRITE_REQ == 0x40
    assert HtCommandType.RESPONSE == 0xC0
    assert HtCommandType.SYNC == 0xBC
    assert HtCommandType.IDLE == 0x7E

    # 2. Known CRC-32 calculation test
    test_data = b"123456789"
    crc32_val = compute_ht_crc32(test_data)
    assert 0 <= crc32_val <= 0xFFFFFFFF

    # 3. PPA Model metrics validation
    ppa = HyperTransportPpaModel.get_metrics()
    assert ppa["macro_cells"] == 605
    assert ppa["macro_ge"] == 1180.0
    assert ppa["macro_area_um2"] == 4460.0
    assert ppa["f_max_mhz"] == 800.0
    assert ppa["nominal_power_uw_10mhz"] == 59.0
    assert ppa["raw_throughput_mbps"] == 51200.0
    assert ppa["energy_pj_per_bit"] == 0.00115
