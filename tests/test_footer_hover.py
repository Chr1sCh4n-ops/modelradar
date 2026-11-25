"""脚注三个计数的悬停契约：精确 data-i 子集、焦点/键盘、卡片复用 MR_CARDS。"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import render_scatter_svg as R


SVG = ('<svg data-plot="54 24 660 406">'
       '<g class="ln" data-k="fam1" data-c="V"></g>'
       '<g class="mk" data-k="fam1" data-c="V" data-i="0" data-n="A&lt;&amp;B"></g>'
       '<g class="mk" data-k="fam1" data-c="V" data-i="1" data-n="C"></g>'
       '<g class="mk" data-k="fam2" data-c="V" data-i="2" data-n="D"></g>'
       '<g class="chip fam" data-k="fam1" data-r0="1 2 3 4"></g>'
       '<g class="chip fam" data-k="fam2" data-r0="5 6 7 8"></g>'
       '</svg>')

GROUPS = {
    "node": [(0, 'A<&"B')],
    "est": [(1, "C")],
    "band": [(2, "D")],
}


class FooterGroups(unittest.TestCase):
    def page(self, **kw):
        return R.build_html(SVG, "2026-10-29 00:00", False, **kw)

    def spans(self, page):
        body = page.split("<script>")[0]
        found = re.findall(
            r'<span class="fgrp" data-foot-group="([^"]+)" data-index="([^"]+)"[^>]*>'
            r'(.*?)</span></span>', body, re.S)
        return {key: (idx, inner) for key, idx, inner in found}

    def test_三个数字各带精确子集且名字转义(self):
        page = self.page(n_node=1, n_est=1, n_band=1, groups=GROUPS)
        got = self.spans(page)
        self.assertEqual(set(got), {"node", "est", "band"})
        self.assertEqual(got["node"][0], "0")
        self.assertEqual(got["est"][0], "1")
        self.assertEqual(got["band"][0], "2")
        self.assertIn("A&lt;&amp;\"B", got["node"][1])
        self.assertNotIn("A<&\"B", got["node"][1])
        self.assertIn('class="bub"', got["node"][1])

    def test_子集列表不多不少且只来自三个计数(self):
        page = self.page(n_node=1, n_est=1, n_band=1, groups=GROUPS)
        body = page.split("<script>")[0]
        self.assertEqual(len(re.findall(r'data-foot-group="', body)), 3)
        self.assertEqual(len(re.findall(r'data-index="', body)), 3)

    def test_缺groups时退回纯数字不造链接(self):
        page = self.page(n_node=1, n_est=1, n_band=1)
        body = page.split("<script>")[0]
        self.assertNotIn('data-foot-group="', body)
        self.assertIn("<b>1</b> 个最小气泡", body)
        self.assertIn("虚线环 <b>1</b> 个", body)
        self.assertIn("淡色小点 <b>1</b> 个", body)

    def test_脚注气泡不依赖原生title(self):
        page = self.page(n_node=1, groups={"node": [(0, "A")]})
        span = re.search(r'<span class="fgrp".*?</span></span>', page, re.S).group(0)
        self.assertNotIn("title=", span)
        self.assertIn('aria-label="1 个最小气泡：A"', span)
        self.assertIn(":focus .bub", page)
        self.assertIn(":focus-visible", page)


class HoverContract(unittest.TestCase):
    def test_精确子集按data_i而不是整族data_k(self):
        js = R.HOVER_JS
        self.assertIn("function lightIds(list, force)", js)
        self.assertIn("svg.querySelectorAll('.mk[data-i]')", js)
        self.assertIn("picked[el.getAttribute('data-i')]", js)
        self.assertIn("if (!picked[mks[i].getAttribute('data-i')]) return false;", js)
        self.assertIn("svg.querySelectorAll('.chip[data-k=\"' + esc(k) + '\"]')", js)
        self.assertIn("cardsOf(chips, cards)", js)

    def test_指针焦点键盘都走同一子集(self):
        js = R.HOVER_JS
        for token in ("data-foot-group", "pointerenter", "pointerleave", "focus",
                      "blur", "keydown", "Escape"):
            self.assertIn(token, js)

    def test_离开还原DOM顺序高亮与卡片几何(self):
        js = R.HOVER_JS
        self.assertIn("function remember(el)", js)
        self.assertIn("function restoreChip(el)", js)
        self.assertIn("restoreChip(h[0])", js)
        # 归位必须"按原父节点分组、原下标升序"，不能按 home 记录顺序逐个插：
        # 同父多个节点一次抬起时，先插的会占掉下标，后插的按旧下标就前后颠倒。
        self.assertIn("var buckets = [];", js)
        self.assertIn("if (buckets[i][0] === h[1])", js)
        self.assertIn("b[1].sort(function(x, y){ return x[2] - y[2]; })", js)
        self.assertIn("b[0].insertBefore(h[0], b[0].children[h[2]] || null)", js)

    def test_归位覆盖末尾的toplayer且不止父级(self):
        js = R.HOVER_JS
        # 只比父节点会漏掉"父子都没错但兄弟顺序漂了"；这里要求按 children 下标归位。
        self.assertIn("b[0].children[h[2]]", js)
        self.assertNotIn("h[1].insertBefore(h[0], h[1].children[h[2]] || null)", js)


class SelectionPriority(unittest.TestCase):
    """清理不是无条件熄灯：指针态 > 焦点态；离开脚注/图区/空白延时都要回退焦点选择。"""

    def test_三种选择态分开记(self):
        js = R.HOVER_JS
        for token in ("var pointerFoot = null, hoverGroup = null, focusFoot = null;",
                      "function applySelection()", "pointerFoot = el",
                      "focusFoot = el", "hoverGroup = {"):
            self.assertIn(token, js)

    def test_优先级指针先于焦点再于熄灯(self):
        js = R.HOVER_JS
        body = js[js.index("function applySelection()"):js.index("function scheduleDark()")]
        i_ptr = body.index("if (pointerFoot)")
        i_hover = body.index("if (hoverGroup)")
        i_focus = body.index("if (focusFoot)")
        i_dark = body.index("cancelDark(); dark();")
        self.assertTrue(i_ptr < i_hover < i_focus < i_dark, body)
        self.assertNotIn("document.activeElement", body)

    def test_延时清理回退焦点而不是直接dark(self):
        js = R.HOVER_JS
        self.assertIn("pend = setTimeout(function(){ pend = null; applySelection(); }, 80);", js)
        self.assertNotIn("pend = setTimeout(function(){ pend = null; dark(); }, 80);", js)

    def test_离开图区与脚注都走优先级回退(self):
        js = R.HOVER_JS
        self.assertIn("hoverGroup = null;\n    applySelection();", js)
        self.assertIn("if (focusFoot || hoverGroup) applySelection(); else scheduleDark();", js)

    def test_Escape显式清场含三种选择态和blur(self):
        js = R.HOVER_JS
        self.assertIn("cancelDark(); pointerFoot = null; hoverGroup = null; focusFoot = null;", js)
        self.assertIn("dark(); el.blur();", js)

    def test_焦点不抢指针高亮(self):
        js = R.HOVER_JS
        self.assertIn("if (pointerFoot || hoverGroup) return;", js)

    def test_模式切换时脚注选择也重排(self):
        self.assertIn("if (c.mode === 'ids') lightIds(c.ids, true);", R.HOVER_JS)

    def test_卡片开关关闭时高亮仍走原有MR_CARDS分支(self):
        self.assertIn("if (window.MR_CARDS === 0) return;", R.HOVER_JS)


class CopyAndSwitch(unittest.TestCase):
    def test_可见文案改叫思考前摇且保留免责声明(self):
        p = {"n": "M", "c": "V", "y": 40, "x": 1, "tok": "100 tok/s",
             "tp": 10, "think": 7, "think_ratio": 70.0,
             "k": "k", "s": "S", "t": "low", "pi": None}
        self.assertIn("思考前摇 70%", R.point_tip(p))
        self.assertNotIn("思考/任务", R.point_tip(p))
        self.assertIn("思考前摇", " ".join(R.family_card_lines([p])))
        page = R.build_html(SVG, "2026-10-29 00:00", False)
        self.assertNotIn("思考/任务", page)
        self.assertIn("思考前摇为不同基准估计值之比，非任务内时间分解", page)

    def test_README可见旧词改叫思考前摇且键名不变(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("思考/任务", readme)
        self.assertIn("思考前摇", readme)
        # 只改可见文案：载荷/看板的派生键仍是 time_per_task_s / think_ratio。
        self.assertIn("time_per_task_s", readme)
        self.assertIn("think_ratio", (ROOT / "dashboard.html").read_text(encoding="utf-8"))

    def test_sizebar只留开关与当前口径说明(self):
        page = R.build_html(SVG, "2026-10-29 00:00", False)
        bar = re.search(r'<div class="sizebar">.*?</div>', page, re.S).group(0)
        self.assertNotIn("圆面积：TPS", bar, "开关已表达语义，不再重复前缀文案")
        self.assertIn("任务耗时（越快越大）", bar)
        self.assertIn('id="task-size"', bar)
        self.assertIn('id="size-caption"', bar)
        self.assertIn("气泡面积 = 输出速度", bar)


if __name__ == "__main__":
    unittest.main()
