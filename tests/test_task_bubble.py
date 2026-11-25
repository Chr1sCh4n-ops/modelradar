"""按任务耗时显示气泡大小：面积编码、缺值空心环、模式开关与数据契约。"""
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

import fetch_model_data as F
import render_scatter_svg as R


def row(name, **kw):
    base = {"name": name, "creator": "V", "index": None, "apt_usd": None,
            "tok_s": None, "ttft_s": None, "total_s": None,
            "time_per_task_s": None, "evaluation_version": "5.0"}
    base.update(kw)
    return base


class TaskRadius(unittest.TestCase):
    def test_面积分数等于设计值且越快越大(self):
        tmin, tmax = 10.0, 1000.0
        fast = R.task_radius(tmin, tmin, tmax)
        slow = R.task_radius(tmax, tmin, tmax)
        mid = R.task_radius(math.sqrt(tmin * tmax), tmin, tmax)
        self.assertEqual(fast, R.R_MAX)
        self.assertAlmostEqual(slow, R.TASK_R_MIN, places=6)
        self.assertAlmostEqual((fast ** 2) / (R.R_MAX ** 2), 1.0, places=6)
        self.assertAlmostEqual((slow ** 2) / (R.R_MAX ** 2), R.TASK_MIN_FRACTION, places=2)
        self.assertAlmostEqual((fast ** 2) / (slow ** 2), 25.0, places=2,
                               msg="最慢 4% 面积 → 快慢面积比 1:25")
        # 几何中点耗时给出 u=.5，面积分数 .04+.96×.5=.52
        self.assertAlmostEqual((mid ** 2) / (R.R_MAX ** 2), 0.52, delta=0.005)

    def test_单调且夹在有效范围内(self):
        times = [1, 2, 5, 10, 100, 1000, 10000]
        radii = [R.task_radius(t, 1, 10000) for t in times]
        self.assertEqual(radii, sorted(radii, reverse=True), "越慢半径越小")
        self.assertTrue(all(R.TASK_R_MIN <= r <= R.R_MAX for r in radii))
        self.assertEqual(R.task_radius(0.1, 1, 10000), R.R_MAX, "低于域下限夹到 1")
        self.assertEqual(R.task_radius(100000, 1, 10000), R.TASK_R_MIN, "高于域上限夹到 0")

    def test_同一耗时全部相同给中间值(self):
        self.assertEqual(R.task_radius(60, 60, 60), 10.8)
        self.assertEqual(R.task_radius(60, 60, 60), R.task_radius(999, 999, 999))

    def test_缺失或非法耗时给2px占位(self):
        for value in (None, 0, -1, True, False, "12", float("nan"), float("inf")):
            self.assertEqual(R.task_radius(value, 1, 100), R.R_MISSING, value)
        self.assertEqual(R.R_MISSING, R.R_MIN)
        self.assertEqual(R.TASK_R_MIN, 0.2 * R.R_MAX)


class ThinkRatio(unittest.TestCase):
    def test_合法比值允许超过100且格式一位小数去尾零(self):
        self.assertEqual(R.think_ratio(7, 10), 70.0)
        self.assertEqual(R.think_ratio(150, 100), 150.0, "不同基准之比可以超过 100%")
        self.assertEqual(R.think_ratio(0, 10), 0.0, "分子为 0 是合法值")
        self.assertEqual(R.think_ratio_text(70.0), "70")
        self.assertEqual(R.think_ratio_text(109.75), "109.8")
        self.assertEqual(R.think_ratio_text(0), "0")
        self.assertEqual(R.think_ratio_cell({"think_ratio": 70.0}), "70%")
        self.assertEqual(R.think_ratio_cell({"think": 7, "tp": 10}), "70%")

    def test_缺失或非法端点给破折号(self):
        for num, den in ((None, 10), (-1, 10), (10, None), (10, 0), (10, -1),
                         (True, 10), (10, True), ("7", 10), (10, "10"),
                         (float("nan"), 10), (10, float("inf"))):
            self.assertIsNone(R.think_ratio(num, den), (num, den))
            self.assertEqual(R.think_ratio_cell({"think": num, "tp": den}), "—")
        self.assertEqual(R.think_ratio_text(None), "—")
        self.assertEqual(R.think_ratio_text(-1), "—")


class TaskBubbleSvg(unittest.TestCase):
    def _load(self, models):
        bands = F.tier_ladder(models)
        F.annotate_efficiency(models, bands)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "latest.json"
            p.write_text(json.dumps({"generated_at": "2026-10-28 07:30", "models": models,
                                     "legend": F.build_legend(models, bands),
                                     "tier_bands": bands,
                                     "tier_tolerance_points": F.TIER_TOLERANCE_POINTS},
                                    ensure_ascii=False), encoding="utf-8")
            with patch.object(R, "LATEST", p):
                return R.load_points()

    def _models(self):
        return [
            row("Fast (low)", index=40, apt_usd=0.5, tok_s=100, total_s=12,
                series_key="v1", series="Alpha", tier="low", time_per_task_s=10.0),
            row("Slow (max)", index=44, apt_usd=2.0, tok_s=200, total_s=1100,
                series_key="v1", series="Alpha", tier="max", time_per_task_s=1000.0),
            row("NoTask", index=30, apt_usd=1.0, tok_s=50,
                series_key="nt", series="NoTask", tier="max",
                time_per_task_s=None),
            row("Est", index=35, apt_usd=0.8, tok_s=None, tok_s_est=60,
                tok_s_est_range=[20, 40], tok_s_est_basis="series-interp",
                tok_s_est_n=2, tok_s_est_note="估计：同族相邻档插值",
                series_key="es", series="Est", tier="max", time_per_task_s=100),
        ]

    def test_默认TPS半径一个字节都不动(self):
        pts, front, order = self._load(self._models())
        before = {p["n"]: p["r"] for p in pts}
        R.apply_task_radii(pts)
        self.assertEqual({p["n"]: p["r"] for p in pts}, before)
        tmax = max(p["v"] for p in pts if p.get("v")) or 1
        for p in pts:
            if p.get("v"):
                self.assertEqual(p["r"], R.radius(p["v"], tmax))

    def test_每个点带默认半径与任务半径契约(self):
        pts, front, order = self._load(self._models())
        svg = R.build_svg(pts, front, order)
        by = {p["n"]: p for p in pts}
        self.assertEqual(by["Fast (low)"]["r_task"], 15.0)
        self.assertEqual(by["Slow (max)"]["r_task"], 3.0)
        self.assertEqual(by["NoTask"]["r_task"], R.R_MISSING)
        self.assertTrue(by["NoTask"]["task_missing"])
        self.assertFalse(by["Fast (low)"]["task_missing"])
        fast = next(g for g in _groups(svg) if 'data-n="Fast (low)"' in g)
        slow = next(g for g in _groups(svg) if 'data-n="Slow (max)"' in g)
        miss = next(g for g in _groups(svg) if 'data-n="NoTask"' in g)
        self.assertIn('data-r="%s"' % _num(by["Fast (low)"]["r"]), fast)
        self.assertIn('data-r-task="15.0"', fast)
        self.assertIn('data-r-task="3.0"', slow)
        self.assertIn('data-r-task="2.0"', miss)
        self.assertIn('data-task-missing="1"', miss)
        self.assertNotIn("data-task-missing", fast)
        self.assertIn('data-role="node"', fast)
        self.assertIn('data-band-ring="1"', svg, "TPS 推估环带要有可隐藏标记")
        self.assertIn('data-ball-task=', svg, "族卡要带任务模式锚点")

    def test_全部缺耗时或全部同耗时(self):
        equal, _, _ = self._load([
            row("A", index=40, apt_usd=0.5, tok_s=100, time_per_task_s=60),
            row("B", index=44, apt_usd=2.0, tok_s=200, time_per_task_s=60),
        ])
        R.apply_task_radii(equal)
        self.assertEqual([p["r_task"] for p in equal], [10.8, 10.8])
        none, _, _ = self._load([
            row("A", index=40, apt_usd=0.5, tok_s=100),
            row("B", index=44, apt_usd=2.0, tok_s=200),
        ])
        R.apply_task_radii(none)
        self.assertEqual({p["r_task"] for p in none}, {R.R_MISSING})
        self.assertTrue(all(p["task_missing"] for p in none))

    def test_开关在HTML里默认关闭且只改尺寸语义(self):
        page = R.build_html("<svg data-plot=\"1 2 3 4\"></svg>", "2026-10-28 07:30", False)
        self.assertIn('role="switch"', page)
        self.assertNotIn("圆面积：TPS", page, "开关本身已表达语义，左边不留重复文案")
        self.assertIn("任务耗时（越快越大）", page)
        self.assertNotIn("按任务耗时显示大小", page)
        self.assertIn('id="task-size"', page)
        self.assertIn('id="size-caption"', page)
        self.assertRegex(page, r'<input[^>]*id="task-size"[^>]*>')
        tag = page.split('id="task-size"')[1].split(">")[0]
        self.assertNotIn(" checked", tag, "默认不勾选")
        self.assertIn('aria-checked="false"', tag)
        self.assertIn("aria-checked", R.SIZE_JS, "切换时同步 switch 状态")
        self.assertIn("气泡面积 = 输出速度", page)
        self.assertIn('data-size-mode="task"', page, "CSS 任务模式规则")
        self.assertIn("[data-band-ring]", page)
        self.assertIn("fill:none", page)
        self.assertIn("SIZE-BEGIN", page)

    def test_比值进入节点族卡与厂商且厂商用逐档中位(self):
        pts, front, order = self._load(self._models())
        by = {p["n"]: p for p in pts}
        self.assertAlmostEqual(by["Fast (low)"]["think_ratio"], 70.0)
        self.assertAlmostEqual(by["Slow (max)"]["think_ratio"], 109.75)
        self.assertIsNone(by["NoTask"]["think_ratio"])
        lines = R.family_card_lines([p for p in pts if p["k"] == "v1"])
        self.assertIn("任务耗时(估) · 思考时间 · 思考前摇 · tok/s", lines[2])
        self.assertIn("70%", lines[4])
        self.assertIn("109.8%", lines[3])
        self.assertIn("思考前摇 109.8%", R.point_tip(by["Slow (max)"]))
        self.assertNotIn("思考前摇", R.point_tip(by["NoTask"]),
                         "取不到思考时间时整段不出现，不能写破折号冒充实测 0")
        txt = R.vendor_stats("V", pts)
        # 逐档比值 [70, 109.75] 的中位 = 89.9%；先取中位再相除会得到 109.4%。
        self.assertIn("思考前摇 89.9%（2/4 档）", txt)
        self.assertNotIn("109.4%", txt, "不能拿两个中位相除冒充比值中位")

    def test_四处不再显示前500TPS(self):
        pts, front, order = self._load(self._models())
        by = {p["n"]: p for p in pts}
        self.assertNotIn("前500", R.point_tip(by["Fast (low)"]))
        self.assertNotIn("前500", R.family_card_lines([by["Fast (low)"]])[-2])
        self.assertNotIn("前500", "\n".join(R.family_card_lines([by["Fast (low)"]])))
        self.assertNotIn("前500", R.vendor_stats("V", pts))
        page = R.build_html("<svg/>", "2026-10-28 07:30", False)
        self.assertNotIn("前500", page)
        self.assertNotIn("非独立实测吞吐", page)
        self.assertIn("不同基准估计值之比，非任务内时间分解", page)
        self.assertFalse(hasattr(R, "t500_rate"), "孤立的派生率助手应随显示一起删除")


class FirstAnswerFallback(unittest.TestCase):
    """新版 AA 不再提供旧基准总响应，只有官方首回答延迟时的显示行为。"""

    _load = TaskBubbleSvg._load

    def _models(self):
        return [
            row("Fb (low)", index=40, apt_usd=0.5, tok_s=100, first_answer_s=24.51,
                series_key="f1", series="Fallback", tier="low", time_per_task_s=100.0),
            row("Fb (max)", index=44, apt_usd=2.0, tok_s=200, first_answer_s=2.17,
                series_key="f1", series="Fallback", tier="max", time_per_task_s=50.0),
            row("NoThink", index=30, apt_usd=1.0, tok_s=50,
                series_key="nt", series="NoThink", tier="max", time_per_task_s=80.0),
        ]

    def test_总响应缺失时用首回答延迟算思考时间与比值(self):
        pts, front, order = self._load(self._models())
        by = {p["n"]: p for p in pts}
        self.assertAlmostEqual(by["Fb (low)"]["think"], 24.51)
        self.assertAlmostEqual(by["Fb (low)"]["think_ratio"], 24.51)
        self.assertIsNone(by["NoThink"]["think"])
        self.assertIsNone(by["NoThink"]["think_ratio"])

    def test_无思考时间的点整段不出现破折号(self):
        pts, front, order = self._load(self._models())
        by = {p["n"]: p for p in pts}
        tip = R.point_tip(by["NoThink"])
        self.assertNotIn("思考时间", tip)
        self.assertNotIn("思考前摇", tip)
        self.assertIn("思考时间", R.point_tip(by["Fb (low)"]))

    def test_整族都没有思考时间时表头也不留空列(self):
        pts, _, _ = self._load(self._models())
        none_lines = R.family_card_lines([p for p in pts if p["k"] == "nt"])
        self.assertNotIn("思考时间", none_lines[2])
        self.assertNotIn("思考前摇", none_lines[2])
        self.assertIn("任务耗时(估) · tok/s", none_lines[2])
        have_lines = R.family_card_lines([p for p in pts if p["k"] == "f1"])
        self.assertIn("任务耗时(估) · 思考时间 · 思考前摇 · tok/s", have_lines[2])
        joined = "\n".join(have_lines)
        self.assertIn("24.5s", joined, "首回答延迟就是思考时间本身")
        self.assertIn("4.3%", joined, "2.17 ÷ 50 = 4.34%")


def _groups(svg):
    import re
    return re.findall(r'<g class="mk[^"]*"[\s\S]*?</g>', svg)


def _num(value):
    return f"{value:.1f}"


if __name__ == "__main__":
    unittest.main()
