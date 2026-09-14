# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""cocotb helper that drives the ISA v1 serial bootloader protocol described
in docs/isa.md, over the same `uio` bus the protocol engine's GPIO
instructions use. This is what proves the chip is actually reprogrammable
after fabrication (see orchestrator/decisions.md) - it does not use
`$readmemh` anywhere.

Wire assignment during the load phase (all on `uio_in`, host-driven):
    bit 0 - LOAD_REQ  (level; must be 1 throughout an active load)
    bit 1 - LOAD_CLK  (host-driven bit clock; one rising edge per bit)
    bit 2 - LOAD_DATA (serial data, MSB-first within each field)

Frame: 8-bit word count, MSB first, followed by that many 16-bit
instruction words, each MSB first. No checksum in v1 - see
docs/limitations.md.

Timing: the core's GPIO input synchronizer (src/gpio.v) is a 2-flop
synchronizer, so every level/edge change needs to be held for at least 2
clock cycles before the core can reliably observe it. This helper is
deliberately conservative (2 cycles per phase) rather than cutting it close.
"""
from cocotb.triggers import RisingEdge

LOAD_REQ_BIT = 0
LOAD_CLK_BIT = 1
LOAD_DATA_BIT = 2

# Cycles the core's LD_WAIT state spends settling the synchronizer before it
# samples LOAD_REQ (must match src/core.v's ld_settle_cnt threshold + 1).
_SETTLE_EDGES = 3


async def _send_bit(dut, uio_val: int, bit: int) -> int:
    """Send one bit: set LOAD_DATA (clk low), pulse LOAD_CLK high, then low.
    Returns the updated uio_in value (LOAD_REQ preserved)."""
    uio_val = (uio_val & ~(1 << LOAD_DATA_BIT) & ~(1 << LOAD_CLK_BIT) & 0xFF) | (bit << LOAD_DATA_BIT)
    dut.uio_in.value = uio_val
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)

    uio_val |= 1 << LOAD_CLK_BIT
    dut.uio_in.value = uio_val
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)

    uio_val &= ~(1 << LOAD_CLK_BIT) & 0xFF
    dut.uio_in.value = uio_val
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)

    return uio_val


def compute_crc8(words: list[int]) -> int:
    """
    Compute standard CRC-8 (poly 0x07, init 0x00) over the bootloader frame:
    1 byte word count + 2 bytes per instruction word (MSB first).
    """
    crc = 0
    count = len(words) & 0xFF
    for b in range(7, -1, -1):
        bit = (count >> b) & 1
        msb = (crc >> 7) & 1
        crc = (((crc << 1) ^ 0x07) if (msb ^ bit) else (crc << 1)) & 0xFF

    for word in words:
        for b in range(15, -1, -1):
            bit = (word >> b) & 1
            msb = (crc >> 7) & 1
            crc = (((crc << 1) ^ 0x07) if (msb ^ bit) else (crc << 1)) & 0xFF

    return crc


async def bootload(
    dut,
    words: list[int],
    corrupt_crc: bool = False,
    abort_early_after_words: int = -1,
) -> None:
    """Load `words` (a list of 16-bit instruction words) into the core's
    program RAM via the serial bootloader, accompanied by an 8-bit CRC-8 checksum,
    then let it settle into normal execution (PC=0). Must be called right after
    releasing rst_n, before any other clock edges elapse, and before anything
    else drives `uio_in`.
    """
    if not (0 <= len(words) <= 255):
        raise ValueError("bootloader frame supports at most 255 words")

    uio_val = 1 << LOAD_REQ_BIT
    dut.uio_in.value = uio_val

    for _ in range(_SETTLE_EDGES):
        await RisingEdge(dut.clk)

    count = len(words)
    for bit_index in range(7, -1, -1):
        uio_val = await _send_bit(dut, uio_val, (count >> bit_index) & 1)

    for word_idx, word in enumerate(words):
        if abort_early_after_words >= 0 and word_idx >= abort_early_after_words:
            # Drop LOAD_REQ early to simulate truncated frame
            uio_val &= ~(1 << LOAD_REQ_BIT) & 0xFF
            dut.uio_in.value = uio_val
            await RisingEdge(dut.clk)
            await RisingEdge(dut.clk)
            return

        for bit_index in range(15, -1, -1):
            uio_val = await _send_bit(dut, uio_val, (word >> bit_index) & 1)

    # Calculate and send CRC-8 checksum
    crc = compute_crc8(words)
    if corrupt_crc:
        crc ^= 0xFF  # Invert bits to induce CRC checksum error

    for bit_index in range(7, -1, -1):
        uio_val = await _send_bit(dut, uio_val, (crc >> bit_index) & 1)

    uio_val &= ~(1 << LOAD_REQ_BIT) & 0xFF
    dut.uio_in.value = uio_val
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)

