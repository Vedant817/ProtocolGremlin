#!/usr/bin/env python3
"""Pure-Python cycle-accurate reference model for the protocol-emulator core,
ISA v0.

This mirrors src/core.v and src/gpio.v register-for-register (same reset
values, same one-instruction-per-cycle timing, same 2-flop GPIO input
synchronizer) so it can be used for differential testing: assemble a
program, run it through both this model and the RTL via cocotb, and assert
that the observable state matches after every clock cycle. See
docs/verification.md.

Deliberately NOT shared code with src/core.v - the whole point is that two
independently written implementations of the same spec (docs/isa.md) should
agree. Keep the opcode table below in sync with tools/assembler.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

OP_NOP = 0
OP_LDI = 1
OP_MOV = 2
OP_ADDI = 3
OP_SUBI = 4
OP_ANDI = 5
OP_ORI = 6
OP_XORI = 7
OP_GDIRI = 8
OP_GDIR = 9
OP_GWRI = 10
OP_GWR = 11
OP_GRD = 12
OP_WAIT = 13
OP_JMP = 14
OP_JZ = 15
OP_JNZ = 16
OP_DECJNZ = 17
OP_HALT = 18
OP_SHIFTOUT = 19
OP_SHIFTIN = 20
OP_WAITEDGE = 21

ADDR_WIDTH = 8
ADDR_MASK = (1 << ADDR_WIDTH) - 1
ROM_DEPTH = 1 << ADDR_WIDTH


@dataclass
class CoreState:
    pc: int = 0
    regs: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    z: bool = False
    halted: bool = False
    wait_remaining: int = 0
    gpio_dir: int = 0
    gpio_out: int = 0
    gpio_in_sync: int = 0
    gpio_in_prev: int = 0
    cycle_cnt: int = 0
    edge_wait_cnt: int = 0

    def snapshot(self) -> dict:
        return {
            "pc": self.pc,
            "r0": self.regs[0],
            "r1": self.regs[1],
            "r2": self.regs[2],
            "r3": self.regs[3],
            "z": self.z,
            "halted": self.halted,
            "gpio_dir": self.gpio_dir,
            "gpio_out": self.gpio_out,
            "cycle_cnt": self.cycle_cnt,
            "edge_wait_cnt": self.edge_wait_cnt,
        }


class CoreModel:
    """Cycle-accurate ISA v0 reference model.

    Usage:
        model = CoreModel(words)
        model.reset()
        model.step(gpio_in_pin=0)   # advance one clock cycle, sample uio_in
        state = model.state.snapshot()
    """

    def __init__(self, words: list[int]):
        if len(words) > ROM_DEPTH:
            raise ValueError(f"program has {len(words)} words, exceeds ROM depth {ROM_DEPTH}")
        self.rom = [0] * ROM_DEPTH
        for i, w in enumerate(words):
            self.rom[i] = w & 0xFFFF
        self.state = CoreState()
        self._sync_stage0 = 0

    def reset(self) -> None:
        self.state = CoreState()
        self._sync_stage0 = 0

    @staticmethod
    def _reg_get(regs: list[int], idx: int) -> int:
        return regs[idx & 0x3]

    @staticmethod
    def _reg_set(regs: list[int], idx: int, val: int) -> None:
        regs[idx & 0x3] = val & 0xFF

    def step(self, gpio_in_pin: int = 0) -> None:
        """Advance the model by exactly one clock cycle (one posedge clk)."""
        s = self.state
        gpio_in_pin &= 0xFF

        # The 2-flop input synchronizer runs every cycle, unconditionally,
        # mirroring gpio.v (it is not gated by halted/wait_remaining).
        old_sync0 = self._sync_stage0
        old_in_sync = s.gpio_in_sync  # value visible to GRD *this* cycle

        if s.halted:
            pass
        elif s.wait_remaining != 0:
            s.wait_remaining -= 1
        else:
            instr = self.rom[s.pc]
            opcode = (instr >> 11) & 0x1F
            rd = (instr >> 9) & 0x3
            operand = (instr >> 1) & 0xFF
            rs = operand & 0x3

            next_pc = (s.pc + 1) & ADDR_MASK

            if opcode == OP_NOP:
                pass
            elif opcode == OP_LDI:
                self._reg_set(s.regs, rd, operand)
                s.z = operand == 0
            elif opcode == OP_MOV:
                val = self._reg_get(s.regs, rs)
                self._reg_set(s.regs, rd, val)
                s.z = val == 0
            elif opcode == OP_ADDI:
                val = (self._reg_get(s.regs, rd) + operand) & 0xFF
                self._reg_set(s.regs, rd, val)
                s.z = val == 0
            elif opcode == OP_SUBI:
                val = (self._reg_get(s.regs, rd) - operand) & 0xFF
                self._reg_set(s.regs, rd, val)
                s.z = val == 0
            elif opcode == OP_ANDI:
                val = self._reg_get(s.regs, rd) & operand
                self._reg_set(s.regs, rd, val)
                s.z = val == 0
            elif opcode == OP_ORI:
                val = self._reg_get(s.regs, rd) | operand
                self._reg_set(s.regs, rd, val)
                s.z = val == 0
            elif opcode == OP_XORI:
                val = self._reg_get(s.regs, rd) ^ operand
                self._reg_set(s.regs, rd, val)
                s.z = val == 0
            elif opcode == OP_GDIRI:
                s.gpio_dir = operand
            elif opcode == OP_GDIR:
                s.gpio_dir = self._reg_get(s.regs, rd)
            elif opcode == OP_GWRI:
                s.gpio_out = operand
            elif opcode == OP_GWR:
                s.gpio_out = self._reg_get(s.regs, rd)
            elif opcode == OP_GRD:
                self._reg_set(s.regs, rd, old_in_sync)
                s.z = old_in_sync == 0
            elif opcode == OP_WAIT:
                s.wait_remaining = operand
            elif opcode == OP_JMP:
                next_pc = operand & ADDR_MASK
            elif opcode == OP_JZ:
                if s.z:
                    next_pc = operand & ADDR_MASK
            elif opcode == OP_JNZ:
                if not s.z:
                    next_pc = operand & ADDR_MASK
            elif opcode == OP_DECJNZ:
                val = (self._reg_get(s.regs, rd) - 1) & 0xFF
                self._reg_set(s.regs, rd, val)
                s.z = val == 0
                if val != 0:
                    next_pc = operand & ADDR_MASK
            elif opcode == OP_HALT:
                s.halted = True
            elif opcode == OP_SHIFTOUT:
                pin = operand & 0x7
                rd_val = self._reg_get(s.regs, rd)
                s.gpio_out = (s.gpio_out & ~(1 << pin) & 0xFF) | ((rd_val & 1) << pin)
                new_val = rd_val >> 1  # zero-fill MSB
                self._reg_set(s.regs, rd, new_val)
                s.z = new_val == 0
            elif opcode == OP_SHIFTIN:
                pin = operand & 0x7
                bit = (old_in_sync >> pin) & 1
                rd_val = self._reg_get(s.regs, rd)
                new_val = (bit << 7) | (rd_val >> 1)
                self._reg_set(s.regs, rd, new_val & 0xFF)
                s.z = new_val == 0
            elif opcode == OP_WAITEDGE:
                pin = operand & 0x7
                mode = (operand >> 3) & 0x3
                if mode == 3:
                    val = s.cycle_cnt & 0xFF
                    self._reg_set(s.regs, rd, val)
                    s.z = val == 0
                else:
                    pin_now = (old_in_sync >> pin) & 1
                    pin_prev = (s.gpio_in_prev >> pin) & 1
                    edge_rise = pin_now == 1 and pin_prev == 0
                    edge_fall = pin_now == 0 and pin_prev == 1
                    edge_any = pin_now != pin_prev
                    matched = (
                        edge_fall if mode == 0 else
                        edge_rise if mode == 1 else
                        edge_any
                    )
                    if matched:
                        captured = (s.edge_wait_cnt + 1) & 0xFF
                        self._reg_set(s.regs, rd, captured)
                        s.z = captured == 0
                        s.edge_wait_cnt = 0
                    else:
                        next_pc = s.pc  # stall PC on current instruction
                        s.edge_wait_cnt = min(255, s.edge_wait_cnt + 1)
            # else: reserved/illegal encoding behaves as NOP in v1

            s.pc = next_pc

        s.cycle_cnt = (s.cycle_cnt + 1) & 0xFFFFFFFF
        s.gpio_in_prev = old_in_sync
        self._sync_stage0 = gpio_in_pin
        s.gpio_in_sync = old_sync0
