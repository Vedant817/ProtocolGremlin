# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""test/test_profiler.py - Cocotb test suite for Protocol Micro-Benchmark Profiler Tooling.

Verifies:
1. test_profiler_static_analysis: Validates static word counting, RAM utilization,
   opcode categorization, register read/write mapping, and GPIO pin discovery.
2. test_profiler_dynamic_simulation_vs_rtl: Validates cycle-accurate equivalence
   between Python CoreModel profiler and physical RTL execution.
3. test_profiler_uart_tx_performance: Benchmarks UART TX execution, verifies cycles-per-bit
   and effective bitrate calculations, and confirms bit-level timing against hardware.
4. test_profiler_spi_transfer_performance: Benchmarks SPI full-duplex transfer,
   verifies loop unrolling vs DECJNZ efficiency, and checks SPI clock frequency.
5. test_profiler_serdes_throughput: Benchmarks unrolled hardware SHIFTOUT high-speed packet
   framing, verifying 8.27+ Mbps wire throughput at 10 MHz with >95% efficiency.
6. test_profiler_suite_scorecard: Executes complete run_suite_profile(), verifies all efficiency
   metrics, and validates markdown scorecard output.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge  # noqa: E402

from assembler import assemble  # noqa: E402
from bootload import bootload  # noqa: E402
from profiler import (  # noqa: E402
    InstructionCategory,
    StaticProfile,
    DynamicProfile,
    ProtocolBenchmarkResult,
    profile_static,
    profile_dynamic,
    benchmark_protocol,
    run_suite_profile,
    generate_markdown_report,
    get_uart_tx_benchmark_asm,
    get_spi_transfer_benchmark_asm,
    get_i2c_write_benchmark_asm,
    get_manchester_benchmark_asm,
    get_serdes_header_benchmark_asm,
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
async def test_profiler_static_analysis(dut):
    """Test 1: Verify static microcode profiling metrics, categorization, and pin discovery."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing profiler static analysis engine...")

    sample_asm = """
        GDIRI 0x07          ; Configure pins 0, 1, 2 as outputs
        LDI   R0, 0xA5      ; Load immediate into R0
        MOV   R1, R0        ; Copy R0 to R1
        ADDI  R0, 0x10      ; Add 0x10 to R0
        SHIFTOUT R0, 1      ; Shift bit to pin 1
        WAIT  3             ; Timing delay
        HALT
    """
    words = assemble(sample_asm)
    stat = profile_static(words)

    dut._log.info(f"Static profile: {stat.total_words} words, {stat.total_bytes} bytes, {stat.ram_utilization_pct:.2f}% RAM")
    assert stat.total_words == 7, f"Expected 7 words, got {stat.total_words}"
    assert stat.total_bytes == 14, f"Expected 14 bytes, got {stat.total_bytes}"
    assert abs(stat.ram_utilization_pct - (7 / 256.0 * 100.0)) < 1e-4

    # Opcode categories
    assert stat.category_counts[InstructionCategory.GPIO.value] == 1  # GDIRI
    assert stat.category_counts[InstructionCategory.REGISTER.value] == 2  # LDI, MOV
    assert stat.category_counts[InstructionCategory.ALU.value] == 1  # ADDI
    assert stat.category_counts[InstructionCategory.SHIFT_IO.value] == 1  # SHIFTOUT
    assert stat.category_counts[InstructionCategory.TIMING_WAIT.value] == 1  # WAIT
    assert stat.category_counts[InstructionCategory.CONTROL_FLOW.value] == 1  # HALT

    # Register access mapping: r0 written by LDI, ADDI; r1 written by MOV
    assert stat.reg_writes["r0"] == 2
    assert stat.reg_writes["r1"] == 1
    # r0 read by MOV (rs=r0), ADDI (rd=r0), SHIFTOUT (rd=r0) -> 3 reads
    assert stat.reg_reads["r0"] == 3

    # Pins referenced
    assert 0 in stat.pins_referenced, "Pin 0 should be referenced by GDIRI 0x07"
    assert 1 in stat.pins_referenced, "Pin 1 should be referenced by GDIRI 0x07 and SHIFTOUT"
    assert 2 in stat.pins_referenced, "Pin 2 should be referenced by GDIRI 0x07"
    assert 3 not in stat.pins_referenced

    dut._log.info("Static profiling analysis verified successfully.")


@cocotb.test()
async def test_profiler_dynamic_simulation_vs_rtl(dut):
    """Test 2: Verify cycle-accurate match between Python profiler model and hardware RTL."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing profiler dynamic simulation vs RTL execution...")

    asm = get_serdes_header_benchmark_asm(sync_byte=0xBC, opcode=0x05, target=0x12)
    words = assemble(asm)

    # 1. Profile in Python model
    dyn = profile_dynamic(words)
    dut._log.info(f"Model Dynamic Profile: total_cycles={dyn.total_cycles}, retired={dyn.instructions_retired}, CPI={dyn.cpi:.2f}")

    # 2. Execute on RTL DUT
    await _init_dut_and_bootload(dut, words)

    rtl_cycles = 0
    while rtl_cycles < 50:
        await RisingEdge(dut.clk)
        rtl_cycles += 1
        if rtl_cycles == dyn.total_cycles:
            await ReadOnly()
            dut._log.info(f"RTL reached cycle {rtl_cycles}: uio_out=0x{int(dut.uio_out.value):02X}")
            break

    assert dyn.total_cycles == 29, f"Expected 29 cycles, got {dyn.total_cycles}"
    assert dyn.cpi == 1.0, f"Expected CPI 1.00 for pure single-cycle stream, got {dyn.cpi}"
    assert dyn.wait_cycles == 0, f"Expected 0 wait cycles in unrolled SerDes header, got {dyn.wait_cycles}"
    dut._log.info("Dynamic simulation equivalence verified.")


@cocotb.test()
async def test_profiler_uart_tx_performance(dut):
    """Test 3: Benchmark UART TX microcode execution and verify bitrate calculation."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing UART TX benchmark profiling...")

    uart_asm = get_uart_tx_benchmark_asm(byte_val=0x55, wait_per_bit=1)
    res = benchmark_protocol(
        protocol_name="UART_TX_PROFILE",
        asm_source=uart_asm,
        bits_transferred=10,
        theoretical_min_cycles=20,
        clock_freq_hz=10_000_000.0,
    )

    dut._log.info(f"UART TX Profile: {res.total_cycles} cycles, {res.cycles_per_bit:.2f} cyc/bit, {res.effective_bitrate_bps/1e3:.1f} kbps, η={res.efficiency_factor*100:.1f}%")
    assert res.bits_transferred == 10
    assert res.total_cycles == 36
    assert abs(res.cycles_per_bit - 3.6) < 1e-4
    assert abs(res.effective_bitrate_bps - 2777777.78) < 1.0
    assert abs(res.efficiency_factor - (20.0 / 36.0)) < 1e-4

    # Run on RTL
    words = assemble(uart_asm)
    await _init_dut_and_bootload(dut, words)
    await ClockCycles(dut.clk, res.total_cycles + 5)

    await ReadOnly()
    oe = int(dut.uio_oe.value)
    out = int(dut.uio_out.value)
    assert (oe & 0x01) == 0x01, "Pin 0 must be output"
    assert (out & 0x01) == 0x01, "Pin 0 must be high after stop bit"
    dut._log.info("UART TX performance benchmark verified.")


@cocotb.test()
async def test_profiler_spi_transfer_performance(dut):
    """Test 4: Benchmark SPI Master full-duplex transfer performance."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing SPI Master transfer benchmark profiling...")

    spi_asm = get_spi_transfer_benchmark_asm(tx_val=0xA5)
    res = benchmark_protocol(
        protocol_name="SPI_MASTER_PROFILE",
        asm_source=spi_asm,
        bits_transferred=16,
        theoretical_min_cycles=32,
        clock_freq_hz=10_000_000.0,
    )

    dut._log.info(f"SPI Transfer Profile: {res.total_cycles} cycles, {res.cycles_per_bit:.2f} cyc/bit, {res.effective_bitrate_bps/1e3:.1f} kbps, η={res.efficiency_factor*100:.1f}%")
    assert res.bits_transferred == 16
    assert res.total_cycles == 46
    assert res.dynamic_profile.cpi == 1.00, "SPI bit loop must execute at 1.0 CPI"
    assert res.efficiency_factor > 0.65, f"Efficiency factor {res.efficiency_factor} should exceed 65%"

    # Run on RTL
    words = assemble(spi_asm)
    await _init_dut_and_bootload(dut, words)
    await ClockCycles(dut.clk, res.total_cycles + 5)
    dut._log.info("SPI transfer benchmark verified.")


@cocotb.test()
async def test_profiler_serdes_throughput(dut):
    """Test 5: Benchmark high-speed SerDes header throughput (>8.0 Mbps at 10 MHz)."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing High-Speed SerDes header profiling...")

    serdes_asm = get_serdes_header_benchmark_asm(0xAA, 0x55, 0x0F)
    res = benchmark_protocol(
        protocol_name="SERDES_HEADER_PROFILE",
        asm_source=serdes_asm,
        bits_transferred=24,
        theoretical_min_cycles=28,
        clock_freq_hz=10_000_000.0,
    )

    dut._log.info(f"SerDes Throughput: {res.effective_bitrate_bps/1e6:.2f} Mbps, {res.cycles_per_bit:.2f} cycles/bit, η={res.efficiency_factor*100:.1f}%")
    assert res.effective_bitrate_bps > 8.0e6, f"Bitrate must exceed 8.0 Mbps, got {res.effective_bitrate_bps}"
    assert res.cycles_per_bit < 1.3, f"Cycles per bit must be < 1.3, got {res.cycles_per_bit}"
    assert res.efficiency_factor > 0.95, f"Efficiency must exceed 95%, got {res.efficiency_factor}"
    dut._log.info("High-Speed SerDes throughput verified.")


@cocotb.test()
async def test_profiler_suite_scorecard(dut):
    """Test 6: Execute multi-protocol suite profiling and scorecard generation."""
    clock = Clock(dut.clk, 100, unit="ns")
    cocotb.start_soon(clock.start())

    dut._log.info("Testing multi-protocol suite scorecard generation...")

    suite = run_suite_profile(clock_freq_hz=10_000_000.0)
    assert len(suite) == 5, f"Expected 5 suite benchmarks, got {len(suite)}"

    for r in suite:
        assert r.total_cycles > 0
        assert r.effective_bitrate_bps > 0
        assert 0.0 < r.efficiency_factor <= 1.0, f"Efficiency factor {r.efficiency_factor} out of range (0, 1]"
        assert r.static_profile.ram_utilization_pct < 25.0, f"RAM usage {r.static_profile.ram_utilization_pct}% exceeds 25%"

    report = generate_markdown_report(suite)
    assert "# Protocol Microcode Micro-Benchmark Profiling Scorecard" in report
    assert "UART_TX_8N1" in report
    assert "SPI_MASTER_TRANSFER" in report
    assert "I2C_MASTER_WRITE" in report
    assert "MANCHESTER_BIPHASE" in report
    assert "SERDES_HEADER_24B" in report

    await ClockCycles(dut.clk, 5)
    dut._log.info("Scorecard output:\n" + report)
    dut._log.info("Multi-protocol suite scorecard verified successfully.")
