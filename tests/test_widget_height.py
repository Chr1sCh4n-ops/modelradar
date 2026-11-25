"""popover 内容高度必须把开关栏（.sizebar）算进去，且老页面仍可量。

判据不是"再抄一份 JS 到测试里"：直接从 widget/ChartWidget.swift 抽出
measureContentHeight() 里的字面量 JS，交给 Node + 最小 DOM 桩真跑一遍。
字面量一旦回退成"只加 .wrap + .foot"，500 这条会当场落到 490。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWIFT = ROOT / "widget" / "ChartWidget.swift"

# nvm 的 node 不在 PATH 上（实测 `which node` 为空），只能按绝对根目录找。
# 用 Path.home() 拼，不写死某台机器的用户名 —— 这份文件要随代码发出去。
NODE_ROOT = Path.home() / ".nvm" / "versions" / "node"

DOM_HARNESS = r"""
const fs = require('fs');
const js = fs.readFileSync(process.argv[1], 'utf8');
const fix = JSON.parse(process.argv[2]);
const heights = fix.heights || {};
global.document = {
  querySelector(sel) {
    if (!(sel in heights)) return null;
    return { getBoundingClientRect: () => ({ height: heights[sel] }) };
  },
  body: fix.bodyHeight == null ? null : { scrollHeight: fix.bodyHeight },
};
console.log(JSON.stringify({ result: eval(js) }));
"""


def node_bin() -> Path:
    """按版本号挑最高的可用 node；全都不可用就跳过并说明根目录。"""
    def key(p: Path):
        return tuple(int(x) for x in re.findall(r"\d+", p.parent.parent.name))

    candidates = sorted(
        (p for p in NODE_ROOT.glob("v*/bin/node") if p.is_file() and os.access(p, os.X_OK)),
        key=key,
    )
    if not candidates:
        raise unittest.SkipTest(f"没有可用的 node：{NODE_ROOT}/v*/bin/node")
    return candidates[-1]


def extract_measure_js() -> str:
    """抽 measureContentHeight() 里 let js = \"\"\" ... \"\"\" 的原文。"""
    source = SWIFT.read_text(encoding="utf-8")
    m = re.search(
        r"private func measureContentHeight\(\)\s*\{.*?let js = \"\"\"\n(.*?)\n\s*\"\"\"",
        source,
        re.S,
    )
    if not m:
        raise AssertionError("ChartWidget.swift 里找不到 measureContentHeight 的 js 字面量")
    return m.group(1)


class MeasureContentHeight(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = node_bin()
        cls.js = extract_measure_js()
        # 这条只负责把"改错文件/抽错块"说清楚，数值判据在下面三条。
        assert ".sizebar" in cls.js, "抽到的 JS 不含 .sizebar：抽错块或补丁被回退"

    def measure(self, heights, body_height=None):
        fix = {"heights": heights, "bodyHeight": body_height}
        with tempfile.TemporaryDirectory() as td:
            js_path = Path(td) / "measure.js"
            js_path.write_text(self.js, encoding="utf-8")
            proc = subprocess.run(
                [str(self.node), "-e", DOM_HARNESS, str(js_path), json.dumps(fix)],
                capture_output=True,
                text=True,
                timeout=20,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)["result"]

    def test_开关栏图表脚注三段相加为500(self):
        got = self.measure({".sizebar": 10, ".wrap": 400, ".foot": 90})
        self.assertEqual(got, 500)

    def test_老页面没有开关栏时仍是490(self):
        got = self.measure({".wrap": 400, ".foot": 90})
        self.assertEqual(got, 490)

    def test_缺少wrap和foot时退回body滚动高度(self):
        got = self.measure({".sizebar": 10}, body_height=620)
        self.assertEqual(got, 620)


if __name__ == "__main__":
    unittest.main()
