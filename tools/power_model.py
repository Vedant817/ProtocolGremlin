# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
power_model.py - Cycle-accurate micro-architectural power and energy profiling
model for the Jane Street Protocol Emulator ASIC on IHP 130nm CMOS5L (SG13G2).

Quantifies:
1. Dynamic switching power (P_dyn = alpha * C_L * Vdd^2 * f_clk) per opcode.
2. Internal cell short-circuit and toggle dissipation.
3. Clock tree distribution network power across 4,240+ flip-flops.
4. Program RAM flip-flop power (4,096 DFFs ungated vs clock-gated).
5. GPIO external capacitive pad driving energy (E = 0.5 * C_pad * V^2 * N_trans).
6. Static subthreshold and gate leakage power (P_leak).
7. Protocol-level energy efficiency (pJ/bit and pJ/instruction).
"""

from typing import Dict, Any, List


# ---------------------------------------------------------------------------
# Physical Technology Constants: IHP 130nm SG13G2 CMOS5L
# ---------------------------------------------------------------------------
VDD_CORE_NOMINAL = 1.2        # Core supply voltage (Volts)
VDD_IO_NOMINAL = 3.3          # I/O pad supply voltage (Volts)
F_CLK_NOMINAL = 10_000_000    # Nominal system clock (10 MHz, T = 100 ns)

# Standard Cell Physical Parameters
C_GATE_AVG = 2.5e-15          # Average logic gate input capacitance (2.5 fF)
C_DFF_CLK = 3.8e-15           # D-Flip-Flop clock pin capacitance (3.8 fF)
C_PAD_DEFAULT = 20.0e-12      # External PCB trace + receiver pin capacitance (20 pF)
I_LEAK_PER_CELL = 25.0e-12    # Average static cell leakage current at 25°C (25 pA)

# ASIC Standard Cell Inventory (from Yosys synthesis mapping)
TOTAL_CELLS_BASELINE = 19291  # Total synthesized standard cells
RAM_FF_COUNT = 4096           # Program RAM 256 words x 16 bits = 4,096 DFFs
CORE_FF_COUNT = 144           # Architectural registers + PC + cycle_cnt + GPIO state
CLOCK_BUFFER_OVERHEAD = 1.30  # Clock tree buffering capacitance overhead (+30%)

# Energy per toggle (Joules)
E_GATE_TOGGLE = 0.5 * C_GATE_AVG * (VDD_CORE_NOMINAL ** 2) + 1.5e-15  # Dynamic + internal toggle
E_DFF_CLK_CYCLE = C_DFF_CLK * (VDD_CORE_NOMINAL ** 2)                 # Clock pin cycle energy


class PowerModel:
    """
    Cycle-accurate power and energy profiling model for the protocol emulator ASIC.
    """

    def __init__(
        self,
        vdd_core: float = VDD_CORE_NOMINAL,
        vdd_io: float = VDD_IO_NOMINAL,
        f_clk: float = F_CLK_NOMINAL,
        c_pad: float = C_PAD_DEFAULT,
    ):
        self.vdd_core = vdd_core
        self.vdd_io = vdd_io
        self.f_clk = f_clk
        self.c_pad = c_pad

        # Calculated physical constants
        self.e_gate_toggle = 0.5 * C_GATE_AVG * (self.vdd_core ** 2) + 1.5e-15
        self.e_dff_clk = C_DFF_CLK * (self.vdd_core ** 2)
        self.e_pad_transition = 0.5 * self.c_pad * (self.vdd_io ** 2)
        self.p_leak_total = TOTAL_CELLS_BASELINE * I_LEAK_PER_CELL * self.vdd_core

        # Cycle & state counters
        self.total_cycles = 0
        self.active_cycles = 0
        self.wait_stall_cycles = 0
        self.edge_stall_cycles = 0
        self.halt_cycles = 0

        # Activity counters
        self.alu_ops = 0
        self.reg_writes = 0
        self.branch_ops = 0
        self.shift_ops = 0
        self.gpio_internal_ops = 0
        self.pin_transitions = 0
        self.prev_pin_out = 0

        # Opcode breakdown
        self.opcode_counts: Dict[str, int] = {}

    def reset_counters(self):
        """Reset profiling counters."""
        self.total_cycles = 0
        self.active_cycles = 0
        self.wait_stall_cycles = 0
        self.edge_stall_cycles = 0
        self.halt_cycles = 0
        self.alu_ops = 0
        self.reg_writes = 0
        self.branch_ops = 0
        self.shift_ops = 0
        self.gpio_internal_ops = 0
        self.pin_transitions = 0
        self.prev_pin_out = 0
        self.opcode_counts.clear()

    def sample_cycle(
        self,
        opcode_name: str = "NOP",
        is_wait_stall: bool = False,
        is_edge_stall: bool = False,
        is_halt: bool = False,
        pin_out: int = 0,
        pin_oe: int = 0,
        reg_written: bool = False,
    ):
        """
        Record architectural activity for a single clock cycle.
        """
        self.total_cycles += 1

        if is_halt:
            self.halt_cycles += 1
            return
        elif is_wait_stall:
            self.wait_stall_cycles += 1
        elif is_edge_stall:
            self.edge_stall_cycles += 1
        else:
            self.active_cycles += 1
            self.opcode_counts[opcode_name] = self.opcode_counts.get(opcode_name, 0) + 1

            if opcode_name in ("ADDI", "SUBI", "ANDI", "ORI", "XORI", "DECJNZ"):
                self.alu_ops += 1
            elif opcode_name in ("JMP", "JZ", "JNZ"):
                self.branch_ops += 1
            elif opcode_name in ("SHIFTOUT", "SHIFTIN"):
                self.shift_ops += 1
            elif opcode_name in ("GWRI", "GWR", "GODRI", "GODR", "GDIRI", "GDIR"):
                self.gpio_internal_ops += 1

            if reg_written or opcode_name in ("LDI", "MOV", "ADDI", "SUBI", "ANDI", "ORI", "XORI", "GRD", "DECJNZ"):
                self.reg_writes += 1

        # Track external pin switching (only enabled output pins dissipate pad energy)
        active_pins_now = pin_out & pin_oe
        prev_active_pins = self.prev_pin_out & pin_oe
        diff = active_pins_now ^ prev_active_pins
        if diff:
            # Count toggled bits
            bit_toggles = bin(diff).count("1")
            self.pin_transitions += bit_toggles
        self.prev_pin_out = pin_out

    def compute_power_breakdown(self, clock_gating: bool = False) -> Dict[str, Any]:
        """
        Compute power and energy breakdown for the observed execution trace.
        If clock_gating is True, applies integrated clock gating on:
          1. Program RAM write clock during user execution (saves 4,096 DFF toggles/cycle).
          2. Core datapath and register file during WAIT/WAITEDGE/HALT stalls.
        """
        if self.total_cycles == 0:
            duration_sec = 0.0
        else:
            duration_sec = self.total_cycles / self.f_clk

        # --- 1. Clock Tree Network Power ---
        # Ungated: all 4,240 flip-flops toggle clock input pins every cycle
        # Gated: RAM DFFs (4,096) clock-gated during execution.
        # Core DFFs (144) gated during wait/halt stalls.
        if not clock_gating:
            avg_dff_toggles_per_cycle = (RAM_FF_COUNT + CORE_FF_COUNT) * CLOCK_BUFFER_OVERHEAD
        else:
            # RAM DFFs are 100% gated during execution (LD_DONE)
            # Core DFFs are active only during active cycles + counters during stalls
            active_core_ratio = (self.active_cycles + 0.15 * (self.wait_stall_cycles + self.edge_stall_cycles)) / max(1, self.total_cycles)
            avg_dff_toggles_per_cycle = (CORE_FF_COUNT * active_core_ratio + 4.0) * CLOCK_BUFFER_OVERHEAD  # 4 for ICG latches

        e_clk_tree = avg_dff_toggles_per_cycle * self.e_dff_clk * self.total_cycles
        p_clk_tree_uw = (e_clk_tree / max(1e-12, duration_sec)) * 1e6

        # --- 2. Program RAM Array Power ---
        # Read Mux Switching: Active only when PC changes (active cycles + branches)
        # In wait stalls, PC is frozen, RAM muxes are static!
        ram_mux_toggles = self.active_cycles * 128  # ~128 internal mux gate toggles per sequential fetch
        e_ram_mux = ram_mux_toggles * self.e_gate_toggle
        p_ram_uw = (e_ram_mux / max(1e-12, duration_sec)) * 1e6

        # --- 3. Core Datapath & Logic Power ---
        # ALU: ~16 gate toggles per ALU operation (carry chain ripple)
        # Registers: ~32 gate toggles per 8-bit register writeback
        # Decoder: ~24 gate toggles per active instruction
        # Stalls: ~8 gate toggles for downcounter/edge detector
        datapath_toggles = (
            self.alu_ops * 16
            + self.reg_writes * 32
            + self.active_cycles * 24
            + (self.wait_stall_cycles + self.edge_stall_cycles) * 8
        )
        e_datapath = datapath_toggles * self.e_gate_toggle
        p_core_datapath_uw = (e_datapath / max(1e-12, duration_sec)) * 1e6

        # --- 4. External GPIO Pad Driving Power ---
        e_gpio_pads = self.pin_transitions * self.e_pad_transition
        p_gpio_pads_uw = (e_gpio_pads / max(1e-12, duration_sec)) * 1e6

        # --- 5. Static Leakage Power ---
        p_leakage_uw = self.p_leak_total * 1e6
        e_leakage = self.p_leak_total * duration_sec

        # --- Total Power and Energy ---
        e_total = e_clk_tree + e_ram_mux + e_datapath + e_gpio_pads + e_leakage
        p_total_uw = p_clk_tree_uw + p_ram_uw + p_core_datapath_uw + p_gpio_pads_uw + p_leakage_uw

        e_total_uj = e_total * 1e6
        e_per_cycle_pj = (e_total / max(1, self.total_cycles)) * 1e12
        retired_insns = self.active_cycles
        e_per_insn_pj = (e_total / max(1, retired_insns)) * 1e12

        return {
            "total_cycles": self.total_cycles,
            "active_cycles": self.active_cycles,
            "wait_stall_cycles": self.wait_stall_cycles,
            "edge_stall_cycles": self.edge_stall_cycles,
            "halt_cycles": self.halt_cycles,
            "p_clk_tree_uw": round(p_clk_tree_uw, 2),
            "p_ram_uw": round(p_ram_uw, 2),
            "p_core_datapath_uw": round(p_core_datapath_uw, 2),
            "p_gpio_pads_uw": round(p_gpio_pads_uw, 2),
            "p_leakage_uw": round(p_leakage_uw, 2),
            "p_total_uw": round(p_total_uw, 2),
            "energy_total_uj": round(e_total_uj, 5),
            "energy_per_cycle_pj": round(e_per_cycle_pj, 2),
            "energy_per_insn_pj": round(e_per_insn_pj, 2),
            "clock_gating": clock_gating,
        }

    def compute_energy_savings(self) -> Dict[str, Any]:
        """Compute relative power and energy savings achieved by clock gating."""
        ungated = self.compute_power_breakdown(clock_gating=False)
        gated = self.compute_power_breakdown(clock_gating=True)

        p_saved_uw = ungated["p_total_uw"] - gated["p_total_uw"]
        savings_pct = (p_saved_uw / max(1e-6, ungated["p_total_uw"])) * 100.0

        return {
            "ungated_p_total_uw": ungated["p_total_uw"],
            "gated_p_total_uw": gated["p_total_uw"],
            "p_saved_uw": round(p_saved_uw, 2),
            "savings_pct": round(savings_pct, 2),
            "ungated_e_per_insn_pj": ungated["energy_per_insn_pj"],
            "gated_e_per_insn_pj": gated["energy_per_insn_pj"],
        }

    def compute_protocol_metric(self, num_bits: int, clock_gating: bool = True) -> float:
        """Compute energy per transmitted/received protocol bit in picojoules (pJ/bit)."""
        summary = self.compute_power_breakdown(clock_gating=clock_gating)
        total_energy_pj = summary["energy_total_uj"] * 1e6
        if num_bits <= 0:
            return 0.0
        return round(total_energy_pj / num_bits, 2)


# ---------------------------------------------------------------------------
# Benchmark Firmware Generators for Micro-Architectural Power Profiling
# ---------------------------------------------------------------------------

def build_alu_heavy_power_asm(iterations: int = 10) -> List[str]:
    """
    Firmware exercising high-switching ALU datapath operations:
    ADDI, SUBI, ANDI, ORI, XORI inside a DECJNZ loop.
    """
    asm = [
        "LDI R0, 0x01",
        "LDI R1, 0xFF",
        f"LDI R2, {iterations}",
        "loop:",
        "ADDI R0, 0x07",
        "SUBI R1, 0x03",
        "ANDI R0, 0xAA",
        "ORI  R0, 0x55",
        "XORI R0, 0xC3",
        "DECJNZ R2, loop",
        "HALT",
    ]
    return asm


def build_gpio_heavy_power_asm(iterations: int = 10) -> List[str]:
    """
    Firmware exercising peak external GPIO pad toggling:
    Alternates 0x55 and 0xAA onto uio_out to maximize capacitive load dissipation.
    """
    asm = [
        "GDIRI 0xFF",           # all pins output
        f"LDI R2, {iterations}",
        "loop:",
        "GWRI 0x55",            # toggle pattern A
        "GWRI 0xAA",            # toggle pattern B
        "DECJNZ R2, loop",
        "GWRI 0x00",            # return pins to 0
        "HALT",
    ]
    return asm


def build_wait_idle_power_asm(wait_duration: int = 40, iterations: int = 4) -> List[str]:
    """
    Firmware executing long WAIT stalls to benchmark clock-gated power savings.
    """
    asm = [
        f"LDI R2, {iterations}",
        "loop:",
        f"WAIT {wait_duration}",
        "ADDI R0, 0x01",
        "DECJNZ R2, loop",
        "HALT",
    ]
    return asm


def build_waitedge_power_asm(pin: int = 0) -> List[str]:
    """
    Firmware waiting for an external edge on a GPIO pin via WAITEDGE.
    """
    # WAITEDGE R0, operand: pin in operand[2:0], mode in operand[4:3] (01 = rising edge)
    operand = (0x01 << 3) | (pin & 0x07)
    asm = [
        "GDIRI 0x00",           # all pins input
        f"WAITEDGE R0, {operand}",
        "HALT",
    ]
    return asm


def build_uart_tx_power_benchmark_asm(data_byte: int = 0x55, baud_div: int = 8) -> List[str]:
    """
    Firmware transmitting an 8-N-1 UART frame on uio[0] for protocol energy benchmark.
    """
    delay = max(0, baud_div - 4)
    asm = [
        "GDIRI 0x01",           # uio[0] output
        "GWRI  0x01",           # idle line HIGH
        f"LDI R0, {data_byte}", # payload
        "LDI R1, 0x08",         # 8 bits
        # Start bit (LOW)
        "GWRI 0x00",
        f"WAIT {delay}",
        # Data bits loop
        "bit_loop:",
        "SHIFTOUT R0, 0x00",    # LSB shift out on pin 0
        f"WAIT {delay}",
        "DECJNZ R1, bit_loop",
        # Stop bit (HIGH)
        "GWRI 0x01",
        f"WAIT {delay}",
        "HALT",
    ]
    return asm
