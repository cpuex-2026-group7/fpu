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

#include "Vftoi.h"
#include "verilated.h"
#include "fp32_spec.hpp"

struct ThreadResult {
  uint64_t checked = 0;
  uint64_t failed = 0;
  std::map<int64_t, uint64_t> diff_hist;
  std::vector<fpu_spec::FailureSample> samples;
};

static constexpr size_t MAX_SAMPLES_PER_THREAD = 10;

// 定義域: -2^31 + 1 <= A <= 2^31 - 1
// A案ビット列で該当するのは:
//   sign in {0, 1}, exp in [0, 157], frac in [0, 2^23 - 1]
//   計 2 * 158 * 2^23 = 2,650,800,128 通り
// インデックス k in [0, 2*158*2^23) からビット列へ写像する:
//   k < 158*2^23 -> 正側 (0x00000000 .. 0x4EFFFFFF)
//   k >= 158*2^23 -> 負側 (0x80000000 .. 0xCEFFFFFF)
static inline uint32_t index_to_ftoi_u32(uint64_t k) {
  constexpr uint64_t HALF = 158ULL << 23;
  if (k < HALF) {
    return static_cast<uint32_t>(k);
  }
  return 0x80000000u | static_cast<uint32_t>(k - HALF);
}

static void run_worker(uint64_t begin_k, uint64_t end_k, int latency, ThreadResult& out) {
  auto ctx = std::make_unique<VerilatedContext>();
  auto dut = std::make_unique<Vftoi>(ctx.get());
  dut->clk = 0;
  dut->rs1_val = 0;
  dut->eval();

  const int depth = latency - 1;
  std::vector<uint32_t> ring(static_cast<size_t>(std::max(1, depth)), 0u);
  int idx = 0;

  auto tick = [&](uint32_t in_val) -> uint32_t {
    dut->rs1_val = in_val;
    dut->clk = 1;
    dut->eval();
    dut->clk = 0;
    dut->eval();
    return dut->result;
  };

  uint64_t cur = begin_k;
  int primed = 0;
  for (int i = 0; i < depth && cur < end_k; ++i) {
    uint32_t in_u = index_to_ftoi_u32(cur);
    ring[static_cast<size_t>(idx)] = in_u;
    idx = (idx + 1) % depth;
    tick(in_u);
    ++cur;
    ++primed;
  }

  uint64_t local_checked = 0;
  uint64_t local_failed = 0;
  uint64_t zero_diff_cnt = 0;

  while (cur < end_k) {
    uint32_t in_u = index_to_ftoi_u32(cur);
    uint32_t eval_u = (depth == 0) ? in_u : ring[static_cast<size_t>(idx)];
    if (depth > 0) {
      ring[static_cast<size_t>(idx)] = in_u;
      idx = (idx + 1) % depth;
    }
    uint32_t res_u = tick(in_u);

    auto chk = fpu_spec::check_ftoi(eval_u, res_u);
    ++local_checked;
    if (chk.passed && chk.diff == 0) {
      ++zero_diff_cnt;
    } else {
      out.diff_hist[chk.diff]++;
      if (!chk.passed) {
        ++local_failed;
        if (out.samples.size() < MAX_SAMPLES_PER_THREAD) {
          out.samples.push_back({eval_u, res_u, chk.ref_u, chk.diff});
        }
      }
    }
    ++cur;
  }

  for (int i = 0; i < primed; ++i) {
    uint32_t eval_u = ring[static_cast<size_t>(idx)];
    idx = (idx + 1) % depth;
    uint32_t res_u = tick(0u);

    auto chk = fpu_spec::check_ftoi(eval_u, res_u);
    ++local_checked;
    if (chk.passed && chk.diff == 0) {
      ++zero_diff_cnt;
    } else {
      out.diff_hist[chk.diff]++;
      if (!chk.passed) {
        ++local_failed;
        if (out.samples.size() < MAX_SAMPLES_PER_THREAD) {
          out.samples.push_back({eval_u, res_u, chk.ref_u, chk.diff});
        }
      }
    }
  }

  out.checked = local_checked;
  out.failed = local_failed;
  if (zero_diff_cnt > 0) {
    out.diff_hist[0] += zero_diff_cnt;
  }
}

int main(int argc, char** argv) {
  int latency = 2;
  unsigned int num_threads = std::thread::hardware_concurrency();
  if (num_threads == 0) num_threads = 4;

  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i];
    if (arg == "--latency" && i + 1 < argc) {
      latency = std::atoi(argv[++i]);
    } else if (arg == "--threads" && i + 1 < argc) {
      num_threads = static_cast<unsigned int>(std::atoi(argv[++i]));
    }
  }
  if (latency < 1) {
    std::fprintf(stderr, "[ERROR] latency must be >= 1\n");
    return 2;
  }
  if (num_threads < 1) num_threads = 1;

  constexpr uint64_t TOTAL = 2ULL * (158ULL << 23);  // 2,650,800,128 通り (定義域全数)
  auto t0 = std::chrono::steady_clock::now();

  std::vector<ThreadResult> results(num_threads);
  std::vector<std::thread> threads;
  threads.reserve(num_threads);

  uint64_t chunk = (TOTAL + num_threads - 1) / num_threads;
  for (unsigned int t = 0; t < num_threads; ++t) {
    uint64_t b = t * chunk;
    uint64_t e = std::min(TOTAL, b + chunk);
    if (b >= e) continue;
    threads.emplace_back(run_worker, b, e, latency, std::ref(results[t]));
  }
  for (auto& th : threads) th.join();

  auto t1 = std::chrono::steady_clock::now();
  double sec = std::chrono::duration<double>(t1 - t0).count();

  uint64_t total_checked = 0;
  uint64_t total_failed = 0;
  std::map<int64_t, uint64_t> hist;
  std::vector<fpu_spec::FailureSample> failures;

  for (const auto& r : results) {
    total_checked += r.checked;
    total_failed += r.failed;
    for (const auto& kv : r.diff_hist) hist[kv.first] += kv.second;
    for (const auto& s : r.samples) {
      if (failures.size() < 20) failures.push_back(s);
    }
  }

  std::printf("=== FTOI 全数検証レポート (Verilator / float_spec.pdf 準拠 / A案) ===\n");
  std::printf("  設定            : latency=%d, threads=%u, elapsed=%.2f s (%.2f Mvec/s)\n",
              latency, num_threads, sec, (total_checked / 1e6) / std::max(sec, 1e-9));
  std::printf("  検査数          : %llu / %llu (定義域 -2^31+1 <= A <= 2^31-1 全数)\n",
              static_cast<unsigned long long>(total_checked),
              static_cast<unsigned long long>(TOTAL));
  std::printf("  PASS / FAIL     : %llu / %llu\n",
              static_cast<unsigned long long>(total_checked - total_failed),
              static_cast<unsigned long long>(total_failed));
  std::printf("  整数差分布 (vs 四捨五入参照値; tie は ±1 差でも仕様 PASS):\n");
  for (const auto& kv : hist) {
    double pct = 100.0 * static_cast<double>(kv.second) / static_cast<double>(total_checked);
    std::printf("    %+4lld : %12llu (%6.2f%%)\n",
                static_cast<long long>(kv.first),
                static_cast<unsigned long long>(kv.second),
                pct);
  }
  if (!failures.empty()) {
    std::printf("  失敗詳細 (先頭 %zu 件):\n", failures.size());
    for (const auto& f : failures) {
      std::printf("    ftoi(0x%08x) -> got=%d (0x%08x), ref_half_away=%d (0x%08x)\n",
                  f.input_u, static_cast<int32_t>(f.got_u), f.got_u,
                  static_cast<int32_t>(f.ref_u), f.ref_u);
    }
  }
  bool pass = (total_failed == 0 && total_checked == TOTAL);
  std::printf("  総合判定        : %s\n", pass ? "PASS" : "FAIL");
  return pass ? 0 : 1;
}
