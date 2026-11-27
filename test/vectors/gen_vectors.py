"""全6命令向けのコーナーケース＋浮動小数点特化ランダムテストベクタ生成スクリプト.

出力フォーマット (1行1ベクタ、スペース区切りHEX):
  - fadd  : <aux_1hex> <rs1_8hex> <rs2_8hex>   (aux=0: fadd, aux=1: fsub)
  - fmul  : <rs1_8hex> <rs2_8hex>
  - fdiv  : <rs1_8hex> <rs2_8hex>
  - fsqrt : <rs1_8hex>
  - ftoi  : <rs1_8hex>
  - itof  : <rs1_8hex>
"""

from __future__ import annotations

import argparse
from pathlib import Path
import random
import sys

_TEST_DIR = Path(__file__).resolve().parent.parent
if str(_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(_TEST_DIR))

from ref.fp32 import (  # noqa: E402
    FRAC_MASK,
    LIMIT_127,
    bits_to_fraction,
    i32_to_u32,
    pack_fp32,
)

ALL_OPS = ("fadd", "fmul", "fdiv", "fsqrt", "ftoi", "itof")

# float_spec.pdf 定義域 (-2^127, 2^127) 内の代表コーナー値 (A案では exp=0 はすべて 0)
CORNER_FP32_POS = [
    0x00000000,  # +0
    0x00000001,  # 非正規化ビット列 (A案では 0)
    0x007FFFFF,  # 最大非正規化ビット列 (A案では 0)
    0x00800000,  # 2^-126 (最小正正規化数 = eps)
    0x00800001,  # 最小正正規化数 + 1 ulp
    0x00FFFFFF,  # exp=1, frac=all 1
    0x01000000,  # 2^-125
    0x20000000,  # 2^-63
    0x20800000,  # 2^-62
    0x33800000,  # 2^-24
    0x34000000,  # 2^-23
    0x3EFFFFFF,  # 0.5 - 2^-25
    0x3F000000,  # 0.5
    0x3F7FFFFF,  # 1.0 - 2^-24
    0x3F800000,  # 1.0
    0x3F800001,  # 1.0 + 2^-23
    0x3FB504F3,  # ~sqrt(2) (mul 正規化境界付近)
    0x3FB504F4,  # ~sqrt(2) + 1 ulp
    0x3FD55555,  # ~5/3
    0x3FFFFFFF,  # 2.0 - 2^-23
    0x40000000,  # 2.0
    0x40400000,  # 3.0
    0x40490FDB,  # pi
    0x4B000000,  # 2^23
    0x4B800000,  # 2^24
    0x4EFFFFFF,  # (2 - 2^-23) * 2^30 = 2^31 - 128 (ftoi 最大定義域値)
    0x5F000000,  # 2^63
    0x7E000000,  # 2^125
    0x7E800000,  # 2^126 (Fpu.pdf §5.1 fdiv バグ領域の境界)
    0x7E800001,  # 2^126 + 1 ulp
    0x7EFFFFFF,  # (2 - 2^-23) * 2^126 (定義域最大正規化数 < 2^127)
]

CORNER_FP32_ALL = CORNER_FP32_POS + [v | 0x80000000 for v in CORNER_FP32_POS]


def _rand_fp32_domain(rng: random.Random, allow_neg: bool = True, exp_lo: int = 1, exp_hi: int = 253) -> int:
    """定義域 (-2^127, 2^127) 内のランダム浮動小数点数を生成 (指数・仮数独立分布)."""
    sign = rng.randint(0, 1) if allow_neg else 0
    r = rng.random()
    if r < 0.02:
        # ゼロ / 非正規化ビット列
        return pack_fp32(sign, 0, rng.randint(0, FRAC_MASK) if rng.random() < 0.5 else 0)
    exp = rng.randint(exp_lo, exp_hi)
    # 仮数部のパターン多様性 (全0 / 全1 / ランダム)
    m_mode = rng.random()
    if m_mode < 0.05:
        frac = 0
    elif m_mode < 0.10:
        frac = FRAC_MASK
    elif m_mode < 0.15:
        frac = 1
    else:
        frac = rng.randint(0, FRAC_MASK)
    return pack_fp32(sign, exp, frac)


def _in_127_u32(u: int) -> bool:
    v = bits_to_fraction(u)
    return -LIMIT_127 < v < LIMIT_127


def gen_fadd_vectors(num_random: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    vecs: list[tuple[int, int, int]] = []

    # 1. コーナー値同士の網羅ペア (fadd/fsub 両方、結果が定義域内のもの)
    small_corners = [
        0x00000000, 0x80000000, 0x00000001, 0x00800000, 0x80800000,
        0x3F000000, 0xBF000000, 0x3F7FFFFF, 0x3F800000, 0xBF800000,
        0x3F800001, 0x3FFFFFFF, 0x40000000, 0xC0000000,
        0x7E000000, 0x7E800000, 0xFE800000, 0x7EFFFFFF, 0xFEFFFFFF,
    ]
    for aux in (0, 1):
        for a in small_corners:
            va = bits_to_fraction(a)
            for b in small_corners:
                vb = bits_to_fraction(b)
                exact = (va - vb) if aux else (va + vb)
                if -LIMIT_127 < exact < LIMIT_127:
                    vecs.append((aux, a, b))

    # 2. 指数差 0..27 および G/R/S・丸め・桁落ち・指数繰り上がり特化ケース
    base_exps = [1, 2, 24, 127, 128, 200, 252]
    for exp_a in base_exps:
        for diff in range(0, 28):
            exp_b = exp_a - diff
            if exp_b < 1:
                continue
            for frac_a in (0x000000, 0x000001, 0x7FFFFF, 0x555555):
                for frac_b in (0x000000, 0x000001, 0x7FFFFF, 0x400000):
                    a = pack_fp32(0, exp_a, frac_a)
                    b = pack_fp32(0, exp_b, frac_b)
                    va, vb = bits_to_fraction(a), bits_to_fraction(b)
                    for aux in (0, 1):
                        exact = (va - vb) if aux else (va + vb)
                        if -LIMIT_127 < exact < LIMIT_127:
                            vecs.append((aux, a, b))
                            # 符号反転・入れ替え版も数点追加
                            vecs.append((aux, b, a))

    # 3. 浮動小数点特化ランダム (半分は指数差 0..26 に集中、半分は広域)
    count = 0
    while count < num_random:
        aux = rng.randint(0, 1)
        if count < (num_random * 3) // 4:
            # 指数差が近いペア (加算丸め・桁落ち・正規化シフトが最も起きやすい)
            exp_a = rng.randint(1, 253)
            delta = rng.randint(-26, 26)
            exp_b = max(1, min(253, exp_a + delta))
            a = pack_fp32(rng.randint(0, 1), exp_a, rng.randint(0, FRAC_MASK))
            b = pack_fp32(rng.randint(0, 1), exp_b, rng.randint(0, FRAC_MASK))
        else:
            a = _rand_fp32_domain(rng)
            b = _rand_fp32_domain(rng)
        va, vb = bits_to_fraction(a), bits_to_fraction(b)
        exact = (va - vb) if aux else (va + vb)
        if -LIMIT_127 < exact < LIMIT_127:
            vecs.append((aux, a, b))
            count += 1

    return [f"{aux:x} {a:08x} {b:08x}" for aux, a, b in vecs]


def gen_fmul_vectors(num_random: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    vecs: list[tuple[int, int]] = []

    # 1. コーナー値ペア
    for a in CORNER_FP32_ALL:
        va = bits_to_fraction(a)
        for b in CORNER_FP32_ALL:
            vb = bits_to_fraction(b)
            exact = va * vb
            if -LIMIT_127 < exact < LIMIT_127:
                vecs.append((a, b))

    # 2. 仮数乗算の丸め・正規化境界 (1.0〜2.0 の区間)
    for fa in (0x000000, 0x000001, 0x0007FF, 0x000800, 0x3504F3, 0x3504F4, 0x7FFFFE, 0x7FFFFF):
        for fb in (0x000000, 0x000001, 0x0007FF, 0x000800, 0x3504F3, 0x3504F4, 0x7FFFFE, 0x7FFFFF):
            vecs.append((pack_fp32(0, 127, fa), pack_fp32(0, 127, fb)))

    # 3. ランダム (積が定義域内かつアンダーフロー境界も含む)
    count = 0
    while count < num_random:
        if count < num_random // 5:
            # アンダーフロー境界 (exp_a + exp_b - 127 が -2..+2 付近)
            exp_a = rng.randint(1, 128)
            exp_b = max(1, min(253, 127 - exp_a + rng.randint(-2, 2)))
            a = pack_fp32(rng.randint(0, 1), exp_a, rng.randint(0, FRAC_MASK))
            b = pack_fp32(rng.randint(0, 1), exp_b, rng.randint(0, FRAC_MASK))
        else:
            exp_a = rng.randint(1, 253)
            # 積が 2^127 未満に収まりやすいよう exp_b の上限を調整
            max_eb = min(253, 252 + 127 - exp_a)
            if max_eb < 1:
                continue
            exp_b = rng.randint(1, max_eb)
            a = pack_fp32(rng.randint(0, 1), exp_a, rng.randint(0, FRAC_MASK))
            b = pack_fp32(rng.randint(0, 1), exp_b, rng.randint(0, FRAC_MASK))
        exact = bits_to_fraction(a) * bits_to_fraction(b)
        if -LIMIT_127 < exact < LIMIT_127:
            vecs.append((a, b))
            count += 1

    return [f"{a:08x} {b:08x}" for a, b in vecs]


def gen_fdiv_vectors(num_random: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    vecs: list[tuple[int, int]] = []

    # 1. コーナー値ペア (Fpu.pdf §5.1 の B in [2^126, 2^127) ケースを含む)
    nonzero_corners = [v for v in CORNER_FP32_ALL if ((v >> 23) & 0xFF) != 0]
    for a in CORNER_FP32_ALL:
        va = bits_to_fraction(a)
        for b in nonzero_corners:
            vb = bits_to_fraction(b)
            exact = va / vb
            if -LIMIT_127 < exact < LIMIT_127:
                vecs.append((a, b))

    # 2. 仮数部比の境界 (m_a == m_b, m_a = m_b - 1, m_a = m_b + 1, Fpu.pdf §5.1 領域)
    for exp_b in (1, 127, 252, 253):
        for fb in (0x000000, 0x000001, 0x3FFFFF, 0x400000, 0x7FFFFE, 0x7FFFFF):
            b = pack_fp32(0, exp_b, fb)
            for fa in (fb, (fb - 1) & FRAC_MASK, (fb + 1) & FRAC_MASK, 0x000000, 0x7FFFFF):
                a = pack_fp32(0, exp_b, fa)
                exact = bits_to_fraction(a) / bits_to_fraction(b)
                if -LIMIT_127 < exact < LIMIT_127:
                    vecs.append((a, b))

    # 3. ランダム (商が定義域内、アンダーフロー・2^126除数境界も重点投入)
    count = 0
    while count < num_random:
        if count < num_random // 6:
            # Fpu.pdf §5.1 領域: |B| in [2^126, 2^127) (exp_b == 253)
            exp_b = 253
            exp_a = rng.randint(127, 253)
        elif count < num_random // 3:
            # アンダーフロー境界: exp_a - exp_b + 127 が -2..+2 付近
            exp_b = rng.randint(127, 253)
            exp_a = max(1, min(253, exp_b - 127 + rng.randint(-2, 2)))
        else:
            exp_b = rng.randint(1, 253)
            max_ea = min(253, exp_b + 125)
            exp_a = rng.randint(1, max_ea)
        a = pack_fp32(rng.randint(0, 1), exp_a, rng.randint(0, FRAC_MASK))
        b = pack_fp32(rng.randint(0, 1), exp_b, rng.randint(0, FRAC_MASK))
        exact = bits_to_fraction(a) / bits_to_fraction(b)
        if -LIMIT_127 < exact < LIMIT_127:
            vecs.append((a, b))
            count += 1

    return [f"{a:08x} {b:08x}" for a, b in vecs]


def gen_fsqrt_vectors(num_random: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    vecs: list[int] = []

    # 1. 非負コーナー値 (+0, -0, 非正規化ビット列, 偶奇指数境界, 完全平方など)
    vecs.extend(CORNER_FP32_POS)
    vecs.extend([0x80000000, 0x80000001, 0x807FFFFF])  # A案では 0.0 -> sqrt(0)=0

    # 偶奇指数ごとの仮数境界 (テーブル引きのインデックス境界)
    for exp in (1, 2, 126, 127, 128, 129, 252, 253):
        for frac in (
            0x000000, 0x000001, 0x001FFF, 0x002000, 0x003FFF, 0x004000,
            0x3FFFFF, 0x400000, 0x7FDFFF, 0x7FE000, 0x7FFFFE, 0x7FFFFF,
        ):
            vecs.append(pack_fp32(0, exp, frac))

    # 2. ランダム (0 <= A < 2^127)
    for _ in range(num_random):
        vecs.append(_rand_fp32_domain(rng, allow_neg=False, exp_lo=1, exp_hi=253))

    return [f"{a:08x}" for a in vecs]


def gen_ftoi_vectors(num_random: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    vecs: list[int] = []

    # 1. コーナーケース (小数・.5 tie・整数境界・最大定義域値)
    ftoi_pos_corners = [
        0x00000000,  # 0.0
        0x00000001,  # denorm -> 0
        0x00800000,  # 2^-126
        0x3E7FFFFF,  # 0.25 - eps
        0x3E800000,  # 0.25
        0x3EFFFFFF,  # 0.5 - 2^-25 (< 0.5)
        0x3F000000,  # 0.5 (tie)
        0x3F000001,  # 0.5 + 2^-24 (> 0.5)
        0x3F7FFFFF,  # 1.0 - 2^-24
        0x3F800000,  # 1.0
        0x3FBFFFFF,  # 1.5 - eps
        0x3FC00000,  # 1.5 (tie)
        0x3FC00001,  # 1.5 + eps
        0x40000000,  # 2.0
        0x40200000,  # 2.5 (tie)
        0x40600000,  # 3.5 (tie)
        0x41280000,  # 10.5 (tie)
        0x4A7FFFFF,  # 2^22 - 0.5
        0x4A800000,  # 2^22
        0x4A800001,  # 2^22 + 0.5 (tie)
        0x4AFFFFFF,  # 2^23 - 0.5 (tie)
        0x4B000000,  # 2^23
        0x4B000001,  # 2^23 + 1
        0x4B7FFFFF,  # 2^24 - 1
        0x4B800000,  # 2^24
        0x4E800000,  # 2^30
        0x4EFFFFFF,  # 2^31 - 128 (単精度で表現可能な最大 <= 2^31 - 1)
    ]
    for v in ftoi_pos_corners:
        vecs.append(v)
        vecs.append(v | 0x80000000)

    # 2. ランダム (小数域・丸め域・大整数域・極小域を網羅)
    for i in range(num_random):
        sign = rng.randint(0, 1)
        if i < num_random // 10:
            # 極小値 (exp=1..124 -> 結果は 0)
            exp = rng.randint(1, 124)
        elif i < num_random // 2:
            # 0.25 <= |A| < 2^23 (小数部あり・丸めが最も効く領域: exp=125..149)
            exp = rng.randint(125, 149)
        else:
            # 2^23 <= |A| < 2^31 (左シフト域: exp=150..157)
            exp = rng.randint(150, 157)
        frac = rng.randint(0, FRAC_MASK)
        vecs.append(pack_fp32(sign, exp, frac))

    return [f"{a:08x}" for a in vecs]


def gen_itof_vectors(num_random: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    vecs: list[int] = []

    # 1. コーナーケース (0, ±1, 2のべき前後, 2^24 丸め境界, 指数繰り上がり, INT_MIN/INT_MAX)
    special_ints = [
        0, 1, -1, 2, -2, 3, -3, 7, -7, 255, -255,
        (1 << 23) - 1, 1 << 23, (1 << 23) + 1,
        -((1 << 23) - 1), -(1 << 23), -((1 << 23) + 1),
        (1 << 24) - 1, 1 << 24, (1 << 24) + 1, (1 << 24) + 2, (1 << 24) + 3,
        -((1 << 24) - 1), -(1 << 24), -((1 << 24) + 1), -((1 << 24) + 2), -((1 << 24) + 3),
        (1 << 25) - 1, (1 << 25) - 2, -((1 << 25) - 1),
        0x00FFFFFF, 0x01000001, 0x01FFFFFF, 0x03FFFFFF,
        0x7FFFFFBF, 0x7FFFFFC0, 0x7FFFFFC1,  # 2^31 - 64 付近の tie / 繰り上がり境界
        0x7FFFFFFE, 0x7FFFFFFF,              # INT_MAX (2^31 - 1 -> 2^31 へ繰り上がり)
        -(0x7FFFFFFF),                       # -(2^31 - 1) -> -2^31 へ繰り上がり
        -(1 << 31),                          # INT_MIN (-2^31, exact 表現可能)
    ]
    for v in special_ints:
        vecs.append(i32_to_u32(v))

    # 各ビット幅 k=1..31 の境界付近 (2^k - 1, 2^k, 2^k + 1)
    for k in range(1, 31):
        for delta in (-2, -1, 0, 1, 2):
            val = (1 << k) + delta
            if -(1 << 31) <= val <= (1 << 31) - 1:
                vecs.append(i32_to_u32(val))
            nval = -val
            if -(1 << 31) <= nval <= (1 << 31) - 1:
                vecs.append(i32_to_u32(nval))

    # 2. ランダム (ビット長一様分布 + 全32bit一様分布)
    for i in range(num_random):
        if i < num_random // 2:
            bits = rng.randint(1, 31)
            mag = rng.randint(1 << (bits - 1), (1 << bits) - 1)
            val = -mag if rng.randint(0, 1) else mag
            vecs.append(i32_to_u32(val))
        else:
            vecs.append(rng.randint(0, 0xFFFFFFFF))

    return [f"{u:08x}" for u in vecs]


GENERATORS = {
    "fadd": gen_fadd_vectors,
    "fmul": gen_fmul_vectors,
    "fdiv": gen_fdiv_vectors,
    "fsqrt": gen_fsqrt_vectors,
    "ftoi": gen_ftoi_vectors,
    "itof": gen_itof_vectors,
}


def generate_op_vectors(op: str, out_dir: Path, num_random: int = 2000, seed: int = 42) -> Path:
    """指定命令のベクタファイルを生成し、出力パスを返す."""
    if op not in GENERATORS:
        raise ValueError(f"Unknown op: {op}")
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = GENERATORS[op](num_random=num_random, seed=seed)
    out_path = out_dir / f"{op}.hex"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="FPU テストベクタ生成ツール")
    parser.add_argument("ops", nargs="*", default=list(ALL_OPS), choices=list(ALL_OPS) + ["all"])
    parser.add_argument("--num-random", type=int, default=2000, help="ランダムテストケース数 (既定: 2000)")
    parser.add_argument("--seed", type=int, default=42, help="乱数シード (既定: 42)")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data",
        help="出力ディレクトリ",
    )
    args = parser.parse_args()

    target_ops = list(ALL_OPS) if ("all" in args.ops or not args.ops) else args.ops
    for op in target_ops:
        path = generate_op_vectors(op, args.out_dir, num_random=args.num_random, seed=args.seed)
        n_lines = sum(1 for _ in path.open("r", encoding="utf-8"))
        print(f"[gen_vectors] {op:5s}: {n_lines:6d} vectors -> {path}")


if __name__ == "__main__":
    main()
