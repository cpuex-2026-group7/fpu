`timescale 1ns / 1ps
`default_nettype none

// 丸めは最近接丸め(四捨五入)
module ftoi (
  input  wire logic        clk,
  input  wire logic [31:0] rs1_val,

  output logic [31:0] result
);

  logic [31:0] rs1_ff;

  always_ff @(posedge clk) begin
    rs1_ff <= rs1_val;
  end

  // =========================================================
  // 1段目
  // =========================================================
  logic        s;
  logic [7:0]  exp;
  logic [22:0] frac;
  logic [31:0] m_ext;
  assign {s, exp, frac} = rs1_ff;
  assign m_ext = {1'b1, frac, 8'b0};

  // 32bit右バレルシフタ
  logic [7:0]  exp_diff;
  logic [4:0]  rshift;
  logic [31:0] shifted;
  assign exp_diff = 8'd157 - exp;
  assign rshift   = exp_diff[4:0];
  assign shifted  = m_ext >> rshift;

  logic [31:0] abs_int;
  logic        r;
  assign abs_int = {1'b0, shifted[31:1]};
  assign r       = shifted[0];

  // 四捨五入 + 符号反転の融合
  logic        is_small;
  logic [31:0] base;
  logic        cin;
  assign is_small = (exp < 8'd126);
  assign base     = is_small ? 32'd0 : (abs_int ^ {32{s}});
  assign cin      = is_small ? 1'b0  : (s ^ r);

  // 結果生成
  assign result = base + {31'd0, cin};

endmodule

`default_nettype wire
