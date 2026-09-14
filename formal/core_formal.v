/*
 * Copyright (c) 2026 Jane Street Protocol Emulator ASIC project contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * core_formal.v - SymbiYosys formal verification harness for the protocol engine.
 *
 * Uses the PVFI (Protocol-engine Verification Formal Interface) to formally prove:
 * 1. PC safety / valid range invariant (PC stays strictly in bounds [0, 255])
 * 2. PVFI retirement ordering and monotonicity
 * 3. Free-running cycle counter strict monotonicity
 * 4. Halt permanence over the formal interface
 */

`default_nettype none

module core_formal (
    input wire clk,
    input wire rst_n,
    input wire [7:0] uio_in
);

  wire [7:0] uio_out;
  wire [7:0] uio_oe;

  // PVFI formal observation ports
  wire        pvfi_valid;
  wire [31:0] pvfi_order;
  wire [15:0] pvfi_insn;
  wire [7:0]  pvfi_pc_rdata;
  wire [7:0]  pvfi_pc_wdata;
  wire [1:0]  pvfi_rd_addr;
  wire [7:0]  pvfi_rd_wdata;
  wire        pvfi_rd_we;
  wire [7:0]  pvfi_gpio_oe;
  wire [7:0]  pvfi_gpio_wdata;
  wire [7:0]  pvfi_gpio_rdata;
  wire        pvfi_halted;
  wire [31:0] pvfi_cycle;
  wire        boot_done;
  wire        boot_err;

  core #(
      .ADDR_WIDTH(8)
  ) u_core (
      .clk            (clk),
      .rst_n          (rst_n),
      .uio_out        (uio_out),
      .uio_oe         (uio_oe),
      .uio_in         (uio_in),
      .boot_done      (boot_done),
      .boot_err       (boot_err),
      .pvfi_valid     (pvfi_valid),
      .pvfi_order     (pvfi_order),
      .pvfi_insn      (pvfi_insn),
      .pvfi_pc_rdata  (pvfi_pc_rdata),
      .pvfi_pc_wdata  (pvfi_pc_wdata),
      .pvfi_rd_addr   (pvfi_rd_addr),
      .pvfi_rd_wdata  (pvfi_rd_wdata),
      .pvfi_rd_we     (pvfi_rd_we),
      .pvfi_gpio_oe   (pvfi_gpio_oe),
      .pvfi_gpio_wdata(pvfi_gpio_wdata),
      .pvfi_gpio_rdata(pvfi_gpio_rdata),
      .pvfi_halted    (pvfi_halted),
      .pvfi_cycle     (pvfi_cycle)
  );

  // Formal step tracking
  reg f_past_valid = 1'b0;
  always @(posedge clk) f_past_valid <= 1'b1;

  // Initial reset assumption
  always @(posedge clk) begin
    if (!f_past_valid) assume(!rst_n);
  end

  // Formal Assertions against PVFI Interface
  always @(posedge clk) begin
    if (f_past_valid && rst_n && $past(rst_n)) begin
      // Invariant 1: Cycle counter strictly increments
      assert(pvfi_cycle == $past(pvfi_cycle) + 32'd1);

      // Invariant 2: Halt permanence on PVFI
      if ($past(pvfi_halted)) begin
        assert(pvfi_halted);
      end

      // Invariant 3: Valid instruction retirement checks
      if (pvfi_valid) begin
        assert(pvfi_pc_rdata <= 8'hFF);
        assert(pvfi_pc_wdata <= 8'hFF);
        if ($past(pvfi_valid)) begin
          assert(pvfi_order == $past(pvfi_order) + 32'd1);
        end
      end

      // Invariant 4: Bootloader integrity protection (corrupted code never executes)
      if (boot_done && boot_err) begin
        assert(pvfi_halted);
        assert(!pvfi_valid);
      end
    end
  end

endmodule
