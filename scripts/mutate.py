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
    {
        "id": "MUT_39_CRC_POLYNOMIAL_TAP",
        "category": "CRC / Hardware Verification",
        "file": "src/core.v",
        "target": "        crc8_step = {c[6:0], 1'b0} ^ 8'h07;",
        "replacement": "        crc8_step = {c[6:0], 1'b0} ^ 8'h09;",
        "description": "CRC LFSR polynomial tap bug: crc8_step uses wrong polynomial 0x09 instead of 0x07, causing all hardware bootloader CRC calculations to mismatch and reject valid bitstreams",
    },
    {
        "id": "MUT_40_I3C_OPEN_DRAIN_ARBITRATION",
        "category": "IO / Open-Drain Arbitration",
        "file": "src/gpio.v",
        "target": "  assign pin_oe  = (dir & ~od_mode) | (dir & od_mode & ~out_val);",
        "replacement": "  assign pin_oe  = (dir & ~od_mode) | (dir & od_mode & out_val);",
        "description": "Open-drain arbitration control bug: inverts active pull-down condition in open-drain mode, asserting pin_oe when out_val=1 instead of out_val=0, corrupting I3C DAA arbitration and I2C ACK detection",
    },
    {
        "id": "MUT_41_QEI_VELOCITY_PERIOD_CAPTURE",
        "category": "Motion / Velocity Feedback",
        "file": "src/core.v",
        "target": "                  write_rd(rd_idx, edge_wait_cnt + 8'd1);",
        "replacement": "                  write_rd(rd_idx, 8'h00);",
        "description": "QEI motion velocity feedback bug: WAITEDGE fails to write elapsed wait cycles to rd (always writes 0), breaking quadrature velocity and acceleration measurement",
    },
    {
        "id": "MUT_42_LIN_BREAK_WAIT_TIMING",
        "category": "Timing / Wait State",
        "file": "src/core.v",
        "target": "              OP_WAIT: wait_remaining <= operand;",
        "replacement": "              OP_WAIT: wait_remaining <= operand + 8'h02;",
        "description": "Deterministic protocol wait timing bug: OP_WAIT loads operand + 2 instead of operand, stretching bit times, violating LIN Break duration constraints, and introducing baud rate phase errors",
    },
    {
        "id": "MUT_43_BISS_SHIFTIN_DIR",
        "category": "Serial / Shift Engine",
        "file": "src/core.v",
        "target": "                  write_rd(rd_idx, {rd_val[6:0], gpio_in[pin_idx]});",
        "replacement": "                  write_rd(rd_idx, {rd_val[6:0], ~gpio_in[pin_idx]});",
        "description": "SSI / BiSS-C serial data shift bug: OP_SHIFTIN in MSB mode inverts sampled GPIO pin value (~gpio_in[pin_idx]), corrupting serial encoder position, status flags, and CRC reception",
    },
    {
        "id": "MUT_44_1553_ALU_ORI_DECODE",
        "category": "Core / ALU Logic",
        "file": "src/core.v",
        "target": "      OP_ORI:  alu_op = ALU_OR;",
        "replacement": "      OP_ORI:  alu_op = ALU_AND;",
        "description": "ALU logic decode bug: OP_ORI decodes to ALU_AND instead of ALU_OR, corrupting bitwise OR operations, register bit setting, and protocol parity/flag accumulation",
    },
    {
        "id": "MUT_45_WIEGAND_WAITEDGE_RISE_POLARITY",
        "category": "Timing / Edge Capture",
        "file": "src/core.v",
        "target": "                      (edge_mode == 2'b01) ? edge_rise :",
        "replacement": "                      (edge_mode == 2'b01) ? edge_fall :",
        "description": "Wiegand pulse timing discovery bug: WAITEDGE rising-edge mode (mode 1) triggers on edge_fall instead of edge_rise, terminating pulse-width wait on leading edge instead of trailing edge and corrupting discovered pulse width and interval timing",
    },
    {
        "id": "MUT_46_ARINC429_PUSHPULL_PIN_OUT",
        "category": "IO / Push-Pull GPIO",
        "file": "src/gpio.v",
        "target": "assign pin_out = out_val & ~od_mode;",
        "replacement": "assign pin_out = out_val & od_mode;",
        "description": "Push-pull GPIO output bug: masks pin_out with od_mode instead of ~od_mode, suppressing all active push-pull high outputs (pin_out always 0 in push-pull mode), breaking ARINC 429 dual-rail BPRZ pulses and SPI/UART transmission",
    },
    {
        "id": "MUT_47_SHIFTOUT_LSB_FILL_BIT",
        "category": "Bit-Serial / Transmit Shift",
        "file": "src/core.v",
        "target": "                  write_rd(rd_idx, {1'b0, rd_val[7:1]});",
        "replacement": "                  write_rd(rd_idx, {1'b1, rd_val[7:1]});",
        "description": "Serial transmitter shift bug: OP_SHIFTOUT in LSB mode shifts in 1'b1 instead of 1'b0 ({1'b1, rd_val[7:1]}), corrupting subsequent transmitted bits across MIDI 2.0 UMP, UART, and LIN frames",
    },
    {
        "id": "MUT_48_I2S_DATA_BIT_INVERT",
        "category": "Audio / Serial Ingress Shift",
        "file": "src/core.v",
        "target": "                  write_rd(rd_idx, {rd_val[6:0], gpio_in[pin_idx]});",
        "replacement": "                  write_rd(rd_idx, {rd_val[6:0], ~gpio_in[pin_idx]});",
        "description": "Serial audio shift input bug: OP_SHIFTIN in MSB mode inverts incoming pin bit ({~gpio_in[pin_idx]}), corrupting MSB-first serial digital audio samples across I2S Left/Right channels and TDM multi-channel streams",
    },
    {
        "id": "MUT_49_SPACEWIRE_SHIFTIN_LSB_BIT_INVERT",
        "category": "Spacecraft / Serial Ingress Shift",
        "file": "src/core.v",
        "target": "                  write_rd(rd_idx, {gpio_in[pin_idx], rd_val[7:1]});",
        "replacement": "                  write_rd(rd_idx, {~gpio_in[pin_idx], rd_val[7:1]});",
        "description": "Serial LSB-first shift input bug: OP_SHIFTIN in LSB mode inverts incoming pin bit ({~gpio_in[pin_idx], rd_val[7:1]}), corrupting LSB-first serial character ingress across SpaceWire ECSS Data-Strobe streams and failing odd parity verification",
    },
    {
        "id": "MUT_50_SENT_WAITEDGE_FALLING_POLARITY",
        "category": "Automotive / Timing Discovery",
        "file": "src/core.v",
        "target": "  wire edge_matched = (edge_mode == 2'b00) ? edge_fall :",
        "replacement": "  wire edge_matched = (edge_mode == 2'b00) ? edge_rise :",
        "description": "SAE J2716 SENT timing discovery bug: WAITEDGE falling-edge mode (mode 0) triggers on edge_rise instead of edge_fall, breaking falling-to-falling pulse-period modulation (PPM), calibration pulse recovery, and data nibble extraction across automotive sensor frames",
    },
    {
        "id": "MUT_51_ETHERCAT_WKC_INCREMENT_ALU_ADD",
        "category": "Industrial / ALU Datapath",
        "file": "src/core.v",
        "target": "      default: alu_op = ALU_ADD;  // ADDI and DECJNZ's implicit -1 both use ADD path via alu_b",
        "replacement": "      default: alu_op = ALU_SUB;  // Mutated: ADDI subtracts instead of adding",
        "description": "EtherCAT Working Counter & arithmetic bug: OP_ADDI selects ALU_SUB instead of ALU_ADD in core ALU op decoder, causing ADDI R1, 1 to decrement WKC (e.g. 0 -> 255) instead of incrementing (+1), breaking EtherCAT in-stream Working Counter accounting and multi-precision carry chains",
    },
    {
        "id": "MUT_52_PROFIBUS_JNZ_INVERTED_BRANCH_CONDITION",
        "category": "Fieldbus / Control Flow",
        "file": "src/core.v",
        "target": "              OP_JNZ: if (!z) pc <= operand[ADDR_WIDTH-1:0];",
        "replacement": "              OP_JNZ: if (z) pc <= operand[ADDR_WIDTH-1:0];",
        "description": "Profibus DP & industrial fieldbus control flow bug: OP_JNZ branches when z is asserted (if (z)) instead of when z is deasserted (if (!z)), causing valid SD1/SD2/SD3/SD4 telegram delimiters and FCS checksums to trigger spurious fault traps",
    },
    {
        "id": "MUT_53_TSN_ANDI_LOGIC_MASK_CORRUPTION",
        "category": "Time-Sensitive Networking / ALU Logic",
        "file": "src/core.v",
        "target": "      OP_ANDI: alu_op = ALU_AND;",
        "replacement": "      OP_ANDI: alu_op = ALU_OR;  // Mutated: OP_ANDI selects ALU_OR instead of ALU_AND",
        "description": "IEEE 802.1Qav/Qbv TSN Priority Classifier & Credit-Based Shaper bug: OP_ANDI executes bitwise OR instead of bitwise AND in ALU operation multiplexer, corrupting 802.1Q PCP priority code point masking (ANDI R3, 0xE0) and CBS negative credit sign evaluation (ANDI R3, 0x80), resulting in incorrect traffic class steering and queue gate misconfiguration",
    },
    {
        "id": "MUT_54_MODBUS_SUBI_ALU_SUB_DECODE",
        "category": "Industrial Fieldbus / ALU Datapath",
        "file": "src/core.v",
        "target": "      OP_SUBI: alu_op = ALU_SUB;",
        "replacement": "      OP_SUBI: alu_op = ALU_ADD;  // Mutated: OP_SUBI selects ALU_ADD instead of ALU_SUB",
        "description": "Modbus RTU/ASCII & industrial serial bus arithmetic bug: OP_SUBI executes addition instead of subtraction in core ALU op decoder, corrupting Modbus Longitudinal Redundancy Check (LRC) two's complement accumulation and CBS credit consumption",
    },
    {
        "id": "MUT_55_FLEXRAY_XORI_ALU_XOR_DECODE",
        "category": "Automotive Determinism / ALU Logic",
        "file": "src/core.v",
        "target": "      OP_XORI: alu_op = ALU_XOR;",
        "replacement": "      OP_XORI: alu_op = ALU_OR;  // Mutated: OP_XORI selects ALU_OR instead of ALU_XOR",
        "description": "FlexRay ISO 17458 TDMA slot engine & Frame ID filter bug: OP_XORI executes bitwise OR instead of bitwise XOR in core ALU op decoder, corrupting single-cycle equality checks (XORI R3, assigned_slot_id), causing false matches and breaking TDMA slot synchronization and Frame ID filtering across automotive determinism engines",
    },
    {
        "id": "MUT_56_CANOPEN_JZ_INVERTED_BRANCH_CONDITION",
        "category": "CANopen & Fieldbus / Control Flow",
        "file": "src/core.v",
        "target": "              OP_JZ: if (z) pc <= operand[ADDR_WIDTH-1:0];",
        "replacement": "              OP_JZ: if (!z) pc <= operand[ADDR_WIDTH-1:0];  // Mutated: branches when !z instead of z",
        "description": "CANopen CiA 301 NMT & SAE J1939 control flow bug: OP_JZ branches when z is deasserted (if (!z)) instead of when z is asserted (if (z)), causing equality matches (Node-ID matching, NMT CS decoding 0x01/0x02/0x80, and J1939 PDU2 broadcast detection) to fail to branch and drop into mismatched bypass paths",
    },
    {
        "id": "MUT_57_PTP_TIMESTAMP_MODE_DECODE",
        "category": "IEEE 1588 PTP & Precision Timing / WAITEDGE Decode",
        "file": "src/core.v",
        "target": "                if (edge_mode == 2'b11) begin",
        "replacement": "                if (edge_mode == 2'b10) begin  // Mutated: timestamp mode decodes on 10 instead of 11",
        "description": "IEEE 1588 PTP & timing discovery bug: OP_WAITEDGE timestamp mode decodes on edge_mode 2'b10 (any-edge stall) instead of 2'b11, breaking single-cycle cycle counter latching (WAITEDGE rd, 0x18) and causing PTP egress/ingress timestamps to hang waiting for pin toggles",
    },
    {
        "id": "MUT_58_I3C_HDR_DOUBLE_EDGE_CLOCK_INVERT",
        "category": "MIPI I3C HDR-DDR / Dual-Edge Timing",
        "file": "src/core.v",
        "target": "                      (edge_mode == 2'b10) ? edge_any : 1'b1;",
        "replacement": "                      (edge_mode == 2'b10) ? edge_rise : 1'b1;  // Mutated: any-edge mode detects only rising edges instead of both edges",
        "description": "MIPI I3C v1.2 HDR-DDR double-edge clocking bug: OP_WAITEDGE any-edge mode (edge_mode 2'b10) detects only rising edges (edge_rise) instead of both edges (edge_any), breaking double data rate reception and causing slave word ingress to drop all odd data bits",
    },
    {
        "id": "MUT_59_USB_FS_FALLING_EDGE_SOP_INVERT",
        "category": "USB 2.0 Full-Speed / SOP Edge Detection",
        "file": "src/core.v",
        "target": "  wire edge_matched = (edge_mode == 2'b00) ? edge_fall :",
        "replacement": "  wire edge_matched = (edge_mode == 2'b00) ? edge_rise :  // Mutated: falling-edge mode detects rising edges instead",
        "description": "USB 2.0 Full-Speed SOP edge detection bug: OP_WAITEDGE falling-edge mode (edge_mode 2'b00) detects rising edges instead of falling edges, causing Start-of-Packet J-to-K transition detection on D+ to stall indefinitely",
    },
    {
        "id": "MUT_60_100BASE_TX_WAITEDGE_RISE_INVERT",
        "category": "Fast Ethernet 100BASE-TX / SSD Rising Edge Detection",
        "file": "src/core.v",
        "target": "                      (edge_mode == 2'b01) ? edge_rise :",
        "replacement": "                      (edge_mode == 2'b01) ? edge_fall :  // Mutated: rising-edge mode detects falling edges instead",
        "description": "Fast Ethernet 100BASE-TX Start-of-Stream Delimiter detection bug: OP_WAITEDGE rising-edge mode (edge_mode 2'b01) detects falling edges instead of rising edges, causing /J/ /K/ SSD delimiter transition detection on TXP to stall indefinitely",
    },
    {
        "id": "MUT_61_1000BASE_T_MOV_INVERT",
        "category": "Gigabit Ethernet 1000BASE-T / Register Data Move",
        "file": "src/core.v",
        "target": "              OP_MOV: begin\n                write_rd(rd_idx, rs_val);",
        "replacement": "              OP_MOV: begin\n                write_rd(rd_idx, ~rs_val);  // Mutated: MOV inverts source register value",
        "description": "Gigabit Ethernet 1000BASE-T quad preservation bug: OP_MOV inverts the transferred source register value (~rs_val instead of rs_val), corrupting the preserved 4-pair quad in R1 during slave ingress (MOV R1, R0) and causing packet verification to fail",
    },
    {
        "id": "MUT_62_USB_SS_GWRI_DATA_INVERT",
        "category": "USB 3.0 SuperSpeed / GPIO Drive Subsystem",
        "file": "src/core.v",
        "target": "              OP_GWRI:  gpio_out <= operand;",
        "replacement": "              OP_GWRI:  gpio_out <= ~operand;  // Mutated: GWRI inverts output bits",
        "description": "USB 3.0 SuperSpeed transmit & LFPS burst bug: OP_GWRI inverts the driven GPIO bus operand (~operand instead of operand), corrupting differential SSTX+/SSTX- levels, destroying 8b/10b symbol transmissions, and breaking LFPS square-wave signaling",
    },
    {
        "id": "MUT_63_PCIE_ALU_XOR_INVERT",
        "category": "PCIe Base Gen 1 / ALU XOR Stream Descrambler",
        "file": "src/alu.v",
        "target": "      OP_XOR:  result = a ^ b;",
        "replacement": "      OP_XOR:  result = ~(a ^ b);  // Mutated: ALU XOR produces XNOR inversion",
        "description": "PCIe Gen 1 LFSR descrambler & FTS validator bug: ALU OP_XOR computes bitwise XNOR (~(a ^ b) instead of a ^ b), corrupting LFSR stream descrambling and causing FTS ordered set symbol validation to fail",
    },
    {
        "id": "MUT_64_10GBASE_R_SHIFTIN_INV",
        "category": "Ethernet 10GBASE-R / PCS Ingress Data Sampling",
        "file": "src/core.v",
        "target": "                  write_rd(rd_idx, {gpio_in[pin_idx], rd_val[7:1]});",
        "replacement": "                  write_rd(rd_idx, {~gpio_in[pin_idx], rd_val[7:1]});  // Mutated: SHIFTIN inverts sampled pin bit",
        "description": "Ethernet 10GBASE-R PCS sync ingress and data sampling bug: OP_SHIFTIN inverts sampled pin data (~gpio_in[pin_idx] instead of gpio_in[pin_idx]), corrupting sampled symbols in R0 during receiver ingress",
    },
    {
        "id": "MUT_65_SATA_ALU_SUB_INVERT",
        "category": "Serial ATA Gen 3 / ALU Subtraction & OOB Discrimination",
        "file": "src/alu.v",
        "target": "      OP_SUB:  result = a - b;",
        "replacement": "      OP_SUB:  result = a + b;  // Mutated: ALU subtract performs addition",
        "description": "SATA Revision 3.0 OOB quiet duration timing discrimination bug: ALU OP_SUB computes addition (a + b instead of a - b), causing in-register quiet threshold subtraction (SUBI R3, threshold) to produce positive sums for both short and long intervals, corrupting COMWAKE vs COMRESET classification",
    },
    {
        "id": "MUT_66_DPHY_WAITEDGE_RISE_INV",
        "category": "MIPI D-PHY v2.5 / WAITEDGE Rising Edge Synchronization",
        "file": "src/core.v",
        "target": "                      (edge_mode == 2'b01) ? edge_rise :",
        "replacement": "                      (edge_mode == 2'b01) ? edge_fall :  // Mutated: rising edge mode evaluates edge_fall",
        "description": "MIPI D-PHY v2.5 SoT sync edge synchronization bug: WAITEDGE rising-edge mode (2'b01) evaluates falling edge condition (edge_fall instead of edge_rise), causing slave receiver SoT sync detection on Dp to hang or trigger on illegal polarity",
    },
    {
        "id": "MUT_67_CPHY_PIN_IDX_SLICE",
        "category": "MIPI C-PHY v2.0 / Pin Index Decode",
        "file": "src/core.v",
        "target": "  wire [2:0] pin_idx = operand[2:0];",
        "replacement": "  wire [2:0] pin_idx = operand[3:1];  // Mutated: pin_idx sliced from operand[3:1]",
        "description": "MIPI C-PHY trio pin decode bug: pin_idx sliced from operand[3:1] instead of operand[2:0], corrupting pin indexing across WAITEDGE and SHIFTIN operations",
    },
    {
        "id": "MUT_68_DP20_ALU_XORI_DECODE",
        "category": "DisplayPort 2.0 / ALU XORI Decode",
        "file": "src/core.v",
        "target": "      OP_XORI: alu_op = ALU_XOR;",
        "replacement": "      OP_XORI: alu_op = ALU_OR;  // Mutated: XORI executes OR instead of XOR",
        "description": "DisplayPort 2.0 sync header and LFSR descrambler bug: OP_XORI decodes to ALU_OR instead of ALU_XOR, corrupting in-register header matching and stream descrambling",
    },
    {
        "id": "MUT_69_SAS4_ALU_ANDI_DECODE",
        "category": "SAS-4 24G / ALU ANDI Decode",
        "file": "src/core.v",
        "target": "      OP_ANDI: alu_op = ALU_AND;",
        "replacement": "      OP_ANDI: alu_op = ALU_OR;  // Mutated: ANDI executes OR instead of AND",
        "description": "SAS-4 24G sync header masking and primitive filtering bug: OP_ANDI decodes to ALU_OR instead of ALU_AND, corrupting bitwise masking of sync headers and primitive fields",
    },
    {
        "id": "MUT_70_RAPIDIO_ALU_ORI_DECODE",
        "category": "RapidIO v4.0 / ALU ORI Decode",
        "file": "src/core.v",
        "target": "      OP_ORI:  alu_op = ALU_OR;",
        "replacement": "      OP_ORI:  alu_op = ALU_AND;  // Mutated: ORI executes AND instead of OR",
        "description": "RapidIO v4.0 control symbol masking and field composition bug: OP_ORI decodes to ALU_AND instead of ALU_OR, corrupting in-register bitwise composition of control symbols and parameters",
    },
    {
        "id": "MUT_71_INFINIBAND_ALU_SUBI_DECODE",
        "category": "InfiniBand HDR/NDR / ALU SUBI Decode",
        "file": "src/core.v",
        "target": "      OP_SUBI: alu_op = ALU_SUB;",
        "replacement": "      OP_SUBI: alu_op = ALU_ADD;  // Mutated: SUBI executes ADD instead of SUB",
        "description": "InfiniBand HDR/NDR packet length validation and link credit accounting bug: OP_SUBI decodes to ALU_ADD instead of ALU_SUB, causing payload length checking and credit decrementing to add instead of subtract",
    },
    {
        "id": "MUT_72_FC_DECJNZ_DECREMENT_VALUE",
        "category": "Fibre Channel 32G/64G / DECJNZ Decrement Value",
        "file": "src/core.v",
        "target": "    if (opcode == OP_DECJNZ) alu_b = 8'hFF;  // -1 mod 256",
        "replacement": "    if (opcode == OP_DECJNZ) alu_b = 8'hFE;  // Mutated: DECJNZ decrements by 2 (-2 mod 256)",
        "description": "Fibre Channel BB_Credit loop and timeout accounting bug: DECJNZ decrements counter by 2 (alu_b = 8'hFE instead of 8'hFF), corrupting loop iterations, premature timeout exits, and buffer credit accounting",
    },
    {
        "id": "MUT_73_CXL_ALU_XORI_DECODE",
        "category": "Coherent Accelerator (CXL / OpenCAPI) / ALU XORI Decode",
        "file": "src/core.v",
        "target": "      OP_XORI: alu_op = ALU_XOR;",
        "replacement": "      OP_XORI: alu_op = ALU_OR;  // Mutated: XORI executes OR instead of XOR",
        "description": "CXL / OpenCAPI FLIT CRC-16 computation and header parity verification bug: OP_XORI decodes to ALU_OR instead of ALU_XOR, corrupting Galois-field polynomial reduction, LFSR bit mixing, and parity validation",
    },
    {
        "id": "MUT_74_HT_JNZ_INVERTED_BRANCH_CONDITION",
        "category": "HyperTransport 3.1 / JNZ Inverted Branch Condition",
        "file": "src/core.v",
        "target": "              OP_JNZ: if (!z) pc <= operand[ADDR_WIDTH-1:0];",
        "replacement": "              OP_JNZ: if (z) pc <= operand[ADDR_WIDTH-1:0];  // Mutated: JNZ branches on z=1 instead of z=0",
        "description": "HyperTransport 3.1 flow control credit polling and command dispatching bug: OP_JNZ inverts branch condition by checking z instead of !z, causing loops and mismatch traps to branch on zero instead of non-zero",
    },
    {
        "id": "MUT_75_UEC_SHIFTIN_LSB_BIT_INVERT",
        "category": "Ultra Ethernet (UEC) & InfiniBand XDR / SHIFTIN LSB Bit Inversion",
        "file": "src/core.v",
        "target": "                  write_rd(rd_idx, {gpio_in[pin_idx], rd_val[7:1]});",
        "replacement": "                  write_rd(rd_idx, {!gpio_in[pin_idx], rd_val[7:1]});  // Mutated: SHIFTIN LSB inverts sampled pin bit",
        "description": "Ultra Ethernet Consortium (UEC) & InfiniBand XDR/GDR packet ingress bug: OP_SHIFTIN inverts incoming LSB-first bitstream bit (!gpio_in[pin_idx]), corrupting sampled packet headers, opcodes, and sequence numbers",
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
