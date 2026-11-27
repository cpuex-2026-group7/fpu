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

  (* use_dsp = "yes" *) logic [47:0] prod;
  logic signed [9:0] exp_base, exp_base_p1;
  logic              s;
  logic              is_zero;

  // 仮数部乗算
  logic [23:0] m1, m2;
  assign m1   = {1'b1, f1};
  assign m2   = {1'b1, f2};
  assign prod = m1 * m2;

  // 指数加算
  logic signed [9:0] exp_sum;
  assign exp_sum     = $signed({2'b00, e1}) + $signed({2'b00, e2});
  assign exp_base    = exp_sum - 10'sd127;
  assign exp_base_p1 = exp_sum - 10'sd126;

  // 符号・ゼロ判定
  assign s           = s1 ^ s2;
  assign is_zero     = (e1 == 8'd0) || (e2 == 8'd0);

  // ラッチ
  logic        [47:17] prod_hi_ff;
  logic signed [9:0]   exp_base_ff;
  logic                exp_base_lt0_ff, exp_base_eq0_ff;
  logic signed [9:0]   exp_base_p1_ff;
  logic                exp_base_p1_lt0_ff, exp_base_p1_eq0_ff;
  logic                sticky_lo17_ff;
  logic                s_ff;
  logic                is_zero_ff;

  always_ff @(posedge clk) begin
    prod_hi_ff         <= prod[47:17];
    exp_base_ff        <= exp_base;
    exp_base_lt0_ff    <= (exp_base < 10'sd0);
    exp_base_eq0_ff    <= (exp_base == 10'sd0);
    exp_base_p1_ff     <= exp_base_p1;
    exp_base_p1_lt0_ff <= (exp_base_p1 < 10'sd0);
    exp_base_p1_eq0_ff <= (exp_base_p1 == 10'sd0);
    sticky_lo17_ff     <= |prod[16:0];
    s_ff               <= s;
    is_zero_ff         <= is_zero;
  end

  // =========================================================
  // 2段目
  // =========================================================
  logic signed [9:0]  exp_norm;
  logic               exp_lt0, exp_eq0;
  logic        [22:0] frac_norm;
  logic               guard;
  logic               round;
  logic               sticky;
  logic               sticky_lo;
  assign sticky_lo = (|prod_hi_ff[20:17]) | sticky_lo17_ff;

  // 仮数部正規化・RNEビット位置計算
  always_comb begin
    if (prod_hi_ff[47]) begin
      exp_norm  = exp_base_p1_ff;
      exp_lt0   = exp_base_p1_lt0_ff;
      exp_eq0   = exp_base_p1_eq0_ff;
      frac_norm = prod_hi_ff[46:24];
      guard     = prod_hi_ff[23];
      round     = prod_hi_ff[22];
      sticky    = prod_hi_ff[21] | sticky_lo;
    end else begin
      exp_norm  = exp_base_ff;
      exp_lt0   = exp_base_lt0_ff;
      exp_eq0   = exp_base_eq0_ff;
      frac_norm = prod_hi_ff[45:23];
      guard     = prod_hi_ff[22];
      round     = prod_hi_ff[21];
      sticky    = sticky_lo;
    end
  end

  // 最近接偶数丸め（RNE）
  logic round_up;
  logic round_carry;
  assign round_up    = guard & (round | sticky | frac_norm[0]);
  assign round_carry = (&frac_norm) & round_up;

  logic signed [9:0]  exp_final;
  logic        [22:0] frac_final;
  assign {exp_final, frac_final} = {exp_norm, frac_norm} + {32'd0, round_up};

  // アンダーフロー判定
  logic underflow;
  assign underflow = exp_lt0 || (exp_eq0 && !round_carry);

  // 結果生成
  assign result = (is_zero_ff || underflow) ? 32'h0000_0000 : {s_ff, exp_final[7:0], frac_final};

endmodule

`default_nettype wire
