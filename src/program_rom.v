/*
 * Copyright (c) 2026 Jane Street Protocol Emulator ASIC project contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * program_rom.v - v0 program memory for the protocol engine core.
 *
 * KNOWN LIMITATION (tracked in orchestrator/queue.md and docs/limitations.md):
 * this is a $readmemh-initialized array, combinationally read. That is fine
 * for RTL/gate-level simulation and for an early Yosys mapping, but:
 *   (a) it cannot be reprogrammed after fabrication, which conflicts with the
 *       competition's "reprogrammable after fabrication" requirement, and
 *   (b) combinational-read memory arrays typically do not map cleanly onto a
 *       dense SRAM macro during place-and-route.
 * A follow-up experiment (serially-loaded, synchronous-read program RAM) is
 * queued as P0 work before any PPA/synthesis milestone is treated as final.
 */

`default_nettype none

module program_rom #(
    parameter ADDR_WIDTH = 8,
    parameter INIT_FILE  = "program.hex"
) (
    input  wire [ADDR_WIDTH-1:0] addr,
    output wire [               15:0] data
);

  localparam integer DEPTH = (1 << ADDR_WIDTH);

  reg [15:0] mem[0:DEPTH-1];

  integer i;
  initial begin
    for (i = 0; i < DEPTH; i = i + 1) begin
      mem[i] = 16'h0000;  // NOP-filled by default
    end
    if (INIT_FILE != "") begin
      $readmemh(INIT_FILE, mem);
    end
  end

  assign data = mem[addr];

endmodule
