`timescale 1ns / 1ps
`default_nettype none

// issuedが立ったサイクルのrs1_val/rs2_valで演算を始める
// 終わったら1サイクルだけdoneを立て、そのサイクルのresultを結果とする
// abort_ffが来たら、次のサイクルから新しいissuedを受けられるようにし、abortした演算のdoneは出さない
// abort_ffのサイクルのdoneはどちらでもよい。idleのときのabort_ffは無視してよい
module fsqrt (
  input wire logic        clk,
  input wire logic        rst,
  input wire logic        issued,
  input wire logic        abort_ff,
  input wire logic [31:0] rs1_val,

  output logic        done,
  output logic [31:0] result
);

  logic        issued_ff;
  logic [31:0] rs1_ff;

  always_ff @(posedge clk) begin
    issued_ff <= !rst & issued;
    rs1_ff    <= rs1_val;
  end

endmodule

`default_nettype wire
