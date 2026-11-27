`timescale 1ns / 1ps
`default_nettype none

// fadd (aux=1 で fsub) 固定レイテンシ・フルパイプライン用テストベンチ
// 毎サイクル連続投入 (II=1) し、LATENCY サイクル後の出力を HEX ダンプする。
module tb_fadd #(
  parameter int DEFAULT_LATENCY = 1
);
  logic        clk;
  logic [31:0] rs1_val;
  logic [31:0] rs2_val;
  logic        aux;
  logic [31:0] result;

  fadd dut (
    .clk     (clk),
    .rs1_val (rs1_val),
    .rs2_val (rs2_val),
    .aux     (aux),
    .result  (result)
  );

  initial clk = 1'b0;
  always #5 clk = ~clk;

  string vec_path;
  string out_path;
  string vcd_path;
  int    latency;
  int    vec_fd;
  int    out_fd;
  int    rc;
  int    cyc;
  int    in_cnt;
  int    out_cnt;
  bit    eof;
  logic [31:0] aux_tmp;
  logic [31:0] a_tmp;
  logic [31:0] b_tmp;

  initial begin
    latency = DEFAULT_LATENCY;
    void'($value$plusargs("LATENCY=%d", latency));
    if (latency < 1) begin
      $display("[TB_ERROR] LATENCY must be >= 1 (got %0d)", latency);
      $finish(1);
    end
    if (!$value$plusargs("VEC=%s", vec_path)) vec_path = "test/vectors/data/fadd.hex";
    if (!$value$plusargs("OUT=%s", out_path)) out_path = "test/work/fadd_out.hex";
    if ($value$plusargs("VCD=%s", vcd_path)) begin
      $dumpfile(vcd_path);
      $dumpvars(0, tb_fadd);
    end

    vec_fd = $fopen(vec_path, "r");
    if (vec_fd == 0) begin
      $display("[TB_ERROR] Failed to open vector file: %s", vec_path);
      $finish(1);
    end
    out_fd = $fopen(out_path, "w");
    if (out_fd == 0) begin
      $display("[TB_ERROR] Failed to open output file: %s", out_path);
      $finish(1);
    end

    rs1_val = 32'h0;
    rs2_val = 32'h0;
    aux     = 1'b0;
    cyc     = 0;
    in_cnt  = 0;
    out_cnt = 0;
    eof     = 1'b0;

    // 初期安定用 1 サイクル
    @(posedge clk);

    while (!eof || (out_cnt < in_cnt)) begin
      @(negedge clk);
      if (!eof) begin
        rc = $fscanf(vec_fd, "%h %h %h\n", aux_tmp, a_tmp, b_tmp);
        if (rc == 3) begin
          aux     = aux_tmp[0];
          rs1_val = a_tmp;
          rs2_val = b_tmp;
          in_cnt  = in_cnt + 1;
        end else begin
          eof     = 1'b1;
          aux     = 1'b0;
          rs1_val = 32'h0;
          rs2_val = 32'h0;
        end
      end else begin
        aux     = 1'b0;
        rs1_val = 32'h0;
        rs2_val = 32'h0;
      end

      // posedge 直前 (半サイクル経過後) で組み合わせ出力をサンプル
      #4;
      if ((cyc >= latency) && (out_cnt < in_cnt)) begin
        $fwrite(out_fd, "%08x\n", result);
        out_cnt = out_cnt + 1;
      end

      @(posedge clk);
      cyc = cyc + 1;
    end

    $fclose(vec_fd);
    $fclose(out_fd);
    $display("[TB_FADD] Done. vectors=%0d latency=%0d", out_cnt, latency);
    $finish;
  end
endmodule

`default_nettype wire
