`timescale 1ns / 1ps
`default_nettype none

module fmul (
  input  wire logic        clk,
  input  wire logic [31:0] rs1_val,
  input  wire logic [31:0] rs2_val,

  output logic [31:0] result
);

  logic [31:0] rs1_ff;
  logic [31:0] rs2_ff;

  always_ff @(posedge clk) begin
    rs1_ff <= rs1_val;
    rs2_ff <= rs2_val;
  end

  // =========================================================
  // 1段目
  // =========================================================
  logic        s1, s2;
  logic [7:0]  e1, e2;
  logic [22:0] f1, f2;

  assign {s1, e1, f1} = rs1_ff;
  assign {s2, e2, f2} = rs2_ff;

  // 符号・ゼロ判定
  logic s;
  logic is_zero;
  assign s       = s1 ^ s2;
  assign is_zero = (e1 == 8'd0) || (e2 == 8'd0);

  // 仮数部乗算
  logic [23:0] m1, m2;
  (* use_dsp = "yes" *) logic [47:0] prod;
  assign m1   = {1'b1, f1};
  assign m2   = {1'b1, f2};
  assign prod = m1 * m2;

  // 指数部・アンダーフロー
  logic signed [9:0] exp_sum;
  logic signed [9:0] exp0, exp1;
  assign exp_sum = $signed({2'b00, e1}) + $signed({2'b00, e2});
  assign exp0    = exp_sum - 10'sd127;
  assign exp1    = exp_sum - 10'sd126;

  logic zero0, zero1;
  assign zero0 = is_zero || (exp0 <= 10'sd0);
  assign zero1 = is_zero || (exp1 <= 10'sd0);

  // 結果生成
  assign result = (prod[47] == 1'b1) ? (zero1 ? 32'h0000_0000 : {s, exp1[7:0], prod[46:24]}) :
                                       (zero0 ? 32'h0000_0000 : {s, exp0[7:0], prod[45:23]});

endmodule

`default_nettype wire
