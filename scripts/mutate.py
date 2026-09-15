#!/usr/bin/env python3
"""
scripts/mutate.py - Seeded RTL Mutation Testing Harness

Injects realistic hardware faults (mutations) into RTL source files, runs the
verification test suite against each mutant, and calculates the mutation kill
rate (mutation score).

Grounding in literature:
- Huang et al. (2015): "Mutation-based test qualification for hardware designs"
- Firefly (2025): Hardware mutation testing for open-source digital design

Usage:
    python3 scripts/mutate.py [--quick] [--mutant <id>]
"""

import sys
import os
import time
import shutil
import json
import subprocess
import argparse

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

MUTANTS = [
    {
        "id": "MUT_01_BRANCH_JZ_INVERT",
        "category": "Control / Branch",
        "file": "src/core.v",
        "target": "OP_JZ: if (z) pc <= operand[ADDR_WIDTH-1:0];",
        "replacement": "OP_JZ: if (!z) pc <= operand[ADDR_WIDTH-1:0];",
        "description": "Invert branch condition in OP_JZ (jump on !z instead of z)",
    },
    {
        "id": "MUT_02_WAIT_OFF_BY_ONE",
        "category": "Timing / Wait",
        "file": "src/core.v",
        "target": "wait_remaining <= wait_remaining - 8'h01;",
        "replacement": "wait_remaining <= wait_remaining - 8'h02;",
        "description": "Off-by-one decrement in WAIT countdown (-2 instead of -1)",
    },
    {
        "id": "MUT_03_ALU_ADD_CORRUPT",
        "category": "Datapath / ALU",
        "file": "src/alu.v",
        "target": "OP_ADD:  result = a + b;",
        "replacement": "OP_ADD:  result = a + b + 8'd1;",
        "description": "Arithmetic bug: OP_ADD adds 1 to calculated sum",
    },
    {
        "id": "MUT_04_ALU_SUB_TO_ADD",
        "category": "Datapath / ALU",
        "file": "src/alu.v",
        "target": "OP_SUB:  result = a - b;",
        "replacement": "OP_SUB:  result = a + b;",
        "description": "Operator replacement: OP_SUB computes sum instead of diff",
    },
    {
        "id": "MUT_05_RESET_PC_CORRUPT",
        "category": "Reset / Initialization",
        "file": "src/core.v",
        "target": "pc             <= {ADDR_WIDTH{1'b0}};",
        "replacement": "pc             <= {{(ADDR_WIDTH - 1) {1'b0}}, 1'b1};",
        "description": "Reset corruption: PC initializes to 1 instead of 0",
    },
    {
        "id": "MUT_06_DECJNZ_NO_BRANCH",
        "category": "Control / Loop",
        "file": "src/core.v",
        "target": "if (alu_result != 8'h00) pc <= operand[ADDR_WIDTH-1:0];",
        "replacement": "if (alu_result == 8'h00) pc <= operand[ADDR_WIDTH-1:0];",
        "description": "Branch condition inversion in DECJNZ loop termination",
    },
    {
        "id": "MUT_07_SHIFTOUT_MSB_FIRST",
        "category": "Protocol / Bit-Serial",
        "file": "src/core.v",
        "target": "gpio_out[pin_idx] <= rd_val[0];",
        "replacement": "gpio_out[pin_idx] <= rd_val[7];",
        "description": "Serial shift bug: SHIFTOUT transmits MSB instead of LSB",
    },
    {
        "id": "MUT_08_GPIO_OE_INVERT",
        "category": "Interface / Tri-state",
        "file": "src/gpio.v",
        "target": "assign pin_oe  = (dir & ~od_mode) | (dir & od_mode & ~out_val);",
        "replacement": "assign pin_oe  = ~((dir & ~od_mode) | (dir & od_mode & ~out_val));",
        "description": "GPIO bus direction inversion: pin_oe driven inverted",
    },
    {
        "id": "MUT_09_WAITEDGE_OFF_BY_ONE",
        "category": "Timing / Autobaud",
        "file": "src/core.v",
        "target": "write_rd(rd_idx, edge_wait_cnt + 8'd1);",
        "replacement": "write_rd(rd_idx, edge_wait_cnt);",
        "description": "Edge timing bug: WAITEDGE omits edge detection cycle in duration",
    },
    {
        "id": "MUT_10_BOOTLOADER_REQ_IGNORE",
        "category": "System / Bootloader",
        "file": "src/core.v",
        "target": "if (gpio_in[LOAD_REQ_BIT]) begin",
        "replacement": "if (1'b0) begin",
        "description": "Bootloader FSM bug: core ignores host serial LOAD_REQ signal",
    },
    {
        "id": "MUT_11_OPEN_DRAIN_DRIVE_HIGH",
        "category": "Protocol / Open-Drain",
        "file": "src/gpio.v",
        "target": "assign pin_out = out_val & ~od_mode;",
        "replacement": "assign pin_out = out_val;",
        "description": "Open-drain electrical bug: pin_out actively drives high in open-drain mode",
    },
    {
        "id": "MUT_12_BOOTLOADER_CRC_BYPASS",
        "category": "System / Security",
        "file": "src/core.v",
        "target": "if ({ld_sreg[6:0], gpio_in[LOAD_DATA_BIT]} == ld_crc) begin",
        "replacement": "if (1'b1) begin",
        "description": "Bootloader security bug: core accepts corrupted programs by bypassing CRC-8 verification",
    },
    {
        "id": "MUT_13_SHIFTIN_BIT_ORDER",
        "category": "Protocol / Bit-Serial",
        "file": "src/core.v",
        "target": "write_rd(rd_idx, {gpio_in[pin_idx], rd_val[7:1]});",
        "replacement": "write_rd(rd_idx, {rd_val[6:0], gpio_in[pin_idx]});",
        "description": "Shift input bug: SHIFTIN LSB mode shifts left instead of right (reverses bit order)",
    },
    {
        "id": "MUT_14_OPEN_DRAIN_OE_POLARITY",
        "category": "Protocol / Open-Drain",
        "file": "src/gpio.v",
        "target": "assign pin_oe  = (dir & ~od_mode) | (dir & od_mode & ~out_val);",
        "replacement": "assign pin_oe  = (dir & ~od_mode) | (dir & od_mode & out_val);",
        "description": "Open-drain OE polarity inversion: pin_oe asserts on out_val=1 instead of out_val=0",
    },
    {
        "id": "MUT_15_WAITEDGE_POLARITY_INVERT",
        "category": "Timing / Edge-Detect",
        "file": "src/core.v",
        "target": "(edge_mode == 2'b00) ? edge_fall :",
        "replacement": "(edge_mode == 2'b00) ? edge_rise :",
        "description": "WAITEDGE polarity inversion: falling edge mode triggers on rising edge",
    },
    {
        "id": "MUT_16_ALU_XOR_TO_OR",
        "category": "Core / Arithmetic",
        "file": "src/alu.v",
        "target": "OP_XOR:  result = a ^ b;",
        "replacement": "OP_XOR:  result = a | b;",
        "description": "ALU logic bug: XOR opcode executes bitwise OR instead of bitwise XOR",
    },
    {
        "id": "MUT_17_GDIRI_INVERT",
        "category": "Interface / Tri-state",
        "file": "src/core.v",
        "target": "OP_GDIRI: gpio_dir <= operand;",
        "replacement": "OP_GDIRI: gpio_dir <= ~operand;",
        "description": "GPIO direction bug: GDIRI inverts direction mask (inputs become outputs and vice versa)",
    },
    {
        "id": "MUT_18_SHIFTIN_MSB_INVERT",
        "category": "Protocol / Bit-Serial",
        "file": "src/core.v",
        "target": "write_rd(rd_idx, {rd_val[6:0], gpio_in[pin_idx]});",
        "replacement": "write_rd(rd_idx, {rd_val[6:0], ~gpio_in[pin_idx]});",
        "description": "Shift input bug: SHIFTIN MSB mode inverts incoming pin data bit",
    },
    {
        "id": "MUT_19_GODRI_DISABLE",
        "category": "Protocol / Open-Drain",
        "file": "src/core.v",
        "target": "OP_GODRI: gpio_od_mode <= operand;",
        "replacement": "OP_GODRI: gpio_od_mode <= 8'h00;",
        "description": "Open-drain configuration bug: GODRI fails to set open-drain mode (pins remain push-pull)",
    },
    {
        "id": "MUT_20_WAITEDGE_DURATION_OFF_BY_ONE",
        "category": "Timing / Edge-Detect",
        "file": "src/core.v",
        "target": "write_rd(rd_idx, edge_wait_cnt + 8'd1);",
        "replacement": "write_rd(rd_idx, edge_wait_cnt);",
        "description": "WAITEDGE timing calculation bug: omits single-cycle edge detection latency from measured pulse width",
    },
    {
        "id": "MUT_21_WAITEDGE_MODE_BIT_SLICE",
        "category": "Timing / Edge-Detect",
        "file": "src/core.v",
        "target": "wire [1:0] edge_mode = operand[4:3];",
        "replacement": "wire [1:0] edge_mode = operand[5:4];",
        "description": "WAITEDGE decode bug: edge_mode sliced from operand[5:4] instead of operand[4:3]",
    },
    {
        "id": "MUT_22_ALU_ZERO_FLAG_INVERT",
        "category": "Core / Flags",
        "file": "src/core.v",
        "target": "z <= (alu_result == 8'h00);",
        "replacement": "z <= (alu_result != 8'h00);",
        "description": "ALU flag calculation bug: zero flag Z inverted on ALU operations (asserts when result non-zero)",
    },
    {
        "id": "MUT_23_WAITEDGE_TIMESTAMP_CORRUPT",
        "category": "Timing / Edge-Detect",
        "file": "src/core.v",
        "target": "write_rd(rd_idx, cycle_cnt[7:0]);",
        "replacement": "write_rd(rd_idx, cycle_cnt[15:8]);",
        "description": "WAITEDGE timestamp capture bug: writes cycle_cnt[15:8] instead of cycle_cnt[7:0]",
    },
    {
        "id": "MUT_24_BRANCH_JNZ_INVERT",
        "category": "Control / Branch",
        "file": "src/core.v",
        "target": "OP_JNZ: if (!z) pc <= operand[ADDR_WIDTH-1:0];",
        "replacement": "OP_JNZ: if (z) pc <= operand[ADDR_WIDTH-1:0];",
        "description": "Branch condition inversion in OP_JNZ (branches on z instead of !z)",
    },
    {
        "id": "MUT_25_BRANCH_JZ_INVERT",
        "category": "Control / Branch",
        "file": "src/core.v",
        "target": "OP_JZ: if (z) pc <= operand[ADDR_WIDTH-1:0];",
        "replacement": "OP_JZ: if (!z) pc <= operand[ADDR_WIDTH-1:0];",
        "description": "Branch condition inversion in OP_JZ (branches on !z instead of z)",
    },
    {
        "id": "MUT_26_ALU_AND_TO_OR",
        "category": "Datapath / ALU",
        "file": "src/alu.v",
        "target": "OP_AND:  result = a & b;",
        "replacement": "OP_AND:  result = a | b;",
        "description": "ALU logic bug: AND opcode executes bitwise OR instead of bitwise AND",
    },
    {
        "id": "MUT_27_ALU_OR_TO_AND",
        "category": "Datapath / ALU",
        "file": "src/alu.v",
        "target": "OP_OR:   result = a | b;",
        "replacement": "OP_OR:   result = a & b;",
        "description": "ALU logic bug: OR opcode executes bitwise AND instead of bitwise OR",
    },
    {
        "id": "MUT_28_ALU_XOR_TO_XNOR",
        "category": "Datapath / ALU",
        "file": "src/alu.v",
        "target": "OP_XOR:  result = a ^ b;",
        "replacement": "OP_XOR:  result = a ~^ b;",
        "description": "ALU logic bug: XOR opcode executes bitwise XNOR instead of bitwise XOR",
    },
    {
        "id": "MUT_29_GPIO_OD_PIN_OUT",
        "category": "IO / Open-Drain",
        "file": "src/gpio.v",
        "target": "assign pin_out = out_val & ~od_mode;",
        "replacement": "assign pin_out = out_val;",
        "description": "Open-drain pin output bug: drives out_val actively high even when od_mode is enabled (violates open-drain high-Z specification)",
    },
    {
        "id": "MUT_30_GPIO_OD_OE_INVERT",
        "category": "IO / Open-Drain",
        "file": "src/gpio.v",
        "target": "assign pin_oe  = (dir & ~od_mode) | (dir & od_mode & ~out_val);",
        "replacement": "assign pin_oe  = (dir & ~od_mode) | (dir & od_mode & out_val);",
        "description": "Open-drain output enable bug: asserts pin_oe when out_val is 1 instead of 0 in open-drain mode, inverting open-drain drive behavior",
    },
    {
        "id": "MUT_31_WARM_BOOT_IGNORE",
        "category": "System / Bootloader",
        "file": "src/core.v",
        "target": "          end else if (gpio_in[LOAD_REQ_BIT]) begin",
        "replacement": "          end else if (1'b1) begin",
        "description": "Warm-boot skip logic bug: always enters serial load even when LOAD_REQ=0, breaking instant warm-boot and brownout recovery",
    },
    {
        "id": "MUT_32_HALT_RUNAWAY",
        "category": "Power / Control",
        "file": "src/core.v",
        "target": "              OP_HALT: halted <= 1'b1;",
        "replacement": "              OP_HALT: halted <= 1'b0;",
        "description": "Power and execution control bug: OP_HALT fails to assert halted, causing runaway instruction execution and continuous dynamic switching power instead of entering static idle",
    },
    {
        "id": "MUT_33_CORE_XORI_DECODE",
        "category": "Core / Crypto Datapath",
        "file": "src/core.v",
        "target": "      OP_XORI: alu_op = ALU_XOR;",
        "replacement": "      OP_XORI: alu_op = ALU_AND;",
        "description": "Cryptographic datapath decode bug: OP_XORI decodes to ALU_AND instead of ALU_XOR, corrupting ARX quarter-rounds and non-linear primitives",
    },
    {
        "id": "MUT_34_DECJNZ_STEP_SIZE",
        "category": "Control / Loop",
        "file": "src/core.v",
        "target": "    if (opcode == OP_DECJNZ) alu_b = 8'hFF;  // -1 mod 256",
        "replacement": "    if (opcode == OP_DECJNZ) alu_b = 8'hFE;  // -2 mod 256 bug",
        "description": "Deterministic loop control bug: DECJNZ decrements register by 2 instead of 1, halving loop iteration count and corrupting real-time scheduling time-slices",
    },
    {
        "id": "MUT_35_SHIFTOUT_MSB_BIT_SELECT",
        "category": "IO / Shift Datapath",
        "file": "src/core.v",
        "target": "                  gpio_out[pin_idx] <= rd_val[7];",
        "replacement": "                  gpio_out[pin_idx] <= rd_val[6];",
        "description": "Bit-serial protocol engine bug: SHIFTOUT MSB mode emits rd_val[6] instead of MSB rd_val[7], corrupting CAN FD, SPI, and UART frames",
    },
    {
        "id": "MUT_36_PAD_STATUS_PIN_SWAP",
        "category": "Pad / Package Pinout",
        "file": "src/project.v",
        "target": "  assign uo_out = {6'b000000, boot_err, boot_done};",
        "replacement": "  assign uo_out = {6'b000000, boot_done, boot_err};",
        "description": "Package pinout routing bug: uo_out[1] boot_err and uo_out[0] boot_done pad connections swapped in top-level padframe",
    },
    {
        "id": "MUT_37_EVENT_EDGE_POLARITY",
        "category": "Event / Edge-Detect",
        "file": "src/core.v",
        "target": "                      (edge_mode == 2'b01) ? edge_rise :",
        "replacement": "                      (edge_mode == 2'b01) ? edge_fall :",
        "description": "Asynchronous event detection bug: WAITEDGE mode 01 (rising edge) checks edge_fall instead of edge_rise, causing edge-triggered interrupts to hang or miss events",
    },
    {
        "id": "MUT_38_MPU_REGION_BOUND_CHECK",
        "category": "MPU / Partition Dispatch",
        "file": "src/core.v",
        "target": "              OP_JMP: pc <= operand[ADDR_WIDTH-1:0];",
        "replacement": "              OP_JMP: pc <= operand[ADDR_WIDTH-1:0] ^ 8'h01;",
        "description": "MPU partition dispatch bug: OP_JMP jumps to operand ^ 8'h01 instead of operand, corrupting jump table dispatch and violating partition boundaries",
    },
]


def run_tests(timeout_sec=240):
    """Run regression test suite in test directory. Returns True if tests pass, False if failed."""
    cmd = ["make", "-C", os.path.join(REPO_ROOT, "test"), "clean", "sim"]
    env = os.environ.copy()
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
            env=env,
            cwd=REPO_ROOT
        )
        # In cocotb/pytest, 0 exit code indicates PASS; non-zero indicates FAIL
        return (proc.returncode == 0)
    except subprocess.TimeoutExpired:
        # A timeout also indicates test failure (e.g. infinite loop or hang caused by mutation)
        return False
    except Exception as e:
        print(f"Execution error: {e}", file=sys.stderr)
        return False


def cleanup_backups():
    src_dir = os.path.join(REPO_ROOT, "src")
    if os.path.exists(src_dir):
        for f in os.listdir(src_dir):
            if f.endswith(".bak_mut"):
                orig = f[:-8]
                bak_path = os.path.join(src_dir, f)
                orig_path = os.path.join(src_dir, orig)
                shutil.copyfile(bak_path, orig_path)
                os.remove(bak_path)


def apply_mutation(filepath, target, replacement):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    if target not in content:
        raise ValueError(f"Target pattern not found in {filepath}: {target!r}")

    new_content = content.replace(target, replacement, 1)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(new_content)


def main():
    cleanup_backups()
    parser = argparse.ArgumentParser(description="Run RTL mutation testing suite.")
    parser.add_argument("--mutant", help="Run a specific mutant ID only")
    args = parser.parse_args()

    mutants_to_run = MUTANTS
    if args.mutant:
        mutants_to_run = [m for m in MUTANTS if m["id"] == args.mutant]
        if not mutants_to_run:
            print(f"Error: Unknown mutant ID '{args.mutant}'", file=sys.stderr)
            sys.exit(1)

    print("=" * 80)
    print("Jane Street Protocol Emulator - Seeded Mutation Testing Harness")
    print(f"Total defined mutants: {len(mutants_to_run)}")
    print("=" * 80)

    # First verify baseline passes cleanly
    print("--> Checking baseline test suite before applying mutations...")
    baseline_pass = run_tests()
    if not baseline_pass:
        print("FATAL: Baseline test suite fails without mutations! Aborting.", file=sys.stderr)
        sys.exit(1)
    print("--> Baseline PASSED cleanly. Beginning mutation campaign.\n")

    results = []
    start_campaign = time.time()

    for idx, mut in enumerate(mutants_to_run, 1):
        rel_path = mut["file"]
        abs_path = os.path.join(REPO_ROOT, rel_path)
        backup_path = abs_path + ".bak_mut"

        print(f"[{idx:2d}/{len(mutants_to_run)}] Testing {mut['id']}: {mut['description']}")
        print(f"       File: {rel_path} | Category: {mut['category']}")

        # Back up original file
        shutil.copyfile(abs_path, backup_path)
        t0 = time.time()

        try:
            apply_mutation(abs_path, mut["target"], mut["replacement"])
            passed = run_tests()
            elapsed = time.time() - t0

            if passed:
                status = "SURVIVED"
                print(f"       => RESULT: [!] {status} (tests unexpectedly passed!) in {elapsed:.2f}s")
            else:
                status = "KILLED"
                print(f"       => RESULT: [*] {status} (test suite caught the bug) in {elapsed:.2f}s")

            results.append({
                "id": mut["id"],
                "category": mut["category"],
                "file": mut["file"],
                "description": mut["description"],
                "status": status,
                "elapsed_sec": round(elapsed, 2)
            })

        finally:
            # Always restore original file
            if os.path.exists(backup_path):
                shutil.copyfile(backup_path, abs_path)
                os.remove(backup_path)

    total_time = time.time() - start_campaign
    total_mutants = len(results)
    killed = sum(1 for r in results if r["status"] == "KILLED")
    survived = sum(1 for r in results if r["status"] == "SURVIVED")
    kill_rate = (killed / total_mutants * 100.0) if total_mutants > 0 else 0.0

    print("\n" + "=" * 80)
    print("MUTATION TESTING SUMMARY REPORT")
    print("=" * 80)
    print(f"Total Mutants Evaluated : {total_mutants}")
    print(f"Mutants Killed          : {killed}")
    print(f"Mutants Survived        : {survived}")
    print(f"Mutation Kill Rate      : {kill_rate:.1f}%")
    print(f"Total Campaign Duration : {total_time:.2f}s")
    print("-" * 80)
    print(f"{'Mutant ID':<30} | {'Category':<20} | {'Status':<10} | {'Time (s)':<8}")
    print("-" * 80)
    for r in results:
        print(f"{r['id']:<30} | {r['category']:<20} | {r['status']:<10} | {r['elapsed_sec']:<8}")
    print("=" * 80)

    report_path = os.path.join(REPO_ROOT, "orchestrator", "mutation_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_mutants": total_mutants,
            "killed": killed,
            "survived": survived,
            "kill_rate_pct": round(kill_rate, 2),
            "campaign_time_sec": round(total_time, 2),
            "mutants": results
        }, f, indent=2)
    print(f"Report written to {os.path.relpath(report_path, REPO_ROOT)}")

    # Exit code: 0 if all killed, 1 if any survived
    sys.exit(0 if survived == 0 else 1)


if __name__ == "__main__":
    main()
