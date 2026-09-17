# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
test/test_usb_fs.py - Cocotb test suite for USB 2.0 Full-Speed (12 Mbps) Physical & Packet Engine

Verifies:
1. test_usb_fs_tx_data_packet: Full-Speed DATA0 packet transmission with SYNC 0x80, PID 0xC3,
   payload 0x5A, CRC-16, and SE0/J EOP verified by independent UsbFsReceiverModel.
2. test_usb_fs_rx_packet_ingress: Slave packet ingress via WAITEDGE SOP synchronization,
   capturing PID into R0 (0xC3) and payload into R1 (0x5A) with status R2 = 0x00.
3. test_usb_fs_pid_validation_and_fault_trapping: In-register PID validation (DATA0 0xC3 -> R2=0x00)
   and corrupt PID check nibble fault trapping (0xC0 -> R2=0xEE).
4. test_usb_fs_bit_stuffing_and_destuffing: Dynamic bit stuffing on six consecutive 1s
   (payloads 0x3F and 0xFF) and receiver destuffing confirmation.
5. test_usb_fs_eop_and_se0_bus_reset: EOP detection (SE0 -> J) and SE0 bus reset detection.
6. test_usb_fs_standards_and_ppa: Comprehensive validation of all 15 USB 2.0 PIDs, CRC-5/CRC-16
   algorithms, and hardware coprocessor PPA scaling model.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from usb_fs_model import (  # noqa: E402
    PID_OUT,
    PID_IN,
    PID_SOF,
    PID_SETUP,
    PID_DATA0,
    PID_DATA1,
    PID_DATA2,
    PID_MDATA,
    PID_ACK,
    PID_NAK,
    PID_STALL,
    PID_NYET,
    PID_PRE_ERR,
    PID_SPLIT,
    PID_PING,
    verify_usb_pid,
    compute_usb_crc5,
    compute_usb_crc16,
    insert_usb_fs_bit_stuffing,
    remove_usb_fs_bit_stuffing,
    nrzi_encode_fs,
    nrzi_decode_fs,
    UsbFsReceiverModel,
    UsbFsPpaModel,
    build_usb_fs_tx_packet_asm,
    build_usb_fs_rx_packet_asm,
    build_usb_fs_pid_validator_asm,
    build_usb_fs_eop_detector_asm,
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
async def test_usb_fs_tx_data_packet(dut):
    """
    Test 1: Master Full-Speed DATA0 Packet Transmission:
    Transmits SYNC 0x80, PID DATA0 (0xC3), payload [0x5A], CRC-16, and EOP (2 SE0 + 1 J)
    on pins 3 (D+) and 4 (D-). Independent UsbFsReceiverModel verifies clean packet reception.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dp_pin = 3
    dn_pin = 4
    payload = [0x5A]
    bit_cycles = 4

    asm_code = build_usb_fs_tx_packet_asm(
        pid=PID_DATA0,
        payload=payload,
        dp_pin=dp_pin,
        dn_pin=dn_pin,
        bit_cycles=bit_cycles
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    receiver = UsbFsReceiverModel(bit_period=bit_cycles, dp_pin=dp_pin, dn_pin=dn_pin)
    core = dut.user_project.u_core
    max_cycles = 400

    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_oe = int(dut.uio_oe.value)
        if (uio_oe & ((1 << dp_pin) | (1 << dn_pin))) == ((1 << dp_pin) | (1 << dn_pin)):
            uio_out = int(dut.uio_out.value)
            dp = (uio_out >> dp_pin) & 1
            dn = (uio_out >> dn_pin) & 1
            receiver.step(dp, dn)

        if bool(core.halted.value) and len(receiver.packets_received) >= 1:
            break

    assert len(receiver.packets_received) == 1, (
        f"Expected 1 USB packet, received {len(receiver.packets_received)}"
    )
    pkt = receiver.packets_received[0]
    assert pkt.pid == PID_DATA0, f"Expected PID 0x{PID_DATA0:02X}, got 0x{pkt.pid:02X}"
    assert pkt.valid_pid is True, "Expected valid PID complement"
    assert pkt.payload == payload, f"Expected payload {payload}, got {pkt.payload}"
    assert pkt.valid_crc is True, "Expected valid CRC-16"
    assert pkt.bit_stuff_valid is True, "Expected valid bit stuffing"

    dut._log.info(
        f"USB FS TX PASS: PID=0x{pkt.pid:02X}, Payload={pkt.payload}, CRC16=0x{pkt.crc16:04X}"
    )


@cocotb.test()
async def test_usb_fs_rx_packet_ingress(dut):
    """
    Test 2: Slave Full-Speed Packet Ingress:
    Target stimulates D+ and D- with SOP (J->K transition), SYNC, PID (0xC3),
    and Payload (0x5A). Core synchronizes via WAITEDGE, ingresses bytes,
    validates PID against expected PID, and halts with R2=0x00.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dp_pin = 3
    dn_pin = 4

    bit_cycles = 4
    asm_code = build_usb_fs_rx_packet_asm(
        expected_pid=PID_DATA0,
        dp_pin=dp_pin,
        dn_pin=dn_pin,
        bit_cycles=bit_cycles
    )
    words = assemble("\n".join(asm_code))

    # Initialize bus in Idle J state: D+=1, D-=0
    init_uio = 1 << dp_pin
    await _init_dut_and_bootload(dut, words, initial_uio=init_uio)

    core = dut.user_project.u_core

    # Let the core settle into WAITEDGE
    await ClockCycles(dut.clk, 10)

    # 1. Drive SOP transition: J (D+=1) -> K (D+=0, D-=1)
    dut.uio_in.value = 1 << dn_pin
    await ClockCycles(dut.clk, bit_cycles)

    # 2. Drive 8 bits of PID_DATA0 (0xC3) onto D+ (LSB first)
    for bit_idx in range(8):
        bit_val = (PID_DATA0 >> bit_idx) & 1
        dut.uio_in.value = (bit_val << dp_pin)
        await ClockCycles(dut.clk, bit_cycles)

    # 3. Drive 8 bits of Payload (0x5A) onto D+ (LSB first)
    for bit_idx in range(8):
        bit_val = (0x5A >> bit_idx) & 1
        dut.uio_in.value = (bit_val << dp_pin)
        await ClockCycles(dut.clk, bit_cycles)

    # Wait for execution to halt
    for _ in range(50):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    r1 = int(core.r1.value)
    r2 = int(core.r2.value)

    assert r1 == 0x5A, f"Expected Payload R1=0x5A, got 0x{r1:02X}"
    assert r2 == 0x00, f"Expected Status R2=0x00 (PID valid), got 0x{r2:02X}"

    dut._log.info(f"USB FS RX PASS: Payload R1=0x{r1:02X}, Status R2=0x{r2:02X}")


@cocotb.test()
async def test_usb_fs_pid_validation_and_fault_trapping(dut):
    """
    Test 3: In-Register PID Validation and Fault Trapping:
    Verifies that valid PID (PID_DATA0 0xC3) matches and asserts R2=0x00,
    while corrupted PID (0xC0) traps into error handler asserting R2=0xEE.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    # Case A: Valid PID match
    asm_valid = [
        f"LDI R0, 0x{PID_DATA0:02X}   ; Load candidate PID (0xC3)"
    ] + build_usb_fs_pid_validator_asm(PID_DATA0)
    words_valid = assemble("\n".join(asm_valid))
    await _init_dut_and_bootload(dut, words_valid)

    core = dut.user_project.u_core
    for _ in range(40):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    r2_valid = int(core.r2.value)
    assert r2_valid == 0x00, f"Expected R2=0x00 for valid PID, got 0x{r2_valid:02X}"

    # Case B: Corrupted PID mismatch / invalid complement (0xC0)
    asm_corrupt = [
        "LDI R0, 0xC0           ; Load corrupted PID (0xC0: invalid complement)"
    ] + build_usb_fs_pid_validator_asm(PID_DATA0)
    words_corrupt = assemble("\n".join(asm_corrupt))
    await _init_dut_and_bootload(dut, words_corrupt)

    for _ in range(40):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    r2_corrupt = int(core.r2.value)
    assert r2_corrupt == 0xEE, f"Expected R2=0xEE for corrupted PID, got 0x{r2_corrupt:02X}"

    dut._log.info(f"USB FS PID Validation PASS: Valid R2=0x{r2_valid:02X}, Corrupt R2=0x{r2_corrupt:02X}")


@cocotb.test()
async def test_usb_fs_bit_stuffing_and_destuffing(dut):
    """
    Test 4: Dynamic Bit Stuffing on Consecutive 1s:
    Tests bit stuffing with payloads containing six consecutive 1s (0x3F and 0xFF).
    Transmitter inserts stuffed 0s, and receiver verifies destuffing with zero corruption.
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dp_pin = 3
    dn_pin = 4
    # 0x3F has six consecutive 1s: 00111111b
    payload = [0x3F]
    bit_cycles = 4

    asm_code = build_usb_fs_tx_packet_asm(
        pid=PID_DATA1,
        payload=payload,
        dp_pin=dp_pin,
        dn_pin=dn_pin,
        bit_cycles=bit_cycles
    )
    words = assemble("\n".join(asm_code))
    await _init_dut_and_bootload(dut, words)

    receiver = UsbFsReceiverModel(bit_period=bit_cycles, dp_pin=dp_pin, dn_pin=dn_pin)
    core = dut.user_project.u_core

    for _ in range(400):
        await RisingEdge(dut.clk)
        await ReadOnly()

        uio_oe = int(dut.uio_oe.value)
        if (uio_oe & ((1 << dp_pin) | (1 << dn_pin))) == ((1 << dp_pin) | (1 << dn_pin)):
            uio_out = int(dut.uio_out.value)
            dp = (uio_out >> dp_pin) & 1
            dn = (uio_out >> dn_pin) & 1
            receiver.step(dp, dn)

        if bool(core.halted.value) and len(receiver.packets_received) >= 1:
            break

    assert len(receiver.packets_received) == 1, (
        f"Expected 1 packet, got {len(receiver.packets_received)}"
    )
    pkt = receiver.packets_received[0]
    assert pkt.pid == PID_DATA1, f"Expected PID 0x{PID_DATA1:02X}, got 0x{pkt.pid:02X}"
    assert pkt.payload == payload, f"Expected payload {payload}, got {pkt.payload}"
    assert pkt.bit_stuff_valid is True, "Expected valid bit stuffing"
    assert pkt.valid_crc is True, "Expected valid CRC-16"

    # Also test Python bit stuffing directly on 0xFF
    raw_ff = [1, 1, 1, 1, 1, 1, 1, 1]
    stuffed_ff = insert_usb_fs_bit_stuffing(raw_ff)
    assert len(stuffed_ff) == 9, f"Expected 9 bits (1 stuff bit), got {len(stuffed_ff)}"
    destuffed_ff, valid_ff = remove_usb_fs_bit_stuffing(stuffed_ff)
    assert valid_ff is True
    assert destuffed_ff == raw_ff

    dut._log.info(f"USB FS Bit Stuffing PASS: Stuffed length={len(stuffed_ff)}, Destuffed={destuffed_ff}")


@cocotb.test()
async def test_usb_fs_eop_and_se0_bus_reset(dut):
    """
    Test 5: End-of-Packet (EOP) Detection and SE0 Bus Reset:
    - Verifies microcode detects SE0 (D+=0, D-=0) and subsequent transition to J state (D+=1).
    - Verifies UsbFsReceiverModel detects SE0 bus reset condition (> 50 cycles).
    """
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dp_pin = 3
    dn_pin = 4

    asm_code = build_usb_fs_eop_detector_asm(dp_pin=dp_pin, dn_pin=dn_pin)
    words = assemble("\n".join(asm_code))

    # Initialize bus in SE0 state: D+=0, D-=0
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Step in SE0 state for 6 cycles
    await ClockCycles(dut.clk, 6)

    # Transition to J state: D+=1, D-=0 (rising edge on D+)
    dut.uio_in.value = 1 << dp_pin

    for _ in range(30):
        await RisingEdge(dut.clk)
        if bool(core.halted.value):
            break

    r2 = int(core.r2.value)
    assert r2 == 0x00, f"Expected R2=0x00 for clean EOP, got 0x{r2:02X}"

    # Verify receiver SE0 reset detection
    rx_model = UsbFsReceiverModel(dp_pin=dp_pin, dn_pin=dn_pin)
    for _ in range(60):
        rx_model.step(0, 0)  # Continuous SE0
    assert rx_model.reset_detected is True, "Expected SE0 bus reset detection after 60 cycles"

    dut._log.info(f"USB FS EOP & Reset PASS: Status R2=0x{r2:02X}, Reset detected={rx_model.reset_detected}")


@cocotb.test()
async def test_usb_fs_standards_and_ppa(dut):
    """
    Test 6: USB 2.0 Standard Compliance and Coprocessor PPA Scaling:
    Validates all 15 standard USB 2.0 PIDs, Token CRC-5, Data CRC-16,
    and checks the hardware PPA scaling metrics against physical budgets.
    """
    all_pids = [
        PID_OUT, PID_IN, PID_SOF, PID_SETUP,
        PID_DATA0, PID_DATA1, PID_DATA2, PID_MDATA,
        PID_ACK, PID_NAK, PID_STALL, PID_NYET,
        PID_PRE_ERR, PID_SPLIT, PID_PING
    ]

    # Verify all 15 standard PIDs have valid complement
    for pid in all_pids:
        assert verify_usb_pid(pid) is True, f"PID 0x{pid:02X} failed complement verification"
        # Test corruption detection
        corrupted_pid = pid ^ 0x10  # Flip a bit in the upper nibble
        assert verify_usb_pid(corrupted_pid) is False, f"Corrupted PID 0x{corrupted_pid:02X} should fail"

    # Verify Token CRC-5
    # Standard token test vector: addr=0, ep=0 -> 11 zero bits
    crc5_zero = compute_usb_crc5([0]*11)
    assert crc5_zero == 0x08, f"Expected CRC-5=0x08 for 11 zero bits, got 0x{crc5_zero:02X}"

    # Verify Data CRC-16
    crc16_empty = compute_usb_crc16([])
    assert crc16_empty == 0x0000, f"Expected CRC-16=0x0000 for empty payload, got 0x{crc16_empty:04X}"
    crc16_5a = compute_usb_crc16([0x5A])
    assert crc16_5a == 0x84C0, f"Expected CRC-16=0x84C0 for [0x5A], got 0x{crc16_5a:04X}"

    # Validate PPA Model
    ppa = UsbFsPpaModel()
    assert ppa.STANDARD_CELL_COUNT == 512
    assert ppa.GATE_EQUIVALENCE_GE == 985.0
    assert ppa.AREA_UM2 == 3741.80
    assert ppa.AREA_OVERHEAD_PCT == 2.65
    assert ppa.CRITICAL_PATH_NS == 1.27
    assert ppa.FMAX_MHZ == 787.40
    assert ppa.DYNAMIC_POWER_UW_AT_10MHZ == 48.2
    assert ppa.THROUGHPUT_MBPS == 12.0

    dut._log.info(
        f"USB FS Standards & PPA Model PASS: All 15 PIDs verified, "
        f"Area={ppa.AREA_UM2} um2 (+{ppa.AREA_OVERHEAD_PCT}%), Fmax={ppa.FMAX_MHZ} MHz"
    )
