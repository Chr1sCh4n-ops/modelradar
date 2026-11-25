#!/usr/bin/env python3
"""第六批 Radar：单次失败非零、循环继续、旧图保留，全部隔离运行。"""
from __future__ import annotations

import os
os.environ.setdefault("MODELRADAR_NO_HEADLESS", "1")  # 测试不启动 Chrome/钥匙串
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TestFetchFailures(unittest.TestCase):
    def run_main(self, loop=False):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            data.mkdir()
            chart = data / "chart.html"
            chart.write_text("old chart")
            env = {**os.environ, "HOME": td, "MODELRADAR_SUPPORT": str(root / "support"),
                   "MODELRADAR_DATA_DIR": str(data), "MODELRADAR_REPO_DATA": str(root / "browser")}
            code = r'''
import sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import fetch_model_data as F
import mr_paths
assert str(mr_paths.DATA).startswith(str(mr_paths.Path.home()))
calls = []
def fail(*a, **kw):
    calls.append(1)
    if len(calls) == 1:
        raise RuntimeError("synthetic fetch failure")
    assert mr_paths.CHART_HTML.read_text() == "old chart"
    raise SystemExit(7)
args = ["fetch_model_data.py", "--no-logos"]
if sys.argv[2] == "loop":
    args += ["--loop", "1"]
with patch.object(F, "run", side_effect=fail), patch.object(F.time, "sleep"), patch.object(sys, "argv", args):
    F.main()
'''
            result = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT), "loop" if loop else "once"],
                                    cwd=td, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(chart.read_text(), "old chart")
            self.assertIn("synthetic fetch failure", result.stderr)
            return result

    def test_real_run_status_controls_single_run_exit(self):
        for expected in ("empty", "stale", "degraded", "ok"):
            with self.subTest(state=expected), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                data = root / "data"
                data.mkdir()
                chart = data / "chart.html"
                chart.write_text("old chart")
                env = {**os.environ, "HOME": td, "MODELRADAR_SUPPORT": str(root / "support"),
                       "MODELRADAR_DATA_DIR": str(data), "MODELRADAR_REPO_DATA": str(root / "browser"),
                       "MR_HUB_NOTIFY": ""}
                code = r'''
import json
from pathlib import Path
import sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import fetch_model_data as F
state = sys.argv[2]
config = Path.home() / "watchlist.json"
config.write_text('{"keyword_include": ["qwen"]}')
html = b"<table><thead><tr><th>Model</th><th>Context Window</th><th>Creator</th><th>Artificial Analysis Intelligence Index</th><th>Cost per Task USD</th><th>Output Speed (t/s)</th><th>Latency First Chunk (s)</th><th>Total Response (s)</th></tr></thead><tbody><tr><td>Qwen3.8</td><td>128k</td><td>Alibaba</td><td>50</td><td>1</td><td>100</td><td>0.1</td><td>1</td></tr></tbody></table>"
def fetched(url, **kw):
    if url == F.AA_METHODOLOGY and state in ("ok", "degraded"):
        return b"Artificial Analysis Intelligence Index v5.0"
    if url == F.SOURCES[F.SRC_AA] and state in ("ok", "degraded"):
        return html
    raise OSError("synthetic network failure")
args = ["fetch_model_data.py", "--sources", "aa,openrouter", "--no-logos"]
with patch.object(F, "CFG_PATH", config), patch.object(F, "fetch", fetched), \
     patch.object(F, "notify_fresh"), patch.object(F, "maybe_summarize"), \
     patch.object(F, "probe_palette", return_value={}), patch.object(sys, "argv", args):
    if state == "stale":
        con = F.ensure_db()
        F.save_snapshot(con, F.SRC_AA, 1, True, None, F.parse_aa(html.decode()))
        con.commit()
        con.close()
    if state == "ok":
        args[2] = "aa"
    try:
        F.main()
    finally:
        payload = json.loads(F.LATEST_JSON.read_text())
        assert payload["data_state"] == state, payload
        assert payload["sources"]["aa"].get("stale") or payload["sources"]["aa"]["ok"] == (state != "empty")
        assert F.mr_paths.CHART_HTML.read_text() == "old chart"
'''
                result = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT), expected],
                                        cwd=td, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 2 if expected in ("empty", "stale") else 0,
                                 result.stdout + result.stderr)
                self.assertEqual(chart.read_text(), "old chart")

    def test_once_exception_exits_nonzero_and_keeps_old_chart(self):
        self.assertEqual(self.run_main().returncode, 1)

    def test_loop_reports_exception_then_runs_next_round(self):
        self.assertEqual(self.run_main(loop=True).returncode, 7)


if __name__ == "__main__":
    unittest.main()
