#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "Vitof.h"
#include "verilated.h"
#include "fp32_spec.hpp"

struct ThreadResult {
  uint64_t checked = 0;
  uint64_t failed = 0;
  std::map<int64_t, uint64_t> ulp_hist;
  std::vector<fpu_spec::FailureSample> samples;
};

static constexpr size_t MAX_SAMPLES_PER_THREAD = 10;

// 正確なタイミング契約 (design_memo.md / tb_itof.sv 準拠):
//   - サイクル t の posedge clk で rs1_val (X_t) が入口 FF に取り込まれる
//   - latency == 1: サイクル t の posedge 後 (組み合わせ出力) で result == f(X_t)
//   - latency == L: サイクル t + (L - 1) の posedge 後で result == f(X_t)
static void run_worker_exact(uint64_t begin_u, uint64_t end_u, int latency, ThreadResult& out) {
  auto ctx = std::make_unique<VerilatedContext>();
  auto dut = std::make_unique<Vitof>(ctx.get());
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

  // 最初の (latency - 1) サイクル分を投入
  uint64_t cur = begin_u;
  int primed = 0;
  for (int i = 0; i < depth && cur < end_u; ++i) {
    ring[static_cast<size_t>(idx)] = static_cast<uint32_t>(cur);
    idx = (idx + 1) % depth;
    tick(static_cast<uint32_t>(cur));
    ++cur;
    ++primed;
  }

  // メインループ: cur を投入すると、その posedge 後に (cur - (latency - 1)) の結果が確定する
  uint64_t local_checked = 0;
  uint64_t local_failed = 0;
  uint64_t zero_ulp_cnt = 0;

  while (cur < end_u) {
    uint32_t in_u = static_cast<uint32_t>(cur);
    uint32_t eval_u = (depth == 0) ? in_u : ring[static_cast<size_t>(idx)];
    if (depth > 0) {
      ring[static_cast<size_t>(idx)] = in_u;
      idx = (idx + 1) % depth;
    }
    uint32_t res_u = tick(in_u);

    auto chk = fpu_spec::check_itof(eval_u, res_u);
    ++local_checked;
    if (chk.passed && chk.ulp_err == 0) {
      ++zero_ulp_cnt;
    } else {
      out.ulp_hist[chk.ulp_err]++;
      if (!chk.passed) {
        ++local_failed;
        if (out.samples.size() < MAX_SAMPLES_PER_THREAD) {
          out.samples.push_back({eval_u, res_u, chk.ref_rne, chk.ulp_err});
        }
      }
    }
    ++cur;
  }

  // 残りの (primed) 件をフラッシュ
  for (int i = 0; i < primed; ++i) {
    uint32_t eval_u = ring[static_cast<size_t>(idx)];
    idx = (idx + 1) % depth;
    uint32_t res_u = tick(0u);

    auto chk = fpu_spec::check_itof(eval_u, res_u);
    ++local_checked;
    if (chk.passed && chk.ulp_err == 0) {
      ++zero_ulp_cnt;
    } else {
      out.ulp_hist[chk.ulp_err]++;
      if (!chk.passed) {
        ++local_failed;
        if (out.samples.size() < MAX_SAMPLES_PER_THREAD) {
          out.samples.push_back({eval_u, res_u, chk.ref_rne, chk.ulp_err});
        }
      }
    }
  }

  out.checked = local_checked;
  out.failed = local_failed;
  if (zero_ulp_cnt > 0) {
    out.ulp_hist[0] += zero_ulp_cnt;
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

  constexpr uint64_t TOTAL = 1ULL << 32;  // 全 4,294,967,296 通り
  auto t0 = std::chrono::steady_clock::now();

  std::vector<ThreadResult> results(num_threads);
  std::vector<std::thread> threads;
  threads.reserve(num_threads);

  uint64_t chunk = (TOTAL + num_threads - 1) / num_threads;
  for (unsigned int t = 0; t < num_threads; ++t) {
    uint64_t b = t * chunk;
    uint64_t e = std::min(TOTAL, b + chunk);
    if (b >= e) continue;
    threads.emplace_back(run_worker_exact, b, e, latency, std::ref(results[t]));
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
    for (const auto& kv : r.ulp_hist) hist[kv.first] += kv.second;
    for (const auto& s : r.samples) {
      if (failures.size() < 20) failures.push_back(s);
    }
  }

  std::printf("=== ITOF 全数検証レポート (Verilator / float_spec.pdf 準拠 / A案) ===\n");
  std::printf("  設定            : latency=%d, threads=%u, elapsed=%.2f s (%.2f Mvec/s)\n",
              latency, num_threads, sec, (total_checked / 1e6) / std::max(sec, 1e-9));
  std::printf("  検査数          : %llu / %llu (全 2^32 通り)\n",
              static_cast<unsigned long long>(total_checked),
              static_cast<unsigned long long>(TOTAL));
  std::printf("  PASS / FAIL     : %llu / %llu\n",
              static_cast<unsigned long long>(total_checked - total_failed),
              static_cast<unsigned long long>(total_failed));
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
      std::printf("    itof(0x%08x / %d) -> got=0x%08x (ref_rne=0x%08x, ulp=%+lld)\n",
                  f.input_u, static_cast<int32_t>(f.input_u), f.got_u, f.ref_u,
                  static_cast<long long>(f.ulp_err));
    }
  }
  bool pass = (total_failed == 0 && total_checked == TOTAL);
  std::printf("  総合判定        : %s\n", pass ? "PASS" : "FAIL");
  return pass ? 0 : 1;
}
