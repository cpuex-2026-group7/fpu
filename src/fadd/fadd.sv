`timescale 1ns / 1ps
`default_nettype none

module fadd (
  input wire logic        clk,
  input wire logic [31:0] rs1_val,
  input wire logic [31:0] rs2_val,
  input wire logic        aux,      // 1でfsub

  output logic [31:0] result
);

  logic [31:0] rs1_ff;
  logic [31:0] rs2_ff;
  logic        aux_ff;

  always_ff @(posedge clk) begin
    rs1_ff <= rs1_val;
    rs2_ff <= rs2_val;
    aux_ff <= aux;
  end

  // =========================================================
  // 1段目：共通前処理
  // =========================================================
  logic        s1, s2;
  logic [7:0]  e1, e2;
  logic [22:0] f1, f2;
  assign {s1, e1, f1} = rs1_ff;
  assign {s2, e2, f2} = rs2_ff;

  logic s2_eff, eff_sub;
  assign s2_eff  = s2 ^ aux_ff;
  assign eff_sub = s1 ^ s2_eff;

  logic [23:0] m1, m2;
  assign m1 = (e1 == 8'd0) ? 24'd0 : {1'b1, f1};
  assign m2 = (e2 == 8'd0) ? 24'd0 : {1'b1, f2};

  logic [8:0] e1_e2;
  logic [8:0] e2_e1;
  assign e1_e2 = {1'b0, e1} - {1'b0, e2};
  assign e2_e1 = {1'b0, e2} - {1'b0, e1};

  logic swap;
  logic [7:0] d;
  logic [7:0] e_L;
  assign swap = e1_e2[8];
  assign d    = swap ? e2_e1[7:0] : e1_e2[7:0];
  assign e_L  = swap ? e2 : e1;

  // =========================================================
  // 1段目：Far Path
  // =========================================================
  logic        s_L;
  logic [23:0] m_L, m_S;
  assign s_L = swap ? s2_eff : s1;
  assign m_L = swap ? m2 : m1;
  assign m_S = swap ? m1 : m2;

  // 26bit右バレルシフト + Sticky計算
  logic        sticky;
  logic [25:0] shifted;

  always_comb begin
    sticky  = 1'b0;
    shifted = {m_S, 2'b00};

    if (d >= 8'd26) begin sticky = |m_S; shifted = 26'd0; end
    else begin
      if (d[4] == 1'b1) begin sticky = sticky | (|shifted[15:0]); shifted = shifted >> 16; end
      if (d[3] == 1'b1) begin sticky = sticky | (|shifted[7:0]);  shifted = shifted >>  8; end
      if (d[2] == 1'b1) begin sticky = sticky | (|shifted[3:0]);  shifted = shifted >>  4; end
      if (d[1] == 1'b1) begin sticky = sticky | (|shifted[1:0]);  shifted = shifted >>  2; end
      if (d[0] == 1'b1) begin sticky = sticky | shifted[0];       shifted = shifted >>  1; end
    end
  end

  // 27bit加減算
  logic [26:0] M_L, M_S;
  logic [27:0] sum_far;
  assign M_L = {m_L, 3'b000};
  assign M_S = {shifted, sticky};
  assign sum_far = eff_sub ? {1'b0, M_L} - {1'b0, M_S} : {1'b0, M_L} + {1'b0, M_S};

  // =========================================================
  // 1段目：Close Path
  // =========================================================
  logic d_is_1;
  assign d_is_1 = e1[0] ^ e2[0];

  // 並列25bit減算
  logic [25:0] diff_12, diff_21;
  assign diff_12 = {1'b0, m1, 1'b0} - (d_is_1 ? {2'b00, m2} : {1'b0, m2, 1'b0});
  assign diff_21 = {1'b0, m2, 1'b0} - (d_is_1 ? {2'b00, m1} : {1'b0, m1, 1'b0});

  logic        use_21;
  logic        s_close;
  logic [25:0] diff_close;
  assign use_21     = e1_e2[8] || (!e2_e1[8] && diff_12[25]);
  assign s_close    = use_21 ? s2_eff : s1;
  assign diff_close = use_21 ? diff_21 : diff_12;

  // 24bitLZC + 左シフト
  logic [4:0]  lzc;
  logic [23:0] norm;

  always_comb begin
    lzc  = 5'd0;
    norm = diff_close[23:0];

    if (norm[23:8]  == 16'd0) begin lzc[4] = 1'b1; norm = norm << 16; end
    if (norm[23:16] ==  8'd0) begin lzc[3] = 1'b1; norm = norm <<  8; end
    if (norm[23:20] ==  4'd0) begin lzc[2] = 1'b1; norm = norm <<  4; end
    if (norm[23:22] ==  2'd0) begin lzc[1] = 1'b1; norm = norm <<  2; end
    if (norm[23]    ==  1'b0) begin lzc[0] = 1'b1; norm = norm <<  1; end
  end

  // =========================================================
  // ラッチ
  // =========================================================
  // 共通
  logic        use_close_ff;
  logic [7:0]  e_L_ff;

  // Far Path
  logic        s_L_ff;
  logic [27:0] sum_far_ff;
  logic        far_is_zero_ff;

  // Close Path
  logic        s_close_ff;
  logic [4:0]  lzc_ff;
  logic [22:0] frac_close_ff;
  logic        close_is_zero_ff;

  always_ff @(posedge clk) begin
    // 共通
    use_close_ff     <= eff_sub && (d <= 8'd1) && !diff_close[24];
    e_L_ff           <= e_L;

    // Far Path
    s_L_ff           <= s_L;
    sum_far_ff       <= sum_far;
    far_is_zero_ff   <= (e_L == 8'd0);

    // Close Path
    s_close_ff       <= s_close;
    lzc_ff           <= lzc;
    frac_close_ff    <= norm[22:0];
    close_is_zero_ff <= (diff_close[23:0] == 24'd0);
  end
  

  // =========================================================
  // 2段目：Far Path
  // =========================================================
  // 3択正規化
  logic [7:0]  exp_norm;
  logic [22:0] frac_norm;
  logic        G;
  logic        S;

  always_comb begin
    if (sum_far_ff[27] == 1'b1) begin
      exp_norm  =  e_L_ff + 8'd1;
      frac_norm =  sum_far_ff[26:4];
      G         =  sum_far_ff[3];
      S         = |sum_far_ff[2:0];
    end else if (sum_far_ff[26] == 1'b1) begin
      exp_norm  =  e_L_ff;
      frac_norm =  sum_far_ff[25:3];
      G         =  sum_far_ff[2];
      S         = |sum_far_ff[1:0];
    end else begin
      exp_norm  =  e_L_ff - 8'd1;
      frac_norm =  sum_far_ff[24:2];
      G         =  sum_far_ff[1];
      S         =  sum_far_ff[0];
    end
  end

  // 最近接偶数丸め（RNE）+ 丸め加算
  logic round_up;
  assign round_up = G & (S | frac_norm[0]);

  logic [7:0]  exp_far;
  logic [22:0] frac_far;
  assign {exp_far, frac_far} = {exp_norm, frac_norm} + {30'd0, round_up};

  // 結果生成
  logic [31:0] result_far;
  assign result_far = far_is_zero_ff ? 32'h0000_0000 : {s_L_ff, exp_far, frac_far};

  // =========================================================
  // 2段目：Close Path
  // =========================================================
  // 符号付き指数減算
  logic signed [9:0]  exp_close;
  assign exp_close  = $signed({2'b00, e_L_ff}) - 10'sd1 - $signed({5'd0, lzc_ff});

  // アンダーフロー判定
  logic underflow;
  assign underflow = (exp_close <= 10'sd0);

  // 結果生成
  logic [31:0] result_close;
  assign result_close = (close_is_zero_ff || underflow) ? 32'h0000_0000 : {s_close_ff, exp_close[7:0], frac_close_ff};

  // =========================================================
  // 最終MUX
  // =========================================================
  assign result = use_close_ff ? result_close : result_far;

endmodule

`default_nettype wire
