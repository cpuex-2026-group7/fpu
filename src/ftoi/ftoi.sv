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

endmodule

`default_nettype wire
