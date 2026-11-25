"""2026-11-04 用户要求：修 AA 抓取失败，但不得拖累健康源，也不得无限重试。

对应实测：AA（Vercel 边缘）在本网络上 TLS 握手间歇卡住、正文 0.5～3 KB/s 挤牙膏。
原 `fetch()` 一次长跑被 `--max-time` 掐掉，已收字节随管道丢弃，重试等于从 0 再来。
本用例锁定新行为：第一段与改动前一致；失败后才转短切片 + Range 续传；到点即止。
"""
import gzip
from pathlib import Path
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fetch_model_data as F


PAYLOAD = b"".join(b"row-%04d;" % index for index in range(1200))


class RangeHandler(BaseHTTPRequestHandler):
    """支持 Range 的分片源；`/flaky` 每次只给一小段就断开，模拟挤牙膏。"""

    def log_message(self, *args):
        pass

    def _range_start(self):
        header = self.headers.get("Range", "")
        if header.startswith("bytes=") and header[6:].split("-")[0].isdigit():
            return int(header[6:].split("-")[0])
        return None

    def do_GET(self):
        start = self._range_start()
        if self.path == "/never":
            time.sleep(30)
            return
        if self.path == "/norange":
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
            return
        if self.path == "/gzip":
            body = gzip.compress(PAYLOAD)
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/flaky":
            chunk = PAYLOAD[start or 0:(start or 0) + 4000]
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            if start:
                self.send_header("Content-Range",
                                 f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
            self.end_headers()
            self.wfile.write(chunk)
            self.wfile.flush()
            self.close_connection = True
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)


class ResumeFetch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_healthy_source_returns_in_one_pass(self):
        self.assertEqual(F.fetch(f"{self.base}/plain"), PAYLOAD)

    def test_gzip_source_is_decompressed(self):
        self.assertEqual(F.fetch(f"{self.base}/gzip"), PAYLOAD)

    def test_range_source_resumes_until_complete(self):
        self.assertEqual(F.fetch(f"{self.base}/flaky", retries=30, budget=3, total_budget=60),
                         PAYLOAD)

    def test_source_ignoring_range_resets_instead_of_corrupting(self):
        self.assertEqual(F.fetch(f"{self.base}/norange", retries=4, budget=1, total_budget=20),
                         PAYLOAD)

    def test_deadline_is_honored_and_attempts_stay_bounded(self):
        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            F.fetch(f"{self.base}/never", retries=50, budget=1, total_budget=2)
        self.assertLess(time.monotonic() - started, 9)

    def test_part_file_is_cleaned_up(self):
        before = set(Path("/tmp").glob("modelradar-fetch-*.part"))
        F.fetch(f"{self.base}/plain")
        self.assertEqual(set(Path("/tmp").glob("modelradar-fetch-*.part")), before)


if __name__ == "__main__":
    unittest.main()
