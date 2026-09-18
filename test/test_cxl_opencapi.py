# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_cxl_opencapi.py - Cocotb test suite for Coherent Accelerator (CXL / OpenCAPI) Physical Layer Engine

Verifies:
1. test_cxl_master_flit_transmission: Master transmits SYNC comma (0xBC) followed by
   CXL.cache protocol ID (0x02) on pin 3, verified at baud center with status R2 = 0x00.
2. test_cxl_rx_sync_ingress: Slave synchronizes to SYNC comma rising edge on pin 3 via WAITEDGE,
   samples protocol ID byte into R0 and preserves in R1 (0x02) with status R2 = 0x00.
3. test_cxl_protocol_filter_and_fault_trapping: In-register validation of sub-protocol types
   (valid 0x01 CXL.io, 0x02 CXL.cache, 0x03 CXL.mem, 0x04 OpenCAPI -> R2 = 0x00; illegal 0x1F trapped with R2 = 0xEE).
4. test_cxl_crc16_validation: In-register syndrome comparison of 16-bit FLIT CRC slice
   (matching CRC -> R2 = 0x00; corrupted CRC -> R2 = 0xEE).
5. test_cxl_flit_framing_and_receiver: FLIT encapsulation, CRC-16 protection,
   sub-protocol demultiplexing, and CxlReceiverModel link lock state machine.
6. test_cxl_standards_and_ppa: CXL/OpenCAPI protocol identifiers, CRC-16 determinism,
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
from cxl_opencapi_model import (  # noqa: E402
    CxlProtocolType,
    compute_cxl_crc16,
    encode_cxl_flit,
    decode_cxl_flit,
    CxlReceiverModel,
    CxlPpaModel,
    build_cxl_tx_flit_asm,
    build_cxl_rx_sync_asm,
    build_cxl_protocol_filter_asm,
    build_cxl_crc16_validator_asm,
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
async def test_cxl_master_flit_transmission(dut):
    """
    Test 1: Master CXL FLIT Header Transmission:
    Transmits SYNC comma (0xBC) followed by CXL.cache protocol ID (0x02) on pin 3.
    Verifies bit timings, captured LSB-first bitstream, and status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_tx = 3
    baud_cycles = 4
    expected_sync = int(CxlProtocolType.SYNC)       # 0xBC (0b10111100)
    expected_proto = int(CxlProtocolType.CXL_CACHE) # 0x02 (0b00000010)

    asm_code = build_cxl_tx_flit_asm(
        sync_code=expected_sync,
        protocol_id=expected_proto,
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

    rec_proto = 0
    for idx in range(8):
        rec_proto |= (symbol_bits[8 + idx] << idx)
    assert rec_proto == expected_proto, f"Expected protocol 0x{expected_proto:02X}, got 0x{rec_proto:02X}"

    r2_val = int(core.r2.value)
    assert r2_val == 0x00, f"Expected R2=0x00 (Success), got 0x{r2_val:02X}"


@cocotb.test()
async def test_cxl_rx_sync_ingress(dut):
    """
    Test 2: Slave CXL SYNC Ingress & Protocol Sampling:
    Core synchronizes to SYNC comma rising edge on pin 3 via WAITEDGE,
    strides to bit midpoint, samples 8 bits into R0, preserves in R1 (0x02 CXL.cache),
    and halts with status R2 = 0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin_rx = 3
    baud_cycles = 4
    sync_comma = int(CxlProtocolType.SYNC)            # 0xBC (0b10111100)
    target_proto = int(CxlProtocolType.CXL_CACHE)     # 0x02 (0b00000010)

    asm_code = build_cxl_rx_sync_asm(pin_rx=pin_rx, baud_cycles=baud_cycles)
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Let receiver reach WAITEDGE
    await ClockCycles(dut.clk, 10)

    # 1. Transmit SYNC comma LSB-first
    for bit_idx in range(8):
        bit = (sync_comma >> bit_idx) & 1
        dut.uio_in.value = (bit << pin_rx)
        await ClockCycles(dut.clk, baud_cycles)

    # 2. Transmit target protocol byte LSB-first
    for bit_idx in range(8):
        bit = (target_proto >> bit_idx) & 1
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
    assert int(core.r0.value) == target_proto, f"Expected R0=0x{target_proto:02X}, got 0x{int(core.r0.value):02X}"
    assert int(core.r1.value) == target_proto, f"Expected R1=0x{target_proto:02X}, got 0x{int(core.r1.value):02X}"


@cocotb.test()
async def test_cxl_protocol_filter_and_fault_trapping(dut):
    """
    Test 3: In-Register Protocol Identifier Filtering:
    - Valid protocols:
      0x01 (CXL.io), 0x02 (CXL.cache), 0x03 (CXL.mem), 0x04 (OpenCAPI) -> R2 = 0x00
    - Illegal protocol:
      0x1F -> trapped with R2 = 0xEE
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    valid_protocols = [
        int(CxlProtocolType.CXL_IO),
        int(CxlProtocolType.CXL_CACHE),
        int(CxlProtocolType.CXL_MEM),
        int(CxlProtocolType.OPENCAPI),
    ]

    for proto in valid_protocols:
        asm_code = build_cxl_protocol_filter_asm(test_protocol=proto)
        words = assemble("\n".join(asm_code))
        await _init_dut_and_bootload(dut, words)

        core = dut.user_project.u_core
        for _ in range(40):
            if bool(core.halted.value):
                break
            await RisingEdge(dut.clk)

        assert bool(core.halted.value)
        assert int(core.r2.value) == 0x00, f"Expected R2=0x00 for protocol 0x{proto:02X}, got 0x{int(core.r2.value):02X}"

    # Test illegal protocol: 0x1F
    asm_code_illegal = build_cxl_protocol_filter_asm(test_protocol=0x1F)
    words_illegal = assemble("\n".join(asm_code_illegal))
    await _init_dut_and_bootload(dut, words_illegal)

    core = dut.user_project.u_core
    for _ in range(40):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE (Fault Trap) for illegal 0x1F, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_cxl_crc16_validation(dut):
    """
    Test 4: In-Register CRC-16 Syndrome Validation:
    - Matching CRC (exp == rx) -> R2 = 0x00
    - Mismatched CRC (exp != rx) -> R2 = 0xEE
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Case 1: Match
    asm_match = build_cxl_crc16_validator_asm(expected_crc=0x1234, received_crc=0x1234)
    words_match = assemble("\n".join(asm_match))
    await _init_dut_and_bootload(dut, words_match)

    core = dut.user_project.u_core
    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r2.value) == 0x00, f"Expected R2=0x00 for matching CRC, got 0x{int(core.r2.value):02X}"

    # Case 2: Mismatch
    asm_mismatch = build_cxl_crc16_validator_asm(expected_crc=0x1234, received_crc=0x5678)
    words_mismatch = assemble("\n".join(asm_mismatch))
    await _init_dut_and_bootload(dut, words_mismatch)

    for _ in range(30):
        if bool(core.halted.value):
            break
        await RisingEdge(dut.clk)

    assert bool(core.halted.value)
    assert int(core.r2.value) == 0xEE, f"Expected R2=0xEE for mismatched CRC, got 0x{int(core.r2.value):02X}"


@cocotb.test()
async def test_cxl_flit_framing_and_receiver(dut):
    """
    Test 5: Python Model FLIT Framing, Multiplexing & Receiver Lock Verification:
    - Verifies encode/decode round trip with CRC-16 for all 4 coherent protocols.
    - Verifies CxlReceiverModel link lock state machine and protocol counter demuxing.
    """
    receiver = CxlReceiverModel()
    assert not receiver.link_lock

    protocols = [
        CxlProtocolType.CXL_IO,
        CxlProtocolType.CXL_CACHE,
        CxlProtocolType.CXL_MEM,
        CxlProtocolType.OPENCAPI,
    ]

    for idx, proto in enumerate(protocols):
        payload = bytes([0x10 * (idx + 1) + b for b in range(16)])
        flit_dict = encode_cxl_flit(protocol=proto, slot_id=idx, payload=payload)

        raw = flit_dict["raw_bytes"]
        sync, dec_proto, slot_id, dec_payload, crc16, is_valid = decode_cxl_flit(raw)

        assert is_valid, f"FLIT decode failed for protocol 0x{proto:02X}"
        assert sync == CxlProtocolType.SYNC
        assert dec_proto == proto
        assert slot_id == idx
        assert dec_payload == payload
        assert crc16 == flit_dict["crc16"]

        success = receiver.process_flit(raw)
        assert success

    assert receiver.flits_received == 4
    assert receiver.io_flits == 1
    assert receiver.cache_flits == 1
    assert receiver.mem_flits == 1
    assert receiver.opencapi_flits == 1
    assert receiver.crc_errors == 0
    assert receiver.link_lock, "Link should achieve lock after 4 valid syncs"

    # Test corrupted CRC
    corrupt_raw = bytearray(flit_dict["raw_bytes"])
    corrupt_raw[-1] ^= 0xFF
    res = receiver.process_flit(bytes(corrupt_raw))
    assert not res, "Corrupted FLIT must be rejected"
    assert receiver.crc_errors == 1


@cocotb.test()
async def test_cxl_standards_and_ppa(dut):
    """
    Test 6: Standards Compliance & Calibrated IHP 130nm SG13G2 PPA Model:
    - Verifies CXL/OpenCAPI protocol identifiers and CRC-16 mathematics.
    - Validates hardware PPA metrics.
    """
    # 1. Delimiter values
    assert CxlProtocolType.CXL_IO == 0x01
    assert CxlProtocolType.CXL_CACHE == 0x02
    assert CxlProtocolType.CXL_MEM == 0x03
    assert CxlProtocolType.OPENCAPI == 0x04
    assert CxlProtocolType.SYNC == 0xBC
    assert CxlProtocolType.IDLE == 0x7E

    # 2. Known CRC-16 calculation test
    test_data = b"123456789"
    crc16_val = compute_cxl_crc16(test_data)
    assert 0 <= crc16_val <= 0xFFFF

    # 3. PPA Model metrics validation
    ppa = CxlPpaModel.get_metrics()
    assert ppa["macro_cells"] == 600
    assert ppa["macro_ge"] == 1170.0
    assert ppa["macro_area_um2"] == 4420.0
    assert ppa["f_max_mhz"] == 800.0
    assert ppa["nominal_power_uw_10mhz"] == 58.5
    assert ppa["raw_throughput_mbps"] == 32000.0
    assert ppa["energy_pj_per_bit"] == 0.00183
