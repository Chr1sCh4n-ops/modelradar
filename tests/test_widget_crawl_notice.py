"""「立即抓取」失败提示的回归契约：rc=2 是旧数据，不是坏图。

2026-11-04 实机故障：三个源全挂时 crawl.sh 以 2 退出（fetch_model_data 里
data_state 为 empty/stale 的既定约定）。crawl.sh 的渲染是无条件执行的，所以
磁盘上那张图仍然有效，只是被标注成旧数据。组件却把 rc=2 一律当"抓取失败"：
整图铺 0.62 红纱、文字标红、关着 popover 时还亮未读红点 —— 用户看到的是
"图坏了"且"点不动"，而提示正文只是爬虫收尾日志的尾巴，看不出哪个源挂了。

这里守两层：
  1) 源码契约（永远可跑）：裁决是纯函数、遮罩有"不铺纱"档、角标跟裁决走；
  2) 行为（有 swiftc 时跑）：直接调用生产裁决与真实 ChartView 的遮罩，
     不复制一份算法 —— 复制出来的那份永远通过。
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWIFT = ROOT / "widget" / "ChartWidget.swift"
FIXTURE_LOGIC = ROOT / "widget" / "verify_crawl_rc2.swift"
FIXTURE_UI = ROOT / "widget" / "verify_crawl_notice_ui.swift"


def _compile(sources, out):
    subprocess.run(
        ["swiftc", "-O", "-o", out, *[str(s) for s in sources],
         "-framework", "AppKit", "-framework", "WebKit",
         "-framework", "OSLog", "-framework", "UserNotifications"],
        check=True, capture_output=True, text=True)


class CrawlNoticeSourceContract(unittest.TestCase):
    def setUp(self):
        self.src = SWIFT.read_text(encoding="utf-8")

    def test_裁决是纯函数且被真实调用(self):
        # 纯函数才能对同一份实现跑各种退出码；否则只能测一份抄来的副本。
        self.assertIn("static func crawlOutcome(", self.src)
        self.assertIn("crawlOutcome(\n                    outcome: r.outcome", self.src)

    def test_rc2_带图走不挡提示_带图缺失才判失败(self):
        body = self.src[self.src.index("static func crawlOutcome("):]
        body = body[:body.index("\n    private func runAsyncTask")]
        self.assertIn("exitCode == 2, chartExists", body,
                      "rc=2 必须先确认磁盘上还有图，才能降级成提示")
        rc2 = body[body.index("exitCode == 2, chartExists"):]
        self.assertIn("return .warn(", rc2.split("return .failed(")[0],
                      "rc=2 且图仍在 → warn（不标红、不遮图）")
        self.assertIn("return .failed(", rc2, "图缺失时必须退回真失败")

    def test_不挡提示不吃鼠标_也不铺纱(self):
        # 只换文案不够：用户抱怨的是"图看起来坏了、还点不动"。
        self.assertIn("var dimsBackground = true", self.src)
        self.assertIn("dimsBackground ? super.hitTest(point) : nil", self.src)
        self.assertIn("overlay.dimsBackground = blocking", self.src)

    def test_关着_popover_时的角标跟裁决走(self):
        # 原来按"退出码非零"亮红点，rc=2 也亮：用户看见红点，打开却是可用的旧图。
        body = self.src[self.src.index("let outcome = onDone(result, landed)"):]
        body = body[:body.index("if self.popover?.isShown == true")]
        self.assertIn("let failed = outcome.isError", body)
        self.assertNotIn("result.outcome != .success", body)

    def test_暂存结果保留不挡标记(self):
        self.assertIn("private var pendingResult: (text: String, isError: Bool, blocking: Bool)?",
                      self.src)
        self.assertIn("chart.showResult(r.text, isError: r.isError, blocking: r.blocking)",
                      self.src)

    def test_失败来源取自落盘台账而非爬虫尾巴(self):
        self.assertIn("static func sourceFailureSummary(", self.src)
        body = self.src[self.src.index("static func sourceFailureSummary("):]
        body = body[:body.index("\n    private static func shortReason")]
        self.assertIn('obj["sources"]', body)
        self.assertIn('v["ok"]', body)
        self.assertIn('v["stale"]', body)
        # 一眼能认的那截原因里不该再塞 URL（每源一个固定榜单地址，分不出区别）。
        self.assertIn('hasPrefix("http")', self.src)


@unittest.skipUnless(shutil.which("swiftc"), "需要 swiftc")
class CrawlNoticeBehavior(unittest.TestCase):
    """跑生产裁决与真实遮罩；算法只有一份，就在 ChartWidget.swift 里。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="mr_crawl_notice_")
        cls.logic = str(Path(cls.tmp) / "logic")
        cls.ui = str(Path(cls.tmp) / "ui")
        _compile([SWIFT, FIXTURE_LOGIC], cls.logic)
        _compile([SWIFT, FIXTURE_UI], cls.ui)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, binary):
        return subprocess.run([binary], capture_output=True, text=True, timeout=120)

    def test_退出码语义(self):
        r = self._run(self.logic)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("VERIFY OK", r.stdout)

    def test_提示不挡图(self):
        r = self._run(self.ui)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("VERIFY OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
