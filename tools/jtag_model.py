# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Independent JTAG (IEEE 1149.1) TAP Controller reference model and firmware generator.

This module provides:
1. JtagTarget: An independent cycle-accurate emulation of an IEEE 1149.1
   JTAG Target (TAP state machine, 32-bit IDCODE register, 1-bit BYPASS register,
   and 4-bit Instruction Register).
2. build_jtag_read_idcode_asm: Generates firmware that resets the TAP, navigates
   to Shift-DR, and reads out the full 32-bit IDCODE directly into registers R0..R3.
3. build_jtag_bypass_verify_asm: Generates firmware that programs the BYPASS
   instruction into the IR, navigates to Shift-DR, and verifies 1-cycle data
   propagation across the BYPASS register.
"""
from __future__ import annotations

# 16 standard IEEE 1149.1 TAP states
TEST_LOGIC_RESET = 0
RUN_TEST_IDLE    = 1
SELECT_DR_SCAN   = 2
CAPTURE_DR       = 3
SHIFT_DR         = 4
EXIT1_DR         = 5
PAUSE_DR         = 6
EXIT2_DR         = 7
UPDATE_DR        = 8
SELECT_IR_SCAN   = 9
CAPTURE_IR       = 10
SHIFT_IR         = 11
EXIT1_IR         = 12
PAUSE_IR         = 13
EXIT2_IR         = 14
UPDATE_IR        = 15

# Standard JTAG instructions
OP_BYPASS = 0b1111
OP_IDCODE = 0b0001


class JtagTarget:
    """Independent cycle-by-cycle model of an IEEE 1149.1 JTAG target device.

    Implements:
    - Standard 16-state TAP Controller state machine.
    - 32-bit Device Identification (IDCODE) Register (IEEE 1149.1 compliant: LSB=1).
    - 1-bit BYPASS Register.
    - 4-bit Instruction Register (IR).
    """

    def __init__(
        self,
        idcode: int = 0x149511C3,
        ir_len: int = 4,
    ):
        self.idcode = idcode & 0xFFFFFFFF
        assert (self.idcode & 1) == 1, "IEEE 1149.1 requires bit 0 of IDCODE to be 1"
        self.ir_len = ir_len

        self.tap_state = TEST_LOGIC_RESET
        self.ir = OP_IDCODE  # IDCODE is default instruction upon reset
        self.shift_ir = 0
        self.shift_dr = 0
        self.dr_len = 32

        self.prev_tck = 0
        self.tdo = 1

    def _next_tap_state(self, current_state: int, tms: int) -> int:
        """Standard IEEE 1149.1 state transition table."""
        if current_state == TEST_LOGIC_RESET:
            return TEST_LOGIC_RESET if tms else RUN_TEST_IDLE
        elif current_state == RUN_TEST_IDLE:
            return SELECT_DR_SCAN if tms else RUN_TEST_IDLE
        elif current_state == SELECT_DR_SCAN:
            return SELECT_IR_SCAN if tms else CAPTURE_DR
        elif current_state == CAPTURE_DR:
            return EXIT1_DR if tms else SHIFT_DR
        elif current_state == SHIFT_DR:
            return EXIT1_DR if tms else SHIFT_DR
        elif current_state == EXIT1_DR:
            return UPDATE_DR if tms else PAUSE_DR
        elif current_state == PAUSE_DR:
            return EXIT2_DR if tms else PAUSE_DR
        elif current_state == EXIT2_DR:
            return UPDATE_DR if tms else SHIFT_DR
        elif current_state == UPDATE_DR:
            return SELECT_DR_SCAN if tms else RUN_TEST_IDLE
        elif current_state == SELECT_IR_SCAN:
            return TEST_LOGIC_RESET if tms else CAPTURE_IR
        elif current_state == CAPTURE_IR:
            return EXIT1_IR if tms else SHIFT_IR
        elif current_state == SHIFT_IR:
            return EXIT1_IR if tms else SHIFT_IR
        elif current_state == EXIT1_IR:
            return UPDATE_IR if tms else PAUSE_IR
        elif current_state == PAUSE_IR:
            return EXIT2_IR if tms else PAUSE_IR
        elif current_state == EXIT2_IR:
            return UPDATE_IR if tms else SHIFT_IR
        elif current_state == UPDATE_IR:
            return SELECT_DR_SCAN if tms else RUN_TEST_IDLE
        return TEST_LOGIC_RESET

    def step(self, tck: int, tms: int, tdi: int) -> int:
        """Advance JTAG target model by one clock cycle given input pin states.

        Args:
            tck: Test Clock (0 or 1)
            tms: Test Mode Select (0 or 1)
            tdi: Test Data In (0 or 1)

        Returns:
            tdo: Test Data Out (0 or 1)
        """
        # Rising edge of TCK: TAP state changes, TDI is sampled
        if self.prev_tck == 0 and tck == 1:
            prev_state = self.tap_state
            self.tap_state = self._next_tap_state(prev_state, tms)

            # Shift actions on rising edge
            if prev_state == SHIFT_IR:
                # Shift in TDI at MSB, shift out LSB
                self.shift_ir = ((tdi & 1) << (self.ir_len - 1)) | (self.shift_ir >> 1)
            elif prev_state == SHIFT_DR:
                self.shift_dr = ((tdi & 1) << (self.dr_len - 1)) | (self.shift_dr >> 1)

            # State entry actions
            if self.tap_state == TEST_LOGIC_RESET:
                self.ir = OP_IDCODE
            elif self.tap_state == CAPTURE_IR:
                # Standard IEEE 1149.1 requires Capture-IR to load fixed pattern ending in 01
                self.shift_ir = 0b0001
            elif self.tap_state == CAPTURE_DR:
                if self.ir == OP_IDCODE:
                    self.shift_dr = self.idcode
                    self.dr_len = 32
                elif self.ir == OP_BYPASS:
                    self.shift_dr = 0
                    self.dr_len = 1
            elif self.tap_state == UPDATE_IR:
                self.ir = self.shift_ir & ((1 << self.ir_len) - 1)

        # Falling edge of TCK: TDO changes state
        elif self.prev_tck == 1 and tck == 0:
            if self.tap_state == SHIFT_IR:
                self.tdo = self.shift_ir & 1
            elif self.tap_state == SHIFT_DR:
                self.tdo = self.shift_dr & 1
            else:
                self.tdo = 1  # Released / pull-up

        self.prev_tck = tck
        return self.tdo


def build_jtag_read_idcode_asm(
    tck_pin: int = 4,
    tms_pin: int = 5,
    tdi_pin: int = 6,
    tdo_pin: int = 7,
    half_period: int = 2,
) -> str:
    """Generate JTAG IEEE 1149.1 32-bit IDCODE readout firmware.

    Sequence:
    1. Configure GPIO: TCK, TMS, TDI as outputs; TDO as input.
    2. Reset TAP: Assert TMS=1 for 5 TCK pulses to enter Test-Logic-Reset.
    3. Navigate to Shift-DR:
       - Pulse 1 (TMS=0): Run-Test/Idle
       - Pulse 2 (TMS=1): Select-DR-Scan
       - Pulse 3 (TMS=0): Capture-DR (loads 32-bit IDCODE into shift register)
       - Pulse 4 (TMS=0): Shift-DR
    4. Shift out 32 bits of IDCODE:
       - 8 bits into R0 (IDCODE[7:0])
       - 8 bits into R1 (IDCODE[15:8])
       - 8 bits into R2 (IDCODE[23:16])
       - 8 bits into R3 (IDCODE[31:24])
       For bits 0..30: TMS=0. For bit 31: TMS=1 (exits to Exit1-DR).
    5. Return to Run-Test/Idle:
       - Pulse 1 (TMS=1): Update-DR
       - Pulse 2 (TMS=0): Run-Test/Idle
    6. Halt.
    """
    out_mask = (1 << tck_pin) | (1 << tms_pin) | (1 << tdi_pin)
    wait_delay = half_period - 1 if half_period > 1 else 0

    lines = [
        f"; JTAG 32-bit IDCODE Readout (TCK={tck_pin}, TMS={tms_pin}, TDI={tdi_pin}, TDO={tdo_pin})",
        f"GDIRI 0x{out_mask:02X}",       # TCK, TMS, TDI are outputs; TDO is input
        f"GWRI  0x{(1 << tms_pin):02X}", # TCK=0, TMS=1
        "WAIT  2",
        "",
        "; --- Step 1: 5 Pulses TMS=1 to force Test-Logic-Reset ---",
    ]

    # 5 pulses TMS=1
    for _ in range(5):
        lines.extend([
            f"GWRI  0x{((1 << tck_pin) | (1 << tms_pin)):02X}",  # TCK=1, TMS=1
            f"WAIT  {wait_delay}",
            f"GWRI  0x{(1 << tms_pin):02X}",                     # TCK=0, TMS=1
            f"WAIT  {wait_delay}",
        ])

    lines.extend([
        "",
        "; --- Step 2: Navigate to Shift-DR ---",
        "; Pulse 1: TLR -> Run-Test/Idle (TMS=0)",
        f"GWRI  0x{(1 << tck_pin):02X}",  # TCK=1, TMS=0
        f"WAIT  {wait_delay}",
        "GWRI  0x00",                     # TCK=0, TMS=0
        f"WAIT  {wait_delay}",
        "; Pulse 2: RTI -> Select-DR-Scan (TMS=1)",
        f"GWRI  0x{((1 << tck_pin) | (1 << tms_pin)):02X}",  # TCK=1, TMS=1
        f"WAIT  {wait_delay}",
        f"GWRI  0x{(1 << tms_pin):02X}",                     # TCK=0, TMS=1
        f"WAIT  {wait_delay}",
        "; Pulse 3: Select-DR -> Capture-DR (TMS=0)",
        f"GWRI  0x{(1 << tck_pin):02X}",  # TCK=1, TMS=0
        f"WAIT  {wait_delay}",
        "GWRI  0x00",                     # TCK=0, TMS=0
        f"WAIT  {wait_delay}",
        "; Pulse 4: Capture-DR -> Shift-DR (TMS=0)",
        f"GWRI  0x{(1 << tck_pin):02X}",  # TCK=1, TMS=0
        f"WAIT  {wait_delay}",
        "GWRI  0x00",                     # TCK=0, TMS=0
        f"WAIT  {wait_delay}",
        "",
        "; --- Step 3: Shift 32 bits into R0, R1, R2, R3 ---",
    ])

    regs = ["R0", "R1", "R2", "R3"]
    for byte_idx, reg in enumerate(regs):
        lines.append(f"; === Byte {byte_idx}: {reg} ===")
        for bit_i in range(8):
            bit_global = byte_idx * 8 + bit_i
            is_last = (bit_global == 31)
            tms_val = (1 << tms_pin) if is_last else 0
            tck_tms = (1 << tck_pin) | tms_val

            lines.extend([
                f"GWRI  0x{tck_tms:02X}",         # TCK=1 (rising edge, host samples TDO)
                f"SHIFTIN {reg}, {tdo_pin}",     # Sample TDO into register LSB-first
                f"GWRI  0x{tms_val:02X}",         # TCK=0 (falling edge, target drives next TDO)
                f"WAIT  {wait_delay}",
            ])

    lines.extend([
        "",
        "; --- Step 4: Return from Exit1-DR to Run-Test/Idle ---",
        "; Pulse 1: Exit1-DR -> Update-DR (TMS=1)",
        f"GWRI  0x{((1 << tck_pin) | (1 << tms_pin)):02X}",
        f"WAIT  {wait_delay}",
        f"GWRI  0x{(1 << tms_pin):02X}",
        f"WAIT  {wait_delay}",
        "; Pulse 2: Update-DR -> Run-Test/Idle (TMS=0)",
        f"GWRI  0x{(1 << tck_pin):02X}",
        f"WAIT  {wait_delay}",
        "GWRI  0x00",
        f"WAIT  {wait_delay}",
        "HALT",
        "",
    ])
    return "\n".join(lines)


def build_jtag_bypass_verify_asm(
    test_byte: int = 0xA5,
    tck_pin: int = 4,
    tms_pin: int = 5,
    tdi_pin: int = 6,
    tdo_pin: int = 7,
    half_period: int = 2,
) -> str:
    """Generate JTAG BYPASS verification firmware.

    Sequence:
    1. Reset TAP to TLR (5 pulses TMS=1).
    2. Navigate to Shift-IR:
       - TLR -> RTI (TMS=0)
       - RTI -> Select-DR-Scan (TMS=1)
       - Select-DR-Scan -> Select-IR-Scan (TMS=1)
       - Select-IR-Scan -> Capture-IR (TMS=0)
       - Capture-IR -> Shift-IR (TMS=0)
    3. Shift in 4-bit BYPASS instruction (0b1111) with TDI=1:
       - Bits 0, 1, 2: TMS=0, TDI=1
       - Bit 3: TMS=1, TDI=1 (Exit1-IR)
    4. Navigate to Shift-DR:
       - Exit1-IR -> Update-IR (TMS=1)
       - Update-IR -> Select-DR-Scan (TMS=1)
       - Select-DR-Scan -> Capture-DR (TMS=0)
       - Capture-DR -> Shift-DR (TMS=0)
    5. In Shift-DR (1-bit BYPASS register):
       - Shift 8 bits of test_byte through TDI.
       - Sample 8 bits of TDO into R0.
       - Since BYPASS register adds 1 TCK cycle of latency, the received bits
         in R0 will match test_byte delayed by 1 cycle!
    6. Halt.
    """
    out_mask = (1 << tck_pin) | (1 << tms_pin) | (1 << tdi_pin)
    wait_delay = half_period - 1 if half_period > 1 else 0

    lines = [
        f"; JTAG BYPASS Instruction & Data Verification (test_byte=0x{test_byte:02X})",
        f"GDIRI 0x{out_mask:02X}",
        f"GWRI  0x{(1 << tms_pin):02X}",
        "WAIT  2",
        "",
        "; --- Reset TAP to TLR ---",
    ]

    for _ in range(5):
        lines.extend([
            f"GWRI  0x{((1 << tck_pin) | (1 << tms_pin)):02X}",
            f"WAIT  {wait_delay}",
            f"GWRI  0x{(1 << tms_pin):02X}",
            f"WAIT  {wait_delay}",
        ])

    lines.extend([
        "",
        "; --- Navigate to Shift-IR ---",
        "; TLR -> RTI (TMS=0)",
        f"GWRI  0x{(1 << tck_pin):02X}",
        f"WAIT  {wait_delay}",
        "GWRI  0x00",
        f"WAIT  {wait_delay}",
        "; RTI -> Select-DR (TMS=1)",
        f"GWRI  0x{((1 << tck_pin) | (1 << tms_pin)):02X}",
        f"WAIT  {wait_delay}",
        f"GWRI  0x{(1 << tms_pin):02X}",
        f"WAIT  {wait_delay}",
        "; Select-DR -> Select-IR (TMS=1)",
        f"GWRI  0x{((1 << tck_pin) | (1 << tms_pin)):02X}",
        f"WAIT  {wait_delay}",
        f"GWRI  0x{(1 << tms_pin):02X}",
        f"WAIT  {wait_delay}",
        "; Select-IR -> Capture-IR (TMS=0)",
        f"GWRI  0x{(1 << tck_pin):02X}",
        f"WAIT  {wait_delay}",
        "GWRI  0x00",
        f"WAIT  {wait_delay}",
        "; Capture-IR -> Shift-IR (TMS=0)",
        f"GWRI  0x{(1 << tck_pin):02X}",
        f"WAIT  {wait_delay}",
        "GWRI  0x00",
        f"WAIT  {wait_delay}",
        "",
        "; --- Shift 4-bit BYPASS instruction (0b1111) with TDI=1 ---",
    ])

    # 3 bits with TMS=0, TDI=1
    for bit_i in range(3):
        lines.extend([
            f"GWRI  0x{((1 << tck_pin) | (1 << tdi_pin)):02X}",  # TCK=1, TDI=1, TMS=0
            f"WAIT  {wait_delay}",
            f"GWRI  0x{(1 << tdi_pin):02X}",                     # TCK=0, TDI=1, TMS=0
            f"WAIT  {wait_delay}",
        ])
    # 4th bit with TMS=1, TDI=1 (Exit1-IR)
    lines.extend([
        f"GWRI  0x{((1 << tck_pin) | (1 << tms_pin) | (1 << tdi_pin)):02X}",  # TCK=1, TDI=1, TMS=1
        f"WAIT  {wait_delay}",
        f"GWRI  0x{((1 << tms_pin) | (1 << tdi_pin)):02X}",                   # TCK=0, TDI=1, TMS=1
        f"WAIT  {wait_delay}",
        "",
        "; --- Navigate to Shift-DR ---",
        "; Exit1-IR -> Update-IR (TMS=1)",
        f"GWRI  0x{((1 << tck_pin) | (1 << tms_pin)):02X}",
        f"WAIT  {wait_delay}",
        f"GWRI  0x{(1 << tms_pin):02X}",
        f"WAIT  {wait_delay}",
        "; Update-IR -> Select-DR (TMS=1)",
        f"GWRI  0x{((1 << tck_pin) | (1 << tms_pin)):02X}",
        f"WAIT  {wait_delay}",
        f"GWRI  0x{(1 << tms_pin):02X}",
        f"WAIT  {wait_delay}",
        "; Select-DR -> Capture-DR (TMS=0)",
        f"GWRI  0x{(1 << tck_pin):02X}",
        f"WAIT  {wait_delay}",
        "GWRI  0x00",
        f"WAIT  {wait_delay}",
        "; Capture-DR -> Shift-DR (TMS=0)",
        f"GWRI  0x{(1 << tck_pin):02X}",
        f"WAIT  {wait_delay}",
        "GWRI  0x00",
        f"WAIT  {wait_delay}",
        "",
        "; --- Shift 8 bits of test_byte through BYPASS register ---",
    ])

    for bit_i in range(8):
        tdi_val = ((test_byte >> bit_i) & 1) << tdi_pin
        lines.extend([
            f"GWRI  0x{(tdi_val):02X}",                          # Set TDI before clock
            f"GWRI  0x{((1 << tck_pin) | tdi_val):02X}",          # TCK=1 (rising edge)
            f"SHIFTIN R0, {tdo_pin}",                             # Sample TDO
            f"GWRI  0x{(tdi_val):02X}",                          # TCK=0 (falling edge)
            f"WAIT  {wait_delay}",
        ])

    lines.extend([
        "HALT",
        "",
    ])
    return "\n".join(lines)
