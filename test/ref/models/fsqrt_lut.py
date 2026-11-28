#!/usr/bin/env python3
"""fsqrt BRAM 線形近似 (1024 x 36bit = Artix-7 RAMB36E1 x 1) モデル＆LUT 最適化・全数検証.

アドレス構成 (10bit = rs1_val[23:14]):
  addr = {e[0], f[22:14]}
    - addr[9] == 1 (e[0] == 1, 真の指数 E が偶数): x = 1 + f * 2^-23 in [1, 2)
    - addr[9] == 0 (e[0] == 0, 真の指数 E が奇数): x = 2 * (1 + f * 2^-23) in [2, 4)

ハードウェア演算式 (DSP48E1 MAC 1回, 符号なし):
  b: W_b bit unsigned (Q0.W_b, 重み 2^-W_b)
  a: W_a bit unsigned (Q0.W_a, 重み 2^-W_a), W_b + W_a == 36
  dx = f[13:0]: 14 bit unsigned (重み 2^-23)
  S_b = (W_a + 23) - W_b
  P = (b << S_b) + round_bias + a * dx
  frac_out = P >> W_a   (23 bit unsigned; 上端キャリーアウト 0 件を制約で保証)

全 2^24 = 16,777,216 通り (e[0] x f[22:0]) を厳密評価し、
float_spec.pdf 基準 |R - sqrt(A)| < max(sqrt(A)*2^-20, 2^-126) を確認する。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
import time

import numpy as np

# test/ を sys.path に追加 (spec_checker クロスチェック用)
TEST_DIR = Path(__file__).resolve().parents[2]
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from ref.spec_checker import check_fsqrt  # noqa: E402


ADDR_BITS = 10
NUM_ENTRIES = 1 << ADDR_BITS  # 1024
K_BITS = 9                    # 仮数上位 9 bit
DX_BITS = 23 - K_BITS         # 仮数下位 14 bit
DX_COUNT = 1 << DX_BITS       # 16384
FRAC_MAX = (1 << 23) - 1


@dataclass
class LutConfig:
    name: str
    w_b: int
    w_a: int
    round_bias: int  # P 加算時の下位固定バイアス (0 または 1 << (w_a - 1) 等)

    @property
    def s_b(self) -> int:
        return (self.w_a + 23) - self.w_b

    @property
    def total_bits(self) -> int:
        return self.w_b + self.w_a


@dataclass
class EvalResult:
    cfg: LutConfig
    b_table: np.ndarray        # uint32[1024]
    a_table: np.ndarray        # uint32[1024]
    max_rel_err: float
    max_spec_ratio: float      # max(|R - sqrt(x)| / (sqrt(x) * 2^-20)); < 1.0 で合格
    max_abs_ulp: int
    ulp_hist: dict[int, int]
    carry_overflows: int
    spec_failures: int
    worst_addr: int
    worst_dx: int
    worst_got_frac: int


def _interval_truth(addr: int, dx: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """指定 addr (10bit) 内の全 16384 点について x, sqrt(x), ideal_frac_f64, rne_frac を返す."""
    e0 = (addr >> K_BITS) & 1
    f_hi = addr & ((1 << K_BITS) - 1)
    f_all = (f_hi << DX_BITS) | dx  # 0 .. 2^23 - 1
    m = 1.0 + f_all.astype(np.float64) * (2.0**-23)
    x = m if e0 == 1 else (2.0 * m)
    y = np.sqrt(x)  # float64 (53bit 精度; 単精度 24bit に対し誤差 < 2^-29 ulp)
    # 単精度 RNE 参照値 (y in [1, 2), exact half-way は sqrt(rational) では有限小数になり得るが
    # float64 の 53bit は単精度 24bit の 2 倍超のため np.float32(y) は単精度 RNE と完全一致)
    y_f32 = y.astype(np.float32)
    y_u32 = y_f32.view(np.uint32)
    # y_f32 == 2.0 (0x40000000) の点では frac_rne = 2^23 と表す (差分計算用)
    rne_frac = np.where(y_u32 == 0x40000000, np.int64(1 << 23), (y_u32 & 0x7FFFFF).astype(np.int64))
    return x, y, rne_frac


def optimize_and_evaluate(cfg: LutConfig, optimize: bool = True) -> EvalResult:
    """1024 区間すべてを最適化し、全 2^24 通りの誤差を厳密集計する."""
    assert cfg.total_bits == 36, f"Total width must be 36, got {cfg.total_bits}"
    assert cfg.s_b >= 0

    dx = np.arange(DX_COUNT, dtype=np.int64)
    b_max = (1 << cfg.w_b) - 1
    a_max = (1 << cfg.w_a) - 1
    s_b = cfg.s_b
    w_a = cfg.w_a
    bias = cfg.round_bias

    b_table = np.zeros(NUM_ENTRIES, dtype=np.uint32)
    a_table = np.zeros(NUM_ENTRIES, dtype=np.uint32)

    max_rel_err = 0.0
    max_spec_ratio = 0.0
    max_abs_ulp = 0
    ulp_hist: dict[int, int] = {}
    carry_overflows = 0
    spec_failures = 0
    worst_addr = 0
    worst_dx = 0
    worst_got_frac = 0

    scale_b = float(1 << cfg.w_b)
    scale_a = float(1 << cfg.w_a)

    for addr in range(NUM_ENTRIES):
        x, y, rne_frac = _interval_truth(addr, dx)
        frac_true = y - 1.0  # in [0, 1)

        # 連続ミニマックス初期値: 両端の割線傾き + 中央シフト切片
        # (dx * 2^-23) に対する傾き
        dx_span = (DX_COUNT - 1) * (2.0**-23)
        a_cont = (frac_true[-1] - frac_true[0]) / dx_span
        resid = frac_true - a_cont * (dx * (2.0**-23))
        b_cont = 0.5 * (float(resid.min()) + float(resid.max()))

        # 切り捨てバイアスを考慮した b 初期中心:
        # P = (b << s_b) + bias + a * dx, frac = floor(P / 2^w_a)
        # 期待値: b / 2^w_b + (bias - 0.5*2^w_a)/2^(w_a+23) + a*dx / 2^(w_a+23)
        # よって b_cont に (0.5*2^w_a - bias) / 2^(w_a+23) を補正して初期値にする
        b_shift_corr = (0.5 * (1 << w_a) - bias) * (2.0 ** -(w_a + 23))
        a0 = int(np.clip(round(a_cont * scale_a), 0, a_max))
        b0 = int(np.clip(round((b_cont + b_shift_corr) * scale_b), 0, b_max))

        if not optimize:
            best_b, best_a = b0, a0
            # 上端キャリーアウト防止だけ適用
            while (((best_b << s_b) + bias + best_a * (DX_COUNT - 1)) >> w_a) > FRAC_MAX and best_b > 0:
                best_b -= 1
        else:
            # 近傍探索: (max_spec_ratio, max_abs_ulp, sum_sq_ulp) の辞書順最小
            best_key = (float("inf"), 10**9, float("inf"))
            best_b, best_a = b0, a0

            # 1回のベクトル評価で (a, b) 候補を精査
            a_lo = max(0, a0 - 2)
            a_hi = min(a_max, a0 + 2)
            b_lo = max(0, b0 - 4)
            b_hi = min(b_max, b0 + 4)

            for a_cand in range(a_lo, a_hi + 1):
                adx = a_cand * dx + bias  # int64[16384]
                for b_cand in range(b_lo, b_hi + 1):
                    # 上端キャリーアウトチェック (最大 dx = 16383 で判定可能: a>=0 なので単調増加)
                    max_frac = ((b_cand << s_b) + int(adx[-1])) >> w_a
                    if max_frac > FRAC_MAX:
                        continue
                    frac_got = ((b_cand << s_b) + adx) >> w_a
                    y_got = 1.0 + frac_got.astype(np.float64) * (2.0**-23)
                    abs_err = np.abs(y_got - y)
                    # float_spec.pdf: |R - sqrt(A)| < sqrt(A) * 2^-20 (全正規化数で sqrt(A)*2^-20 >= 2^-126)
                    spec_ratio = float(np.max(abs_err / (y * (2.0**-20))))
                    ulp_d = frac_got - rne_frac
                    m_ulp = int(np.max(np.abs(ulp_d)))
                    sq_ulp = float(np.dot(ulp_d, ulp_d))
                    key = (spec_ratio, m_ulp, sq_ulp)
                    if key < best_key:
                        best_key = key
                        best_b, best_a = b_cand, a_cand

        b_table[addr] = best_b
        a_table[addr] = best_a

        # 最終評価 (この区間の全 16384 点)
        p_all = (int(best_b) << s_b) + bias + int(best_a) * dx
        frac_got = p_all >> w_a
        ovf = int(np.sum(frac_got > FRAC_MAX))
        carry_overflows += ovf

        y_got = 1.0 + (frac_got & FRAC_MAX).astype(np.float64) * (2.0**-23)
        abs_err = np.abs(y_got - y)
        rel_err = abs_err / y
        ratios = rel_err * (2.0**20)

        idx_w = int(np.argmax(ratios))
        local_max_ratio = float(ratios[idx_w])
        if local_max_ratio > max_spec_ratio:
            max_spec_ratio = local_max_ratio
            worst_addr = addr
            worst_dx = idx_w
            worst_got_frac = int(frac_got[idx_w])
        local_max_rel = float(rel_err[idx_w])
        if local_max_rel > max_rel_err:
            max_rel_err = local_max_rel

        spec_failures += int(np.sum(ratios >= 1.0))

        ulp_d = frac_got - rne_frac
        vals, cnts = np.unique(ulp_d, return_counts=True)
        for v, c in zip(vals.tolist(), cnts.tolist()):
            ulp_hist[v] = ulp_hist.get(v, 0) + c
            if abs(v) > max_abs_ulp:
                max_abs_ulp = abs(v)

    return EvalResult(
        cfg=cfg,
        b_table=b_table,
        a_table=a_table,
        max_rel_err=max_rel_err,
        max_spec_ratio=max_spec_ratio,
        max_abs_ulp=max_abs_ulp,
        ulp_hist=ulp_hist,
        carry_overflows=carry_overflows,
        spec_failures=spec_failures,
        worst_addr=worst_addr,
        worst_dx=worst_dx,
        worst_got_frac=worst_got_frac,
    )


def crosscheck_exact(res: EvalResult) -> None:
    """最悪ケース・端点・代表ケースを spec_checker.check_fsqrt (Fraction 厳密判定) でクロスチェック."""
    cfg = res.cfg
    test_points: list[tuple[int, int]] = [
        (res.worst_addr, res.worst_dx),
        (0, 0),                    # e[0]=0, f=0 -> sqrt(2.0)
        (0, DX_COUNT - 1),
        (511, DX_COUNT - 1),       # e[0]=0, f=0x7FFFFF -> sqrt(4 - 2^-22) (上端)
        (512, 0),                  # e[0]=1, f=0 -> sqrt(1.0)
        (512, 1),
        (1023, DX_COUNT - 1),      # e[0]=1, f=0x7FFFFF -> sqrt(2 - 2^-23)
    ]
    # 各区間の両端と中央もスポットチェック (計 ~3000 点)
    for addr in range(0, NUM_ENTRIES, 4):
        test_points.append((addr, 0))
        test_points.append((addr, DX_COUNT // 2))
        test_points.append((addr, DX_COUNT - 1))

    for addr, dx_v in test_points:
        e0 = (addr >> K_BITS) & 1
        f_hi = addr & ((1 << K_BITS) - 1)
        f_in = (f_hi << DX_BITS) | dx_v
        # 代表指数: e0==1 -> e=127 (1.0..2.0), e0==0 -> e=128 (2.0..4.0), さらに最小 e=1,2 と最大 e=253,252
        for e_in in ((1, 127, 253) if e0 == 1 else (2, 128, 252)):
            a_u = (e_in << 23) | f_in
            b_v = int(res.b_table[addr])
            a_v = int(res.a_table[addr])
            p = (b_v << cfg.s_b) + cfg.round_bias + a_v * dx_v
            frac_out = p >> cfg.w_a
            assert 0 <= frac_out <= FRAC_MAX, f"carry overflow at addr={addr}, dx={dx_v}: {frac_out:#x}"
            e_out = (e_in + 127) >> 1
            res_u = (e_out << 23) | frac_out
            chk = check_fsqrt(a_u, res_u)
            assert chk.passed, f"Exact spec_checker FAILED: in=0x{a_u:08x} out=0x{res_u:08x} ({chk.reason})"


def format_report(res: EvalResult) -> str:
    cfg = res.cfg
    lines = [
        f"--- {cfg.name} (W_b={cfg.w_b}, W_a={cfg.w_a}, S_b={cfg.s_b}, bias={cfg.round_bias:#x}) ---",
        f"  検査数 (全仮数×偶奇) : {NUM_ENTRIES * DX_COUNT:,} 通り",
        f"  キャリーアウト件数   : {res.carry_overflows}",
        f"  仕様違反 (ratio>=1)  : {res.spec_failures}",
        f"  最大誤差比           : {res.max_spec_ratio:.6f} (< 1.0 が合格; 1.0 = 2^-20 相対誤差)",
        f"  最大相対誤差         : {res.max_rel_err:.6e} (≈ {res.max_rel_err * (2**23):.3f} x 2^-23)",
        f"  最大 |ULP 差| vs RNE : {res.max_abs_ulp} ulp",
        f"  最悪点               : addr={res.worst_addr} (e0={res.worst_addr>>9}, f_hi={res.worst_addr&0x1FF}), dx={res.worst_dx}",
        "  ULP 差分布 (vs RNE):",
    ]
    total = NUM_ENTRIES * DX_COUNT
    for k in sorted(res.ulp_hist.keys()):
        cnt = res.ulp_hist[k]
        lines.append(f"    {k:+3d} ulp : {cnt:10,d} ({100.0 * cnt / total:6.2f}%)")
    return "\n".join(lines)


def emit_sv_initial(res: EvalResult, per_line: int = 4) -> str:
    """fsqrt.sv 内にそのまま貼れる initial ブロック文字列を生成する."""
    cfg = res.cfg
    lines = [
        f"// Auto-generated by test/ref/models/fsqrt_lut.py ({cfg.name})",
        f"// Format: rom[addr] = {{ b[{cfg.w_b-1}:0] (Q0.{cfg.w_b}), a[{cfg.w_a-1}:0] (Q0.{cfg.w_a}) }} = 36bit",
        f"// Addr  : rs1_val[23:14] = {{e[0], f[22:14]}}",
        f"// Formula: P = ({{b, {cfg.s_b}'d0}}) + {cfg.round_bias} + (a * dx); frac_out = P[{cfg.w_a+22}:{cfg.w_a}];",
        "initial begin",
    ]
    mask_a = (1 << cfg.w_a) - 1
    for base in range(0, NUM_ENTRIES, per_line):
        items = []
        for i in range(base, min(base + per_line, NUM_ENTRIES)):
            word = (int(res.b_table[i]) << cfg.w_a) | (int(res.a_table[i]) & mask_a)
            items.append(f"rom[{i:4d}] = 36'h{word:09x};")
        lines.append("  " + " ".join(items))
    lines.append("end")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="fsqrt BRAM 線形近似 LUT 最適化＆全数検証")
    parser.add_argument(
        "--emit-sv",
        type=Path,
        default=None,
        help="最良構成の SV initial ブロックを指定パスに出力する (例: test/work/fsqrt_rom_init.svh)",
    )
    parser.add_argument(
        "--config",
        choices=["compare", "b23_a13", "b24_a12", "b24_a12_nobias", "b25_a11"],
        default="compare",
        help="評価する構成 (既定: compare で全候補比較)",
    )
    args = parser.parse_args()

    candidates = {
        "b23_a13": LutConfig("b23_a13 (bias=13'h1000)", w_b=23, w_a=13, round_bias=1 << 12),
        "b24_a12": LutConfig("b24_a12 (bias=11'h400)", w_b=24, w_a=12, round_bias=1 << 10),
        "b24_a12_nobias": LutConfig("b24_a12 (bias=0)", w_b=24, w_a=12, round_bias=0),
        "b25_a11": LutConfig("b25_a11 (bias=9'h100)", w_b=25, w_a=11, round_bias=1 << 8),
    }

    keys = list(candidates.keys()) if args.config == "compare" else [args.config]
    results: list[EvalResult] = []
    for k in keys:
        t0 = time.perf_counter()
        res = optimize_and_evaluate(candidates[k], optimize=True)
        dt = time.perf_counter() - t0
        crosscheck_exact(res)
        print(format_report(res))
        print(f"  (計算＋厳密クロスチェック時間: {dt:.2f} s)\n")
        results.append(res)

    best = min(results, key=lambda r: (r.spec_failures, r.carry_overflows, r.max_spec_ratio, r.max_abs_ulp))
    print(f"=== 最良構成: {best.cfg.name} (max_spec_ratio={best.max_spec_ratio:.6f}, max_ulp=±{best.max_abs_ulp}) ===")

    if args.emit_sv is not None:
        args.emit_sv.parent.mkdir(parents=True, exist_ok=True)
        args.emit_sv.write_text(emit_sv_initial(best), encoding="utf-8")
        print(f"[emit] SV initial block -> {args.emit_sv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
