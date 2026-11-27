#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <vector>

namespace fpu_spec {

// A案 (Fpu.pdf §2.2) の単調増加インデックス (ULP 差計算用)
//   exp == 0 -> 0
//   正の正規化数 (exp=1..255) -> +((exp-1)*2^23 + frac + 1)
//   負の正規化数 (exp=1..255) -> -((exp-1)*2^23 + frac + 1)
inline int64_t fp32_to_ordered_index(uint32_t u) {
  uint32_t sign = u >> 31;
  uint32_t exp = (u >> 23) & 0xFFu;
  uint32_t frac = u & 0x7FFFFFu;
  if (exp == 0) return 0;
  int64_t mag = (static_cast<int64_t>(exp - 1u) << 23) + static_cast<int64_t>(frac) + 1;
  return sign ? -mag : mag;
}

inline int64_t ulp_diff(uint32_t actual_u, uint32_t expected_u) {
  return fp32_to_ordered_index(actual_u) - fp32_to_ordered_index(expected_u);
}

struct FailureSample {
  uint32_t input_u;
  uint32_t got_u;
  uint32_t ref_u;
  int64_t ulp_err;
};

struct ItofCheck {
  bool passed;
  uint32_t ref_rne;
  int64_t ulp_err;
};

// itof (float_of_int) 厳密判定:
//   任意の 32bit 符号付き整数 I に対し、|A - I| < |R - I| となる有効 FP32 が存在しないこと。
//   ちょうど中間の tie は上下どちらでも PASS。
inline ItofCheck check_itof(uint32_t i_u, uint32_t res_u) {
  if (i_u == 0u) {
    bool ok = ((res_u & 0x7F800000u) == 0u);
    return {ok, 0x00000000u, ulp_diff(res_u, 0x00000000u)};
  }

  uint32_t sign = i_u >> 31;
  // INT_MIN (0x80000000) も符号なし 2 の補数演算で安全に 0x80000000 (= 2^31) になる
  uint32_t abs_i = sign ? (0u - i_u) : i_u;
  int e = 31 - __builtin_clz(abs_i);  // 0..31

  if (e <= 23) {
    uint32_t frac = (abs_i << (23 - e)) & 0x7FFFFFu;
    uint32_t exact_u = (sign << 31) | (static_cast<uint32_t>(e + 127) << 23) | frac;
    bool ok = (res_u == exact_u);
    return {ok, exact_u, ok ? 0 : ulp_diff(res_u, exact_u)};
  }

  int shift = e - 23;  // 1..8
  uint32_t step = 1u << shift;
  uint32_t rem = abs_i & (step - 1u);
  uint32_t half = step >> 1;

  uint32_t lo_mant = abs_i >> shift;
  uint32_t lo_u = (sign << 31) | (static_cast<uint32_t>(e + 127) << 23) | (lo_mant & 0x7FFFFFu);
  // lo_mant == 0xFFFFFF のときも lo_u + 1 で exp が自動で +1, frac = 0 へ繰り上がる (e+127 <= 158)
  uint32_t hi_u = lo_u + 1u;

  uint32_t rne_u = (rem > half || (rem == half && (lo_u & 1u))) ? hi_u : lo_u;
  bool ok;
  if (rem < half) {
    ok = (res_u == lo_u);
  } else if (rem > half) {
    ok = (res_u == hi_u);
  } else {
    ok = (res_u == lo_u) || (res_u == hi_u);
  }
  return {ok, rne_u, (res_u == rne_u) ? 0 : ulp_diff(res_u, rne_u)};
}

struct FtoiCheck {
  bool passed;
  uint32_t ref_u;  // 四捨五入 (tie はゼロから遠い方) 参照値
  int64_t diff;    // got_i - ref_i
};

// ftoi (int_of_float) 厳密判定:
//   定義域 -2^31 + 1 <= A <= 2^31 - 1 (すなわち exp <= 157) に対して、
//   |I - A| < |R - A| となる 32bit 符号付き整数 I が存在しないこと。
//   ちょうど .5 の tie は上下どちらでも PASS。
inline FtoiCheck check_ftoi(uint32_t a_u, uint32_t res_u) {
  uint32_t sign = a_u >> 31;
  uint32_t exp = (a_u >> 23) & 0xFFu;
  uint32_t frac = a_u & 0x7FFFFFu;
  int32_t got_i = static_cast<int32_t>(res_u);

  if (exp <= 125u) {
    // |A| < 0.5 (exp == 0 の FTZ 含む) -> 0 が一意に最近接
    return {got_i == 0, 0u, static_cast<int64_t>(got_i)};
  }

  if (exp == 126u) {
    // 0.5 <= |A| < 1.0
    int32_t away = sign ? -1 : 1;
    if (frac == 0u) {
      // ちょうど ±0.5 (tie)
      bool ok = (got_i == 0) || (got_i == away);
      return {ok, static_cast<uint32_t>(away), static_cast<int64_t>(got_i) - away};
    }
    return {got_i == away, static_cast<uint32_t>(away), static_cast<int64_t>(got_i) - away};
  }

  if (exp <= 149u) {
    // 1.0 <= |A| < 2^23 (小数部あり)
    uint32_t mant = (1u << 23) | frac;
    int rshift = 150 - static_cast<int>(exp);  // 1..23
    uint32_t int_part = mant >> rshift;
    uint32_t rem = mant & ((1u << rshift) - 1u);
    uint32_t half = 1u << (rshift - 1);

    uint32_t lo_abs, hi_abs, ref_abs;
    if (rem < half) {
      lo_abs = hi_abs = ref_abs = int_part;
    } else if (rem > half) {
      lo_abs = hi_abs = ref_abs = int_part + 1u;
    } else {
      lo_abs = int_part;
      hi_abs = int_part + 1u;
      ref_abs = hi_abs;  // 一般的な四捨五入
    }

    int32_t c1 = sign ? -static_cast<int32_t>(lo_abs) : static_cast<int32_t>(lo_abs);
    int32_t c2 = sign ? -static_cast<int32_t>(hi_abs) : static_cast<int32_t>(hi_abs);
    int32_t ref_i = sign ? -static_cast<int32_t>(ref_abs) : static_cast<int32_t>(ref_abs);
    bool ok = (got_i == c1) || (got_i == c2);
    return {ok, static_cast<uint32_t>(ref_i), static_cast<int64_t>(got_i) - ref_i};
  }

  // 150 <= exp <= 157: 2^23 <= |A| <= 2^31 - 128 (厳密な整数)
  uint32_t abs_int = ((1u << 23) | frac) << (exp - 150u);
  int32_t exact_i = sign ? -static_cast<int32_t>(abs_int) : static_cast<int32_t>(abs_int);
  return {got_i == exact_i, static_cast<uint32_t>(exact_i), static_cast<int64_t>(got_i) - exact_i};
}

struct FsqrtCheck {
  bool passed;
  uint32_t ref_rne;
  int64_t ulp_err;
  double err_ratio;
};

// fsqrt 厳密判定 (float_spec.pdf / A案):
//   0 <= A < 2^127 のとき |R - sqrt(A)| < max(sqrt(A) * 2^-20, 2^-126)
//   __uint128_t の整数平方不等式で丸め誤差ゼロの厳密判定を行う。
inline FsqrtCheck check_fsqrt(uint32_t a_u, uint32_t res_u) {
  uint32_t exp_a = (a_u >> 23) & 0xFFu;
  uint32_t frac_a = a_u & 0x7FFFFFu;

  if (exp_a == 0u) {
    // A == 0 -> |R| < 2^-126 <=> exp_r == 0 (A案)
    bool ok = ((res_u & 0x7F800000u) == 0u);
    return {ok, 0x00000000u, ulp_diff(res_u, 0x00000000u), ok ? 0.0 : 1e300};
  }

  // 参照 RNE 値: 24bit 単精度入力の平方根を倍精度 (53bit) で計算してから単精度へ RNE 変換
  // (単精度入力の sqrt は 24bit+1bit 境界に 53bit の midway が存在しないため倍精度経由で厳密一致)
  float a_f;
  std::memcpy(&a_f, &a_u, sizeof(a_f));
  double exact_d = std::sqrt(static_cast<double>(a_f));
  float ref_f = static_cast<float>(exact_d);
  uint32_t ref_u;
  std::memcpy(&ref_u, &ref_f, sizeof(ref_u));

  uint32_t sign_r = res_u >> 31;
  uint32_t exp_r = (res_u >> 23) & 0xFFu;
  uint32_t frac_r = res_u & 0x7FFFFFu;

  int64_t u_err = ulp_diff(res_u, ref_u);

  if (sign_r != 0u || exp_r == 0u) {
    return {false, ref_u, u_err, 1e300};
  }

  // A = M_A * 2^E_A, R = M_R * 2^E_R (E = exp - 150)
  // すべての正の正規化数 A >= 2^-126 > 2^-212 なので bound = sqrt(A) * 2^-20
  // 厳密判定: (1 - 2^-20)^2 * A < R^2 < (1 + 2^-20)^2 * A
  int e_a = static_cast<int>(exp_a) - 150;
  int e_r = static_cast<int>(exp_r) - 150;
  int delta = 2 * e_r - e_a;

  // R^2/A = (M_R^2 / M_A) * 2^delta, M_R^2 / M_A in (2^22, 2^25)
  // よって R^2 ≈ A の必要条件は delta in [-26, -21]
  bool ok = false;
  if (delta >= -26 && delta <= -21) {
    constexpr uint64_t LO_1 = (1ULL << 20) - 1ULL;
    constexpr uint64_t HI_1 = (1ULL << 20) + 1ULL;
    constexpr uint64_t LO_SQ = LO_1 * LO_1;  // (2^20 - 1)^2
    constexpr uint64_t HI_SQ = HI_1 * HI_1;  // (2^20 + 1)^2

    uint64_t m_a = (1ULL << 23) | frac_a;
    uint64_t m_r = (1ULL << 23) | frac_r;

    __uint128_t lhs_lo = (static_cast<__uint128_t>(LO_SQ) * static_cast<__uint128_t>(m_a)) << 26;
    __uint128_t lhs_hi = (static_cast<__uint128_t>(HI_SQ) * static_cast<__uint128_t>(m_a)) << 26;
    __uint128_t r_sq = (static_cast<__uint128_t>(m_r) * static_cast<__uint128_t>(m_r)) << (66 + delta);
    ok = (lhs_lo < r_sq) && (r_sq < lhs_hi);
  }

  // 統計用の誤差比 (A案では exp_r == 255 も正規化数なので ldexp で計算)
  double r_d = std::ldexp(static_cast<double>((1u << 23) | frac_r), e_r);
  double bound_d = exact_d * (1.0 / 1048576.0);  // sqrt(A) * 2^-20
  double ratio = std::fabs(r_d - exact_d) / bound_d;

  return {ok, ref_u, u_err, ratio};
}

}  // namespace fpu_spec
