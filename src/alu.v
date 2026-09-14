/*
 * Copyright (c) 2026 Jane Street Protocol Emulator ASIC project contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * alu.v - Combinational 8-bit ALU for the protocol engine core (ISA v0).
 *
 * See docs/isa.md for the opcode table this feeds.
 */

`default_nettype none

module alu (
    input  wire [7:0] a,
    input  wire [7:0] b,
    input  wire [2:0] op,
    output reg  [7:0] result
);

  localparam [2:0] OP_ADD = 3'd0;
  localparam [2:0] OP_SUB = 3'd1;
  localparam [2:0] OP_AND = 3'd2;
  localparam [2:0] OP_OR  = 3'd3;
  localparam [2:0] OP_XOR = 3'd4;

  always @(*) begin
    case (op)
      OP_ADD:  result = a + b;
      OP_SUB:  result = a - b;
      OP_AND:  result = a & b;
      OP_OR:   result = a | b;
      OP_XOR:  result = a ^ b;
      default: result = 8'h00;
    endcase
  end

endmodule
