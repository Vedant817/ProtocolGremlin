"""
tools/spi_model.py - Parameterized SPI Master Firmware Generator and Independent SPI Slave Model

Supports all 4 SPI Modes:
  Mode 0: CPOL=0, CPHA=0 (Clock idle low, sample on rising edge, shift on falling)
  Mode 1: CPOL=0, CPHA=1 (Clock idle low, sample on falling edge, shift on rising)
  Mode 2: CPOL=1, CPHA=0 (Clock idle high, sample on falling edge, shift on rising)
  Mode 3: CPOL=1, CPHA=1 (Clock idle high, sample on rising edge, shift on falling)
"""

from typing import List, Tuple, Optional


def build_spi_master_asm(
    tx_byte: int,
    mode: int = 0,
    half_period: int = 4,
    cs_pin: int = 3,
    sclk_pin: int = 4,
    mosi_pin: int = 5,
    miso_pin: int = 6
) -> str:
    """
    Generate assembly to transmit tx_byte via SPI Master in the given mode,
    while simultaneously sampling MISO into R1 (full-duplex).
    """
    cpol = 1 if mode in (2, 3) else 0
    cpha = 1 if mode in (1, 3) else 0

    dir_mask = (1 << cs_pin) | (1 << sclk_pin) | (1 << mosi_pin)
    init_out = (1 << cs_pin) | (cpol << sclk_pin)

    lines: List[str] = [
        "; -------------------------------------------------------------",
        f"; SPI Master Firmware: Mode {mode} (CPOL={cpol}, CPHA={cpha}), T_half={half_period}",
        f"; Pins: CS_N={cs_pin}, SCLK={sclk_pin}, MOSI={mosi_pin}, MISO={miso_pin}",
        "; -------------------------------------------------------------",
        f"    GDIRI 0x{dir_mask:02X}     ; Set CS, SCLK, MOSI as outputs, MISO as input",
        f"    GWRI  0x{init_out:02X}     ; CS_N=1, SCLK=CPOL, MOSI=0",
        f"    LDI   R0, 0x{tx_byte & 0xFF:02X} ; Byte to transmit",
        "    LDI   R1, 0x00       ; Clear RX buffer",
        "    LDI   R2, 0xFF       ; Constant 1s for pin set",
        "    LDI   R3, 0x00       ; Constant 0s for pin clear",
        "",
        "; Assert CS_N low",
        f"    SHIFTOUT R3, {cs_pin}   ; Drive CS_N low",
    ]

    wait_half = max(0, half_period - 2)

    for bit_idx in range(8):
        lines.append(f"; --- Bit {7 - bit_idx} ---")
        if mode == 0:
            # Mode 0: MOSI changes while SCLK=0, sampled on SCLK rising edge
            lines.append(f"    SHIFTOUT R0, {mosi_pin}, MSB ; Drive MOSI bit")
            if wait_half > 0:
                lines.append(f"    WAIT 0x{wait_half:02X}")
            lines.append("    LDI R2, 0xFF")
            lines.append(f"    SHIFTOUT R2, {sclk_pin}      ; SCLK -> 1")
            lines.append(f"    SHIFTIN  R1, {miso_pin}, MSB ; Sample MISO")
            if wait_half > 0:
                lines.append(f"    WAIT 0x{wait_half:02X}")
            lines.append("    LDI R3, 0x00")
            lines.append(f"    SHIFTOUT R3, {sclk_pin}      ; SCLK -> 0")

        elif mode == 1:
            # Mode 1: SCLK rises, MOSI changes, sampled on SCLK falling edge
            lines.append("    LDI R2, 0xFF")
            lines.append(f"    SHIFTOUT R2, {sclk_pin}      ; SCLK -> 1")
            lines.append(f"    SHIFTOUT R0, {mosi_pin}, MSB ; Drive MOSI bit")
            if wait_half > 0:
                lines.append(f"    WAIT 0x{wait_half:02X}")
            lines.append("    LDI R3, 0x00")
            lines.append(f"    SHIFTOUT R3, {sclk_pin}      ; SCLK -> 0")
            lines.append(f"    SHIFTIN  R1, {miso_pin}, MSB ; Sample MISO")
            if wait_half > 0:
                lines.append(f"    WAIT 0x{wait_half:02X}")

        elif mode == 2:
            # Mode 2: CPOL=1, CPHA=0: MOSI changes while SCLK=1, sampled on falling edge
            lines.append(f"    SHIFTOUT R0, {mosi_pin}, MSB ; Drive MOSI bit")
            if wait_half > 0:
                lines.append(f"    WAIT 0x{wait_half:02X}")
            lines.append("    LDI R3, 0x00")
            lines.append(f"    SHIFTOUT R3, {sclk_pin}      ; SCLK -> 0")
            lines.append(f"    SHIFTIN  R1, {miso_pin}, MSB ; Sample MISO")
            if wait_half > 0:
                lines.append(f"    WAIT 0x{wait_half:02X}")
            lines.append("    LDI R2, 0xFF")
            lines.append(f"    SHIFTOUT R2, {sclk_pin}      ; SCLK -> 1")

        elif mode == 3:
            # Mode 3: CPOL=1, CPHA=1: SCLK falls, MOSI changes, sampled on rising edge
            lines.append("    LDI R3, 0x00")
            lines.append(f"    SHIFTOUT R3, {sclk_pin}      ; SCLK -> 0")
            lines.append(f"    SHIFTOUT R0, {mosi_pin}, MSB ; Drive MOSI bit")
            if wait_half > 0:
                lines.append(f"    WAIT 0x{wait_half:02X}")
            lines.append("    LDI R2, 0xFF")
            lines.append(f"    SHIFTOUT R2, {sclk_pin}      ; SCLK -> 1")
            lines.append(f"    SHIFTIN  R1, {miso_pin}, MSB ; Sample MISO")
            if wait_half > 0:
                lines.append(f"    WAIT 0x{wait_half:02X}")

    lines.extend([
        "",
        "; Deassert CS_N high",
        "    LDI   R2, 0xFF",
        f"    SHIFTOUT R2, {cs_pin}   ; CS_N -> 1",
        "    HALT",
    ])

    return "\n".join(lines) + "\n"


class SpiSlave:
    """
    Independent software SPI Slave state machine.
    Monitors bus pins, samples MOSI on active clock edges according to mode,
    and shifts MISO out on inactive clock edges.
    """

    def __init__(
        self,
        mode: int = 0,
        tx_byte: int = 0x00,
        cs_pin: int = 3,
        sclk_pin: int = 4,
        mosi_pin: int = 5,
        miso_pin: int = 6
    ):
        self.mode = mode
        self.cpol = 1 if mode in (2, 3) else 0
        self.cpha = 1 if mode in (1, 3) else 0
        self.cs_pin = cs_pin
        self.sclk_pin = sclk_pin
        self.mosi_pin = mosi_pin
        self.miso_pin = miso_pin

        self.tx_shift = tx_byte & 0xFF
        self.rx_shift = 0
        self.bits_received = 0
        self.rx_bytes: List[int] = []

        self.prev_sclk = self.cpol
        self.prev_cs_n = 1
        self.current_miso = (self.tx_shift >> 7) & 1

    def step(self, uio_out: int) -> int:
        """
        Advance one clock cycle. Takes the DUT's uio_out bus, returns MISO bit (0 or 1)
        to drive on DUT's uio_in.
        """
        cs_n = (uio_out >> self.cs_pin) & 1
        sclk = (uio_out >> self.sclk_pin) & 1
        mosi = (uio_out >> self.mosi_pin) & 1

        if cs_n == 0 and self.prev_cs_n == 1:
            # Chip selected: initialize shift register
            self.rx_shift = 0
            self.bits_received = 0
            # For CPHA=0, drive bit 7 immediately on CS assertion
            if self.cpha == 0:
                self.current_miso = (self.tx_shift >> 7) & 1

        elif cs_n == 1:
            # Inactive
            self.prev_sclk = sclk
            self.prev_cs_n = cs_n
            return 0

        # Detect SCLK edges
        sclk_rise = (sclk == 1 and self.prev_sclk == 0)
        sclk_fall = (sclk == 0 and self.prev_sclk == 1)

        # Determine sampling and shift edges
        # Mode 0: sample rise, shift fall
        # Mode 1: sample fall, shift rise
        # Mode 2: sample fall, shift rise
        # Mode 3: sample rise, shift fall
        sample_edge = sclk_rise if self.mode in (0, 3) else sclk_fall
        shift_edge = sclk_fall if self.mode in (0, 3) else sclk_rise

        if sample_edge and cs_n == 0:
            self.rx_shift = ((self.rx_shift << 1) & 0xFF) | mosi
            self.bits_received += 1
            if self.bits_received == 8:
                self.rx_bytes.append(self.rx_shift)
                self.rx_shift = 0
                self.bits_received = 0

        if shift_edge and cs_n == 0:
            self.tx_shift = (self.tx_shift << 1) & 0xFF
            self.current_miso = (self.tx_shift >> 7) & 1

        self.prev_sclk = sclk
        self.prev_cs_n = cs_n
        return self.current_miso
