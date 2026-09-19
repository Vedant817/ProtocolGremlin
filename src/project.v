/*
 * Copyright (c) 2026 Jane Street Protocol Emulator ASIC project contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * project.v - Tiny Tapeout top-level wrapper for the programmable protocol
 * emulator. See docs/architecture.md and docs/isa.md.
 *
 * Pin mapping:
 *   uio[7:0] - the programmable protocol GPIO bus (GDIR, GWR, GRD instructions)
 *   uo_out[0] - BOOT_DONE (bootloader complete status)
 *   uo_out[1] - BOOT_ERR  (bootloader CRC error / halt lock)
 *   uo_out[7:2] - reserved (tied to 0)
 *   ui_in[0] - LOAD_REQ  (bootloader load request)
 *   ui_in[1] - LOAD_DATA (bootloader serial data input)
 *   ui_in[2] - LOAD_CLK  (bootloader serial clock input)
 *   ui_in[7:3] - reserved (unused)
 */

`default_nettype none

module tt_um_Vedant817_protocol_emulator (
    input  wire [7:0] ui_in,    // Dedicated inputs (reserved, unused in v0)
    output wire [7:0] uo_out,   // Dedicated outputs (reserved, unused in v0)
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered
    input  wire        clk,      // clock
    input  wire        rst_n     // reset_n - low to reset
);

  wire boot_done;
  wire boot_err;

  core #(
      .ADDR_WIDTH(8)
  ) u_core (
      .clk      (clk),
      .rst_n    (rst_n),
      .uio_out  (uio_out),
      .uio_oe   (uio_oe),
      .uio_in   (uio_in),
      .boot_done(boot_done),
      .boot_err (boot_err)
  );

  assign uo_out = {6'b000000, boot_err, boot_done};

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, ui_in, 1'b0};

endmodule
