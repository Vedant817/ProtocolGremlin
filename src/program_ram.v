/*
 * Copyright (c) 2026 Jane Street Protocol Emulator ASIC project contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * program_ram.v - ISA v1 program memory: a genuinely loadable RAM, written by
 * core.v's serial bootloader FSM over the same uio bus the protocol engine
 * uses for everything else. Replaces v0's program_rom.v ($readmemh, fixed at
 * elaboration time, not reprogrammable after fabrication - see
 * docs/limitations.md history and orchestrator/decisions.md).
 *
 * Read is still combinational (`assign data = mem[addr]`), which is simpler
 * to reason about and keeps the one-instruction-per-cycle timing model
 * unchanged. This is a known PPA/synthesis-mapping tradeoff (a combinational
 * read typically maps less densely onto a real SRAM macro than a
 * synchronous-read design) tracked as follow-up work in
 * orchestrator/queue.md; it does NOT affect reprogrammability, which is what
 * this module exists to fix.
 *
 * The `initial` NOP-fill below is a simulation-only convenience for
 * deterministic testbenches. Real silicon SRAM has undefined content until
 * the bootloader actually writes it - see docs/isa.md "Bootloader protocol".
 */

`default_nettype none

module program_ram #(
    parameter ADDR_WIDTH = 8
) (
    input  wire                  clk,
    input  wire [ADDR_WIDTH-1:0] addr,
    output wire [          15:0] data,

    input  wire                  we,
    input  wire [ADDR_WIDTH-1:0] waddr,
    input  wire [          15:0] wdata
);

  localparam integer DEPTH = (1 << ADDR_WIDTH);

  reg [15:0] mem[0:DEPTH-1];

  integer i;
  initial begin
    for (i = 0; i < DEPTH; i = i + 1) begin
      mem[i] = 16'h0000;  // simulation-only determinism, see header comment
    end
  end

  always @(posedge clk) begin
    if (we) begin
      mem[waddr] <= wdata;
    end
  end

  assign data = mem[addr];

endmodule
