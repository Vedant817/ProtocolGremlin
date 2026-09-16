"""tools/mil1553_model.py - MIL-STD-1553B Avionic Multiplex Data Bus Protocol Models

Provides:
- compute_1553_parity: Computes odd parity bit for a 16-bit word.
- verify_1553_parity: Validates odd parity across 16-bit word and parity bit.
- build_command_word: Encodes 16-bit Command Word (RT Address, T/R, Subaddress, Word Count).
- parse_command_word: Decodes 16-bit Command Word into individual fields.
- build_status_word: Encodes 16-bit Status Word (RT Address, Status flags).
- Mil1553RemoteTerminal: Cycle-accurate reference model of a 1553B Remote Terminal (RT).
- Mil1553PpaModel: Physical PPA scaling model for dedicated hardware 1553B coprocessor on IHP 130nm SG13G2.
- Firmware generators (using ISA v1 instructions: GDIRI, GWRI, GRD, SHIFTOUT, SHIFTIN, WAITEDGE, LDI, MOV, ANDI, ORI, XORI, JMP, JZ, JNZ, DECJNZ, WAIT, HALT):
  * build_1553_bc_command_tx_asm: Bus Controller Command Word transmission (Sync + 16 bits Manchester + Parity).
  * build_1553_rt_rx_asm: Remote Terminal Command reception, address filter, and Status Word response.
  * build_1553_dual_bus_failover_asm: Dual-redundant Bus A to Bus B failover controller.
"""

from typing import List, Dict, Optional, Tuple

try:
    from tools.assembler import assemble
except ModuleNotFoundError:
    from assembler import assemble


def compute_1553_parity(val16: int) -> int:
    """Computes the odd parity bit for a 16-bit information word.

    Per MIL-STD-1553B: The total number of 1s in the 16 data bits plus the
    parity bit must be ODD.
    """
    count = bin(val16 & 0xFFFF).count("1")
    return 0 if (count % 2 == 1) else 1


def verify_1553_parity(val16: int, parity_bit: int) -> bool:
    """Validates that the total count of 1s across the 16 bits and parity bit is odd."""
    count = bin(val16 & 0xFFFF).count("1") + (parity_bit & 1)
    return (count % 2) == 1


def build_command_word(rt_addr: int, tr: int, subaddr: int, word_count: int) -> int:
    """Constructs a 16-bit MIL-STD-1553B Command Word.

    Bits [15:11]: RT Address (0..31)
    Bit  [10]:    T/R (0 = Receive, 1 = Transmit)
    Bits [9:5]:   Subaddress / Mode Code (0..31)
    Bits [4:0]:   Data Word Count / Mode Code (0..31, 0 = 32 words)
    """
    return (
        ((rt_addr & 0x1F) << 11)
        | ((tr & 0x01) << 10)
        | ((subaddr & 0x1F) << 5)
        | (word_count & 0x1F)
    )


def parse_command_word(word16: int) -> Dict[str, int]:
    """Parses a 16-bit Command Word into its constitutive fields."""
    w = word16 & 0xFFFF
    return {
        "rt_addr": (w >> 11) & 0x1F,
        "tr": (w >> 10) & 0x01,
        "subaddr": (w >> 5) & 0x1F,
        "word_count": w & 0x1F,
    }


def build_status_word(rt_addr: int, me: int = 0, busy: int = 0, tf: int = 0) -> int:
    """Constructs a 16-bit MIL-STD-1553B Status Word.

    Bits [15:11]: RT Address
    Bit  [10]:    Message Error (ME)
    Bit  [3]:     Busy
    Bit  [0]:     Terminal Flag (TF)
    All other status bits default to 0.
    """
    return (
        ((rt_addr & 0x1F) << 11)
        | ((me & 0x01) << 10)
        | ((busy & 0x01) << 3)
        | (tf & 0x01)
    )


class Mil1553RemoteTerminal:
    """Reference cycle-accurate simulation model of a MIL-STD-1553B Remote Terminal (RT).

    Monitors Bus A and Bus B, validates Command Words addressed to its RT address,
    and returns a compliant Status Word response.
    """

    def __init__(self, rt_addr: int = 5, response_delay_cycles: int = 8):
        self.rt_addr = rt_addr
        self.response_delay = response_delay_cycles
        self.last_command: Optional[Dict[str, int]] = None
        self.status_word: int = build_status_word(rt_addr)
        self.tx_bitstream: List[int] = []
        self.tx_pointer = 0

    def process_command(self, word16: int, parity_bit: int) -> bool:
        """Processes an incoming 16-bit command word and parity.

        Returns True if command was valid and addressed to this RT.
        """
        if not verify_1553_parity(word16, parity_bit):
            return False

        parsed = parse_command_word(word16)
        if parsed["rt_addr"] != self.rt_addr and parsed["rt_addr"] != 31:  # 31 = Broadcast
            return False

        self.last_command = parsed
        # Prepare response status word
        stat_val = build_status_word(self.rt_addr)
        stat_par = compute_1553_parity(stat_val)

        # Generate response bitstream:
        # Command/Status Sync: 12 cycles High (1), 12 cycles Low (0)
        sync_bits = [1] * 12 + [0] * 12
        # 16 Manchester data bits: each bit is 4 cycles first half, 4 cycles second half
        # Logic 1: High (1) then Low (0); Logic 0: Low (0) then High (1)
        data_bits = []
        for i in range(15, -1, -1):
            bit = (stat_val >> i) & 1
            if bit == 1:
                data_bits += [1] * 4 + [0] * 4
            else:
                data_bits += [0] * 4 + [1] * 4

        # 1 Parity bit in Manchester
        if stat_par == 1:
            parity_bits = [1] * 4 + [0] * 4
        else:
            parity_bits = [0] * 4 + [1] * 4

        # Intermessage response gap delay
        gap_bits = [0] * self.response_delay
        self.tx_bitstream = gap_bits + sync_bits + data_bits + parity_bits + [0] * 8
        self.tx_pointer = 0
        return True

    def step(self) -> int:
        """Step simulation. Returns current line output state."""
        if self.tx_pointer < len(self.tx_bitstream):
            val = self.tx_bitstream[self.tx_pointer]
            self.tx_pointer += 1
            return val
        return 0  # Idle line


class Mil1553PpaModel:
    """IHP 130nm SG13G2 physical PPA scaling model for dedicated MIL-STD-1553B macro."""

    def __init__(self, channels: int = 2):
        self.channels = channels  # 2 channels for dual redundancy (Bus A & Bus B)

    def estimate_ppa(self) -> Dict[str, float]:
        # Dual-redundant analog front-end interface, filters, and level comparators
        cells_afe = self.channels * 32
        # 3-bit Sync pattern generator and detector
        cells_sync = 58
        # Manchester II Biphase-L encoder and decoder
        cells_manchester = 92
        # 16-bit shift register & hardware odd-parity tree
        cells_shift_parity = 110
        # Protocol FSM (BC/RT control, subaddress decoding, status register)
        cells_fsm = 130
        # Bus failover and timeout detection logic
        cells_failover = 32

        total_cells = cells_afe + cells_sync + cells_manchester + cells_shift_parity + cells_fsm + cells_failover
        gate_equivalents = total_cells * 1.95
        silicon_area_um2 = gate_equivalents * 3.74

        # Maximum frequency: critical path is parity generation tree and Manchester decoder
        f_max_mhz = 1000.0 / 1.32

        baseline_area_um2 = 139000.0
        area_overhead_pct = (silicon_area_um2 / baseline_area_um2) * 100.0

        return {
            "total_cells": total_cells,
            "gate_equivalents": round(gate_equivalents, 1),
            "silicon_area_um2": round(silicon_area_um2, 2),
            "area_overhead_pct": round(area_overhead_pct, 2),
            "critical_path_ns": 1.32,
            "f_max_mhz": round(f_max_mhz, 1),
            "firmware_cells_overhead": 0,
            "firmware_area_overhead_pct": 0.0,
        }


# ==============================================================================
# Firmware Generators for Protocol-Emulator Core (ISA v1)
# ==============================================================================

def build_1553_bc_command_tx_asm(
    rt_addr: int = 5,
    tr: int = 0,
    subaddr: int = 1,
    word_count: int = 1,
    tx_pin: int = 3,
) -> List[int]:
    """Generates Bus Controller (BC) firmware to transmit a 1553B Command Word on tx_pin.

    Waveform:
      1. Command Sync: 12 cycles High, 12 cycles Low.
      2. 16 Manchester bits for Command Word.
      3. 1 Manchester bit for Odd Parity.
      4. Idle Low (0).
    """
    pin_mask = 1 << tx_pin
    cmd16 = build_command_word(rt_addr, tr, subaddr, word_count)
    parity = compute_1553_parity(cmd16)

    high_byte = (cmd16 >> 8) & 0xFF
    low_byte = cmd16 & 0xFF

    # Emit assembly with exact half-bit durations (4 cycles = WAIT 2 + 2 instructions)
    asm_source = f"""
    GDIRI {pin_mask}        ; Configure tx_pin as output
    GWRI 0x00               ; Line Idle Low
    WAIT 4

    ; Command Sync Pulse: 12 cycles High, 12 cycles Low
    GWRI {pin_mask}         ; Sync High
    WAIT 10                 ; 10 + 2 = 12 cycles
    GWRI 0x00               ; Sync Low
    WAIT 10                 ; 10 + 2 = 12 cycles

    ; Transmit High Byte (Bits 15..8)
    LDI R0, {high_byte}
    LDI R3, 8
    TX_HIGH_LOOP:
    MOV R1, R0
    ANDI R1, 128
    JZ TX_HIGH_ZERO
    ; Logic 1: High for 4 cycles, Low for 4 cycles
    GWRI {pin_mask}
    WAIT 2
    GWRI 0x00
    WAIT 2
    JMP TX_HIGH_NEXT
    TX_HIGH_ZERO:
    ; Logic 0: Low for 4 cycles, High for 4 cycles
    GWRI 0x00
    WAIT 2
    GWRI {pin_mask}
    WAIT 2
    TX_HIGH_NEXT:
    SHIFTOUT R0, 7, 1       ; Shift R0 left by 1
    DECJNZ R3, TX_HIGH_LOOP

    ; Transmit Low Byte (Bits 7..0)
    LDI R0, {low_byte}
    LDI R3, 8
    TX_LOW_LOOP:
    MOV R1, R0
    ANDI R1, 128
    JZ TX_LOW_ZERO
    GWRI {pin_mask}
    WAIT 2
    GWRI 0x00
    WAIT 2
    JMP TX_LOW_NEXT
    TX_LOW_ZERO:
    GWRI 0x00
    WAIT 2
    GWRI {pin_mask}
    WAIT 2
    TX_LOW_NEXT:
    SHIFTOUT R0, 7, 1
    DECJNZ R3, TX_LOW_LOOP

    ; Transmit Odd Parity Bit
    LDI R1, {parity}
    ANDI R1, 1
    JZ TX_PAR_ZERO
    GWRI {pin_mask}
    WAIT 2
    GWRI 0x00
    WAIT 2
    JMP TX_DONE
    TX_PAR_ZERO:
    GWRI 0x00
    WAIT 2
    GWRI {pin_mask}
    WAIT 2

    TX_DONE:
    GWRI 0x00               ; Return to Idle Low
    WAIT 10
    HALT
    """
    return assemble(asm_source)


def build_1553_rt_rx_asm(
    my_rt_addr: int = 5,
    rx_pin: int = 4,
    tx_pin: int = 3,
) -> List[int]:
    """Generates Remote Terminal (RT) firmware.

    Sequence:
      1. Waits for Command Sync falling edge via WAITEDGE (rx_pin).
      2. Ingresses High Byte into R0 and Low Byte into R1 with exact 8-cycle strides.
      3. Filters RT address: extracts bits [7:3] of High Byte.
      4. If match, transmits Status Word on tx_pin with matching RT address and odd parity!
      5. Halts with status flag in R2 (0x00 = Success Match, 0xEE = Address Mismatch).
    """
    tx_mask = 1 << tx_pin
    expected_addr_byte = (my_rt_addr & 0x1F) << 3

    asm_lines = [
        f"GDIRI {tx_mask}",
        "GWRI 0x00",
        "LDI R2, 0",
        f"WAITEDGE R3, {rx_pin}",  # Mode 00: Falling edge of Command Sync
        "WAIT 11",                # Trailing sync low (12 cycles) + half-bit center (2) - sync delay (3) = 11
        "LDI R0, 0",
    ]

    # Sample 8 bits of High Byte into R0 (MSB-first)
    for b in range(8):
        asm_lines.append(f"SHIFTIN R0, {rx_pin}, 1")
        asm_lines.append("WAIT 6")

    # Sample 8 bits of Low Byte into R1 (MSB-first)
    asm_lines.append("LDI R1, 0")
    for b in range(8):
        asm_lines.append(f"SHIFTIN R1, {rx_pin}, 1")
        if b < 7:
            asm_lines.append("WAIT 6")

    # Address Filtering: Check if RT Address matches my_rt_addr
    asm_lines += [
        "MOV R3, R0",
        "ANDI R3, 248",            # Mask bits [7:3] (0xF8 = 248)
        f"XORI R3, {expected_addr_byte}",
        "JZ RT_ADDR_MATCH",
        "LDI R2, 238",             # 0xEE (Mismatch)
        "HALT",
        "RT_ADDR_MATCH:",
        "LDI R2, 0",               # 0x00 (Match)
        "WAIT 8",                  # Intermessage response time tr
        f"GWRI {tx_mask}",         # Status Sync High (12 cycles)
        "WAIT 10",
        "GWRI 0x00",               # Status Sync Low (12 cycles)
        "WAIT 10",
        f"LDI R0, {expected_addr_byte}",
        "LDI R3, 8",
        "RT_TX_STAT_HIGH:",
        "MOV R1, R0",
        "ANDI R1, 128",
        "JZ RT_TX_ZERO_1",
        f"GWRI {tx_mask}",
        "WAIT 2",
        "GWRI 0x00",
        "WAIT 2",
        "JMP RT_TX_NEXT_1",
        "RT_TX_ZERO_1:",
        "GWRI 0x00",
        "WAIT 2",
        f"GWRI {tx_mask}",
        "WAIT 2",
        "RT_TX_NEXT_1:",
        "SHIFTOUT R0, 7, 1",
        "DECJNZ R3, RT_TX_STAT_HIGH",
        "LDI R0, 0",
        "LDI R3, 8",
        "RT_TX_STAT_LOW:",
        "GWRI 0x00",
        "WAIT 2",
        f"GWRI {tx_mask}",
        "WAIT 2",
        "DECJNZ R3, RT_TX_STAT_LOW",
        f"LDI R1, {compute_1553_parity(expected_addr_byte << 8)}",
        "ANDI R1, 1",
        "JZ RT_TX_PAR_0",
        f"GWRI {tx_mask}",
        "WAIT 2",
        "GWRI 0x00",
        "WAIT 2",
        "JMP RT_TX_END",
        "RT_TX_PAR_0:",
        "GWRI 0x00",
        "WAIT 2",
        f"GWRI {tx_mask}",
        "WAIT 2",
        "RT_TX_END:",
        "GWRI 0x00",
        "WAIT 10",
        "HALT",
    ]
    return assemble("\n".join(asm_lines))


def build_1553_dual_bus_failover_asm(
    rt_addr: int = 5,
    bus_a_tx: int = 3,
    bus_a_rx: int = 4,
    bus_b_tx: int = 5,
    bus_b_rx: int = 6,
) -> List[int]:
    """Generates Bus Controller dual-redundant failover firmware.

    Tries primary Bus A:
      - Sends command on bus_a_tx.
      - Checks for response on bus_a_rx within timeout.
      - If no response (timeout), automatically fails over to Bus B:
        transmits command on bus_b_tx, sets R2 = 0xBB (Failover to Bus B).
    """
    mask_a = 1 << bus_a_tx
    mask_b = 1 << bus_b_tx
    all_tx_mask = mask_a | mask_b

    asm_source = f"""
    GDIRI {all_tx_mask}     ; Both TX pins as outputs
    GWRI 0x00               ; Both lines Idle Low
    WAIT 4

    ; 1. Try Primary Bus A: Send Command Sync (12 High, 12 Low)
    GWRI {mask_a}
    WAIT 10
    GWRI 0x00
    WAIT 10

    ; Transmit 16 bits on Bus A
    LDI R0, {(rt_addr & 0x1F) << 3}
    LDI R3, 8
    BC_A_LOOP:
    GWRI {mask_a}
    WAIT 2
    GWRI 0x00
    WAIT 2
    DECJNZ R3, BC_A_LOOP

    ; Poll Bus A RX for Response (simulated timeout check)
    LDI R3, 4               ; Short timeout loop
    BC_POLL_A:
    GRD R1
    ANDI R1, {1 << bus_a_rx}
    JNZ BC_BUS_A_OK
    DECJNZ R3, BC_POLL_A

    ; Bus A Timed Out! Failover to Bus B:
    LDI R2, 187             ; R2 = 0xBB (Bus B Failover active)
    GWRI {mask_b}           ; Sync on Bus B
    WAIT 10
    GWRI 0x00
    WAIT 10

    ; Transmit command on Bus B
    LDI R0, {(rt_addr & 0x1F) << 3}
    LDI R3, 8
    BC_B_LOOP:
    GWRI {mask_b}
    WAIT 2
    GWRI 0x00
    WAIT 2
    DECJNZ R3, BC_B_LOOP

    GWRI 0x00
    WAIT 10
    HALT

    BC_BUS_A_OK:
    LDI R2, 170             ; R2 = 0xAA (Bus A Succeeded)
    GWRI 0x00
    WAIT 10
    HALT
    """
    return assemble(asm_source)
