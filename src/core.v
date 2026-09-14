/*
 * Copyright (c) 2026 Jane Street Protocol Emulator ASIC project contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * core.v - Deterministic, single-instruction-per-cycle protocol engine core
 * implementing ISA v0. See docs/isa.md for the full instruction set
 * reference; the opcode encoding here MUST stay in sync with docs/isa.md and
 * with tools/isa_model.py and tools/assembler.py (the differential test in
 * test/test.py exists specifically to catch any drift between these).
 *
 * Instruction word layout (16 bits):
 *   [15:11] opcode  (5 bits)
 *   [10:9]  rd      (2 bits, register index 0-3)
 *   [8:1]   operand (8 bits, reinterpreted per-opcode: imm8 / addr8 / rs)
 *   [0]     reserved (must be 0 in v0)
 *
 * No internal debug ports are exposed on purpose: TT's tt_um_* wrapper has a
 * fixed pinout, so verification reaches into this module's registers via
 * cocotb's hierarchical signal access instead (see docs/verification.md).
 */

`default_nettype none

module core #(
    parameter ADDR_WIDTH = 8,
    parameter INIT_FILE  = "program.hex"
) (
    input  wire       clk,
    input  wire       rst_n,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire [7:0] uio_in
);

  // ---------------------------------------------------------------------
  // Opcodes (keep in sync with docs/isa.md)
  // ---------------------------------------------------------------------
  localparam [4:0] OP_NOP    = 5'd0;
  localparam [4:0] OP_LDI    = 5'd1;
  localparam [4:0] OP_MOV    = 5'd2;
  localparam [4:0] OP_ADDI   = 5'd3;
  localparam [4:0] OP_SUBI   = 5'd4;
  localparam [4:0] OP_ANDI   = 5'd5;
  localparam [4:0] OP_ORI    = 5'd6;
  localparam [4:0] OP_XORI   = 5'd7;
  localparam [4:0] OP_GDIRI  = 5'd8;
  localparam [4:0] OP_GDIR   = 5'd9;
  localparam [4:0] OP_GWRI   = 5'd10;
  localparam [4:0] OP_GWR    = 5'd11;
  localparam [4:0] OP_GRD    = 5'd12;
  localparam [4:0] OP_WAIT   = 5'd13;
  localparam [4:0] OP_JMP    = 5'd14;
  localparam [4:0] OP_JZ     = 5'd15;
  localparam [4:0] OP_JNZ    = 5'd16;
  localparam [4:0] OP_DECJNZ = 5'd17;
  localparam [4:0] OP_HALT   = 5'd18;

  localparam [2:0] ALU_ADD = 3'd0;
  localparam [2:0] ALU_SUB = 3'd1;
  localparam [2:0] ALU_AND = 3'd2;
  localparam [2:0] ALU_OR  = 3'd3;
  localparam [2:0] ALU_XOR = 3'd4;

  // ---------------------------------------------------------------------
  // Architectural state
  // ---------------------------------------------------------------------
  reg [ADDR_WIDTH-1:0] pc;
  reg [7:0] r0, r1, r2, r3;
  reg z;
  reg halted;
  reg [7:0] wait_remaining;

  reg [7:0] gpio_dir;
  reg [7:0] gpio_out;
  wire [7:0] gpio_in;

  // ---------------------------------------------------------------------
  // Submodules
  // ---------------------------------------------------------------------
  wire [15:0] instr;

  program_rom #(
      .ADDR_WIDTH(ADDR_WIDTH),
      .INIT_FILE (INIT_FILE)
  ) u_rom (
      .addr(pc),
      .data(instr)
  );

  gpio u_gpio (
      .clk    (clk),
      .rst_n  (rst_n),
      .dir    (gpio_dir),
      .out_val(gpio_out),
      .pin_in (uio_in),
      .pin_out(uio_out),
      .pin_oe (uio_oe),
      .in_sync(gpio_in)
  );

  // Decode
  wire [4:0] opcode  = instr[15:11];
  wire [1:0] rd_idx  = instr[10:9];
  wire [7:0] operand = instr[8:1];
  wire [1:0] rs_idx  = operand[1:0];

  // NOTE: deliberately NOT a function-based continuous assign here. Icarus
  // Verilog (observed with v13.0) does not reliably re-trigger a continuous
  // assignment driven by a function call when the function reads
  // module-level regs (r0-r3) that aren't part of its argument list - only
  // changes to the explicit argument (rd_idx/rs_idx) were tracked. That
  // produced stale reads whenever consecutive instructions targeted the same
  // register (e.g. ANDI R0 -> ORI R0 -> XORI R0, where rd_idx never changes).
  // Explicit muxes have unambiguous sensitivity.
  wire [7:0] rd_val = (rd_idx == 2'd0) ? r0 :
                       (rd_idx == 2'd1) ? r1 :
                       (rd_idx == 2'd2) ? r2 : r3;
  wire [7:0] rs_val = (rs_idx == 2'd0) ? r0 :
                       (rs_idx == 2'd1) ? r1 :
                       (rs_idx == 2'd2) ? r2 : r3;

  reg [2:0] alu_op;
  reg [7:0] alu_b;
  wire [7:0] alu_result;

  alu u_alu (
      .a(rd_val),
      .b(alu_b),
      .op(alu_op),
      .result(alu_result)
  );

  always @(*) begin
    case (opcode)
      OP_SUBI: alu_op = ALU_SUB;
      OP_ANDI: alu_op = ALU_AND;
      OP_ORI:  alu_op = ALU_OR;
      OP_XORI: alu_op = ALU_XOR;
      default: alu_op = ALU_ADD;  // ADDI and DECJNZ's implicit -1 both use ADD path via alu_b
    endcase
    if (opcode == OP_DECJNZ) alu_b = 8'hFF;  // -1 mod 256
    else alu_b = operand;
  end

  task write_rd;
    input [1:0] idx;
    input [7:0] val;
    begin
      case (idx)
        2'd0: r0 <= val;
        2'd1: r1 <= val;
        2'd2: r2 <= val;
        default: r3 <= val;
      endcase
    end
  endtask

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      pc             <= {ADDR_WIDTH{1'b0}};
      r0             <= 8'h00;
      r1             <= 8'h00;
      r2             <= 8'h00;
      r3             <= 8'h00;
      z              <= 1'b0;
      halted         <= 1'b0;
      wait_remaining <= 8'h00;
      gpio_dir       <= 8'h00;
      gpio_out       <= 8'h00;
    end else if (halted) begin
      // Halted: hold all state until reset.
    end else if (wait_remaining != 8'h00) begin
      wait_remaining <= wait_remaining - 8'h01;
    end else begin
      pc <= pc + {{(ADDR_WIDTH - 1) {1'b0}}, 1'b1};

      case (opcode)
        OP_NOP: ;  // no state change

        OP_LDI: begin
          write_rd(rd_idx, operand);
          z <= (operand == 8'h00);
        end

        OP_MOV: begin
          write_rd(rd_idx, rs_val);
          z <= (rs_val == 8'h00);
        end

        OP_ADDI, OP_SUBI, OP_ANDI, OP_ORI, OP_XORI: begin
          write_rd(rd_idx, alu_result);
          z <= (alu_result == 8'h00);
        end

        OP_GDIRI: gpio_dir <= operand;
        OP_GDIR:  gpio_dir <= rd_val;
        OP_GWRI:  gpio_out <= operand;
        OP_GWR:   gpio_out <= rd_val;

        OP_GRD: begin
          write_rd(rd_idx, gpio_in);
          z <= (gpio_in == 8'h00);
        end

        OP_WAIT: wait_remaining <= operand;

        OP_JMP: pc <= operand[ADDR_WIDTH-1:0];

        OP_JZ: if (z) pc <= operand[ADDR_WIDTH-1:0];

        OP_JNZ: if (!z) pc <= operand[ADDR_WIDTH-1:0];

        OP_DECJNZ: begin
          write_rd(rd_idx, alu_result);
          z <= (alu_result == 8'h00);
          if (alu_result != 8'h00) pc <= operand[ADDR_WIDTH-1:0];
        end

        OP_HALT: halted <= 1'b1;

        default: ;  // reserved/illegal encodings behave as NOP in v0
      endcase
    end
  end

endmodule
