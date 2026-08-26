from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
JS_TESTS = ROOT / "frontend_tests" / "js"


def test_frontend_js_suites() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("当前环境无 node；前端交互套件需在含 node 的环境执行")
    test_files = sorted(str(path) for path in JS_TESTS.glob("*.test.mjs"))
    assert test_files, "frontend_tests/js 下应存在 *.test.mjs 套件"
    result = subprocess.run(
        [node, "--test", *test_files],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"node --test 失败：\n{result.stdout}\n{result.stderr}"
        )
