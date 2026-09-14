/*
 * Copyright (c) 2026 Jane Street Protocol Emulator ASIC project contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * core.v - Deterministic protocol engine core, ISA v1.
 *
 * ISA v1 adds two things over the v0 bootstrap on top of the same 16-bit
 * instruction word format (see docs/isa.md for the full opcode reference,
 * which MUST stay in sync with this file, tools/assembler.py and
 * tools/isa_model.py - the differential test exists specifically to catch
 * drift between them):
 *
 *   1. SHIFTOUT/SHIFTIN - bit-serial shift operations against a single GPIO
 *      pin, needed for any real byte-oriented protocol firmware (UART, SPI).
 *   2. A serial bootloader FSM that runs immediately after reset and, if the
 *      external host asserts LOAD_REQ, loads a brand new program into
 *      program_ram over the SAME uio bus the protocol engine uses for
 *      everything else - no `$readmemh`, no fixed-at-elaboration-time ROM.
 *      This is what makes the chip actually reprogrammable after
 *      fabrication. See docs/isa.md "Bootloader protocol" for the exact
 *      framing and timing requirements.
 *
 * Instruction word layout (16 bits, unchanged from v0):
 *   [15:11] opcode  (5 bits)
 *   [10:9]  rd      (2 bits, register index 0-3)
 *   [8:1]   operand (8 bits, reinterpreted per-opcode: imm8 / addr8 / rs / pin)
 *   [0]     reserved (must be 0 in v1)
 */

`default_nettype none

module core #(
    parameter ADDR_WIDTH = 8
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
  localparam [4:0] OP_NOP     = 5'd0;
  localparam [4:0] OP_LDI     = 5'd1;
  localparam [4:0] OP_MOV     = 5'd2;
  localparam [4:0] OP_ADDI    = 5'd3;
  localparam [4:0] OP_SUBI    = 5'd4;
  localparam [4:0] OP_ANDI    = 5'd5;
  localparam [4:0] OP_ORI     = 5'd6;
  localparam [4:0] OP_XORI    = 5'd7;
  localparam [4:0] OP_GDIRI   = 5'd8;
  localparam [4:0] OP_GDIR    = 5'd9;
  localparam [4:0] OP_GWRI    = 5'd10;
  localparam [4:0] OP_GWR     = 5'd11;
  localparam [4:0] OP_GRD     = 5'd12;
  localparam [4:0] OP_WAIT    = 5'd13;
  localparam [4:0] OP_JMP     = 5'd14;
  localparam [4:0] OP_JZ      = 5'd15;
  localparam [4:0] OP_JNZ     = 5'd16;
  localparam [4:0] OP_DECJNZ  = 5'd17;
  localparam [4:0] OP_HALT    = 5'd18;
  localparam [4:0] OP_SHIFTOUT = 5'd19;
  localparam [4:0] OP_SHIFTIN  = 5'd20;

  localparam [2:0] ALU_ADD = 3'd0;
  localparam [2:0] ALU_SUB = 3'd1;
  localparam [2:0] ALU_AND = 3'd2;
  localparam [2:0] ALU_OR  = 3'd3;
  localparam [2:0] ALU_XOR = 3'd4;

  // ---------------------------------------------------------------------
  // Bootloader FSM states
  // ---------------------------------------------------------------------
  localparam [2:0] LD_WAIT  = 3'd0;  // settling + sampling LOAD_REQ
  localparam [2:0] LD_COUNT = 3'd1;  // shifting in the 8-bit word count
  localparam [2:0] LD_WORD  = 3'd2;  // shifting in N x 16-bit program words
  localparam [2:0] LD_DONE  = 3'd3;  // bootloader finished; normal execution

  localparam LOAD_REQ_BIT  = 0;
  localparam LOAD_CLK_BIT  = 1;
  localparam LOAD_DATA_BIT = 2;

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

  // Bootloader state
  reg [2:0] ld_state;
  reg [1:0] ld_settle_cnt;
  reg [15:0] ld_sreg;
  reg [4:0] ld_bitcnt;
  reg [7:0] ld_word_count;
  reg [7:0] ld_word_idx;
  reg ld_clk_prev;

  wire ld_clk_rise = gpio_in[LOAD_CLK_BIT] && !ld_clk_prev;

  // One-cycle write pulse into program_ram, decoded combinationally from the
  // same conditions the sequential bootloader FSM below uses to advance.
  wire ram_we = (ld_state == LD_WORD) && gpio_in[LOAD_REQ_BIT] && ld_clk_rise && (ld_bitcnt == 5'd15);
  wire [ADDR_WIDTH-1:0] ram_waddr = ld_word_idx[ADDR_WIDTH-1:0];
  wire [15:0] ram_wdata = {ld_sreg[14:0], gpio_in[LOAD_DATA_BIT]};

  // ---------------------------------------------------------------------
  // Submodules
  // ---------------------------------------------------------------------
  wire [15:0] instr;

  program_ram #(
      .ADDR_WIDTH(ADDR_WIDTH)
  ) u_ram (
      .clk  (clk),
      .addr (pc),
      .data (instr),
      .we   (ram_we),
      .waddr(ram_waddr),
      .wdata(ram_wdata)
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
  wire [2:0] pin_idx = operand[2:0];

  // See src/core.v history / orchestrator/decisions.md: explicit muxes here,
  // not a function reading module-level regs, to avoid an Icarus Verilog
  // continuous-assignment sensitivity bug found during ISA v0 bring-up.
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

      ld_state       <= LD_WAIT;
      ld_settle_cnt  <= 2'd0;
      ld_sreg        <= 16'h0000;
      ld_bitcnt      <= 5'd0;
      ld_word_count  <= 8'h00;
      ld_word_idx    <= 8'h00;
      ld_clk_prev    <= 1'b0;
    end else begin
      ld_clk_prev <= gpio_in[LOAD_CLK_BIT];

      case (ld_state)
        // -------------------------------------------------------------
        // LD_WAIT: give the GPIO input synchronizer time to settle (it
        // resets to 0 and needs 2 cycles to reflect the real pin value -
        // see src/gpio.v), then sample LOAD_REQ exactly once.
        // -------------------------------------------------------------
        LD_WAIT: begin
          if (ld_settle_cnt < 2'd2) begin
            ld_settle_cnt <= ld_settle_cnt + 2'd1;
          end else if (gpio_in[LOAD_REQ_BIT]) begin
            ld_state  <= LD_COUNT;
            ld_sreg   <= 16'h0000;
            ld_bitcnt <= 5'd0;
          end else begin
            ld_state <= LD_DONE;
          end
        end

        // -------------------------------------------------------------
        // LD_COUNT: shift in an 8-bit word count, MSB first, one bit per
        // detected LOAD_CLK rising edge.
        // -------------------------------------------------------------
        LD_COUNT: begin
          if (!gpio_in[LOAD_REQ_BIT]) begin
            ld_state <= LD_DONE;  // host aborted the load
          end else if (ld_clk_rise) begin
            if (ld_bitcnt == 5'd7) begin
              ld_word_count <= {ld_sreg[6:0], gpio_in[LOAD_DATA_BIT]};
              ld_bitcnt     <= 5'd0;
              ld_state      <= LD_WORD;
            end else begin
              ld_sreg   <= {ld_sreg[14:0], gpio_in[LOAD_DATA_BIT]};
              ld_bitcnt <= ld_bitcnt + 5'd1;
            end
          end
        end

        // -------------------------------------------------------------
        // LD_WORD: shift in ld_word_count x 16-bit words, MSB first.
        // ram_we/ram_waddr/ram_wdata (combinational, above) perform the
        // actual write in the same cycle this reaches bit 15.
        // -------------------------------------------------------------
        LD_WORD: begin
          if (ld_word_idx == ld_word_count) begin
            ld_state <= LD_DONE;  // word_count == 0: nothing to load
          end else if (!gpio_in[LOAD_REQ_BIT]) begin
            ld_state <= LD_DONE;  // host aborted the load (best-effort)
          end else if (ld_clk_rise) begin
            if (ld_bitcnt == 5'd15) begin
              ld_word_idx <= ld_word_idx + 8'd1;
              ld_bitcnt   <= 5'd0;
              if (ld_word_idx + 8'd1 == ld_word_count) ld_state <= LD_DONE;
            end else begin
              ld_sreg   <= {ld_sreg[14:0], gpio_in[LOAD_DATA_BIT]};
              ld_bitcnt <= ld_bitcnt + 5'd1;
            end
          end
        end

        // -------------------------------------------------------------
        // LD_DONE: bootloader finished (loaded or skipped). Normal
        // fetch/execute, identical in spirit to ISA v0.
        // -------------------------------------------------------------
        default: begin  // LD_DONE
          if (halted) begin
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

              OP_SHIFTOUT: begin
                gpio_out[pin_idx] <= rd_val[0];
                write_rd(rd_idx, {1'b0, rd_val[7:1]});
                z <= (rd_val[7:1] == 7'h00);
              end

              OP_SHIFTIN: begin
                write_rd(rd_idx, {gpio_in[pin_idx], rd_val[7:1]});
                z <= (rd_val[7:1] == 7'h00) && !gpio_in[pin_idx];
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

              default: ;  // reserved/illegal encodings behave as NOP in v1
            endcase
          end
        end
      endcase
    end
  end

endmodule
