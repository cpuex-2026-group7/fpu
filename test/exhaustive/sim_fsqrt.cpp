#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "Vfsqrt.h"
#include "verilated.h"
#include "fp32_spec.hpp"

struct ThreadResult {
  uint64_t checked = 0;
  uint64_t failed = 0;
  uint64_t timeouts = 0;
  double max_err_ratio = 0.0;
  uint32_t worst_input_u = 0;
  uint32_t worst_got_u = 0;
  std::map<int64_t, uint64_t> ulp_hist;
  std::vector<fpu_spec::FailureSample> samples;
};

static constexpr size_t MAX_SAMPLES_PER_THREAD = 10;

// 定義域: 0 <= A < 2^127 (A案)
//   - 正の 0..2^127 未満: 0x00000000 .. 0x7EFFFFFF (254 * 2^23 通り)
//   - 負の exp == 0 (FTZ で実数値 0.0): 0x80000000 .. 0x807FFFFF (1 * 2^23 通り)
//   計 255 * 2^23 = 2,139,095,040 通り
static inline uint32_t index_to_fsqrt_u32(uint64_t k) {
  constexpr uint64_t POS_CNT = 254ULL << 23;
  if (k < POS_CNT) {
    return static_cast<uint32_t>(k);
  }
  return 0x80000000u | static_cast<uint32_t>(k - POS_CNT);
}

static void run_worker(uint64_t begin_k, uint64_t end_k, int timeout, ThreadResult& out) {
  auto ctx = std::make_unique<VerilatedContext>();
  auto dut = std::make_unique<Vfsqrt>(ctx.get());
  dut->clk = 0;
  dut->rst = 1;
  dut->issued = 0;
  dut->abort_ff = 0;
  dut->rs1_val = 0;
  dut->eval();

  auto tick = [&]() {
    dut->clk = 1;
    dut->eval();
    dut->clk = 0;
    dut->eval();
  };

  // リセット 2 サイクル
  tick();
  tick();
  dut->rst = 0;
  tick();

  uint64_t local_checked = 0;
  uint64_t local_failed = 0;
  uint64_t local_timeouts = 0;
  uint64_t zero_ulp_cnt = 0;
  double max_ratio = 0.0;
  uint32_t worst_in = 0;
  uint32_t worst_got = 0;

  for (uint64_t cur = begin_k; cur < end_k; ++cur) {
    uint32_t a_u = index_to_fsqrt_u32(cur);

    // 1 サイクル発行
    dut->issued = 1;
    dut->rs1_val = a_u;
    tick();
    dut->issued = 0;

    int wait = 0;
    while (!dut->done && wait < timeout) {
      tick();
      ++wait;
    }

    ++local_checked;
    if (!dut->done) {
      ++local_timeouts;
      ++local_failed;
      if (out.samples.size() < MAX_SAMPLES_PER_THREAD) {
        out.samples.push_back({a_u, 0xDEADBEEFu, 0u, 0});
      }
      // タイムアウト後はリセットして復帰
      dut->rst = 1;
      tick();
      dut->rst = 0;
      continue;
    }

    uint32_t res_u = dut->result;
    auto chk = fpu_spec::check_fsqrt(a_u, res_u);

    if (chk.err_ratio > max_ratio) {
      max_ratio = chk.err_ratio;
      worst_in = a_u;
      worst_got = res_u;
    }
    if (chk.passed && chk.ulp_err == 0) {
      ++zero_ulp_cnt;
    } else {
      out.ulp_hist[chk.ulp_err]++;
      if (!chk.passed) {
        ++local_failed;
        if (out.samples.size() < MAX_SAMPLES_PER_THREAD) {
          out.samples.push_back({a_u, res_u, chk.ref_rne, chk.ulp_err});
        }
      }
    }
  }

  out.checked = local_checked;
  out.failed = local_failed;
  out.timeouts = local_timeouts;
  out.max_err_ratio = max_ratio;
  out.worst_input_u = worst_in;
  out.worst_got_u = worst_got;
  if (zero_ulp_cnt > 0) {
    out.ulp_hist[0] += zero_ulp_cnt;
  }
}

int main(int argc, char** argv) {
  int timeout = 1000;
  unsigned int num_threads = std::thread::hardware_concurrency();
  if (num_threads == 0) num_threads = 4;

  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i];
    if (arg == "--timeout" && i + 1 < argc) {
      timeout = std::atoi(argv[++i]);
    } else if (arg == "--threads" && i + 1 < argc) {
      num_threads = static_cast<unsigned int>(std::atoi(argv[++i]));
    }
  }
  if (num_threads < 1) num_threads = 1;

  constexpr uint64_t TOTAL = 255ULL << 23;  // 2,139,095,040 通り (定義域 0 <= A < 2^127 全数)
  auto t0 = std::chrono::steady_clock::now();

  std::vector<ThreadResult> results(num_threads);
  std::vector<std::thread> threads;
  threads.reserve(num_threads);

  uint64_t chunk = (TOTAL + num_threads - 1) / num_threads;
  for (unsigned int t = 0; t < num_threads; ++t) {
    uint64_t b = t * chunk;
    uint64_t e = std::min(TOTAL, b + chunk);
    if (b >= e) continue;
    threads.emplace_back(run_worker, b, e, timeout, std::ref(results[t]));
  }
  for (auto& th : threads) th.join();

  auto t1 = std::chrono::steady_clock::now();
  double sec = std::chrono::duration<double>(t1 - t0).count();

  uint64_t total_checked = 0;
  uint64_t total_failed = 0;
  uint64_t total_timeouts = 0;
  double max_ratio = 0.0;
  uint32_t worst_in = 0;
  uint32_t worst_got = 0;
  std::map<int64_t, uint64_t> hist;
  std::vector<fpu_spec::FailureSample> failures;

  for (const auto& r : results) {
    total_checked += r.checked;
    total_failed += r.failed;
    total_timeouts += r.timeouts;
    if (r.max_err_ratio > max_ratio) {
      max_ratio = r.max_err_ratio;
      worst_in = r.worst_input_u;
      worst_got = r.worst_got_u;
    }
    for (const auto& kv : r.ulp_hist) hist[kv.first] += kv.second;
    for (const auto& s : r.samples) {
      if (failures.size() < 20) failures.push_back(s);
    }
  }

  std::printf("=== FSQRT 全数検証レポート (Verilator / float_spec.pdf 準拠 / A案) ===\n");
  std::printf("  設定            : timeout=%d, threads=%u, elapsed=%.2f s (%.2f Mvec/s)\n",
              timeout, num_threads, sec, (total_checked / 1e6) / std::max(sec, 1e-9));
  std::printf("  検査数          : %llu / %llu (定義域 0 <= A < 2^127 全数)\n",
              static_cast<unsigned long long>(total_checked),
              static_cast<unsigned long long>(TOTAL));
  std::printf("  PASS / FAIL     : %llu / %llu (うちタイムアウト: %llu)\n",
              static_cast<unsigned long long>(total_checked - total_failed),
              static_cast<unsigned long long>(total_failed),
              static_cast<unsigned long long>(total_timeouts));
  std::printf("  最大誤差比      : %.6e (abs_err / spec_bound; < 1.0 が合格, worst_in=0x%08x -> got=0x%08x)\n",
              max_ratio, worst_in, worst_got);
  std::printf("  ULP 差分布 (vs RNE 参照値):\n");
  for (const auto& kv : hist) {
    double pct = 100.0 * static_cast<double>(kv.second) / static_cast<double>(total_checked);
    std::printf("    %+4lld ulp : %12llu (%6.2f%%)\n",
                static_cast<long long>(kv.first),
                static_cast<unsigned long long>(kv.second),
                pct);
  }
  if (!failures.empty()) {
    std::printf("  失敗詳細 (先頭 %zu 件):\n", failures.size());
    for (const auto& f : failures) {
      std::printf("    fsqrt(0x%08x) -> got=0x%08x (ref_rne=0x%08x, ulp=%+lld)\n",
                  f.input_u, f.got_u, f.ref_u, static_cast<long long>(f.ulp_err));
    }
  }
  bool pass = (total_failed == 0 && total_checked == TOTAL);
  std::printf("  総合判定        : %s\n", pass ? "PASS" : "FAIL");
  return pass ? 0 : 1;
}
