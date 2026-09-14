"""
tools/fuzzer.py - Constrained-Random Instruction Fuzzer and Shrinker

Generates legal, guaranteed-terminating random instruction sequences for the
Jane Street Protocol Emulator ASIC (ISA v1, 22 opcodes). Executes differential
comparison against tools/isa_model.py, with automated delta-debugging shrinking
upon detecting any architectural mismatch.
"""

import random
from typing import List, Callable, Optional, Tuple


REGISTERS = ["R0", "R1", "R2", "R3"]
PINS = list(range(8))


def generate_random_program(
    seed: Optional[int] = None,
    target_length: int = 20,
    allow_loops: bool = True
) -> str:
    """
    Generate a random, syntactically legal assembly program guaranteed to terminate.
    All backward jumps are strictly bounded loop constructs using DECJNZ.
    """
    rng = random.Random(seed)
    lines: List[str] = []

    # Initialize registers with random known values to ensure coverage
    for r in REGISTERS:
        lines.append(f"    LDI {r}, 0x{rng.randint(0, 255):02X}")

    loop_count = 0
    instruction_count = len(lines)

    while instruction_count < target_length:
        op_category = rng.choices(
            ["alu", "reg", "shift", "gpio", "wait", "loop"],
            weights=[35, 20, 15, 10, 10, 10 if allow_loops else 0],
            k=1
        )[0]

        rd = rng.choice(REGISTERS)
        rs = rng.choice(REGISTERS)
        imm = rng.randint(0, 255)
        pin = rng.choice(PINS)

        if op_category == "alu":
            alu_op = rng.choice(["ADDI", "SUBI", "ANDI", "ORI", "XORI"])
            lines.append(f"    {alu_op} {rd}, 0x{imm:02X}")
            instruction_count += 1

        elif op_category == "reg":
            reg_op = rng.choice(["MOV", "LDI", "NOP"])
            if reg_op == "MOV":
                lines.append(f"    MOV {rd}, {rs}")
            elif reg_op == "LDI":
                lines.append(f"    LDI {rd}, 0x{imm:02X}")
            else:
                lines.append("    NOP")
            instruction_count += 1

        elif op_category == "shift":
            shift_op = rng.choice(["SHIFTOUT", "SHIFTIN"])
            lines.append(f"    {shift_op} {rd}, {pin}")
            instruction_count += 1

        elif op_category == "gpio":
            gpio_op = rng.choice(["GWRI", "GDIRI", "GWR", "GDIR", "GRD"])
            if gpio_op in ["GWRI", "GDIRI"]:
                lines.append(f"    {gpio_op} 0x{imm:02X}")
            elif gpio_op in ["GWR", "GDIR", "GRD"]:
                lines.append(f"    {gpio_op} {rd}")
            instruction_count += 1

        elif op_category == "wait":
            wait_type = rng.choice(["WAIT", "WAITEDGE_TS"])
            if wait_type == "WAIT":
                # Small wait duration (0-4 cycles) to keep simulation fast
                small_wait = rng.randint(0, 4)
                lines.append(f"    WAIT 0x{small_wait:02X}")
            else:
                # WAITEDGE timestamp mode: captures cycle_cnt without stalling
                lines.append(f"    WAITEDGE {rd}, 0x03")
            instruction_count += 1

        elif op_category == "loop" and instruction_count + 5 <= target_length:
            # Strictly bounded DECJNZ loop: counter initialised to 1..3
            loop_reg = rng.choice(REGISTERS)
            iterations = rng.randint(1, 3)
            loop_lbl = f"fuzz_loop_{loop_count}"
            loop_count += 1

            lines.append(f"    LDI {loop_reg}, {iterations}")
            lines.append(f"{loop_lbl}:")
            # 1-2 loop body operations on different registers
            body_reg = rng.choice([r for r in REGISTERS if r != loop_reg])
            lines.append(f"    ADDI {body_reg}, 0x{rng.randint(1, 15):02X}")
            lines.append(f"    DECJNZ {loop_reg}, {loop_lbl}")
            instruction_count += 4

    lines.append("    HALT")
    return "\n".join(lines) + "\n"


def shrink_program(
    program_text: str,
    test_fn: Callable[[str], bool],
    max_steps: int = 50
) -> str:
    """
    Automated delta-debugging shrinker for failing assembly sequences.
    test_fn(candidate_text) should return True if the bug/mismatch STILL REPRODUCES,
    and False if the bug disappears (passes) or the program is invalid.
    """
    lines = [line for line in program_text.strip().splitlines() if line.strip()]

    # Guarantee last line is HALT
    if not lines or "HALT" not in lines[-1]:
        lines.append("    HALT")

    step = 0
    changed = True
    while changed and step < max_steps:
        changed = False
        step += 1

        # Try removing one line at a time (from top to bottom, keeping HALT)
        for i in range(len(lines) - 1):
            candidate_lines = lines[:i] + lines[i + 1:]
            candidate_text = "\n".join(candidate_lines) + "\n"

            # Check if this reduced program still triggers the mismatch
            if test_fn(candidate_text):
                lines = candidate_lines
                changed = True
                break

    return "\n".join(lines) + "\n"
