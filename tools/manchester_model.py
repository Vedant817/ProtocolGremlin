# tools/manchester_model.py - Cycle-accurate Manchester Biphase-L encoder, decoder & firmware generator
#
# Implements IEEE 802.3 / MIL-STD-1553 Manchester Biphase-L line encoding:
# - Self-clocking binary line code: mid-bit transition in every bit cell.
# - IEEE 802.3 convention:
#     Logic '0': Low-to-High transition at mid-bit (0 in first half, 1 in second half).
#     Logic '1': High-to-Low transition at mid-bit (1 in first half, 0 in second half).
# - Mid-bit transition provides continuous clock and data recovery (CDR).
# - Biphase violation detection: line failing to invert between halves is flagged as an error.
#
# Part of the Jane Street Protocol Emulator ASIC verification suite.

from typing import List, Tuple, Optional


def encode_manchester_bits(byte_val: int, msb_first: bool = True) -> List[int]:
    """Encode an 8-bit byte into 16 Manchester half-bit symbols.
    Each bit produces 2 half-bit levels:
    '0' -> [0, 1]
    '1' -> [1, 0]
    """
    symbols = []
    bit_indices = range(7, -1, -1) if msb_first else range(8)
    for i in bit_indices:
        bit = (byte_val >> i) & 1
        if bit == 0:
            symbols.extend([0, 1])
        else:
            symbols.extend([1, 0])
    return symbols


class ManchesterDecoder:
    """Independent cycle-accurate Python reference decoder for Manchester waveforms.

    Tracks transitions and decodes bit values using mid-bit edge detection.
    """

    def __init__(self, half_period: int):
        self.half_period = half_period
        self.bit_period = half_period * 2
        self.decoded_bits: List[int] = []
        self.phase_violations = 0

    def decode_symbols(self, symbols: List[int]) -> Tuple[int, bool]:
        """Decode pairs of half-bit symbols into an 8-bit byte.
        Returns (byte_val, is_valid).
        """
        if len(symbols) < 16:
            return 0, False

        byte_val = 0
        valid = True
        for bit_idx in range(8):
            first_half = symbols[bit_idx * 2]
            second_half = symbols[bit_idx * 2 + 1]

            if first_half == second_half:
                # Biphase violation: no transition in bit cell
                valid = False
                self.phase_violations += 1
                continue

            # In IEEE 802.3: first=0, second=1 is bit 0; first=1, second=0 is bit 1
            bit = 1 if (first_half == 1 and second_half == 0) else 0
            byte_val = (byte_val << 1) | bit

        return byte_val, valid


class ManchesterTransmitter:
    """Independent stimulus generator for testing the ASIC Manchester receiver."""

    def __init__(self, dut, pin: int = 4, half_period: int = 8):
        self.dut = dut
        self.pin = pin
        self.half_period = half_period
        self.inject_violation_at_bit: Optional[int] = None

    def drive_level(self, level: int):
        curr = int(self.dut.uio_in.value) if self.dut.uio_in.value.is_resolvable else 0
        if level:
            curr |= (1 << self.pin)
        else:
            curr &= ~(1 << self.pin)
        self.dut.uio_in.value = curr

    async def transmit_frame(self, data_byte: int, clock):
        """Transmit a full Manchester frame:
        Preamble start bit '1' ([1, 0]) followed by 8 data bits (MSB-first).
        """
        # Start bit: logic '1' -> first half 1, second half 0
        self.drive_level(1)
        for _ in range(self.half_period):
            await clock

        self.drive_level(0)
        for _ in range(self.half_period):
            await clock

        # 8 data bits (MSB-first)
        for bit_idx in range(7, -1, -1):
            bit = (data_byte >> bit_idx) & 1

            if self.inject_violation_at_bit == bit_idx:
                # Inject biphase violation: hold line at 0 for entire bit cell
                self.drive_level(0)
                for _ in range(self.half_period * 2):
                    await clock
                continue

            if bit == 0:
                # Logic 0: first half 0, second half 1
                self.drive_level(0)
                for _ in range(self.half_period):
                    await clock
                self.drive_level(1)
                for _ in range(self.half_period):
                    await clock
            else:
                # Logic 1: first half 1, second half 0
                self.drive_level(1)
                for _ in range(self.half_period):
                    await clock
                self.drive_level(0)
                for _ in range(self.half_period):
                    await clock

        # Return to idle low
        self.drive_level(0)
        for _ in range(self.half_period):
            await clock


def build_manchester_tx_asm(
    data_byte: int,
    half_period: int = 4,
    pin: int = 4
) -> str:
    """Generate assembly firmware to transmit a byte using Manchester Biphase-L encoding.

    Bit layout: Start bit '1' followed by 8 data bits (MSB-first).
    Pin direction configured as output.
    """
    pin_mask = 1 << pin
    wait_half = max(0, half_period - 2)

    asm = []
    asm.append("; --- Manchester Biphase-L TX Firmware ---")
    asm.append(f"GDIRI 0x{pin_mask:02X}")
    asm.append("GWRI 0x00")  # Idle low
    asm.append("WAIT 4")     # Hold idle low for settling post-bootload

    # Start bit '1': first half 1, second half 0
    asm.append(f"GWRI 0x{pin_mask:02X}")
    if wait_half > 0:
        asm.append(f"WAIT {wait_half}")
    asm.append("GWRI 0x00")
    if wait_half > 0:
        asm.append(f"WAIT {wait_half}")

    # 8 data bits (MSB-first)
    for bit_idx in range(7, -1, -1):
        bit = (data_byte >> bit_idx) & 1
        if bit == 0:
            # Bit 0: 0 then 1
            asm.append("GWRI 0x00")
            if wait_half > 0:
                asm.append(f"WAIT {wait_half}")
            asm.append(f"GWRI 0x{pin_mask:02X}")
            if wait_half > 0:
                asm.append(f"WAIT {wait_half}")
        else:
            # Bit 1: 1 then 0
            asm.append(f"GWRI 0x{pin_mask:02X}")
            if wait_half > 0:
                asm.append(f"WAIT {wait_half}")
            asm.append("GWRI 0x00")
            if wait_half > 0:
                asm.append(f"WAIT {wait_half}")

    # Return to idle low and halt
    asm.append("GWRI 0x00")
    asm.append("WAIT 2")
    asm.append("HALT")
    return "\n".join(asm) + "\n"


def build_manchester_rx_asm(
    half_period: int = 8,
    pin: int = 4
) -> str:
    """Generate assembly firmware to receive and decode a Manchester Biphase-L byte.

    Algorithm:
    1. Synchronize to start bit '1' mid-bit transition (falling edge) via WAITEDGE.
    2. For bit 0: wait 1.5 * half_period to reach middle of bit 0 first half.
    3. For bits 0..7:
       - Sample line via SHIFTIN R0, pin, MSB (directly captures bit and shifts left).
       - Wait half_period to reach middle of second half.
       - Sample line into R1.
       - Wait half_period to reach middle of next bit's first half.
    4. Upon completion, R0 contains the decoded byte, and execution halts.
    """
    asm = []
    asm.append("; --- Manchester Biphase-L RX Firmware ---")
    asm.append("GDIRI 0x00")  # All pins input
    asm.append("LDI R0, 0")   # Initialize accumulator

    # Wait for falling edge of start bit '1' mid-bit transition (mode 00 = falling edge)
    asm.append(f"WAITEDGE R3, 0x{pin:02X}")

    # Synchronizer compensation: 2 cycles delay through synchronizer.
    # From falling edge of start bit (t=0) to center of bit 0 first half is:
    # half_period (end of start bit) + half_period // 2 (center of first half).
    # Since WAITEDGE finishes at t=2 and next instruction begins at t=3:
    # wait_cycles = half_period + half_period // 2 - 2.
    initial_wait = half_period + (half_period // 2) - 2
    asm.append(f"WAIT {initial_wait}")

    # Now we are at the center of bit 0's first half!
    for bit_idx in range(8):
        # 1. Sample first half directly into R0 (MSB mode shifts left and inserts bit)
        asm.append(f"SHIFTIN R0, {pin}, MSB")

        if bit_idx < 7:
            # Stride to next bit's first half (2 * half_period = 16 cycles)
            # SHIFTIN takes 1 cycle.
            # We need 15 more cycles: WAIT 14 (stalls 14+1 = 15 cycles).
            stride_wait = (2 * half_period) - 2
            asm.append(f"WAIT {stride_wait}")

    # Done: R0 contains the 8 decoded bits
    asm.append("WAIT 4")
    asm.append("HALT")
    return "\n".join(asm) + "\n"
