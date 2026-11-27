#!/usr/bin/env python3
"""FPU 統合検証ランナー (ベクタ生成 -> iverilog/vvp 実行 -> float_spec.pdf 厳密検証).

使用例:
  # 検証基盤自身のセルフテスト (RTL 不要)
  python3 test/run_sim.py --self-test

  # 単一モジュールのシミュレーション＆精度検証 (パイプライン組は --latency を指定)
  python3 test/run_sim.py fadd --latency 3
  python3 test/run_sim.py fmul --latency 3
  python3 test/run_sim.py fdiv
  python3 test/run_sim.py fsqrt

  # テストベクタのみ生成 (Vivado xsim 向け)
  python3 test/run_sim.py --gen-only all

  # 外部シミュレータ (Vivado xsim 等) が出力した HEX ログの精度検証のみ実行
  python3 test/run_sim.py --check-only fadd --vec test/vectors/data/fadd.hex --out out.hex
"""

from __future__ import annotations

import argparse
from fractions import Fraction
import math
from pathlib import Path
import subprocess
import sys

# test/ ディレクトリを sys.path 先頭に追加 (Python 標準ライブラリ `test` との衝突を回避)
TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from ref.fp32 import (  # noqa: E402
    bits_to_fraction,
    fraction_to_rne_fp32,
    i32_to_u32,
    parse_hex_u32,
    u32_to_i32,
)
from ref.spec_checker import (  # noqa: E402
    SummaryReport,
    check_fadd,
    check_fdiv,
    check_fmul,
    check_fsqrt,
    check_ftoi,
    check_itof,
)
from vectors.gen_vectors import ALL_OPS, generate_op_vectors  # noqa: E402

PIPE_OPS = {"fadd", "fmul", "ftoi", "itof"}
MULTI_OPS = {"fdiv", "fsqrt"}


def check_hex_files(op: str, vec_path: Path, out_path: Path) -> SummaryReport:
    """ベクタファイルとシミュレーション出力 HEX ファイルを突き合わせて厳密判定する."""
    vec_lines = [ln.strip() for ln in vec_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    out_lines = [ln.strip() for ln in out_path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    report = SummaryReport(op=op)
    if len(out_lines) != len(vec_lines):
        report.failures.append(
            f"[COUNT_MISMATCH] vector count ({len(vec_lines)}) != output count ({len(out_lines)})"
        )
        report.failed += 1

    n = min(len(vec_lines), len(out_lines))
    for i in range(n):
        parts = vec_lines[i].split()
        out_tok = out_lines[i]
        res_u = parse_hex_u32(out_tok)

        if op == "fadd":
            aux = int(parts[0], 16) & 1
            a_u = int(parts[1], 16)
            b_u = int(parts[2], 16)
            res = check_fadd(a_u, b_u, res_u, is_sub=bool(aux))
            desc = f"{'fsub' if aux else 'fadd'}(0x{a_u:08x}, 0x{b_u:08x})"
        elif op == "fmul":
            a_u = int(parts[0], 16)
            b_u = int(parts[1], 16)
            res = check_fmul(a_u, b_u, res_u)
            desc = f"fmul(0x{a_u:08x}, 0x{b_u:08x})"
        elif op == "fdiv":
            a_u = int(parts[0], 16)
            b_u = int(parts[1], 16)
            res = check_fdiv(a_u, b_u, res_u)
            desc = f"fdiv(0x{a_u:08x}, 0x{b_u:08x})"
        elif op == "fsqrt":
            a_u = int(parts[0], 16)
            res = check_fsqrt(a_u, res_u)
            desc = f"fsqrt(0x{a_u:08x})"
        elif op == "ftoi":
            a_u = int(parts[0], 16)
            res = check_ftoi(a_u, res_u)
            desc = f"ftoi(0x{a_u:08x})"
        elif op == "itof":
            i_u = int(parts[0], 16)
            res = check_itof(i_u, res_u)
            desc = f"itof(0x{i_u:08x} / {u32_to_i32(i_u)})"
        else:
            raise ValueError(f"Unknown op: {op}")

        report.record(i, desc, out_tok, res)

    return report


def _rel_str(path: Path) -> str:
    """REPO_ROOT 配下なら相対パス (ASCII) を返す (日本語を含む親パス対策)."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT.resolve()))
    except ValueError:
        return str(path)


def run_iverilog_sim(
    op: str,
    vec_path: Path,
    latency: int,
    timeout: int,
    wave: bool,
    skip_proto: bool,
) -> tuple[bool, Path]:
    """src/<op>/*.sv と test/tb/tb_<op>.sv を iverilog + vvp で実行する."""
    src_dir = REPO_ROOT / "src" / op
    sv_files = sorted(src_dir.rglob("*.sv"))
    if not sv_files:
        print(f"[ERROR] No .sv files found under {src_dir}")
        return False, Path()

    tb_file = REPO_ROOT / "test" / "tb" / f"tb_{op}.sv"
    work_dir = REPO_ROOT / "test" / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    vvp_path = work_dir / f"sim_{op}.vvp"
    out_path = work_dir / f"{op}_out.hex"
    vcd_path = work_dir / f"{op}.vcd"

    compile_cmd = [
        "iverilog",
        "-g2012",
        "-Wall",
        "-o",
        _rel_str(vvp_path),
        *[_rel_str(p) for p in sv_files],
        _rel_str(tb_file),
    ]
    print(f"[compile] {' '.join(compile_cmd)}")
    comp = subprocess.run(
        compile_cmd, cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if comp.stdout:
        print(comp.stdout, end="")
    if comp.stderr:
        print(comp.stderr, end="", file=sys.stderr)
    if comp.returncode != 0:
        print(f"[ERROR] iverilog compile failed for {op} (rc={comp.returncode})")
        return False, out_path

    run_cmd = [
        "vvp",
        _rel_str(vvp_path),
        f"+VEC={_rel_str(vec_path)}",
        f"+OUT={_rel_str(out_path)}",
    ]
    if op in PIPE_OPS:
        run_cmd.append(f"+LATENCY={latency}")
    else:
        run_cmd.append(f"+TIMEOUT={timeout}")
        if skip_proto:
            run_cmd.append("+SKIP_PROTO=1")
    if wave:
        run_cmd.append(f"+VCD={_rel_str(vcd_path)}")

    print(f"[sim]     {' '.join(run_cmd)}")
    sim = subprocess.run(
        run_cmd, cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if sim.stdout:
        print(sim.stdout, end="")
    if sim.stderr:
        print(sim.stderr, end="", file=sys.stderr)
    if sim.returncode != 0 or "[PROTO_FAIL]" in sim.stdout or "[TB_ERROR]" in sim.stdout:
        print(f"[ERROR] Simulation/protocol failed for {op} (rc={sim.returncode})")
        return False, out_path

    return True, out_path


def run_self_test() -> int:
    """fp32.py / spec_checker.py / gen_vectors.py の動作セルフテスト."""
    print("=== 検証基盤セルフテスト (A案 / float_spec.pdf 厳密チェッカー) ===")

    # 1. A案の数値変換テスト (exp=0 は全て 0、exp=255 は正規化数)
    assert bits_to_fraction(0x00000000) == 0
    assert bits_to_fraction(0x80000000) == 0
    assert bits_to_fraction(0x00000001) == 0
    assert bits_to_fraction(0x007FFFFF) == 0
    assert bits_to_fraction(0x00800000) == Fraction(1, 1 << 126)
    assert bits_to_fraction(0x3F800000) == Fraction(1, 1)
    assert bits_to_fraction(0xBF800000) == Fraction(-1, 1)
    assert bits_to_fraction(0x7F800000) == Fraction(1 << 128, 1)  # exp=255 正規化数扱い

    # 2. fadd/fsub 境界テスト (ちょうど bound なら FAIL, bound 未満なら PASS)
    # 1.0 + 1.0 = 2.0 (bound = 2.0 * 2^-23 = 2^-22 = 1 ulp at 2.0? 2.0の1ulpは2^-22)
    # exact=2.0 (0x40000000), 1ulpずれ (0x40000001) は誤差 2^-22 == bound なので厳密不等号 `<` で FAIL になるべき!
    assert check_fadd(0x3F800000, 0x3F800000, 0x40000000, is_sub=False).passed
    assert not check_fadd(0x3F800000, 0x3F800000, 0x40000001, is_sub=False).passed
    # 1.5 + 1.5 = 3.0 (0x40400000): bound = 3 * 2^-23 > 2^-22 (=1ulp), よって ±1ulp は PASS, ±2ulp は FAIL
    assert check_fadd(0x3FC00000, 0x3FC00000, 0x40400001, is_sub=False).passed
    assert not check_fadd(0x3FC00000, 0x3FC00000, 0x40400002, is_sub=False).passed
    # X/Z は FAIL
    assert not check_fadd(0x3F800000, 0x3F800000, None, is_sub=False).passed

    # 3. fmul 境界テスト (bound = max(|AB|*2^-22, eps))
    # 1.0 * 1.0 = 1.0 (1ulp = 2^-23, bound = 2^-22 = 2ulp): ±1ulp は PASS, ±2ulp は FAIL
    assert check_fmul(0x3F800000, 0x3F800000, 0x3F800001).passed
    assert not check_fmul(0x3F800000, 0x3F800000, 0x3F800002).passed

    # 4. fdiv 境界テスト (bound = max(|A/B|*2^-20, eps))
    # 1.0 / 1.0 = 1.0 (1ulp = 2^-23, bound = 2^-20 = 8ulp): +7ulp は PASS, +8ulp は FAIL
    assert check_fdiv(0x3F800000, 0x3F800000, 0x3F800007).passed
    assert not check_fdiv(0x3F800000, 0x3F800000, 0x3F800008).passed
    # Fpu.pdf §5.1 ケース: 2^126 / 2^126 = 1.0 -> 0.0 を返すと FAIL
    assert check_fdiv(0x7E800000, 0x7E800000, 0x3F800000).passed
    assert not check_fdiv(0x7E800000, 0x7E800000, 0x00000000).passed

    # 5. fsqrt 境界テスト (bound = max(sqrt(A)*2^-20, eps))
    # sqrt(1.0) = 1.0: +7ulp PASS, +8ulp FAIL; sqrt(0)=0 PASS, sqrt(0)=2^-126 FAIL
    assert check_fsqrt(0x3F800000, 0x3F800007).passed
    assert not check_fsqrt(0x3F800000, 0x3F800008).passed
    assert check_fsqrt(0x00000000, 0x00000000).passed
    assert check_fsqrt(0x80000000, 0x00000000).passed
    assert not check_fsqrt(0x00000000, 0x00800000).passed

    # 6. ftoi 境界テスト (.5 の tie は上下どちらも PASS、それ以外は最近接のみ PASS)
    assert check_ftoi(0x3F000000, i32_to_u32(0)).passed       # 0.5 -> 0 PASS
    assert check_ftoi(0x3F000000, i32_to_u32(1)).passed       # 0.5 -> 1 PASS
    assert not check_ftoi(0x3F000000, i32_to_u32(2)).passed   # 0.5 -> 2 FAIL
    assert check_ftoi(0xBF000000, i32_to_u32(0)).passed       # -0.5 -> 0 PASS
    assert check_ftoi(0xBF000000, i32_to_u32(-1)).passed      # -0.5 -> -1 PASS
    assert check_ftoi(0x3F000001, i32_to_u32(1)).passed       # 0.5+eps -> 1 PASS
    assert not check_ftoi(0x3F000001, i32_to_u32(0)).passed   # 0.5+eps -> 0 FAIL

    # 7. itof 境界テスト (|I| <= 2^24 exact, 2^24+1 は tie で 2^24 と 2^24+2 の両方 PASS, INT_MIN exact)
    assert check_itof(i32_to_u32(0), 0x00000000).passed
    assert check_itof(i32_to_u32(-(1 << 31)), 0xCF000000).passed  # -2^31 exact
    assert check_itof(i32_to_u32((1 << 24) + 1), 0x4B800000).passed  # 2^24 (tie)
    assert check_itof(i32_to_u32((1 << 24) + 1), 0x4B800001).passed  # 2^24+2 (tie)
    assert not check_itof(i32_to_u32((1 << 24) + 1), 0x4B800002).passed

    # 8. 全6命令の生成ベクタに対し理想 RNE 出力を食わせて全件 PASS することを確認
    tmp_dir = REPO_ROOT / "test" / "work" / "selftest"
    for op in ALL_OPS:
        vec_p = generate_op_vectors(op, tmp_dir, num_random=200, seed=123)
        vec_lines = [ln.strip() for ln in vec_p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        out_lines = []
        for ln in vec_lines:
            p = ln.split()
            if op == "fadd":
                aux, a, b = int(p[0], 16), int(p[1], 16), int(p[2], 16)
                exact = bits_to_fraction(a) - bits_to_fraction(b) if aux else bits_to_fraction(a) + bits_to_fraction(b)
                out_u = fraction_to_rne_fp32(exact)
            elif op == "fmul":
                a, b = int(p[0], 16), int(p[1], 16)
                out_u = fraction_to_rne_fp32(bits_to_fraction(a) * bits_to_fraction(b))
            elif op == "fdiv":
                a, b = int(p[0], 16), int(p[1], 16)
                out_u = fraction_to_rne_fp32(bits_to_fraction(a) / bits_to_fraction(b))
            elif op == "fsqrt":
                a = int(p[0], 16)
                # セルフテストでは float64 sqrt から近似して RNE (単精度24bitに対し倍精度53bitは十分)
                val = float(bits_to_fraction(a))
                out_u = fraction_to_rne_fp32(Fraction(math.sqrt(val)))
            elif op == "ftoi":
                a = bits_to_fraction(int(p[0], 16))
                out_u = i32_to_u32(round(a))
            elif op == "itof":
                i_val = u32_to_i32(int(p[0], 16))
                out_u = fraction_to_rne_fp32(Fraction(i_val, 1))
            out_lines.append(f"{out_u:08x}")
        out_p = tmp_dir / f"{op}_ideal.hex"
        out_p.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        rep = check_hex_files(op, vec_p, out_p)
        print(f"  [self-test] {op:5s}: checked={rep.checked:5d}, skipped={rep.skipped_out_of_domain:3d}, failed={rep.failed}")
        assert rep.failed == 0 and rep.checked > 0, rep.format_text()

    print("=== セルフテスト全件 PASS ===")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="FPU 精度検証＆シミュレーション統合ランナー")
    parser.add_argument(
        "op",
        nargs="?",
        choices=list(ALL_OPS) + ["all"],
        help="対象命令 (fadd, fmul, fdiv, fsqrt, ftoi, itof, all)",
    )
    parser.add_argument("--self-test", action="store_true", help="検証基盤自身のセルフテストを実行")
    parser.add_argument("--gen-only", action="store_true", help="テストベクタ生成のみ実行")
    parser.add_argument("--check-only", action="store_true", help="外部シミュレータ出力の精度検証のみ実行")
    parser.add_argument("--vec", type=Path, help="--check-only 用の入力ベクタファイル")
    parser.add_argument("--out", type=Path, help="--check-only 用の出力 HEX ファイル")
    parser.add_argument("--latency", type=int, default=1, help="パイプライン組 (fadd/fmul/ftoi/itof) のレイテンシ段数 (既定: 1)")
    parser.add_argument("--timeout", type=int, default=1000, help="マルチサイクル組 (fdiv/fsqrt) のタイムアウトサイクル数")
    parser.add_argument("--num-random", type=int, default=2000, help="ランダムベクタ生成数 (既定: 2000)")
    parser.add_argument("--seed", type=int, default=42, help="乱数シード (既定: 42)")
    parser.add_argument("--reuse-vectors", action="store_true", help="既存の test/vectors/data/<op>.hex を再利用")
    parser.add_argument("--wave", action="store_true", help="波形ファイル (test/work/<op>.vcd) を出力")
    parser.add_argument("--skip-proto", action="store_true", help="fdiv/fsqrt の abort_ff プロトコルテストをスキップ")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    if not args.op:
        parser.error("対象命令 (fadd/fmul/fdiv/fsqrt/ftoi/itof/all) または --self-test を指定してください")

    if args.check_only:
        if args.op == "all" or not args.vec or not args.out:
            parser.error("--check-only では単一命令と --vec, --out の指定が必要です")
        rep = check_hex_files(args.op, args.vec, args.out)
        print(rep.format_text())
        return 0 if (rep.failed == 0 and rep.checked > 0) else 1

    target_ops = list(ALL_OPS) if args.op == "all" else [args.op]
    vec_dir = REPO_ROOT / "test" / "vectors" / "data"

    if args.gen_only:
        for op in target_ops:
            p = generate_op_vectors(op, vec_dir, num_random=args.num_random, seed=args.seed)
            n = sum(1 for _ in p.open("r", encoding="utf-8"))
            print(f"[gen] {op:5s}: {n:6d} vectors -> {p}")
        return 0

    overall_ok = True
    for op in target_ops:
        vec_path = vec_dir / f"{op}.hex"
        if not args.reuse_vectors or not vec_path.exists():
            vec_path = generate_op_vectors(op, vec_dir, num_random=args.num_random, seed=args.seed)

        sim_ok, out_path = run_iverilog_sim(
            op=op,
            vec_path=vec_path,
            latency=args.latency,
            timeout=args.timeout,
            wave=args.wave,
            skip_proto=args.skip_proto,
        )
        if not sim_ok:
            overall_ok = False
            continue

        rep = check_hex_files(op, vec_path, out_path)
        print(rep.format_text())
        if rep.failed != 0 or rep.checked == 0:
            overall_ok = False

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
