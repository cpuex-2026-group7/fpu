`timescale 1ns / 1ps
`default_nettype none

// fdiv 非パイプライン・マルチサイクル用テストベンチ
// 1. テストベクタ精度試験 (issued -> 1サイクル done パルス確認 -> result 出力)
// 2. プロトコル試験 (IDLE時 abort_ff、演算途中各サイクルでの abort_ff + 翌サイクル即 issued、連続発行)
module tb_fdiv;
  logic        clk;
  logic        rst;
  logic        issued;
  logic        abort_ff;
  logic [31:0] rs1_val;
  logic [31:0] rs2_val;
  logic        done;
  logic [31:0] result;

  fdiv dut (
    .clk      (clk),
    .rst      (rst),
    .issued   (issued),
    .abort_ff (abort_ff),
    .rs1_val  (rs1_val),
    .rs2_val  (rs2_val),
    .done     (done),
    .result   (result)
  );

  initial clk = 1'b0;
  always #5 clk = ~clk;

  string vec_path;
  string out_path;
  string vcd_path;
  int    timeout_cycles;
  int    skip_proto;
  int    vec_fd;
  int    out_fd;
  int    rc;
  int    vec_cnt;
  int    proto_errors;
  int    lat;
  int    min_lat;
  int    max_lat;
  logic [31:0] a_tmp;
  logic [31:0] b_tmp;
  logic [31:0] res_tmp;
  bit    ok_tmp;

  // 1件の通常演算を実行し、done パルスと結果を取得する
  task automatic run_single(
    input  logic [31:0] a,
    input  logic [31:0] b,
    input  bit          check_one_cycle_pulse,
    output logic [31:0] out_res,
    output int          out_lat,
    output bit          out_ok
  );
    int c;
    out_res = 32'hxxxxxxxx;
    out_lat = 0;
    out_ok  = 1'b0;

    @(negedge clk);
    issued   = 1'b1;
    abort_ff = 1'b0;
    rs1_val  = a;
    rs2_val  = b;
    @(posedge clk);

    for (c = 1; c <= timeout_cycles; c = c + 1) begin
      @(negedge clk);
      issued   = 1'b0;
      abort_ff = 1'b0;
      // 入力は翌サイクルで変化してもラッチ値が保持されるべき
      rs1_val  = 32'hDEAD_BEEF;
      rs2_val  = 32'hCAFE_BABE;
      #4;
      if (done === 1'bx || done === 1'bz) begin
        $display("[PROTO_FAIL] done is X/Z at cycle %0d after issued (a=%08x b=%08x)", c, a, b);
        proto_errors = proto_errors + 1;
        @(posedge clk);
        return;
      end
      if (done === 1'b1) begin
        out_res = result;
        out_lat = c;
        out_ok  = 1'b1;
        @(posedge clk);
        if (check_one_cycle_pulse) begin
          @(negedge clk);
          issued   = 1'b0;
          abort_ff = 1'b0;
          #4;
          if (done !== 1'b0) begin
            $display("[PROTO_FAIL] done stayed high >1 cycle after completion (done=%b)", done);
            proto_errors = proto_errors + 1;
          end
          @(posedge clk);
        end
        return;
      end
      @(posedge clk);
    end

    $display("[PROTO_FAIL] Timeout (%0d cycles) waiting for done (a=%08x b=%08x)", timeout_cycles, a, b);
    proto_errors = proto_errors + 1;
  endtask

  // abort_ff および連続発行プロトコル検証
  task automatic run_protocol_tests(input int base_lat);
    int k;
    int max_k;
    int c;
    int wait_lat;
    logic [31:0] r;
    bit ok;
    // 10.0 / 2.0 = 5.0 (0x40a00000), 9.0 / 3.0 = 3.0 (0x40400000)
    localparam logic [31:0] A1 = 32'h41200000; // 10.0
    localparam logic [31:0] B1 = 32'h40000000; // 2.0
    localparam logic [31:0] A2 = 32'h41100000; // 9.0
    localparam logic [31:0] B2 = 32'h40400000; // 3.0

    $display("[TB_FDIV] Running protocol tests (base_lat=%0d)...", base_lat);

    // 1. IDLE 中の abort_ff 無視テスト
    @(negedge clk);
    issued   = 1'b0;
    abort_ff = 1'b1;
    #4;
    if (done === 1'b1) begin
      $display("[PROTO_FAIL] Unexpected done during IDLE abort_ff");
      proto_errors = proto_errors + 1;
    end
    @(posedge clk);
    @(negedge clk);
    abort_ff = 1'b0;
    #4;
    if (done !== 1'b0) begin
      $display("[PROTO_FAIL] Unexpected done after IDLE abort_ff");
      proto_errors = proto_errors + 1;
    end
    @(posedge clk);

    // 2. 各サイクル位置 k=1..min(base_lat, 16) での abort_ff テスト
    max_k = (base_lat < 16) ? base_lat : 16;
    for (k = 1; k <= max_k; k = k + 1) begin
      // 2a. abort 後に新規 issued を出さない -> 以降 done が出ないこと
      @(negedge clk);
      issued   = 1'b1;
      abort_ff = 1'b0;
      rs1_val  = A1;
      rs2_val  = B1;
      @(posedge clk);

      for (c = 1; c < k; c = c + 1) begin
        @(negedge clk);
        issued   = 1'b0;
        abort_ff = 1'b0;
        @(posedge clk);
      end

      // k サイクル目に abort_ff を立てる (同サイクルの done はどちらでも可)
      @(negedge clk);
      issued   = 1'b0;
      abort_ff = 1'b1;
      @(posedge clk);

      // 次のサイクル以降、abort された演算の done が出ないこと
      for (c = 1; c <= base_lat + 4; c = c + 1) begin
        @(negedge clk);
        issued   = 1'b0;
        abort_ff = 1'b0;
        #4;
        if (done !== 1'b0) begin
          $display("[PROTO_FAIL] Spurious done=%b at +%0d cycles after abort at k=%0d", done, c, k);
          proto_errors = proto_errors + 1;
        end
        @(posedge clk);
      end

      // 2b. abort の直後 (次サイクル) に即座に新しい issued を受けて正しく完了すること
      @(negedge clk);
      issued   = 1'b1;
      abort_ff = 1'b0;
      rs1_val  = A1;
      rs2_val  = B1;
      @(posedge clk);

      for (c = 1; c < k; c = c + 1) begin
        @(negedge clk);
        issued   = 1'b0;
        abort_ff = 1'b0;
        @(posedge clk);
      end

      // k サイクル目に abort_ff=1
      @(negedge clk);
      issued   = 1'b0;
      abort_ff = 1'b1;
      @(posedge clk);

      // 直後のサイクルに A2 / B2 (= 3.0 = 0x40400000) を新規発行
      run_single(A2, B2, 1'b1, r, wait_lat, ok);
      if (!ok) begin
        $display("[PROTO_FAIL] Re-issue right after abort (k=%0d) did not complete", k);
        proto_errors = proto_errors + 1;
      end else if (^r === 1'bx || r[31:20] !== 12'h404) begin
        // 3.0 = 0x40400000 (許容誤差 ±8ulp でも上位12bitは必ず 0x404 付近; 0x403ffff8..0x40400008)
        if (!(r >= 32'h403ffff0 && r <= 32'h40400010)) begin
          $display("[PROTO_FAIL] Re-issue right after abort (k=%0d) returned wrong value: got=%08x (expected ~40400000, not 40a00000)", k, r);
          proto_errors = proto_errors + 1;
        end
      end
    end

    // 3. done 直後の次サイクル連続発行 (back-to-back) テスト
    run_single(A1, B1, 1'b0, r, wait_lat, ok);
    if (!ok) begin
      $display("[PROTO_FAIL] Back-to-back 1st op failed");
      proto_errors = proto_errors + 1;
    end
    run_single(A2, B2, 1'b1, r, wait_lat, ok);
    if (!ok || !(r >= 32'h403ffff0 && r <= 32'h40400010)) begin
      $display("[PROTO_FAIL] Back-to-back 2nd op failed: ok=%b got=%08x", ok, r);
      proto_errors = proto_errors + 1;
    end
  endtask

  initial begin
    timeout_cycles = 1000;
    skip_proto     = 0;
    void'($value$plusargs("TIMEOUT=%d", timeout_cycles));
    void'($value$plusargs("SKIP_PROTO=%d", skip_proto));
    if (!$value$plusargs("VEC=%s", vec_path)) vec_path = "test/vectors/data/fdiv.hex";
    if (!$value$plusargs("OUT=%s", out_path)) out_path = "test/work/fdiv_out.hex";
    if ($value$plusargs("VCD=%s", vcd_path)) begin
      $dumpfile(vcd_path);
      $dumpvars(0, tb_fdiv);
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

    // リセット (アクティブ High: src/fdiv/fdiv.sv の `issued_ff <= !rst & issued` 準拠)
    rst          = 1'b1;
    issued       = 1'b0;
    abort_ff     = 1'b0;
    rs1_val      = 32'h0;
    rs2_val      = 32'h0;
    vec_cnt      = 0;
    proto_errors = 0;
    min_lat      = 999999;
    max_lat      = 0;

    repeat (3) @(posedge clk);
    @(negedge clk);
    rst = 1'b0;
    @(posedge clk);

    // 精度テストベクタの順次実行 (偶数件は1サイクルパルス検査、奇数件は連続発行)
    while ($fscanf(vec_fd, "%h %h\n", a_tmp, b_tmp) == 2) begin
      run_single(a_tmp, b_tmp, (vec_cnt < 16) ? 1'b1 : 1'b0, res_tmp, lat, ok_tmp);
      $fwrite(out_fd, "%08x\n", res_tmp);
      vec_cnt = vec_cnt + 1;
      if (ok_tmp) begin
        if (lat < min_lat) min_lat = lat;
        if (lat > max_lat) max_lat = lat;
      end else begin
        // タイムアウト等で停止した場合は残りベクタも中断
        break;
      end
    end

    if (!skip_proto && proto_errors == 0 && max_lat > 0) begin
      run_protocol_tests(max_lat);
    end

    $fclose(vec_fd);
    $fclose(out_fd);
    $display("[TB_FDIV] Done. vectors=%0d lat_min=%0d lat_max=%0d proto_errors=%0d",
             vec_cnt, min_lat, max_lat, proto_errors);
    if (proto_errors != 0) begin
      $finish(1);
    end
    $finish;
  end
endmodule

`default_nettype wire
