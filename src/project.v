/*
 * Copyright (c) 2026 Jane Street Protocol Emulator ASIC project contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * project.v - Tiny Tapeout top-level wrapper for the programmable protocol
 * emulator (ISA v0 bootstrap). See docs/architecture.md and docs/isa.md.
 *
 * Pin mapping (v0):
 *   uio[7:0] - the programmable protocol GPIO bus (GDIR, GWR, GRD instructions)
 *   uo_out   - reserved for future use (tied to 0 in v0, see docs/limitations.md)
 *   ui_in    - reserved for future use (unused in v0, see docs/limitations.md)
 *
 * TODO before Tiny Tapeout submission: rename this module (and the
 * top_module entry in info.yaml) to include the actual GitHub username,
 * per Tiny Tapeout's uniqueness requirement. Tracked in orchestrator/queue.md.
 */

`default_nettype none

module tt_um_change_me_protocol_emulator (
    input  wire [7:0] ui_in,    // Dedicated inputs (reserved, unused in v0)
    output wire [7:0] uo_out,   // Dedicated outputs (reserved, unused in v0)
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered
    input  wire        clk,      // clock
    input  wire        rst_n     // reset_n - low to reset
);

  core #(
      .ADDR_WIDTH(8),
      .INIT_FILE ("program.hex")
  ) u_core (
      .clk    (clk),
      .rst_n  (rst_n),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .uio_in (uio_in)
  );

  assign uo_out = 8'h00;

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, ui_in, 1'b0};

endmodule
