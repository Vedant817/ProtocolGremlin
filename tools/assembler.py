#!/usr/bin/env python3
"""Two-pass assembler for the protocol-emulator core, ISA v0.

Encoding (16-bit instruction word, MUST stay in sync with docs/isa.md,
src/core.v and tools/isa_model.py):

    [15:11] opcode  (5 bits)
    [10:9]  rd      (2 bits, register index 0-3)
    [8:1]   operand (8 bits: imm8 / addr8 / rs, meaning depends on opcode)
    [0]     reserved (0)

Usage:
    python3 tools/assembler.py program.asm -o program.hex
    python3 tools/assembler.py program.asm --bin   # print binary, for debugging

Also importable as a library:
    from assembler import assemble
    words = assemble(source_text)   # -> list[int] of 16-bit instruction words
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field

OPCODES = {
    "NOP": 0,
    "LDI": 1,
    "MOV": 2,
    "ADDI": 3,
    "SUBI": 4,
    "ANDI": 5,
    "ORI": 6,
    "XORI": 7,
    "GDIRI": 8,
    "GDIR": 9,
    "GWRI": 10,
    "GWR": 11,
    "GRD": 12,
    "WAIT": 13,
    "JMP": 14,
    "JZ": 15,
    "JNZ": 16,
    "DECJNZ": 17,
    "HALT": 18,
    "SHIFTOUT": 19,
    "SHIFTIN": 20,
    "WAITEDGE": 21,
    "GODRI": 22,
    "GODR": 23,
}

# Instructions that take (rd, imm8). SHIFTOUT/SHIFTIN's "imm8" is a
# 3-bit pin select. WAITEDGE takes (rd, mode_pin).
RD_IMM_OPS = {"LDI", "ADDI", "SUBI", "ANDI", "ORI", "XORI", "SHIFTOUT", "SHIFTIN", "WAITEDGE"}
# Instructions that take (rd, rs)
RD_RS_OPS = {"MOV"}
# Instructions that take (imm8) only
IMM_ONLY_OPS = {"GDIRI", "GWRI", "WAIT", "GODRI"}
# Instructions that take (rd) only
RD_ONLY_OPS = {"GDIR", "GWR", "GRD", "GODR"}
# Instructions that take (addr) only
ADDR_ONLY_OPS = {"JMP", "JZ", "JNZ"}
# Instructions that take (rd, addr)
RD_ADDR_OPS = {"DECJNZ"}
# Instructions that take no operands
NO_OPERAND_OPS = {"NOP", "HALT"}


class AssemblerError(Exception):
    pass


@dataclass
class Instr:
    line_no: int
    mnemonic: str
    args: list[str] = field(default_factory=list)


def _strip_comment(line: str) -> str:
    for marker in (";", "#", "//"):
        idx = line.find(marker)
        if idx != -1:
            line = line[:idx]
    return line.strip()


def _parse_reg(tok: str, line_no: int) -> int:
    tok = tok.strip().upper()
    if not re.fullmatch(r"R[0-3]", tok):
        raise AssemblerError(f"line {line_no}: expected register R0-R3, got {tok!r}")
    return int(tok[1])


def _parse_int(tok: str, line_no: int) -> int:
    tok = tok.strip()
    try:
        return int(tok, 0)
    except ValueError as exc:
        raise AssemblerError(f"line {line_no}: expected integer, got {tok!r}") from exc


def parse(source: str) -> tuple[list[Instr], dict[str, int]]:
    """First pass: strip comments/labels, return (instructions, label->address)."""
    instructions: list[Instr] = []
    labels: dict[str, int] = {}

    for raw_line_no, raw_line in enumerate(source.splitlines(), start=1):
        line = _strip_comment(raw_line)
        if not line:
            continue

        # A line may be "LABEL:" alone, or "LABEL: MNEMONIC args", or just "MNEMONIC args".
        label_match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
        if label_match:
            label, rest = label_match.group(1), label_match.group(2).strip()
            if label in labels:
                raise AssemblerError(f"line {raw_line_no}: duplicate label {label!r}")
            labels[label] = len(instructions)
            line = rest
            if not line:
                continue

        parts = line.split(None, 1)
        mnemonic = parts[0].upper()
        arg_str = parts[1] if len(parts) > 1 else ""
        args = [a.strip() for a in arg_str.split(",")] if arg_str else []

        if mnemonic not in OPCODES:
            raise AssemblerError(f"line {raw_line_no}: unknown mnemonic {mnemonic!r}")

        instructions.append(Instr(raw_line_no, mnemonic, args))

    return instructions, labels


def _resolve_addr(tok: str, labels: dict[str, int], line_no: int) -> int:
    tok = tok.strip()
    if tok in labels:
        return labels[tok]
    return _parse_int(tok, line_no)


def assemble(source: str) -> list[int]:
    """Assemble ISA v0 source text into a list of 16-bit instruction words."""
    instructions, labels = parse(source)
    words: list[int] = []

    for instr in instructions:
        op = instr.mnemonic
        opcode = OPCODES[op]
        rd = 0
        operand = 0

        if op in NO_OPERAND_OPS:
            if instr.args and instr.args != [""]:
                raise AssemblerError(f"line {instr.line_no}: {op} takes no operands")
        elif op in RD_IMM_OPS:
            if len(instr.args) == 2:
                rd = _parse_reg(instr.args[0], instr.line_no)
                operand = _parse_int(instr.args[1], instr.line_no) & 0xFF
            elif len(instr.args) == 3 and op in ("SHIFTOUT", "SHIFTIN"):
                rd = _parse_reg(instr.args[0], instr.line_no)
                pin = _parse_int(instr.args[1], instr.line_no) & 0x7
                dir_flag = 0x08 if instr.args[2].upper() in ("1", "MSB", "TRUE") else 0x00
                operand = pin | dir_flag
            else:
                raise AssemblerError(f"line {instr.line_no}: {op} needs rd, imm8 (or rd, pin, msb)")
        elif op in RD_RS_OPS:
            if len(instr.args) != 2:
                raise AssemblerError(f"line {instr.line_no}: {op} needs rd, rs")
            rd = _parse_reg(instr.args[0], instr.line_no)
            operand = _parse_reg(instr.args[1], instr.line_no)  # rs in operand[1:0]
        elif op in IMM_ONLY_OPS:
            if len(instr.args) != 1:
                raise AssemblerError(f"line {instr.line_no}: {op} needs one immediate")
            operand = _parse_int(instr.args[0], instr.line_no) & 0xFF
        elif op in RD_ONLY_OPS:
            if len(instr.args) != 1:
                raise AssemblerError(f"line {instr.line_no}: {op} needs one register")
            rd = _parse_reg(instr.args[0], instr.line_no)
        elif op in ADDR_ONLY_OPS:
            if len(instr.args) != 1:
                raise AssemblerError(f"line {instr.line_no}: {op} needs an address")
            operand = _resolve_addr(instr.args[0], labels, instr.line_no) & 0xFF
        elif op in RD_ADDR_OPS:
            if len(instr.args) != 2:
                raise AssemblerError(f"line {instr.line_no}: {op} needs rd, addr")
            rd = _parse_reg(instr.args[0], instr.line_no)
            operand = _resolve_addr(instr.args[1], labels, instr.line_no) & 0xFF
        else:  # pragma: no cover - defensive, every opcode above is categorized
            raise AssemblerError(f"line {instr.line_no}: unhandled opcode {op!r}")

        word = ((opcode & 0x1F) << 11) | ((rd & 0x3) << 9) | ((operand & 0xFF) << 1)
        words.append(word)

    return words


def write_hex(words: list[int], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for w in words:
            fh.write(f"{w:04x}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="assembly source file")
    ap.add_argument("-o", "--output", default="program.hex", help="output $readmemh hex file")
    ap.add_argument("--bin", action="store_true", help="print binary encoding to stdout")
    args = ap.parse_args(argv)

    with open(args.source, encoding="utf-8") as fh:
        source = fh.read()

    try:
        words = assemble(source)
    except AssemblerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.bin:
        for i, w in enumerate(words):
            print(f"{i:3d}: {w:016b}  (0x{w:04x})")

    write_hex(words, args.output)
    print(f"wrote {len(words)} words to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
