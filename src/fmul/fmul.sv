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

endmodule

`default_nettype wire
