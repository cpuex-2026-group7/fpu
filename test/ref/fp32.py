"""A案 (Fpu.pdf §2.2 方式) に基づく IEEE 754 単精度表現と厳密有理数変換ユーティリティ.

A案の有効浮動小数点数定義:
  - 32bit 全パターンを有効な浮動小数点数として一意に実数値へ対応づける。
  - exp == 0 のビット列はすべて実数値 0 (FTZ: +0, -0, 非正規化数ビット列をすべて 0 扱い)。
  - 1 <= exp <= 255 のビット列はすべて正規化数:
      (-1)^sign * (1 + frac * 2^-23) * 2^(exp - 127)
    (exp == 255 も Inf/NaN ではなく正規化数として扱う)
"""

from __future__ import annotations

from fractions import Fraction
import math

U32_MASK = 0xFFFFFFFF
SIGN_MASK = 0x80000000
EXP_MASK = 0x7F800000
FRAC_MASK = 0x007FFFFF

EXP_BIAS = 127
FRAC_BITS = 23
MANT_IMPLICIT = 1 << FRAC_BITS  # 2^23

# float_spec.pdf で規定される定数
LIMIT_127 = Fraction(1 << 127, 1)  # 2^127
EPS = Fraction(1, 1 << 126)        # 2^-126 (最小正正規化数)
MIN_NORMAL_POS = EPS
MAX_DOMAIN_BITS = 0x7EFFFFFF       # (2 - 2^-23) * 2^126 < 2^127 (exp=253, frac=0x7fffff)

I32_MIN = -(1 << 31)
I32_MAX = (1 << 31) - 1


def unpack_fp32(u: int) -> tuple[int, int, int]:
    """32bit 符号なし整数を (sign, exp, frac) に分解する."""
    u &= U32_MASK
    sign = (u >> 31) & 0x1
    exp = (u >> 23) & 0xFF
    frac = u & FRAC_MASK
    return sign, exp, frac


def pack_fp32(sign: int, exp: int, frac: int) -> int:
    """(sign, exp, frac) から 32bit 符号なし整数を構成する."""
    return ((sign & 0x1) << 31) | ((exp & 0xFF) << 23) | (frac & FRAC_MASK)


def is_valid_fp32(u: int) -> bool:
    """A案では 0 <= u <= 0xFFFFFFFF の全パターンが有効な浮動小数点数."""
    return 0 <= u <= U32_MASK


def is_zero_fp32(u: int) -> bool:
    """A案では exp == 0 の全ビット列が実数値 0."""
    return ((u >> 23) & 0xFF) == 0


def bits_to_fraction(u: int) -> Fraction:
    """A案に従い 32bit ビット列を厳密な有理数 (Fraction) に変換する."""
    sign, exp, frac = unpack_fp32(u)
    if exp == 0:
        return Fraction(0, 1)
    mant = MANT_IMPLICIT | frac
    shift = exp - EXP_BIAS - FRAC_BITS  # exp - 150
    if shift >= 0:
        val = Fraction(mant << shift, 1)
    else:
        val = Fraction(mant, 1 << (-shift))
    return -val if sign else val


def bits_to_float(u: int) -> float:
    """表示・統計用に Python float (倍精度) へ変換する (exp==255 もオーバーフローしない)."""
    sign, exp, frac = unpack_fp32(u)
    if exp == 0:
        return 0.0
    val = math.ldexp(MANT_IMPLICIT | frac, exp - EXP_BIAS - FRAC_BITS)
    return -val if sign else val


def u32_to_i32(u: int) -> int:
    """32bit 符号なし整数を 2 の補数符号付き 32bit 整数へ変換する."""
    u &= U32_MASK
    return u - (1 << 32) if (u & SIGN_MASK) else u


def i32_to_u32(v: int) -> int:
    """符号付き 32bit 整数を 32bit 符号なしビット表現へ変換する."""
    return v & U32_MASK


def fp32_to_ordered_index(u: int) -> int:
    """ULP 差計算のため、A案の単精度浮動小数点数を単調増加な整数インデックスに写像する.

    - exp == 0 (すべて 0.0) -> 0
    - 正の正規化数 (exp=1..255, frac=0..2^23-1) -> +( (exp-1)*2^23 + frac + 1 )
    - 負の正規化数 -> -( (exp-1)*2^23 + frac + 1 )
    注: +0 から最小正正規化数 2^-126 (0x00800000) への差は +1 となる。
    """
    sign, exp, frac = unpack_fp32(u)
    if exp == 0:
        return 0
    mag_idx = ((exp - 1) << FRAC_BITS) + frac + 1
    return -mag_idx if sign else mag_idx


def fraction_to_rne_fp32(x: Fraction) -> int:
    """厳密有理数 x を A案の有効浮動小数点数へ最近接偶数丸め (RNE) で変換する.

    レポート上の期待値および ULP 差計算のための参照関数 (合否判定には使わない)。
    |x| < 2^-127 は 0 に丸め、|x| が exp=255 最大値を超える場合は exp=255 最大値に飽和する。
    """
    if x == 0:
        return 0x00000000
    sign = 1 if x < 0 else 0
    ax = -x if sign else x

    # 最小正正規化数 2^-126 の半分 (2^-127) 未満・またはちょうど 2^-127 (偶数 0 へ)
    half_min = Fraction(1, 1 << 127)
    if ax <= half_min:
        return 0x00000000

    # floor(log2(ax)) を厳密に求める
    num, den = ax.numerator, ax.denominator
    e = num.bit_length() - den.bit_length()
    # ax < 2^e なら e を調整
    if e >= 0:
        if num < (den << e):
            e -= 1
    else:
        if (num << (-e)) < den:
            e -= 1

    # アンダーフロー境界 (2^-127 < ax < 2^-126) は最小正規化数 2^-126 へ丸め
    if e < -126:
        return pack_fp32(sign, 1, 0)

    # scaled = ax / 2^(e - 23) は [2^23, 2^24) の範囲
    shift = e - FRAC_BITS
    if shift >= 0:
        # scaled = num / (den << shift)
        s_num = num
        s_den = den << shift
    else:
        s_num = num << (-shift)
        s_den = den

    q, r = divmod(s_num, s_den)
    twice_r = r << 1
    if twice_r > s_den or (twice_r == s_den and (q & 1)):
        q += 1
    if q == (1 << (FRAC_BITS + 1)):
        q >>= 1
        e += 1

    exp = e + EXP_BIAS
    if exp > 255:
        return pack_fp32(sign, 255, FRAC_MASK)
    return pack_fp32(sign, exp, q & FRAC_MASK)


def ulp_diff(actual_u: int, expected_u: int) -> int:
    """A案インデックスに基づく符号付き ULP 差 (actual - expected) を返す."""
    return fp32_to_ordered_index(actual_u) - fp32_to_ordered_index(expected_u)


def format_fp32(u: int) -> str:
    """デバッグ表示用に 32bit 浮動小数点数を整形する."""
    sign, exp, frac = unpack_fp32(u)
    val = bits_to_float(u)
    return f"0x{u:08x} (s={sign} e={exp:3d} f=0x{frac:06x}, val={val:+.8e})"


def parse_hex_u32(token: str) -> int | None:
    """シミュレーション出力の 8 桁 HEX 文字列を int に変換する。x/z 等を含めば None."""
    s = token.strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if not s:
        return None
    for ch in s:
        if ch not in "0123456789abcdef":
            return None
    val = int(s, 16)
    if not (0 <= val <= U32_MASK):
        return None
    return val
