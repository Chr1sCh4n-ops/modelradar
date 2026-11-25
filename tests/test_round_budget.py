"""2026-10-29 用户要求：超时不能阻断已成功数据发布。"""
import gzip
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fetch_model_data as F
os.environ.setdefault("MODELRADAR_NO_HEADLESS", "1")  # 测试不启动 Chrome/钥匙串


class SlowHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/compressed")
            self.end_headers()
            return
        if self.path == "/compressed":
            body = gzip.compress(b"model data")
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/headers":
            time.sleep(1)
        self.send_response(200)
        self.send_header("Content-Length", "100")
        self.end_headers()
        try:
            for _ in range(100):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.03)
        except (BrokenPipeError, ConnectionResetError):
            pass


class RoundBudget(unittest.TestCase):
    def test_real_headers_and_dripping_body_have_a_hard_deadline(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(F.fetch(f"http://127.0.0.1:{server.server_port}/redirect",
                                     retries=0, budget=1, total_budget=1), b"model data")
            for path in ("/headers", "/body"):
                with self.subTest(path=path):
                    started = time.monotonic()
                    with self.assertRaises(RuntimeError):
                        F.fetch(f"http://127.0.0.1:{server.server_port}{path}",
                                retries=0, budget=0.15, total_budget=0.15)
                    self.assertLess(time.monotonic() - started, 0.8)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_expired_round_starts_no_process_and_restores_deadline(self):
        with patch.object(F.subprocess, "run") as transport:
            with F.network_budget(0):
                with self.assertRaisesRegex(RuntimeError, "网络总时长上限"):
                    F.fetch("https://example.invalid")
            transport.assert_not_called()
        self.assertEqual(F._network_deadline.get(), float("inf"))

    def test_source_deadline_cannot_extend_round_or_start_fallback(self):
        """重试只吃同一份来源预算：每次 curl 的超时都不能超过剩余截止。"""
        def transport(command, **kwargs):
            self.assertLessEqual(kwargs["timeout"], 0.2)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        started = time.monotonic()
        with F.network_budget(0.2), F.network_budget(60), patch.object(F.subprocess, "run", side_effect=transport) as process:
            with self.assertRaises(RuntimeError):
                F.fetch_task_manifest("https://example.invalid")
            self.assertLessEqual(process.call_count, 1 + F.FETCH_RESUME_IDLE_SLICES)
            self.assertLess(time.monotonic() - started, 1)

    def test_partial_success_publishes_and_stale_does_not_add_history(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as td:
            env = {**os.environ, "HOME": td, "MODELRADAR_SUPPORT": td + "/support",
                   "MODELRADAR_DATA_DIR": td + "/data", "MODELRADAR_REPO_DATA": td + "/browser",
                   "MR_HUB_NOTIFY": "", "MODELRADAR_NO_SUMMARY": "1"}
            code = r'''
import json, sqlite3, sys, time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import fetch_model_data as F
html=b'<table><thead><tr><th>Model</th><th>Context Window</th><th>Creator</th><th>Artificial Analysis Intelligence Index</th><th>Cost per Task USD</th><th>Output Speed (t/s)</th><th>Latency First Chunk (s)</th><th>Total Response (s)</th></tr></thead><tbody><tr><td>Qwen3.8</td><td>128k</td><td>Alibaba</td><td>50</td><td>1</td><td>100</td><td>0.1</td><td>1</td></tr></tbody></table>'
cfg=Path.home()/'watchlist.json'
cfg.write_text('{"keyword_include":["qwen"]}')
def fetched(url, **kw):
    if url==F.SOURCES[F.SRC_AA]: return html
    if url==F.AA_METHODOLOGY: return b'Artificial Analysis Intelligence Index v5.0'
    return real_fetch(url, retries=0, budget=0.05, total_budget=0.05)
real_fetch=F.fetch
with patch.object(F,'CFG_PATH',cfg), patch.object(F,'notify_fresh'), patch.object(F,'maybe_summarize'), patch.object(F,'probe_palette',return_value={}):
    with F.network_budget(0):
        empty=F.run([F.SRC_AA,F.SRC_OR], False)
    assert empty['data_state']=='empty',empty
    F.LATEST_JSON.parent.mkdir(parents=True,exist_ok=True)
    F.LATEST_JSON.write_text('{"generated_at":"old"}')
    started=time.monotonic()
    with patch.dict(F.SOURCES,{F.SRC_OR:sys.argv[2]}), patch.object(F,'fetch',fetched), patch.object(F,'guard_logos',side_effect=OSError('synthetic logo failure')), F.network_budget(0.2):
        p=F.run([F.SRC_AA,F.SRC_OR], True)
    assert time.monotonic()-started<1
    assert p['data_state']=='degraded',p
    assert p['sources'][F.SRC_OR]['ok'] is False
    assert json.loads(F.LATEST_JSON.read_text())['generated_at']!='old'
    con=sqlite3.connect(F.DB_PATH)
    before=con.execute('SELECT COUNT(*) FROM series').fetchone()[0]
    assert before>0
    assert con.execute("SELECT COUNT(*) FROM snapshots WHERE source='aa' AND ok=1").fetchone()[0]>0
    con.close()
    with F.network_budget(0):
        p=F.run([F.SRC_AA,F.SRC_OR], False)
    assert p['data_state']=='stale',p
    assert p['sources'][F.SRC_AA]['stale'] is True
    con=sqlite3.connect(F.DB_PATH)
    assert con.execute('SELECT COUNT(*) FROM series').fetchone()[0]==before
    con.close()
'''
            result = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT),
                                     f"http://127.0.0.1:{server.server_port}/headers"],
                                    cwd=td, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class BudgetShape(unittest.TestCase):
    """2026-11-08 用户要求：按 AA 慢滴实测放宽预算，但外壳必须仍大于整轮。

    实测（2026-11-08）：六个源正文合计约 3.85 MB，AA 榜单单页 1,763,489 字节；
    本网络续传约 22 KB/s。原 180/60 的配置下，单源 60 秒只能拿到 1.34 MB，
    AA 主表一份都收不满。放宽到 240/100 后仍要守住两条不变式，否则会退化成
    "外壳先掐断抓取"，那正是 2026-10-29 用户报的那个 bug。
    """

    def test_shell_timeout_exceeds_round_budget(self):
        # 只取「立即抓取」那一处（label: "crawl"）：同文件还有 render 的 90s，别抓错。
        source = (ROOT / "widget" / "ChartWidget.swift").read_text(encoding="utf-8")
        crawl = source[source.index('label: "crawl"'):]
        shell = int(re.search(r"timeoutSec: (\d+)", crawl).group(1))
        self.assertGreater(shell, F.ROUND_NETWORK_BUDGET,
                           "外壳必须大于整轮预算，否则抓取还没跑完就被终止")

    def test_round_budget_covers_every_source(self):
        self.assertGreater(F.ROUND_NETWORK_BUDGET, F.SOURCE_NETWORK_BUDGET)
        self.assertLess(F.SOURCE_NETWORK_BUDGET, F.ROUND_NETWORK_BUDGET / 2,
                        "单源预算要小于整轮一半，否则一个挂掉的源能吃掉整轮的大半")

    def test_round_budget_fits_largest_measured_source(self):
        """按实测 22 KB/s、AA 榜单 1,763,489 字节推算单源最少需要的秒数。"""
        needed = 1763489 / (22 * 1024)
        self.assertGreater(F.SOURCE_NETWORK_BUDGET, needed,
                           f"单源预算 {F.SOURCE_NETWORK_BUDGET}s 不足以在实测速率下收完 AA 主表"
                           f"（约需 {needed:.0f}s）")

    def test_task_manifest_cap_does_not_undercut_source_budget(self):
        """2026-11-08 实测：aa_times 要抓两次 1.46 MB，45 秒的单独封顶会先掐断。"""
        self.assertGreaterEqual(F.TASK_FETCH_MAX_TIME, F.SOURCE_NETWORK_BUDGET,
                                "任务耗时的单次封顶不能小于单源预算，否则先于共享截止生效")


class TaskTimesErrorSurface(unittest.TestCase):
    """下载失败不能被伪装成"数据包未唯一识别"。

    2026-11-08 实测：慢网络下 manifest 抓取被截断，parse_task_times 走到
    len(candidates) != 1 就报"未唯一识别"，日志里完全看不到是网络问题。
    """

    PAGE = ('<script>self.__next_f.push([1,"{\\"manifest\\":{\\"path\\":\\"/data/abc.txt\\",'
            '\\"key\\":\\"' + 'a' * 64 + '\\"}}"])</script>')

    def test_download_error_is_reported_as_download_error(self):
        with unittest.mock.patch.object(F, "_decode_manifest",
                                        side_effect=RuntimeError("抓取失败 x: 首轮直传 45s 未完成")):
            with self.assertRaises(ValueError) as ctx:
                F.parse_task_times(self.PAGE)
        self.assertIn("下载失败", str(ctx.exception))
        self.assertIn("45s 未完成", str(ctx.exception))

    def test_shape_error_still_reported_as_shape_error(self):
        # 下载成功但没有可取候选（空 models）：这是上游形状问题，不该报成网络错误。
        with unittest.mock.patch.object(F, "_decode_manifest", return_value={"models": []}):
            with self.assertRaises(ValueError) as ctx:
                F.parse_task_times(self.PAGE)
        self.assertIn("未唯一识别", str(ctx.exception))
        self.assertNotIn("下载失败", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
