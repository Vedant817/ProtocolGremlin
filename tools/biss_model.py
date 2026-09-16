"""tools/biss_model.py - SSI & BiSS-C Absolute Rotary Encoder Protocol Models

Provides:
- gray_to_binary: Decodes reflected binary (Gray code) to standard binary.
- binary_to_gray: Encodes standard binary to reflected binary (Gray code).
- compute_biss_crc6: Computes 6-bit inverted CRC (polynomial P(x) = x^6 + x + 1, 0x43).
- verify_biss_crc6: Validates CRC-6 for a stream of data and status bits.
- SsiEncoderModel: Cycle-accurate reference model of an SSI absolute optical/magnetic encoder.
- BissEncoderModel: Cycle-accurate reference model of a BiSS-C slave absolute encoder.
- BissPpaModel: Physical PPA scaling model for dedicated hardware SSI/BiSS-C peripheral on IHP 130nm SG13G2.
- Firmware generators (using ISA v1 instructions: GDIRI, GWRI, GRD, SHIFTIN, LDI, MOV, ANDI, ORI, XORI, JMP, JZ, JNZ, DECJNZ, WAIT, HALT):
  * build_ssi_gray_to_binary_asm: ALU-based Gray-to-Binary decoder (R0 -> R2).
  * build_ssi_master_asm: Bit-serial synchronous SSI master engine sampling SLO on MA clock.
  * build_biss_master_asm: Full BiSS-C master frame acquisition (Ack, Start, Position R0, Flags R1, CRC R2).
"""

from typing import List, Dict, Optional, Tuple

try:
    from tools.assembler import assemble
except ModuleNotFoundError:
    from assembler import assemble


def gray_to_binary(gray: int, bits: int = 8) -> int:
    """Decodes an N-bit reflected Gray code integer to standard binary.

    Mathematical identity:
      b_i = XOR_{k=i}^{N-1} g_k
    """
    binary = 0
    running_parity = 0
    for i in range(bits - 1, -1, -1):
        g_bit = (gray >> i) & 1
        running_parity ^= g_bit
        if running_parity:
            binary |= (1 << i)
    return binary


def binary_to_gray(binary: int, bits: int = 8) -> int:
    """Encodes an N-bit standard binary integer to reflected Gray code."""
    mask = (1 << bits) - 1
    return (binary ^ (binary >> 1)) & mask


def compute_biss_crc6(bits: List[int]) -> int:
    """Computes the 6-bit BiSS-C Cyclic Redundancy Check (CRC-6).

    Polynomial: P(x) = x^6 + x^1 + 1 (0x43).
    Bit order: Input stream processed bit-by-bit MSB first.
    Final output: Inverted (crc ^ 0x3F).
    """
    crc = 0
    for b in bits:
        inv = (b & 1) ^ ((crc >> 5) & 1)
        crc = ((crc << 1) & 0x3F) ^ (0x03 if inv else 0x00)
    return (crc ^ 0x3F) & 0x3F


def verify_biss_crc6(bits: List[int], expected_crc: int) -> bool:
    """Verifies that the computed CRC-6 matches the expected 6-bit CRC."""
    calc_crc = compute_biss_crc6(bits)
    return (calc_crc & 0x3F) == (expected_crc & 0x3F)


class SsiEncoderModel:
    """Simulates an SSI (Synchronous Serial Interface) absolute rotary encoder.

    Pin assignment convention:
      MA  (Clock): Input to encoder (driven by master)
      SLO (Data):  Output from encoder (read by master)
    """

    def __init__(self, position: int = 0xAB, bits: int = 8, gray_mode: bool = True):
        self.bits = bits
        self.gray_mode = gray_mode
        self.set_position(position)
        self.ma_prev = 1
        self.bit_idx = 0
        self.idle = True

    def set_position(self, position: int):
        self.position = position & ((1 << self.bits) - 1)
        if self.gray_mode:
            self.encoded_val = binary_to_gray(self.position, self.bits)
        else:
            self.encoded_val = self.position

    def step(self, ma_in: int) -> int:
        """Step simulation on MA clock edge. Returns current SLO output bit."""
        ma = 1 if ma_in else 0
        falling_edge = (self.ma_prev == 1 and ma == 0)
        self.ma_prev = ma

        if falling_edge:
            if self.idle:
                # First clock pulse initiates transmission: latch position and present bit 0 (MSB)
                self.idle = False
                self.bit_idx = 0
            else:
                # Subsequent clock pulses shift out the next bits
                self.bit_idx += 1
                if self.bit_idx >= self.bits:
                    self.idle = True

        # While active, drive the current bit MSB-first
        if not self.idle and self.bit_idx < self.bits:
            shift = (self.bits - 1) - self.bit_idx
            return (self.encoded_val >> shift) & 1
        return 1  # Idle High


class BissEncoderModel:
    """Simulates a BiSS-C Point-to-Point slave absolute encoder.

    Transmits Single Cycle Data (SCD):
      1. Ack: Slave pulls SLO LOW (0).
      2. Start bit: Slave drives SLO HIGH (1).
      3. CDS bit: Control Data Slave (0 or 1).
      4. Position Data: N bits, MSB first.
      5. Error bit (nE): Active low (0 = error, 1 = OK).
      6. Warning bit (nW): Active low (0 = warning, 1 = OK).
      7. CRC-6: 6 bits inverted CRC.
      8. Timeout: SLO driven LOW then released HIGH.
    """

    def __init__(
        self,
        position: int = 0x5A,
        bits: int = 8,
        error: bool = False,
        warning: bool = False,
        cds: int = 0,
    ):
        self.position = position & ((1 << bits) - 1)
        self.bits = bits
        self.error = error
        self.warning = warning
        self.cds = cds & 1

        # Format bit sequence
        self.nE = 0 if error else 1
        self.nW = 0 if warning else 1

        # Position bits (MSB first)
        pos_bits = [(self.position >> i) & 1 for i in range(self.bits - 1, -1, -1)]

        # Protected bits for CRC: Position + nE + nW
        crc_input_bits = pos_bits + [self.nE, self.nW]
        self.crc6 = compute_biss_crc6(crc_input_bits)
        crc_bits = [(self.crc6 >> i) & 1 for i in range(5, -1, -1)]

        # Complete frame sequence: Ack (0), Start (1), CDS, Pos, nE, nW, CRC
        self.frame_bits = [0, 1, self.cds] + pos_bits + [self.nE, self.nW] + crc_bits
        self.bit_pointer = 0
        self.ma_prev = 1

    def step(self, ma_in: int) -> int:
        """Step simulation on MA clock edge. Returns current SLO pin value."""
        ma = 1 if ma_in else 0
        falling_edge = (self.ma_prev == 1 and ma == 0)
        self.ma_prev = ma

        if falling_edge:
            if self.bit_pointer < len(self.frame_bits):
                bit = self.frame_bits[self.bit_pointer]
                self.bit_pointer += 1
                return bit
            return 1  # Idle High
        elif self.bit_pointer > 0 and self.bit_pointer <= len(self.frame_bits):
            return self.frame_bits[self.bit_pointer - 1]
        return 1


class BissPpaModel:
    """IHP 130nm SG13G2 PPA model for dedicated hardware BiSS-C/SSI peripheral."""

    def __init__(self, position_bits: int = 16, baud_prescaler_bits: int = 8):
        self.position_bits = position_bits
        self.baud_prescaler_bits = baud_prescaler_bits

    def estimate_ppa(self) -> Dict[str, float]:
        # Dual-edge MA clock generator & prescaler
        cells_clk = self.baud_prescaler_bits * 7 + 18
        # BiSS-C Framing FSM (Ack, Start, CDS, Data, Status, CRC, Timeout)
        cells_fsm = 88
        # Shift register for Position + Status + CRC
        total_rx_bits = self.position_bits + 2 + 6
        cells_sreg = total_rx_bits * 6
        # Hardware CRC-6 LFSR (6 flip-flops + XOR feedback network)
        cells_crc6 = 6 * 7 + 12
        # Hardware Gray-to-Binary combinatorial decoder
        cells_gray = self.position_bits * 4
        # Control & Status Registers (CSR)
        cells_csr = 42

        total_cells = cells_clk + cells_fsm + cells_sreg + cells_crc6 + cells_gray + cells_csr
        gate_equivalents = total_cells * 1.95
        silicon_area_um2 = gate_equivalents * 3.74

        # Maximum frequency: critical path is through CRC-6 LFSR XOR tree and clock prescaler
        f_max_mhz = 1000.0 / (0.42 + 0.12 * 6 + 0.14)

        baseline_area_um2 = 139000.0
        area_overhead_pct = (silicon_area_um2 / baseline_area_um2) * 100.0

        return {
            "total_cells": total_cells,
            "gate_equivalents": round(gate_equivalents, 1),
            "silicon_area_um2": round(silicon_area_um2, 2),
            "area_overhead_pct": round(area_overhead_pct, 2),
            "critical_path_ns": round(0.42 + 0.12 * 6 + 0.14, 2),
            "f_max_mhz": round(f_max_mhz, 1),
            "firmware_cells_overhead": 0,
            "firmware_area_overhead_pct": 0.0,
        }


# ==============================================================================
# Firmware Generators for Protocol-Emulator Core (ISA v1)
# ==============================================================================

def build_ssi_gray_to_binary_asm(gray_val: int) -> List[int]:
    """Generates firmware to decode an 8-bit Gray code value in R0 into binary in R2.

    Uses running XOR parity accumulation across all 8 bit positions.
    """
    asm_source = f"""
    LDI R0, {gray_val & 0xFF}  ; Load Gray code test value into R0
    LDI R2, 0                  ; R2 = Binary accumulator
    LDI R3, 0                  ; R3 = Running parity

    ; Bit 7 (0x80)
    MOV R1, R0
    ANDI R1, 128
    JZ G7_0
    XORI R3, 1
    G7_0:
    MOV R1, R3
    ANDI R1, 1
    JZ G7_DONE
    ORI R2, 128
    G7_DONE:

    ; Bit 6 (0x40)
    MOV R1, R0
    ANDI R1, 64
    JZ G6_0
    XORI R3, 1
    G6_0:
    MOV R1, R3
    ANDI R1, 1
    JZ G6_DONE
    ORI R2, 64
    G6_DONE:

    ; Bit 5 (0x20)
    MOV R1, R0
    ANDI R1, 32
    JZ G5_0
    XORI R3, 1
    G5_0:
    MOV R1, R3
    ANDI R1, 1
    JZ G5_DONE
    ORI R2, 32
    G5_DONE:

    ; Bit 4 (0x10)
    MOV R1, R0
    ANDI R1, 16
    JZ G4_0
    XORI R3, 1
    G4_0:
    MOV R1, R3
    ANDI R1, 1
    JZ G4_DONE
    ORI R2, 16
    G4_DONE:

    ; Bit 3 (0x08)
    MOV R1, R0
    ANDI R1, 8
    JZ G3_0
    XORI R3, 1
    G3_0:
    MOV R1, R3
    ANDI R1, 1
    JZ G3_DONE
    ORI R2, 8
    G3_DONE:

    ; Bit 2 (0x04)
    MOV R1, R0
    ANDI R1, 4
    JZ G2_0
    XORI R3, 1
    G2_0:
    MOV R1, R3
    ANDI R1, 1
    JZ G2_DONE
    ORI R2, 4
    G2_DONE:

    ; Bit 1 (0x02)
    MOV R1, R0
    ANDI R1, 2
    JZ G1_0
    XORI R3, 1
    G1_0:
    MOV R1, R3
    ANDI R1, 1
    JZ G1_DONE
    ORI R2, 2
    G1_DONE:

    ; Bit 0 (0x01)
    MOV R1, R0
    ANDI R1, 1
    JZ G0_0
    XORI R3, 1
    G0_0:
    MOV R1, R3
    ANDI R1, 1
    JZ G0_DONE
    ORI R2, 1
    G0_DONE:

    HALT
    """
    return assemble(asm_source)


def build_ssi_master_asm(clk_pin: int = 3, data_pin: int = 4, num_bits: int = 8) -> List[int]:
    """Generates SSI master firmware that clocks MA on clk_pin and reads SLO on data_pin.

    Ingresses num_bits into R0 (MSB-first).
    """
    clk_mask = 1 << clk_pin
    asm_source = f"""
    GDIRI {clk_mask}        ; Configure clk_pin as output, all others input
    GWRI {clk_mask}         ; MA Idle High
    WAIT 4
    LDI R0, 0               ; R0 = received position word
    LDI R3, {num_bits}      ; Bit counter

    SSI_BIT_LOOP:
    GWRI 0x00               ; Drive MA Low (falling edge: encoder latches position)
    WAIT 2
    GWRI {clk_mask}         ; Drive MA High (rising edge: encoder shifts out bit)
    WAIT 2
    SHIFTIN R0, {data_pin}, 1 ; Sample SLO into R0 (MSB mode)
    DECJNZ R3, SSI_BIT_LOOP ; Repeat for all bits

    GWRI {clk_mask}         ; Hold MA High
    WAIT 10                 ; Monoflop dwell timeout tm
    HALT
    """
    return assemble(asm_source)


def build_biss_master_asm(clk_pin: int = 3, data_pin: int = 4) -> List[int]:
    """Generates BiSS-C master firmware.

    Sequence:
      1. Toggles MA until Ack (SLO=0) detected.
      2. Toggles MA until Start (SLO=1) detected.
      3. Clocks CDS bit.
      4. Clocks 8 position bits into R0 (MSB first).
      5. Clocks nE and nW bits into R1[1:0].
      6. Clocks 6 CRC bits into R2[5:0].
      7. Holds MA High and Halts.
    """
    clk_mask = 1 << clk_pin
    data_mask = 1 << data_pin
    asm_source = f"""
    GDIRI {clk_mask}        ; Pin clk_pin output, others input
    GWRI {clk_mask}         ; MA High
    WAIT 2

    ; Synchronize: Clock MA until Ack (SLO = 0) is detected
    BISS_WAIT_ACK:
    GWRI 0x00               ; MA Low
    WAIT 2
    GWRI {clk_mask}         ; MA High
    WAIT 1
    GRD R1                  ; Read GPIO
    ANDI R1, {data_mask}    ; Mask SLO
    JNZ BISS_WAIT_ACK       ; If SLO != 0, keep waiting for Ack

    ; Synchronize: Clock MA until Start bit (SLO = 1) is detected
    BISS_WAIT_START:
    GWRI 0x00               ; MA Low
    WAIT 2
    GWRI {clk_mask}         ; MA High
    WAIT 1
    GRD R1
    ANDI R1, {data_mask}
    JZ BISS_WAIT_START      ; If SLO == 0, keep waiting for Start

    ; CDS (Control Data Slave) bit: 1 clock cycle
    GWRI 0x00
    WAIT 2
    GWRI {clk_mask}
    WAIT 1

    ; Position Data: 8 bits into R0
    LDI R0, 0
    LDI R3, 8
    BISS_POS_LOOP:
    GWRI 0x00
    WAIT 2
    GWRI {clk_mask}
    WAIT 1
    SHIFTIN R0, {data_pin}, 1
    DECJNZ R3, BISS_POS_LOOP

    ; Status Flags: nE and nW into R1
    LDI R1, 0
    ; Clock nE
    GWRI 0x00
    WAIT 2
    GWRI {clk_mask}
    WAIT 1
    SHIFTIN R1, {data_pin}, 1
    ; Clock nW
    GWRI 0x00
    WAIT 2
    GWRI {clk_mask}
    WAIT 1
    SHIFTIN R1, {data_pin}, 1

    ; CRC-6: 6 bits into R2
    LDI R2, 0
    LDI R3, 6
    BISS_CRC_LOOP:
    GWRI 0x00
    WAIT 2
    GWRI {clk_mask}
    WAIT 1
    SHIFTIN R2, {data_pin}, 1
    DECJNZ R3, BISS_CRC_LOOP

    GWRI {clk_mask}         ; Hold MA High
    WAIT 10                 ; Timeout tm
    HALT
    """
    return assemble(asm_source)
