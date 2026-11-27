"""doc/fpu/float_spec.pdf 厳密準拠チェッカー (A案: Fpu.pdf §2.2 表現モデル).

全6命令 (fadd/fsub, fmul, fdiv, fsqrt, ftoi, itof) の合否判定を
fractions.Fraction を用いた丸め誤差ゼロの厳密計算で行う。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from fractions import Fraction
import math

from .fp32 import (
    EPS,
    FRAC_BITS,
    I32_MAX,
    I32_MIN,
    LIMIT_127,
    bits_to_fraction,
    format_fp32,
    fraction_to_rne_fp32,
    is_valid_fp32,
    u32_to_i32,
    ulp_diff,
)

TWO_M23 = Fraction(1, 1 << 23)
TWO_M22 = Fraction(1, 1 << 22)
TWO_M20 = Fraction(1, 1 << 20)

# fsqrt の平方不等式用定数: (1 - 2^-20)^2, (1 + 2^-20)^2, 2^-212
SQRT_LO_SQ = (Fraction(1, 1) - TWO_M20) ** 2
SQRT_HI_SQ = (Fraction(1, 1) + TWO_M20) ** 2
SQRT_EPS_THRESHOLD = Fraction(1, 1 << 212)  # sqrt(A) * 2^-20 >= 2^-126 <=> A >= 2^-212


@dataclass
class CheckResult:
    """単一テストケースの判定結果."""

    passed: bool
    in_domain: bool
    reason: str = ""
    abs_err: float = 0.0
    bound: float = 0.0
    rel_err: float = 0.0
    ulp_err: int | None = None
    expected_u32: int | None = None


@dataclass
class SummaryReport:
    """バッチ検証の統計サマリー."""

    op: str
    total: int = 0
    checked: int = 0
    skipped_out_of_domain: int = 0
    passed: int = 0
    failed: int = 0
    xz_errors: int = 0
    max_abs_err: float = 0.0
    max_rel_err: float = 0.0
    max_err_ratio: float = 0.0  # abs_err / bound の最大値 (< 1.0 で合格)
    ulp_hist: Counter = field(default_factory=Counter)
    failures: list[str] = field(default_factory=list)
    max_failure_samples: int = 20

    def record(self, idx: int, inputs_desc: str, res_token: str, res: CheckResult) -> None:
        self.total += 1
        if not res.in_domain:
            self.skipped_out_of_domain += 1
            return
        self.checked += 1
        if res.passed:
            self.passed += 1
        else:
            self.failed += 1
            if "X/Z" in res.reason:
                self.xz_errors += 1
            if len(self.failures) < self.max_failure_samples:
                exp_str = f"0x{res.expected_u32:08x}" if res.expected_u32 is not None else "N/A"
                self.failures.append(
                    f"[#{idx}] {inputs_desc} -> got={res_token} (ref_rne={exp_str}) | {res.reason}"
                )

        if res.abs_err > self.max_abs_err:
            self.max_abs_err = res.abs_err
        if res.rel_err > self.max_rel_err:
            self.max_rel_err = res.rel_err
        if res.bound > 0.0:
            ratio = res.abs_err / res.bound
            if ratio > self.max_err_ratio:
                self.max_err_ratio = ratio
        if res.ulp_err is not None:
            self.ulp_hist[res.ulp_err] += 1

    def format_text(self) -> str:
        lines = [
            f"=== {self.op.upper()} 精度検証レポート (float_spec.pdf 準拠 / A案) ===",
            f"  総ベクタ数      : {self.total}",
            f"  仕様対象 (検査) : {self.checked} (定義域外スキップ: {self.skipped_out_of_domain})",
            f"  PASS / FAIL     : {self.passed} / {self.failed} (うち X/Z 不定値: {self.xz_errors})",
        ]
        if self.checked > 0 and self.op in ("fadd", "fsub", "fmul", "fdiv", "fsqrt"):
            lines.append(f"  最大誤差比      : {self.max_err_ratio:.6e} (abs_err / spec_bound; < 1.0 が合格基準)")
            lines.append(f"  最大相対誤差    : {self.max_rel_err:.6e}")
        if self.ulp_hist:
            lines.append("  ULP 差分布 (vs RNE 参照値):")
            for k in sorted(self.ulp_hist.keys()):
                cnt = self.ulp_hist[k]
                pct = 100.0 * cnt / self.checked if self.checked else 0.0
                lines.append(f"    {k:+4d} ulp : {cnt:8d} ({pct:6.2f}%)")
        if self.failures:
            lines.append(f"  失敗詳細 (先頭 {len(self.failures)} 件):")
            for f in self.failures:
                lines.append(f"    {f}")
        status = "PASS" if self.failed == 0 and self.checked > 0 else "FAIL"
        lines.append(f"  総合判定        : {status}")
        return "\n".join(lines)


def _in_open_127(x: Fraction) -> bool:
    return -LIMIT_127 < x < LIMIT_127


def _safe_float(x: Fraction) -> float:
    try:
        return float(x)
    except OverflowError:
        return math.copysign(math.inf, x.numerator)


def _sqrt_reference_rne(a_val: Fraction) -> int:
    """fsqrt のレポート・ULP 計算用に高精度 Decimal で RNE 参照 fp32 ビット列を求める."""
    if a_val == 0:
        return 0x00000000
    getcontext().prec = 60
    # a_val = mant * 2^shift の形なので Decimal(num) / Decimal(den) は十分高精度
    d = (Decimal(a_val.numerator) / Decimal(a_val.denominator)).sqrt()
    # Decimal を厳密 Fraction に変換してから RNE 化 (60桁精度なら単精度24bitに対し完全に正確)
    return fraction_to_rne_fp32(Fraction(d))


def check_fadd(a_u: int, b_u: int, res_u: int | None, is_sub: bool = False) -> CheckResult:
    """fadd / fsub の float_spec.pdf 厳密判定.

    条件:
      -2^127 < A, B, A ± B < 2^127 のとき
      |R - (A ± B)| < max(|A|*2^-23, |B|*2^-23, |A ± B|*2^-23, 2^-126)
    """
    a = bits_to_fraction(a_u)
    b = bits_to_fraction(b_u)
    exact = (a - b) if is_sub else (a + b)

    if not (_in_open_127(a) and _in_open_127(b) and _in_open_127(exact)):
        return CheckResult(passed=True, in_domain=False, reason="out of spec domain")

    exp_rne = fraction_to_rne_fp32(exact)
    if res_u is None:
        return CheckResult(passed=False, in_domain=True, reason="X/Z or invalid HEX in output", expected_u32=exp_rne)
    if not is_valid_fp32(res_u):
        return CheckResult(passed=False, in_domain=True, reason="invalid fp32 output", expected_u32=exp_rne)

    r = bits_to_fraction(res_u)
    err = abs(r - exact)
    bound = max(abs(a) * TWO_M23, abs(b) * TWO_M23, abs(exact) * TWO_M23, EPS)
    passed = err < bound
    rel = _safe_float(err / abs(exact)) if exact != 0 else (0.0 if err == 0 else math.inf)
    u_err = ulp_diff(res_u, exp_rne)
    reason = "" if passed else (
        f"|err|={_safe_float(err):.6e} >= bound={_safe_float(bound):.6e} "
        f"(got={format_fp32(res_u)}, exact={_safe_float(exact):+.8e}, ulp={u_err:+d})"
    )
    return CheckResult(
        passed=passed,
        in_domain=True,
        reason=reason,
        abs_err=_safe_float(err),
        bound=_safe_float(bound),
        rel_err=rel,
        ulp_err=u_err,
        expected_u32=exp_rne,
    )


def check_fmul(a_u: int, b_u: int, res_u: int | None) -> CheckResult:
    """fmul の float_spec.pdf 厳密判定.

    条件:
      -2^127 < A, B, A*B < 2^127 のとき
      |R - A*B| < max(|A*B|*2^-22, 2^-126)
    """
    a = bits_to_fraction(a_u)
    b = bits_to_fraction(b_u)
    exact = a * b

    if not (_in_open_127(a) and _in_open_127(b) and _in_open_127(exact)):
        return CheckResult(passed=True, in_domain=False, reason="out of spec domain")

    exp_rne = fraction_to_rne_fp32(exact)
    if res_u is None:
        return CheckResult(passed=False, in_domain=True, reason="X/Z or invalid HEX in output", expected_u32=exp_rne)
    if not is_valid_fp32(res_u):
        return CheckResult(passed=False, in_domain=True, reason="invalid fp32 output", expected_u32=exp_rne)

    r = bits_to_fraction(res_u)
    err = abs(r - exact)
    bound = max(abs(exact) * TWO_M22, EPS)
    passed = err < bound
    rel = _safe_float(err / abs(exact)) if exact != 0 else (0.0 if err == 0 else math.inf)
    u_err = ulp_diff(res_u, exp_rne)
    reason = "" if passed else (
        f"|err|={_safe_float(err):.6e} >= bound={_safe_float(bound):.6e} "
        f"(got={format_fp32(res_u)}, exact={_safe_float(exact):+.8e}, ulp={u_err:+d})"
    )
    return CheckResult(
        passed=passed,
        in_domain=True,
        reason=reason,
        abs_err=_safe_float(err),
        bound=_safe_float(bound),
        rel_err=rel,
        ulp_err=u_err,
        expected_u32=exp_rne,
    )


def check_fdiv(a_u: int, b_u: int, res_u: int | None) -> CheckResult:
    """fdiv の float_spec.pdf 厳密判定.

    条件:
      B != 0 かつ -2^127 < A, B, A/B < 2^127 のとき
      |R - A/B| < max(|A/B|*2^-20, 2^-126)
    """
    a = bits_to_fraction(a_u)
    b = bits_to_fraction(b_u)
    if b == 0:
        return CheckResult(passed=True, in_domain=False, reason="division by zero (out of spec domain)")
    exact = a / b

    if not (_in_open_127(a) and _in_open_127(b) and _in_open_127(exact)):
        return CheckResult(passed=True, in_domain=False, reason="out of spec domain")

    exp_rne = fraction_to_rne_fp32(exact)
    if res_u is None:
        return CheckResult(passed=False, in_domain=True, reason="X/Z or invalid HEX in output", expected_u32=exp_rne)
    if not is_valid_fp32(res_u):
        return CheckResult(passed=False, in_domain=True, reason="invalid fp32 output", expected_u32=exp_rne)

    r = bits_to_fraction(res_u)
    err = abs(r - exact)
    bound = max(abs(exact) * TWO_M20, EPS)
    passed = err < bound
    rel = _safe_float(err / abs(exact)) if exact != 0 else (0.0 if err == 0 else math.inf)
    u_err = ulp_diff(res_u, exp_rne)
    reason = "" if passed else (
        f"|err|={_safe_float(err):.6e} >= bound={_safe_float(bound):.6e} "
        f"(got={format_fp32(res_u)}, exact={_safe_float(exact):+.8e}, ulp={u_err:+d})"
    )
    return CheckResult(
        passed=passed,
        in_domain=True,
        reason=reason,
        abs_err=_safe_float(err),
        bound=_safe_float(bound),
        rel_err=rel,
        ulp_err=u_err,
        expected_u32=exp_rne,
    )


def check_fsqrt(a_u: int, res_u: int | None) -> CheckResult:
    """fsqrt の float_spec.pdf 厳密判定.

    条件:
      0 <= A < 2^127 のとき
      |R - sqrt(A)| < max(sqrt(A)*2^-20, 2^-126)

    両辺の平方を用いた有理数不等式により丸め誤差ゼロで厳密判定する:
      - A == 0: |R| < 2^-126
      - A >= 2^-212 (すべての正の正規化数が該当):
          R > 0 かつ (1 - 2^-20)^2 * A < R^2 < (1 + 2^-20)^2 * A
      - 0 < A < 2^-212:
          R - EPS < sqrt(A) < R + EPS
    """
    a = bits_to_fraction(a_u)
    if not (0 <= a < LIMIT_127):
        return CheckResult(passed=True, in_domain=False, reason="out of spec domain")

    exp_rne = _sqrt_reference_rne(a)
    if res_u is None:
        return CheckResult(passed=False, in_domain=True, reason="X/Z or invalid HEX in output", expected_u32=exp_rne)
    if not is_valid_fp32(res_u):
        return CheckResult(passed=False, in_domain=True, reason="invalid fp32 output", expected_u32=exp_rne)

    r = bits_to_fraction(res_u)
    if a == 0:
        passed = abs(r) < EPS
        abs_err_f = _safe_float(abs(r))
        bound_f = float(EPS)
        rel_f = 0.0 if r == 0 else math.inf
        exact_f = 0.0
    elif a >= SQRT_EPS_THRESHOLD:
        r_sq = r * r
        passed = (r > 0) and (SQRT_LO_SQ * a < r_sq < SQRT_HI_SQ * a)
        exact_f = math.sqrt(_safe_float(a))
        abs_err_f = abs(_safe_float(r) - exact_f)
        bound_f = max(exact_f * (2.0**-20), float(EPS))
        rel_f = abs_err_f / exact_f
    else:
        # A案の正規化数では発生しないが、定義上の一般性を保つ
        lo = r - EPS
        hi = r + EPS
        passed = (hi > 0) and (a < hi * hi) and (lo < 0 or lo * lo < a)
        exact_f = math.sqrt(_safe_float(a))
        abs_err_f = abs(_safe_float(r) - exact_f)
        bound_f = float(EPS)
        rel_f = abs_err_f / exact_f

    u_err = ulp_diff(res_u, exp_rne)
    reason = "" if passed else (
        f"|err|={abs_err_f:.6e} >= bound={bound_f:.6e} "
        f"(got={format_fp32(res_u)}, exact={exact_f:+.8e}, ulp={u_err:+d})"
    )
    return CheckResult(
        passed=passed,
        in_domain=True,
        reason=reason,
        abs_err=abs_err_f,
        bound=bound_f,
        rel_err=rel_f,
        ulp_err=u_err,
        expected_u32=exp_rne,
    )


def check_ftoi(a_u: int, res_u: int | None) -> CheckResult:
    """ftoi (int_of_float) の float_spec.pdf 厳密判定.

    条件:
      -2^31 + 1 <= A <= 2^31 - 1 のとき、32bit 符号付き整数 I_out を返し、
      |I - A| < |I_out - A| を満たす 32bit 整数 I が存在しないこと。
      (小数部がちょうど .5 の tie の場合は隣接する2整数のどちらも仕様上 PASS)
    """
    a = bits_to_fraction(a_u)
    if not (Fraction(-(1 << 31) + 1) <= a <= Fraction((1 << 31) - 1)):
        return CheckResult(passed=True, in_domain=False, reason="out of spec domain")

    # 参考値として四捨五入 (round half away from zero) の 32bit 表現を計算
    fl = math.floor(a)
    ce = math.ceil(a)
    d_fl = a - fl
    d_ce = ce - a
    min_dist = min(d_fl, d_ce)
    if d_fl < d_ce:
        ref_i = fl
    elif d_ce < d_fl:
        ref_i = ce
    else:
        # ちょうど .5: ゼロから遠い方 (一般的な四捨五入)
        ref_i = ce if a >= 0 else fl
    ref_u = ref_i & 0xFFFFFFFF

    if res_u is None:
        return CheckResult(passed=False, in_domain=True, reason="X/Z or invalid HEX in output", expected_u32=ref_u)

    got_i = u32_to_i32(res_u)
    err = abs(Fraction(got_i) - a)
    passed = err == min_dist
    reason = "" if passed else (
        f"got_i={got_i} (0x{res_u:08x}), |got-A|={_safe_float(err):.6f} > min_dist={_safe_float(min_dist):.6f} "
        f"(A={_safe_float(a):+.8f}, nearest={fl}/{ce})"
    )
    return CheckResult(
        passed=passed,
        in_domain=True,
        reason=reason,
        abs_err=_safe_float(err),
        bound=_safe_float(min_dist),
        ulp_err=got_i - ref_i,
        expected_u32=ref_u,
    )


def check_itof(i_u: int, res_u: int | None) -> CheckResult:
    """itof (float_of_int) の float_spec.pdf 厳密判定.

    条件:
      任意の 32bit 符号付き整数 I に対し有効な浮動小数点数 R を返し、
      |A - I| < |R - I| を満たす有効な浮動小数点数 A が存在しないこと。
      (ちょうど中間の tie の場合は隣接する2つの単精度数のどちらも仕様上 PASS)
    """
    i_val = u32_to_i32(i_u)
    exact = Fraction(i_val, 1)
    exp_rne = fraction_to_rne_fp32(exact)

    if res_u is None:
        return CheckResult(passed=False, in_domain=True, reason="X/Z or invalid HEX in output", expected_u32=exp_rne)
    if not is_valid_fp32(res_u):
        return CheckResult(passed=False, in_domain=True, reason="invalid fp32 output", expected_u32=exp_rne)

    r = bits_to_fraction(res_u)
    err = abs(r - exact)

    abs_i = abs(i_val)
    if abs_i == 0:
        min_dist = Fraction(0, 1)
    else:
        e = abs_i.bit_length() - 1
        if e <= FRAC_BITS:
            # |I| < 2^24 はすべて単精度で厳密表現可能
            min_dist = Fraction(0, 1)
        else:
            step = 1 << (e - FRAC_BITS)
            rem = abs_i & (step - 1)
            min_dist = Fraction(min(rem, step - rem), 1)

    passed = err == min_dist
    u_err = ulp_diff(res_u, exp_rne)
    reason = "" if passed else (
        f"got={format_fp32(res_u)}, |R-I|={_safe_float(err):.6e} > min_dist={_safe_float(min_dist):.6e} "
        f"(I={i_val}, ulp_vs_rne={u_err:+d})"
    )
    return CheckResult(
        passed=passed,
        in_domain=True,
        reason=reason,
        abs_err=_safe_float(err),
        bound=_safe_float(min_dist),
        ulp_err=u_err,
        expected_u32=exp_rne,
    )
