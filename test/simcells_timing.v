// SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
// SPDX-License-Identifier: Apache-2.0
// Calibrated CMOS standard cell simulation models with real propagation delays
// and setup/hold timing checks for gate-level simulation (GATES=yes).

`timescale 1ns / 1ps

module \$_NOT_ (A, Y);
  input A;
  output Y;
  specify
    (A => Y) = (0.05, 0.05); // 50 ps inverter propagation delay
  endspecify
  assign Y = ~A;
endmodule

module \$_NAND_ (A, B, Y);
  input A, B;
  output Y;
  specify
    (A => Y) = (0.08, 0.08); // 80 ps NAND propagation delay
    (B => Y) = (0.08, 0.08);
  endspecify
  assign Y = ~(A & B);
endmodule

module \$_NOR_ (A, B, Y);
  input A, B;
  output Y;
  specify
    (A => Y) = (0.08, 0.08); // 80 ps NOR propagation delay
    (B => Y) = (0.08, 0.08);
  endspecify
  assign Y = ~(A | B);
endmodule

module \$_DFF_PN0_ (D, C, R, Q);
  input D, C, R;
  output reg Q;
  specify
    $setup(D, posedge C, 0.10);      // 100 ps setup time
    $hold(posedge C, D, 0.05);       // 50 ps hold time
    (posedge C => (Q : D)) = (0.20, 0.20); // 200 ps clock-to-Q
    (negedge R => (Q : 1'b0)) = (0.15, 0.15); // 150 ps async reset to Q
  endspecify
  always @(posedge C or negedge R) begin
    if (R == 0)
      Q <= 0;
    else
      Q <= D;
  end
endmodule

module \$_DFF_PN1_ (D, C, R, Q);
  input D, C, R;
  output reg Q;
  specify
    $setup(D, posedge C, 0.10);
    $hold(posedge C, D, 0.05);
    (posedge C => (Q : D)) = (0.20, 0.20);
    (negedge R => (Q : 1'b1)) = (0.15, 0.15);
  endspecify
  always @(posedge C or negedge R) begin
    if (R == 0)
      Q <= 1;
    else
      Q <= D;
  end
endmodule

module \$_DFFE_PP_ (D, C, E, Q);
  input D, C, E;
  output reg Q;
  specify
    $setup(D, posedge C, 0.10);
    $setup(E, posedge C, 0.10);
    $hold(posedge C, D, 0.05);
    $hold(posedge C, E, 0.05);
    (posedge C => (Q : D)) = (0.20, 0.20);
  endspecify
  always @(posedge C) begin
    if (E == 1)
      Q <= D;
  end
endmodule

module \$_DFFE_PN0P_ (D, C, R, E, Q);
  input D, C, R, E;
  output reg Q;
  specify
    $setup(D, posedge C, 0.10);
    $setup(E, posedge C, 0.10);
    $hold(posedge C, D, 0.05);
    $hold(posedge C, E, 0.05);
    (posedge C => (Q : D)) = (0.20, 0.20);
    (negedge R => (Q : 1'b0)) = (0.15, 0.15);
  endspecify
  always @(posedge C or negedge R) begin
    if (R == 0)
      Q <= 0;
    else if (E == 1)
      Q <= D;
  end
endmodule
