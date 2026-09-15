# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Independent PS/2 Keyboard/Mouse reference model and firmware generator.

This module provides:
1. PS2Device: An independent cycle-accurate emulation of a PS/2 keyboard/mouse
   peripheral operating on open-drain clock and data lines.
   - Generates clock pulses (10-16.7 kHz equivalent in simulation cycle scaling)
   - Transmits 11-bit frames (Start=0, 8 Data LSB-first, Odd Parity, Stop=1)
   - Injects parity or framing errors when commanded for verification
   - Receives host commands with Host Inhibit / Request-to-Send detection
   - Acknowledges host transmissions with ACK bit (12th clock cycle)
2. build_ps2_rx_asm: Generates firmware that synchronizes on falling clock edges,
   deserializes 8 data bits into R0 via SHIFTIN, accumulates odd parity in R1,
   and asserts R2=0xFD on parity error, R2=0xFE on framing error, or R2=0x00 on success.
3. build_ps2_tx_asm: Generates firmware that inhibits clock, asserts Request-To-Send,
   transmits 8 data bits and calculated odd parity, and reads the device ACK bit into R2.
"""
from __future__ import annotations


class PS2Device:
    """Independent cycle-by-cycle model of a PS/2 peripheral (keyboard/mouse).

    Simulates open-drain bus interaction with pull-up resistors on CLK and DATA lines.
    - Device always generates clock pulses during communication.
    - Device-to-Host: 11-bit frame (Start=0, 8 Data LSB-first, Odd Parity, Stop=1).
    - Host-to-Device: 12-bit frame (Start=0, 8 Data LSB-first, Odd Parity, Stop=1, ACK=0).
    """

    def __init__(
        self,
        clk_half_period: int = 15,
        tx_bytes: list[int] | None = None,
        inject_parity_error: bool = False,
        inject_framing_error: bool = False,
        ack_host: bool = True,
    ):
        self.clk_half_period = clk_half_period
        self.tx_bytes = list(tx_bytes) if tx_bytes else []
        self.inject_parity_error = inject_parity_error
        self.inject_framing_error = inject_framing_error
        self.ack_host = ack_host

        self.rx_bytes: list[int] = []
        self.dev_clk_low = False
        self.dev_data_low = False

        # Internal state machine
        # Mode: 'IDLE', 'DEV_TX', 'HOST_TX'
        self.state = "IDLE"
        self.timer = 0
        self.tx_frame: list[int] = []
        self.tx_bit_idx = 0
        self.tx_phase = "HIGH"  # 'HIGH' or 'LOW'

        # Host-to-device reception tracking
        self.prev_master_clk_low = False
        self.host_inhibit_cycles = 0
        self.host_rx_cycle = 0
        self.host_rx_byte = 0
        self.host_rx_phase = "HIGH"

    def _prepare_tx_frame(self, byte_val: int) -> list[int]:
        """Construct standard 11-bit PS/2 device-to-host frame."""
        # 1 start bit: 0
        bits = [0]
        # 8 data bits: LSB first
        ones_count = 0
        for i in range(8):
            bit = (byte_val >> i) & 1
            bits.append(bit)
            if bit:
                ones_count += 1

        # 1 odd parity bit: sum of 8 data bits + parity bit is odd
        parity_bit = 0 if (ones_count % 2 == 1) else 1
        if self.inject_parity_error:
            parity_bit ^= 1
        bits.append(parity_bit)

        # 1 stop bit: 1
        stop_bit = 0 if self.inject_framing_error else 1
        bits.append(stop_bit)
        return bits

    def step(self, master_clk_low: bool, master_data_low: bool) -> tuple[bool, bool]:
        """Advance device model by one clock cycle.

        Args:
            master_clk_low: True if host is actively driving clock LOW.
            master_data_low: True if host is actively driving data LOW.

        Returns:
            (device_clk_low, device_data_low)
        """
        bus_clk_low = master_clk_low or self.dev_clk_low
        bus_data_low = master_data_low or self.dev_data_low

        # Host inhibit detection: if host pulls clock low for >= 15 cycles,
        # device must abort any current transmission and wait.
        if master_clk_low:
            self.host_inhibit_cycles += 1
            if self.state != "IDLE":
                # Abort transmission
                self.state = "IDLE"
                self.dev_clk_low = False
                self.dev_data_low = False
        else:
            # Check for Host Request-to-Send: Host was inhibiting clock, and releases clock while holding data LOW
            if self.host_inhibit_cycles >= 15 and master_data_low:
                self.state = "HOST_TX"
                self.host_inhibit_cycles = 0
                self.host_rx_cycle = 0
                self.host_rx_byte = 0
                self.host_rx_phase = "HIGH"
                self.timer = self.clk_half_period
                self.dev_clk_low = False
                self.dev_data_low = False
            else:
                self.host_inhibit_cycles = 0

        self.prev_master_clk_low = master_clk_low

        # State machine processing
        if self.state == "IDLE":
            self.dev_clk_low = False
            self.dev_data_low = False
            if self.tx_bytes and not master_clk_low:
                cur_byte = self.tx_bytes.pop(0)
                self.tx_frame = self._prepare_tx_frame(cur_byte)
                self.tx_bit_idx = 0
                self.tx_phase = "HIGH"
                self.timer = self.clk_half_period
                self.state = "DEV_TX"
                # Set initial data bit
                self.dev_data_low = (self.tx_frame[0] == 0)

        elif self.state == "DEV_TX":
            cur_bit = self.tx_frame[self.tx_bit_idx]
            if self.tx_phase == "HIGH":
                self.dev_clk_low = False
                self.dev_data_low = (cur_bit == 0)
                self.timer -= 1
                if self.timer == 0:
                    self.tx_phase = "LOW"
                    self.timer = self.clk_half_period
            else:  # 'LOW'
                self.dev_clk_low = True
                self.dev_data_low = (cur_bit == 0)
                self.timer -= 1
                if self.timer == 0:
                    self.tx_bit_idx += 1
                    if self.tx_bit_idx >= len(self.tx_frame):
                        # Finished frame
                        self.state = "IDLE"
                        self.dev_clk_low = False
                        self.dev_data_low = False
                    else:
                        self.tx_phase = "HIGH"
                        self.timer = self.clk_half_period
                        next_bit = self.tx_frame[self.tx_bit_idx]
                        self.dev_data_low = (next_bit == 0)

        elif self.state == "HOST_TX":
            # Device generates clock pulses to receive host transmission
            # Total 12 clock cycles:
            # cycle 0: Start bit (0)
            # cycles 1..8: Data bits 0..7
            # cycle 9: Parity bit
            # cycle 10: Stop bit (1)
            # cycle 11: Device ACK (Device pulls data low if self.ack_host)
            if self.host_rx_phase == "HIGH":
                self.dev_clk_low = False
                if self.host_rx_cycle == 11 and self.ack_host:
                    self.dev_data_low = True
                else:
                    self.dev_data_low = False

                self.timer -= 1
                if self.timer == 0:
                    self.host_rx_phase = "LOW"
                    self.timer = self.clk_half_period
                    # Sample on falling edge
                    bit_val = 0 if bus_data_low else 1
                    if 1 <= self.host_rx_cycle <= 8:
                        data_bit_idx = self.host_rx_cycle - 1
                        self.host_rx_byte |= (bit_val << data_bit_idx)
                    elif self.host_rx_cycle == 10:
                        # Stop bit sampled: complete byte received from host
                        self.rx_bytes.append(self.host_rx_byte)
            else:  # 'LOW'
                self.dev_clk_low = True
                if self.host_rx_cycle == 11 and self.ack_host:
                    self.dev_data_low = True
                else:
                    self.dev_data_low = False

                self.timer -= 1
                if self.timer == 0:
                    self.host_rx_cycle += 1
                    if self.host_rx_cycle >= 12:
                        # Finished Host-to-Device transaction
                        self.state = "IDLE"
                        self.dev_clk_low = False
                        self.dev_data_low = False
                    else:
                        self.host_rx_phase = "HIGH"
                        self.timer = self.clk_half_period

        return (self.dev_clk_low, self.dev_data_low)


def build_ps2_rx_asm(clk_pin: int = 4, data_pin: int = 5) -> str:
    """Generate PS/2 Host Receive firmware.

    Sequence:
    1. Configure clk_pin and data_pin as open-drain outputs with latches high (pull-up release).
    2. Wait for Start Bit (0) on falling edge of clk_pin. If data!=0, sets R2=0xFF (Start Error).
    3. Loop or unroll 8 data bits:
       - Wait for clk_pin falling edge (WAITEDGE R3, clk_pin).
       - Sample data bit into R0 via SHIFTIN R0, data_pin.
       - Sample data bit for parity calculation: if bit==1, XORI R1, 1.
    4. Parity bit:
       - Wait for clk_pin falling edge.
       - Sample parity bit: if bit==1, XORI R1, 1.
       - If parity was valid odd parity, R1 must equal 0!
       - If R1 != 0, sets R2=0xFD (Parity Error).
    5. Stop bit:
       - Wait for clk_pin falling edge.
       - Sample stop bit (must be 1).
       - If stop bit == 0, sets R2=0xFE (Framing Error).
    6. If all valid: R2=0x00 (Success).
    7. Halt.
    """
    clk_mask = 1 << clk_pin
    data_mask = 1 << data_pin
    both_mask = clk_mask | data_mask
    edge_fall = clk_pin

    lines = [
        f"; PS/2 Host Receiver on CLK=pin {clk_pin}, DATA=pin {data_pin}",
        f"GWRI  0x{both_mask:02X}",       # Latches high before OD enable
        f"GDIRI 0x{both_mask:02X}",       # Directions = output (open-drain controls drive)
        f"GODRI 0x{both_mask:02X}",       # Enable open-drain mode
        "LDI   R0, 0x00",                # Received byte accumulator
        "LDI   R1, 0x01",                # Parity check accumulator (1 for odd parity)
        "LDI   R2, 0x00",                # Status register (0 = OK)
        "",
        "; --- Bit 0: Start Bit (must be 0) ---",
        f"WAITEDGE R3, 0x{edge_fall:02X}",
        "GRD   R2",
        f"ANDI  R2, 0x{data_mask:02X}",
        "JZ    start_ok",
        "LDI   R2, 0xFF",                # Start bit error
        "JMP   done",
        "start_ok:",
        "",
    ]

    for bit_i in range(8):
        lines.extend([
            f"; --- Bit {bit_i + 1}: Data bit {bit_i} ---",
            f"WAITEDGE R3, 0x{edge_fall:02X}",
            f"SHIFTIN  R0, {data_pin}",
            "GRD      R2",
            f"ANDI     R2, 0x{data_mask:02X}",
            f"JZ       data_zero_{bit_i}",
            "XORI     R1, 0x01",
            f"data_zero_{bit_i}:",
            "",
        ])

    lines.extend([
        "; --- Bit 9: Parity Bit (Odd) ---",
        f"WAITEDGE R3, 0x{edge_fall:02X}",
        "GRD      R2",
        f"ANDI     R2, 0x{data_mask:02X}",
        "JZ       par_zero",
        "XORI     R1, 0x01",
        "par_zero:",
        "MOV      R2, R1",                # If odd parity correct, R1 should be 0
        "JZ       parity_ok",
        "LDI      R2, 0xFD",              # Parity error status
        "JMP      done",
        "parity_ok:",
        "",
        "; --- Bit 10: Stop Bit (must be 1) ---",
        f"WAITEDGE R3, 0x{edge_fall:02X}",
        "GRD      R2",
        f"ANDI     R2, 0x{data_mask:02X}",
        "JNZ      stop_ok",
        "LDI      R2, 0xFE",              # Framing error status
        "JMP      done",
        "stop_ok:",
        "LDI      R2, 0x00",              # Success
        "done:",
        "HALT",
        "",
    ])
    return "\n".join(lines)


def build_ps2_tx_asm(
    cmd_byte: int,
    clk_pin: int = 4,
    data_pin: int = 5,
    inhibit_cycles: int = 30,
) -> str:
    """Generate PS/2 Host-to-Device Command Transmission firmware.

    Sequence:
    1. Configure clk_pin and data_pin as open-drain.
    2. Inhibit State: Host pulls CLK low for inhibit_cycles.
    3. Request-To-Send: Host pulls DATA low, then releases CLK.
    4. Device detects RTS and generates 12 clock pulses:
       - Cycle 0: Device clocks Start bit (Host holds DATA low). Host waits for falling clock.
       - Cycles 1..8: For each data bit (LSB-first):
         Host waits for rising edge of CLK (WAITEDGE R3, clk_pin | 0x08).
         Host drives bit onto DATA.
         Host waits for falling edge of CLK (WAITEDGE R3, clk_pin).
       - Cycle 9: Parity bit (Odd). Host waits for rising edge, drives parity, waits for falling edge.
       - Cycle 10: Stop bit (1). Host waits for rising edge, releases DATA, waits for falling edge.
       - Cycle 11: ACK bit (Device to Host).
         Host waits for falling edge of CLK.
         Host samples DATA via GRD. If DATA == 0, ACK received (R2 = 0x00); else NACK (R2 = 0xFC).
    5. Halt.
    """
    clk_mask = 1 << clk_pin
    data_mask = 1 << data_pin
    both_mask = clk_mask | data_mask
    edge_fall = clk_pin
    edge_rise = (1 << 3) | clk_pin

    # Calculate odd parity for cmd_byte
    ones = bin(cmd_byte).count("1")
    parity_bit = 0 if (ones % 2 == 1) else 1

    lines = [
        f"; PS/2 Host-to-Device Command 0x{cmd_byte:02X} on CLK=pin {clk_pin}, DATA=pin {data_pin}",
        f"GWRI  0x{both_mask:02X}",
        f"GDIRI 0x{both_mask:02X}",
        f"GODRI 0x{both_mask:02X}",
        "",
        "; --- Step 1: Host Inhibit (pull CLK low) ---",
        f"GWRI  0x{data_mask:02X}",        # clk=0, data=1 (high-Z)
        f"WAIT  {inhibit_cycles - 2}",
        "",
        "; --- Step 2: Request-To-Send (pull DATA low) ---",
        "GWRI  0x00",                     # clk=0, data=0
        "WAIT  4",
        "",
        "; --- Step 3: Release CLK (start bit is on DATA) ---",
        f"GWRI  0x{clk_mask:02X}",        # clk=1 (high-Z), data=0
        "",
        "; --- Step 4: Cycle 0 (Start bit clocked by device) ---",
        f"WAITEDGE R3, 0x{edge_fall:02X}",
        "",
    ]

    # 8 data bits
    for bit_i in range(8):
        bit_val = (cmd_byte >> bit_i) & 1
        lines.extend([
            f"; --- Cycle {bit_i + 1}: Data bit {bit_i} ({bit_val}) ---",
            f"WAITEDGE R3, 0x{edge_rise:02X}",
        ])
        if bit_val == 0:
            lines.append(f"GWRI  0x{clk_mask:02X}")  # clk=1 (high-Z), data=0
        else:
            lines.append(f"GWRI  0x{both_mask:02X}")  # clk=1, data=1 (high-Z)
        lines.extend([
            f"WAITEDGE R3, 0x{edge_fall:02X}",
            "",
        ])

    # Parity bit
    lines.extend([
        f"; --- Cycle 9: Parity bit ({parity_bit}) ---",
        f"WAITEDGE R3, 0x{edge_rise:02X}",
    ])
    if parity_bit == 0:
        lines.append(f"GWRI  0x{clk_mask:02X}")
    else:
        lines.append(f"GWRI  0x{both_mask:02X}")
    lines.extend([
        f"WAITEDGE R3, 0x{edge_fall:02X}",
        "",
    ])

    # Stop bit: Host releases data
    lines.extend([
        "; --- Cycle 10: Stop bit (1 - Host releases DATA) ---",
        f"WAITEDGE R3, 0x{edge_rise:02X}",
        f"GWRI  0x{both_mask:02X}",       # both released
        f"WAITEDGE R3, 0x{edge_fall:02X}",
        "",
        "; --- Cycle 11: Device ACK (Device pulls DATA low) ---",
        f"WAITEDGE R3, 0x{edge_fall:02X}",
        "GRD   R2",
        f"ANDI  R2, 0x{data_mask:02X}",
        "JZ    ack_ok",
        "LDI   R2, 0xFC",                # Device NACK error
        "JMP   tx_done",
        "ack_ok:",
        "LDI   R2, 0x00",                # Device ACK OK
        "tx_done:",
        "WAIT  20",
        "HALT",
        "",
    ])
    return "\n".join(lines)
