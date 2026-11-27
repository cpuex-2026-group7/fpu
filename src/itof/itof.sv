`timescale 1ns / 1ps
`default_nettype none

module itof (
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
  // 符号・ゼロ判定・絶対値化
  logic s;
  logic is_zero;
  logic [31:0] abs_val;
  assign s       = rs1_ff[31];
  assign is_zero = (rs1_ff == 32'd0);
  assign abs_val = s ? (~rs1_ff + 32'd1) : rs1_ff;

  // LZC & 左シフト
  logic [4:0]  lzc;
  logic [31:0] norm;

  always_comb begin
    lzc  = 5'd0;
    norm = abs_val;

    if (norm[31:16] == 16'd0) begin lzc[4] = 1'b1; norm = norm << 16; end
    if (norm[31:24] ==  8'd0) begin lzc[3] = 1'b1; norm = norm <<  8; end
    if (norm[31:28] ==  4'd0) begin lzc[2] = 1'b1; norm = norm <<  4; end
    if (norm[31:30] ==  2'd0) begin lzc[1] = 1'b1; norm = norm <<  2; end
    if (norm[31]    ==  1'b0) begin lzc[0] = 1'b1; norm = norm <<  1; end
  end

  // 指数ベース計算
  logic [7:0] exp_base;
  assign exp_base  = 8'd158 - {3'b0, lzc};

  // 最近接丸め（RNE）
  logic guard;
  logic sticky_rs;
  logic round_up;
  assign guard     =  norm[7];
  assign sticky_rs = |norm[6:0];
  assign round_up  =  guard & (sticky_rs | norm[8]);

  // ラッチ
  logic [7:0]  exp_base_ff;
  logic [22:0] frac_norm_ff;
  logic        round_up_ff;
  logic        s_ff;
  logic        is_zero_ff;

  always_ff @(posedge clk) begin
    exp_base_ff  <= exp_base;
    frac_norm_ff <= norm[30:8];
    round_up_ff  <= round_up;
    s_ff         <= s;
    is_zero_ff   <= is_zero;
  end

  // =========================================================
  // 2段目
  // =========================================================
  // 丸め加算
  logic [7:0]  exp_final;
  logic [22:0] frac_final;
  assign {exp_final, frac_final} = {exp_base_ff, frac_norm_ff} + {30'd0, round_up_ff};

  // 結果生成
  assign result = is_zero_ff ? 32'h0000_0000 : {s_ff, exp_final, frac_final};

endmodule

`default_nettype wire
