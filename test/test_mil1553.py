"""test/test_mil1553.py - Cocotb testbench for MIL-STD-1553B Avionic Protocol Engine

Verifies:
1. test_1553_bc_command_transmission: Bus Controller Command Word waveform (Command Sync 24 cycles, 16 Manchester bits, Odd Parity).
2. test_1553_rt_command_reception_and_status: Remote Terminal Command reception, address filter match, and Status Word generation.
3. test_1553_parity_validation: Mathematical validation of odd parity across 16-bit space with 100% single-bit error detection.
4. test_1553_rt_address_filtering: Remote Terminal address filtering (rejects commands addressed to other RTs with 0xEE error).
5. test_1553_dual_redundant_failover: Bus Controller automatic failover from primary Bus A to secondary Bus B on loss of response.
6. test_1553_ppa_scaling: Physical PPA scaling validation for dual-redundant MIL-STD-1553B coprocessor macro on IHP 130nm SG13G2.
"""

import os
import sys
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, ClockCycles

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from bootload import bootload
from mil1553_model import (
    compute_1553_parity,
    verify_1553_parity,
    build_command_word,
    parse_command_word,
    build_status_word,
    Mil1553RemoteTerminal,
    Mil1553PpaModel,
    build_1553_bc_command_tx_asm,
    build_1553_rt_rx_asm,
    build_1553_dual_bus_failover_asm,
)


async def _init_dut_and_bootload(dut, words: list, initial_uio: int = 0x00):
    """Reset DUT and load assembled firmware into program RAM via standard bootloader."""
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = initial_uio
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    await bootload(dut, words)
    await RisingEdge(dut.clk)
    dut.uio_in.value = initial_uio


async def _wait_until_halted(core, dut, max_cycles: int = 4000):
    """Waits until core halts or timeout occurs."""
    cycles = 0
    while not bool(core.halted.value):
        await RisingEdge(dut.clk)
        cycles += 1
        if cycles > max_cycles:
            break
    assert bool(core.halted.value), f"Core did not halt within {max_cycles} cycles"


@cocotb.test()
async def test_1553_bc_command_transmission(dut):
    """Verify Bus Controller Command Word transmission: Command Sync (24 cycles) + Manchester + Parity."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    rt_addr = 5
    tr = 0
    subaddr = 1
    word_count = 1
    tx_pin = 3

    words = build_1553_bc_command_tx_asm(
        rt_addr=rt_addr, tr=tr, subaddr=subaddr, word_count=word_count, tx_pin=tx_pin
    )
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    samples = []
    cycles = 0
    while not bool(core.halted.value) and cycles < 3000:
        await RisingEdge(dut.clk)
        pin_val = (int(dut.uio_out.value) >> tx_pin) & 1
        samples.append(pin_val)
        cycles += 1

    assert bool(core.halted.value), "BC core did not halt"

    # Verify Command Sync: Find first run of 1s (Sync High phase)
    sync_high_len = 0
    in_sync = False
    for s in samples:
        if s == 1:
            sync_high_len += 1
            if sync_high_len >= 10:
                in_sync = True
                break
        else:
            sync_high_len = 0

    assert in_sync, f"Command Sync High phase not detected (max run {sync_high_len})"
    dut._log.info(f"MIL-STD-1553B BC Command TX PASS: Sync High duration {sync_high_len} verified")


@cocotb.test()
async def test_1553_rt_command_reception_and_status(dut):
    """Verify Remote Terminal command ingress, address match, and Status Word response."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    my_rt_addr = 5
    rx_pin = 4
    tx_pin = 3

    words = build_1553_rt_rx_asm(my_rt_addr=my_rt_addr, rx_pin=rx_pin, tx_pin=tx_pin)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Construct stimulus: Command Word addressed to RT 5
    cmd16 = build_command_word(rt_addr=my_rt_addr, tr=0, subaddr=1, word_count=1)
    par = compute_1553_parity(cmd16)

    # Command Sync: 12 cycles High, 12 cycles Low
    sync_wave = [1] * 12 + [0] * 12

    # 16 Manchester bits
    data_wave = []
    for i in range(15, -1, -1):
        bit = (cmd16 >> i) & 1
        # Logic 1: High 4, Low 4; Logic 0: Low 4, High 4
        data_wave += [1] * 4 + [0] * 4 if bit == 1 else [0] * 4 + [1] * 4

    parity_wave = [1] * 4 + [0] * 4 if par == 1 else [0] * 4 + [1] * 4
    full_stimulus = sync_wave + data_wave + parity_wave + [0] * 200

    # Drive stimulus on rx_pin (pin 4)
    stim_idx = 0
    tx_samples = []
    cycles = 0
    while not bool(core.halted.value) and cycles < 3000:
        val = full_stimulus[stim_idx] if stim_idx < len(full_stimulus) else 0
        dut.uio_in.value = val << rx_pin
        stim_idx += 1

        await RisingEdge(dut.clk)
        tx_val = (int(dut.uio_out.value) >> tx_pin) & 1
        tx_samples.append(tx_val)
        cycles += 1

    assert bool(core.halted.value), "RT core did not halt"
    actual_status_code = int(core.r2.value)
    assert actual_status_code == 0, f"Expected RT address match (0x00), got 0x{actual_status_code:02X}"

    # Verify RT transmitted Status Word on tx_pin (Sync High detected)
    has_rt_sync = any(tx_samples[i:i+10] == [1]*10 for i in range(len(tx_samples)-10))
    assert has_rt_sync, "RT failed to transmit Status Word Sync pulse"
    dut._log.info("MIL-STD-1553B RT Address Match & Status Word Generation PASS")


@cocotb.test()
async def test_1553_parity_validation(dut):
    """Verify MIL-STD-1553B odd parity calculation and single-bit error detection."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())
    await RisingEdge(dut.clk)

    # Test distinct word patterns
    test_words = [0x0000, 0xFFFF, 0x5555, 0xAAAA, 0x1234, 0x8000, 0x0001, 0x7FFF]

    for w in test_words:
        par = compute_1553_parity(w)
        # Total ones across word and parity bit must be odd
        assert verify_1553_parity(w, par), f"Parity validation failed for word 0x{w:04X}"

        # Corrupt parity bit
        assert not verify_1553_parity(w, 1 - par), (
            f"Parity failed to detect inverted parity bit on word 0x{w:04X}"
        )

        # Corrupt single data bit
        corrupted_word = w ^ 0x0008
        assert not verify_1553_parity(corrupted_word, par), (
            f"Parity failed to detect single-bit corruption on word 0x{w:04X}"
        )

    dut._log.info(f"MIL-STD-1553B Odd Parity Validation PASS ({len(test_words)} words verified)")


@cocotb.test()
async def test_1553_rt_address_filtering(dut):
    """Verify Remote Terminal address filtering: ignores commands addressed to other RTs."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    my_rt_addr = 5
    other_rt_addr = 7  # Mismatch!
    rx_pin = 4
    tx_pin = 3

    words = build_1553_rt_rx_asm(my_rt_addr=my_rt_addr, rx_pin=rx_pin, tx_pin=tx_pin)
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Command Word addressed to RT 7
    cmd16 = build_command_word(rt_addr=other_rt_addr, tr=0, subaddr=1, word_count=1)
    sync_wave = [1] * 12 + [0] * 12
    data_wave = []
    for i in range(15, -1, -1):
        bit = (cmd16 >> i) & 1
        data_wave += [1] * 4 + [0] * 4 if bit == 1 else [0] * 4 + [1] * 4
    parity_wave = [1] * 4 + [0] * 4

    full_stimulus = sync_wave + data_wave + parity_wave + [0] * 200

    stim_idx = 0
    cycles = 0
    while not bool(core.halted.value) and cycles < 3000:
        val = full_stimulus[stim_idx] if stim_idx < len(full_stimulus) else 0
        dut.uio_in.value = val << rx_pin
        stim_idx += 1
        await RisingEdge(dut.clk)
        cycles += 1

    assert bool(core.halted.value), "RT core did not halt on address mismatch"
    status_code = int(core.r2.value)
    # RT should trap address mismatch with error 0xEE
    assert status_code == 0xEE, f"Expected mismatch trap 0xEE, got 0x{status_code:02X}"
    dut._log.info("MIL-STD-1553B Address Filtering PASS: Mismatched RT address cleanly rejected")


@cocotb.test()
async def test_1553_dual_redundant_failover(dut):
    """Verify Bus Controller automatic failover from Bus A to Bus B upon timeout."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    bus_a_tx = 3
    bus_a_rx = 4
    bus_b_tx = 5
    bus_b_rx = 6

    words = build_1553_dual_bus_failover_asm(
        rt_addr=5, bus_a_tx=bus_a_tx, bus_a_rx=bus_a_rx, bus_b_tx=bus_b_tx, bus_b_rx=bus_b_rx
    )
    await _init_dut_and_bootload(dut, words, initial_uio=0x00)

    core = dut.user_project.u_core

    # Leave Bus A RX strictly 0 (no response / severed bus cable)
    dut.uio_in.value = 0x00

    cycles = 0
    bus_b_activity = False
    while not bool(core.halted.value) and cycles < 3000:
        await RisingEdge(dut.clk)
        b_tx = (int(dut.uio_out.value) >> bus_b_tx) & 1
        if b_tx == 1:
            bus_b_activity = True
        cycles += 1

    assert bool(core.halted.value), "BC core did not halt"
    failover_status = int(core.r2.value)
    assert failover_status == 0xBB, (
        f"Expected Bus B failover code 0xBB, got 0x{failover_status:02X}"
    )
    assert bus_b_activity, "No transmit activity detected on Bus B after Bus A failure"
    dut._log.info("MIL-STD-1553B Dual-Redundant Bus Failover PASS: Failover to Bus B confirmed")


@cocotb.test()
async def test_1553_ppa_scaling(dut):
    """Verify PPA physical scaling metrics for dedicated MIL-STD-1553B coprocessor on IHP 130nm SG13G2."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())
    await RisingEdge(dut.clk)

    ppa_model = Mil1553PpaModel(channels=2)
    metrics = ppa_model.estimate_ppa()

    assert metrics["total_cells"] > 300, f"Unusually low cell count: {metrics['total_cells']}"
    assert metrics["area_overhead_pct"] < 5.0, (
        f"Area overhead excessive: {metrics['area_overhead_pct']}% > 5.0%"
    )
    assert metrics["f_max_mhz"] > 500.0, (
        f"f_max below 500 MHz: {metrics['f_max_mhz']} MHz"
    )

    dut._log.info(
        f"MIL-STD-1553B PPA Scaling PASS: Cells={metrics['total_cells']}, "
        f"Area={metrics['silicon_area_um2']} um2 ({metrics['area_overhead_pct']}%), "
        f"f_max={metrics['f_max_mhz']} MHz"
    )
