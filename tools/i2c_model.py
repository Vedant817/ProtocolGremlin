#!/usr/bin/env python3
"""
tools/i2c_model.py - I2C Protocol Firmware Generator & Reference Slave Model

Provides:
1. build_i2c_write_asm(): Parameterized I2C Master write transaction firmware.
2. build_i2c_read_asm(): Parameterized I2C Master read transaction firmware.
3. I2cSlave: Independent cycle-by-cycle I2C bus monitor and slave state machine.
"""
from __future__ import annotations
from typing import List, Tuple, Optional


def build_i2c_write_asm(
    addr7: int,
    data_bytes: List[int],
    half_period: int = 4,
    sda_pin: int = 0,
    scl_pin: int = 1
) -> str:
    """
    Generate compact assembly using DECJNZ loops for an I2C write transaction:
    START -> [Address 7-bit + W(0)] -> ACK -> [Data bytes...] -> ACKs -> STOP.
    Uses hardware open-drain (GODRI) so outputting 1 releases the line to pullup/slave.
    """
    pins_mask = (1 << sda_pin) | (1 << scl_pin)
    wait_half = max(0, half_period - 2)

    lines: List[str] = [
        "; -------------------------------------------------------------",
        f"; I2C Master Write: Address 0x{addr7:02X}, {len(data_bytes)} bytes",
        f"; Pins: SDA={sda_pin}, SCL={scl_pin}, T_half={half_period} cycles",
        "; -------------------------------------------------------------",
        f"    GDIRI 0x{pins_mask:02X}     ; Enable SDA and SCL as outputs",
        f"    GODRI 0x{pins_mask:02X}     ; Configure SDA and SCL as OPEN-DRAIN",
        f"    GWRI  0x{pins_mask:02X}     ; Release both lines (idle HIGH)",
        "    LDI   R2, 0xFF       ; Constant 1s (release pin)",
        "    LDI   R3, 0x00       ; Constant 0s (pull pin LOW)",
        "",
        "; --- START Condition ---",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        f"    SHIFTOUT R3, {sda_pin} ; SDA -> 0 (while SCL=1: START)",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        f"    SHIFTOUT R3, {scl_pin} ; SCL -> 0",
        "",
    ]

    def emit_byte_tx(byte_val: int, label_suffix: str) -> List[str]:
        b_lines = [
            f"; Byte 0x{byte_val:02X}",
            f"    LDI   R0, 0x{byte_val & 0xFF:02X}",
            "    LDI   R1, 0x08       ; 8 bits loop counter",
            f"loop_{label_suffix}:",
            f"    SHIFTOUT R0, {sda_pin}, MSB ; Drive bit on SDA",
        ]
        if wait_half > 0:
            b_lines.append(f"    WAIT  0x{wait_half:02X}")
        b_lines += [
            "    LDI   R2, 0xFF",
            f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 1",
        ]
        if wait_half > 0:
            b_lines.append(f"    WAIT  0x{wait_half:02X}")
        b_lines += [
            "    LDI   R3, 0x00",
            f"    SHIFTOUT R3, {scl_pin}      ; SCL -> 0",
            f"    DECJNZ R1, loop_{label_suffix}",
            "; 9th clock: ACK bit",
            "    LDI   R2, 0xFF",
            f"    SHIFTOUT R2, {sda_pin}      ; Release SDA for slave ACK",
            "    LDI   R1, 0x00       ; Clear ACK receiver register",
        ]
        if wait_half > 0:
            b_lines.append(f"    WAIT  0x{wait_half:02X}")
        b_lines += [
            "    LDI   R2, 0xFF",
            f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 1",
            f"    SHIFTIN  R1, {sda_pin}, MSB ; Sample ACK bit (0=ACK, 1=NACK)",
        ]
        if wait_half > 0:
            b_lines.append(f"    WAIT  0x{wait_half:02X}")
        b_lines += [
            "    LDI   R3, 0x00",
            f"    SHIFTOUT R3, {scl_pin}      ; SCL -> 0",
            "",
        ]
        return b_lines

    # Address byte (7-bit address + Write bit 0)
    addr_byte = (addr7 << 1) & 0xFE
    lines += emit_byte_tx(addr_byte, "addr")

    # Data bytes
    for idx, dbyte in enumerate(data_bytes):
        lines += emit_byte_tx(dbyte, f"d{idx}")

    # STOP Condition: SDA transitions 0 -> 1 while SCL is 1
    lines += [
        "; --- STOP Condition ---",
        "    LDI   R3, 0x00",
        f"    SHIFTOUT R3, {sda_pin}      ; Ensure SDA=0 while SCL=0",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        "    LDI   R2, 0xFF",
        f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 1",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        f"    SHIFTOUT R2, {sda_pin}      ; SDA -> 1 (while SCL=1: STOP)",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        "    HALT",
        "",
    ]
    return "\n".join(lines)


def build_i2c_read_asm(
    addr7: int,
    num_bytes: int = 1,
    half_period: int = 4,
    sda_pin: int = 0,
    scl_pin: int = 1
) -> str:
    """
    Generate compact assembly using DECJNZ loops for an I2C read transaction:
    START -> [Address 7-bit + R(1)] -> ACK -> [Read N bytes from Slave] -> STOP.
    Reads byte into R0, with NACK generated on last byte.
    """
    pins_mask = (1 << sda_pin) | (1 << scl_pin)
    wait_half = max(0, half_period - 2)

    lines: List[str] = [
        "; -------------------------------------------------------------",
        f"; I2C Master Read: Address 0x{addr7:02X}, {num_bytes} bytes",
        f"; Pins: SDA={sda_pin}, SCL={scl_pin}, T_half={half_period} cycles",
        "; -------------------------------------------------------------",
        f"    GDIRI 0x{pins_mask:02X}     ; Enable SDA and SCL as outputs",
        f"    GODRI 0x{pins_mask:02X}     ; Configure SDA and SCL as OPEN-DRAIN",
        f"    GWRI  0x{pins_mask:02X}     ; Release both lines (idle HIGH)",
        "    LDI   R2, 0xFF       ; Constant 1s (release pin)",
        "    LDI   R3, 0x00       ; Constant 0s (pull pin LOW)",
        "",
        "; --- START Condition ---",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        f"    SHIFTOUT R3, {sda_pin} ; SDA -> 0 (START)",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        f"    SHIFTOUT R3, {scl_pin} ; SCL -> 0",
        "",
        "; --- Address Byte (7-bit address + Read bit 1) ---",
        f"    LDI   R0, 0x{(((addr7 << 1) | 1) & 0xFF):02X}",
        "    LDI   R1, 0x08       ; 8 bits loop counter",
        "loop_r_addr:",
        f"    SHIFTOUT R0, {sda_pin}, MSB ; Drive address bit",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        "    LDI   R2, 0xFF",
        f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 1",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        "    LDI   R3, 0x00",
        f"    SHIFTOUT R3, {scl_pin}      ; SCL -> 0",
        "    DECJNZ R1, loop_r_addr",
        "; Sample Slave ACK",
        "    LDI   R2, 0xFF",
        f"    SHIFTOUT R2, {sda_pin}      ; Release SDA for slave ACK",
        "    LDI   R1, 0x00       ; Clear ACK register",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        "    LDI   R2, 0xFF",
        f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 1",
        f"    SHIFTIN  R1, {sda_pin}, MSB ; Sample ACK bit",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        "    LDI   R3, 0x00",
        f"    SHIFTOUT R3, {scl_pin}      ; SCL -> 0",
        "",
        "; --- Receive Data Byte from Slave ---",
        "    LDI   R0, 0x00       ; Clear RX register",
        "    LDI   R2, 0xFF",
        f"    SHIFTOUT R2, {sda_pin}      ; Release SDA so slave can drive",
        "    LDI   R1, 0x08       ; 8 bits loop counter",
        "loop_r_data:",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        "    LDI   R2, 0xFF",
        f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 1",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        f"    SHIFTIN  R0, {sda_pin}, MSB ; Sample data bit",
        "    LDI   R3, 0x00",
        f"    SHIFTOUT R3, {scl_pin}      ; SCL -> 0",
        "    DECJNZ R1, loop_r_data",
        "",
        "; Send NACK (release SDA to 1) on last byte",
        "    LDI   R2, 0xFF",
        f"    SHIFTOUT R2, {sda_pin}      ; SDA -> 1 (NACK)",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        "    LDI   R2, 0xFF",
        f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 1",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        "    LDI   R3, 0x00",
        f"    SHIFTOUT R3, {scl_pin}      ; SCL -> 0",
        "",
        "; --- STOP Condition ---",
        "    LDI   R3, 0x00",
        f"    SHIFTOUT R3, {sda_pin}      ; SDA -> 0",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        "    LDI   R2, 0xFF",
        f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 1",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        f"    SHIFTOUT R2, {sda_pin}      ; SDA -> 1 (STOP)",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        "    HALT",
        "",
    ]
    return "\n".join(lines)


def build_i2c_write_with_stretch_asm(
    addr7: int,
    data_bytes: List[int],
    half_period: int = 4,
    sda_pin: int = 0,
    scl_pin: int = 1
) -> str:
    """
    Generate I2C Master write firmware with clock-stretching synchronization via WAITEDGE:
    Whenever SCL is released to 1 for an ACK clock pulse, the core executes:
        WAITEDGE R3, (0x08 | scl_pin)
    This stalls execution until the slave releases SCL, automatically absorbing
    arbitrary slave clock stretching without timing violations, and captures the
    stretch duration (in cycles) into R3.
    """
    pins_mask = (1 << sda_pin) | (1 << scl_pin)
    wait_half = max(0, half_period - 2)

    lines: List[str] = [
        "; -------------------------------------------------------------",
        f"; I2C Master Write with Clock Stretch Sync: Addr 0x{addr7:02X}, {len(data_bytes)} bytes",
        f"; Pins: SDA={sda_pin}, SCL={scl_pin}, T_half={half_period} cycles",
        "; -------------------------------------------------------------",
        f"    GDIRI 0x{pins_mask:02X}     ; Enable SDA and SCL as outputs",
        f"    GODRI 0x{pins_mask:02X}     ; Configure SDA and SCL as OPEN-DRAIN",
        f"    GWRI  0x{pins_mask:02X}     ; Release both lines (idle HIGH)",
        "    LDI   R2, 0xFF       ; Constant 1s (release pin)",
        "    LDI   R3, 0x00       ; Initialize stretch measurement to 0",
        "",
        "; --- START Condition ---",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        "    LDI   R2, 0x00",
        f"    SHIFTOUT R2, {sda_pin} ; SDA -> 0 (START)",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        f"    SHIFTOUT R2, {scl_pin} ; SCL -> 0",
        "",
    ]

    def emit_byte_tx_with_stretch(byte_val: int, label_suffix: str) -> List[str]:
        b_lines = [
            f"; Byte 0x{byte_val:02X}",
            f"    LDI   R0, 0x{byte_val & 0xFF:02X}",
            "    LDI   R1, 0x08       ; 8 bits loop counter",
            f"loop_{label_suffix}:",
            f"    SHIFTOUT R0, {sda_pin}, MSB ; Drive bit on SDA",
        ]
        if wait_half > 0:
            b_lines.append(f"    WAIT  0x{wait_half:02X}")
        b_lines += [
            "    LDI   R2, 0xFF",
            f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 1",
        ]
        if wait_half > 0:
            b_lines.append(f"    WAIT  0x{wait_half:02X}")
        b_lines += [
            "    LDI   R2, 0x00",
            f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 0",
            f"    DECJNZ R1, loop_{label_suffix}",
            "; 9th clock: ACK bit with WAITEDGE clock stretching sync",
            "    LDI   R2, 0xFF",
            f"    SHIFTOUT R2, {sda_pin}      ; Release SDA for slave ACK",
            "    LDI   R1, 0x00       ; Clear ACK receiver register",
        ]
        if wait_half > 0:
            b_lines.append(f"    WAIT  0x{wait_half:02X}")
        b_lines += [
            "    LDI   R2, 0xFF",
            f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 1 (release line)",
            f"    WAITEDGE R3, 0x{0x08 | scl_pin:02X} ; Wait for SCL rising edge (slave stretch sync)",
            f"    SHIFTIN  R1, {sda_pin}, MSB ; Sample ACK bit (0=ACK, 1=NACK)",
        ]
        if wait_half > 0:
            b_lines.append(f"    WAIT  0x{wait_half:02X}")
        b_lines += [
            "    LDI   R2, 0x00",
            f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 0",
            "",
        ]
        return b_lines

    # Address byte (7-bit address + Write bit 0)
    addr_byte = (addr7 << 1) & 0xFE
    lines += emit_byte_tx_with_stretch(addr_byte, "addr")

    # Data bytes
    for idx, dbyte in enumerate(data_bytes):
        lines += emit_byte_tx_with_stretch(dbyte, f"d{idx}")

    # STOP Condition
    lines += [
        "; --- STOP Condition ---",
        "    LDI   R2, 0x00",
        f"    SHIFTOUT R2, {sda_pin}      ; Ensure SDA=0 while SCL=0",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        "    LDI   R2, 0xFF",
        f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 1",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        f"    SHIFTOUT R2, {sda_pin}      ; SDA -> 1 (STOP)",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        "    HALT",
        "",
    ]
    return "\n".join(lines)


def build_i2c_write_with_arbitration_asm(
    addr7: int,
    data_bytes: List[int],
    half_period: int = 4,
    sda_pin: int = 0,
    scl_pin: int = 1
) -> str:
    """
    Generate I2C Master write firmware with multi-master arbitration loss detection:
    For every bit transmitted where the Master drives '1' (releases open-drain SDA),
    the core reads back the physical bus state via GRD while SCL is high.
    If SDA is sampled as 0 (indicating another master pulled the bus LOW),
    the master immediately aborts the transaction, releases all bus lines (GWRI 0xFF),
    stores error code 0xEE in R0, and halts without asserting STOP.
    If arbitration is retained through all bytes, R0 is set to 0x00.
    """
    pins_mask = (1 << sda_pin) | (1 << scl_pin)
    wait_half = max(0, half_period - 2)

    lines: List[str] = [
        "; -------------------------------------------------------------",
        f"; I2C Write with Arbitration Detection: Addr 0x{addr7:02X}, {len(data_bytes)} bytes",
        f"; Pins: SDA={sda_pin}, SCL={scl_pin}, T_half={half_period} cycles",
        "; -------------------------------------------------------------",
        f"    GDIRI 0x{pins_mask:02X}     ; Enable SDA and SCL as outputs",
        f"    GODRI 0x{pins_mask:02X}     ; Configure SDA and SCL as OPEN-DRAIN",
        f"    GWRI  0x{pins_mask:02X}     ; Release both lines (idle HIGH)",
        "    LDI   R0, 0x00       ; Default status = SUCCESS (0x00)",
        "    LDI   R2, 0xFF       ; Constant 1s (release pin)",
        "    LDI   R3, 0x00       ; Constant 0s (pull pin LOW)",
        "",
        "; --- START Condition ---",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        f"    SHIFTOUT R3, {sda_pin} ; SDA -> 0 (START)",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        f"    SHIFTOUT R3, {scl_pin} ; SCL -> 0",
        "",
    ]

    def emit_byte_tx_with_arb(byte_val: int, prefix: str) -> List[str]:
        b_lines = [
            f"; Byte 0x{byte_val:02X} with Arbitration Check",
        ]
        for b_idx in range(7, -1, -1):
            bit_val = (byte_val >> b_idx) & 1
            b_lines.append(f"; Bit {b_idx}: value {bit_val}")
            if bit_val == 0:
                b_lines += [
                    "    LDI   R3, 0x00",
                    f"    SHIFTOUT R3, {sda_pin}      ; SDA -> 0",
                ]
                if wait_half > 0:
                    b_lines.append(f"    WAIT  0x{wait_half:02X}")
                b_lines += [
                    "    LDI   R2, 0xFF",
                    f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 1",
                ]
                if wait_half > 0:
                    b_lines.append(f"    WAIT  0x{wait_half:02X}")
                b_lines += [
                    "    LDI   R3, 0x00",
                    f"    SHIFTOUT R3, {scl_pin}      ; SCL -> 0",
                ]
            else:
                b_lines += [
                    "    LDI   R2, 0xFF",
                    f"    SHIFTOUT R2, {sda_pin}      ; SDA -> 1 (release)",
                ]
                if wait_half > 0:
                    b_lines.append(f"    WAIT  0x{wait_half:02X}")
                b_lines += [
                    f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 1",
                    "    WAIT  0x02                  ; Wait for bus synchronizer (2 cycles)",
                    "    GRD   R1                    ; Sample bus",
                    f"    ANDI  R1, 0x{1 << sda_pin:02X}     ; Check if SDA is 1",
                    "    JZ    arb_lost              ; If SDA is 0, arbitration lost!",
                ]
                if wait_half > 0:
                    b_lines.append(f"    WAIT  0x{wait_half:02X}")
                b_lines += [
                    "    LDI   R3, 0x00",
                    f"    SHIFTOUT R3, {scl_pin}      ; SCL -> 0",
                ]

        # 9th clock: ACK bit
        b_lines += [
            "; 9th clock: ACK bit",
            "    LDI   R2, 0xFF",
            f"    SHIFTOUT R2, {sda_pin}      ; Release SDA for slave ACK",
            "    LDI   R1, 0x00       ; Clear ACK receiver register",
        ]
        if wait_half > 0:
            b_lines.append(f"    WAIT  0x{wait_half:02X}")
        b_lines += [
            "    LDI   R2, 0xFF",
            f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 1",
            f"    SHIFTIN  R1, {sda_pin}, MSB ; Sample ACK bit",
        ]
        if wait_half > 0:
            b_lines.append(f"    WAIT  0x{wait_half:02X}")
        b_lines += [
            "    LDI   R3, 0x00",
            f"    SHIFTOUT R3, {scl_pin}      ; SCL -> 0",
            "",
        ]
        return b_lines

    # Address byte
    addr_byte = (addr7 << 1) & 0xFE
    lines += emit_byte_tx_with_arb(addr_byte, "addr")

    # Data bytes
    for idx, dbyte in enumerate(data_bytes):
        lines += emit_byte_tx_with_arb(dbyte, f"d{idx}")

    # STOP Condition
    lines += [
        "; --- STOP Condition ---",
        "    LDI   R3, 0x00",
        f"    SHIFTOUT R3, {sda_pin}      ; Ensure SDA=0 while SCL=0",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        "    LDI   R2, 0xFF",
        f"    SHIFTOUT R2, {scl_pin}      ; SCL -> 1",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        f"    SHIFTOUT R2, {sda_pin}      ; SDA -> 1 (STOP)",
    ]
    if wait_half > 0:
        lines.append(f"    WAIT  0x{wait_half:02X}")
    lines += [
        "    HALT",
        "",
        "; --- Arbitration Loss Handler ---",
        "arb_lost:",
        "    GWRI  0xFF                  ; Immediate bus release (tri-state all outputs)",
        "    LDI   R0, 0xEE              ; Error code 0xEE = Arbitration Lost",
        "    HALT",
        "",
    ]
    return "\n".join(lines)


class I2cSlave:
    """
    Independent cycle-by-cycle I2C Bus Monitor & Slave Model.
    Tracks bus conditions (START, STOP, Address match, ACK generation, Data rx/tx).
    """

    def __init__(self, address: int, tx_bytes: Optional[List[int]] = None, clock_stretch_cycles: int = 0):
        self.address = address & 0x7F
        self.tx_bytes = list(tx_bytes) if tx_bytes else []
        self.tx_byte_idx = 0
        self.received_bytes: List[int] = []

        self.clock_stretch_cycles = clock_stretch_cycles
        self.stretch_counter = 0
        self.slave_drive_scl_low = False

        self.prev_scl = 1
        self.prev_sda = 1
        self.bit_cnt = 0
        self.curr_byte = 0
        self.is_read = False
        self.matched = False
        self.slave_drive_sda_low = False

        # Phase: "IDLE", "ADDR", "DATA_RX", "DATA_TX"
        self.phase = "IDLE"

        self.start_count = 0
        self.stop_count = 0

    def step(self, scl: int, sda: int) -> bool:
        """
        Process bus state for the current cycle.
        Returns: True if the slave is pulling SDA low (open-drain active drive), False otherwise.
        """
        # Handle active clock stretch countdown
        if self.stretch_counter > 0:
            self.stretch_counter -= 1
            if self.stretch_counter == 0:
                self.slave_drive_scl_low = False

        # Detect START condition: SDA falling while SCL is high
        if self.prev_scl == 1 and scl == 1 and self.prev_sda == 1 and sda == 0:
            self.start_count += 1
            self.phase = "START"
            self.bit_cnt = 0
            self.curr_byte = 0
            self.matched = False
            self.slave_drive_sda_low = False

        # Detect STOP condition: SDA rising while SCL is high
        elif self.prev_scl == 1 and scl == 1 and self.prev_sda == 0 and sda == 1:
            self.stop_count += 1
            self.phase = "IDLE"
            self.slave_drive_sda_low = False

        # SCL Rising Edge: sample incoming bit
        elif self.prev_scl == 0 and scl == 1:
            if self.bit_cnt < 8:
                if self.phase in ("ADDR", "DATA_RX"):
                    self.curr_byte = ((self.curr_byte << 1) | (sda & 1)) & 0xFF
            else:
                # 9th bit (ACK/NACK clock high phase)
                if self.phase == "DATA_TX":
                    master_nack = bool(sda & 1)
                    if master_nack:
                        self.phase = "IDLE"
                    else:
                        self.tx_byte_idx += 1

        # SCL Falling Edge: update driven bit (SDA changes while SCL is low)
        elif self.prev_scl == 1 and scl == 0:
            if self.phase == "START":
                # SCL falling after START condition: begin receiving address bits
                self.phase = "ADDR"
                self.bit_cnt = 0
                self.curr_byte = 0
            else:
                self.bit_cnt += 1

                if self.bit_cnt == 8:
                    # 8 bits finished: entering 9th bit (ACK phase)
                    if self.phase == "ADDR":
                        addr_rcvd = (self.curr_byte >> 1) & 0x7F
                        self.is_read = bool(self.curr_byte & 1)
                        if addr_rcvd == self.address:
                            self.matched = True
                            self.slave_drive_sda_low = True  # Drive ACK
                            if self.clock_stretch_cycles > 0:
                                self.slave_drive_scl_low = True
                                self.stretch_counter = self.clock_stretch_cycles
                        else:
                            self.matched = False
                            self.slave_drive_sda_low = False
                    elif self.phase == "DATA_RX" and self.matched:
                        self.received_bytes.append(self.curr_byte)
                        self.slave_drive_sda_low = True  # Drive ACK
                        if self.clock_stretch_cycles > 0:
                            self.slave_drive_scl_low = True
                            self.stretch_counter = self.clock_stretch_cycles
                    else:
                        self.slave_drive_sda_low = False

                elif self.bit_cnt == 9:
                    # 9th bit (ACK) finished: release SDA and prepare for next byte
                    self.slave_drive_sda_low = False
                    self.bit_cnt = 0
                    self.curr_byte = 0

                    if self.phase == "ADDR":
                        if self.matched:
                            self.phase = "DATA_TX" if self.is_read else "DATA_RX"
                            if self.phase == "DATA_TX":
                                tx_val = self.tx_bytes[self.tx_byte_idx] if self.tx_byte_idx < len(self.tx_bytes) else 0xFF
                                bit_val = (tx_val >> 7) & 1
                                self.slave_drive_sda_low = (bit_val == 0)
                        else:
                            self.phase = "IDLE"

                    elif self.phase == "DATA_TX":
                        tx_val = self.tx_bytes[self.tx_byte_idx] if self.tx_byte_idx < len(self.tx_bytes) else 0xFF
                        bit_val = (tx_val >> 7) & 1
                        self.slave_drive_sda_low = (bit_val == 0)

                elif self.phase == "DATA_TX" and self.bit_cnt < 8:
                    tx_val = self.tx_bytes[self.tx_byte_idx] if self.tx_byte_idx < len(self.tx_bytes) else 0xFF
                    bit_val = (tx_val >> (7 - self.bit_cnt)) & 1
                    self.slave_drive_sda_low = (bit_val == 0)

                else:
                    self.slave_drive_sda_low = False

        self.prev_scl = scl
        self.prev_sda = sda
        return self.slave_drive_sda_low

