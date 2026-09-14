# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Independent Dallas 1-Wire reference model and firmware generator.

This module provides:
1. OneWireSlave: An independent cycle-accurate emulation of a Dallas DS18B20
   1-Wire slave device (reset detection, presence pulse generation, read/write
   timeslot handling).
2. build_onewire_reset_presence_asm: Generates firmware that drives the master
   reset pulse, releases the open-drain bus, and measures the slave presence pulse
   using WAITEDGE with single-cycle precision.
3. build_onewire_read_byte_asm: Generates firmware that issues 8 read timeslots
   and samples incoming data into R0 using SHIFTIN.
4. build_onewire_write_byte_asm: Generates firmware that serializes an 8-bit
   command or data byte using open-drain timeslots.
"""
from __future__ import annotations


class OneWireSlave:
    """Independent cycle-by-cycle model of a 1-Wire slave (e.g., Dallas DS18B20).

    Simulates open-drain bus interaction with pull-up resistor:
    - Senses master low pulses.
    - Emits presence pulse with configurable delay (presence_delay) and duration (presence_duration).
    - Emits bit data during read timeslots (low for bit 0, float high for bit 1).
    - Samples bit data during write timeslots at mid-slot.
    """

    def __init__(
        self,
        reset_threshold: int = 25,
        presence_delay: int = 8,
        presence_duration: int = 20,
        tx_bytes: list[int] | None = None,
    ):
        self.reset_threshold = reset_threshold
        self.presence_delay = presence_delay
        self.presence_duration = presence_duration
        self.tx_bytes = list(tx_bytes) if tx_bytes else []
        self.rx_bytes: list[int] = []
        self.reset()

    def reset(self) -> None:
        self.low_cycles = 0
        self.prev_bus_low = False
        self.presence_cnt = -1
        self.presence_duration_cnt = 0
        self.slave_drive_low = False
        self.current_tx_byte_idx = 0
        self.current_tx_bit_idx = 0
        self.slot_low_cnt = 0
        # Write reception state
        self.rx_byte_accum = 0
        self.rx_bit_idx = 0
        self.rx_slot_timer = 0

    def step(self, master_drive_low: bool) -> bool:
        """Step slave by one cycle given master's open-drain drive state.

        Args:
            master_drive_low: True if master is actively pulling line low, False if released.

        Returns:
            True if slave is actively pulling line low, False if released.
        """
        bus_low = master_drive_low or self.slave_drive_low

        # Detect reset pulse (master holds low >= reset_threshold cycles)
        if bus_low:
            self.low_cycles += 1
        else:
            if self.low_cycles >= self.reset_threshold:
                # Master released bus after reset: schedule presence pulse after presence_delay
                self.presence_cnt = self.presence_delay
                self.current_tx_byte_idx = 0
                self.current_tx_bit_idx = 0
            self.low_cycles = 0

        # Handle presence sequence
        if self.presence_cnt > 0:
            self.presence_cnt -= 1
            if self.presence_cnt == 0:
                self.slave_drive_low = True
                self.presence_duration_cnt = self.presence_duration
        elif self.presence_duration_cnt > 0:
            self.presence_duration_cnt -= 1
            if self.presence_duration_cnt == 0:
                self.slave_drive_low = False

        # Detect falling edge to initiate read/write timeslot
        if not self.prev_bus_low and bus_low:
            if self.presence_cnt <= 0 and self.presence_duration_cnt <= 0 and self.low_cycles < self.reset_threshold:
                # Timeslot initiated
                self.rx_slot_timer = 1
                if self.current_tx_byte_idx < len(self.tx_bytes):
                    cur_byte = self.tx_bytes[self.current_tx_byte_idx]
                    bit = (cur_byte >> self.current_tx_bit_idx) & 1
                    if bit == 0:
                        self.slave_drive_low = True
                        self.slot_low_cnt = 15
                    else:
                        self.slave_drive_low = False
                        self.slot_low_cnt = 0
                    self.current_tx_bit_idx += 1
                    if self.current_tx_bit_idx == 8:
                        self.current_tx_bit_idx = 0
                        self.current_tx_byte_idx += 1

        elif self.slot_low_cnt > 0:
            self.slot_low_cnt -= 1
            if self.slot_low_cnt == 0:
                self.slave_drive_low = False

        # Sampling incoming write timeslot at cycle 10
        if self.rx_slot_timer > 0:
            self.rx_slot_timer += 1
            if self.rx_slot_timer == 10:
                sampled_bit = 0 if bus_low else 1
                self.rx_byte_accum |= (sampled_bit << self.rx_bit_idx)
                self.rx_bit_idx += 1
                if self.rx_bit_idx == 8:
                    self.rx_bytes.append(self.rx_byte_accum)
                    self.rx_byte_accum = 0
                    self.rx_bit_idx = 0

        self.prev_bus_low = bus_low
        return self.slave_drive_low


def build_onewire_reset_presence_asm(
    pin: int = 3,
    reset_low_cycles: int = 40,
    min_presence_cycles: int = 15,
    max_presence_cycles: int = 35,
) -> str:
    """Generate 1-Wire Master Reset and Presence Detect firmware.

    Sequence:
    1. Configure pin for hardware open-drain (GWRI high-Z, GDIRI out, GODRI open-drain).
    2. Pull line low for reset_low_cycles to issue reset pulse.
    3. Release line to high-Z.
    4. WAITEDGE stalls until slave pulls line low (falling edge).
    5. WAITEDGE stalls until slave releases line (rising edge), capturing
       elapsed presence pulse width directly into R1!
    6. Halt. R1 contains the exact measured presence duration in clock cycles.
    """
    pin_mask = 1 << pin
    wait_reset = reset_low_cycles - 2
    edge_fall = pin
    edge_rise = (1 << 3) | pin

    lines = [
        f"; 1-Wire Master Reset & Presence Pulse Discovery on pin {pin}",
        f"GWRI  0x{pin_mask:02X}",       # Set output latch to 1 first (avoid glitch)
        f"GDIRI 0x{pin_mask:02X}",       # Pin is output
        f"GODRI 0x{pin_mask:02X}",       # Pin is open-drain
        "LDI   R2, 0x00",              # Status OK default
        "; --- Step 1: Master Reset Pulse (pull low) ---",
        "GWRI  0x00",
        f"WAIT  {wait_reset}",
        "; --- Step 2: Release Bus (pull-up takes line high) ---",
        f"GWRI  0x{pin_mask:02X}",
        "; --- Step 3: Wait for Slave Presence Falling Edge ---",
        f"WAITEDGE R3, 0x{edge_fall:02X}",
        "; --- Step 4: Capture Slave Presence Pulse Width on Rising Edge ---",
        f"WAITEDGE R1, 0x{edge_rise:02X}",
        "HALT",
        "",
    ]
    return "\n".join(lines)


def build_onewire_read_byte_asm(
    pin: int = 3,
    slot_total_cycles: int = 30,
) -> str:
    """Generate 1-Wire Master 8-bit Read firmware.

    For each bit (0 to 7, LSB first):
    1. Master pulls low for 2 cycles to initiate read timeslot.
    2. Master releases line (open-drain high-Z).
    3. Master waits for synchronizer propagation (2 cycles).
    4. SHIFTIN R0, pin (LSB first) samples the line into R0.
    5. Master waits remainder of timeslot + recovery time.
    6. Repeated 8 times to receive full 8-bit byte in R0.
    """
    pin_mask = 1 << pin
    recovery_delay = slot_total_cycles - 8

    lines = [
        f"; 1-Wire Master Read 1 Byte on pin {pin}",
        f"GWRI  0x{pin_mask:02X}",
        f"GDIRI 0x{pin_mask:02X}",
        f"GODRI 0x{pin_mask:02X}",
        "WAIT  5",
    ]

    for bit_i in range(8):
        lines.extend([
            f"; --- Read Bit {bit_i} Timeslot ---",
            "GWRI  0x00",              # Pull low to start slot (cycle 0)
            "WAIT  1",                  # Hold low (cycle 1)
            f"GWRI  0x{pin_mask:02X}",  # Release line (cycle 2)
            "WAIT  2",                  # Settle & sync (cycles 3-5)
            f"SHIFTIN R0, {pin}",      # Sample line into R0 (cycle 6)
            f"WAIT  {recovery_delay}", # Remainder of timeslot (cycles 7-29)
        ])

    lines.extend([
        "HALT",
        "",
    ])
    return "\n".join(lines)


def build_onewire_write_byte_asm(
    byte_val: int,
    pin: int = 3,
    slot_total_cycles: int = 30,
) -> str:
    """Generate 1-Wire Master 8-bit Write firmware.

    Writes 8 bits LSB-first.
    For each bit:
    - Bit 1: pull low for 2 cycles, release for slot_total_cycles - 4.
    - Bit 0: pull low for 20 cycles, release for 7 cycles recovery.
    """
    pin_mask = 1 << pin
    lines = [
        f"; 1-Wire Master Write Byte 0x{byte_val:02X} on pin {pin}",
        f"GWRI  0x{pin_mask:02X}",
        f"GDIRI 0x{pin_mask:02X}",
        f"GODRI 0x{pin_mask:02X}",
        "WAIT  5",
    ]

    for bit_i in range(8):
        bit = (byte_val >> bit_i) & 1
        lines.append(f"; --- Write Bit {bit_i} (value={bit}) ---")
        if bit == 1:
            lines.extend([
                "GWRI  0x00",
                "WAIT  1",
                f"GWRI  0x{pin_mask:02X}",
                f"WAIT  {slot_total_cycles - 4}",
            ])
        else:
            lines.extend([
                "GWRI  0x00",
                f"WAIT  {slot_total_cycles - 10}",
                f"GWRI  0x{pin_mask:02X}",
                "WAIT  7",
            ])

    lines.extend([
        "HALT",
        "",
    ])
    return "\n".join(lines)
