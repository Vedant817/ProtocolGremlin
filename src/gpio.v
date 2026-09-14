/*
 * Copyright (c) 2026 Jane Street Protocol Emulator ASIC project contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * gpio.v - Programmable protocol GPIO bus, mapped onto Tiny Tapeout's
 * bidirectional uio[7:0] pins.
 *
 * dir/out_val are architectural registers written by the core (GDIR,
 * GWR instructions). pin_in is the raw external uio_in value; it is passed
 * through a 2-flop synchronizer before being made available to the core
 * (GRD instruction), since uio_in may be driven by an external, asynchronous
 * source on real silicon.
 *
 * ui_in / uo_out (the dedicated TT pins) are NOT touched by this module in
 * v0 - see docs/limitations.md.
 */

`default_nettype none

module gpio (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] dir,      // 1 = output, 0 = input
    input  wire [7:0] out_val,  // -> uio_out data
    input  wire [7:0] od_mode,  // 1 = open-drain mode, 0 = push-pull mode
    input  wire [7:0] pin_in,   // <- uio_in (raw, async)
    output wire [7:0] pin_out,  // -> uio_out
    output wire [7:0] pin_oe,   // -> uio_oe
    output reg  [7:0] in_sync   // synchronized value visible to GRD
);

  reg [7:0] sync_stage0;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      sync_stage0 <= 8'h00;
      in_sync     <= 8'h00;
    end else begin
      sync_stage0 <= pin_in;
      in_sync     <= sync_stage0;
    end
  end

  // Push-pull mode: pin_out = out_val, pin_oe = dir
  // Open-drain mode:
  //   pin_out = 1'b0 (never drive high actively)
  //   pin_oe  = 1 when dir=1 and out_val=0 (drive 0 actively)
  //   pin_oe  = 0 when dir=0 or out_val=1 (high-Z / released to pullup)
  assign pin_out = out_val & ~od_mode;
  assign pin_oe  = (dir & ~od_mode) | (dir & od_mode & ~out_val);

endmodule
