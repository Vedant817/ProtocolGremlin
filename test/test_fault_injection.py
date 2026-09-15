# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Cocotb tests for the Deterministic Fault Injection & Protocol Stress Engine.

Verifies intentional injection of protocol-level anomalies and non-compliances:
1. CAN 2.0A Faults:
   - Bit stuff error (transmitting 6 consecutive dominant bits without complementary stuff bit).
   - CRC-15 checksum corruption (inverting CRC bits).
   - End-of-Frame (EOF) dominant bit violation.
2. HDLC / SDLC Faults:
   - Premature abort sequence (7 consecutive 1s inside active frame).
   - Stuff bit omission on payloads with >= 6 consecutive 1s.
   - Corrupted frame closing delimiter.
3. UART Faults:
   - Framing error (forcing stop bit LOW).
   - Sub-baud noise glitch rejection (1-cycle runt pulse on idle line).
4. Manchester Biphase-L Faults:
   - Biphase violation (omission of mid-bit transition).
5. Electrical Safety:
   - Pin direction and open-drain safety during fault injection.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from fault_injector_model import (  # noqa: E402
    build_can_fault_tx_asm,
    build_hdlc_fault_tx_asm,
    build_uart_fault_tx_asm,
    build_manchester_fault_tx_asm,
)
from can_model import (  # noqa: E402
    compute_can_crc15,
    insert_can_bit_stuffing,
    remove_can_bit_stuffing,
)
from hdlc_model import HdlcReceiver  # noqa: E402
from uart_model import UartReceiver, UartFramingError  # noqa: E402
from manchester_model import ManchesterDecoder  # noqa: E402


async def _init_dut_and_bootload(dut, words: list[int], initial_uio: int = 0):
    """Clean reset and immediate bootload."""
    await RisingEdge(dut.clk)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = initial_uio
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await bootload(dut, words)

    for _ in range(4):
        await FallingEdge(dut.clk)
        dut.uio_in.value = initial_uio
        await RisingEdge(dut.clk)


@cocotb.test()
async def test_can_fault_stuff_error(dut):
    """Verify CAN transmitter injects intentional bit stuffing violation detected by ISO model."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    can_id = 0x123
    data_byte = 0x00
    pin = 4
    bit_period = 8

    asm = build_can_fault_tx_asm(can_id=can_id, data_byte=data_byte, fault_type="stuff_error", bit_period=bit_period, pin=pin)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, initial_uio=(1 << pin))

    # Sample open-drain bus
    sampled_bits = []
    in_frame = False
    sof_cycle = -1

    for cycle in range(600):
        await FallingEdge(dut.clk)
        asic_out = (int(dut.uio_out.value) >> pin) & 1 if dut.uio_out.value.is_resolvable else 1
        asic_oe = (int(dut.uio_oe.value) >> pin) & 1 if dut.uio_oe.value.is_resolvable else 0
        bus = 0 if (asic_oe and asic_out == 0) else 1

        if not in_frame and bus == 0:
            in_frame = True
            sof_cycle = cycle

        if in_frame:
            elapsed = cycle - sof_cycle
            if elapsed % bit_period == 4:
                sampled_bits.append(bus)

        dut.uio_in.value = (bus << pin)
        await RisingEdge(dut.clk)

    # Destuff using ISO 11898 model
    destuffed, is_valid = remove_can_bit_stuffing(sampled_bits)
    dut._log.info(f"CAN Stuff Error test: sampled {len(sampled_bits)} bits, is_valid={is_valid}")
    assert not is_valid, "Expected CAN Stuff Error (is_valid == False), but destuffing succeeded!"


@cocotb.test()
async def test_can_fault_crc_corruption(dut):
    """Verify CAN transmitter injects inverted CRC-15 detected as checksum mismatch."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    can_id = 0x123
    data_byte = 0xA5
    pin = 4
    bit_period = 8

    asm = build_can_fault_tx_asm(can_id=can_id, data_byte=data_byte, fault_type="crc_error", bit_period=bit_period, pin=pin)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, initial_uio=(1 << pin))

    # Calculate expected tx_bits length for pre-ACK field
    raw_bits = [0] + [(can_id >> b) & 1 for b in range(10, -1, -1)] + [0, 0, 0, 0, 0, 0, 1] + [(data_byte >> b) & 1 for b in range(7, -1, -1)]
    corrupted_crc = compute_can_crc15(raw_bits) ^ 0x5555
    raw_with_crc = raw_bits + [(corrupted_crc >> b) & 1 for b in range(14, -1, -1)]
    stuffed_len = len(insert_can_bit_stuffing(raw_with_crc))

    sampled_bits = []
    in_frame = False
    sof_cycle = -1

    for cycle in range(800):
        await FallingEdge(dut.clk)
        asic_out = (int(dut.uio_out.value) >> pin) & 1 if dut.uio_out.value.is_resolvable else 1
        asic_oe = (int(dut.uio_oe.value) >> pin) & 1 if dut.uio_oe.value.is_resolvable else 0
        bus = 0 if (asic_oe and asic_out == 0) else 1

        if not in_frame and bus == 0:
            in_frame = True
            sof_cycle = cycle

        if in_frame:
            elapsed = cycle - sof_cycle
            if elapsed % bit_period == 4:
                sampled_bits.append(bus)

        dut.uio_in.value = (bus << pin)
        await RisingEdge(dut.clk)

    # Bit stuffing is checked on pre-ACK bits (stuffed_len)
    pre_ack_samples = sampled_bits[:stuffed_len]
    destuffed, is_valid = remove_can_bit_stuffing(pre_ack_samples)
    assert is_valid, f"Bit stuffing should be valid in CRC corruption mode, got {is_valid}"
    assert len(destuffed) == 42, f"Expected 42 destuffed bits, got {len(destuffed)}"

    # Reconstruct expected CRC-15 from pre-CRC fields (bits 0 to 26: SOF, ID, RTR, IDE, r0, DLC, Data)
    pre_crc_bits = destuffed[:27]
    expected_crc = compute_can_crc15(pre_crc_bits)

    # Extract received 15-bit CRC (bits 27 to 41)
    received_crc = 0
    for b in range(27, 42):
        received_crc = (received_crc << 1) | destuffed[b]

    dut._log.info(f"CAN CRC Corruption: expected=0x{expected_crc:04X}, received=0x{received_crc:04X}")
    assert expected_crc != received_crc, f"CRC should NOT match! expected=0x{expected_crc:04X}, received=0x{received_crc:04X}"
    assert received_crc == corrupted_crc, f"Received CRC 0x{received_crc:04X} should equal injected corrupted CRC 0x{corrupted_crc:04X}"


@cocotb.test()
async def test_can_fault_eof_dominant_glitch(dut):
    """Verify CAN transmitter drives intentional dominant glitch during Recessive EOF."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    can_id = 0x123
    data_byte = 0xA5
    pin = 4
    bit_period = 8

    asm = build_can_fault_tx_asm(can_id=can_id, data_byte=data_byte, fault_type="eof_error", bit_period=bit_period, pin=pin)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, initial_uio=(1 << pin))

    # Calculate pre-ACK stuffed length
    raw_bits = [0] + [(can_id >> b) & 1 for b in range(10, -1, -1)] + [0, 0, 0, 0, 0, 0, 1] + [(data_byte >> b) & 1 for b in range(7, -1, -1)]
    valid_crc = compute_can_crc15(raw_bits)
    raw_with_crc = raw_bits + [(valid_crc >> b) & 1 for b in range(14, -1, -1)]
    stuffed_len = len(insert_can_bit_stuffing(raw_with_crc))

    sampled_bits = []
    in_frame = False
    sof_cycle = -1

    for cycle in range(900):
        await FallingEdge(dut.clk)
        asic_out = (int(dut.uio_out.value) >> pin) & 1 if dut.uio_out.value.is_resolvable else 1
        asic_oe = (int(dut.uio_oe.value) >> pin) & 1 if dut.uio_oe.value.is_resolvable else 0
        bus = 0 if (asic_oe and asic_out == 0) else 1

        if not in_frame and bus == 0:
            in_frame = True
            sof_cycle = cycle

        if in_frame:
            elapsed = cycle - sof_cycle
            if elapsed % bit_period == 4:
                sampled_bits.append(bus)

        dut.uio_in.value = (bus << pin)
        await RisingEdge(dut.clk)

    # In EOF error mode:
    # bit stuffed_len: CRC delimiter (1)
    # bit stuffed_len + 1: ACK slot (1)
    # bit stuffed_len + 2: ACK delimiter (1)
    # bit stuffed_len + 3: EOF bit 0 (1)
    # bit stuffed_len + 4: EOF bit 1 (1)
    # bit stuffed_len + 5: EOF bit 2 (0 - Dominant glitch!)
    eof_glitch_idx = stuffed_len + 3 + 2
    dut._log.info(f"Checking EOF dominant bit at index {eof_glitch_idx} (total sampled: {len(sampled_bits)})")
    assert eof_glitch_idx < len(sampled_bits), f"Not enough samples: {len(sampled_bits)} <= {eof_glitch_idx}"
    assert sampled_bits[eof_glitch_idx] == 0, f"Expected Dominant 0 glitch at EOF bit 2, got {sampled_bits[eof_glitch_idx]}"



@cocotb.test()
async def test_hdlc_fault_abort_sequence(dut):
    """Verify HDLC transmitter injects 7 consecutive 1s abort sequence detected by receiver."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin = 1
    bit_period = 16
    asm = build_hdlc_fault_tx_asm(payload=[0xA5], fault_type="abort_sequence", bit_period=bit_period, pin=pin)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, initial_uio=(1 << pin))

    receiver = HdlcReceiver(bit_period=bit_period, pin=pin, initial_level=1)
    sampled_levels = []
    in_frame = False
    start_cycle = -1

    for cycle in range(600):
        await FallingEdge(dut.clk)
        val = (int(dut.uio_out.value) >> pin) & 1 if dut.uio_out.value.is_resolvable else 1

        # Look for falling edge transition of opening flag
        if not in_frame and val == 0:
            in_frame = True
            start_cycle = cycle

        if in_frame:
            elapsed = cycle - start_cycle
            if elapsed % bit_period == bit_period // 2:
                sampled_levels.append(val)

        await RisingEdge(dut.clk)

    # Decode NRZI and frame
    bits = receiver.decode_nrzi(sampled_levels, initial_level=1)
    payload, closing_flag, abort_detected = receiver.decode_frame(bits)
    dut._log.info(f"HDLC Abort: bits={bits}, payload={payload}, abort_detected={abort_detected}")
    assert abort_detected, "Expected HDLC Abort Sequence (7 consecutive 1s), but not detected!"


@cocotb.test()
async def test_hdlc_fault_stuff_omission(dut):
    """Verify HDLC transmitter omitting zero stuffing triggers frame destuffing / abort violation."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin = 1
    bit_period = 16
    asm = build_hdlc_fault_tx_asm(payload=[0xFF], fault_type="stuff_omission", bit_period=bit_period, pin=pin)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, initial_uio=(1 << pin))

    receiver = HdlcReceiver(bit_period=bit_period, pin=pin, initial_level=1)
    sampled_levels = []
    in_frame = False
    start_cycle = -1

    for cycle in range(800):
        await FallingEdge(dut.clk)
        val = (int(dut.uio_out.value) >> pin) & 1 if dut.uio_out.value.is_resolvable else 1

        if not in_frame and val == 0:
            in_frame = True
            start_cycle = cycle

        if in_frame:
            elapsed = cycle - start_cycle
            if elapsed % bit_period == bit_period // 2:
                sampled_levels.append(val)

        await RisingEdge(dut.clk)

    bits = receiver.decode_nrzi(sampled_levels, initial_level=1)
    payload, closing_flag, abort_detected = receiver.decode_frame(bits)
    dut._log.info(f"HDLC Stuff Omission: payload={payload}, closing_flag={closing_flag}, abort_detected={abort_detected}")
    # Since 8 consecutive 1s were transmitted without stuff bits, it must be detected as an abort or malformed payload
    assert abort_detected or payload != [0xFF], "Unstuffed 0xFF should NOT be decoded cleanly as [0xFF]!"


@cocotb.test()
async def test_uart_fault_framing_error(dut):
    """Verify UART transmitter forcing stop bit LOW raises UartFramingError in receiver."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin = 0
    bit_period = 8
    byte_val = 0x5A

    asm = build_uart_fault_tx_asm(byte_val=byte_val, fault_type="framing_error", bit_period=bit_period, pin=pin)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, initial_uio=(1 << pin))

    receiver = UartReceiver(bit_period_cycles=bit_period)
    framing_error_caught = False

    for _ in range(200):
        await FallingEdge(dut.clk)
        val = (int(dut.uio_out.value) >> pin) & 1 if dut.uio_out.value.is_resolvable else 1
        try:
            receiver.step(val)
        except UartFramingError as e:
            framing_error_caught = True
            dut._log.info(f"Caught expected UART framing error: {e}")
            break
        await RisingEdge(dut.clk)

    assert framing_error_caught, "Expected UartFramingError on corrupted stop bit, but none raised!"


@cocotb.test()
async def test_uart_fault_noise_glitch(dut):
    """Verify UART receiver rejects 1-cycle runt pulse without decoding spurious bytes."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin = 0
    bit_period = 8

    asm = build_uart_fault_tx_asm(byte_val=0x00, fault_type="noise_glitch", bit_period=bit_period, pin=pin)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, initial_uio=(1 << pin))

    receiver = UartReceiver(bit_period_cycles=bit_period)

    for _ in range(100):
        await FallingEdge(dut.clk)
        val = (int(dut.uio_out.value) >> pin) & 1 if dut.uio_out.value.is_resolvable else 1
        receiver.step(val)
        await RisingEdge(dut.clk)

    dut._log.info(f"UART Noise Glitch: received_bytes={receiver.received_bytes}")
    assert len(receiver.received_bytes) == 0, f"Expected 0 decoded bytes on glitch, got {receiver.received_bytes}"


@cocotb.test()
async def test_manchester_fault_biphase_violation(dut):
    """Verify Manchester transmitter omitting mid-bit transition triggers biphase violation."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin = 2
    half_period = 4
    bit_period = half_period * 2
    data_byte = 0x3C
    violation_idx = 3

    asm = build_manchester_fault_tx_asm(data_byte=data_byte, violation_bit_index=violation_idx, half_period=half_period, pin=pin)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, initial_uio=0)

    decoder = ManchesterDecoder(half_period=half_period)
    in_frame = False
    start_cycle = -1
    symbols = []

    for cycle in range(300):
        await FallingEdge(dut.clk)
        val = (int(dut.uio_out.value) >> pin) & 1 if dut.uio_out.value.is_resolvable else 0

        # Look for preamble rising edge (0 -> 1)
        if not in_frame and val == 1:
            in_frame = True
            start_cycle = cycle

        if in_frame:
            elapsed = cycle - start_cycle
            # Preamble takes 1 bit cell (8 cycles). 8 data bits begin at cycle 8.
            if elapsed >= bit_period:
                data_elapsed = elapsed - bit_period
                bit_idx = data_elapsed // bit_period
                phase = data_elapsed % bit_period

                if bit_idx < 8:
                    if phase == 2:  # Midpoint of first half-bit
                        symbols.append(val)
                    elif phase == 6:  # Midpoint of second half-bit
                        symbols.append(val)

        await RisingEdge(dut.clk)

    dut._log.info(f"Manchester symbols ({len(symbols)}): {symbols}")
    assert len(symbols) == 16, f"Expected 16 half-bit symbols, got {len(symbols)}"

    decoded_byte, valid = decoder.decode_symbols(symbols)
    dut._log.info(f"Decoded Manchester: byte=0x{decoded_byte:02X}, valid={valid}, violations={decoder.phase_violations}")
    assert not valid, "Expected biphase violation (valid == False), but frame was marked valid!"
    assert decoder.phase_violations >= 1, f"Expected >= 1 phase violation, got {decoder.phase_violations}"


@cocotb.test()
async def test_fault_injection_pin_safety(dut):
    """Verify open-drain and tri-state pin safety during fault injection."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    pin = 4
    asm = build_can_fault_tx_asm(can_id=0x123, data_byte=0x00, fault_type="stuff_error", bit_period=8, pin=pin)
    words = assemble(asm)
    await _init_dut_and_bootload(dut, words, initial_uio=0)

    # During transmission, whenever line is recessive (1), uio_oe should NOT actively drive 1
    for _ in range(400):
        await FallingEdge(dut.clk)
        oe = (int(dut.uio_oe.value) >> pin) & 1 if dut.uio_oe.value.is_resolvable else 0
        out = (int(dut.uio_out.value) >> pin) & 1 if dut.uio_out.value.is_resolvable else 0
        # In open-drain mode, active driving only happens when out == 0
        if oe == 1:
            assert out == 0, f"Open-drain violation: pin_oe is 1 while pin_out is {out}!"
        await RisingEdge(dut.clk)
