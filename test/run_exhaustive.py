#!/usr/bin/env python3
"""1入力 FPU 命令 (itof / ftoi / fsqrt) 向け Verilator 全数テストランナー.

float_spec.pdf / A案の仕様定義域内の全ビットパターン (21億〜42.9億通り) を
Verilator + C++ マルチスレッドで全数検証する。

使用例:
  python3 test/run_exhaustive.py itof --latency 2
  python3 test/run_exhaustive.py ftoi --latency 2
  python3 test/run_exhaustive.py fsqrt
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parent
EXHAUSTIVE_DIR = TEST_DIR / "exhaustive"
WORK_DIR = TEST_DIR / "work" / "verilator"

SUPPORTED_OPS = ("itof", "ftoi", "fsqrt")
PIPE_OPS = {"itof", "ftoi"}


def _rel_str(path: Path) -> str:
    """REPO_ROOT 配下なら相対パス (ASCII) を返す (日本語を含む親パス対策)."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT.resolve()))
    except ValueError:
        return str(path)


def build_and_run(op: str, latency: int, timeout: int, threads: int, force_rebuild: bool) -> int:
    if shutil.which("verilator") is None:
        print("[ERROR] verilator が見つかりません。`brew install verilator` でインストールしてください。", file=sys.stderr)
        return 1

    src_dir = REPO_ROOT / "src" / op
    sv_files = sorted(src_dir.rglob("*.sv"))
    if not sv_files:
        print(f"[ERROR] {src_dir} 配下に .sv ファイルがありません。", file=sys.stderr)
        return 1

    cpp_main = EXHAUSTIVE_DIR / f"sim_{op}.cpp"
    obj_dir = WORK_DIR / op
    if force_rebuild and obj_dir.exists():
        shutil.rmtree(obj_dir)
    obj_dir.mkdir(parents=True, exist_ok=True)

    exe_path = obj_dir / f"V{op}"

    # Verilator ビルド (常に -O3 + pthread, 各スレッドで独立 DUT を実行するため --threads 1)
    verilate_cmd = [
        "verilator",
        "--cc",
        "--exe",
        "--build",
        "-O3",
        "--x-assign",
        "fast",
        "-j",
        str(max(1, threads)),
        "--top-module",
        op,
        "--Mdir",
        _rel_str(obj_dir),
        "-Wno-fatal",
        "-MAKEFLAGS",
        "OPT_FAST=-O3",
        "-CFLAGS",
        f"-O3 -std=c++17 -pthread -I{os.path.relpath(EXHAUSTIVE_DIR, obj_dir)}",
        "-LDFLAGS",
        "-pthread",
        *[_rel_str(p) for p in sv_files],
        _rel_str(cpp_main),
    ]

    print(f"[build] {' '.join(verilate_cmd)}")
    build = subprocess.run(verilate_cmd, cwd=REPO_ROOT)
    if build.returncode != 0 or not exe_path.exists():
        print(f"[ERROR] Verilator ビルドに失敗しました (rc={build.returncode})", file=sys.stderr)
        return 1

    run_cmd = [_rel_str(exe_path), "--threads", str(threads)]
    if op in PIPE_OPS:
        run_cmd.extend(["--latency", str(latency)])
    else:
        run_cmd.extend(["--timeout", str(timeout)])

    print(f"[run]   {' '.join(run_cmd)}")
    res = subprocess.run(run_cmd, cwd=REPO_ROOT)
    return res.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="1入力 FPU 命令 (itof/ftoi/fsqrt) Verilator 全数テストランナー")
    parser.add_argument("op", choices=SUPPORTED_OPS, help="対象命令 (itof, ftoi, fsqrt)")
    parser.add_argument("--latency", type=int, default=2, help="itof / ftoi のパイプラインレイテンシ段数 (既定: 2)")
    parser.add_argument("--timeout", type=int, default=1000, help="fsqrt のタイムアウトサイクル数 (既定: 1000)")
    parser.add_argument(
        "--threads",
        type=int,
        default=os.cpu_count() or 4,
        help=f"並列スレッド数 (既定: {os.cpu_count() or 4})",
    )
    parser.add_argument("--rebuild", action="store_true", help="ビルドキャッシュを削除して再ビルド")
    args = parser.parse_args()

    return build_and_run(
        op=args.op,
        latency=args.latency,
        timeout=args.timeout,
        threads=args.threads,
        force_rebuild=args.rebuild,
    )


if __name__ == "__main__":
    raise SystemExit(main())
