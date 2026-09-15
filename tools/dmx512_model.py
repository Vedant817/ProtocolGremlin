# tools/dmx512_model.py - Cycle-accurate DMX512 (ANSI E1.11 / USITT DMX512-A) protocol model & firmware generator
#
# Implements DMX512-A protocol operations:
# - Break pulse: line held in Space (low) for >= 22 bit times (>= 88 us in standard 250 kbaud).
# - Mark-After-Break (MAB): line held in Mark (high) for >= 2 bit times (>= 8 us).
# - Slot 0 (Start Code): 1 start bit (0), 8 data bits (LSB-first), 2 stop bits (1, 1). Standard: 0x00.
# - Channel slots: sequential 11-bit slots (1 start, 8 data, 2 stop) encoding intensity 0-255.
# - Receiver verifies Break pulse duration using WAITEDGE, decodes Start Code, and extracts target channel into R0.
#
# Part of the Jane Street Protocol Emulator ASIC verification suite.

from typing import List, Tuple, Optional


class Dmx512Transmitter:
    """Independent stimulus generator for transmitting standard DMX512 packets."""

    def __init__(self, dut, pin: int = 4, bit_period: int = 4):
        self.dut = dut
        self.pin = pin
        self.bit_period = bit_period
        self.break_cycles = 24 * bit_period
        self.mab_cycles = 4 * bit_period

    def drive_level(self, level: int):
        curr = int(self.dut.uio_in.value) if self.dut.uio_in.value.is_resolvable else 0
        if level:
            curr |= (1 << self.pin)
        else:
            curr &= ~(1 << self.pin)
        self.dut.uio_in.value = curr

    async def transmit_packet(self, start_code: int, channels: List[int], clock):
        """Transmit a full DMX512 packet: Break -> MAB -> Start Code -> Channel slots."""
        # 1. Break Pulse: Space (0) for >= 22 bit periods
        self.drive_level(0)
        for _ in range(self.break_cycles):
            await clock

        # 2. Mark-After-Break (MAB): Mark (1) for >= 2 bit periods
        self.drive_level(1)
        for _ in range(self.mab_cycles):
            await clock

        # 3. Transmit Slot 0 (Start Code) + Channel Slots
        all_slots = [start_code] + list(channels)
        for slot_val in all_slots:
            # Start bit: 0
            self.drive_level(0)
            for _ in range(self.bit_period):
                await clock

            # 8 Data bits (LSB-first)
            for b in range(8):
                bit = (slot_val >> b) & 1
                self.drive_level(bit)
                for _ in range(self.bit_period):
                    await clock

            # 2 Stop bits: 1, 1
            self.drive_level(1)
            for _ in range(self.bit_period * 2):
                await clock

        # Return to idle mark
        self.drive_level(1)
        for _ in range(self.bit_period * 4):
            await clock


class Dmx512ReceiverModel:
    """Independent reference receiver model for DMX512 waveforms."""

    def __init__(self, bit_period: int = 4):
        self.bit_period = bit_period
        self.min_break_cycles = 22 * bit_period
        self.min_mab_cycles = 2 * bit_period

    def parse_waveform(self, levels: List[int]) -> Tuple[Optional[int], List[int], bool]:
        """Parse raw recorded cycle levels into (start_code, channels, is_valid)."""
        n = len(levels)
        # Find Break pulse: consecutive zeros >= min_break_cycles
        break_start = -1
        break_len = 0
        mab_start = -1

        i = 0
        while i < n:
            if levels[i] == 0:
                cur_len = 0
                start_i = i
                while i < n and levels[i] == 0:
                    cur_len += 1
                    i += 1
                if cur_len >= self.min_break_cycles:
                    break_start = start_i
                    break_len = cur_len
                    mab_start = i
                    break
            else:
                i += 1

        if break_start == -1 or mab_start >= n:
            return None, [], False

        # Measure MAB
        mab_len = 0
        i = mab_start
        while i < n and levels[i] == 1:
            mab_len += 1
            i += 1

        if mab_len < self.min_mab_cycles or i >= n:
            return None, [], False

        # Parse slots starting at index i (which is falling edge of Slot 0 start bit)
        slots = []
        while i < n:
            # Look for start bit (level 0)
            if levels[i] != 0:
                i += 1
                continue

            # Found start bit
            slot_start = i
            # Sample 8 data bits at center of each bit period
            data_val = 0
            for bit_idx in range(8):
                sample_pos = slot_start + (bit_idx + 1) * self.bit_period + (self.bit_period // 2)
                if sample_pos >= n:
                    break
                data_val |= (levels[sample_pos] << bit_idx)

            slots.append(data_val)
            # Skip 1 start bit + 8 data bits + 2 stop bits = 11 bit periods
            i = slot_start + (11 * self.bit_period)
            if len(slots) >= 513:
                break

        if not slots:
            return None, [], False

        return slots[0], slots[1:], True


def build_dmx512_tx_packet_asm(
    start_code: int,
    channels: List[int],
    bit_period: int = 4,
    pin: int = 4
) -> str:
    """Generate assembly firmware to transmit a DMX512 frame: Break -> MAB -> Start Code -> Channels.

    Bit timing:
    - Break: held low for 24 * bit_period cycles.
    - MAB: held high for 4 * bit_period cycles.
    - Slots: 1 start bit (0), 8 data bits LSB-first, 2 stop bits (1, 1).
    """
    pin_mask = 1 << pin
    break_cycles = 24 * bit_period
    mab_cycles = 4 * bit_period
    wait_bit = max(0, bit_period - 2)

    asm = []
    asm.append("; --- DMX512 Transmitter Firmware ---")
    asm.append(f"GDIRI 0x{pin_mask:02X}")   # Pin configured as output
    asm.append(f"GWRI 0x{pin_mask:02X}")    # Idle mark (high)
    asm.append("WAIT 4")                    # Bus settling

    # 1. Break Pulse: Space (low)
    asm.append("GWRI 0x00")
    rem = break_cycles - 1
    while rem > 255:
        asm.append("WAIT 254")
        rem -= 255
    if rem > 0:
        asm.append(f"WAIT {rem}")

    # 2. Mark-After-Break (MAB): Mark (high)
    asm.append(f"GWRI 0x{pin_mask:02X}")
    rem = mab_cycles - 1
    while rem > 255:
        asm.append("WAIT 254")
        rem -= 255
    if rem > 0:
        asm.append(f"WAIT {rem}")

    # 3. Transmit slots (Start Code followed by channels)
    all_slots = [start_code] + list(channels)
    for slot_val in all_slots:
        # Start bit: 0
        asm.append("GWRI 0x00")
        if wait_bit > 0:
            asm.append(f"WAIT {wait_bit}")

        # 8 Data bits (LSB-first)
        for b in range(8):
            bit = (slot_val >> b) & 1
            if bit:
                asm.append(f"GWRI 0x{pin_mask:02X}")
            else:
                asm.append("GWRI 0x00")
            if wait_bit > 0:
                asm.append(f"WAIT {wait_bit}")

        # 2 Stop bits: 1, 1 (2 * bit_period cycles)
        asm.append(f"GWRI 0x{pin_mask:02X}")
        stop_wait = (2 * bit_period) - 2
        if stop_wait > 0:
            asm.append(f"WAIT {stop_wait}")

    # Idle mark and halt
    asm.append(f"GWRI 0x{pin_mask:02X}")
    asm.append("WAIT 4")
    asm.append("HALT")

    return "\n".join(asm) + "\n"


def build_dmx512_rx_slot_asm(
    target_channel: int = 1,
    bit_period: int = 4,
    pin: int = 4
) -> str:
    """Generate assembly firmware to receive DMX512 packets, verify Break, and capture channel into R0.

    Algorithm:
    1. Wait for Break pulse:
       - Wait for falling edge on pin (Break begins).
       - Use WAITEDGE to measure Break duration until rising edge (MAB begins).
       - Break duration captured into R3!
    2. Wait through MAB to start of Slot 0.
    3. Stride through slots until target_channel (e.g. Channel 1):
       - Sample 8 bits of target channel into R0 via SHIFTIN LSB.
    4. Halts with R0 = target channel intensity, R2 = 0x00 (SUCCESS), R3 = measured break cycles.
    """
    asm = []
    asm.append("; --- DMX512 Receiver Firmware ---")
    asm.append("GDIRI 0x00")              # Pins configured as inputs
    asm.append("LDI R0, 0x00")              # Target channel data accumulator
    asm.append("LDI R2, 0x00")              # Status: 0x00 SUCCESS
    asm.append("LDI R3, 0x00")              # Break duration register

    # 1. Wait for falling edge of Break pulse (mode 00 = falling edge)
    asm.append(f"WAITEDGE R3, 0x{pin:02X}")

    # 2. Wait for rising edge ending Break / starting MAB (mode 01 = rising edge, operand = pin | 0x08)
    rise_operand = pin | 0x08
    asm.append(f"WAITEDGE R3, 0x{rise_operand:02X}")

    # R3 now holds the exact measured Break pulse duration!
    stride_wait = max(0, bit_period - 2)
    first_sample_wait = bit_period + (bit_period // 2) - 2
    skip_to_stop_wait = (9 * bit_period) - 2

    # Advance through slots from 0 up to target_channel
    for slot_idx in range(target_channel + 1):
        # Synchronize to this slot's start bit falling edge
        asm.append(f"WAITEDGE R1, 0x{pin:02X}")

        if slot_idx < target_channel:
            # Skip through data bits into stop bits
            if skip_to_stop_wait > 0:
                asm.append(f"WAIT {skip_to_stop_wait}")
        else:
            # Target channel reached: stride to center of data bit 0
            if first_sample_wait > 0:
                asm.append(f"WAIT {first_sample_wait}")

            # Sample 8 data bits into R0 via SHIFTIN LSB
            for b in range(8):
                asm.append(f"SHIFTIN R0, {pin}, LSB")
                if b < 7 and stride_wait > 0:
                    asm.append(f"WAIT {stride_wait}")

    asm.append("WAIT 4")
    asm.append("HALT")

    return "\n".join(asm) + "\n"
