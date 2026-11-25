#!/usr/bin/env python3
"""规则层的纯函数单测：不联网、不写运行时目录。

    /usr/bin/python3 -m unittest discover -s tools

为什么单独测这几个函数：它们决定"变动卡说什么、不说什么"，而错法都是静默的
（漏报、把缺失当 0、NaN%），看板渲染出来看着完全正常，只有对着输入输出比才看得见。
"""
from __future__ import annotations

import inspect
import json
import math
import os
import re
import sqlite3
import sys
import time
import contextlib
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def support_path_patches(mod, root: Path, skip=("DATA", "CHART_HTML")):
    """把 `mod.SUPPORT` 底下的**每一个**派生落点都搬进临时目录 —— 按前缀枚举，不手写清单。

    手写清单漏一条就会写进用户真实目录：10-15 加 `.last_alert.json` 时就是这样，
    跑一轮单测把线上"上次提示"记录覆盖成夹具数据。`DATA` / `CHART_HTML` 由调用点
    按自己的子目录布局给，这里跳过，免得同一属性打两次。
    """
    base = str(mod.SUPPORT) + os.sep
    outs = []
    for name in sorted(dir(mod)):
        if name in skip:
            continue
        val = getattr(mod, name)
        if isinstance(val, Path) and str(val).startswith(base):
            outs.append(unittest.mock.patch.object(mod, name, root / val.name))
    return outs


@contextlib.contextmanager
def support_path_stack(mod, root: Path, skip=("DATA", "CHART_HTML")):
    """`support_path_patches` 的 `with` 形式，给那些用反斜杠续行的站点用。"""
    with contextlib.ExitStack() as st:
        for p in support_path_patches(mod, root, skip):
            st.enter_context(p)
        yield st


def assert_support_fully_isolated(test: unittest.TestCase, mod, root: Path) -> None:
    """盯"隔离表覆盖全了"：SUPPORT 底下每个落点都必须被搬走（除调用点自己按子目录给的那两个）。

    这条比逐条列举有用 —— 以后再加一个写进 SUPPORT 的文件（`.last_alert.json` 就是这么漏的），
    不用改这条判据，它自动就把它纳进覆盖范围；漏了就红。
    """
    base = str(mod.SUPPORT) + os.sep
    names = {n for n, v in vars(mod).items()
             if isinstance(v, Path) and str(v).startswith(base)}
    covered = {p.attribute for p in support_path_patches(mod, root)}
    test.assertEqual(covered, names - {"DATA", "CHART_HTML"},
                     "SUPPORT 下有落点没被夹具搬走 —— 跑测试会写进用户真实目录")


import fetch_model_data as F    # noqa: E402

# 夹具绝不读本机实盘的隔离清单。实盘条目按「模型名 + 区间」命中，而这里的合成轮次 ts 只有
# "11:00" 这种不带日期的串 —— `from_ts` 为空（=不限下限）时字符串比把它算进窗口，于是单测
# 红不红取决于用户机器上筛过哪几条（2026-11-06 实测：装上跨 11-04 的速度口径清单后
# TestFamilyTail 两条红，同一份清单置空即绿）。要用清单的用例自己 patch 或显式传 rules。
_QUARANTINE_EMPTY = Path(tempfile.mkdtemp(prefix="modelradar-rules-quarantine-")) / "quarantine.json"
_QUARANTINE_EMPTY.write_text('{"quarantined": []}', encoding="utf-8")
F.QUARANTINE_PATH = _QUARANTINE_EMPTY

# 夹具里"刚刚抓完"的时间戳：写死绝对值会在几天后被 26 小时算龄判成 not-running（10-12 就红过一片）
_NOW8 = F.datetime.now(F._TZ8)
GEN_TS = (_NOW8 - F.timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
import render_scatter_svg as R  # noqa: E402
import summarize as S           # noqa: E402


def row(name, **kw):
    base = {"name": name, "creator": "Acme", "index": None, "apt_usd": None,
            "in_per_1m_usd": None, "out_per_1m_usd": None, "tok_s": None, "ttft_s": None,
            "evaluation_version": "5.0"}
    base.update(kw)
    return base


# 2026-11-04 起 parse_aa 只按表头语义认列，没有 <th> 的表格直接拒绝（旧夹具正是这种）。
# 夹具必须跟真实页面同构：带一份语义表头，列数也要和数据行一致。
AA_HEAD = ("<thead><tr><th>Model</th><th>Context Window</th><th>Creator</th>"
           "<th>Artificial Analysis Intelligence Index</th>"
           "<th>Cost per Task USD</th><th>Output Speed (t/s)</th>"
           "<th>Latency First Chunk (s)</th><th>Total Response (s)</th></tr></thead>")


def aa_page(body: str) -> bytes:
    return f"<html><body><table>{AA_HEAD}<tbody>{body}</tbody></table></body></html>".encode()


class TestDiff(unittest.TestCase):
    def test_有值变有值_给幅度(self):
        cur = [row("A", index=47)]
        prev = {"A": row("A", index=43)}
        ch = F.diff_models(cur, prev)
        self.assertEqual(len(ch), 1)
        f = ch[0]["fields"][0]
        self.assertEqual((f["kind"], f["from"], f["to"], f["pct"]), ("changed", 43, 47, 9.3))

    def test_从无到有要报(self):
        """回归：以前 `a is None or b is None → continue` 把这类整类吞掉，
        AA 第一次测出 tok_s、价格第一次出现，变动卡全程静默。"""
        cur = [row("A", tok_s=88)]
        prev = {"A": row("A", tok_s=None)}
        ch = F.diff_models(cur, prev)
        self.assertEqual(len(ch), 1, "无→有必须算变动")
        f = ch[0]["fields"][0]
        self.assertEqual(f["kind"], "appeared")
        self.assertIsNone(f["pct"], "没有基数就不该有百分比")

    def test_从有到无要报(self):
        cur = [row("A", tok_s=None)]
        prev = {"A": row("A", tok_s=88)}
        f = F.diff_models(cur, prev)[0]["fields"][0]
        self.assertEqual(f["kind"], "disappeared")
        self.assertEqual(f["from"], 88, "要说清原来是多少，别只说没了")

    def test_两边都缺_不算变动(self):
        cur = [row("A", tok_s=None)]
        prev = {"A": row("A", tok_s=None)}
        self.assertEqual(F.diff_models(cur, prev), [])

    def test_沿用行记成carried而不是新进榜(self):
        """沿用行是台账恢复的旧读数，AA 本轮没有它；记成 new 会让浮层说"新进榜 N 个"。"""
        cur = [row("Gone (high)"), row("Fresh (high)")]
        cur[0]["carried_asof"] = "2026-11-09 08:15:06"
        ch = {c["name"]: c["type"] for c in F.diff_models(cur, {})}
        self.assertEqual(ch["Gone (high)"], "carried")
        self.assertEqual(ch["Fresh (high)"], "new",
                         "真新进榜的仍要记 new，别把两支合并")

    def test_零不是缺失(self):
        """0 是实测值，不能被当成"没有"漏报，也不能被 `if a` 之类判成 False。"""
        cur = [row("A", apt_usd=0.0)]
        prev = {"A": row("A", apt_usd=0.25)}
        f = F.diff_models(cur, prev)[0]["fields"][0]
        self.assertEqual(f["to"], 0.0)
        self.assertEqual(f["kind"], "changed")

    def test_新进榜与掉榜(self):
        ch = F.diff_models([row("新")], {"旧": row("旧")})
        kinds = {c["type"] for c in ch}
        self.assertEqual(kinds, {"new", "gone"})


class TestRename(unittest.TestCase):
    """2026-11-06 用户 case：AA 去掉 with fallback，五档全被报成一进一出。

    判据只收"同厂商、同代次、同档位"的写法变化，且两侧都有的来源标识不能冲突。
    改名不是"数值没变"：提醒门槛内的波动照旧保留，达门槛的变化仍要报。
    """

    NEW = "Claude Opus 5.5 (max)"
    OLD = "Claude Opus 5.5 (max with fallback)"

    def _pair(self, **kw):
        base = {"creator": "Anthropic", "index": 59, "apt_usd": 10.04,
                "in_per_1m_usd": 4.0, "out_per_1m_usd": 20.0,
                "or_id": "anthropic/claude-opus-5.5", "aa_slug": "claude-opus-5-5"}
        old = row(self.OLD, **{**base, "tok_s": 100})
        new = row(self.NEW, **{**base, "tok_s": 100, **kw})
        return F.diff_models([new], {self.OLD: old})

    def test_同族同档写法变化归为改名(self):
        ch = self._pair(tok_s=111.5)
        self.assertEqual([c["type"] for c in ch], ["renamed"], "不能报成新进榜+落榜")
        self.assertEqual(ch[0]["old_name"], self.OLD)
        self.assertTrue(ch[0]["quiet"])
        self.assertEqual([f["field"] for f in ch[0]["fields"]], ["tok_s"], "真实差异不能丢")

    def test_达门槛的变化仍要报(self):
        ch = self._pair(tok_s=200)
        self.assertEqual(ch[0]["type"], "renamed")
        self.assertFalse(ch[0]["quiet"])
        self.assertEqual([m["field"] for m in F.notable_moves(ch)], ["tok_s"])

    def test_目录报价变化不能藏进静默改名(self):
        ch = self._pair(in_per_1m_usd=5.0)
        self.assertFalse(ch[0]["quiet"], "单价不在提醒规则里，但它是真变化")

    def test_来源标识冲突就退回一进一出(self):
        old = row(self.OLD, creator="Anthropic", or_id="anthropic/claude-opus-5.5")
        new = row(self.NEW, creator="Anthropic", or_id="anthropic/claude-opus-5.5-2026")
        self.assertEqual({c["type"] for c in F.diff_models([new], {self.OLD: old})},
                         {"new", "gone"})

    def test_厂商不同不配对(self):
        old = row(self.OLD, creator="Other", aa_slug="opus")
        new = row(self.NEW, creator="Anthropic", aa_slug="opus")
        self.assertEqual({c["type"] for c in F.diff_models([new], {self.OLD: old})},
                         {"new", "gone"})

    def test_候选不唯一不配对(self):
        old = {self.OLD: row(self.OLD, creator="Anthropic", aa_slug="opus"),
               "Claude Opus 5.5 (max  with fallback)": row(
                   "Claude Opus 5.5 (max  with fallback)", creator="Anthropic", aa_slug="opus")}
        new = row(self.NEW, creator="Anthropic", aa_slug="opus")
        self.assertEqual({c["type"] for c in F.diff_models([new], old)}, {"new", "gone"})

    def test_缺少来源标识不猜测改名(self):
        ch = F.diff_models([row(self.NEW)], {self.OLD: row(self.OLD)})
        self.assertEqual({c["type"] for c in ch}, {"new", "gone"})

    def test_换代或换档不当成改名(self):
        old = row(self.OLD, aa_slug="opus")
        for name in ("Claude Opus 5.6 (max)", "Claude Opus 5.5 (high)"):
            ch = F.diff_models([row(name, aa_slug="opus")], {self.OLD: old})
            self.assertEqual({c["type"] for c in ch}, {"new", "gone"})

    def test_改名同时换评测版本不能说纯名称变化(self):
        ch = self._pair(evaluation_version="6.0")
        self.assertFalse(ch[0]["quiet"])

    def test_整族改名只出一句(self):
        old = {f"Claude Opus 5.5 ({t} with fallback)": row(
                   f"Claude Opus 5.5 ({t} with fallback)", creator="Anthropic", index=50, aa_slug="opus")
               for t in ("low", "high", "max")}
        new = [row(f"Claude Opus 5.5 ({t})", creator="Anthropic", index=50, aa_slug="opus")
               for t in ("low", "high", "max")]
        ch = F.diff_models(new, old)
        self.assertEqual(len([c for c in ch if c["type"] == "renamed"]), 3)
        lines = F.rename_lines(ch)
        self.assertEqual(len(lines), 1, "同族只提示一句")
        self.assertIn("Claude Opus 5.5：3 档改名", lines[0])
        self.assertIn("去掉 with fallback", lines[0])
        self.assertIn("未达变化门槛", lines[0])


class TestPriceAttribution(unittest.TestCase):
    """单价变动的**归因**：是上游真变价，还是我们换到了另一个目录条目。

    真实触发：GLM-5.3 输入单价 0.2219 → 1.4（+531%），而 AA 的每任务成本一分没动。
    当时基线不存 or_id，只能回去翻 snapshots 里的原始快照才敢下结论 —— 于是"单价暴涨"
    会被读者当成厂商涨价，而它可能只是匹配换了对象。
    """

    def _changed(self, prev, cur, field):
        ch = F.diff_models([cur], {"A": prev})
        self.assertEqual(len(ch), 1, "变了就该有一条变动")
        fs = {f["field"]: f for f in ch[0]["fields"]}
        self.assertIn(field, fs)
        return fs[field].get("tags", [])

    def test_同一目录条目变价_只标跨源分歧(self):
        tags = self._changed(
            row("A", in_per_1m_usd=0.2219, apt_usd=0.85, or_id="z-ai/glm-5.3"),
            row("A", in_per_1m_usd=1.4, apt_usd=0.85, or_id="z-ai/glm-5.3"),
            "in_per_1m_usd")
        self.assertIn("AA 每任务成本未变", tags)
        self.assertFalse(any("匹配对象" in t for t in tags), f"id 没变却宣称换了：{tags}")

    def test_换了条目必须说出来(self):
        tags = self._changed(
            row("A", in_per_1m_usd=0.2219, apt_usd=0.85, or_id="z-ai/glm-5.3"),
            row("A", in_per_1m_usd=1.4, apt_usd=0.85, or_id="z-ai/glm-5.3-turbo"),
            "in_per_1m_usd")
        self.assertTrue(any(t.startswith("匹配对象换了") for t in tags),
                        f"两个 id 不同却没标出来：{tags}")

    def test_两源都说变价时不贴标签(self):
        tags = self._changed(
            row("A", in_per_1m_usd=0.2219, apt_usd=0.85, or_id="x/y"),
            row("A", in_per_1m_usd=1.4, apt_usd=2.1, or_id="x/y"),
            "in_per_1m_usd")
        self.assertEqual(tags, [], "每任务成本也跟着变了就是真涨价，不该被标签淡化")

    def test_老基线没有or_id时不宣称没换(self):
        """缺证据不等于"证明了没换"：这种时候宁可不归因。"""
        tags = self._changed(
            row("A", in_per_1m_usd=0.2219, apt_usd=0.85),                  # 旧基线：无 or_id 键
            row("A", in_per_1m_usd=1.4, apt_usd=0.85, or_id="x/y"),
            "in_per_1m_usd")
        self.assertFalse(any("匹配对象" in t for t in tags), f"把「没有记录」说成了「没换」：{tags}")
        self.assertIn("AA 每任务成本未变", tags)

    def test_归因只跟单价走(self):
        tags = self._changed(
            row("A", index=43, apt_usd=0.85, or_id="x/y"),
            row("A", index=47, apt_usd=0.85, or_id="x/y"),
            "index")
        self.assertEqual(tags, [], "智能指数与 OpenRouter 目录无关，不该带单价归因")

    def test_or_id活得过一轮存盘(self):
        """or_id 不进 DIFF_FIELDS（不是数值），但必须进基线，否则下一轮无从归因。"""
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.execute("""CREATE TABLE snapshots(ts TEXT NOT NULL, source TEXT NOT NULL,
                       n_rows INTEGER, ok INTEGER, err TEXT, payload TEXT,
                       evaluation_version TEXT, PRIMARY KEY (ts, source))""")
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "cfg.json"
            cfg.write_text('{"keyword_include": [], "keyword_exclude": []}', encoding="utf-8")
            with unittest.mock.patch.object(F, "CFG_PATH", cfg):
                F.save_diff_base(con, [row("A", in_per_1m_usd=1.4, or_id="z-ai/glm-5.3")])
                prev, first = F.load_previous(con)
        self.assertFalse(first)
        self.assertEqual(prev["A"].get("or_id"), "z-ai/glm-5.3")
        self.assertNotIn("or_id", F.DIFF_FIELDS, "or_id 不能进 DIFF_FIELDS，否则会被算成百分比变动")


class TestPeerBand(unittest.TestCase):
    """第四档「同级能力参照带」的契约。

    这一档的错法全是静默的：给了中心值就会被当成一个测量结果参与前沿、进表格排序、
    被读成"这个模型大概这么快"；没落空就触发则会盖掉更准的同族推估。
    所以断言只围绕两件事——**只在前三档全落空时才算**、**只给区间不给值**。
    """

    BASE = 53

    def _peers(self, n=3):
        """窗外无关系列、窗内实测的别家模型（series_key 各不相同，避免撞词根）。"""
        return [row(f"P{i}", creator=f"V{i}", index=self.BASE, tok_s=40.0 + 7.0 * i,
                    series_key=f"peer{i}", apt_usd=0.5) for i in range(n)]

    def _lonely(self, **kw):
        # 同族、同词根都没有实测值：series_key 的词根不会在任何 peer 的词根里
        base = row("Lonely", creator="Acme", index=self.BASE, series_key="lonely", apt_usd=0.9)
        base.update(kw)
        return base

    def test_前三档落空才给带_且不给中心值(self):
        m = self._lonely()
        got_est, got_band = F.estimate_missing_tok_s(self._peers() + [m])
        self.assertEqual((got_est, got_band), (0, 1))
        self.assertEqual(m["tok_s_band"], [40.0, 54.0], "带必须正好是窗内实测的最小/最大")
        self.assertEqual(m["tok_s_band_n"], 3)
        self.assertEqual(m["tok_s_band_same_creator"], 0, "peer 全是别家，同厂商数应为 0")
        self.assertNotIn("tok_s_est", m, "有带就不许有点估计")
        self.assertNotIn("tok_s_est_range", m)
        self.assertIsNone(m["tok_s"], "tok_s 本身绝不能被区间顶掉")
        self.assertIn("不给中心值", m["tok_s_band_note"])

    def test_同族有实测时仍走点估计_不给带(self):
        # 只要前三档能命中，第四档就不该出现——它是兜底，不是并列的另一种口径
        sibs = [row("S low", index=50, tok_s=60.0, series_key="lonely"),
                row("S high", index=56, tok_s=90.0, series_key="lonely")]
        m = self._lonely()
        got_est, got_band = F.estimate_missing_tok_s(self._peers() + sibs + [m])
        self.assertEqual((got_est, got_band), (1, 0))
        self.assertEqual(m["tok_s_est"], 75.0, "53 分应落在 50/56 两档之间插值")
        self.assertNotIn("tok_s_band", m)

    def test_跨代次同词根命中时也不给带(self):
        kin = row("Kin", index=61, tok_s=88.0, series_key="lonely2")   # 去掉数字后同词根
        m = self._lonely()
        got_est, got_band = F.estimate_missing_tok_s(self._peers() + [kin] + [m])
        self.assertEqual((got_est, got_band), (1, 0))
        self.assertEqual(m["tok_s_est_basis"], "family-anchor")
        self.assertNotIn("tok_s_band", m)

    def test_窗内不足参照行数时什么都不给(self):
        m = self._lonely()
        got_est, got_band = F.estimate_missing_tok_s(self._peers(F.PEER_MIN_ROWS - 1) + [m])
        self.assertEqual((got_est, got_band), (0, 0))
        self.assertNotIn("tok_s_band", m, "凑不够参照行就退化成「无数据」占位，不能硬编一条带")

    def test_窗外模型不进带(self):
        far = [row(f"F{i}", index=self.BASE + F.PEER_WINDOW + 1, tok_s=500.0 + i,
                   series_key=f"far{i}") for i in range(3)]
        m = self._lonely()
        F.estimate_missing_tok_s(self._peers() + far + [m])
        self.assertEqual(m["tok_s_band"], [40.0, 54.0], "±PEER_WINDOW 之外的 500 tok/s 不该把带撑开")

    def test_同厂商参照单独计数(self):
        me = row("Home", creator="Acme", index=self.BASE, tok_s=99.0, series_key="home1")
        m = self._lonely()
        F.estimate_missing_tok_s(self._peers() + [me] + [m])
        self.assertEqual(m["tok_s_band"], [40.0, 99.0])
        self.assertEqual(m["tok_s_band_same_creator"], 1, "同厂商几个要如实报，读者据此判断这条带有多「外」")

    def test_没有智能指数就不可能有带(self):
        # 带是按指数开窗的：index 缺失时既没地方开窗，也不该被画在图上
        m = self._lonely(index=None)
        got_est, got_band = F.estimate_missing_tok_s(self._peers() + [m])
        self.assertEqual((got_est, got_band), (0, 0))
        self.assertNotIn("tok_s_band", m)

    def test_两类行互斥且计数分开(self):
        est = row("E", index=50, series_key="fam")
        est_kin = [row("E low", index=47, tok_s=30.0, series_key="fam"),
                   row("E high", index=52, tok_s=31.0, series_key="fam")]
        band = self._lonely()
        got = F.estimate_missing_tok_s(self._peers() + est_kin + [est, band])
        self.assertEqual(got, (1, 1), "返回的两个数就是图上的两类点，不能混")
        models = self._peers() + est_kin + [est, band]
        both = [m["name"] for m in models if m.get("tok_s_est") and m.get("tok_s_band")]
        self.assertEqual(both, [], "同一行不能既是点又是带")


class TestAlertPolicy(unittest.TestCase):
    """通知策略：正常更新不出声，异常才响。判据只在 alert_policy 一处。"""

    def _p(self, **kw):
        base = {"data_state": "ok", "generated_at": F.now_str(), "content_hash": "abc123",
                "data_note": ""}
        base.update(kw)
        return base

    def test_正常更新不出声(self):
        a = F.alert_policy(self._p())
        self.assertEqual(a["kind"], "fresh")
        self.assertEqual(a["key"], "abc123", "正常更新的去重键就是内容指纹")

    def test_降级要说且带原因(self):
        a = F.alert_policy(self._p(data_state="degraded", data_note="另有源缺失：OpenRouter 报价"))
        self.assertEqual(a["kind"], "degraded")
        self.assertIn("OpenRouter", a["text"])

    def test_异常文案不能是空的(self):
        """弹一条没有正文的通知等于没弹。"""
        for st in ("degraded", "stale", "empty", "从没见过的状态"):
            a = F.alert_policy(self._p(data_state=st, data_note=""))
            self.assertTrue(a["text"].strip(), f"{st} 的文案为空")

    def test_同一种异常一天只响一次(self):
        k1 = F.alert_policy(self._p(data_state="degraded"),
                            now=F.datetime(2026, 10, 2, 8, 0, tzinfo=F._TZ8))["key"]
        k2 = F.alert_policy(self._p(data_state="degraded", generated_at="2026-10-02 07:30:00"),
                            now=F.datetime(2026, 10, 2, 23, 0, tzinfo=F._TZ8))["key"]
        self.assertEqual(k1, k2, "同一天同一状态必须是同一个去重键")
        k3 = F.alert_policy(self._p(data_state="degraded"),
                            now=F.datetime(2026, 10, 3, 8, 0, tzinfo=F._TZ8))["key"]
        self.assertNotEqual(k1, k3, "换了一天要能再提醒")

    def test_载荷过期就是定时没跑(self):
        old = (F.datetime(2026, 10, 2, 7, 30, tzinfo=F._TZ8)
               - F.timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
        a = F.alert_policy(self._p(generated_at=old),
                           now=F.datetime(2026, 10, 2, 7, 30, tzinfo=F._TZ8))
        self.assertEqual(a["kind"], "not-running")
        self.assertIn("30", a["text"], "要说清多久没跑，别只说异常")

    def test_阈值两侧(self):
        """26 小时是"每天一次 + 一点余量"；边界必须可判定，不能两边都算正常。"""
        def at(hours):
            gen = (F.datetime(2026, 10, 2, 7, 30, tzinfo=F._TZ8)
                   - F.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
            return F.alert_policy(self._p(generated_at=gen),
                                  now=F.datetime(2026, 10, 2, 7, 30, tzinfo=F._TZ8))["kind"]
        self.assertEqual(at(25), "fresh")
        self.assertEqual(at(27), "not-running")

    def test_时间戳读不出来就不猜龄(self):
        a = F.alert_policy(self._p(generated_at="不是时间"))
        self.assertEqual(a["kind"], "fresh", "判不了龄就别编一个「过期」出来")

    def test_阈值和文案必须随载荷下发(self):
        """组件在**读取侧**算龄，所以阈值与文案模板必须出现在载荷里 ——
        否则"多久算没跑"就变成 Swift 里另写一份口径。"""
        a = F.alert_policy(self._p())
        self.assertEqual(a["kind"], "fresh")
        self.assertEqual(a["stale_after_hours"], F.STALE_ALERT_HOURS)
        self.assertIn("{h}", a["stale_text"], "模板要留占位符，让组件填自己算出来的小时数")

    def test_生产者侧那条算龄分支单独问才有效(self):
        """记录我自己犯过的错：爬虫调 alert_policy 时载荷刚生成，龄永远≈0，
        所以"定时没跑"只能在读取侧判。这条测试锁住这个事实 —— 谁要是把它当成
        唯一的检查点，就会发现它永远不触发。"""
        just_now = F.alert_policy(self._p())
        self.assertEqual(just_now["kind"], "fresh", "刚生成的载荷不可能算出过期")
        asked_later = F.alert_policy(self._p(), now=_NOW8 + F.timedelta(hours=48))
        self.assertEqual(asked_later["kind"], "not-running", "同一份载荷换个 now 就该报没跑")


class TestSeriesGap(unittest.TestCase):
    """整族缺口：上一轮 ≥GAP_MIN_ROWS 档、这一轮一档都不剩。

    为什么不用"AA 总行数骤降"当判据：台账实测十天里就在 113–123 之间漂，
    拿总数设阈值要么天天响、要么永远不响（P-28 里"整族没抓到却一声不吭"是这个形态）。
    """

    def _sol(self, n=6, series="GPT-6 Sol"):
        tiers = ["non-reasoning", "low", "medium", "high", "xhigh", "max"][:n]
        return [row(f"{series} ({t})", creator="OpenAI", index=30 + i, series_key="gpt6sol")
                for i, t in enumerate(tiers)]

    def test_六档全没_报一条(self):
        g = F.series_gap(self._sol(), [])
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["label"], "GPT-6 Sol")
        self.assertEqual(g[0]["n_prev"], 6)
        self.assertEqual(g[0]["n_now"], 0)

    def test_还剩一档就不算整族不见(self):
        """剩一档可能是改名/换代次，那是变动卡的话事；图上"线断了"才是这条判据的对象。"""
        self.assertEqual(F.series_gap(self._sol(), [self._sol()[2]]), [])

    def test_上一轮不足三档_不报(self):
        self.assertEqual(F.series_gap(self._sol(n=2), []), [])
        self.assertEqual(len(F.series_gap(self._sol(n=3), [])), 1, "边界：正好 3 档要报")

    def test_没有基线就说没有可比而不是谁没了(self):
        self.assertEqual(F.series_gap([], self._sol()), [])
        self.assertEqual(F.series_gap([{"name": ""}], self._sol()), [], "空名字不能自成一族")

    def test_同族不同档位归到一条(self):
        prev = self._sol() + [row(f"MiMo-V2.6-Pro ({t})", creator="Xiaomi", series_key="mimo26pro")
                              for t in ("low", "medium", "max")]
        cur = [row("GPT-6 Sol (low)", creator="OpenAI", series_key="gpt6sol")]
        g = F.series_gap(prev, cur)
        self.assertEqual([e["label"] for e in g], ["MiMo-V2.6-Pro"],
                         "MiMo 三档全没 → 报；Sol 还剩一档，不算整族不见")

    def test_按消失的档位数降序(self):
        big = self._sol(n=5, series="Alpha 9") + self._sol(n=3, series="Beta 9")
        g = F.series_gap(big, [])
        self.assertEqual([e["n_prev"] for e in g], [5, 3])

    def test_判据只看名字里的族_不依赖基线存了哪些字段(self):
        """基线只存 name/creator + 比的那几个数，没有 series_key —— 族名必须能从名字现推。"""
        prev = [{"name": m["name"], "creator": m["creator"]} for m in self._sol()]
        self.assertEqual(len(F.series_gap(prev, [])), 1)


class TestGoneAlert(unittest.TestCase):
    """整族缺口怎么出声：优先级、文案、去重键，判据仍只在 alert_policy 一处。"""

    def _p(self, **kw):
        base = {"data_state": "ok", "generated_at": F.now_str(), "content_hash": "abc123",
                "data_note": "", "series_gaps": []}
        base.update(kw)
        return base

    def _g(self, label="GPT-6 Sol", n=6):
        return [{"key": "gpt6sol", "label": label, "creator": "OpenAI", "n_prev": n, "n_now": 0}]

    def test_族没了要响(self):
        a = F.alert_policy(self._p(series_gaps=self._g()))
        self.assertEqual(a["kind"], "series-gone")
        self.assertIn("GPT-6 Sol", a["text"])
        self.assertIn("6 档", a["text"], "要说清消失了几档，否则读者无从判断严重程度")
        self.assertTrue(a["key"].startswith("alert:series-gone:"))

    def test_没有缺口时不许凭空报(self):
        for gaps in ([], None, [{"label": ""}], ["不是字典"], [{"n_prev": 3}]):
            a = F.alert_policy(self._p(series_gaps=gaps))
            self.assertEqual(a["kind"], "fresh", f"gaps={gaps} 时不该响")

    def test_源状态优先_一次只响一声(self):
        a = F.alert_policy(self._p(data_state="stale", data_note="主源没抓到",
                                   series_gaps=self._g()))
        self.assertEqual(a["kind"], "stale", "主源都在读快照时，族缺口是同一件事的另一面，不重复弹")

    def test_族缺口优先于太久没更新(self):
        """两条都成立时选信息量更大的那条，并且不能被算龄分支改写。"""
        old = (F.datetime(2026, 10, 2, 7, 30, tzinfo=F._TZ8)
               - F.timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
        a = F.alert_policy(self._p(generated_at=old, series_gaps=self._g()),
                           now=F.datetime(2026, 10, 2, 7, 30, tzinfo=F._TZ8))
        self.assertEqual(a["kind"], "series-gone")

    def test_多条族收成一条文案且有上限(self):
        gaps = self._g() + self._g("MiMo-V2.6-Pro", 4) + self._g("Alpha 9", 3) \
            + self._g("Beta 9", 3) + self._g("Gamma 9", 3)
        a = F.alert_policy(self._p(series_gaps=gaps))
        self.assertIn("5 条能力线", a["text"])
        self.assertIn("等 5 条", a["text"], "超过 3 条要收口，不能把通知写成清单")
        self.assertLessEqual(len(a["text"]), 200)

    def test_同一天只响一次_换天再提醒(self):
        k1 = F.alert_policy(self._p(series_gaps=self._g()),
                            now=F.datetime(2026, 10, 2, 8, 0, tzinfo=F._TZ8))["key"]
        k2 = F.alert_policy(self._p(series_gaps=self._g("Alpha 9", 3)),
                            now=F.datetime(2026, 10, 2, 23, 0, tzinfo=F._TZ8))["key"]
        self.assertEqual(k1, k2, "同一种异常一天只响一次（族名并进当天那条文案，不另起一声）")
        self.assertNotEqual(k1, F.alert_policy(self._p(series_gaps=self._g()),
                             now=F.datetime(2026, 10, 3, 8, 0, tzinfo=F._TZ8))["key"])

    def test_正常更新仍然只用内容指纹(self):
        a = F.alert_policy(self._p())
        self.assertEqual((a["kind"], a["key"]), ("fresh", "abc123"))


class TestFactsheet(unittest.TestCase):
    def test_事实卡里的数字都能在文本里用(self):
        """factsheet 是唯一的事实来源：LLM 只许搬运，不许自己算。"""
        changes = [{"name": "Alpha 3", "type": "changed", "fields": [
                       {"field": "index", "label": "智能指数", "kind": "changed",
                        "from": 43, "to": 47, "delta": 4, "pct": 9.3}]},
                   {"name": "Beta", "type": "new", "fields": []}]
        facts = S.build_factsheet(changes, generated_at="2026-10-01 07:30")
        self.assertIn("Alpha 3", facts)
        self.assertIn("9.3", facts)
        self.assertIn("Beta", facts)          # 新上榜也要进卡，模型不该自己数
        self.assertTrue(S.numbers_in(facts).issuperset({"43", "47", "9.3"}))

    def test_没有变动就不出事实卡(self):
        self.assertEqual(S.build_factsheet([], generated_at="x"), "")

    def test_有周变化时它排在前面且当日条目被收窄(self):
        """周变化才是读者想知道的；不给收窄的话事实卡会长到模型自己去补没看到的条目。"""
        changes = [{"name": f"M{i}", "type": "changed", "fields": [
                       {"field": "index", "label": "智能指数", "kind": "changed",
                        "from": 40, "to": 41, "delta": 1, "pct": 2.5}]} for i in range(9)]
        weekly = [{"name": "Week 1", "field": "tok_s", "label": "输出速度",
                   "from": 60, "to": 90, "pct": 50.0}]
        facts = S.build_factsheet(changes, generated_at="2026-10-01 07:30", weekly=weekly)
        self.assertLess(facts.index("Week 1"), facts.index("M0"))
        self.assertIn("近 7 天变化最大的 1 项", facts)

    def test_改名只进一句且不冒充新上榜(self):
        """2026-11-06 用户 case：AA 去掉 with fallback，事实卡不能再让模型把它说成换榜。"""
        changes = [
            {"name": "Claude Opus 5.5 (max)", "old_name": "Claude Opus 5.5 (max with fallback)",
             "creator": "Anthropic", "type": "renamed", "quiet": True, "fields": []},
            {"name": "Claude Opus 5.5 (low)", "old_name": "Claude Opus 5.5 (low with fallback)",
             "creator": "Anthropic", "type": "renamed", "quiet": True, "fields": []},
            {"name": "Beta", "type": "new", "fields": []},
        ]
        facts = S.build_factsheet(changes, generated_at="2026-11-06 08:19")
        self.assertIn("改名 2", facts)
        self.assertIn("Claude Opus 5.5：2 档改名", facts)
        self.assertIn("未达变化门槛", facts)
        self.assertIn("新上榜：Beta", facts)
        self.assertNotIn("新上榜：Claude Opus", facts)
        self.assertNotIn("掉榜：Claude Opus", facts)


class TestWeekMoves(unittest.TestCase):
    """series 表以前只写不读，这条判据就是它的读者存在的证据。"""

    def setUp(self):
        import sqlite3
        self.con = sqlite3.connect(":memory:")
        self.con.execute("CREATE TABLE series(ts TEXT, name TEXT, field TEXT, value REAL,"
                         " evaluation_version TEXT)")

    def _put(self, ts, name, field, value, version="5.0"):
        self.con.execute("INSERT INTO series(ts,name,field,value,evaluation_version) "
                         "VALUES(?,?,?,?,?)", (ts, name, field, value, version))

    def test_窗口内最早与最晚比(self):
        self._put("2026-09-24 07:30:00", "A", "tok_s", 60)
        self._put("2026-09-27 07:30:00", "A", "tok_s", 75)     # 中间点不该影响结果
        self._put("2026-10-01 07:30:00", "A", "tok_s", 90)
        w = S.week_moves(self.con)
        self.assertEqual(len(w), 1)
        self.assertEqual((w[0]["from"], w[0]["to"], w[0]["pct"]), (60.0, 90.0, 50.0))

    def test_只有一份快照时返回空(self):
        self._put("2026-10-01 07:30:00", "A", "tok_s", 90)
        self.assertEqual(S.week_moves(self.con), [], "首装当天没有周变化，不能编一个出来")

    def test_零基数的不算(self):
        self._put("2026-09-24 07:30:00", "A", "apt_usd", 0)
        self._put("2026-10-01 07:30:00", "A", "apt_usd", 0.5)
        self.assertEqual(S.week_moves(self.con), [], "除以零算百分比，宁可不算")

    def test_指数与成本只比同一非空版本(self):
        """跨版本的分差不是能力变化；未知版本同样不能拿来算。"""
        self._put("2026-09-24 07:30:00", "A", "index", 40, "5.0")
        self._put("2026-10-01 07:30:00", "A", "index", 44, "5.0")
        self._put("2026-09-24 07:30:00", "B", "apt_usd", 1.0, "5.0")
        self._put("2026-10-01 07:30:00", "B", "apt_usd", 1.2, "5.0")
        got = {(d["name"], d["field"]): d for d in S.week_moves(self.con)}
        self.assertEqual((got[("A", "index")]["from"], got[("A", "index")]["to"]), (40.0, 44.0))
        self.assertEqual((got[("B", "apt_usd")]["from"], got[("B", "apt_usd")]["to"]),
                         (1.0, 1.2))

    def test_跨版本或未知版本的指数与成本不算(self):
        self._put("2026-09-24 07:30:00", "A", "index", 40, "5.0")
        self._put("2026-10-01 07:30:00", "A", "index", 44, "4.3")
        self._put("2026-09-24 07:30:00", "B", "apt_usd", 1.0, "5.0")
        self._put("2026-10-01 07:30:00", "B", "apt_usd", 1.2, None)
        self._put("2026-09-24 07:30:00", "C", "tok_s", 60, "5.0")
        self._put("2026-10-01 07:30:00", "C", "tok_s", 90, "4.3")
        got = {(d["name"], d["field"]) for d in S.week_moves(self.con)}
        self.assertNotIn(("A", "index"), got, "5.0→4.3 的指数差不能当周变化")
        self.assertNotIn(("B", "apt_usd"), got, "一侧未知版本的成本差不能当周变化")
        self.assertIn(("C", "tok_s"), got, "速度不受评测版本门控，跨版本仍应照常比较")


class TestCrossCheck(unittest.TestCase):
    """整个方案唯一的支点：文本里的数字必须都能在事实卡里找到，否则丢弃。"""

    def setUp(self):
        self.facts = "智能指数 43→47（+9.3%）；每任务成本 $0.25→$0.19（-24.0%）；新上榜 3 个"

    def test_搬运事实_通过(self):
        text = "Alpha 3 智能指数从 43 升到 47，涨幅 9.3%；成本下降 24.0%。另外 3 个模型新上榜。"
        self.assertEqual(S.check_numbers(text, self.facts), [])

    def test_四舍五入与去尾零都算正当写法(self):
        self.assertEqual(S.check_numbers("指数 43→47，涨 9%；成本降 24%。", self.facts), [])

    def test_编造数字_被抓(self):
        text = "整体吞吐提升 15.5%，其中 128 分最高。"
        bad = S.check_numbers(text, self.facts)
        self.assertTrue(bad, "编造的数字必须被列出来")
        self.assertIn("15.5", bad)

    def test_不留相对容差(self):
        """曾经写成 abs 差 2% 以内放过：那 100% 被模型写成 102% 就查不出来了。"""
        self.assertEqual(S.check_numbers("涨了 102%。", "涨了 100%"), ["102"])

    def test_中文序数与年份不算数字(self):
        """否则"第三次""2026 年"这类会被误判成编造，判据就废了。"""
        text = "这是 2026 年第三次出现这种变化。"
        self.assertEqual(S.check_numbers(text, self.facts + " 2026 三"), [])


class TestLedgerRetention(unittest.TestCase):
    """台账清理与基线迁移：这类代码平时不动，一旦错了丢的是不可再生的历史。"""

    def setUp(self):
        import sqlite3
        self.con = sqlite3.connect(":memory:")
        self.con.execute("""CREATE TABLE snapshots(ts TEXT NOT NULL, source TEXT NOT NULL,
                            n_rows INTEGER, ok INTEGER, err TEXT, payload TEXT,
                            evaluation_version TEXT, PRIMARY KEY (ts, source))""")
        self.con.execute("""CREATE TABLE series(ts TEXT NOT NULL, name TEXT NOT NULL,
                            field TEXT NOT NULL, value REAL, evaluation_version TEXT,
                            PRIMARY KEY (ts, name, field))""")

    def _snap(self, ts, source="aa"):
        self.con.execute("INSERT OR REPLACE INTO snapshots"
                         "(ts,source,n_rows,ok,err,payload,evaluation_version) "
                         "VALUES(?,?,?,1,NULL,NULL,NULL)", (ts, source, 1))
        self.con.execute("INSERT OR REPLACE INTO series"
                         "(ts,name,field,value,evaluation_version) VALUES(?,?,?,?,NULL)",
                         (ts, "A", "index", 40))

    def test_按天保留而不是按ts条数(self):
        """一次抓取留下 AA/OpenRouter/基线 三个 ts；拿 ts 条数当"次数"会把保留 90 次缩成约 30 次。"""
        self._snap("2026-06-01 07:30:00"); self._snap("2026-06-01 07:30:04")
        self._snap("2026-06-01 07:30:09")            # 同一天三个 ts
        self._snap("2026-09-30 07:30:00")            # 最新
        n_snap, n_series = F.prune_history(self.con, keep_days=90)
        self.assertEqual(n_snap, 3, "只该清掉 6 月那三个 ts")
        left = [r[0] for r in self.con.execute("SELECT DISTINCT ts FROM snapshots")]
        self.assertEqual(left, ["2026-09-30 07:30:00"])
        self.assertEqual(n_series, 3)

    def test_空库不动手(self):
        self.assertEqual(F.prune_history(self.con, keep_days=90), (0, 0))


class TestDiffBaseline(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.con = sqlite3.connect(":memory:")
        self.con.execute("""CREATE TABLE snapshots(ts TEXT NOT NULL, source TEXT NOT NULL,
                            n_rows INTEGER, ok INTEGER, err TEXT, payload TEXT,
                            evaluation_version TEXT, PRIMARY KEY (ts, source))""")

    def _models(self):
        return [row("A", index=45, apt_usd=0.2, in_per_1m_usd=3.0,
                    out_per_1m_usd=15.0, tok_s=90)]

    def test_基线是合并结果而不是AA原始行(self):
        """回归：基线取 AA 源行时没有 in/out 单价，每次抓取都把几十个模型的价格
        报成"从无到有"（142 条假变动）。换成合并结果后价格第一次出现只报一次。

        load_previous 会按当前 watchlist 再筛一遍基线（历史上存过未过滤的行，否则
        没关注的行会被报成「消失」），所以这里给一份不设关键词的临时配置。
        """
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "cfg.json"
            cfg.write_text('{"keyword_include": [], "keyword_exclude": []}', encoding="utf-8")
            with unittest.mock.patch.object(F, "CFG_PATH", cfg):
                F.save_diff_base(self.con, self._models())
                prev, first = F.load_previous(self.con)
        self.assertFalse(first)
        self.assertEqual(prev["A"]["in_per_1m_usd"], 3.0, "基线里必须带价格")
        ch = F.diff_models(self._models(), prev)
        self.assertEqual(ch, [], "完全一样就该零变动")

    def test_没有基线时报告首次而不是把所有模型报成新进榜(self):
        prev, first = F.load_previous(self.con)
        self.assertEqual(prev, {})
        self.assertTrue(first, "run() 靠这个标志跳过本轮 diff")


class TestSchemaMigration(unittest.TestCase):
    def test_旧主键迁移后同秒多源不再互相覆盖(self):
        """旧表 ts 单列主键：AA 与 OpenRouter 同一秒写入，后一条 INSERT OR REPLACE
        把前一条静默删掉 —— 表现为降级读不到快照，且没有任何报错。"""
        import sqlite3
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.db"
            old = sqlite3.connect(db)
            old.execute("""CREATE TABLE snapshots(ts TEXT PRIMARY KEY, source TEXT NOT NULL,
                           n_rows INTEGER, ok INTEGER, err TEXT, payload TEXT)""")
            old.execute("INSERT INTO snapshots VALUES('2026-09-01 07:30:00','aa',5,1,NULL,'[]')")
            old.execute("INSERT INTO snapshots VALUES('2026-09-01 07:30:01','openrouter',7,1,NULL,'[]')")
            old.commit(); old.close()
            with unittest.mock.patch.object(F, "DB_PATH", str(db)):
                con = F.ensure_db()
                rows = con.execute("SELECT source FROM snapshots ORDER BY source").fetchall()
                self.assertEqual([r[0] for r in rows], ["aa", "openrouter"], "两份历史都得留下")
                for table in ("snapshots", "series"):
                    columns = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                    self.assertIn("evaluation_version", columns,
                                  f"{table} 迁移后缺评测版本列，跨版本历史会混在一起")
                con.execute("INSERT INTO snapshots"
                            "(ts,source,n_rows,ok,err,payload,evaluation_version) "
                            "VALUES('2026-10-01 07:30:00','aa',1,1,NULL,NULL,NULL)")
                con.execute("INSERT INTO snapshots"
                            "(ts,source,n_rows,ok,err,payload,evaluation_version) "
                            "VALUES('2026-10-01 07:30:00','openrouter',1,1,NULL,NULL,NULL)")
                con.commit()
                n = con.execute("SELECT COUNT(*) FROM snapshots WHERE ts='2026-10-01 07:30:00'").fetchone()[0]
                self.assertEqual(n, 2, "迁移后同一秒写两个源不再互相覆盖")
                con.close()


class TestOrMatch(unittest.TestCase):
    """or_match 曾经返回 id 字符串而不是那个 dict（元组加排序键后下标右移，
    `cands[0][3]` 从 info 变成 oid），merge() 里一个 .get 就 AttributeError 整轮抓取崩掉。"""

    ORM = {
        "anthropic/claude-opus-5": {"id": "anthropic/claude-opus-5", "in_per_1m_usd": 15.0,
                                     "out_per_1m_usd": 75.0},
        "anthropic/claude-opus-5-reseller": {"id": "anthropic/claude-opus-5-reseller",
                                             "in_per_1m_usd": 99.0, "out_per_1m_usd": 99.0},
        "upstage/solar-x": {"id": "upstage/solar-x", "in_per_1m_usd": 1.0, "out_per_1m_usd": 2.0},
    }

    def test_返回的是带价格的dict不是id字符串(self):
        got = F.or_match(self.ORM, {"name": "Claude Opus 5", "creator": "Anthropic"})
        self.assertIsInstance(got, dict, "返回字符串就是那次错位 bug 本身")
        self.assertEqual(got["in_per_1m_usd"], 15.0)

    def test_并列时取最短那个(self):
        got = F.or_match(self.ORM, {"name": "Claude Opus 5", "creator": "Anthropic"})
        self.assertEqual(got["id"], "anthropic/claude-opus-5", "reseller 命名的托管不能抢走正主")

    def test_厂商对不上就不给匹配(self):
        self.assertIsNone(F.or_match(self.ORM, {"name": "Claude Opus 5", "creator": "Google"}))

    def test_完全无候选返回None(self):
        self.assertIsNone(F.or_match(self.ORM, {"name": " unrelated model", "creator": "Nothing"}))


class TestCliWiring(unittest.TestCase):

    def test_远端来的模型名不能变成命令(self):
        """提示词是一个独立 argv：把事实卡拼进 bash -c 就是命令注入的入口，
        而事实卡里的模型名来自远端页面，今天可以是 "GPT-6 Sol"，明天就能是 "$(…)"。"""
        hostile = ' ($(touch /tmp/mr_pwned) 版本) 涨了 9.3%'
        argv = S.build_argv('bash ~/some/readonly-cli -C {dir} -o {out} --timeout {timeout}',
                             Path("/tmp/w"), Path("/tmp/w/o.md"), 90, hostile)
        self.assertEqual(argv[-1], hostile, "提示词必须整段作为最后一个参数")
        self.assertNotIn("-c", argv, "不该经 shell 解释")
        self.assertFalse(Path("/tmp/mr_pwned").exists(), "参数不该被执行")

    def test_占位符被替换且波浪号展开(self):
        argv = S.cli_argv("my-cli -C {dir} -o {out} --timeout {timeout}",
                          Path("/tmp/w"), Path("/tmp/w/o.md"), 90)
        self.assertEqual(argv, ["my-cli", "-C", "/tmp/w", "-o", "/tmp/w/o.md", "--timeout", "90"])

    def test_没配命令时不做总结而不是报错(self):
        """把运行时目录指到空临时目录：否则这条在本机会真去调 CLI（慢、且依赖网络）。"""
        import os
        with tempfile.TemporaryDirectory() as td:
            with unittest.mock.patch.object(S.mr_paths, "SUPPORT", Path(td)), \
                 unittest.mock.patch.object(S.mr_paths, "DATA", Path(td) / "data"):
                os.environ.pop("MODELRADAR_SUMMARY_CMD", None)
                res = S.run_summary({"changes": [{"name": "A", "type": "new", "fields": []}],
                                     "generated_at": "2026-10-01 07:30"}, log=lambda *a: None)
        self.assertFalse(res["written"])
        self.assertEqual(res["state"], "no-cli", "没配命令要走「没有」这条分支，不是报错、也不是假装生成了")




class TestScatterFrontier(unittest.TestCase):
    """渲染面那条"带/推估不进前沿"必须有被观察到的机会。

    只用当天真实载荷不够：那天的带点恰好在"成本-分数"两轴上都被别家支配，
    忘了排除既不改变金线的点数也不改变坐标，判据全程空转（这点是变异实测抓出来的）。
    这里摆一个必然支配全场的位置，让这条排除真的有事可管。
    """

    def _load(self, models):
        """夹具走生产路径：前沿成员/梯队/图例顺序都由爬虫算，渲染面只消费。

        旧写法是"把裸模型写进 latest.json 让 render 自己扫支配关系"，现在那段扫描已经收敛
        到 fetch —— 所以这里必须先 annotate，否则渲染面按新代码会拒绝渲染（那条就是防空转的闸）。
        """
        bands = F.tier_ladder(models)
        F.annotate_efficiency(models, bands)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "latest.json"
            p.write_text(json.dumps({"generated_at": "2026-10-07 07:30", "models": models,
                                     "legend": F.build_legend(models, bands),
                                     "tier_bands": bands,
                                     "tier_tolerance_points": F.TIER_TOLERANCE_POINTS},
                                    ensure_ascii=False), encoding="utf-8")
            with unittest.mock.patch.object(R, "LATEST", p):
                return R.load_points()

    def _models(self):
        return [
            row("A", index=40, apt_usd=0.2, tok_s=100.0, series_key="aa1"),
            row("B", index=45, apt_usd=1.0, tok_s=60.0, series_key="bb1"),
            row("C", index=50, apt_usd=0.5, tok_s=30.0, series_key="cc1"),
            # 最便宜 + 最高分：不排除就一定进金线
            row("Band", index=70, apt_usd=0.01, tok_s=None, series_key="dd1",
                tok_s_band=[150.0, 200.0], tok_s_band_n=3, tok_s_band_same_creator=0,
                tok_s_band_note="同级能力参照：刻意不给中心值"),
            row("Est", index=60, apt_usd=0.02, tok_s=None, series_key="ee1",
                tok_s_est=55.0, tok_s_est_range=[20.0, 40.0], tok_s_est_basis="series-interp",
                tok_s_est_n=2, tok_s_est_note="估计：同族相邻档插值"),
        ]

    def test_档位连线保留分数倒挂(self):
        tiers = ["non-reasoning", "low", "medium", "high", "xhigh", "max"]
        models = [row("Terra (" + t + ")", creator="OpenAI",
                      series_key="terra", series="Terra", tier=t,
                      index=s, apt_usd=i + 1, tok_s=91)
                  for i, (t, s) in enumerate(zip(tiers, [30, 29, 38, 42, 45, 51]))]
        pts, front, order = TestLegendHover()._load(list(reversed(models)))
        svg = R.build_svg(pts, front, order)
        line = re.search(r'<polyline class="ln"[^>]*points="([^"]+)"', svg)[1]
        coords = [tuple(map(float, p.split(","))) for p in line.split()]
        # 成本逐档增加，故正确顺序的 x 递增；首段 y 必须向下走。
        self.assertEqual([p[0] for p in coords], sorted(p[0] for p in coords))
        self.assertGreater(coords[1][1], coords[0][1])
        self.assertEqual(R.tier_rank("low with fallback"), R.tier_rank("low"))
        self.assertLess(R.tier_rank("minimal"), R.tier_rank("low"))
        self.assertEqual(R.tier_rank("default"), 99)

    def test_frontier_note_marks_only_highest_frontier_tier_per_release(self):
        models = [row(f"{series} ({tier})", series=series, series_key=series, tier=tier,
                      index=score, apt_usd=cost, tok_s=90)
                  for series, tier, score, cost in [("A", "low", 10, 1),
                      ("A", "high", 20, 2), ("A", "max", 25, 4), ("B", "max", 30, 3)]]
        pts, front, order = self._load(models)
        svg = R.build_svg(pts, front, order)
        self.assertEqual({p["n"] for p in front}, {"A (low)", "A (high)", "B (max)"})
        self.assertEqual(set(re.findall(r'<text data-note-for="[^"]+"[^>]*data-effort-view="discrete"[^>]*>([^<]+)</text>', svg)),
                         {"A (high)", "B (max)"})

    def test_frontier_notes_fall_back_then_use_outward_leader(self):
        def p(k, t, x, y):
            return {"k": k, "t": t, "x": x, "y": y, "n": f"{k} Model ({t})", "r": 5}
        upper = p("A", "high", 310, 200)
        close = p("B", "high", 316, 197)
        lower = p("B", "medium", 180, 160)
        px, py = lambda x: x, lambda y: 400 - y
        notes = R.frontier_notes([lower, close, upper], px, py)
        self.assertEqual([n[0] for n in notes], [upper, lower])
        notes = R.frontier_notes([close, upper], px, py)
        self.assertEqual([n[0] for n in notes], [upper, close])
        self.assertLess(notes[0][1], px(upper["x"]))
        self.assertLess(notes[0][2], py(upper["y"]))
        self.assertGreater(notes[1][1], px(close["x"]))
        self.assertGreater(notes[1][2], py(close["y"]))

    def test_effort_view_skips_covered_release_but_keeps_inner_tier(self):
        models = [row(f"{series} ({tier})", creator="OpenAI", series=series,
                      series_key=series, tier=tier, index=score, apt_usd=cost, tok_s=90)
                  for series, tier, score, cost in [
                      ("GPT-6.2 Sol", "low", 41, .18), ("GPT-6.1 Sol", "medium", 46, .36),
                      ("GPT-6.2 Sol", "medium", 53, .41), ("GPT-6.2 Sol", "high", 55, .56),
                      ("GPT-6.2 Sol", "xhigh", 56, .65), ("GPT-6.2 Sol", "max", 57, 1.01),
                      ("Unrelated", "low", 48, .38)]]
        pts, front, order = self._load(models)
        svg = R.build_svg(pts, front, order)
        mode_points = {mode: coords for coords, mode in
                       re.findall(r'<polyline points="([^"]*)" data-effort-view="([^"]+)"', svg)}
        self.assertEqual(len(mode_points["discrete"].split()), 7)
        self.assertGreater(len(mode_points["smooth"].split()), 6)
        marks = {name: f"{x},{y}" for name, x, y in
                 re.findall(r'data-n="([^"]+)" data-cx="([^"]+)" data-cy="([^"]+)"', svg)}
        self.assertIn(marks["GPT-6.2 Sol (high)"], mode_points["smooth"].split())
        self.assertNotIn(marks["GPT-6.1 Sol (medium)"], mode_points["smooth"].split())
        self.assertRegex(svg, r'data-effort-view="discrete"[^>]+>GPT-6\.1 Sol \(medium\)</text>')
        self.assertEqual(svg.count('data-role="node"'), 7)

    def test_effort_curve_preserves_opening_straight_segment_and_foldbacks(self):
        tiers = [{"x": x, "y": y, "t": t} for x, y, t in [
            (.5, 25, "non-reasoning"), (.2, 30, "low"), (.3, 28, "medium"), (.7, 45, "max")]]
        curve = R.effort_envelope(tiers)
        self.assertEqual(curve[:2], tiers[:2])
        self.assertEqual([p for p in curve if "t" in p], tiers)
        self.assertGreater(len(curve), len(tiers))

    def test_effort_frontier_reconsiders_points_dominated_by_a_covered_tier(self):
        models = [row(f"{series} ({tier})", creator="OpenAI", series=series,
                      series_key=series, tier=tier, index=score, apt_usd=cost, tok_s=90)
                  for series, tier, score, cost in [
                      ("GPT-6.2 Sol", "low", 41, .18), ("GPT-6.2 Sol", "medium", 53, .41),
                      ("GPT-6.1 Sol", "medium", 46, .36), ("GPT-6.1 Terra", "xhigh", 45, .40)]]
        pts, front, order = self._load(models)
        svg = R.build_svg(pts, front, order)
        self.assertNotIn("GPT-6.1 Terra (xhigh)", {p["n"] for p in front})
        self.assertRegex(svg, r'data-effort-view="smooth"[^>]+>GPT-6\.1 Terra \(xhigh\)</text>')
        self.assertNotRegex(svg, r'data-effort-view="smooth"[^>]+>GPT-6\.1 Sol \(medium\)</text>')

    def test_history_matches_nearest_predecessor_within_major_version(self):
        groups = {v: [{"s": f"Claude Fable {v}", "c": "Anthropic", "carry": date}]
                  for v, date in [("4.9", "2026-11-01"), ("5", "2026-11-05"),
                                  ("5.1", "2026-11-09"), ("5.5", ""), ("5.6", "")]}
        self.assertEqual(R.history_successors(groups), {"5.1": "5.5"})
        del groups["5.1"]
        self.assertEqual(R.history_successors(groups), {"5": "5.5"})

    def test_history_summary_uses_paired_medians_and_skips_mismatched_rows(self):
        old = [{"s": "GPT-6.1 Sol", "carry": "2026-11-17", "iv": "5.0.1", "t": tier,
                "y": score, "x": cost, "v": tps}
               for tier, score, cost, tps in [("low", 10, 1, 50), ("medium", 20, 4, 80), ("max", 30, 10, 90)]]
        new = [{"s": "GPT-6.2 Sol", "iv": version, "t": tier, "y": score, "x": cost, "v": tps}
               for tier, score, cost, tps, version in [("low", 13, 2, 60, "5.0.1"),
                   ("medium", 29, 2, None, "5.0.1"), ("max", 70, 30, 200, "6")]]
        self.assertEqual(R.history_comparison(old, new), [
            "旧版 GPT-6.1 Sol · 沿用 2026-11-17", "新版 GPT-6.2 Sol · 同档变化中位",
            "智能指数 15 → 21 · +6 分（2 档）", "每任务成本 2.5 → 2 · +25.0%（2 档）",
            "TPS 50 → 60 · +20.0%（1 档）"])

    def test_带与推估都被搬出金线(self):
        pts, front, _order = self._load(self._models())
        self.assertEqual({p["n"] for p in front}, {"A", "C"}, "只该剩下实测的那两个非支配点")
        self.assertNotIn("Band", {p["n"] for p in front})
        self.assertNotIn("Est", {p["n"] for p in front})

    def test_这个反例真的会进前沿_所以判据不是空转(self):
        pts, front, _order = self._load(self._models())
        naive: list[dict] = []
        for p in sorted(pts, key=lambda z: z["x"]):
            if not any(q["y"] >= p["y"] for q in naive):
                naive.append(p)
        self.assertEqual({q["n"] for q in naive}, {"Band"},
                         "带点摆在全场最优位；它没进这条「不设防」的前沿，本用例就证明不了任何事")
        self.assertNotEqual({q["n"] for q in naive}, {q["n"] for q in front},
                            "漏掉排除会实打实改变金线的形状")

    def test_带点上界参与规模上限_且中心用占位尺寸(self):
        pts, _, _order = self._load(self._models())
        band = next(p for p in pts if p["n"] == "Band")
        est = next(p for p in pts if p["n"] == "Est")
        real_a = next(p for p in pts if p["n"] == "A")
        # tmax 必须被带的上界 200 抬起来：否则真实点会被一个非实测值压扁/撑爆
        self.assertAlmostEqual(band["r_hi"], 15.0, places=9, msg="外环应等于 R_MAX（带的上界就是 tmax）")
        self.assertAlmostEqual(real_a["r"], round(15.0 * (100.0 / 200.0) ** 0.5, 1), places=9)
        # 带没有中心值 → 半径只能用占位那一号，不能按区间定径
        exp_node = math.ceil(15.0 * (30.0 / 200.0) ** 0.5) - 1   # 最小的实测气泡再低一号
        self.assertEqual(band["r"], float(exp_node), "带点必须用占位尺寸，不能按区间定径")
        self.assertTrue(band["band"] and not band["est"])
        self.assertAlmostEqual(band["r_lo"], round(15.0 * (150.0 / 200.0) ** 0.5, 1), places=9)

    def test_没有带字段时一切照旧(self):
        pts, front, _order = self._load([m for m in self._models() if m["name"] != "Band"])
        self.assertFalse(any(p["band"] for p in pts))
        self.assertEqual({p["n"] for p in front}, {"A", "C"})


class TestGapWiring(unittest.TestCase):
    """缺口必须从**抓取管线**里长出来，不能只在纯函数层面对。

    这里跑真的 parse_aa → want_row → merge → diff → series_gap → alert_policy → 落盘，
    只换掉两个口子：网络（fetch）与投递（notify_fresh / maybe_summarize）。
    把 payload 里那一行 `"series_gaps": …` 删掉，纯函数测试全绿也拦不住 —— 这条就是补那个面。
    """

    SOL = [f"GPT-6 Sol ({t})" for t in ("non-reasoning", "low", "medium", "high", "xhigh", "max")]

    def _aa(self, names):
        body = "".join(
            f"<tr><td>{n}</td><td>200K</td><td>OpenAI</td><td>{30 + i}</td>"
            f"<td>$0.{i + 1}0</td><td>{60 + i}</td><td>1.2</td><td>3.4</td></tr>"
            for i, n in enumerate(names))
        return aa_page(body)

    def _runs(self, pages):
        """依次用这几份 AA 页面跑 run()，每轮同时取回内存载荷与落盘那份。"""
        import mr_paths
        import json
        out = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            data.mkdir()
            cfg = root / "cfg.json"
            # watchlist 是"空列表=一个都不留"（want_row 末尾那个 any()），夹具必须自己给关键词
            cfg.write_text('{"keyword_include": ["GPT-6 Sol", "Alpha 9"], "keyword_exclude": []}',
                           encoding="utf-8")
            feed = {"i": 0}

            def fake_fetch(url):
                if url == F.AA_METHODOLOGY:
                    return b"Artificial Analysis Intelligence Index v5.0"
                page = pages[feed["i"]]
                feed["i"] += 1
                return page

            with support_path_stack(mr_paths, root), \
                 unittest.mock.patch.object(mr_paths, "SUPPORT", root), \
                 unittest.mock.patch.object(mr_paths, "DATA", data), \
                 unittest.mock.patch.object(mr_paths, "REPO_DATA", root / "repo"), \
                 unittest.mock.patch.object(F, "DB_PATH", data / "t.db"), \
                 unittest.mock.patch.object(F, "LATEST_JSON", data / "latest.json"), \
                 unittest.mock.patch.object(F, "LATEST_JS", data / "latest.js"), \
                 unittest.mock.patch.object(F, "CFG_PATH", cfg), \
                 unittest.mock.patch.object(F, "fetch", fake_fetch), \
                 unittest.mock.patch.object(F, "notify_fresh", lambda p: None), \
                 unittest.mock.patch.object(F, "maybe_summarize", lambda p: None):
                for _ in pages:
                    p = F.run(["aa"], False)
                    # 组件读的是磁盘上的 latest.json，不是函数返回值 —— 只在内存里对没用
                    disk = json.loads((data / "latest.json").read_text(encoding="utf-8"))
                    out.append({"payload": p, "disk": disk})
        return out

    def test_族整条消失时管线会把它带进载荷并落盘(self):
        first, second = self._runs([self._aa(self.SOL + ["Alpha 9"]), self._aa(["Alpha 9"])])
        self.assertEqual(first["payload"]["alert"]["kind"], "fresh", "首轮没有可比基线，不该报缺口")
        self.assertEqual(first["payload"]["series_gaps"], [])
        self.assertEqual(second["payload"]["data_state"], "ok", "缺口这条要独立于源状态")
        self.assertEqual([g["label"] for g in second["payload"]["series_gaps"]], ["GPT-6 Sol"])
        self.assertEqual(second["payload"]["series_gaps"][0]["n_prev"], 6)
        self.assertEqual(second["payload"]["alert"]["kind"], "series-gone")
        self.assertIn("GPT-6 Sol", second["payload"]["alert"]["text"])
        self.assertEqual(second["disk"]["series_gaps"], second["payload"]["series_gaps"])
        self.assertEqual(second["disk"]["alert"]["kind"], "series-gone",
                         "落盘那份没带上结论，组件就永远看不见这条")

    def test_同一个缺口只在出现那一轮报一次(self):
        """第二轮之后基线里已经没有这个族了，第三轮不该天天提醒同一件事。"""
        p1, p2, p3 = (r["payload"] for r in self._runs(
            [self._aa(self.SOL), self._aa(["Alpha 9"]), self._aa(["Alpha 9"])]))
        self.assertEqual(p2["alert"]["kind"], "series-gone")
        self.assertEqual(p3["series_gaps"], [], "缺口说的是「上一轮有、这一轮没」这个转义，不是长期状态")
        self.assertEqual(p3["alert"]["kind"], "fresh")

    def test_族还在但少了几档_不算整族缺口(self):
        """掉档是变动卡的逐条话事范围，这里报了就天天响（换代次、改价都会掉一两档）。"""
        p2 = self._runs([self._aa(self.SOL), self._aa(self.SOL[:3] + ["Alpha 9"])])[1]["payload"]
        self.assertEqual(p2["series_gaps"], [])
        self.assertEqual(p2["alert"]["kind"], "fresh")

    def test_版本从官方方法页取并写进每一行(self):
        """榜单页没有版本号；当前代次必须取自 AA_METHODOLOGY，而不是从日期或分数猜。"""
        p = self._runs([self._aa(self.SOL)])[0]["payload"]
        self.assertEqual(p["evaluation_version"], "5.0")
        self.assertEqual(p["sources"]["aa"]["evaluation_version"], "5.0")
        self.assertTrue(p["models"])
        self.assertTrue(all(m["evaluation_version"] == "5.0" for m in p["models"]))


class TestFetchBudget(unittest.TestCase):
    """抓取必须有**墙钟**上限：慢滴的上游不能把每日任务占住十几分钟。"""

    def _fetch(self, process_impl, **kw):
        args = dict(timeout=1, retries=2, budget=0.25, total_budget=0.5)
        args.update(kw)
        with unittest.mock.patch.object(F.subprocess, "run", side_effect=process_impl):
            return F.fetch("https://example.invalid/page", **args)

    def test_慢滴会被墙钟掐断并报出原因(self):
        """传输进程的截止包含响应头与正文，不靠块间检查。"""
        calls = []

        def impl(*a, **k):
            calls.append(1)
            raise F.subprocess.TimeoutExpired(a[0], k["timeout"])

        t0 = time.monotonic()
        with self.assertRaises(RuntimeError) as cm:
            self._fetch(impl)
        spent = time.monotonic() - t0
        self.assertIn("抓取失败", str(cm.exception))
        self.assertLess(spent, 6.0, f"封顶失效：耗时 {spent:.1f}s —— 慢滴没被掐住")

    def test_总时长用尽后不再重试(self):
        """旧写法 retries 次重试各能拖十几分钟；现在整轮只有一次封顶。"""
        calls = []

        def impl(*a, **k):
            calls.append(1)
            time.sleep(k["timeout"])
            raise F.subprocess.TimeoutExpired(a[0], k["timeout"])

        with self.assertRaises(RuntimeError):
            self._fetch(impl, budget=10.0, total_budget=0.3)   # 单次给 10s，但整轮只给 0.3s
        self.assertEqual(len(calls), 1, f"整轮上限到了还重试 {len(calls)} 次")

    def test_正常响应原样收全(self):
        self.assertEqual(self._fetch([F.subprocess.CompletedProcess([], 0, b"abcd", b"")]), b"abcd")

    def test_失败一次仍然会重试(self):
        """封顶不能把该有的退避重试一起砍掉（5xx 一次就放弃是另一种静默断档）。

        第一次失败会转入续传段（与真 curl 同形：正文落 `-o` 指定的文件，stdout 为空），
        所以夹具要把字节写进那个文件，才算验证了"重试真的发生了"。
        """
        calls = []

        def impl(args, **k):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("503 Service Unavailable")
            target = args[args.index("-o") + 1] if "-o" in args else None
            if target:
                Path(target).write_bytes(b"ok")
            return F.subprocess.CompletedProcess([], 0, b"", b"")

        with unittest.mock.patch.object(F.time, "sleep", lambda s: None):
            self.assertEqual(self._fetch(impl, budget=5.0, total_budget=9.0), b"ok")
        self.assertEqual(len(calls), 2)

    def test_上限是常量而不是散落的数字(self):
        sig = inspect.signature(F.fetch).parameters
        self.assertEqual(sig["budget"].default, F.FETCH_BUDGET)
        self.assertEqual(sig["total_budget"].default, F.FETCH_TOTAL_BUDGET)
        self.assertLessEqual(F.FETCH_BUDGET, F.FETCH_TOTAL_BUDGET)
        self.assertEqual(F.FETCH_TOTAL_BUDGET, F.SOURCE_NETWORK_BUDGET)
        self.assertLess(F.SOURCE_NETWORK_BUDGET, F.ROUND_NETWORK_BUDGET)
        # 整轮预算必须**小于组件外壳的超时**，否则外壳先杀进程，爬虫自己那句"预算用尽"
        # 永远不会出现，用户只看到"点了没反应"。
        # 原来这条写的是 `<= 180`：180 是 76bfacd 之前的实测值，那笔按"AA 榜单 1.76 MB
        # @ ~22 KB/s 续传"把整轮放宽到 240，却没同步这条判据 —— 于是任何机器上跑闸门都红，
        # 红的还不是预算错了，是判据的口径旧了。
        # 上限不写死数字，从 ChartWidget.swift 里读**跑爬虫那一条**的超时（`label: "crawl"`
        # 那次 runAsyncTask；组件里 regenerate 用的是另一个更小的值，取 min 会绑错对象）：
        # 以后再放宽整轮预算，就必然先撞上"外壳超时也得改"，而不是两边各说一套。
        shell_src = (Path(F.__file__).resolve().parent / "widget" / "ChartWidget.swift") \
            .read_text(encoding="utf-8")
        m = re.search(r'label:\s*"crawl".*?timeoutSec:\s*(\d+)', shell_src, re.S)
        self.assertTrue(m, "读不到外壳跑爬虫那条的超时，判据就空转了")
        self.assertLess(F.ROUND_NETWORK_BUDGET, int(m.group(1)))


class TestWatchlistGrow(unittest.TestCase):
    """词表增量：抓到当前词表盖不住的新家族，本轮就补词，且**只加不减**。

    单独立一层的原因：Haiku 5.5 那次漏捕是"词表 = 09-29 的一次性快照"造成的，而它
    在所有既有判据上都是绿的 —— 源状态 ok、缺口告警也不响（告警只认"上一轮有、
    这一轮没了"，新家族本来就没在上一轮）。所以这里既测纯函数，也测 run() 的钩子：
    补的词必须在**同一轮**就进图表，不是下一轮。
    """

    # 三个族、每族两档；族名各命中 2 行、跨 1 家、全在命名主体里 → 只有族名能过判据，
    # 档位词（air/gale…）各只命中 1 行，贪心覆盖里轮不到它们。
    ROWS = [("Lumo Air", "Acme"), ("Lumo Gale", "Acme"),
            ("Nexo Bolt", "Beta"), ("Nexo Dash", "Beta"),
            ("Piko Arc", "Gamma"), ("Piko Rim", "Gamma")]

    def setUp(self):
        """这个类盯的是**精确词表**，所以把仓库种子也搬到临时目录 ——
        否则将来有人在 `config/watchlist.json` 里钉一个词，这里所有列表断言会跟着漂。"""
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        seed = Path(d.name) / "seed.json"
        seed.write_text(json.dumps({"keyword_include": [], "keyword_exclude": [],
                                    "keyword_include_pinned": [],
                                    "vendor_brand_color_override": {}}), encoding="utf-8")
        p = unittest.mock.patch.object(F, "CFG_SEED", seed)
        p.start()
        self.addCleanup(p.stop)

    CFG = json.dumps({"keyword_include": ["lumo", "nexo"], "keyword_exclude": [],
                      "keyword_include_pinned": []}, ensure_ascii=False, indent=2) + "\n"

    def _rows(self, pairs=None):
        return [row(n, creator=c) for n, c in (pairs or self.ROWS)]

    def _cfg(self, td, include, **extra):
        cfg = Path(td) / "cfg.json"
        cfg.write_text(json.dumps({"keyword_include": include, **extra},
                                  ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return cfg

    def _aa(self, pairs):
        body = "".join(
            f"<tr><td>{n}</td><td>200K</td><td>{c}</td><td>{30 + i}</td>"
            f"<td>$0.{i + 1}0</td><td>{60 + i}</td><td>1.2</td><td>3.4</td></tr>"
            for i, (n, c) in enumerate(pairs))
        return aa_page(body)

    def _pipeline(self, cfg_text, pages, fetch=None, snapshot=None):
        """跑真的 run()，只换网络与投递两个口子，返回每轮的 (载荷, 日志)。"""
        import mr_paths
        logs: list[str] = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            data.mkdir()
            cfg = root / "cfg.json"
            cfg.write_text(cfg_text, encoding="utf-8")
            feed = {"i": 0}

            def fake_fetch(url):
                if url == F.AA_METHODOLOGY:
                    return b"Artificial Analysis Intelligence Index v5.0"
                if fetch is not None:
                    return fetch(url)
                page = pages[feed["i"]]
                feed["i"] += 1
                return page

            patches = [unittest.mock.patch.object(mr_paths, "SUPPORT", root),
                       *support_path_patches(mr_paths, root),
                       unittest.mock.patch.object(mr_paths, "DATA", data),
                       unittest.mock.patch.object(mr_paths, "REPO_DATA", root / "repo"),
                       unittest.mock.patch.object(F, "DB_PATH", data / "t.db"),
                       unittest.mock.patch.object(F, "LATEST_JSON", data / "latest.json"),
                       unittest.mock.patch.object(F, "LATEST_JS", data / "latest.js"),
                       unittest.mock.patch.object(F, "CFG_PATH", cfg),
                       unittest.mock.patch.object(F, "fetch", fake_fetch),
                       unittest.mock.patch.object(F, "log", lambda m: logs.append(m)),
                       unittest.mock.patch.object(F, "notify_fresh", lambda p: None),
                       unittest.mock.patch.object(F, "maybe_summarize", lambda p: None)]
            if snapshot is not None:
                rows, ts = snapshot
                patches.append(unittest.mock.patch.object(
                    F, "last_good_snapshot", lambda con, r=rows, t=ts: (r, t)))
            with contextlib.ExitStack() as st:
                for p in patches:
                    st.enter_context(p)
                rounds = []
                for _ in range(len(pages) if fetch is None else 1):
                    before = len(logs)
                    p = F.run(["aa"], False)
                    rounds.append((p, logs[before:], cfg.read_text(encoding="utf-8")))
            return rounds, cfg

    def test_新家族本轮补词并写回配置(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td, ["lumo", "nexo"])
            with unittest.mock.patch.object(F, "CFG_PATH", cfg):
                r = F.grow_watchlist(self._rows())
            self.assertEqual(r["added"], ["piko"])
            self.assertEqual((r["rows_before"], r["rows_after"]), (4, 6),
                             "补词必须真的多留出行，否则增量是空的")
            disk = json.loads(cfg.read_text(encoding="utf-8"))
            self.assertEqual(disk["keyword_include"], ["lumo", "nexo", "piko"])
            meta = disk["_watchlist_derived"]
            self.assertEqual(meta["mode"], "incremental")
            self.assertEqual(meta["added"], ["piko"])
            self.assertEqual(meta["criteria"]["max_creator_spread"], F.MAX_CREATOR_SPREAD)
            self.assertEqual(meta["criteria"]["rows_total"], 6)

    def test_只加不减_derive选不上的旧词照样留着(self):
        """'gale' 在派生里落选（族名已盖住它），但删词是判断不是度量 —— grow 不能顺手减。"""
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td, ["lumo", "nexo", "gale"])
            with unittest.mock.patch.object(F, "CFG_PATH", cfg):
                r = F.grow_watchlist(self._rows())
            self.assertEqual(r["added"], ["piko"])
            self.assertIn("gale", r["new"], "增量把非派生词冲掉了 —— 那是整表替换的行为")
            self.assertIn("gale", json.loads(cfg.read_text(encoding="utf-8"))["keyword_include"])

    def test_没有新家族时不落盘(self):
        """幂等：每日抓取会天天走到这里，没东西可加就不能碰文件 ——
        否则每轮都改写公共配置，mtime/内容抖动会把"词表到底变没变"这条信息磨没。"""
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td, ["lumo", "nexo", "piko", "gale"])
            before, mtime = cfg.read_text(encoding="utf-8"), cfg.stat().st_mtime_ns
            with unittest.mock.patch.object(F, "CFG_PATH", cfg):
                r = F.grow_watchlist(self._rows())
            self.assertEqual(r["added"], [])
            self.assertEqual(r["new"], json.loads(before)["keyword_include"])
            self.assertEqual((r["rows_before"], r["rows_after"]), (6, 6))
            self.assertEqual(cfg.read_text(encoding="utf-8"), before)
            self.assertEqual(cfg.stat().st_mtime_ns, mtime, "无新增仍写了文件")

    def test_pinned与exclude原样保留(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td, ["lumo", "nexo"], keyword_include_pinned=["zed"],
                            keyword_exclude=["solar"])
            with unittest.mock.patch.object(F, "CFG_PATH", cfg):
                r = F.grow_watchlist(self._rows())
            disk = json.loads(cfg.read_text(encoding="utf-8"))
            self.assertEqual(disk["keyword_exclude"], ["solar"], "增量不该动排除词")
            self.assertEqual(disk["keyword_include_pinned"], ["zed"])
            # pinned 漏进 include 时（例如上次整表替换把它冲了）增量要顺手补回
            self.assertEqual(r["added"], ["piko", "zed"])
            self.assertIn("zed", disk["keyword_include"])

    def test_坏配置报错且不留下改动(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "cfg.json"
            cfg.write_text("{这不是合法的 json", encoding="utf-8")
            with unittest.mock.patch.object(F, "CFG_PATH", cfg):
                with self.assertRaises(Exception):
                    F.grow_watchlist(self._rows())
            self.assertEqual(cfg.read_text(encoding="utf-8"), "{这不是合法的 json")

    def test_落盘校验拦住没生效的写(self):
        """把 _atomic_write 换成空操作（模拟写失败/被别处覆盖）：grow 必须报错，
        而不是返回"已新增"让下一轮以为词表好了。"""
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td, ["lumo", "nexo"])
            with unittest.mock.patch.object(F, "CFG_PATH", cfg), \
                 unittest.mock.patch.object(F, "_atomic_write", lambda *a, **k: None):
                with self.assertRaises(RuntimeError) as cm:
                    F.grow_watchlist(self._rows())
            self.assertIn("落盘校验失败", str(cm.exception))
            self.assertEqual(json.loads(cfg.read_text(encoding="utf-8"))["keyword_include"],
                             ["lumo", "nexo"])

    def test_前N名这个数只有一个来源(self):
        """派生、增量、CLI 默认值必须同一个数：否则抓取按 50 补词、手工刷新按别的数重算，
        两次跑出来的词表不一致，没人能解释差异。"""
        for fn in (F.derive_watchlist, F.grow_watchlist, F.refresh_watchlist):
            self.assertEqual(inspect.signature(fn).parameters["top_n"].default,
                             F.WATCHLIST_TOP_N, fn.__name__)

    def test_钩子在本轮就把新家族带进模型表(self):
        """增量钩子必须长在 run() 里、且在过滤之前。删掉 run() 里那几行调用，
        上面所有纯函数测试仍然全绿 —— 这条补的就是那个面。"""
        rounds, _ = self._pipeline(self.CFG, [self._aa(self.ROWS[:4]), self._aa(self.ROWS)])
        names1 = sorted(m["name"] for m in rounds[0][0]["models"])
        self.assertEqual(names1, ["Lumo Air", "Lumo Gale", "Nexo Bolt", "Nexo Dash"])
        self.assertTrue(any("词表增量：本轮无未覆盖" in l for l in rounds[0][1]),
                        rounds[0][1])
        self.assertEqual(json.loads(rounds[0][2]), json.loads(self.CFG),
                         "首轮没有新家族就不该改写配置")

        names2 = sorted(m["name"] for m in rounds[1][0]["models"])
        self.assertIn("Piko Arc", names2, "新家族要到下一轮才出现 —— 钩子放晚了")
        self.assertIn("Piko Rim", names2)
        self.assertTrue(any("词表增量：新增" in l for l in rounds[1][1]), rounds[1][1])
        disk = json.loads(rounds[1][2])
        self.assertEqual(sorted(disk["keyword_include"]), ["lumo", "nexo", "piko"])

    def test_零命中词会出现在抓取日志里(self):
        """整词边界的代价必须被 run() 报出来，不然失配的 pinned 就是静默不再关注。"""
        cfg = json.dumps({"keyword_include": ["lumo", "nexo", "piko", "zzz-not-a-model"],
                          "keyword_exclude": [], "keyword_include_pinned": []},
                         ensure_ascii=False, indent=2) + "\n"
        rounds, _ = self._pipeline(cfg, [self._aa(self.ROWS)])
        logs = rounds[0][1]
        self.assertTrue(any("一行都没命中" in l for l in logs), logs)
        self.assertTrue(any("zzz-not-a-model" in l for l in logs), logs)

    def test_AA降级轮不改写词表(self):
        """主源挂掉时沿用旧快照：那批行派生不出新东西，还在故障期间改公共输入。"""
        def boom(url):
            raise RuntimeError("AA 503")

        # 快照行必须来自真的 parse_aa：降级后这些行会一路走 merge，
        # 用 name/creator 手搓的行缺 ctx，会先在 merge 里崩，报不到增量这条上。
        stale = (F.parse_aa(self._aa(self.ROWS).decode("utf-8", "ignore")),
                 "2026-10-08 07:35:00")
        rounds, _ = self._pipeline(self.CFG, [self._aa([])], fetch=boom, snapshot=stale)
        payload, logs, cfg_text = rounds[0]
        self.assertTrue(payload["sources"]["aa"]["stale"], "夹具没造出降级轮")
        self.assertEqual(payload["data_state"], "stale")
        self.assertNotIn("词表增量", "".join(logs), "降级轮仍跑了增量")
        self.assertEqual(json.loads(cfg_text), json.loads(self.CFG), "降级轮改写了词表")


class TestWatchlistBoundary(unittest.TestCase):
    """名次线扩边：基础 N 名之外，历史关注厂商按**当前最高名次**扩边。

    单独立一层的原因：50 名一刀切与"只加不减"的词表互相矛盾 —— 老关注厂商掉到 54 名后
    派生不出任何词，整表重算就会把它静默剔除。而扩边本身有个显而易见的错法：拿同厂的
    低档（#85 的 Mistral Small）去算，名次线会被拖到榜尾、等于取消名次线。这两面都要钉住。
    """

    def _rows(self, pairs):
        return [row(n, creator=c, index=x) for n, c, x in pairs]

    def test_没有历史词时就是基础线(self):
        rows = self._rows([(f"M{i}", "V", 100 - i) for i in range(80)])
        r = F.watchlist_boundary(rows, 50, [])
        self.assertEqual((r["base_n"], r["effective_top_n"]), (50, 50))
        self.assertEqual(r["expanded_by"], [])

    def test_历史厂商在基础线内时不扩边(self):
        rows = self._rows([("Old Model", "Acme", 100)] + [(f"M{i}", "V", 90 - i) for i in range(79)])
        r = F.watchlist_boundary(rows, 50, ["acme"])
        self.assertEqual(r["effective_top_n"], 50)
        self.assertEqual(r["expanded_by"], [])
        self.assertEqual(r["vendors"]["Acme"]["best_rank"], 1)

    def test_历史厂商掉出基础线时按它最好那一行扩边(self):
        """Acme 最好一行在 #54，同厂另两行在 #59/#85 —— 扩到 54，不是 85。"""
        pairs = [(f"M{i}", "V", 200 - i) for i in range(53)]
        pairs += [("Acme High", "Acme", 26), ("Acme Mid", "Acme", 23),
                  ("M52", "V", 22), ("Acme Small", "Acme", 4)]
        r = F.watchlist_boundary(self._rows(pairs), 50, ["acme"])
        self.assertEqual(r["effective_top_n"], 54)
        self.assertEqual(r["expanded_by"], ["Acme"])
        self.assertEqual(r["vendors"]["Acme"]["best_rank"], 54)
        self.assertEqual(r["vendors"]["Acme"]["name"], "Acme High")

    def test_扩边取所有历史厂商里最靠后的那个(self):
        pairs = [(f"M{i}", "V", 500 - i) for i in range(50)]
        pairs += [("A1", "Acme", 95), ("B1", "Beta", 90)]
        r = F.watchlist_boundary(self._rows(pairs), 50, ["acme", "beta"])
        self.assertEqual(r["effective_top_n"], 52)
        self.assertEqual(r["expanded_by"], ["Acme", "Beta"])

    def test_不看页面顺序(self):
        """上游乱序时名次必须还按分数算，否则扩边会用错行。"""
        pairs = [(f"M{i}", "V", 200 - i) for i in range(53)]
        pairs += [("Acme Small", "Acme", 4), ("Acme High", "Acme", 26)]
        shuffled = list(reversed(pairs))
        r = F.watchlist_boundary(self._rows(shuffled), 50, ["acme"])
        self.assertEqual(r["vendors"]["Acme"]["best_rank"], 54)
        self.assertEqual(r["vendors"]["Acme"]["name"], "Acme High")

    def test_并列按竞赛式名次_边界不拆散同分(self):
        """同分同名次、下一档跳过被占名次：50 名处三条同分行要一起进。"""
        rows = self._rows([(f"M{i}", "V", 1000 - i) for i in range(49)]
                          + [("Tie A", "V", 51), ("Tie B", "V", 51), ("Tie C", "V", 51),
                             ("After", "V", 40)])
        self.assertEqual(F.competition_ranks(rows)[49:], [50, 50, 50, 53])
        self.assertEqual(F.rows_within_rank(rows, 50), set(range(52)))
        self.assertEqual(F.rows_within_rank(rows, 52), set(range(52)),
                         "名次 52 不存在（被并列占掉），不该多收一行")

    def test_派生窗口跟着生效名次线走(self):
        """边界扩到 54 后，#51–#54 的厂商词必须能被派生出来，否则扩边只是空转。"""
        pairs = [(f"M{i}", "V", 200 - i) for i in range(50)]
        pairs += [("Late One", "Late", 28), ("Late Two", "Late", 27),
                  ("Acme High", "Acme", 26)]
        rows = self._rows(pairs)
        narrow = F.derive_watchlist(rows, 50)
        wide = F.derive_watchlist(rows, 54)
        self.assertNotIn("late", narrow, "50 线以内的派生不该看到 #51 的家族")
        self.assertIn("late", wide)

    def test_生效线与基础线一起落进元信息(self):
        rows = self._rows([(f"M{i}", "V", 200 - i) for i in range(53)]
                          + [("Acme High", "Acme", 26)])
        b = F.watchlist_boundary(rows, 50, ["acme"])
        meta = F._watchlist_meta(rows, b["effective_top_n"], ["acme"], "full", [], b)
        self.assertEqual((meta["base_n"], meta["top_n"]), (50, 54))
        self.assertEqual(meta["expanded_by"], ["Acme"])
        self.assertEqual(meta["vendors"]["Acme"]["best_rank"], 54)

    def test_刷新与增量走同一个派生入口(self):
        """两条路各算一次边界就会产出两套词表，差异没人解释得清。"""
        self.assertEqual(inspect.signature(F.resolve_watchlist).parameters["base_n"].default,
                         F.WATCHLIST_TOP_N)
        src = inspect.getsource(F.refresh_watchlist) + inspect.getsource(F.grow_watchlist)
        self.assertEqual(src.count("resolve_watchlist("), 2, "有一条路绕过了统一入口")
        self.assertNotIn("derive_watchlist(", src, "有站点直接调派生、跳过边界计算")


class TestBoundaryMatch(unittest.TestCase):
    """watchlist 匹配必须按整词边界 —— 碎片不能把隔壁厂商整族捞进来。

    2026-10-09 在真实 AA 快照（126 行）上量的三个坑：`sol` 子串命中 `Solar Mini 4`、
    `ling` 命中 `Inkling Small`、`mini` 命中 `Gemini 3.8 Flash`；最宽的 `a` 靠子串命中
    117 行。旧写法先把标点删光再取子串，边界信息在删的那一步就丢了。
    """

    def test_碎片不再命中隔壁族(self):
        self.assertFalse(F.want_row(row("Solar Mini 4", creator="Upstage"), ["sol"], []))
        self.assertFalse(F.want_row(row("Inkling Small", creator="Thinking Machines"), ["ling"], []))
        self.assertFalse(F.want_row(row("Gemini 3.8 Flash", creator="Google"), ["mini"], []))
        self.assertFalse(F.want_row(row("A.X-K2", creator="SK Telecom"), ["a"], []))
        self.assertFalse(F.want_row(row("MiniCPM5-2B", creator="OpenBMB"), ["mini"], []))
        # 族自己的行照旧命中 —— 判据不能靠"少捞"通过，得是"少捞错的"
        self.assertTrue(F.want_row(row("GPT-6.1 Sol (max)", creator="OpenAI"), ["sol"], []))
        self.assertTrue(F.want_row(row("Ling 3.1 Flash", creator="InclusionAI"), ["ling"], []))
        self.assertTrue(F.want_row(row("Claude Haiku 5.5 (max)", creator="Anthropic"), ["haiku"], []))

    def test_多段关键词仍然可用(self):
        """pinned 里手写的常是多段词（'deepseek-v4'），边界化不能把它变成零命中。"""
        r = row("DeepSeek V4 Pro 0813 (max)", creator="DeepSeek")
        self.assertTrue(F.want_row(r, ["deepseek-v4"], []), "多段词应按连续段对齐命中")
        self.assertTrue(F.want_row(r, ["DeepSeek V4 Pro"], []))

    def test_版本号尾巴不再顺带命中(self):
        """'v4' 顺手命中 'V4.1' 等于把两代模型并成一族 —— 现在必须写全，这是有意的收紧。"""
        r = row("DeepSeek V4.1 Flash", creator="DeepSeek")
        self.assertFalse(F.want_row(r, ["v4"], []), "版本尾巴被当成同一代了")
        self.assertTrue(F.want_row(r, ["v4.1"], []))

    def test_空关键词不再有能力清空整张表(self):
        """旧写法 `'' in blob` 恒真：一个规范化后为空的排除词（'免费'）就能静默筛空全表，
        而 data_state 仍是 ok。现在空段恒不命中，两侧都拦不住任何行。"""
        rows = [row("GPT-6 Sol (max)", creator="OpenAI"), row("Kimi K3", creator="Kimi")]
        for dead in ("", "免费", "——", ".", "..."):
            self.assertTrue(all(F.want_row(r, ["gpt", "kimi"], [dead]) for r in rows),
                            f"排除词 {dead!r} 把整张表清空了")
            self.assertFalse(any(F.want_row(r, [dead], []) for r in rows),
                             f"关键词 {dead!r} 命中了一切")

    def test_排除词也走边界(self):
        """'solar' 只该压住 Upstage 的 Solar，不该连 'sol' 的 GPT-6 Sol 一起压掉。"""
        self.assertTrue(F.want_row(row("GPT-6.1 Sol (max)", creator="OpenAI"), ["sol"], ["solar"]))
        self.assertFalse(F.want_row(row("Solar Mini 4", creator="Upstage"), ["sol", "mini"], ["solar"]))

    def test_norm_runs的分词形态(self):
        """一条正则只切两种段：字母段与数字段。版本号 '3.8' 仍是一个数字段，点不被拆开。"""
        self.assertEqual(F.norm_runs("Qwen3.8-235B-A22B"),
                         ["qwen", "3.8", "235", "b", "a", "22", "b"])
        self.assertEqual(F.norm_runs("A.X-K2"), ["a.x", "k", "2"])
        self.assertEqual(F.norm_runs("GPT-6 Sol (max)"), ["gpt", "6", "sol", "max"])
        self.assertEqual(F.norm_runs("GLM-5.3"), ["glm", "5.3"])
        self.assertEqual(F.norm_runs(""), [])
        self.assertEqual(F.norm_runs(None), [])

    def test_家族词跨代代次词钉代(self):
        """单段家族词命中全部代次；带数字段的词只钉那一代。"""
        self.assertTrue(F.want_row(row("Qwen3.8 27B (medium)", creator="Alibaba"), ["qwen"], []))
        self.assertTrue(F.want_row(row("Qwen4 Max", creator="Alibaba"), ["qwen"], []))
        self.assertFalse(F.want_row(row("Qwen4 Max", creator="Alibaba"), ["qwen3.8"], []))
        self.assertTrue(F.want_row(row("Tencent Hy4", creator="Tencent"), ["hy"], []))
        self.assertFalse(F.want_row(row("Tencent Hy3", creator="Tencent"), ["hy4"], []))

    def test_派生主轮只发单段家族词(self):
        """'Qwen3.8'/'Qwen4' 归成一个 qwen；'Tencent Hy4' 取命名首段 tencent；不产出带版本号的词。"""
        rows = [row("Qwen3.8 72B (max)", creator="Alibaba"), row("Qwen4 72B (max)", creator="Alibaba"),
                row("Tencent Hy4 (max)", creator="Tencent"), row("Kimi K3.1 (max)", creator="Kimi")]
        self.assertEqual(F.derive_watchlist(rows, top_n=len(rows)),
                         ["kimi", "qwen", "tencent"])

    def test_厂商独有词不受行数限制(self):
        """'claude' 这种整族词行数再多也不该出局：它只属于一家，新代次换代号也不会漏。"""
        rows = [row(f"Claude Fable {i} (max)", creator="Anthropic") for i in range(1, 26)]
        rows.append(row("Nexo Bolt", creator="Beta"))
        derived = F.derive_watchlist(rows, top_n=len(rows))
        self.assertIn("claude", derived)
        self.assertIn("nexo", derived)

    def test_跨两家就是通用词(self):
        """'max'/'one' 这类词只有跨厂商才算通用词，单靠行数不能把它们和族名混为一谈。"""
        rows = [row("Alpha Max", creator="A"), row("Alpha One", creator="A"),
                row("Nexo Max", creator="B"), row("Nexo One", creator="B"),
                row("Gamma Max", creator="C"), row("Gamma One", creator="C")]
        derived = F.derive_watchlist(rows, top_n=len(rows))
        self.assertNotIn("max", derived)
        self.assertNotIn("one", derived)
        self.assertEqual(sorted(derived), ["alpha", "gamma", "nexo"])

    def test_同覆盖优先命名首段(self):
        """'Mistral Large 4' 里 mistral 与 large 覆盖相同，但 mistral 是首段，优先。"""
        rows = [row("Mistral Large 4 (max)", creator="Mistral"),
                row("Mistral Small 4 (max)", creator="Mistral"),
                row("Nexo Bolt", creator="Beta"), row("Nexo Dash", creator="Beta")]
        derived = F.derive_watchlist(rows, top_n=len(rows))
        self.assertNotIn("large", derived)
        self.assertNotIn("small", derived)
        self.assertEqual(sorted(derived), ["mistral", "nexo"])

    def test_派生与过滤用的是同一套匹配(self):
        """token_stats 报的命中行必须等于该词在 want_row 下真正留下的行。
        两套口径一旦分裂，派生会说"这个词只覆盖 4 行"，过滤实际捞回 13 行。"""
        rows = [row("Lumo Air", creator="Acme"), row("Lumo Gale", creator="Acme"),
                row("Inkling Small", creator="Thinking Machines"),
                row("Solar Mini 4", creator="Upstage")]
        derived = F.derive_watchlist(rows, top_n=len(rows))
        self.assertTrue(derived, "夹具没派生出任何词，这条判据就是空转的")
        for w, (hits, sp, hd) in F.token_stats(rows, set(derived)).items():
            kept = frozenset(i for i, r in enumerate(rows) if F.want_row(r, [w], []))
            self.assertEqual(hits, kept, f"词 {w} 在派生侧与过滤侧覆盖的行不一致")

    def test_宽度量的是真边界(self):
        """判据吃的行数必须是边界命中数：'mini' 在子串口径下跨 4 行 6 家（被当通用词杀掉），
        边界口径下只属于 1 行 —— 这决定它是出局还是入局。"""
        rows = [row("Solar Mini 4", creator="Upstage"), row("Gemini 3.8 Flash", creator="Google"),
                row("Gemini 4 Argon", creator="Google"), row("MiniCPM5-2B", creator="OpenBMB")]
        sub = sum(1 for r in rows if F.norm_key("mini") in F.norm_key(f"{r['name']} {r['creator']}"))
        st = F.token_stats(rows, {"mini", "sol"})
        self.assertEqual(sub, 4, "夹具本身要能复现子串口径的宽度，否则这条判据没电")
        self.assertEqual(len(st["mini"][0]), 1, "边界口径下 'mini' 只该命中 Solar Mini 4 那一行")
        self.assertEqual(sorted({rows[i]["creator"] for i in st["mini"][0]}), ["Upstage"])
        self.assertNotIn("sol", st, "'sol' 不该因为 'Solar' 而有命中")

    def test_只在括号档位后缀里出现的词不入选(self):
        """head 判据同样吃边界口径：'mini' 只活在 `(mini)` 里时它是档位不是族。

        夹具刻意用 `Gemini Air (mini)` —— 主体里含 'mini' 这个子串（gem**ini**），
        旧写法会把 in_head 量成 1.0、档位词照样入选；这条判据只有配上这形状才会咬。
        """
        rows = [row("Gemini Air (mini)", creator="Google"), row("Gemini Gale (mini)", creator="Google"),
                row("Nexo Bolt", creator="Beta"), row("Nexo Dash", creator="Beta")]
        st = F.token_stats(rows, {"mini", "gemini", "nexo"})
        self.assertEqual(st["mini"][2], 0.0, "括号里的档位词被算进了命名主体")
        self.assertEqual(st["gemini"][2], 1.0)
        derived = F.derive_watchlist(rows, top_n=len(rows))
        self.assertNotIn("mini", derived)
        self.assertEqual(sorted(derived), ["gemini", "nexo"])

    def test_零命中的词会被报出来(self):
        """收紧匹配的代价：写错的词从"顺带多捞几家"变成"静默一行不捞"，所以必须出声。"""
        rows = [row("GPT-6 Sol (max)", creator="OpenAI")]
        self.assertEqual(F.zero_hit_words(rows, ["sol", "hy3", "免费"]), ["hy3", "免费"])
        self.assertEqual(F.zero_hit_words(rows, ["sol"]), [])


class TestChartWrite(unittest.TestCase):
    """chart.html 的写盘纪律：同内容不重写，真重写必须换 inode（原子替换）。

    组件按 mtime 决定重画，就地 write_text 会带来两种可见故障：同一轮渲染两次就闪两下；
    读到半截文件就画出一张空图。2026-10-09 的 crawler.log + script.log 各有一条"已重画/已写出"，
    证明每日那轮确实渲染了两次。
    """

    def _stat(self, p):
        st = os.stat(p)
        return st.st_ino, st.st_mtime_ns

    def test_同内容重写是空操作(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "chart.html"
            self.assertTrue(R.write_if_changed(p, "<html>A</html>"), "首写必须落盘")
            ino, mt = self._stat(p)
            self.assertFalse(R.write_if_changed(p, "<html>A</html>"), "内容没变仍重写了")
            self.assertEqual(self._stat(p), (ino, mt), "mtime/inode 动了 → 组件会白闪一次")

    def test_内容变了要换inode(self):
        """就地 open(path,'w') 会保留原 inode —— 换 inode 是"写临时文件再 os.replace"的指纹。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "chart.html"
            R.write_if_changed(p, "<html>old</html>")
            ino, _ = self._stat(p)
            self.assertTrue(R.write_if_changed(p, "<html>new-and-longer</html>"))
            ino2, _ = self._stat(p)
            self.assertNotEqual(ino, ino2, "没走原子替换，读侧可能拿到半截文件")
            self.assertEqual(p.read_text(encoding="utf-8"), "<html>new-and-longer</html>")
            self.assertFalse((Path(td) / "chart.html.tmp").exists(), "临时文件残留")

    def test_差一个字符也算变(self):
        """判据不能是"长度相同就当没变"——图上一个点挪位、时间戳秒数都要能触发重画。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "chart.html"
            R.write_if_changed(p, "x" * 100)
            self.assertTrue(R.write_if_changed(p, "x" * 99 + "y"))
            self.assertEqual(p.read_text(encoding="utf-8"), "x" * 99 + "y")

    def test_软链路径也写在真实目录里(self):
        """chart.html 可能被软链到别处；tmp 必须落在解链后的真实父目录，否则跨文件系统 rename 报错。"""
        with tempfile.TemporaryDirectory() as td:
            real_dir = Path(td) / "real"
            real_dir.mkdir()
            link = Path(td) / "chart.html"
            target = real_dir / "chart.html"
            os.symlink(target, link)
            self.assertTrue(R.write_if_changed(link, "<html>via-symlink</html>"))
            self.assertEqual(target.read_text(encoding="utf-8"), "<html>via-symlink</html>")
            self.assertFalse(list(real_dir.glob("*.tmp")), "临时文件建到了软链另一头")


class TestLegendOrder(unittest.TestCase):
    """图例排序：梯队（该厂最高分所在档）→ 同档内按**指数加权**的离前沿距离。

    这里测的是**没有用量源时的回退口径**；带用量时的 指数×log1p(平衡用量) 加权
    见 `TestUsageWeighting`。

    这一层的判据都得有可观察后果：加权这条在今天的真实数据上恰好不换序
    （13 家厂商名次一模一样），所以夹具必须故意造出"加权与简单平均给出不同名次"的形状。
    """

    def mdl(self, name, creator, index, apt, tok=100.0):
        return {"name": name, "creator": creator, "index": index, "apt_usd": apt,
                "tok_s": tok, "ttft_s": None, "ctx": "200K", "in_per_1m_usd": None,
                "out_per_1m_usd": None, "or_id": None, "dl_index": None,
                "series": name, "series_key": F.norm_key(name), "tier": ""}

    def test_前沿支配规则(self):
        """更便宜且分不低于自己 → 自己被支配；分相同时留便宜的那个。"""
        ms = [self.mdl("A", "Va", 40, 1.0), self.mdl("B", "Vb", 35, 2.0),
              self.mdl("C", "Vc", 45, 8.0), self.mdl("D", "Vd", 40, 3.0)]
        front = {m["name"] for m in F.pareto_frontier(ms)}
        self.assertEqual(front, {"A", "C"}, "B 被 A 支配、D 与 A 同分但更贵应出局")

    def test_推估与参照带不进前沿(self):
        ms = [self.mdl("Real", "Va", 30, 2.0), self.mdl("Fake", "Vb", 90, 1.0, tok=None)]
        ms[1]["tok_s_est"] = 50
        ms[1]["tok_s_est_range"] = [40, 60]
        ms[1]["tok_s_est_note"] = "同族插值"
        Band = self.mdl("Band", "Vc", 80, 1.5, tok=None)
        Band["tok_s_band"] = [30, 90]
        Band["tok_s_band_note"] = "同级参照带"
        ms.append(Band)
        F.annotate_efficiency(ms)
        self.assertEqual({m["name"] for m in ms if m["on_frontier"]}, {"Real"},
                         "插值出来的高分点或只有区间的带点改写了前沿")

    def test_前沿点的距离恒为0(self):
        ms = [self.mdl("A", "Va", 40, 1.0), self.mdl("B", "Vb", 35, 2.0)]
        F.annotate_efficiency(ms)
        self.assertEqual(next(m for m in ms if m["name"] == "A")["dist_front"], 0.0)

    def test_距离量到折线而不是拐角(self):
        """同分只是更贵：横向价差就是距离；若"到最近的前沿点"会多算一段对角线。"""
        ms = [self.mdl("Cheap", "Va", 20, 0.5), self.mdl("High", "Vb", 60, 4.0),
              self.mdl("SameAsCheapButPricier", "Vc", 20, 2.0)]
        F.annotate_efficiency(ms)
        # 归一化：x=log10 成本 [log0.5, log4]，y 分 [20,60]
        import math
        x0, x1 = math.log10(0.5), math.log10(4.0)
        xn = (math.log10(2.0) - x0) / (x1 - x0)
        got = next(m for m in ms if m["name"] == "SameAsCheapButPricier")["dist_front"]
        # 前沿折线在 (0,0)→(1,1) 之间；该点在 y=0 上，到线段的垂直距离 = xn·sin45°
        self.assertAlmostEqual(got, round(xn * math.sin(math.pi / 4), 4), places=4,
                               msg="距离没按折线算（拐角距离会把'只是更贵'误判成更远）")

    def test_梯队按正负1窗口从最高分起级(self):
        ms = [self.mdl("A", "Va", 53, 1.0), self.mdl("B", "Vb", 46, 2.0),
              self.mdl("C", "Vc", 5, 3.0)]
        bands = F.tier_ladder(ms)
        F.annotate_efficiency(ms, bands)
        self.assertEqual(bands, [{"rank": 0, "high": 53, "low": 53},
                                 {"rank": 1, "high": 46, "low": 46},
                                 {"rank": 2, "high": 5, "low": 5}],
                         "分差都 >1，应该各起一级")
        self.assertEqual({m["name"]: m["cap_band"] for m in ms}, {"A": 0, "B": 1, "C": 2})
        self.assertEqual(F.TIER_TOLERANCE_POINTS, 1)

    def test_窗口钉在起级分数上不滚动(self):
        """[50,49,48,45] → 50/49 同级，48 另起一级。

        如果窗口跟着"上一个分数"滚动，49 会把 48 也吸进来，一条每分一档的连续阶梯会一路
        并成巨档（今天真实数据就会把 54–38 并成一级）—— 梯队就白切了。
        """
        ms = [self.mdl("a", "V", 50, 1.0), self.mdl("b", "V", 49, 1.0),
              self.mdl("c", "V", 48, 1.0), self.mdl("d", "V", 45, 1.0)]
        bands = F.tier_ladder(ms)
        self.assertEqual([(b["rank"], b["high"], b["low"]) for b in bands],
                         [(0, 50, 49), (1, 48, 48), (2, 45, 45)])

    def test_分差恰好1并到同一级(self):
        ms = [self.mdl("x", "V", 41, 1.0), self.mdl("y", "V", 40, 1.0),
              self.mdl("z", "V", 39, 1.0)]
        bands = F.tier_ladder(ms)
        self.assertEqual([(b["rank"], b["high"], b["low"]) for b in bands], [(0, 41, 40), (1, 39, 39)])
        self.assertEqual(F.tier_of(41, bands), 0)
        self.assertEqual(F.tier_of(40, bands), 0)
        self.assertEqual(F.tier_of(39, bands), 1)
        self.assertEqual(F.tier_of(None, bands), None)

    def test_厂商梯队取自己最高分所在档(self):
        """厂商级 = 它最能打那个模型所在的梯队；同梯队内才轮到性价比说话。"""
        ms = [self.mdl("Lo", "Va", 39, 1.0), self.mdl("Hi", "Va", 41, 9.0),
              self.mdl("Only", "Vb", 40, 1.0)]
        bands = F.tier_ladder(ms)
        F.annotate_efficiency(ms, bands)
        lg = {r["creator"]: r for r in F.build_legend(ms, bands)}
        self.assertEqual(lg["Va"]["band"], 0, "Va 最高分 41 应该起第一级")
        self.assertEqual(lg["Vb"]["band"], 0, "Vb 的 40 与 41 只差 1 分，必须还在同一梯队")
        self.assertEqual(next(m for m in ms if m["name"] == "Lo")["cap_band"], 1)

    def test_加权与简单平均在这里给出不同名次(self):
        """不种这个形状，"按分数加权"这条实现坏了也不会有任何可见后果。

        Heavy 的旗舰（46 分）几乎贴着前沿，两个 24 分的小档却很贵；Steady 的旗舰同为 46 分
        （所以两家被 ±1 窗口并到**同一梯队**，梯队不再替它们决定名次）但那个点更贵。
        简单平均把 Heavy 拖到 Steady 之后，加权后旗舰说了算 → Heavy 提前。
        """
        ms = [self.mdl("Heavy Flag", "Heavy", 46, 1.0), self.mdl("Heavy Mid", "Heavy", 43, 1.2),
              self.mdl("Heavy Small 0", "Heavy", 24, 3.4), self.mdl("Heavy Small 1", "Heavy", 24, 3.4),
              self.mdl("Steady One", "Steady", 46, 2.5), self.mdl("Anchor", "Anchor", 20, 0.2)]
        bands = F.tier_ladder(ms)
        F.annotate_efficiency(ms, bands)
        lg = F.build_legend(ms, bands)
        by_weight = [r["creator"] for r in lg]
        self.assertEqual(by_weight, ["Heavy", "Steady", "Anchor"],
                         "梯队升序（0=最高）+ 档内加权距离升序没落地")
        self.assertEqual({r["creator"]: r["band"] for r in lg}["Heavy"],
                         {r["creator"]: r["band"] for r in lg}["Steady"],
                         "两家旗舰同分却不同梯队 → 夹具没落进 ±1 窗口，分歧测不到")
        # 防空转：同一批数据按简单平均必须给出**不同**名次，否则这条判据只是在重复实现
        band_of = {r["creator"]: r["band"] for r in lg}
        by_mean = [c for c, _ in sorted(((r["creator"], r["dist_mean"]) for r in lg),
                                        key=lambda kv: (band_of[kv[0]], kv[1]))]
        self.assertNotEqual(by_mean, by_weight,
                            f"夹具没造出加权与简单平均的分歧（都是 {by_weight}），判据空转")
        heavy = next(r for r in lg if r["creator"] == "Heavy")
        steady = next(r for r in lg if r["creator"] == "Steady")
        self.assertLess(heavy["dist_weighted"], steady["dist_weighted"])
        self.assertGreater(heavy["dist_mean"], steady["dist_mean"])

    def test_梯队优先于距离(self):
        """低梯队里"很便宜"的厂商不能插到高梯队前面。"""
        ms = [self.mdl("Top", "Va", 52, 9.0), self.mdl("Cheap Bottom", "Vb", 30, 0.2),
              self.mdl("Mid", "Vc", 41, 0.9)]
        F.annotate_efficiency(ms)
        self.assertEqual([r["creator"] for r in F.build_legend(ms)], ["Va", "Vc", "Vb"])

    def test_不可画的点不参与距离但保留在名单里(self):
        ms = [self.mdl("A", "Va", 40, 1.0), self.mdl("NoCost", "Vb", 35, None)]
        F.annotate_efficiency(ms)
        nocost = next(m for m in ms if m["name"] == "NoCost")
        self.assertIsNone(nocost["dist_front"])
        self.assertFalse(nocost["on_frontier"])
        self.assertEqual(nocost["cap_band"], 1)   # 分数只有 40 / 35 两级，差 5 分 → 各起一级
        lg = F.build_legend(ms)
        self.assertEqual([r["creator"] for r in lg], ["Va", "Vb"], "没有距离的厂商应垫底")
        self.assertIsNone(lg[1]["dist_weighted"])

    def test_不可画的高分不能抬高厂商梯队(self):
        """AA 有「有分但没成本」的行（成本列是 `--`）：它不在图上，就不能拿它排图例。

        真实形状：Meta 的 Muse Wonder Full(56) 无成本，图上只有 Muse Wonder(52)；
        旧派生按全部行取最高分，把它抬到第 1 梯队，与图上最高点 52 分自相矛盾。
        """
        ms = [self.mdl("Ghost 56", "Va", 56, None),
              self.mdl("Rendered 52", "Va", 52, 4.5),
              self.mdl("Rival 53", "Vb", 53, 2.0)]
        F.annotate_efficiency(ms)
        lg = {r["creator"]: r for r in F.build_legend(ms)}
        self.assertEqual(lg["Va"]["best_index"], 52, "图例最高分应取可画点")
        self.assertEqual(lg["Va"]["best_index_all"], 56, "全部行最高分要留档，不能丢")
        self.assertEqual(lg["Va"]["plotted"], 1)
        self.assertEqual([r["creator"] for r in F.build_legend(ms)], ["Vb", "Va"],
                         "不可画的 56 分把 Va 抬到 Vb(53) 前面了")


        """"只消费"的另一面：拿一份没有这些字段的 latest.json 来渲染，必须报错而不是画哑图。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "latest.json"
            p.write_text(json.dumps({"generated_at": "2026-10-09 07:30",
                                     "models": [self.mdl("A", "Va", 40, 1.0)]}),
                         encoding="utf-8")
            with unittest.mock.patch.object(R, "LATEST", p):
                with self.assertRaises(SystemExit) as cm:
                    R.load_points()
            self.assertIn("on_frontier", str(cm.exception))

    def test_空载荷不炸(self):
        self.assertEqual(F.annotate_efficiency([]), [])
        self.assertEqual(F.build_legend([]), [])
        self.assertEqual(F.plotted_models([]), [])

    def test_只有一个可画点时归一化不除零(self):
        ms = [self.mdl("Alone", "Va", 40, 1.0)]
        F.annotate_efficiency(ms)
        self.assertEqual(ms[0]["dist_front"], 0.0)
        self.assertTrue(ms[0]["on_frontier"])
        self.assertEqual(F.build_legend(ms)[0]["creator"], "Va")

    def test_状态栏图例照payload的顺序排(self):
        """渲染面只消费：给了 order 就按它排，图上有但 order 里没有的厂商垫底按名字。"""
        pts = [{"c": "Zeta", "col": "#0000ff"}, {"c": "Alpha", "col": "#ff0000"},
               {"c": "Omen", "col": "#00ff00"}]
        rows = R.legend_rows(pts, ["Alpha", "Zeta"])
        self.assertEqual([n for _, n in rows], ["Alpha", "Zeta", "Omen"])
        self.assertEqual(rows[0][0], "#ff0000", "颜色串了厂商")
        rows2 = R.legend_rows(pts, [])
        self.assertEqual([n for _, n in rows2], ["Alpha", "Omen", "Zeta"])
        _items, extra = R.legend_items(rows2, R.legend_layout([n for _, n in rows2]))
        self.assertEqual(extra, 0)

    def test_图例第一行避开标注_下面各行用满宽度(self):
        """2026-11-06 用户口径：每行用满自己的允许宽度，但仍共用同一份列网格。

        第一行与「Pareto 前沿」同带，必须停在标注左边界之前；下面各行没有标注，
        按绘图区右边界算列数。列数可以不同，列起点不能错位。
        """
        names = ["Anthropic", "Google", "OpenAI", "Kimi", "Meta", "SpaceXAI", "Tencent",
                 "DeepSeek", "Alibaba", "Thinking Machines", "Z AI", "Xiaomi",
                 "MiniMax", "StepFun", "InclusionAI", "Mistral"]
        lay = R.legend_layout(names)
        self.assertGreaterEqual(lay["pitch"], R.LEGEND_MIN_PITCH)
        self.assertEqual(len(lay["cols"]), lay["rows_max"])
        self.assertEqual(lay["cap"], sum(lay["cols"]))
        counts = R.legend_counts(lay["cols"], len(names))
        self.assertEqual(sum(counts), len(names))
        self.assertLessEqual(max(counts) - min(c for c in counts if c), 1, "行间只差一格，不留空尾巴")
        limits = [lay["label_left"] - 6] + [R.W - R.PR - 2] * (lay["rows_max"] - 1)
        offset = 0
        for c, limit in zip(counts, limits):
            row = names[offset:offset + c]
            offset += c
            if not row:
                break
            right = R.PL + 2 + (len(row) - 1) * lay["pitch"] + R.LEGEND_DOT_TEXT_GAP + R.text_w(row[-1])
            self.assertLessEqual(right, limit, f"一行 {c} 格越界：{row[-1]}")
        self.assertGreater(lay["cols"][0], 0)
        # 气泡就是障碍：同一行带里有气泡时，最后一格必须停在它左边
        blocked = R.legend_layout(names, [(300.0, R.PT + 6 + R.LEGEND_ROW_H, 8.0)])
        bcounts = R.legend_counts(blocked["cols"], len(names))
        row1 = names[bcounts[0]:bcounts[0] + bcounts[1]]
        right1 = (R.PL + 2 + (len(row1) - 1) * blocked["pitch"]
                  + R.LEGEND_DOT_TEXT_GAP + R.text_w(row1[-1]))
        self.assertLessEqual(right1, 300 - 8 - R.LEGEND_GAP, "最后一格压到了气泡")
        # 球的包围盒才是障碍：圆心落在两行之间、半径比行距大的球，以前两条带都是
        # |圆心−基线| > 8，谁都躲，球照样压字（2026-11-10 截图那两处）。
        base, h = R.PT + 6, R.LEGEND_TEXT_HALF
        between = R.legend_layout(names, [(300.0, base + R.LEGEND_ROW_H, 12.0)])
        self.assertLessEqual(between["cols"][0], lay["cols"][0] - 1, "跨两行的球没让第一行收窄")
        self.assertLessEqual(between["cols"][1], lay["cols"][1] - 1, "跨两行的球没让第二行收窄")
        # 每一行都被挡住时退回旧行为（至少留一格），图例不能整块消失
        floor = R.legend_layout(names, [(0.0, base, 400.0)])
        self.assertGreater(floor["cols"][0], 0, "整行让位到最后不能把图例抹掉")
        # 名字变长 → 格子变宽 → 每行 fewer 格，而不是把名字截断
        wide = R.legend_layout(["VeryLongVendorName"] * 3)
        self.assertGreater(wide["pitch"], lay["pitch"])
        self.assertLessEqual(wide["cols"][0], lay["cols"][0])

    def test_厂商全部列出不省略(self):
        def rows(n):
            return [(f"#{i:06X}", f"V{i:02d}") for i in range(n)]
        cap = R.legend_layout([f"V{i:02d}" for i in range(9)])["cap"]
        for n in (13, 20, cap):
            names = [f"V{i:02d}" for i in range(n)]
            items, extra = R.legend_items(rows(n), R.legend_layout(names))
            self.assertEqual(len(items), n, f"{n} 家厂商全列，不许省略")
            self.assertEqual(extra, 0)
        names = [f"V{i:02d}" for i in range(cap + 5)]
        items, extra = R.legend_items(rows(cap + 5), R.legend_layout(names))
        self.assertEqual((len(items), extra), (cap, 5), "只有多到装不下才省略，且如实报数")

    def test_切分必须用传进来的那份布局(self):
        """`cap` 和绘制用的 `pitch/cols` 必须同源。

        各自算一次时会打架：全量名字里有一家名特别长 → 格子宽 → 容量小；省略掉它之后再算，
        格子窄 → 容量又大 → 图上一条「+N 家」和实际画出的行数对不上。用一个小到装不下的
        假布局当探针：实现若自己重算 cap，就会吐出比它多的条目。
        """
        names = [f"V{i:02d}" for i in range(40)] + ["ExtremelyLongVendorNameX"]
        rows = [(f"#{i:06X}", n) for i, n in enumerate(names)]
        lay = R.legend_layout(names)
        self.assertLess(lay["cap"], len(rows), "夹具得真的溢出，否则这条判据是空转的")
        items, extra = R.legend_items(rows, lay)
        self.assertEqual((len(items), extra), (lay["cap"], len(rows) - lay["cap"]))
        tight = dict(lay, cap=3)
        items2, extra2 = R.legend_items(rows, tight)
        self.assertEqual((len(items2), extra2), (3, len(rows) - 3),
                         "忽略传入布局、按剩下的名字重算容量 —— 两处就不是同一张网格")

    def test_名字不截断(self):
        """以前超过 10 字符就写成 "Inclusion…"；厂商名就是读者要找的东西。"""
        pts = [{"c": "InclusionAI", "col": "#123456"}, {"c": "VeryLongVendorName", "col": "#654321"}]
        rows = R.legend_rows(pts, [])
        self.assertEqual([n for _, n in rows], ["InclusionAI", "VeryLongVendorName"])
        self.assertTrue(all("…" not in n for _c, n in rows))

    def test_图例最多占绘图区四成(self):
        lay = R.legend_layout(["A"])
        plot_h = R.H - R.PB - R.PT
        self.assertLessEqual((lay["rows_max"] - 1) * R.LEGEND_ROW_H, plot_h * R.LEGEND_HEIGHT_FRACTION + R.LEGEND_ROW_H)
        self.assertGreaterEqual(lay["rows_max"], 2, "至少两行，否则又回到省略")

class TestUsageWeighting(unittest.TestCase):
    """图例第二指标：指数 × log1p(平衡用量) 加权的平均离前沿距离。

    口径（2026-10-19 与用户确认）：
        dist_weighted = Σ(index × log1p(平衡用量) × dist) / Σ(index × log1p(平衡用量))
    平衡用量 = 请求量 × (该模型 token 量 / 全站 token 量)，同一 permaslug 的多档均分。
    缺用量的行分子分母都不进；整厂缺用量 → dist_weighted=None，同梯队垫底；
    只有用量源整体失败才退回旧的指数加权。
    """

    def row(self, name, creator, index, dist, usage=None):
        return {"name": name, "creator": creator, "index": index,
                "dist_front": dist, "usage_balanced": usage}

    def test_解析按permaslug汇总并给出全站分母(self):
        payload = json.dumps({"data": [
            {"model_permaslug": "deepseek/x-20261017", "count": 10,
             "total_prompt_tokens": 100, "total_completion_tokens": 50, "date": "2026-10-18"},
            {"model_permaslug": "deepseek/x-20261017", "count": 5,
             "total_prompt_tokens": 60, "total_completion_tokens": 40, "date": "2026-10-19"},
            {"model_permaslug": "other/y", "count": 2,
             "total_prompt_tokens": 20, "total_completion_tokens": 0, "date": "2026-10-17"},
            {"model_permaslug": "broken/z", "count": 3},
        ]}).encode()
        by_slug, meta = F.parse_openrouter_usage(payload)
        self.assertEqual(by_slug["deepseek/x-20261017"],
                         {"requests": 15, "tokens": 250, "date": "2026-10-19"},
                         "同 slug 多日必须汇总，日期取最新")
        self.assertEqual(meta["total_requests"], 17)
        self.assertEqual(meta["total_tokens"], 270, "缺 token 字段的行不能按 0 计入分母")
        self.assertEqual(meta["missing"], 1)

    def test_解析空数据或零合计必须报错(self):
        """抓不到 ≠ 没人用：解析出 0 个模型就抛，让上层退回指数加权并标 degraded。"""
        for payload in ({"data": []}, {"data": [{"model_permaslug": "a/b"}]},
                        {"data": [{"model_permaslug": "a/b", "count": 0,
                                   "total_prompt_tokens": 0, "total_completion_tokens": 0}]},
                        {"nope": 1}):
            with self.assertRaises(RuntimeError, msg=f"没报错：{payload}"):
                F.parse_openrouter_usage(json.dumps(payload).encode())

    def test_平衡用量是token占比乘请求量且同slug多档均分(self):
        or_models = {
            "deepseek/deepseek-v4.1-flash-1017": {
                "canonical_slug": "deepseek/deepseek-v4.1-flash-20261017",
                "name": "DeepSeek V4.1 Flash 1017",
            },
        }
        usage = {"deepseek/deepseek-v4.1-flash-20261017":
                 {"requests": 10, "tokens": 100, "date": "2026-10-19"}}
        meta = {"total_tokens": 1000, "total_requests": 100}
        models = [
            {"name": "DeepSeek V4.1 Flash 1017 (max)", "creator": "DeepSeek"},
            {"name": "DeepSeek V4.1 Flash 1017 (high)", "creator": "DeepSeek"},
            {"name": "Other Model", "creator": "Other"},
        ]
        matched, missing = F.attach_usage(models, or_models, usage, meta)
        self.assertEqual((matched, missing), (2, 1))
        self.assertEqual([m["usage_balanced"] for m in models], [0.5, 0.5, None],
                         "同一 permaslug 的两档必须均分，不能各自记满一份")
        self.assertEqual(models[0]["usage_permaslug"],
                         "deepseek/deepseek-v4.1-flash-20261017",
                         "用量榜只认 canonical_slug，不能用目录 id")
        self.assertIsNone(models[2]["usage_week_requests"], "对不上的行要留空，不能当 0 用量")

    def test_没有用量源时清空既有字段(self):
        """降级轮不能沿用上一轮的用量：那会把旧流量当成这轮的真实输入。"""
        models = [{"name": "A", "creator": "V", "usage_balanced": 3.0,
                   "usage_week_requests": 1, "usage_week_tokens": 2, "usage_permaslug": "x"}]
        self.assertEqual(F.attach_usage(models, {}, {}, {}), (0, 1))
        self.assertEqual({k: v for k, v in models[0].items() if k.startswith("usage_")},
                         {"usage_week_requests": None, "usage_week_tokens": None,
                          "usage_balanced": None, "usage_permaslug": None})

    def test_加权距离等于分子分母各自求和(self):
        ms = [self.row("Flag", "Va", 50, 0.0, usage=9.0),
              self.row("Small", "Va", 20, 1.0, usage=1.0)]
        lg = F.build_legend(ms, usage_available=True)[0]
        w1, w2 = 50 * math.log1p(9.0), 20 * math.log1p(1.0)
        self.assertAlmostEqual(lg["dist_weighted"], round((w1 * 0 + w2 * 1) / (w1 + w2), 4),
                               places=4)
        self.assertEqual(lg["usage_rows"], 2)

    def test_缺用量的行不进分子也不进分母(self):
        """如果漏加这道挡，缺用量的行会被当成权重 1 或 0，把整厂均值拖偏。"""
        ms = [self.row("Flag", "Va", 50, 0.0, usage=9.0),
              self.row("NoUsage", "Va", 20, 1.0, usage=None)]
        lg = F.build_legend(ms, usage_available=True)[0]
        self.assertEqual(lg["dist_weighted"], 0.0)
        self.assertEqual(lg["usage_rows"], 1)
        self.assertEqual(lg["dist_mean"], 0.5, "诊断用的简单平均仍按全部可画行算")

    def test_整厂无用量在同梯队垫底(self):
        ms = [self.row("A", "NoUsage", 50, 0.0, usage=None),
              self.row("B", "HasUsage", 50, 0.9, usage=5.0)]
        lg = F.build_legend(ms, usage_available=True)
        self.assertEqual([r["creator"] for r in lg], ["HasUsage", "NoUsage"])
        self.assertIsNone(lg[1]["dist_weighted"])

    def test_用量源失败退回指数加权(self):
        """重仓的旗舰会让 Heavy 在用量加权下提前，指数加权则被两个档拉平淘汰。"""
        ms = [self.row("HeavyFlag", "Heavy", 46, 0.01, usage=1000.0),
              self.row("HeavySmall", "Heavy", 20, 0.9, usage=1.0),
              self.row("Steady", "Steady", 46, 0.2, usage=1.0)]
        with_usage = [r["creator"] for r in F.build_legend(ms, usage_available=True)]
        fallback = [r["creator"] for r in F.build_legend(ms, usage_available=False)]
        self.assertEqual(with_usage, ["Heavy", "Steady"], "用量加权后 Heavy 的旗舰主导")
        self.assertEqual(fallback, ["Steady", "Heavy"], "没用量时必须回到指数加权")
        self.assertEqual(F.build_legend(ms, usage_available=False)[0]["usage_rows"], 0,
                         "回退口径不应声称用了用量")

    def test_没有用量字段时自动判定为回退口径(self):
        ms = [self.row("A", "Va", 40, 0.0), self.row("B", "Vb", 40, 1.0)]
        self.assertEqual([r["creator"] for r in F.build_legend(ms)], ["Va", "Vb"])


class TestLegendHover(unittest.TestCase):
    """悬停图例 = 整厂高亮 + 综述卡（按系列）+ 各系列族卡；悬停一个点 = 该族一张合并卡。

    三条必须测到的口径：
    · **数字按图上真画的点算**：`load_points` 会丢掉没有 index 或没有成本的模型，
      今天 Anthropic 载荷里 85 个模型、图上 20 个 —— 用错集合 min/max/中位全错。
    · **实测与推估分行**：推估/参照带不是测量结果，并进同一区间就是把估计当实测说。
    · **共有尾巴只标一次但必须还在**："with fallback" 是测量口径，抽成一次是排版，
      抽没了就是丢信息。
    夹具走生产路径（爬虫 annotate → render.load_points），不手抄 pts 字段。
    """

    def _load(self, models):
        bands = F.tier_ladder(models)
        F.annotate_efficiency(models, bands)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "latest.json"
            p.write_text(json.dumps({"generated_at": "2026-10-13 07:30", "models": models,
                                     "legend": F.build_legend(models, bands),
                                     "tier_bands": bands,
                                     "tier_tolerance_points": F.TIER_TOLERANCE_POINTS},
                                    ensure_ascii=False), encoding="utf-8")
            with unittest.mock.patch.object(R, "LATEST", p):
                return R.load_points()

    def _models(self):
        return [
            row("V Alpha (low)", creator="V", index=40, apt_usd=0.5, tok_s=100,
                series_key="va1", series="Alpha", tier="low with fallback"),
            row("V Alpha (max)", creator="V", index=44, apt_usd=2.0, tok_s=200,
                series_key="va1", series="Alpha", tier="max with fallback"),
            row("V Beta", creator="V", index=42, apt_usd=1.0, tok_s=None,
                series_key="vb1", series="Beta", tier="high",
                tok_s_est=900.0, tok_s_est_range=[800.0, 1000.0], tok_s_est_n=2,
                tok_s_est_note="估计：同族相邻档插值"),
            row("W One", creator="W", index=50, apt_usd=0.2, tok_s=None,
                series_key="wo1", series="One",
                tok_s_band=[50.0, 120.0], tok_s_band_n=3, tok_s_band_same_creator=0,
                tok_s_band_note="同级能力参照：刻意不给中心值"),
            # 载荷里有、但画不到图上（没成本）。统计若按载荷算就会把它算进来
            row("V Ghost", creator="V", index=70, apt_usd=None, tok_s=999.0,
                series_key="vg1", series="Ghost"),
        ]

    def _by_creator(self, pts):
        out: dict[str, list[dict]] = {}
        for p in pts:
            out.setdefault(p["c"], []).append(p)
        return out

    # ---------------------------------------------------------------- 尾巴与档位串
    def test_共有尾巴抽一次但一个字不丢(self):
        self.assertEqual(R.hoist_suffix(["low with fallback", "max with fallback"]),
                         (["low", "max"], "with fallback"))
        # 各档尾巴不同 → 不抽（抽了就把"只有部分档带 fallback"说成全都带）
        self.assertEqual(R.hoist_suffix(["low with fallback", "max"])[1], "")
        # 单档不抽：没有"重复"可省
        self.assertEqual(R.hoist_suffix(["max with fallback"]), (["max with fallback"], ""))
        ts, sfx = R.hoist_suffix(["low with fallback", "high with fallback", "max with fallback"])
        self.assertTrue(all(sfx in x or sfx for x in [sfx]) and sfx == "with fallback")
        self.assertEqual("/".join(ts) + f"（均 {sfx}）", "low/high/max（均 with fallback）")

    def test_思考时间只从有效总响应与实测速度推算(self):
        self.assertEqual(R.think_seconds(207, 100), 202.0)
        self.assertEqual(R.think_seconds(4.9, 100), 0.0, "取整误差内的小负数按 0")
        self.assertIsNone(R.think_seconds(1, 100), "明显为负说明口径对不上，留空")
        for total, tok in ((None, 100), (200, None), (0, 100), (200, 0),
                           (True, 100), (200, False), (float("nan"), 100),
                           (200, float("inf"))):
            self.assertIsNone(R.think_seconds(total, tok), (total, tok))
        self.assertEqual(R.time_cell(656.77), "10.9min")
        self.assertEqual(R.time_cell(7.56), "7.6s")
        self.assertEqual(R.time_cell(None), "—")
        self.assertEqual(R.time_cell(float("nan")), "—")

    def test_总响应缺失时退回官方首回答延迟(self):
        # 官方 timeToFirstAnswerToken 本身就是首回答前等待，不再减 500/tok。
        self.assertEqual(R.think_seconds(None, 100, 24.51), 24.51)
        self.assertEqual(R.think_seconds(None, None, 24.51), 24.51, "缺速度也能用首回答延迟")
        self.assertEqual(R.think_seconds(None, 100, 0), 0.0, "0 是有效实测值，不是缺值")
        for bad in (-1, True, False, float("nan"), float("inf"), "12"):
            self.assertIsNone(R.think_seconds(None, 100, bad), bad)
        # total_s 有值却无效/矛盾时必须留空：不拿另一个口径掩盖对不上的数据。
        for total in (0, -5, float("nan")):
            self.assertIsNone(R.think_seconds(total, 100, 24.51), total)
        self.assertIsNone(R.think_seconds(1, 100, 24.51), "差得比容差更远仍算口径矛盾")
        self.assertEqual(R.think_seconds(207, 100, 24.51), 202.0, "有 total 时不用兜底值")

    def test_时间格按量级切换单位并拒无效值(self):
        # 显示自带单位，half-up 一位小数并去尾零；原始秒值与排序不受影响。
        for value, want in ((59.9, "59.9s"), (60, "1min"), (600, "10min"),
                            (0, "0s"), (48.34, "48.3s"), (654, "10.9min"),
                            (17.25, "17.3s"), (48.25, "48.3s"),
                            (59.95, "1min"), (3599.9, "60min")):
            self.assertEqual(R.time_cell(value), want, value)
        for value in (-1, -0.1, True, False, None, float("nan"),
                      float("inf"), float("-inf"), "12"):
            self.assertEqual(R.time_cell(value), "—", value)

    def test_族卡把每档详述并成一张(self):
        pts, front, order = self._load(self._models())
        fam = [p for p in pts if p["k"] == "va1"]
        lines = R.family_card_lines(fam)
        self.assertEqual(lines[0], "Alpha · 2 档（均 with fallback）", "表头标一次尾巴")
        self.assertEqual(lines[1:3], ["智能指数v5 / 历史参照 · $每任务",
                                      "任务耗时(估) · tok/s"],
                         "夹具没有总响应/首回答，整族都不该有思考两列")
        self.assertEqual(lines[3:5], ["max · 44 / — · $2 · — · 200",
                                      "low · 40 / — · $0.5 · — · 100"],
                         "没有总响应/速度的夹具必须留空，不许把 tok_s 复制成时间")
        self.assertEqual(len(lines), len(fam) + 3,
                         "表头 + 两行列名 + 每档一行，不再有汇总/明细尾行")
        self.assertFalse(any("Progress" in r or " · P" in r for r in lines),
                         "族卡不再重复 Progress 段，也不再单列逐档 Progress")
        self.assertNotIn("with fallback ·", "\n".join(lines), "每档行不该再重复尾巴")

    def test_族卡与系列行都按推理档位反序_与分数无关(self):
        """Terra 夹具：non-reasoning 30 分、low 29 分、max 51 分。

        若按分数排，non-reasoning 会排在 low 前面；固定档位顺序必须让它落到 low 之后。
        智能指数与 Progress 每档同一行成对写出，顺序同样按档位反序。
        """
        models = [
            row("Terra (non-reasoning)", creator="OpenAI", index=30, apt_usd=1.0, tok_s=91,
                series_key="terra", series="Terra", tier="non-reasoning",
                progress_index=90, progress_version="v1"),
            row("Terra (low)", creator="OpenAI", index=29, apt_usd=2.0, tok_s=91,
                series_key="terra", series="Terra", tier="low",
                progress_index=30, progress_version="v1"),
            row("Terra (max)", creator="OpenAI", index=51, apt_usd=3.0, tok_s=91,
                series_key="terra", series="Terra", tier="max",
                progress_index=60, progress_version="v1"),
        ]
        pts, _front, _order = self._load(list(reversed(models)))
        fam = [p for p in pts if p["k"] == "terra"]
        self.assertEqual(len(fam), 3, "夹具本身要真的画出三档")
        lines = R.family_card_lines(fam)
        self.assertEqual(lines[0], "Terra · 3 档")
        self.assertEqual(lines[1:3], ["智能指数v5 / 历史参照v1 · $每任务",
                                      "任务耗时(估) · tok/s"])
        self.assertEqual(lines[3:6], ["max · 51 / 60 · $3 · — · 91",
                                      "low · 29 / 30 · $2 · — · 91",
                                      "non-reasoning · 30 / 90 · $1 · — · 91"],
                         "分数是 non(30) > low(29)，档位顺序仍必须是 max/low/non，"
                         "Progress 与智能指数同排")
        self.assertEqual(len(lines), len(fam) + 3)
        self.assertEqual(R.series_row("Terra", fam),
                         "Terra · max/low/non-reasoning · 29~51 / 30~90 · 最优 max（前沿）",
                         "系列行的档位串同样固定反序，双评分成对")
        self.assertFalse(any("Progress" in l or " · P" in l for l in lines),
                         "Progress 已并入每档行，不能再有汇总行或逐档明细行")

    def test_单档族不重复系列名(self):
        pts, front, order = self._load(self._models())
        lines = R.family_card_lines([p for p in pts if p["k"] == "wo1"])
        self.assertEqual(lines[0], "One · 1 档")
        self.assertEqual(lines[1:3], ["智能指数v5 / 历史参照 · $每任务",
                                      "任务耗时(估) · tok/s"])
        self.assertEqual(lines[3], "50 / — · $0.2 · — · 50–120",
                         "没有档位标签时不该把系列名再念一遍，缺分写破折号")
        self.assertEqual(len(lines), 4)
        self.assertFalse(any("Progress" in l or " · P" in l for l in lines))

    # ---------------------------------------------------------------- 综述按系列
    def test_综述按系列一行含档位选项范围与最优档(self):
        pts, front, order = self._load(self._models())
        txt = R.vendor_stats("V", self._by_creator(pts)["V"])
        self.assertIn("按系列：档位 · 双评分 · 最优档", txt)
        self.assertIn("Alpha · max/low（均 with fallback） · 40~44 / — · 最优 max（前沿）", txt,
                      "档位串按推理档位反序，指数与 Progress 成对")
        # 只有一档：最优档那栏留空（用户口径），但档位与双评分照给
        self.assertIn("Beta · high · 42 / —", txt)
        self.assertNotIn("单点不成族", txt, "那是解释，不是数据")
        self.assertNotIn("档位明细", txt, "逐档明细已经搬进族卡，这里再列就是两处一份数据")
        lines = txt.split("\n")
        self.assertTrue(lines[1].startswith("智能指数v5 / 历史参照"),
                        "厂商双评分行紧随表头")
        self.assertIn("（各取族最高）", lines[1], "厂商聚合只取每族最高档")
        self.assertFalse(any(l.startswith("Progress ") for l in lines),
                         "不再单列 Progress 汇总行")

    def test_未上图行只在综述卡点名(self):
        """有指数但缺每任务成本的行不参与梯队排序（见 TestLegendOrder），
        但盘点卡要能解释"这个分数去哪了"——否则 56 分凭空出现在悬停里没有出处。"""
        pts, front, order = self._load(self._models())
        ghost = row("Ghost", creator="V", index=56, apt_usd=None)
        txt = R.vendor_stats("V", self._by_creator(pts)["V"], [ghost])
        self.assertIn("未上图：Ghost · 56 分（缺每任务成本）", txt)
        # 排序列不掺它：按系列仍然只有真画出来的 Alpha / Beta
        series_part = txt.split("按系列：档位 · 双评分 · 最优档", 1)[1]
        self.assertNotIn("Ghost", series_part)
        svg = R.build_svg(pts, front, order, {"V": [ghost]})
        self.assertIn("未上图：Ghost · 56 分", svg)
        self.assertIn("data-c=\"V\"", svg)

    def test_单点系列最优档留空(self):
        """只有一个点就没有"族"可比：档位列表退化成模型名、最优档退化成"就是它"，
        前沿与否也不是这一行能回答的 → 那一栏什么都不写。"""
        ms = [row("Solo", creator="S", index=30, apt_usd=0.3, tok_s=88,
                  series_key="so1", series="Solo", tier="")]
        pts, front, order = self._load(ms)
        one = [p for p in pts if p["k"] == "so1"]
        line = R.series_row("Solo", one)
        self.assertEqual(line, "Solo · 30 / —", f"该只给系列名与双评分 →「{line}」")

    def test_单点系列有档位标签时给档位不给最优(self):
        ms = [row("Solo (max)", creator="S", index=30, apt_usd=0.3, tok_s=88,
                  series_key="so1", series="Solo", tier="max")]
        pts, front, order = self._load(ms)
        line = R.series_row("Solo", [p for p in pts if p["k"] == "so1"])
        self.assertEqual(line, "Solo · max · 30 / —", line)

    def test_最优档只在真有前沿成员时标注(self):
        """一族可能整族被别家支配（更便宜还更高分）。那时写"最优 X（前沿）"就是编的。

        反过来也成立：被支配的那一档**什么都不后缀**（10-13 用户口径：只有帕累托前沿的
        需要标注）—— 每行都拖一个"（不在前沿）"就把要标的和不用标的拉成一样长。
        前沿成员由爬虫判（`on_frontier`），这里只消费 —— 所以夹具也走 annotate_efficiency，
        不手搓 f=True/False：手搓等于把被测判据替掉，判据坏了也测不出来。
        """
        # 每族两档：单点族不成族，前沿判定要两档以上才有意义（见 test_单点系列最优档留空）
        ms = [row("Dom (low)", creator="D", index=55, apt_usd=0.6, tok_s=100,
                  series_key="d1", series="Dom", tier="low"),
              row("Dom (max)", creator="D", index=60, apt_usd=0.5, tok_s=100,
                  series_key="d1", series="Dom", tier="max"),
              row("Weak (low)", creator="W", index=45, apt_usd=1.2, tok_s=100,
                  series_key="w1", series="Weak", tier="low"),
              row("Weak (max)", creator="W", index=50, apt_usd=1.0, tok_s=100,
                  series_key="w1", series="Weak", tier="max")]
        pts, front, order = self._load(ms)
        self.assertTrue(any(p["f"] for p in pts if p["c"] == "D"), "夹具里 Dom 那族必须在前沿上")
        self.assertFalse(any(p["f"] for p in pts if p["c"] == "W"), "被支配的 Weak 族不该在前沿上")
        self.assertIn("最优 max（前沿）", R.series_row("Dom", [p for p in pts if p["c"] == "D"]))
        weak = R.series_row("Weak", [p for p in pts if p["c"] == "W"])
        self.assertIn("最优 max", weak, "被支配也要给出这一族里最高的那档，只去掉标注")
        self.assertNotIn("前沿", weak, f"不在前沿不该有任何后缀 →「{weak}」")

    def test_统计只按图上真画的点算(self):
        pts, front, order = self._load(self._models())
        self.assertEqual(len(pts), 4, "V Ghost 没成本，画不出来")
        txt = R.vendor_stats("V", self._by_creator(pts)["V"])
        summary = txt.split("\n")[1]
        self.assertIn("智能指数v5 / 历史参照（各取族最高） 42~44", summary)
        self.assertIn("中位 43", summary, "每族只取最高档后，Alpha 44 / Beta 42 的中位是 43")
        self.assertNotIn("40~44", summary, "各取族最高后不该再把 Alpha 低档算进区间")
        self.assertIn("每任务成本 $0.5~$2（中位 $1）", txt)
        self.assertNotIn("70", txt)
        self.assertNotIn("999", txt)

    def test_综述卡给时间指标厂商中位(self):
        ms = [row("T Alpha", creator="T", index=40, apt_usd=1.0, tok_s=100,
                  time_per_task_s=100, total_s=10, ttft_s=3,
                  series_key="t1", series="T Alpha", tier="max"),
              row("T Beta", creator="T", index=41, apt_usd=2.0, tok_s=100,
                  time_per_task_s=200, total_s=20, ttft_s=4,
                  series_key="t2", series="T Beta", tier="max")]
        pts, _front, _order = self._load(ms)
        txt = R.vendor_stats("T", self._by_creator(pts)["T"])
        # 任务耗时 100/200 中位 150s=2.5min；思考 10-5=5s、20-5=15s 中位 10s；
        # 比值先逐档算（5%、7.5%）再取中位 6.25%→6.3%，不是两个中位相除。
        self.assertIn("时间中位：任务耗时 2.5min · 思考时间 10s · 思考前摇 6.3%（2/2 档）", txt)
        self.assertNotIn("前500", txt, "前500 列已撤掉，综述卡不能再提")

    def test_实测区间不含推估_汇总解释行不进卡(self):
        """用户口径（10-13）：删掉的是"同族推估 N 档…""同级参照带 N 档…"那两行**汇总解释**，
        逐档行留着。所以两头都得钉住：解释行不许回来，口径标记也不许跟着丢。"""
        pts, front, order = self._load(self._models())
        txt = R.vendor_stats("V", self._by_creator(pts)["V"])
        self.assertIn("输出速度 实测 2 档 100~200 tok/s（中位 150）", txt)
        self.assertNotIn("100~900", txt, "推估把实测区间撑开 = 把估计当实测")
        for kw in ("同族推估", "同级参照带"):
            self.assertNotIn(kw, txt, f"综述卡里不该再有「{kw}」那行解释")
        svg = R.build_svg(pts, front, order)
        body = "".join(re.findall(r'<g class="chip fam"[\s\S]*?</g>', svg))
        self.assertIn("≈900", body, "推估那档的逐档行必须还带 ≈，否则读起来就是实测")

    # ---------------------------------------------------------------- 族卡落点
    def test_族卡贴在中心点的左上角(self):
        """卡右下角对着那颗球；不越界时必须严丝合缝。"""
        pts, front, order = self._load(self._models())
        fam = [p for p in pts if p["k"] == "va1"]
        cx = lambda v: 300.0
        cy = lambda v: 200.0
        box = R.layout_family_card(fam, cx, cy)
        mid = R.family_center(fam, cx, cy)
        self.assertEqual(box["anchor"], "br")
        # 六列指标让卡比原宽度更大；球在画布中央时左侧放不下，只能夹到左边界。
        # 只要没被左边界夹住，卡右下角就必须严丝合缝对到球的左上。
        if box["x"] > R.PL + 2:
            self.assertAlmostEqual(box["x"] + box["w"], mid[0] - mid[2] - R.CHIP_GAP, delta=0.01,
                                   msg="卡右下角没对到球的左上")
        else:
            self.assertEqual(box["x"], R.PL + 2, "左侧放不下时应夹在左边界")
        self.assertAlmostEqual(box["y"] + box["h"], mid[1] - mid[2] - R.CHIP_GAP, delta=0.01)

    def test_族卡越界只夹紧不换侧(self):
        """球贴着左上角时卡片放不下：夹紧留在画布内，但不许翻到球的另一侧去。"""
        pts, front, order = self._load(self._models())
        fam = [p for p in pts if p["k"] == "va1"]
        box = R.layout_family_card(fam, lambda v: R.PL + 6.0, lambda v: R.PT + 8.0)
        self.assertGreaterEqual(box["x"], R.PL, "左缘出画")
        self.assertGreaterEqual(box["y"], R.PT, "顶缘出画")
        self.assertLessEqual(box["x"] + box["w"], R.W - R.PR)
        self.assertLessEqual(box["y"] + box["h"], R.H - R.PB)
        self.assertEqual(box["anchor"], "br", "越界也不改贴法（换侧就等于卡片离球太远）")

    def test_族卡每厂一张不漏且pri按强弱排(self):
        pts, front, order = self._load(self._models())
        svg = R.build_svg(pts, front, order)
        # 属性按名字取，不按出现顺序写死正则 —— 加一个 data-r0 就把整条判据弄红，
        # 那种红是判据脆，不是代码错
        cards = []
        got = []
        for m in re.finditer(r'<g class="chip fam"([^>]*)>', svg):
            a = dict(re.findall(r'(data-[a-z0-9]+)="([^"]*)"', m.group(1)))
            seg = svg[m.end():m.end() + 400]
            r = re.search(r'<rect x="([\d.\-]+)" y="([\d.\-]+)" width="([\d.]+)" height="([\d.]+)"', seg)
            got.append((a.get("data-anchor"), a.get("data-k"), a.get("data-c"), a.get("data-pri"), *r.groups()))
        cards = got
        self.assertEqual({k for _a, k, _c, _p, *_ in cards}, {"va1", "vb1", "wo1"},
                         "每个族都得有一张卡")
        for _a, _k, _c, _p, x, y, w, h in cards:
            x, y, w, h = float(x), float(y), float(w), float(h)
            self.assertGreaterEqual(x, R.PL - 0.5, "左缘出画")
            self.assertGreaterEqual(y, R.PT - 0.5, "顶缘出画")
            self.assertLessEqual(x + w, R.W - R.PR + 0.5, "右缘被切")
            self.assertLessEqual(y + h, R.H - R.PB + 0.5, "底缘被切")
        v = [int(p) for _a, _k, c, p, *_ in cards if c == "V"]
        self.assertEqual(sorted(v), [1, 2], "同一厂内的族卡 pri 从 1 连续排")

    def test_每档都在某张族卡里被说一次(self):
        pts, front, order = self._load(self._models())
        svg = R.build_svg(pts, front, order)
        body = "".join(re.findall(r'<g class="chip fam"[\s\S]*?</g>', svg))
        for p in pts:
            self.assertIn(f'{p["y"]:g} / {R.progress_cell(p)} · ${p["x"]:g}', body,
                          f"{p['n']} 的成对分数没出现在任何族卡里")

    # ---------------------------------------------------------------- 综述卡形状
    def test_综述卡栏宽自洽且文字不越卡(self):
        pts, front, order = self._load(self._models())
        svg = R.build_svg(pts, front, order)
        for m in re.finditer(r'<g class="chip lg"([^>]*)>([\s\S]*?)</g>', svg):
            a = dict(re.findall(r'(data-[a-z0-9]+)="([^"]*)"', m.group(1)))
            colsw, name, body = a.get("data-cols"), a.get("data-c"), m.group(2)
            r0 = re.search(r'<rect x="([\d.\-]+)" y="([\d.\-]+)" width="([\d.]+)" height="([\d.]+)"', body)
            x, y, w, h = r0.groups()
            x, y, w, h = float(x), float(y), float(w), float(h)
            self.assertAlmostEqual(sum(float(v) for v in colsw.split(",")), w, delta=0.6,
                                   msg=f"{name}：栏宽之和 ≠ 卡宽（文字会排到卡外）")
            for tx in re.findall(r'<text x="([\d.\-]+)"', body):
                self.assertTrue(x - 0.5 <= float(tx) <= x + w - 4, f"{name}：某栏文字起点跑到卡外")
            self.assertGreaterEqual(x, R.PL - 0.5)
            self.assertLessEqual(y + h, R.H - R.PB + 0.5)

    def test_综述卡只贴在左下角或右下角(self):
        """用户口径（10-13）：图例那张卡"尽可能放表格左下角或者右下角"。
        钉在底部两角才有可预期的位置，也才不会压到帕累托右上角那句标注。"""
        pts, front, order = self._load(self._models())
        svg = R.build_svg(pts, front, order)
        n = 0
        for m in re.finditer(r'<g class="chip lg"([^>]*)>([\s\S]*?)</g>', svg):
            body = m.group(2)
            x, y, w, h = (float(v) for v in re.search(
                r'<rect x="([\d.\-]+)" y="([\d.\-]+)" width="([\d.]+)" height="([\d.]+)"', body).groups())
            self.assertAlmostEqual(y + h, R.H - R.PB - 2, delta=0.6, msg="卡没贴在底边上")
            self.assertTrue(abs(x - (R.PL + 2)) < 0.6 or abs(x + w - (R.W - R.PR - 2)) < 0.6,
                            f"卡既不在左下角也不在右下角（x={x}）")
            n += 1
        self.assertGreater(n, 0, "一张综述卡都没有，本用例就是空转")

    def test_折行可还原且标点不劈开(self):
        s = "输出速度 实测 20 档 52~236 tok/s（中位 95.5）"
        self.assertEqual("".join(R.wrap_lines(s, 120)), s, "折行必须能逐字还原")
        rows = R.wrap_lines(s, 60)
        self.assertTrue(all(not r[0] in "）。，" for r in rows), f"收尾标点顶到行首：{rows}")
        self.assertTrue(all(not r[-1] in "（" for r in rows), f"开口括号留在行尾：{rows}")

    def test_中位取法与fetch侧同族合并一致(self):
        self.assertEqual(R.median([1, 2, 3, 4]), 2.5)
        self.assertEqual(R.median([1, 2, 3]), 2)
        self.assertEqual(R.span([7.5], " tok/s", "≈"), "≈7.5 tok/s", "单值不该写成 7.5~7.5")
        self.assertEqual(R.span([7.5, 9.0], " tok/s", "≈"), "≈7.5~≈9 tok/s")

    # ---------------------------------------------------------------- 开关与样式
    def test_顶栏开关关掉时一张卡都不显(self):
        body = R.HOVER_JS
        self.assertIn("window.MR_CARDS === 0", body, "共享 JS 里没有开关判断")
        self.assertIn("if (window.MR_CARDS === 0) return;", body,
                      "开关必须同时管住族卡与综述卡（它们都走 cardsOf 这一个口）")

    def test_卡片会按实际文字塌缩(self):
        body = R.HOVER_JS
        self.assertIn("function fitCard(", body)
        self.assertIn("getBBox()", body, "塌缩要量真实字宽")
        self.assertIn('data-anchor', R.HOVER_JS or "", "塌缩得按贴法钉住对应那一角")
        self.assertIn("var q = fitCard(ch);", body, "先塌缩再比位：拿未塌缩的尺寸比会白让位")

    def test_样式里卡片默认隐藏且不吃指针(self):
        self.assertIn(".chip{display:none;pointer-events:none}", R.HOVER_CSS)
        self.assertIn(".chip.hi{display:block}", R.HOVER_CSS)
        self.assertIn(".lgc{cursor:pointer}", R.HOVER_CSS)
        for var in ("--mr-bub", "--mr-bub-ink", "--mr-bub-line"):
            self.assertIn(var, R.HOVER_CSS, "卡片配色复用脚注浮层那三个变量，不另起一套")

    def test_厂商名转义不撕开标记(self):
        ms = self._models()
        for m in ms:
            m["creator"] = 'X&Y "<z>'
        pts, front, order = self._load(ms)
        svg = R.build_svg(pts, front, order)
        self.assertIn('data-c="X&amp;Y &quot;&lt;z&gt;"', svg)
        self.assertNotIn('data-c="X&Y', svg)


class TestTaskRadius(unittest.TestCase):
    """按任务耗时编码面积：u 由 log10 反比归一，面积 .04→1；缺失画 2px 空心环。

    口径：u = clamp((log10(tmax) − log10(t)) / (log10(tmax) − log10(tmin)), 0, 1)，
    面积分数 = .04 + .96u，r = 15·√分数。面积直接线性跟上 u，不再乘 u²；
    log10 与 ln 只差常数倍，在归一化比值里完全等价。所有点同耗时 u=.5，
    不偏袒最快或最慢。默认 TPS 半径 r 一个字节都不改，任务半径只新增在 data-r-task。
    """

    def test_公式与面积边界(self):
        self.assertEqual(R.TASK_MIN_FRACTION, 0.04)
        self.assertEqual(R.TASK_R_MIN, 0.2 * R.R_MAX)
        self.assertEqual(R.R_MISSING, R.R_MIN)
        # t 是任务耗时：10 秒最快（u=1, 面积 100%），1000 秒最慢（u=0, 面积 4%）。
        fast = R.task_radius(10, 10, 1000)
        slow = R.task_radius(1000, 10, 1000)
        self.assertEqual(fast, R.R_MAX, "最快点面积 100% => r=15")
        self.assertEqual(slow, R.TASK_R_MIN, "最慢点面积 4% => r=3")
        self.assertAlmostEqual((fast / R.R_MAX) ** 2, 1.0, places=12)
        self.assertAlmostEqual((slow / R.R_MAX) ** 2, 0.04, places=12)
        self.assertAlmostEqual((fast / slow) ** 2, 25.0, places=6,
                               msg="快慢面积比应为 1:25（.04 起点）")
        # log10 中点 u=.5 => 面积 .04+.96×.5=.52，r=10.8
        self.assertEqual(R.task_radius(100, 10, 1000), 10.8)
        self.assertAlmostEqual(R.task_radius(100, 10, 1000), 15 * math.sqrt(0.52), places=1)
        # 等对数步长：每差十倍耗时 u 走 .5 步，面积分数 1→.52→.04 等步下降
        areas = [(R.task_radius(t, 10, 1000) / R.R_MAX) ** 2 for t in (10, 100, 1000)]
        for got, want in zip(areas, (1.0, 0.52, 0.04)):
            self.assertAlmostEqual(got, want, delta=0.005)
        # 耗时越长半径越小（快者更大）
        radii = [R.task_radius(t, 10, 1000) for t in (10, 50, 100, 500, 1000)]
        self.assertEqual(radii, sorted(radii, reverse=True))
        # 全部同耗时 => u=.5，不能拿 0 或 1 偏袒
        self.assertEqual(R.task_radius(100, 100, 100), R.task_radius(50, 50, 50))
        self.assertAlmostEqual(R.task_radius(100, 100, 100), 15 * math.sqrt(0.52), places=1)
        # 域外值按端点夹紧
        self.assertEqual(R.task_radius(5, 10, 1000), R.R_MAX)
        self.assertEqual(R.task_radius(2000, 10, 1000), R.TASK_R_MIN)
        # 缺失：0/负/布尔/非有限/字符串都算没有，绝不拿 TPS 兜底
        for value in (0, -1, None, True, False, float("nan"),
                      float("inf"), float("-inf"), "12"):
            self.assertEqual(R.task_radius(value, 10, 1000), R.R_MISSING, value)
        for lo, hi in ((None, 1000), (10, None), (0, 1000), (10, 0),
                       (-1, 1000), (10, -1), (True, 1000),
                       (float("nan"), 1000), (float("inf"), 1000)):
            self.assertEqual(R.task_radius(100, lo, hi), R.R_MISSING, (lo, hi))

    def test_定义域只取图上有效耗时且不改TPS半径(self):
        pts = [{"tp": 10}, {"tp": 1000}, {"tp": None}, {"tp": 0},
               {"tp": -1}, {"tp": True}, {"tp": float("nan")}, {"tp": float("inf")}]
        self.assertEqual(R.task_time_domain(pts), (10.0, 1000.0),
                         "0/负/布尔/非有限都不是有效耗时")
        self.assertIsNone(R.task_time_domain([{"tp": None}, {"tp": 0}, {"tp": -1}]))
        probe = [{"tp": 10, "r": 7.5}, {"tp": 1000, "r": 12.0}, {"tp": None, "r": 2}]
        before = [p["r"] for p in probe]
        R.apply_task_radii(probe)
        self.assertEqual([p["r"] for p in probe], before, "TPS 半径不能被任务模式改写")
        self.assertEqual([p["r_task"] for p in probe], [15.0, 3.0, R.R_MISSING])
        self.assertEqual([p["task_missing"] for p in probe], [False, False, True])

    def test_SVG与切换钩子(self):
        models = [
            row("Quick (max)", creator="X", index=40, apt_usd=1.0, tok_s=100,
                time_per_task_s=10, series_key="quick", series="Quick", tier="max"),
            row("Slow (max)", creator="X", index=41, apt_usd=2.0, tok_s=50,
                time_per_task_s=1000, series_key="slow", series="Slow", tier="max"),
            row("Band (max)", creator="X", index=42, apt_usd=3.0, tok_s=None,
                time_per_task_s=100, series_key="band", series="Band", tier="max",
                tok_s_band=[50.0, 120.0], tok_s_band_n=3, tok_s_band_same_creator=0,
                tok_s_band_note="同级能力参照：刻意不给中心值"),
            row("Missing (max)", creator="X", index=43, apt_usd=4.0, tok_s=25,
                time_per_task_s=None, series_key="miss", series="Missing", tier="max"),
        ]
        pts, front, order = TestLegendHover()._load(models)
        before = [(p["n"], p["r"]) for p in pts]
        svg = R.build_svg(pts, front, order)
        self.assertEqual([(p["n"], p["r"]) for p in pts], before,
                         "默认 TPS 半径 r 必须原样保留")
        self.assertIn('data-r-task=', svg)
        self.assertIn('data-tp="1000"', svg)
        self.assertIn('data-band-ring="1"', svg, "参照带环带仍在，切换靠 CSS 隐藏而不是删节点")
        # 每个 mk 组都要带 data-r/data-r-task；缺耗时的组带 data-task-missing 且 r_task=2
        groups = re.findall(r'<g class="mk[^"]*"[^>]*>', svg)
        self.assertEqual(len(groups), len(pts))
        for g in groups:
            name = re.search(r'data-n="([^"]+)"', g).group(1)
            r = float(re.search(r'data-r="([\d.]+)"', g).group(1))
            rt = float(re.search(r'data-r-task="([\d.]+)"', g).group(1))
            if name == "Missing (max)":
                self.assertIn('data-task-missing="1"', g)
                self.assertEqual(rt, R.R_MISSING)
            elif name == "Quick (max)":
                self.assertEqual(rt, R.R_MAX)
            elif name == "Slow (max)":
                self.assertEqual(rt, R.TASK_R_MIN)
            elif name == "Band (max)":
                self.assertEqual(rt, 10.8, "中间耗时面积 .52")
            self.assertTrue(math.isfinite(r) and r > 0)
        html = R.build_html(svg, "2026-10-13 07:30", False)
        self.assertIn('id="task-size"', html)
        self.assertIn('role="switch"', html)
        self.assertIn('aria-checked="false"', html, "默认关闭且无障碍状态同步")
        self.assertIn('id="size-caption"', html)
        self.assertIn('气泡面积 = 输出速度', html)
        self.assertIn('svg[data-size-mode="task"] [data-band-ring]{display:none}', html,
                      "任务模式必须隐藏环带但保留节点，切回才能恢复")
        self.assertIn('svg[data-size-mode="task"] .mk[data-task-missing="1"] '
                      'circle[data-role="node"]{fill:none;pointer-events:all}', html,
                      "缺任务耗时要画空心环且仍可悬停")
        self.assertIn("__mrRelayout", html, "切换后要能按新半径重贴当前悬停卡")
        self.assertIn("aria-checked", R.SIZE_JS, "切换必须同步 switch 的无障碍状态")
        self.assertIn("data-size-mode", R.SIZE_JS)


class TestThinkRatio(unittest.TestCase):
    """思考前摇 = think ÷ task × 100，只做除法；0 有效、>100 不夹、缺值留空。

    分子与分母来自不同评测口径（思考含首回答前等待，任务耗时是官方解码估计），
    所以这是估计值之比，不是任务内部时间分解。脚注必须把这一点说清。
    """

    def test_公式与边界(self):
        self.assertEqual(R.think_ratio(0, 100), 0.0, "0 是有效比值，不能当缺失")
        self.assertEqual(R.think_ratio(950, 100), 950.0, ">100% 不夹到 100")
        self.assertAlmostEqual(R.think_ratio(1, 3), 100 / 3, places=12)
        for think, task in ((None, 100), (1, None), (1, 0), (1, -5), (-1, 100),
                            (True, 100), (1, True), (float("nan"), 100),
                            (1, float("nan")), (float("inf"), 100), (1, float("inf"))):
            self.assertIsNone(R.think_ratio(think, task), (think, task))

    def test_文本half_up且不带多余零(self):
        for value, want in ((0, "0"), (1.25, "1.3"), (6.25, "6.3"), (50.0, "50"),
                            (109.75, "109.8"), (955.0, "955"), (0.04, "0")):
            self.assertEqual(R.think_ratio_text(value), want, value)
        for value in (None, float("nan"), float("inf"), float("-inf"), -1,
                      True, False, "12"):
            self.assertEqual(R.think_ratio_text(value), "—", value)

    def test_点上优先级与缺值(self):
        self.assertEqual(R.think_ratio_cell({"think": 5, "tp": 100}), "5%")
        self.assertEqual(R.think_ratio_cell({"think_ratio": 950.0, "think": 1, "tp": 100}),
                         "950%", "点上的派生值优先")
        self.assertEqual(R.think_ratio_cell({"ratio": 12.5, "think": 1, "tp": 100}),
                         "12.5%", "旧键 ratio 仍可消费")
        self.assertEqual(R.think_ratio_cell({"think_ratio": None, "think": 5, "tp": 100}),
                         "5%", "派生键无效时按同一公式现算，不复制第二套口径")
        for p in ({"think": 5, "tp": None}, {"think": None, "tp": 100},
                  {"think": 5, "tp": 0}, {"think": 5, "tp": -1}):
            self.assertEqual(R.think_ratio_cell(p), "—", p)

    def test_三处渲染面与不同基准说明(self):
        models = [
            row("Ratio Hundred (max)", creator="R", index=60, apt_usd=1.0, tok_s=100,
                total_s=105, time_per_task_s=100,
                series_key="r100", series="Ratio Hundred", tier="max"),
            row("Ratio Over (max)", creator="R", index=61, apt_usd=2.0, tok_s=100,
                total_s=200, time_per_task_s=100,
                series_key="rover", series="Ratio Over", tier="max"),
        ]
        pts, front, order = TestLegendHover()._load(models)
        by = {p["n"]: p for p in pts}
        hundred, over = by["Ratio Hundred (max)"], by["Ratio Over (max)"]
        # think = total − 500/100：105−5=100（100%）、200−5=195（195%）。
        self.assertEqual(hundred["think_ratio"], 100.0)
        self.assertEqual(over["think_ratio"], 195.0)
        self.assertIn("思考前摇 100%", R.point_tip(hundred))
        self.assertIn("思考前摇 195%", R.point_tip(over))
        self.assertEqual(R.family_card_lines([hundred])[3],
                         "max · 60 / — · $1 · 1.7min · 1.7min · 100% · 100")
        self.assertEqual(R.family_card_lines([over])[3],
                         "max · 61 / — · $2 · 1.7min · 3.3min · 195% · 100")
        self.assertIn("思考前摇 147.5%（2/2 档）",
                      R.vendor_stats("R", [hundred, over]),
                      "先逐档算比值再取中位（100、195 → 147.5），不是两个中位相除")
        svg = R.build_svg(pts, front, order)
        self.assertIn("思考前摇 100%", svg)
        self.assertIn("思考前摇 195%", svg, "族卡/SVG 不把 >100% 夹成 100%")
        html = R.build_html(svg, "2026-10-13 07:30", False)
        self.assertIn("思考前摇为不同基准估计值之比，非任务内时间分解", html,
                      "必须在脚注说清两个口径不同，避免被读成任务内分解")


class TestTipCardParity(unittest.TestCase):
    """悬停与族卡是同一批数字的两个出口，同一个数必须同一写法。

    真实触发：`GPT-6.2 Astra (max)` 每任务成本恰好 4.0，悬停写 `$4.0`、族卡写 `$4`，
    族卡成对分数的逐字校验因此判红 —— 两处必须共用一种格式。
    """

    def _pts(self):
        models = [
            row("T Alpha (max)", creator="T", index=60, apt_usd=4.0, tok_s=100,
                series_key="ta", series="Alpha", tier="max"),
            row("T Alpha (high)", creator="T", index=55, apt_usd=0.38, tok_s=80,
                series_key="ta", series="Alpha", tier="high"),
        ]
        return TestLegendHover()._load(models)[0]

    def test_整值价两处都不带多余小数(self):
        pts = self._pts()
        by = {p["n"]: p for p in pts}
        tip = R.point_tip(by["T Alpha (max)"])
        self.assertIn(" · $4", tip)
        self.assertNotIn("$4.0", tip, "整值价不能被写成 $4.0 而与族卡分叉")
        lines = R.family_card_lines([p for p in pts if p["k"] == "ta"])
        self.assertTrue(any(r.startswith("max · 60 / — · $4 ·") for r in lines), lines)
        self.assertFalse(any("$4.0" in r for r in lines), lines)

    def test_小数价两处都保留有效位(self):
        pts = self._pts()
        by = {p["n"]: p for p in pts}
        self.assertIn(" · $0.38", R.point_tip(by["T Alpha (high)"]))
        lines = R.family_card_lines([p for p in pts if p["k"] == "ta"])
        self.assertTrue(any(r.startswith("high · 55 / — · $0.38 ·") for r in lines), lines)


class TestProgressScores(unittest.TestCase):
    """AA Progress 必须按「模型 × 推理档」原样展示，缺分不能借用别档的分。

    三处口径一起测：节点悬停、族卡逐档、厂商卡，智能指数与 Progress 均成对显示；
    厂商聚合每族只投一票（最高指数与最高 Progress），缺分保持破折号。
    """

    def _models(self):
        return [
            row("P Alpha (low)", creator="P", index=40, apt_usd=0.5, tok_s=100,
                series_key="pa", series="Alpha", tier="low",
                progress_index=10, progress_version="v1"),
            row("P Alpha (max)", creator="P", index=44, apt_usd=2.0, tok_s=200,
                series_key="pa", series="Alpha", tier="max",
                progress_index=90, progress_version="v1"),
            row("P Beta (low)", creator="P", index=45, apt_usd=1.0, tok_s=50,
                series_key="pb", series="Beta", tier="low",
                progress_index=50, progress_version="v1"),
            # 同族上一档有 50 分，这一档仍必须显示不可用（不许把最高分补下来）
            row("P Beta (max)", creator="P", index=46, apt_usd=3.0, tok_s=60,
                series_key="pb", series="Beta", tier="max",
                progress_index=None, progress_version="v1"),
            # 整族没有 Progress：覆盖数要把它算进分母
            row("P Gamma", creator="P", index=30, apt_usd=0.2, tok_s=70,
                series_key="pg", series="Gamma", tier="max",
                progress_index=None, progress_version="v1"),
        ]

    def _pts(self):
        return TestLegendHover()._load(self._models())[0]

    def test_节点悬停按成对格式给分或缺分(self):
        pts = self._pts()
        by = {p["n"]: p for p in pts}
        self.assertIn("智能指数v5 / 历史参照v1：40 / 10", R.point_tip(by["P Alpha (low)"]))
        self.assertIn("智能指数v5 / 历史参照v1：44 / 90", R.point_tip(by["P Alpha (max)"]))
        beta = R.point_tip(by["P Beta (max)"])
        self.assertIn("：46 / —", beta, "缺分保持破折号")
        self.assertNotIn("：46 / 50", beta, "缺分的那档不能借用同族最高分")
        self.assertNotIn("：46 / 0", beta, "缺分不能写成 0")
        gamma = R.point_tip(by["P Gamma"])
        self.assertIn("：30 / —", gamma)
        self.assertNotIn("：30 / 0", gamma)

    def test_族卡逐档成对给分且缺分保持破折号(self):
        pts = self._pts()
        alpha = [p for p in pts if p["k"] == "pa"]
        lines = R.family_card_lines(alpha)
        self.assertEqual(lines[1:3], ["智能指数v5 / 历史参照v1 · $每任务",
                                      "任务耗时(估) · tok/s"])
        self.assertEqual(lines[3:5], ["max · 44 / 90 · $2 · — · 200",
                                      "low · 40 / 10 · $0.5 · — · 100"],
                         "每档把智能指数与 Progress 成对写出，按档位反序；无总响应/速度时时间留空")
        self.assertEqual(len(lines), 5, "两档就两行，不再有 Progress 汇总/明细行")
        self.assertFalse(any("Progress" in r or " · P" in r for r in lines),
                         "Progress 已并入每档行，不再单列")

        beta = [p for p in pts if p["k"] == "pb"]
        blines = R.family_card_lines(beta)
        self.assertEqual(blines[3:5], ["max · 46 / — · $3 · — · 60",
                                       "low · 45 / 50 · $1 · — · 50"],
                         "缺分也要逐档写原始指标，且按档位反序")
        self.assertIn(" / — ", blines[3], "缺分保持破折号，不能整行消失也不能借用别档")
        self.assertNotIn(" / 0 ", blines[3])

        gamma = [p for p in pts if p["k"] == "pg"]
        glines = R.family_card_lines(gamma)
        self.assertEqual(glines[3], "max · 30 / — · $0.2 · — · 70", "缺分档仍保留破折号")
        self.assertEqual(len(glines), 4)

    def test_厂商聚合每族各取最高再算中位范围(self):
        pts = self._pts()
        pv = [p for p in pts if p["c"] == "P"]
        txt = R.vendor_stats("P", pv)
        lines = txt.split("\n")
        line = next(l for l in lines if l.startswith("智能指数v5 / 历史参照v1"))
        self.assertIn("（各取族最高）", line, "厂商聚合只取每族最高档")
        self.assertIn("30~46 / 50~90", line,
                      "指数取各族最高 y，Progress 取各族最高 pi；每族只投一票")
        self.assertIn("（中位 44 / 70", line)
        self.assertIn("2/3 族", line, "Gamma 整族缺分要写进历史覆盖")
        self.assertNotIn("10~90", line, "不能把低档 Progress 混进聚合")
        self.assertNotIn("中位 50", line, "逐档混算（10/90/50）正是要防住的多档偏差")
        self.assertFalse(any(l.startswith("Progress ") for l in lines),
                         "Progress 已并入厂商双评分行，不再单列")
        self.assertGreater(lines.index(line), 0)

    def test_三处产物都真的写进SVG(self):
        pts, front, order = TestLegendHover()._load(self._models())
        svg = R.build_svg(pts, front, order)
        self.assertIn("智能指数v5 / 历史参照v1：40 / 10", svg, "节点 <title>")
        self.assertIn("智能指数v5 / 历史参照v1：46 / —", svg, "缺分节点 <title>")
        self.assertIn("智能指数v5 / 历史参照v1 · $每任务", svg, "族卡列名")
        self.assertIn("任务耗时(估) · tok/s", svg, "新增时间列列名")
        self.assertNotIn("思考时间", svg, "夹具没有思考数据时这两列整列不出现，不留破折号")
        self.assertIn("（各取族最高）", svg, "厂商卡双评分")
        self.assertNotIn("Progress", svg, "旧 Progress 汇总/明细字样不得回来")

    def test_标题带当前智能指数版本且脚注不再重复(self):
        page = R.build_html("<svg/>", "2026-10-28 07:30", False, evaluation_version="5.0")
        self.assertIn("<title>速度与成本性价比定位图 v5</title>", page,
                      "5.0 只在有小数时裁掉尾零")
        self.assertNotIn("智能指数版本", page, "旧脚注版本标签必须消失")
        page61 = R.build_html("<svg/>", "2026-10-28 07:30", False, evaluation_version="6.1")
        self.assertIn("<title>速度与成本性价比定位图 v6.1</title>", page61,
                      "只裁 .0 尾零，6.1 必须原样保留")
        self.assertNotIn("智能指数版本", page61)
        no_ver = R.build_html("<svg/>", "2026-10-28 07:30", False)
        self.assertIn("<title>速度与成本性价比定位图</title>", no_ver, "载荷没给版本时不许编一个")
        self.assertNotIn("智能指数版本", no_ver)


class TestNotableMoves(unittest.TestCase):
    """达门槛的变动必须被挑出来交给 alert_policy 出声；门槛之下的不许吵。

    门槛是用户给的口径（指数 >1 分 / 每任务成本与输出速度 >20%，10-12 从 10% 提上来），
    所以"恰好等于门槛"
    这一格必须实测 —— 写成 `>=` 与写成 `>` 在真实数据上常常看不出差别。
    """

    def fld(self, field, a, b, kind="changed"):
        label = F.DIFF_LABEL[field]
        if kind != "changed":
            return {"field": field, "label": label, "from": a, "to": b,
                    "delta": None, "pct": None, "kind": kind}
        return {"field": field, "label": label, "from": a, "to": b,
                "delta": round(b - a, 6), "pct": round((b - a) / a * 100, 2) if a else None,
                "kind": "changed"}

    def ch(self, name, fields, type_="changed"):
        return {"name": name, "type": type_, "fields": fields}

    def test_指数按绝对分差_恰好1分不报(self):
        got = F.notable_moves([self.ch("A", [self.fld("index", 40, 41)])])
        self.assertEqual(got, [], "指数 +1 分正好在门槛上，不该报")
        got2 = F.notable_moves([self.ch("A", [self.fld("index", 40, 42)])])
        self.assertEqual([m["field"] for m in got2], ["index"])
        self.assertEqual(got2[0]["unit"], "分")
        self.assertIn("智能指数 40→42（+2分）", got2[0]["text"])

    def test_百分比门槛恰好等于不报刚过才报(self):
        """数字全部由 `MOVE_PCT` 推出来 —— 门槛改了（10% → 20% 就是今天改的）这条不会烂。"""
        thr = F.MOVE_PCT
        self.assertEqual(F.notable_moves([self.ch("A", [self.fld("apt_usd", 1.0, 1 + thr / 100)])]), [],
                         f"恰好 {thr}% 不该报（门槛是严格大于）")
        one = F.notable_moves([self.ch("A", [self.fld("apt_usd", 1.0, 1 + (thr + 2) / 100)])])
        self.assertEqual([m["field"] for m in one], ["apt_usd"], f"{thr + 2}% 该报")
        self.assertIn(f"$1→$1.{thr + 2:02d}", one[0]["text"])
        down = F.notable_moves([self.ch("B", [self.fld("tok_s", 150, 150 * (1 - (thr + 10) / 100))])])
        self.assertEqual([m["field"] for m in down], ["tok_s"])
        self.assertIn(f"-{thr + 10}%", down[0]["text"])

    def test_规则之外的字段与缺失值都不报(self):
        # 输入单价/首块延迟不在门槛里；"没有→有"没有基数；新入榜/掉榜归变动卡与族缺口告警
        self.assertEqual(F.notable_moves([self.ch("A", [self.fld("in_per_1m_usd", 1.0, 9.0)])]), [])
        self.assertEqual(F.notable_moves([self.ch("A", [self.fld("ttft_s", 1.0, 5.0)])]), [])
        self.assertEqual(F.notable_moves([self.ch("A", [self.fld("tok_s", None, 200, "appeared")])]), [])
        self.assertEqual(F.notable_moves([self.ch("A", [], "new")]), [])
        self.assertEqual(F.notable_moves([self.ch("A", [], "gone")]), [])

    def test_按幅度降序排_名字顺序不能干扰(self):
        """故意让"字母序"与"幅度序"相反 —— 否则拿名字排序的实现也照样通过。"""
        ms = F.notable_moves([self.ch("AAA", [self.fld("index", 40, 43)]),      # +3 分
                              self.ch("ZZZ", [self.fld("index", 40, 50)])])    # +10 分
        self.assertEqual([m["name"] for m in ms], ["ZZZ", "AAA"],
                         "通知文案只有 200 字，幅度大的必须先说")
        # 基线是 0 时 pct 为 None（除零），不能被当成"变动 0%"或崩掉
        zero_base = [self.ch("C", [{"field": "apt_usd", "label": "每任务成本", "from": 0,
                                    "to": 2.0, "delta": 2.0, "pct": None, "kind": "changed"}])]
        self.assertEqual(F.notable_moves(zero_base), [])
        self.assertEqual(F.notable_moves([]), [])
        self.assertEqual(F.notable_moves(None), [])

    def test_alert_policy_moved的文案_去重键与语气(self):
        p = {"data_state": "ok", "generated_at": GEN_TS,
             "content_hash": "deadbeef", "series_gaps": [],
             "moves": [{"name": f"M{i}", "field": "index", "label": "智能指数",
                        "amount": 9 - i, "unit": "分", "text": f"M{i} 智能指数 +{9 - i}分"}
                       for i in range(5)]}
        a = F.alert_policy(p)
        self.assertEqual(a["kind"], "moved")
        self.assertEqual(a["tone"], "info", "正常的变动不能顶着故障的语气")
        self.assertEqual(a["key"], f"alert:moved:{_NOW8.strftime('%Y-%m-%d')}",
                         "去重键要按天（写死日期会在第二天自己腐烂）")
        self.assertIn("这轮有 5 项", a["text"])
        self.assertIn("…等另 2 条", a["text"])
        self.assertIn("M0 智能指数 +9分", a["text"])
        self.assertNotIn("M4", a["text"], "只列前 3 项，其余并进计数")
        self.assertLessEqual(len(a["text"]), 200)

    def test_优先级_源状态与整族缺口都压过moved(self):
        moves = [{"name": "A", "field": "index", "label": "智能指数", "amount": 3,
                  "unit": "分", "text": "A 智能指数 +3分"}]
        base = {"generated_at": GEN_TS, "content_hash": "x", "moves": moves}
        degraded = F.alert_policy({**base, "data_state": "degraded", "data_note": "OR 挂了",
                                   "series_gaps": []})
        self.assertEqual(degraded["kind"], "degraded")
        self.assertEqual(degraded["tone"], "warn")
        gone = F.alert_policy({**base, "data_state": "ok",
                               "series_gaps": [{"label": "GPT-6 Sol", "n_prev": 6}]})
        self.assertEqual(gone["kind"], "series-gone", "一族整条没了比几条变价更该先说")
        # moved 也必须压过"太久没更新"：这一格嵌在 else 里，放错位置会互相覆写
        old = F.alert_policy({**base, "data_state": "ok", "series_gaps": [],
                              "generated_at": "2026-10-01 07:30:00"})
        self.assertEqual(old["kind"], "moved")
        self.assertEqual(F.alert_policy({**base, "data_state": "ok", "series_gaps": [],
                                         "moves": [],
                                         "generated_at": "2026-10-01 07:30:00"})["kind"],
                         "not-running")

    def test_文案超长也被截到200字内(self):
        """通知正文有硬上限（横幅会被系统截掉），所以自己先截 —— 不截就是判据没活干。"""
        moves = [{"name": f"一个很长的模型名字 {i}", "field": "index", "label": "智能指数",
                  "amount": 40 - i, "unit": "分",
                  "text": f"一个很长的模型名字 {i} 智能指数 10→{50 - i}（+{40 - i}分）"}
                 for i in range(30)]
        a = F.alert_policy({"data_state": "ok", "generated_at": GEN_TS,
                            "content_hash": "x", "series_gaps": [], "moves": moves})
        self.assertEqual(a["kind"], "moved")
        self.assertLessEqual(len(a["text"]), 200, a["text"])
        self.assertIn("…等另 27 条", a["text"])

    def test_没有moves时结论照旧是fresh(self):
        a = F.alert_policy({"data_state": "ok", "generated_at": GEN_TS,
                            "content_hash": "abc", "series_gaps": [], "moves": []})
        self.assertEqual((a["kind"], a["tone"], a["key"]), ("fresh", "info", "abc"))
        legacy = F.alert_policy({"data_state": "ok", "generated_at": GEN_TS,
                                 "content_hash": "abc", "series_gaps": []})
        self.assertEqual(legacy["kind"], "fresh", "旧载荷没有 moves 字段也不能崩")


class TestMoveAlertWiring(unittest.TestCase):
    """门槛判定要长在**抓取管线**里：只在纯函数对过，等于没接上。"""

    def _aa(self, rows):
        body = "".join(
            f"<tr><td>{n}</td><td>200K</td><td>{c}</td><td>{idx}</td>"
            f"<td>$0.50</td><td>{tps}</td><td>1.2</td><td>3.4</td></tr>"
            for n, c, idx, tps in rows)
        return aa_page(body)

    def _rounds(self, pages):
        import mr_paths
        logs: list[str] = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            data.mkdir()
            cfg = root / "cfg.json"
            cfg.write_text(json.dumps({"keyword_include": ["gpt"], "keyword_exclude": []}),
                           encoding="utf-8")
            feed = {"i": 0}

            def fake_fetch(url):
                if url == F.AA_METHODOLOGY:
                    return b"Artificial Analysis Intelligence Index v5.0"
                page = pages[feed["i"]]
                feed["i"] += 1
                return page

            with support_path_stack(mr_paths, root), \
                 unittest.mock.patch.object(mr_paths, "SUPPORT", root), \
                 unittest.mock.patch.object(mr_paths, "DATA", data), \
                 unittest.mock.patch.object(mr_paths, "REPO_DATA", root / "repo"), \
                 unittest.mock.patch.object(F, "DB_PATH", data / "t.db"), \
                 unittest.mock.patch.object(F, "LATEST_JSON", data / "latest.json"), \
                 unittest.mock.patch.object(F, "LATEST_JS", data / "latest.js"), \
                 unittest.mock.patch.object(F, "CFG_PATH", cfg), \
                 unittest.mock.patch.object(F, "fetch", fake_fetch), \
                 unittest.mock.patch.object(F, "log", lambda m: logs.append(m)), \
                 unittest.mock.patch.object(F, "notify_fresh", lambda p: None), \
                 unittest.mock.patch.object(F, "maybe_summarize", lambda p: None):
                out = []
                for _ in pages:
                    before = len(logs)
                    p = F.run(["aa"], False)
                    out.append((p, logs[before:]))
                return out

    def test_指数跳2分这轮就出声_跳1分不出(self):
        rounds = self._rounds([
            self._aa([("GPT-6 Sol (max)", "OpenAI", 40, 100)]),
            self._aa([("GPT-6 Sol (max)", "OpenAI", 41, 100)]),      # Δ1 分：门槛上，不报
            self._aa([("GPT-6 Sol (max)", "OpenAI", 43, 100)]),      # Δ2 分：报
            self._aa([("GPT-6 Sol (max)", "OpenAI", 43, 100)]),      # 一模一样：不报
        ])
        # 首轮没有可比基线，changes=[]，不能把"第一次见到"当成变动刷屏
        self.assertEqual(rounds[0][0]["moves"], [])
        self.assertEqual(rounds[0][0]["alert"]["kind"], "fresh")
        self.assertEqual(rounds[1][0]["moves"], [], rounds[1][1])
        self.assertEqual(rounds[1][0]["alert"]["kind"], "fresh")
        m = rounds[2][0]["moves"]
        self.assertEqual([(x["field"], x["amount"]) for x in m], [("index", 2.0)])
        self.assertEqual(rounds[2][0]["alert"]["kind"], "moved")
        self.assertEqual(rounds[2][0]["alert"]["tone"], "info")
        self.assertIn("智能指数 41→43（+2分）", rounds[2][0]["alert"]["text"])
        self.assertTrue(any("值得提醒的变动" in l for l in rounds[2][1]), rounds[2][1])
        self.assertEqual(rounds[3][0]["moves"], [])
        self.assertEqual(rounds[3][0]["alert"]["kind"], "fresh")

    def test_载荷里的明细已经是合并行(self):
        """浮层与横幅都读载荷里的 `move_lines` —— 爬虫侧不合并，产物就不会合并。

        这类"只在纯函数对过、没接到写盘那条路"的错本项目栽过好几次（tip 漏传、
        prev_board 读晚），所以直接跑 `run()` 看载荷。
        """
        tiers = [(f"GPT-6 Sol ({v})", "OpenAI", 40, 100) for v in ("max", "high", "medium", "low")]
        up = [(n, c, i, int(t * 1.5)) for n, c, i, t in tiers]
        rounds = self._rounds([self._aa(tiers), self._aa(up)])
        p = rounds[1][0]
        self.assertEqual(len(p["moves"]), 4, [m["text"] for m in p["moves"]])
        self.assertEqual(len(p["move_lines"]), 1, p["move_lines"])
        self.assertIn("GPT-6 Sol 输出速度 4 档同向 +50.0%", p["move_lines"][0])
        self.assertIn("中位 +50.0%", p["move_lines"][0])
        # 同一秒内两轮会被 INSERT OR REPLACE 合成一条快照（生产是分钟级间隔，这里不依赖它）
        self.assertIn("lines", p["cumulative"] if p["cumulative"] else {"lines": p["move_lines"]},
                     "累计结果必须带 lines，浮层与横幅共用一份")

    def test_速度变动也走同一条(self):
        rounds = self._rounds([
            self._aa([("GPT-6 Sol (max)", "OpenAI", 40, 100)]),
            self._aa([("GPT-6 Sol (max)", "OpenAI", 40, 160)]),      # tok_s +60%
        ])
        m = rounds[1][0]["moves"]
        self.assertEqual([x["field"] for x in m], ["tok_s"])
        self.assertEqual(rounds[1][0]["alert"]["kind"], "moved")
        self.assertIn("输出速度 100→160 tok/s（+60%）", rounds[1][0]["alert"]["text"])


class TestFootTooltip(unittest.TestCase):
    """脚注"抓取于 …"的悬停提示：通知那条结论要能在原地再读到。

    这里最怕两件事：① 文案在渲染面重算一遍，于是横幅说一套、悬停说另一套；
    ② 提示里嵌着远端模型名，`"` 能提前关掉 title 属性、把文本变成注入面。
    """

    def _html(self, alert):
        D = {"generated_at": GEN_TS, "data_state": "ok", "alert": alert}
        tip = R.foot_tip(D)
        page = R.build_html("<svg/>", D["generated_at"], False, tip=tip)
        return tip, page

    def test_正常更新也说清结论与门槛(self):
        a = F.alert_policy({"data_state": "ok", "generated_at": GEN_TS,
                            "content_hash": "abc", "series_gaps": [], "moves": []})
        tip, page = self._html(a)
        self.assertIn("本轮结论：正常更新，没有达门槛的变动（fresh）", tip)
        self.assertIn("这轮没有需要提醒的变动", tip)
        self.assertIn("门槛", tip)
        self.assertRegex(page, r'<span class="tip">抓取于 <b>', "脚注没被包成可悬停的 span")
        self.assertRegex(page, r'<span class="bub"[^>]*>', "浮层没画出来")
        self.assertNotIn('class="tip" title=', page,
                         "原生 title 在 popover 里根本不弹，留着还会与自绘浮层撞成两套")

    def test_门槛文案与规则常量同源(self):
        """改常量而不改文案，这条就该红 —— 否则提示会在说过期的话。"""
        a = F.alert_policy({"data_state": "ok", "generated_at": GEN_TS,
                            "content_hash": "x", "series_gaps": [], "moves": []})
        tip, _ = self._html(a)
        for field, how, thr in F.MOVE_RULES:
            unit = " 分" if how == "abs" else "%"
            self.assertIn(f"{F.DIFF_LABEL[field]} >{thr}{unit}", tip,
                          f"提示里没有 {field} 的当前门槛")
        self.assertIn(f">{F.MOVE_INDEX_POINTS} 分", tip)
        self.assertIn(f">{F.MOVE_PCT}%", tip)

    def test_moved时把通知全文原样带上(self):
        a = F.alert_policy({"data_state": "ok", "generated_at": GEN_TS,
                            "content_hash": "x", "series_gaps": [],
                            "moves": [{"name": "A", "field": "index", "label": "智能指数",
                                       "amount": 3.0, "unit": "分",
                                       "text": "A 智能指数 40→43（+3分）"}]})
        tip, _ = self._html(a)
        self.assertIn("有达门槛的变动（moved）", tip)
        self.assertIn("A 智能指数 40→43（+3分）", tip)

    def test_浮层文本里的标签与尖括号被转义(self):
        """注入面从属性换成了元素内容：`<` 能关掉 .bub，后果一样。"""
        evil = 'A" onmouseover="alert(1) <x></span><img src=x>'
        a = {"kind": "moved", "tone": "info", "label": "有达门槛的变动", "key": "k",
             "text": f"这轮有 1 项：{evil}", "hint": F.move_hint()}
        tip, page = self._html(a)
        seg = re.search(r'<span class="bub"[^>]*>([\s\S]*?)</span></span>', page).group(1)
        self.assertNotIn("<", seg, "裸尖括号会提前关掉浮层、后半段掉进页面")
        self.assertIn("&lt;x&gt;", seg)
        self.assertIn("&lt;/span&gt;&lt;img", seg, "关标签的串没被转义")
        self.assertIn('A" onmouseover="alert(1)', seg, "文本里的裸引号无害，不该被改写成实体")

    def test_浮层靠hover显示且默认隐藏(self):
        """失效面就是这个浮层出不出现：默认必须藏、:hover 必须让它显。"""
        _tip, page = self._html({"kind": "fresh", "label": "k", "key": "x", "text": "", "hint": ""})
        self.assertRegex(page, r"\.foot \.bub\{[^}]*display:none", "浮层没有默认隐藏 → 一直挡在图上")
        self.assertRegex(page, r"\.foot \.tip:hover \.bub,\.foot \.fgrp:hover \.bub,"
                               r"\.foot \.fgrp:focus \.bub\{display:block\}",
                         "没有 hover/focus 展开规则 → 永远不出现")
        self.assertRegex(page, r"\.bub\{[^}]*position:absolute[^}]*bottom:calc\(100%", "浮层没往时间戳上方定位，弹了也错位")
        # 定位上下文只能是整条脚注：挂回内联 .tip 上，绝对定位会按它的 ~140px 宽
        # 做 shrink-to-fit，四行文案挤成一根竖条（实测过：530px → 141px、84px → 221px）
        self.assertRegex(page, r"\.foot\{position:relative", "浮层没有定位上下文 → 会相对整页定位")
        self.assertNotRegex(page, r"\.foot \.tip\{[^}]*position:relative",
                            ".tip 自己带 position:relative 会把浮层挤成竖条")
        self.assertIn("max-width:calc(100% - 24px)", page, "浮层宽度要按脚注宽度收，不按视口")
        self.assertIn("white-space:pre-line", page, "四行文案里的换行会被吞成空格")
        self.assertEqual(page.count("--mr-bub:"), 2, "浮层底色要深浅两套各给一个值")
        self.assertEqual(page.count("--mr-bub-ink:"), 2, "浮层文字色也要两套")
        self.assertIn("var(--mr-bub)", page, "底色得走变量而不是写死一种外观")

    def test_旧载荷没有alert字段也不崩(self):
        """组件与渲染面都会读旧文件：缺 alert / 缺 hint 时兜个能读的结论，不抛。"""
        tip = R.foot_tip({"data_state": "degraded"})
        self.assertIn("degraded", tip)
        tip2 = R.foot_tip({})
        self.assertIn("fresh", tip2)
        tip3 = R.foot_tip({"alert": {"kind": "moved", "text": "只有正文"}})
        self.assertIn("只有正文", tip3)


class TestBacktrack(unittest.TestCase):
    """本轮没达门槛时，往前翻台账要找对那一轮，并报出**对比的两个快照时间**。

    动机是实测到的：11:40 那轮弹过 5 项，紧接着 11:41 恢复 fresh —— 悬停里只剩
    "没有达门槛的变动"，等于把刚发生的事抹掉了。判据最怕的是"任何变动都算找到"，
    所以夹具专门摆了一对**没过门槛**的相邻轮（+5%）挡在前面。
    """

    def _row(self, name, index=40, apt=1.0, tok=100):
        return {"name": name, "creator": "A", "index": index, "apt_usd": apt,
                "in_per_1m_usd": 1.0, "out_per_1m_usd": 2.0, "tok_s": tok, "ttft_s": 1.0}

    def _db(self, rounds, bad=()):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE snapshots(ts TEXT, source TEXT, n_rows INT, ok INT,"
                    " err TEXT, payload TEXT, evaluation_version TEXT, PRIMARY KEY(ts, source))")
        for ts, rows in rounds:
            pl = "坏掉的 json{" if ts in bad else json.dumps(rows)
            con.execute("INSERT INTO snapshots"
                        "(ts,source,n_rows,ok,err,payload,evaluation_version) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (ts, F.SRC_DIFF, len(rows) if ts not in bad else 0, 1, None, pl, None))
        return con

    def test_本轮有变动时直接用生产算的那份不重放(self):
        con = self._db([("10:00", [self._row("A", tok=200)]),
                        ("09:00", [self._row("A", tok=100)])])
        mv = [{"text": "A 输出速度 100→200 tok/s（+100%）"}]
        got = F.last_notable_round(con, mv, {})
        self.assertEqual((got["steps"], got["at"], got["base"]), (0, "10:00", "09:00"), got)
        self.assertEqual(got["items"], mv[0]["text"].split("、"))

    def test_本轮那一对绝不重放(self):
        """台账里本轮那对"看着有变动"，但生产说没有 —— 必须以生产为准，不能自打脸。

        这就是 10-12 用户抓到的形状：横幅"这轮有 2 项"与浮层"最近一次达门槛是六轮前"同时出现。
        """
        con = self._db([("10:00", [self._row("A", tok=200)]),
                        ("09:00", [self._row("A", tok=100)]),
                        ("08:00", [self._row("A", tok=50)])])
        got = F.last_notable_round(con, [], {})          # 生产说本轮没有
        self.assertEqual((got["steps"], got["at"], got["base"]), (1, "09:00", "08:00"),
                         f"本轮那一对被重放了：{got}")

    def test_回溯与生产同一份基线口径(self):
        """词表变窄时（--refresh-watchlist、改 pinned），被排除的行不能当有基数可比。

        生产侧 `load_previous` 会按当前词表把旧基线再筛一遍；回溯若忘了筛，就会报出生产
        根本不会报的变动 —— 与 10-12 那次"本轮重放与真榜不同形"是同一族错。
        """
        rows_hi = [self._row("DeepSeek V4 Pro", tok=100), self._row("Muse Spark 1.3", tok=20)]
        rows_lo = [self._row("DeepSeek V4 Pro", tok=100), self._row("Muse Spark 1.3", tok=5)]
        con = self._db([("12:00", rows_lo), ("11:00", rows_lo),
                        ("10:00", rows_hi), ("09:00", rows_lo)])
        cfg = {"keyword_include": ["deepseek"], "keyword_exclude": []}
        got = F.last_notable_round(con, [], cfg)
        self.assertEqual(got["items"], [], f"被词表排除的行仍被当成有基数：{got}")
        got_all = F.last_notable_round(con, [], {})
        self.assertTrue(any("Muse" in x for x in got_all["items"]),
                        "夹具本身得能挑出 Muse 那条，否则这条判据没有后果")

    def test_基线构造只有一处(self):
        src = inspect.getsource(F)
        self.assertEqual(src.count('{r["name"]: r for r in'), 1, "基线映射写两遍，迟早一遍筛一遍不筛")
        self.assertIn("return baseline_map(rows, load_config(), ts=str(row[0] or \"\")), False", src)
        # 回溯那一对：必须走 baseline_map，且 diff 的**全部**档都要留给整族视角
        # （以前是 notable_moves(diff_models(...)) 一把过，chg 没落地，尾巴就没有输入）
        loop = inspect.getsource(F.last_notable_round)
        # 带 ts 是隔离的要求（区间按观测时间判），但"只此一处"这条不变
        self.assertIn("chg = diff_models(cur, baseline_map(prev, cfg, ts=", loop)
        self.assertIn("notable_moves(chg, metric)", loop)
        # 基线只此一处。cur 那一次是把**本轮对面**那批行按同一口径筛/隔离，不是第二套基线。
        self.assertEqual(loop.count("baseline_map(prev, cfg, ts="), 1,
                         "回溯里构造两次基线 = 两套筛法")
        self.assertEqual(loop.count("baseline_map("), 2,
                         "对面那批行也得走同一份筛法，多出来的第三处才是分叉")

    def test_调用点必须在本轮基线落库之后(self):
        """台账第 0 条得是本轮，否则本轮会被当成"上一轮"、steps 全错一档。"""
        src = inspect.getsource(F.run)
        self.assertLess(src.index("save_diff_base(con, models)"),
                        src.index("last_notable_round(con, moves, cfg, skip_keys=already_keys)"),
                        "last_notable_round 跑在 save_diff_base 之前 → 本轮还没进台账")

    def test_本轮没有就往前翻_报出那一轮与它的基线(self):
        con = self._db([("12:00", [self._row("A", index=40)]),        # 与 11:00 一模一样
                        ("11:00", [self._row("A", index=40)]),
                        ("10:00", [self._row("A", index=35)])])       # 11:00 vs 10:00：+5 分
        got = F.last_notable_round(con, [], {})
        self.assertEqual((got["steps"], got["at"], got["base"]), (1, "11:00", "10:00"), got)

    def test_没过门槛的相邻轮不能算找到(self):
        """+5% 是变动但没达门槛 —— 停在这儿就等于什么都没翻到。"""
        con = self._db([("12:00", [self._row("A", tok=105)]),         # vs 11:00：+5%，不过门槛
                        ("11:00", [self._row("A", tok=100)]),
                        ("10:00", [self._row("A", tok=50)])])         # vs 10:00：+100%，过
        got = F.last_notable_round(con, [], {})
        self.assertEqual((got["steps"], got["at"], got["base"]), (1, "11:00", "10:00"), got)
        self.assertEqual(got["n"], 1)

    def test_翻到上限还没有就如实说翻了几轮(self):
        same = [self._row("A")]
        con = self._db([("12:00", same), ("11:00", same), ("10:00", same), ("09:00", same)])
        with unittest.mock.patch.object(F, "BACKTRACK_MAX_ROUNDS", 4):
            got = F.last_notable_round(con, [], {})
        self.assertEqual(got["steps"], -1, got)
        # 4 条快照 = 3 对，本轮那对不重放 → 真扫过 2 对
        self.assertEqual((got["scanned"], got["cap"]), (2, 4), "报出来的轮数要真等于扫过的")

    def test_坏行跳过去但继续往前找(self):
        """坏掉的那轮两侧都配不成对，跨过它之后仍要能找到更早的那一次。"""
        con = self._db([("12:00", [self._row("A", tok=100)]),
                        ("11:00", [self._row("A", tok=100)]),
                        ("10:30", [self._row("A", tok=100)]),
                        ("10:00", [self._row("A", tok=20)])], bad={"11:00"})
        got = F.last_notable_round(con, [], {})
        self.assertEqual((got["steps"], got["at"], got["base"]), (2, "10:30", "10:00"), got)

    def test_条目逐条原样带出而不是拼成一句(self):
        """浮层要列全，所以回溯给的是 items（每条一项），不是截断过的正文。"""
        con = self._db([("10:00", [self._row("A", tok=100), self._row("B", tok=50)]),
                        ("09:00", [self._row("A", tok=100), self._row("B", tok=50)]),
                        ("08:00", [self._row("A", tok=20), self._row("B", tok=50)])])
        got = F.last_notable_round(con, [], {})
        mv = F.notable_moves(F.diff_models(json.loads(con.execute(
            "SELECT payload FROM snapshots WHERE ts='09:00'").fetchone()[0]),
            {r["name"]: r for r in json.loads(con.execute(
                "SELECT payload FROM snapshots WHERE ts='08:00'").fetchone()[0])}))
        self.assertTrue(mv, "夹具得先真有过门槛的变动")
        self.assertEqual(got["items"], [m["text"] for m in mv])
        self.assertNotIn("text", got, "不该再带一份截断过的整句，两处表示同一列表迟早漂移")


class TestBacktrackSkipDuplicate(unittest.TestCase):
    """往前翻时，**同一件事**的那一轮不算答案，要接着往前 —— 判"同一件事"用模型+字段，不用字符串。

    用户 10-15 的原话：「如果提示的内容明细没有变化，还要再往前 diff 出真实的上次变更。」
    姊妹仓线上实测到的形状：同一次"新上榜"在相邻两轮里写成"当前第 20 位"与"当前第 19 位"，
    逐字符比会把它当成两次变更，历史那一格于是又把同一件事念了一遍。
    """

    def _row(self, name, index=40, apt=1.0, tok=100):
        return {"name": name, "creator": "A", "index": index, "apt_usd": apt,
                "in_per_1m_usd": 1.0, "out_per_1m_usd": 2.0, "tok_s": tok, "ttft_s": 1.0}

    def _db(self, rounds):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE snapshots(ts TEXT, source TEXT, n_rows INT, ok INT,"
                    " err TEXT, payload TEXT, evaluation_version TEXT, PRIMARY KEY(ts, source))")
        for ts, rows in rounds:
            con.execute("INSERT INTO snapshots"
                        "(ts,source,n_rows,ok,err,payload,evaluation_version) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (ts, F.SRC_DIFF, len(rows), 1, None, json.dumps(rows), None))
        return con

    def _rounds(self):
        # 12:00 是本轮（绝不重放）。i=1 与 i=2 都只有 `tok_s|A` 这一件事（+200% 与 +67%，
        # 数字不同、事同一件）；i=3 才换成 `index|C` —— 那才是"另一次变更"。
        return [("12:00", [self._row("A", tok=100)]),
                ("11:00", [self._row("A", tok=300)]),
                ("10:00", [self._row("A", tok=100)]),
                ("09:00", [self._row("A", tok=60), self._row("C", index=50)]),
                ("08:00", [self._row("A", tok=60), self._row("C", index=40)]),
                ("07:00", [self._row("A", tok=60), self._row("C", index=40)])]

    def _pair_at(self, con, i):
        """第 i 轮那一对真实产出的（事件指纹, 合并明细）—— 从生产拿，不手写。"""
        rows = con.execute("SELECT ts, payload FROM snapshots WHERE source=? AND ok=1 "
                           "ORDER BY ts DESC", (F.SRC_DIFF,)).fetchall()
        cur = json.loads(rows[i][1])
        prev = F.baseline_map(json.loads(rows[i + 1][1]), {})
        mv = F.notable_moves(F.diff_models(cur, prev))
        return F.move_keys(mv), [str(x) for x in (F.merged_move_lines(mv) or []) if x]

    def test_不带skip时还是最近那一轮(self):
        con = self._db(self._rounds())
        got = F.last_notable_round(con, [], {})
        self.assertEqual((got["steps"], got["at"]), (1, "11:00"), got)
        self.assertEqual(got.get("dup_skipped"), 0, "没跳过就该如实报 0，不是缺字段")

    def test_同一件事即使数字漂了也跳过并接着往前(self):
        con = self._db(self._rounds())
        k1, l1 = self._pair_at(con, 1)
        k2, l2 = self._pair_at(con, 2)
        self.assertTrue(k1 and k2, "夹具得真能产出事件，否则这条判据在空转")
        self.assertEqual(k1, k2, "夹具必须是同一件事（A 的输出速度）")
        self.assertNotEqual(l1, l2, "两轮的文字必须不同 —— 相同就成了在测字符串去重")
        got = F.last_notable_round(con, [], {}, skip_keys=k1)
        self.assertEqual((got["steps"], got["at"]), (3, "09:00"), got)
        self.assertEqual(got["dup_skipped"], 2, got)
        self.assertEqual(got["keys"], ["index|C"], got)

    def test_一路同一件事就如实说没有新的变化(self):
        con = self._db(self._rounds())
        allk = set()
        for i in (1, 2, 3):
            allk |= set(self._pair_at(con, i)[0])
        self.assertTrue(allk)
        got = F.last_notable_round(con, [], {}, skip_keys=allk)
        self.assertGreaterEqual(got["all_duplicated"], 3, got)
        self.assertEqual(got["items"], [])
        self.assertIsNone(got["steps"], "没有答案时不能编一个轮次编号出来")

    def test_浮层里两种形状各说什么(self):
        base = {"data_state": "ok", "moves": [], "move_lines": [],
                "alert": {"kind": "fresh", "label": "正常更新，没有达门槛的变动", "text": ""}}
        t1 = R.foot_tip({**base, "prev_moves": {"at": "10:00", "base": "09:00", "steps": 2,
                                                "n": 1, "lines": ["C 智能指数 40→45（+5 分）"],
                                                "dup_skipped": 1}})
        self.assertIn("上次有达门槛变动的轮次：10:00（往前第 2 轮，对比 09:00）", t1)
        self.assertIn("中间 1 轮是同一批变动，已跳过", t1)
        t2 = R.foot_tip({**base, "prev_moves": {"at": "11:00", "steps": None, "n": 0,
                                                "items": [], "lines": [], "all_duplicated": 3}})
        self.assertIn("往前翻了 3 轮，都是同一批变动（最近一次是 11:00）", t2)
        self.assertNotIn("（明细与上面同批，不重列）", t2)

    def test_调用点把本轮与累计块的事件都喂给回溯(self):
        """不传 skip_keys 就等于这条功能在线上不存在 —— 盯着调用点，不只盯函数。"""
        src = inspect.getsource(F.run)
        self.assertIn("prev_moves = last_notable_round(con, [], cfg, skip_keys=already_keys)", src)
        self.assertIn("set(move_keys(moves))", src)
        self.assertIn("last_moves = last_notable_round(con, moves, cfg, skip_keys=already_keys)", src)
        # 顺序也是判据：累计块必须先算，它的 keys 才进得了 already_keys
        self.assertLess(src.index("cumulative = cumulative_moves"), src.index("already_keys = set("),
                        "skip 集合要用到累计块的事件")
        self.assertIn('cumulative.get("keys")', src)

    def test_累计块也带事件指纹(self):
        con = self._db(self._rounds())
        models = json.loads(json.dumps([self._row("A", tok=300)]))
        cum = F.cumulative_moves(con, models, {})
        self.assertTrue(cum.get("keys"), cum)
        self.assertTrue(all("|" in k for k in cum["keys"]), cum["keys"])


class TestBacktrackInTip(unittest.TestCase):
    """回溯结果进脚注悬停：位置说清、不重复、旧载荷不崩、去重键不再露出。"""

    def _tip(self, alert, moves=None):
        D = {"generated_at": "2026-10-10 12:00:00", "data_state": "ok", "alert": alert,
             "moves": moves or []}
        return R.foot_tip(D)

    def _a(self, **kw):
        base = {"kind": "fresh", "label": "正常更新，没有达门槛的变动", "key": "k",
                "text": "", "hint": F.move_hint()}
        base.update(kw)
        return base

    def test_往前翻到就报两个快照时间并把每条列全(self):
        items = [f"A 输出速度 175→122 tok/s（-30%）", "B 每任务成本 $0.46→$0.36（-22%）",
                 "C 智能指数 40→43（+3 分）", "D 输出速度 64→74 tok/s（+16%）",
                 "E 输出速度 365→410 tok/s（+12%）"]
        tip = self._tip(self._a(last={"at": "2026-10-10 11:40:49", "base": "2026-10-10 10:16:52",
                                      "steps": 1, "n": 5, "items": items}))
        # 没有发生合并时不再重复报计数 —— 行数本身就是条数
        self.assertIn("最近一次相邻达门槛：2026-10-10 11:40:49（往前第 1 轮，对比 2026-10-10 10:16:52）", tip)
        for it in items:
            self.assertIn("· " + it, tip, f"这条被省略了：{it}")
        self.assertNotIn("…等", tip, "浮层里不许用「…等 N 项」省略条目")
        self.assertNotIn("这轮有 5 项", tip)

    def test_本轮就是那次时逐条列全而不重复块(self):
        # 5 条 > 通知横幅的"列前 3"上限：少于 4 条时 items[:3] 与 items 同值，判据会失明
        mv = [{"text": f"{c} 智能指数 40→43（+3 分）"} for c in "ABCDE"]
        tip = self._tip(self._a(kind="moved", label="有达门槛的变动",
                               text="这轮有 5 项值得注意的变动：A 智能指数 40→43（+3 分）；B 智能指数 40→43（+3 分）；C 智能指数 40→43（+3 分）…等 5 项",
                               last={"at": "x", "base": "2026-10-10 11:41:29", "steps": 0,
                                     "n": 5, "items": [m["text"] for m in mv]}), moves=mv)
        self.assertNotIn("最近一次达门槛", tip)
        self.assertIn("本轮达门槛的变动 5 项（对比 2026-10-10 11:41:29）", tip)
        for c in "ABCDE":
            self.assertIn(f"· {c} 智能指数 40→43（+3 分）", tip, f"第 {c} 条被省略了")
        self.assertNotIn("…等", tip, "横幅那句截断不该进浮层")

    def test_旧载荷没有items时退回横幅正文(self):
        mv_free = self._tip(self._a(kind="moved", label="有达门槛的变动",
                                    text="这轮有 1 项：A +3 分",
                                    last={"at": "x", "base": "y", "steps": 0}))
        self.assertIn("这轮有 1 项：A +3 分", mv_free)

    def test_故障类结论仍走alert正文不套列表(self):
        tip = self._tip(self._a(kind="degraded", label="有源缺失", text="有源缺失，部分数字还是上一轮的"))
        self.assertIn("有源缺失，部分数字还是上一轮的", tip)
        self.assertNotIn("共 ", tip)

    def test_没翻到就说翻了几个轮次(self):
        tip = self._tip(self._a(last={"at": "", "base": "", "steps": -1, "n": 0, "text": "",
                                      "scanned": 3, "cap": 40}))
        self.assertIn("往前翻了 3 轮都没到过门槛（最多翻 40 轮）", tip)

    def test_旧载荷没有last也不崩(self):
        self.assertNotIn("最近一次达门槛", self._tip(self._a()))

    def test_去重键不再出现在提示里(self):
        self.assertNotIn("去重键", self._tip(self._a(key="alert:moved:2026-10-10")))



class TestLastAlertInTip(unittest.TestCase):
    """「上次提示」这一节：真弹过的那条 + 台账派生的"上次有达门槛变动的轮次"，两个来源都要在。

    为什么给两条：moved 的去重键按天算，同一天连响两轮时第二轮其实没出声 —— 只按台账派生
    就会把"没弹过的轮次"说成"上次提示"；只按落盘那条又漏掉"有变动但当天不再响"的情况。
    两条一起给，读者自己就能对上。
    """

    def _tip(self, **kw):
        D = {"generated_at": "2026-10-13 12:00:00", "data_state": "ok",
             "moves": [{"text": "A 智能指数 40→43（+3 分）"}],
             "move_lines": ["A 智能指数 40→43（+3 分）"],
             "alert": {"kind": "moved", "label": "有达门槛的变动", "key": "k", "text": "",
                       "hint": F.move_hint(),
                       "last": {"at": "2026-10-13 12:00:00", "base": "2026-10-13 11:00:00",
                                "steps": 0, "n": 1, "lines": ["A 智能指数 40→43（+3 分）"]}}}
        D.update(kw)
        return R.foot_tip(D)

    def test_本轮有变动时也追加真弹过的那条(self):
        tip = self._tip(last_alert={"at": "2026-10-12 11:40:05", "ts": "2026-10-12 11:40:03",
                                    "kind": "moved", "steps": 1,
                                    "lines": ["B 每任务成本 $0.4→$0.6 (+50%)"]})
        self.assertIn("本轮达门槛的变动 1 项", tip, "追加不能把本轮那段挤掉")
        self.assertIn("上次提示：2026-10-12 11:40:05（moved，往前第 1 轮）", tip)
        self.assertIn("· B 每任务成本 $0.4→$0.6 (+50%)", tip)

    def test_两个来源同一轮就并成一条标题(self):
        same = {"at": "2026-10-12 11:40:05", "steps": 1, "kind": "moved",
                "lines": ["B 每任务成本 $0.4→$0.6 (+50%)"]}
        tip = self._tip(last_alert=dict(same, ts="x"),
                        prev_moves=dict(same, base="2026-10-12 10:00:00", n=1))
        self.assertEqual(tip.count("上次提示："), 1, "同一轮念了两遍")
        self.assertIn("也正是最近一次有达门槛变动的轮次", tip)
        self.assertNotIn("上次有达门槛变动的轮次：", tip)

    def test_没有记录时整节不出现(self):
        """首次运行、或上一次那轮压根没出声：宁可不写，也不能编一个"上次提示"。"""
        tip = self._tip()
        self.assertNotIn("上次提示", tip)
        self.assertNotIn("上次有达门槛变动", tip)

    def test_轮距取不到就只报时间不编数字(self):
        """记录那一轮的快照已被保留期清掉：steps=None 时不能写成"往前第 None 轮"。"""
        tip = self._tip(last_alert={"at": "2026-09-01 08:00:00", "kind": "moved", "steps": None,
                                    "lines": ["A 智能指数 40→44（+4 分）"]})
        self.assertIn("上次提示：2026-09-01 08:00:00（moved）", tip)
        self.assertNotIn("往前第 None", tip)

    def test_台账派生那条带对比基线(self):
        tip = self._tip(prev_moves={"at": "2026-10-11 09:12:00", "base": "2026-10-10 09:11:00",
                                    "steps": 3, "n": 1, "lines": ["C 输出速度 90→120 (+33%)"]})
        self.assertIn("上次有达门槛变动的轮次：2026-10-11 09:12:00（往前第 3 轮，"
                      "对比 2026-10-10 09:11:00）", tip)
        self.assertIn("· C 输出速度 90→120 (+33%)", tip)

    def test_故障类提示没有逐条时把正文原样带出(self):
        tip = self._tip(last_alert={"at": "2026-10-12 03:00:00", "kind": "degraded", "steps": 6,
                                    "text": "有源缺失：OpenRouter 报价（timeout）"})
        self.assertIn("上次提示：2026-10-12 03:00:00（degraded，往前第 6 轮）", tip)
        self.assertIn("· 有源缺失：OpenRouter 报价（timeout）", tip)

    def test_历史行与本轮同批时标题仍然要在(self):
        """2026-10-15 早上的真实形状：本轮走累计块，prev_moves 那一条正好是它的子集。

        行去重如果整节吞掉，浮层就一个字都不提"上一次是哪一轮"，用户看到的就是
        「没有历史变动」—— 而 prev_moves 本来就是同一叠台账快照里往前翻出来的，
        撞车是常态。所以标题（带时间与轮距，那才是信息本体）必须留下，明细折掉。
        """
        a = {"kind": "moved", "label": "有达门槛的变动", "key": "k", "text": "",
             "hint": F.move_hint(),
             "last": {"at": "2026-10-13 12:00:00", "base": "2026-10-13 11:00:00",
                      "steps": 0, "n": 2,
                      "lines": ["A 智能指数 40→43（+3 分）", "B 每任务成本 $0.4→$0.6 (+50%)"]}}
        tip = R.foot_tip({"data_state": "ok", "alert": a,
                          "moves": [{"text": "A 智能指数 40→43（+3 分）"},
                                    {"text": "B 每任务成本 $0.4→$0.6 (+50%)"}],
                          "move_lines": ["A 智能指数 40→43（+3 分）",
                                         "B 每任务成本 $0.4→$0.6 (+50%)"],
                          "prev_moves": {"at": "2026-10-12 07:30:23", "base": "2026-10-11 09:00:00",
                                         "steps": 1, "n": 1,
                                         "lines": ["B 每任务成本 $0.4→$0.6 (+50%)"]}})
        self.assertIn("上次有达门槛变动的轮次：2026-10-12 07:30:23", tip)
        self.assertIn("（明细与上面同批，不重列）", tip)
        self.assertEqual(tip.count("· B 每任务成本"), 1, "明细被重列了")

    def test_两路指到同一轮时只说一次并带别名(self):
        """强制往前翻与相邻那一路常常是同一轮；说两遍读起来像两个不同结论。"""
        same = {"at": "2026-10-12 07:30:23", "base": "2026-10-11 09:00:00",
                "steps": 1, "n": 1, "lines": ["C 输出速度 90→120 (+33%)"]}
        tip = self._tip(alert={"kind": "moved", "label": "有达门槛的变动", "text": "",
                               "last": dict(same)}, prev_moves=dict(same))
        self.assertEqual(tip.count("07:30:23"), 1, "同一轮被两节各念了一遍")
        self.assertIn("相邻那一路也是这一轮", tip)
        self.assertNotIn("最近一次相邻达门槛：", tip)

    def test_两路不同轮时各说一次(self):
        tip = self._tip(alert={"kind": "moved", "label": "有达门槛的变动", "text": "",
                               "last": {"at": "2026-10-13 08:00:00", "base": "2026-10-12 09:00:00",
                                        "steps": 1, "n": 1,
                                        "lines": ["D 智能指数 41→45（+4 分）"]}},
                        prev_moves={"at": "2026-10-11 07:30:00", "base": "2026-10-10 07:30:00",
                                    "steps": 3, "n": 1,
                                    "lines": ["C 输出速度 90→120 (+33%)"]})
        self.assertIn("上次有达门槛变动的轮次：2026-10-11 07:30:00（往前第 3 轮", tip)
        self.assertIn("最近一次相邻达门槛：2026-10-13 08:00:00（往前第 1 轮", tip)
        self.assertNotIn("相邻那一路也是这一轮", tip)

    def test_远端名字里的标签被转义(self):
        """注入面从"本轮"扩到"上次提示"这一节，转义必须同样覆盖（浮层是页面自绘的）。"""
        tip = self._tip(last_alert={"at": "2026-10-12 11:40:05", "kind": "moved", "steps": 1,
                                    "lines": ["<img src=x onerror=alert(1)> 智能指数 40→43（+3 分）"]})
        page = R.build_html("<svg></svg>", "2026-10-13 12:00:00", False, tip=tip)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;img src=x", page)

    # ---------------------------------------------------------------- 落盘与判据同源
    def test_落一条记录再渲染浮层就带上它(self):
        """`alert_record` 写的字段名必须正好是 `foot_tip` 读的那几个。

        两边各测各的时打错一个键名谁都绿 —— 这条把"写 → 读 → 渲染"接起来跑一遍。
        """
        with tempfile.TemporaryDirectory() as td:
            rec = Path(td) / ".last_alert.json"
            with unittest.mock.patch.object(F.mr_paths, "LAST_ALERT", rec):
                prev = {"generated_at": "2026-10-12 11:40:05", "diff_ts": "2026-10-12 11:40:03",
                        "move_lines": ["B 每任务成本 $0.4→$0.6 (+50%)", "C 智能指数 41→45（+4 分）"],
                        "moves": [{"text": "B 每任务成本 $0.4→$0.6 (+50%)"}]}
                F.write_last_alert(F.alert_record(prev, {
                    "kind": "moved", "label": "有达门槛的变动", "tone": "info",
                    "key": "alert:moved:2026-10-12", "text": "这轮有 1 项"}))
                la = F.read_last_alert("2026-10-13 12:00:00")
                la["steps"] = 4
        tip = self._tip(last_alert=la)
        self.assertIn("上次提示：2026-10-12 11:40:05（moved，往前第 4 轮）", tip)
        for ln in prev["move_lines"]:
            self.assertIn("· " + ln, tip, f"记录里这条没进浮层：{ln}")

    def test_整段浮层与夹具逐字等值(self):
        """两路都塞进载荷时的**整段**文本。

        运行产物今天还带不出这一节（要等下一轮抓取真的落下记录），所以跨实现的镜像判据
        暂时没有可观察后果 —— 这条把它补成有后果的：改脚注任何一行的措辞/顺序都会红。
        """
        tip = self._tip(
            last_alert={"at": "2026-10-12 11:40:05", "kind": "moved", "steps": 1,
                        "lines": ["B 每任务成本 $0.4→$0.6 (+50%)"]},
            prev_moves={"at": "2026-10-11 09:12:00", "base": "2026-10-10 09:11:00", "steps": 3,
                        "n": 2, "lines": ["C 输出速度 90→120 (+33%)", "A 智能指数 40→43（+3 分）"]})
        self.assertEqual(tip, "\n".join([
            "本轮结论：有达门槛的变动（moved）",
            "本轮达门槛的变动 1 项（对比 2026-10-13 11:00:00）",
            "· A 智能指数 40→43（+3 分）",
            "上次提示：2026-10-12 11:40:05（moved，往前第 1 轮）",
            "· B 每任务成本 $0.4→$0.6 (+50%)",
            "上次有达门槛变动的轮次：2026-10-11 09:12:00（往前第 3 轮，对比 2026-10-10 09:11:00）"
            "（另 1 条上面已列）",
            "· C 输出速度 90→120 (+33%)",
            "门槛：智能指数 >1 分 · 每任务成本 >20% · 输出速度 >20% · 任务耗时(估) >20%"
            "（新入榜/掉榜不过这条门槛，浮层另列在「榜单进出」）"
            "；关联读数为同轮对比、非同比，基准口径不同，不证明因果",
        ]), tip)

    def test_去重谓词与弹不弹同源(self):
        self.assertFalse(F.will_pop({"kind": "fresh", "key": "h"}, ""))
        self.assertFalse(F.will_pop({"kind": "moved", "key": "alert:moved:10-13"},
                                    "alert:moved:10-13"), "同一条已经响过，不能再算一次提示")
        self.assertTrue(F.will_pop({"kind": "moved", "key": "alert:moved:10-13"}, "别的键"))
        self.assertFalse(F.will_pop({"kind": "moved", "key": ""}, ""), "没有键就无从去重，别乱记")

    def test_会出声且值得记才落盘(self):
        """`should_record_alert` = 会出声 ∧ 值得记；两个条件各拆一半都要红。"""
        good = {"kind": "moved", "tone": "info", "key": "alert:moved:10-13"}
        self.assertTrue(F.should_record_alert(good, "别的键"))
        self.assertFalse(F.should_record_alert(good, "alert:moved:10-13"), "已经响过的那条不再记")
        self.assertFalse(F.should_record_alert({"kind": "fresh", "tone": "info", "key": "h"}, ""),
                         "fresh 不出声，也就没有上次提示可言")
        self.assertFalse(F.should_record_alert({"kind": "degraded", "tone": "warn", "key": "k"}, ""),
                         "故障告警不占这个位置")

    def test_失败类提示不当成上次提示(self):
        """抓取失败/太久没跑/整族没了都是 warn 语气，那些是"这一刻坏了"不是"上次动了什么"。

        判据走 `alert_policy` 已经算好的 tone，不另列 kind 清单 —— 两份清单迟早一份改了一份没改。
        """
        for kind in ("moved", "moved-cumulative"):
            self.assertTrue(F.worth_remembering({"tone": "info", "kind": kind}), f"{kind} 是要留的那种")
        for kind in ("degraded", "stale", "empty", "not-running", "series-gone"):
            self.assertFalse(F.worth_remembering({"tone": "warn", "kind": kind}),
                             f"{kind} 是故障告警，不该盖掉上一条真变动")
        self.assertFalse(F.worth_remembering({}), "没有语气就不下判断")
        # 语气本身由 alert_policy 决定：这几条必须真的是 warn（否则上面那个判据是空转的）
        for st, extra in (("degraded", {}), ("empty", {}), ("ok", {"series_gaps": [{"label": "X", "n_prev": 4}]})):
            a = F.alert_policy({"data_state": st, "generated_at": "2026-10-13 12:00:00",
                                "moves": [], "series_gaps": extra.get("series_gaps", []),
                                "cumulative": {}})
            self.assertEqual(a["tone"], "warn", f"{st or a['kind']} 的结论是 warn")
            self.assertFalse(F.worth_remembering(a))

    def test_失败轮不覆盖上一次真变动的记录(self):
        """写盘侧：一轮抓取失败时，浮层那句"上次提示"必须还指着上一次动过什么的那轮。"""
        with tempfile.TemporaryDirectory() as td:
            rec = Path(td) / ".last_alert.json"
            with unittest.mock.patch.object(F.mr_paths, "LAST_ALERT", rec):
                good = {"generated_at": "2026-10-12 11:40:05", "diff_ts": "t1",
                        "move_lines": ["B 每任务成本 $0.4→$0.6 (+50%)"], "last_moves": {}}
                F.write_last_alert(F.alert_record(good, {"kind": "moved", "tone": "info",
                                                         "key": "k1", "label": "有达门槛的变动", "text": "x"}))
                bad = {"generated_at": "2026-10-13 09:00:00", "diff_ts": "t2", "move_lines": [], "moves": []}
                bad_alert = {"kind": "degraded", "tone": "warn", "key": "k2",
                             "label": "有源缺失", "text": "有源缺失：OpenRouter 报价"}
                self.assertTrue(F.will_pop(bad_alert, "k1"), "这一轮本来会出声（不然测不到覆盖）")
                # 走生产那个具名判据，不在测试里复刻条件 —— 复刻了就是"改生产永远不红"
                self.assertFalse(F.should_record_alert(bad_alert, F.read_stamp()),
                                 "会出声不等于值得记：失败轮不该覆盖上一条真变动")
                if F.should_record_alert(bad_alert, F.read_stamp()):
                    F.write_last_alert(F.alert_record(bad, bad_alert))
                got = F.read_last_alert("2026-10-13 12:00:00")
                self.assertEqual((got.get("kind"), got.get("lines")),
                                 ("moved", ["B 每任务成本 $0.4→$0.6 (+50%)"]),
                                 "失败轮把上一条真变动盖掉了")

    def test_磁盘上是失败记录时宁可不出现(self):
        """读取侧也要挡：写盘那条判据是后加的，磁盘上可能已经躺着一份 warn 记录。"""
        with tempfile.TemporaryDirectory() as td:
            rec = Path(td) / ".last_alert.json"
            rec.write_text(json.dumps({"at": "2026-10-13 13:57:26", "ts": "x", "kind": "stale",
                                       "tone": "warn", "text": "主源本次没抓到", "lines": []}),
                           encoding="utf-8")
            with unittest.mock.patch.object(F.mr_paths, "LAST_ALERT", rec):
                self.assertEqual(F.read_last_alert("2026-10-13 15:00:00"), {},
                                 "失败记录不能当上次提示端出去")
                tip = self._tip(last_alert=F.read_last_alert("2026-10-13 15:00:00"))
                self.assertNotIn("上次提示：", tip)

    def test_记录读写与本轮自己那条不算上次(self):
        with tempfile.TemporaryDirectory() as td:
            rec = Path(td) / ".last_alert.json"
            with unittest.mock.patch.object(F.mr_paths, "LAST_ALERT", rec):
                self.assertEqual(F.read_last_alert("10:00"), {}, "没有文件就当没有")
                F.write_last_alert(F.alert_record(
                    {"generated_at": "10:00", "diff_ts": "09:59:58",
                     "move_lines": ["A +3 分"], "last_moves": {"base": "09:00"}},
                    {"kind": "moved", "label": "有达门槛的变动", "tone": "info",
                     "key": "alert:moved:10-13", "text": "这轮有 1 项"}))
                got = F.read_last_alert("11:00")
                self.assertEqual((got["at"], got["kind"], got["lines"]),
                                 ("10:00", "moved", ["A +3 分"]), got)
                self.assertEqual(got["ts"], "09:59:58", "轮距靠台账 ts 配对，必须一起存")
                self.assertEqual(F.read_last_alert("09:59:58"), {},
                                 "本轮自己刚弹的不能当上次提示（配的是台账 ts）")
                rec.write_text("{坏掉的 json", encoding="utf-8")
                self.assertEqual(F.read_last_alert("11:00"), {}, "坏文件不能把抓取带崩")
                # 改版前落下的老记录没有 tone：判不出是不是"真变动"，宁可不出现也不瞎认
                rec.write_text(json.dumps({"at": "10:00", "kind": "moved"}), encoding="utf-8")
                self.assertEqual(F.read_last_alert("11:00"), {}, "没有 tone 就不下判断")
                rec.write_text(json.dumps({"at": "10:00", "kind": "moved", "tone": "info"}),
                               encoding="utf-8")
                self.assertEqual(F.read_last_alert("11:00")["at"], "10:00", "没有 ts 的老记录退回比 at")
                self.assertEqual(F.read_last_alert("10:00"), {}, "按 at 也要挡住本轮自己那条")

    def test_轮距按台账ts配对(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE snapshots(ts TEXT, source TEXT, n_rows INT, ok INT,"
                    " err TEXT, payload TEXT, evaluation_version TEXT, PRIMARY KEY(ts, source))")
        for ts in ["10:00", "09:00", "08:00"]:
            con.execute("INSERT INTO snapshots"
                        "(ts,source,n_rows,ok,err,payload,evaluation_version) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (ts, F.SRC_DIFF, 1, 1, None, json.dumps([]), None))
        self.assertEqual(F.round_steps(con, "08:00"), 2)
        self.assertIsNone(F.round_steps(con, "07:00"), "台账里没有的时间要如实说没有")

    def test_夹具的隔离表覆盖SUPPORT下每一个落点(self):
        """2026-10-15 事故：`.last_alert.json` 是新加的，而 run() 型夹具手写清单没跟上，
        跑一轮单测就把用户线上那条"上次提示"覆盖成了夹具数据。清单改成按前缀枚举之后，
        这条盯着"枚举真的覆盖全了"—— 以后再加写进 SUPPORT 的文件不用改这条，漏了就红。
        """
        import mr_paths
        with tempfile.TemporaryDirectory() as td:
            assert_support_fully_isolated(self, mr_paths, Path(td))

    def test_上次提示那一轮就是相邻那轮时只说一次(self):
        """10-15 线上实测：两条都指向 09:47:09，浮层就把同一个时间念了两遍、还都只挂"不重列"。"""
        same = {"at": "2026-10-15 09:47:09", "base": "2026-10-15 07:30:13", "steps": 1,
                "n": 1, "lines": ["A 输出速度 100→160 tok/s（+60%）"], "items": []}
        tip = R.foot_tip({"data_state": "ok", "moves": [], "move_lines": [],
                          "alert": {"kind": "fresh", "label": "正常更新", "text": "", "last": dict(same)},
                          "last_alert": {"at": same["at"], "ts": "t", "kind": "moved",
                                         "tone": "info", "steps": 1, "lines": same["lines"]}})
        self.assertIn("上次提示：2026-10-15 09:47:09（moved，往前第 1 轮）；也正是最近一次相邻达门槛的轮次", tip)
        self.assertNotIn("最近一次相邻达门槛：", tip, "同一个时间被念了两遍")
        self.assertEqual(tip.count("09:47:09"), 1, tip)

    def test_落记录排在渲染之后(self):
        """反了就完了：渲染时读到的是本轮刚写的那条，浮层会把"本轮"说成"上次提示"。"""
        src = inspect.getsource(F.run)
        self.assertLess(src.index("write_outputs(payload, palette)"),
                        src.index("write_last_alert(alert_record("),
                        "记录写在了渲染之前 → 浮层会把本轮当成上次提示")
        self.assertLess(src.index('payload["last_alert"] = last_alert'),
                        src.index("write_outputs(payload, palette)"),
                        "记录得在渲染前就塞进载荷，否则浮层永远少这一节")

    def test_prev_moves走的是强制往前翻那一路(self):
        """last_moves 在本轮有变动时停在 steps=0；prev_moves 必须传空 moves 才翻得到上一批。

        2026-10-15 这条的期望串改过两次：`prev_moves` 先带上 `skip_rows`、当天又换成按事件
        指纹的 `skip_keys`（同一件事即使数字漂了也不算新的变更）。传空 moves 这个要点不变。
        """
        src = inspect.getsource(F.run)
        self.assertIn("prev_moves = last_notable_round(con, [], cfg, skip_keys=already_keys)", src)
        self.assertIn("last_moves = last_notable_round(con, moves, cfg, skip_keys=already_keys)", src)


class TestCarryForward(unittest.TestCase):
    """被榜单撤下的旧模型：只要**评测版本没变**、且在窗口内见过，就带旧读数回来。

    动机是实测的：撤榜不等于没价值，而旧判据（现役行三项全面优于才排除）会让被新一代
    全面压过的档位静默消失 —— 读者看不出"它退出榜单了"，只看到它凭空不见。
    2026-11-17 用户改口径，见 fetch_model_data.carry_forward 的说明。
    补回来的必须是**旧读数且标明日期**，不能混成今天的实测值。
    """

    def _con(self, rounds):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE snapshots(ts TEXT, source TEXT, n_rows INT, ok INT,"
                    " err TEXT, payload TEXT, evaluation_version TEXT, PRIMARY KEY(ts, source))")
        for ts, rows in rounds:
            con.execute("INSERT INTO snapshots"
                        "(ts,source,n_rows,ok,err,payload,evaluation_version) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (ts, F.SRC_AA, len(rows), 1, None,
                         json.dumps(rows, ensure_ascii=False), F.version_of(rows)))
        return con

    def _aa(self, name, index=43, apt=0.38, tok=63, version="5.0"):
        return {"name": name, "creator": "OpenAI", "index": index, "apt_usd": apt,
                "tok_s": tok, "ctx": 200000, "total_s": 1.0, "ttft_s": 1.0, "logo": "",
                "evaluation_version": version}

    def test_没被全面优于的就沿用回来且带派生字段(self):
        cur = [dict(row("GPT-6.1 Sol (high)", index=50, apt_usd=0.32, tok_s=49,
                        series_key="s61", creator="OpenAI", evaluation_version="5.0"))]
        con = self._con([("2026-10-08 07:30:00", [self._aa("GPT-6 Sol (high)")]),
                         ("2026-10-09 07:30:00", [])])
        n = F.carry_forward(cur, con, {}, [], {"keyword_include": ["sol"], "keyword_exclude": []},
                            now="2026-10-10 07:30:00")
        self.assertEqual(n, 1, cur)
        got = [m for m in cur if m.get("carried_asof")][0]
        self.assertEqual(got["name"], "GPT-6 Sol (high)")
        self.assertEqual(got["carried_asof"], "2026-10-08 07:30:00",
                         "要取它最后一次真在榜那一轮的值（10-09 那轮已经没有这行了）")
        # 走 merge() 才有这些字段：直接塞台账里存的合并结果时 apply_vendor_colors 会 KeyError
        for k in ("creator_slug", "series", "series_key", "tier", "key"):
            self.assertIn(k, got, f"沿用行缺派生字段 {k}")
        self.assertEqual((got["index"], got["tok_s"]), (43, 63))

    def test_被全面优于的也沿用回来(self):
        """2026-11-17 改口径：被现役行三项全面压过不再是排除理由。

        旧用例断言这里必须为 0。那条正是"撤榜行静默消失"的来源：读者看不到
        Gemini 3.8 Flash (high) 已经退出榜单，只发现它不见了。
        """
        cur = [dict(row("New (max)", index=60, apt_usd=0.2, tok_s=90, series_key="n1",
                       creator="OpenAI", evaluation_version="5.0"))]
        con = self._con([("2026-10-09 07:30:00", [self._aa("Old (max)", index=43, apt=0.38, tok=63)])])
        n = F.carry_forward(cur, con, {}, [], {"keyword_include": ["old"]},
                            now="2026-10-10 07:30:00")
        self.assertEqual(n, 1, "版本没变就带回来，旧读数由 carried_asof 标明")
        got = [m for m in cur if m.get("carried_asof")][0]
        self.assertEqual(got["name"], "Old (max)")
        self.assertEqual(got["carried_asof"], "2026-10-09 07:30:00")
        self.assertEqual((got["index"], got["apt_usd"], got["tok_s"]), (43, 0.38, 63),
                         "带回来的是旧读数，不能被现役行的数值覆盖")

    def test_超过窗口就不再用旧读数(self):
        cur = [dict(row("Sol", index=10, apt_usd=9.0, tok_s=1, series_key="z1",
                        creator="OpenAI", evaluation_version="5.0"))]
        con = self._con([("2026-08-01 07:30:00", [self._aa("GPT-6 Sol (max)")])])
        self.assertEqual(F.carry_forward(cur, con, {}, [], {"keyword_include": ["sol"]},
                                         now="2026-10-10 07:30:00"), 0,
                         "两个月前的读数不可比，宁缺毋滥")
        self.assertGreater(F.CARRY_MAX_DAYS, 13, "窗口要盖得住换版后一个月的跨度，否则这条在测常数")

    def test_词表不再关注的行不硬补(self):
        cur = [dict(row("Sol", index=10, apt_usd=9.0, tok_s=1, series_key="z1",
                        creator="OpenAI", evaluation_version="5.0"))]
        con = self._con([("2026-10-09 07:30:00", [self._aa("GPT-6 Sol (max)")])])
        self.assertEqual(F.carry_forward(cur, con, {}, [], {"keyword_include": ["别的词"]},
                                         now="2026-10-10 07:30:00"), 0,
                         "merge() 的 want_row 说了算：不再关注就不该被塞回来")

    def test_版本不同或未知的旧行不冒充当前代次(self):
        cur = [dict(row("New (min)", index=10, apt_usd=9.0, tok_s=1, series_key="n1",
                        creator="OpenAI", evaluation_version="5.0"))]
        con = self._con([("2026-10-09 07:30:00",
                          [self._aa("GPT-6 Sol v4 (max)", version="4.3"),
                           self._aa("GPT-6 Sol unknown (max)", version=None),
                           self._aa("GPT-6 Sol same (max)", version="5.0")])])
        n = F.carry_forward(cur, con, {}, [],
                            {"keyword_include": ["sol"], "keyword_exclude": []},
                            now="2026-10-10 07:30:00")
        self.assertEqual(n, 1, "只该补回与当前 5.0 同代次的那一行")
        got = [m for m in cur if m.get("carried_asof")]
        self.assertEqual([m["name"] for m in got], ["GPT-6 Sol same (max)"])
        self.assertEqual(got[0]["evaluation_version"], "5.0")

    def test_沿用不改写历史快照原文(self):
        con = self._con([("2026-10-09 07:30:00",
                          [self._aa("GPT-6 Sol v4 (max)", version="4.3"),
                           self._aa("GPT-6 Sol unknown (max)", version=None),
                           self._aa("GPT-6 Sol same (max)", version="5.0")])])
        raw_before = con.execute("SELECT payload FROM snapshots").fetchone()[0]
        cur = [dict(row("New (min)", index=10, apt_usd=9.0, tok_s=1, series_key="n1",
                        creator="OpenAI", evaluation_version="5.0"))]
        F.carry_forward(cur, con, {}, [],
                        {"keyword_include": ["sol"], "keyword_exclude": []},
                        now="2026-10-10 07:30:00")
        raw_after = con.execute("SELECT payload FROM snapshots").fetchone()[0]
        self.assertEqual(raw_after, raw_before, "沿用只读历史，原始快照不能被改写")
        saved = json.loads(raw_after)
        self.assertEqual([r["evaluation_version"] for r in saved], ["4.3", None, "5.0"],
                         "v4 与未知版本的原始行必须原样留在历史里")

    def test_图上与卡片都得说清是沿用(self):
        pts = [{"x": 0.38, "y": 43, "r": 5.0, "r_hi": None, "r_lo": None,
                "n": "GPT-6 Sol (high)", "c": "OpenAI", "c_esc": "OpenAI",
                "k": "GPT-6 Sol", "s": "GPT-6 Sol", "t": "high", "col": "#111111",
                "f": False, "tok": "63 tok/s", "est": False, "band": False,
                "carry": "2026-10-09"}]
        self.assertIn("沿用 10-09", R.point_tip(pts[0]))
        svg = R.build_svg(pts, [], ["OpenAI"])
        self.assertIn('data-carry="2026-10-09"', svg, "图上的点要能被判据数出来")
        self.assertIn("carr", svg)
        card = "\n".join(R.family_card_lines(pts + [dict(pts[0], y=44, t="xhigh", carry="")]))
        high_line = next(l for l in card.split("\n") if l.startswith("high · 43"))
        self.assertEqual(high_line, "high · 43 / — · $0.38 · — · — · 沿用 10-09",
                         "族卡每档行里沿用那档必须标出来，缺分写破折号")
        xhigh_line = next(l for l in card.split("\n") if l.startswith("xhigh · 44"))
        self.assertEqual(xhigh_line, "xhigh · 44 / — · $0.38 · — · —",
                         "没沿用的那档不能跟着被标上")
        self.assertNotIn("P—", card, "不再单列逐档 Progress 明细行")
        self.assertEqual(len(card.split("\n")), 5, "表头 + 两行列名 + 两档各一行")

    def test_调用点在前沿计算之前(self):
        """沿用行是图上的一部分，必须参与梯队与前沿，不能被 annotate 之后才塞进来。"""
        src = inspect.getsource(F.run)
        self.assertLess(src.index("carry_forward(models"), src.index("annotate_efficiency(models"),
                        "沿用要赶在前沿/梯队之前，否则图上结论按一套、点位按另一套")
        self.assertGreater(src.index("carry_forward(models"), src.index("models = merge("))


class TestCumulative(unittest.TestCase):
    """本轮 vs 往前逐轮累计 diff：相邻都不过门槛的慢漂移，攒几轮就该被看见。"""

    def _row(self, name, tok):
        return {"name": name, "creator": "A", "index": 40, "apt_usd": 1.0,
                "in_per_1m_usd": 1.0, "out_per_1m_usd": 2.0, "tok_s": tok, "ttft_s": 1.0}

    def _db(self, rounds):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE snapshots(ts TEXT, source TEXT, n_rows INT, ok INT,"
                    " err TEXT, payload TEXT, evaluation_version TEXT, PRIMARY KEY(ts, source))")
        for ts, rows in rounds:
            con.execute("INSERT INTO snapshots"
                        "(ts,source,n_rows,ok,err,payload,evaluation_version) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (ts, F.SRC_DIFF, len(rows), 1, None, json.dumps(rows), None))
        return con

    def test_相邻没过而更早一轮过了_报出轮距与那一轮(self):
        con = self._db([("14:00", [self._row("A", 110)]),      # 本轮（台账里也是这条）
                        ("13:00", [self._row("A", 100)]),      # 差 10% → 不过门槛
                        ("12:00", [self._row("A", 60)])])      # 差 83% → 过
        got = F.cumulative_moves(con, [self._row("A", 110)], {})
        self.assertEqual((got["steps"], got["at"]), (2, "12:00"), got)
        self.assertEqual(got["items"], ["A 输出速度 60→110 tok/s（+83%）"])

    def test_本轮一律用传进来的榜_不读台账第0条(self):
        """台账第 0 条也是本轮，但生产那份 `models` 才是准的（词表/字段口径都一致）。"""
        con = self._db([("14:00", [self._row("A", 500)]),      # 故意与传入的 models 不一致
                        ("13:00", [self._row("A", 100)])])
        got = F.cumulative_moves(con, [self._row("A", 105)], {})
        self.assertEqual(got, {}, "105 vs 100 只有 5%，不该过门槛；过门槛说明读了台账那条")

    def test_隔一轮才算累计_相邻那轮由moved负责(self):
        con = self._db([("14:00", [self._row("A", 200)]), ("13:00", [self._row("A", 100)])])
        got = F.cumulative_moves(con, [self._row("A", 200)], {})
        self.assertEqual((got["steps"], got["at"]), (1, "13:00"), got)
        a = F.alert_policy({"data_state": "ok", "generated_at": GEN_TS, "content_hash": "x",
                            "series_gaps": [], "moves": [], "cumulative": got})
        self.assertEqual(a["kind"], "fresh", "steps=1 就是相邻对比，另由 moved 那条负责")

    def test_alert_policy_累计分支的kind_语气_去重键(self):
        cum = {"at": "2026-10-12 12:00:00", "steps": 3, "n": 4,
               "items": [f"A{i} 输出速度 60→110 tok/s（+83%）" for i in range(4)]}
        a = F.alert_policy({"data_state": "ok", "generated_at": GEN_TS, "content_hash": "x",
                            "series_gaps": [], "moves": [], "cumulative": cum})
        self.assertEqual(a["kind"], "moved-cumulative")
        self.assertEqual(a["tone"], "info", "累计变动也是好消息，不是故障")
        self.assertEqual(a["key"], f"alert:moved-cumulative:{_NOW8.strftime('%Y-%m-%d')}")
        self.assertIn("往前第 3 轮", a["text"])
        self.assertIn("…等另 1 条", a["text"], "横幅只列前 3")

    def test_moved压过累计(self):
        """相邻已经过门槛时，结论就是 moved —— 同一天不该再为累计响第二次。"""
        cum = {"at": "x", "steps": 2, "n": 1, "items": ["A 输出速度 60→110 tok/s（+83%）"]}
        a = F.alert_policy({"data_state": "ok", "generated_at": GEN_TS, "content_hash": "x",
                            "series_gaps": [], "cumulative": cum,
                            "moves": [{"text": "B 智能指数 40→43（+3分）"}]})
        self.assertEqual(a["kind"], "moved")

    def test_坏行跳过与上限(self):
        con = self._db([("14:00", [self._row("A", 110)]), ("13:30", [self._row("A", 100)]),
                        ("13:00", [self._row("A", 100)]), ("12:00", [self._row("A", 60)])])
        con.execute("UPDATE snapshots SET payload='坏 json{' WHERE ts='13:00'")
        got = F.cumulative_moves(con, [self._row("A", 110)], {})
        self.assertEqual((got["steps"], got["at"]), (3, "12:00"), f"坏行应被跳过：{got}")
        with unittest.mock.patch.object(F, "CUMULATIVE_MAX_ROUNDS", 1):
            self.assertEqual(F.cumulative_moves(con, [self._row("A", 110)], {}), {},
                             "只看相邻一轮时不该翻到更早")


class TestCumulativeInTip(unittest.TestCase):
    """浮层里三块内容的分工：本轮自己有结论时只说本轮，安静轮才翻历史 + 列累计。"""

    def _tip(self, alert, moves=None, cum=None):
        D = {"generated_at": "2026-10-12 14:00:00", "data_state": "ok", "alert": alert,
             "moves": moves or [], "cumulative": cum or {}}
        return R.foot_tip(D)

    def _a(self, **kw):
        base = {"kind": "fresh", "label": "正常更新，没有达门槛的变动", "key": "k", "text": "",
                "hint": F.move_hint(), "last": {}}
        base.update(kw)
        return base

    def test_累计只隔一轮时不另开一块(self):
        """steps=1 就是相邻那一轮，与 moved 是同一笔账 —— 再列一遍就是重复。"""
        cum = {"at": "13:00", "steps": 1, "n": 4, "items": [f"A{i} 用量 1→3（+200%）" for i in range(4)]}
        tip = self._tip(self._a(cumulative=cum), cum=cum)
        self.assertNotIn("累计对比", tip)
        self.assertNotIn("累计达门槛", tip)


    def test_安静轮同时给出历史与累计(self):
        """两块都要在：相邻那笔与累计那笔是两个问题，谁也不覆盖谁。"""
        cum = {"at": "09:00", "steps": 4, "n": 4,
               "items": [f"A{i} 用量 1→3（+200%）" for i in range(4)]}
        last = {"at": "10:00", "base": "08:00", "steps": 2, "n": 1,
                "items": ["C 智能指数 40→43（+3分）"]}
        tip = self._tip(self._a(last=last), cum=cum)
        self.assertIn("最近一次相邻达门槛：10:00（往前第 2 轮，对比 08:00）", tip)
        self.assertIn("· C 智能指数 40→43（+3分）", tip)
        self.assertIn("累计对比：相对 09:00（往前第 4 轮）有 4 项达门槛", tip)
        for i in range(4):
            self.assertIn(f"· A{i} 用量 1→3（+200%）", tip, f"第 {i} 条被省略")

    def test_本轮有结论时两块仍在但同一批不重复念(self):
        """用户要求"之前的那种提示也要保留"：相邻块不因本轮走累计就消失；
        但同一批条目只念一遍，否则浮层成了复读机。"""
        mv = [{"text": f"B{i} 智能指数 40→43（+3分）"} for i in range(4)]
        same = [m["text"] for m in mv]
        tip = self._tip(self._a(kind="moved", label="有达门槛的变动", text="横幅那句",
                               last={"at": "x", "base": "y", "steps": 0, "n": 4, "items": same}),
                        moves=mv, cum={"at": "z", "steps": 3, "n": 4, "items": same})
        self.assertIn("本轮达门槛的变动 4 项（对比 y）", tip)
        self.assertNotIn("横幅那句", tip, "本轮的明细要逐条列，不是复述横幅")
        for m in mv:
            self.assertIn("· " + m["text"], tip)
        self.assertNotIn("累计对比", tip, "与本轮完全同一批条目 → 不重复念")
        self.assertNotIn("最近一次相邻达门槛", tip)
        lines = [l for l in tip.split("\n") if l.startswith("· ")]
        self.assertEqual(len(lines), len(set(lines)), "同一批条目被列了两遍")

    def test_累计过门槛时列条目而不是重复整句(self):
        cum = {"at": "2026-10-12 09:00:00", "steps": 2, "n": 4,
               "items": [f"A{i} 输出速度 60→110 tok/s（+83%）" for i in range(4)]}
        tip = self._tip(self._a(kind="moved-cumulative", label="有累计达门槛的变动（隔了几轮才看出来）",
                               text="相对 2026-10-12 09:00:00（往前第 2 轮）累计有 4 项达门槛：…等 4 项",
                               cumulative=cum), cum=cum)
        self.assertIn("累计达门槛的 4 项（相对 2026-10-12 09:00:00，往前第 2 轮）", tip)
        for i in range(4):
            self.assertIn(f"· A{i} 输出速度 60→110 tok/s（+83%）", tip, f"第 {i} 条被省略")
        self.assertNotIn("…等另 1 条", tip)
        self.assertNotIn("累计对比：", tip, "同一批条目不能既走正文又走累计块")
        self.assertNotIn("最近一次达门槛", tip)

    def test_三块并存时不许出现重复行(self):
        items = [f"A{i} 输出速度 60→110 tok/s（+83%）" for i in range(3)]
        tip = self._tip(self._a(last={"at": "10:00", "base": "08:00", "steps": 2, "n": 3,
                                      "items": items}),
                        cum={"at": "09:00", "steps": 3, "n": 3, "items": items})
        lines = [l for l in tip.split("\n") if l.startswith("· ")]
        self.assertEqual(len(lines), len(set(lines)), "同一批条目被列了两遍")


class TestBoardInOut(unittest.TestCase):
    """榜单进出：报了"新上了谁"就必须同时报"下去了谁"（10-13 用户口径）。

    这两个是同一件事的两面 —— 只列新进榜，读者就会以为榜单只在往上长。
    它们不进 `notable_moves`（那里要"有基数的幅度"，且每天常规换榜会把**通知**刷成噪音），
    浮层是拉取式的，就该在这里说全。
    """

    def _tip(self, io=None, **kw):
        # 浮层吃的是 changes 里的 type=new/gone（渲染侧现算），所以夹具也按 changes 喂
        ch = ([{"name": n, "type": "new"} for n in (io or {}).get("new", [])]
              + [{"name": n, "type": "gone"} for n in (io or {}).get("gone", [])])
        D = {"generated_at": "2026-10-13 12:00:00", "data_state": "ok",
             "moves": [{"text": "A 智能指数 40→43（+3 分）"}],
             "move_lines": ["A 智能指数 40→43（+3 分）"],
             "changes": ch,
             "alert": {"kind": "moved", "label": "有达门槛的变动", "key": "k", "text": "",
                       "hint": F.move_hint(),
                       "last": {"at": "2026-10-13 12:00:00", "base": "2026-10-13 11:00:00",
                                "steps": 0, "n": 1, "lines": ["A 智能指数 40→43（+3 分）"]}}}
        D.update(kw)
        return R.foot_tip(D)

    def test_从changes里成对取进与出(self):
        ch = [{"name": "Zeta", "type": "new"}, {"name": "Alpha", "type": "new"},
              {"name": "Alpha", "type": "new"},              # 重复行不该数两遍
              {"name": "Old", "type": "gone"},
              {"name": "Gemini 3.8 Flash (high)", "type": "carried"},
              {"name": "B", "type": "changed", "fields": []},
              {"type": "new"}, {"type": "gone"}]             # 没名字的脏行要丢掉
        self.assertEqual(R.board_io(ch),
                         {"new": ["Alpha", "Zeta"], "gone": ["Old"],
                          "carried": ["Gemini 3.8 Flash (high)"]})
        self.assertEqual(R.board_io([]), {"new": [], "gone": [], "carried": []})
        self.assertEqual(R.board_io(None), {"new": [], "gone": [], "carried": []})

    def test_沿用行不算新进榜(self):
        """沿用行是从台账恢复的旧读数，AA 本轮没有它，不能算"新进榜"。"""
        ch = [{"name": "Inkling Small", "type": "carried"},
              {"name": "Another", "type": "carried"}]
        io = R.board_io(ch)
        self.assertEqual(io["new"], [])
        self.assertEqual(io["carried"], ["Another", "Inkling Small"])

    def test_浮层两栏都列且逐条一行(self):
        tip = self._tip({"new": ["Alpha 4", "Zeta 3"], "gone": ["Old 1"]})
        self.assertIn("新进榜 2 个", tip)
        self.assertIn("落榜 1 个", tip)
        for n in ("Alpha 4", "Zeta 3", "Old 1"):
            self.assertIn("· " + n, tip, f"{n} 没单独成行")
        self.assertNotIn("…等", tip, "浮层是给人读明细的，省略就等于少信息")

    def test_只有一边时不硬凑另一边(self):
        tip = self._tip({"new": ["Alpha 4"], "gone": []})
        self.assertIn("新进榜 1 个", tip)
        self.assertNotIn("落榜", tip)

    def test_两边都空时一节都不出现(self):
        tip = self._tip({"new": [], "gone": []})
        self.assertNotIn("新进榜", tip)
        self.assertNotIn("落榜", tip)
        # 门槛那句本来就要提"另列在「榜单进出」"，所以判"有没有这一节"要看行首，不能看全文
        self.assertFalse([l for l in tip.split("\n") if l.startswith("新进榜")])

    def test_没有changes也不崩(self):
        D = {"generated_at": "x", "data_state": "ok", "alert": {"kind": "fresh", "label": "正常更新"}}
        self.assertNotIn("新进榜", R.foot_tip(D))

    def test_改名不计榜单进出且只列一句(self):
        line = "Claude Opus 5.5：5 档改名（去掉 with fallback；提醒指标未达变化门槛）"
        D = {"generated_at": "x", "data_state": "ok", "moves": [], "changes": [
                 {"name": "Claude Opus 5.5 (max)", "old_name": "Claude Opus 5.5 (max with fallback)",
                  "type": "renamed", "quiet": True, "fields": []}],
             "rename_lines": [line],
             "alert": {"kind": "fresh", "label": "正常更新", "text": ""}}
        tip = R.foot_tip(D)
        self.assertIn("名称调整", tip)
        self.assertIn("· " + line, tip)
        self.assertNotIn("新进榜", tip)
        self.assertNotIn("落榜", tip)

    def test_门槛文案不再把读者支到别处(self):
        h = F.move_hint()
        self.assertNotIn("看变动列表", h, "浮层已经自己列了这一节，不能再把读者支去看别的地方")
        self.assertIn("榜单进出", h)


class TestMergeFamily(unittest.TestCase):
    """同族各档并成一条，给均值与中位数 —— 一档一行会把同一件事说五遍。"""

    def _m(self, name, amount, field="tok_s", label="输出速度", unit="%", text=None):
        return {"name": name, "field": field, "label": label, "unit": unit,
                "amount": amount, "text": text or f"{name} {label} {amount:+.0f}{unit}"}

    def test_同族同向并成一条(self):
        mv = [self._m("GPT-6 Astra (xhigh)", 44.2), self._m("GPT-6 Astra (max)", 36.2),
              self._m("GPT-6 Astra (high)", 35.7), self._m("GPT-6 Astra (medium)", 34.1),
              self._m("GPT-6 Astra (low)", 26.2)]
        got = F.merged_move_lines(mv)
        self.assertEqual(len(got), 1, got)
        self.assertIn("GPT-6 Astra 输出速度 5 档同向 +35.3%", got[0])
        self.assertIn("中位 +35.7%", got[0], "奇数条取中间那档")
        self.assertIn("区间 +26.2~+44.2%", got[0])
        self.assertIn("xhigh/max/high/medium/low", got[0], "档位名要列出来，读者才知道并了哪几档")

    def test_偶数条中位取中间两数平均(self):
        mv = [self._m("X Pro (high)", 10.0), self._m("X Pro (low)", 20.0)]
        self.assertIn("中位 +15.0%", F.merged_move_lines(mv)[0])

    def test_有涨有跌不并(self):
        mv = [self._m("Y Air (max)", 30.0), self._m("Y Air (low)", -25.0)]
        got = F.merged_move_lines(mv)
        self.assertEqual(len(got), 2, f"方向不同说明不是整体漂移：{got}")
        self.assertTrue(all("同向" not in x for x in got))

    def test_只有一档原样输出(self):
        got = F.merged_move_lines([self._m("Z Mini (max)", 12.0)])
        self.assertEqual(got, ["Z Mini (max) 输出速度 +12%"])

    def test_不同代次不并(self):
        """'(0902)' 这类日期/版本按 split_series 的口径留在族名里，与档位不同。"""
        mv = [self._m("Qwen3.8 Max (0902)", 20.0), self._m("Qwen3.8 Max (0815)", 18.0)]
        self.assertEqual(len(F.merged_move_lines(mv)), 2)

    def test_不同字段不并(self):
        mv = [self._m("A One", 20.0), self._m("A One", 12.0, field="apt_usd",
                                              label="每任务成本", unit="$")]
        self.assertEqual(len(F.merged_move_lines(mv)), 2)

    def test_横幅正文也用合并行(self):
        mv = [self._m("GPT-6 Astra (xhigh)", 44.2), self._m("GPT-6 Astra (max)", 36.2),
              self._m("GPT-6 Astra (low)", 26.2), self._m("Other (max)", 15.0)]
        txt = F.moves_text(mv)
        self.assertIn("5 档", txt) if False else None
        self.assertIn("3 档同向", txt, f"横幅该说合并后的事：{txt}")
        self.assertEqual(txt.count("GPT-6 Astra"), 1, "同一族在横幅里也只该出现一次")


class TestFamilyRollup(unittest.TestCase):
    """整族视角的汇总：把**没到门槛的档**也收进来（用户 10-15 提的要求）。

    合并行只念过门槛的那几档，读者于是把「整族一起调过价」看成「只动了两档」。
    门槛值全部从 `MOVE_PCT` / `MOVE_INDEX_POINTS` 推出来 —— 门槛改了（10% → 20% 就改过）
    夹具跟着走，不用手改数字，也不会出现「测试还绿着、线上口径已经变了」的那种绿。
    """

    def fld(self, field, a, b, kind="changed"):
        label = F.DIFF_LABEL[field]
        if kind != "changed":
            return {"field": field, "label": label, "from": a, "to": b,
                    "delta": None, "pct": None, "kind": kind}
        return {"field": field, "label": label, "from": a, "to": b,
                "delta": round(b - a, 6), "pct": round((b - a) / a * 100, 2) if a else None,
                "kind": "changed"}

    def ch(self, name, fields, type_="changed"):
        return {"name": name, "type": type_, "fields": fields}

    def tier(self, tier, pct, fam="GPT-6 Astra", field="tok_s", base=150.0):
        """按「比基线动了多少个百分点」造一档 —— 幅度直接给百分点，门槛用常量推。"""
        step = (base * pct / 100) if field != "index" else pct
        return self.ch(f"{fam} ({tier})",
                       [self.fld(field, base, round(base + step, 6))])

    def test_未到门槛的档也在汇总里_且过门槛的集合与notable_moves同源(self):
        thr = F.MOVE_PCT
        pcts = {"max": thr + 8.0, "xhigh": -(thr + 3.0),        # 过门槛
                "high": thr - 2.0, "medium": 5.0, "low": -7.0}  # 未到门槛
        chg = [self.tier(t, p) for t, p in pcts.items()]
        ctx = F.family_rollup(chg)
        g = ctx[("GPT-6 Astra", "tok_s")]
        self.assertEqual(len(g["tiers"]), len(pcts), "整族视角要的是**全部**档，不只过门槛的")
        self.assertEqual(g["unit"], "%")
        over = {t for t, _a, o in g["tiers"] if o}
        mv_over = {F.split_series(m["name"])[1] for m in F.notable_moves(chg)}
        self.assertEqual(over, mv_over,
                         "汇总里「过没过门槛」必须与出声那条判据同一套门槛 —— 两套门槛就会一边说 5 档一边只报 2 档")
        self.assertEqual(len(over), 2, "夹具得真有未到门槛的档，否则这条判据在空转")

    def test_恰好等于门槛算未到(self):
        """与 notable_moves 一样是严格大于：正好压线不报，也不能算「过门槛的档」。"""
        thr = F.MOVE_PCT
        g = F.family_rollup([self.tier("max", thr), self.tier("low", thr + 0.5)])[(
            "GPT-6 Astra", "tok_s")]
        flags = {t: o for t, _a, o in g["tiers"]}
        self.assertFalse(flags["max"], f"恰好 {thr}% 在门槛上，不算过")
        self.assertTrue(flags["low"])
        self.assertEqual([F.split_series(m["name"])[1] for m in
                          F.notable_moves([self.tier("max", thr), self.tier("low", thr + 0.5)])],
                         ["low"])

    def test_指数字段按分差汇总_单位是分(self):
        thr = F.MOVE_INDEX_POINTS
        chg = [self.tier("max", thr + 2, field="index", base=40.0),
               self.tier("low", thr - 0.5, field="index", base=40.0)]
        g = F.family_rollup(chg)[("GPT-6 Astra", "index")]
        self.assertEqual(g["unit"], "分")
        self.assertEqual(sorted(a for _t, a, _o in g["tiers"]),
                         sorted([thr + 2, thr - 0.5]), "abs 字段要的是分差，不是百分比")

    def test_门槛外的字段_没基数的_非changed行都不进汇总(self):
        """同一条记录里别的字段必须**进得来** —— 不然「全都排除了」与「全都没排除」看起来一样。"""
        thr = F.MOVE_PCT
        chg = [self.ch("Other One (max)", [self.fld("ttft_s", 1.0, 9.0),
                                           self.fld("in_per_1m_usd", 1.0, 9.0),
                                           self.fld("tok_s", None, 200, "appeared")]),
               self.ch("Gone Row (max)", [self.fld("tok_s", 200, None, "disappeared")], "gone"),
               self.ch("New Row (max)", [self.fld("tok_s", 100, 100 * (1 + (thr + 5) / 100))], "new"),
               self.tier("high", thr - 9)]
        ctx = F.family_rollup(chg)
        self.assertEqual({k[0] for k in ctx}, {"GPT-6 Astra"},
                         f"只该剩这一族的 tok_s：{sorted(ctx)}")
        self.assertEqual(list({k[1] for k in ctx}), ["tok_s"])
        self.assertEqual(len(ctx[("GPT-6 Astra", "tok_s")]["tiers"]), 1)

    def test_族与字段分开成组_不同代次不并(self):
        thr = F.MOVE_PCT
        chg = [self.tier("max", thr + 5, fam="Qwen3.8 Max"),
               self.tier("max", thr + 4, fam="Qwen3.8 Max", field="apt_usd", base=1.0),
               self.ch("Qwen3.8 Max (0902)", [self.fld("tok_s", 150, 150 * (1 + (thr + 3) / 100))])]
        ctx = F.family_rollup(chg)
        self.assertEqual(sorted(ctx), [("Qwen3.8 Max", "apt_usd"), ("Qwen3.8 Max", "tok_s"),
                                       ("Qwen3.8 Max (0902)", "tok_s")],
                         "同代次才算一族；日期/版本号留在族名里（与 split_series 同口径）")

    def test_空输入不崩(self):
        self.assertEqual(F.family_rollup([]), {})
        self.assertEqual(F.family_rollup(None), {})


class TestFamilyTail(unittest.TestCase):
    """尾巴：同族没过门槛的档 + 整族在这个字段上的综合变化。

    尾巴只出现在**浮层明细**里（那里不截断），不进通知横幅（200 字硬上限）——
    两面的差别也得有判据盯着，不然改一处就把线上通知挤没了。
    """

    def _lines(self, pcts, fam="GPT-6 Astra"):
        """从「每档的百分比」走一遍生产：notable_moves → merged_move_lines(mv, chg)。"""
        changes = []
        base = 150.0
        for t, p in pcts.items():
            changes.append({"name": f"{fam} ({t})", "type": "changed",
                            "fields": [{"field": "tok_s", "label": F.DIFF_LABEL["tok_s"],
                                        "from": base, "to": round(base * (1 + p / 100), 6),
                                        "delta": round(base * p / 100, 6),
                                        "pct": round(p, 2), "kind": "changed"}]})
        mv = F.notable_moves(changes)
        return mv, F.merged_move_lines(mv, changes), changes

    def test_尾巴列出未到门槛的档并给整族综合(self):
        thr = F.MOVE_PCT
        pcts = {"max": -(thr + 8.0), "xhigh": -(thr + 2.0),
                "high": -18.9, "medium": -15.4, "low": -19.2}
        mv, lines, _ = self._lines(pcts)
        self.assertEqual(len(lines), 1, f"同族同向该并成一条：{lines}")
        got = lines[0]
        others = sorted([p for p in pcts.values() if abs(p) <= thr], reverse=True)
        self.assertIn("2 档同向", got, "合并行本体只说过门槛的档")
        self.assertIn(f"同族另有 {len(others)} 档未到门槛", got, got)
        for p in others:
            self.assertIn(f"{p:+.1f}%", got, f"未到门槛的每一档都得报出幅度：{got}")
        amts = [round(p, 2) for p in pcts.values()]
        srt = sorted(amts)
        mid = srt[len(srt) // 2] if len(srt) % 2 else (srt[len(srt) // 2 - 1] + srt[len(srt) // 2]) / 2
        self.assertIn(f"整族 {len(amts)} 档", got, "整族的档数要把过门槛的也算进去")
        self.assertIn(f"均值 {sum(amts) / len(amts):+.1f}%", got)
        self.assertIn(f"中位 {mid:+.1f}%", got)
        self.assertIn(f"区间 {min(amts):+.1f}~{max(amts):+.1f}%", got)
        self.assertIn("同向", got)
        self.assertNotIn("有涨有跌", got)

    def test_整族都过了门槛不加尾巴(self):
        """那种情况合并行说的就是全部，再念一遍是噪音。"""
        thr = F.MOVE_PCT
        mv, lines, _ = self._lines({"max": thr + 8.0, "low": thr + 3.0})
        self.assertEqual(len(lines), 1)
        self.assertNotIn("同族另有", lines[0], lines[0])
        self.assertNotIn("整族", lines[0])

    def test_有涨有跌就说有涨有跌(self):
        thr = F.MOVE_PCT
        _mv, lines, _ = self._lines({"max": thr + 8.0, "high": 12.0, "low": -12.0})
        self.assertIn("有涨有跌", lines[0], lines[0])
        self.assertNotIn("档同向", lines[0], "方向不一致时不能报「同向」")

    def test_未到门槛超过3档只列前3并报总数(self):
        thr = F.MOVE_PCT
        pcts = {"max": -(thr + 8.0), "xhigh": -3.0, "high": -4.0,
                "medium": -5.0, "low": -6.0, "min": -7.0}
        _mv, lines, _ = self._lines(pcts)
        got = lines[0]
        self.assertIn("同族另有 5 档未到门槛", got, got)
        listed = re.search(r"同族另有 \d+ 档未到门槛（(.*?)…等 (\d+) 档）", got)
        self.assertTrue(listed, got)
        self.assertEqual(len(listed.group(1).split("、")), 3,
                         f"括号里只该列前 3 档，其余并进计数：{got}")
        self.assertEqual(listed.group(2), "5", "省略号后面的计数要等于总数，不是「还剩几个没列」")
        self.assertNotIn("low -6.0%", got, "第 4、5 档不进正文")

    def test_逐档行也把尾巴挂在最后一行(self):
        """有涨有跌时不合并、逐档列 —— 尾巴仍要说，但只能说一遍（挂在最后一行）。"""
        thr = F.MOVE_PCT
        mv, lines, _ = self._lines({"max": thr + 8.0, "low": -(thr + 4.0), "high": 6.0})
        self.assertEqual(len(lines), 2, f"方向不同要分开说：{lines}")
        self.assertEqual(sum(x.count("同族另有") for x in lines), 1, lines)
        self.assertIn("同族另有", lines[-1])
        self.assertNotIn("同族另有", lines[0])

    def test_不传changes时与旧行为逐字相同(self):
        """尾巴是**加了可选参数**得到的，不能把原来的输出也改了：
        横幅、以及任何没传 changes 的调用点必须逐字不变。"""
        thr = F.MOVE_PCT
        pcts = {"max": -(thr + 8.0), "xhigh": -(thr + 2.0), "high": -18.9}
        mv, lines, changes = self._lines(pcts)
        old = F.merged_move_lines(mv)
        self.assertNotIn("同族另有", "\n".join(old))
        self.assertTrue(all(new.startswith(o) or o in new for o, new in zip(old, lines)),
                        f"带尾巴的那份应当是原串的加长：{old} / {lines}")
        self.assertEqual(F.merged_move_lines(mv, None), old)
        self.assertEqual(F.merged_move_lines(mv, []), old)

    def test_横幅两处都不带尾巴_浮层两处都带(self):
        """moves_text（本轮/历史横幅）与 cumulative_text（累计横幅）都截 200 字，
        尾巴上百字会挤掉「到底动了什么」；payload 里的明细与累计 lines 则要带尾巴。"""
        thr = F.MOVE_PCT
        pcts = {"max": -(thr + 8.0), "xhigh": -(thr + 2.0),
                "high": -18.9, "medium": -15.4, "low": -19.2}
        mv, lines, _ = self._lines(pcts)
        self.assertIn("同族另有", lines[0])
        banner = F.moves_text(mv)
        self.assertNotIn("同族另有", banner, f"横幅只有 200 字：{banner}")
        self.assertLessEqual(len(banner), 200)

        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE snapshots(ts TEXT, source TEXT, n_rows INT, ok INT,"
                    " err TEXT, payload TEXT, evaluation_version TEXT, PRIMARY KEY(ts, source))")

        def row(name, tok):
            return {"name": name, "creator": "A", "index": 40, "apt_usd": 1.0,
                    "in_per_1m_usd": 1.0, "out_per_1m_usd": 2.0, "tok_s": tok, "ttft_s": 1.0}
        base = 150.0
        cur = {t: round(base * (1 + p / 100), 6) for t, p in pcts.items()}

        def snap(vals):
            return json.dumps([row(f"GPT-6 Astra ({t})", v) for t, v in vals.items()])

        # rows[0]=本轮（跳过），相邻两轮与本轮同值（累计要往前翻才撞到），09:00 才是那一对答案
        for ts, vals in (("12:00", cur), ("11:00", cur), ("10:00", cur),
                         ("09:00", {t: base for t in pcts})):
            con.execute("INSERT INTO snapshots"
                        "(ts,source,n_rows,ok,err,payload,evaluation_version) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (ts, F.SRC_DIFF, len(vals), 1, None, snap(vals), None))
        models = json.loads(snap(cur))
        cum = F.cumulative_moves(con, models, {})
        self.assertEqual(cum.get("steps"), 3, cum)
        self.assertTrue(cum.get("lines"), cum)
        self.assertIn("同族另有", cum["lines"][0], cum["lines"])
        self.assertNotIn("同族另有", "".join(cum.get("lines_plain") or []))
        txt = F.cumulative_text(cum)
        self.assertLessEqual(len(txt), 200)
        self.assertNotIn("同族另有", txt, f"累计横幅也不能带尾巴：{txt}")
        self.assertIn("累计有", txt)

    def test_回溯翻到的那一轮也带尾巴(self):
        """历史那一路是从台账重放的：尾巴必须在 `last_notable_round` 里也长出来，
        不然线上「上次有达门槛变动的轮次」那一节就只有过门槛的档。"""
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE snapshots(ts TEXT, source TEXT, n_rows INT, ok INT,"
                    " err TEXT, payload TEXT, evaluation_version TEXT, PRIMARY KEY(ts, source))")

        def row(name, tok):
            return {"name": name, "creator": "A", "index": 40, "apt_usd": 1.0,
                    "in_per_1m_usd": 1.0, "out_per_1m_usd": 2.0, "tok_s": tok, "ttft_s": 1.0}
        thr = F.MOVE_PCT
        base = 150.0
        # 本轮(12:00)安静，11:00 那轮才是答案：max/xhigh 过门槛、high/medium 没过
        cur = {t: base for t in ("max", "xhigh", "high", "medium")}
        hit = {"max": base * (1 - (thr + 8.0) / 100), "xhigh": base * (1 - (thr + 2.0) / 100),
               "high": base * (1 - 12.0 / 100), "medium": base * (1 - 6.0 / 100)}
        for ts, vals in (("12:00", cur), ("11:00", hit), ("10:00", cur), ("09:00", cur)):
            rows = [row(f"GPT-6 Astra ({t})", round(v, 6)) for t, v in vals.items()]
            con.execute("INSERT INTO snapshots"
                        "(ts,source,n_rows,ok,err,payload,evaluation_version) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (ts, F.SRC_DIFF, len(rows), 1, None, json.dumps(rows), None))
        got = F.last_notable_round(con, [], {})
        self.assertEqual((got["steps"], got["at"]), (1, "11:00"), got)
        self.assertTrue(got["lines"], got)
        self.assertIn("同族另有 2 档未到门槛", got["lines"][0], got["lines"])
        self.assertIn("整族 4 档", got["lines"][0], "整族计数含过门槛的那两档")

    def test_调用点把changes喂进本轮明细(self):
        """不传 changes 就等于这条功能在线上不存在 —— 盯着调用点，不只盯函数。"""
        src = inspect.getsource(F.run)
        self.assertIn('payload["move_lines"] = merged_move_lines(moves, changes)', src)
        loop = inspect.getsource(F.last_notable_round)
        self.assertIn("merged_move_lines(mv, chg)", loop)
        self.assertIn("chg = diff_models(cur, baseline_map(prev, cfg, ts=", loop,
                      "整族视角要的是这一对的**全部**档，不是过门槛的那几条")
        self.assertIn("merged_move_lines(mv, chg)", inspect.getsource(F.cumulative_moves))


class TestNotifyEsc(unittest.TestCase):
    """osascript 兜底拼 AppleScript 时必须先转义 —— 少了这个函数整个通知出口抛 NameError。

    其余用例一律把 notify_fresh 整个 mock 掉（避免真弹通知），所以这条出口没有别的
    覆盖：这里既盯住 esc() 本身，也盯住它确实被用在拼脚本的那一行。
    """

    def test_esc_只转义反斜杠与双引号(self):
        self.assertEqual(F.esc('a"b\\c'), 'a\\"b\\\\c')
        self.assertEqual(F.esc("普通文本"), "普通文本")

    def test_通知兜底脚本里的引号已转义(self):
        payload = {"models": [{"name": "X"}], "changes": [], "generated_at": "12:00",
                   "alert": {"kind": "stale", "key": "k1", "tone": "warn",
                             "text": '含"双引号"与\\反斜杠'}}
        with unittest.mock.patch.object(F, "mr_paths") as mp, \
             unittest.mock.patch.object(F, "read_stamp", lambda: ""), \
             unittest.mock.patch.object(F, "_stamp_notified", lambda k: None), \
             unittest.mock.patch.object(F, "log", lambda *a, **k: None), \
             unittest.mock.patch("subprocess.run") as run:
            mp.HUB_NOTIFY = ""
            run.return_value = unittest.mock.Mock(returncode=0, stdout="")
            F.notify_fresh(payload)   # 修复前这里抛 NameError，被 except 吞成一行日志
        script = run.call_args.args[0][2]
        self.assertIn('\\"双引号\\"', script)
        self.assertNotIn('"双引号"', script, "未转义的双引号会截断 AppleScript 字符串")


class TestConfigSplit(unittest.TestCase):
    """词表分两家：可变真身在状态目录，仓库那份只是种子 + 人写的三段（10-20 定的）。

    动机是实测到的脏法：安装副本 `src/config/watchlist.json` 与仓库那份是**同一个 inode**，
    而 `grow_watchlist` 每轮都写它 —— 10-17 加 claude、10-20 加 minimax 各把 git 工作树
    弄脏一次，看起来像"有人改了配置没提交"。所以这里最要紧的一条是：**写词表时种子文件
    逐字节不变**（判据落在 sha 上，不落在"内容看起来一样"上）。
    """

    def _paths(self, td: Path, live=None, seed=None, nested=False):
        """造 (真身, 种子) 两个落点；给 None 的就是"不存在"。

        `nested=True` 把真身放进一个**还没创建的子目录** —— 线上真身就在
        `SUPPORT/config/watchlist.json`，新机器上那个 config/ 目录并不存在，
        写之前不 mkdir 就是当场 FileNotFoundError（这条是被变异反测逼出来的）。
        """
        lp = (td / "config" / "live.json") if nested else (td / "live.json")
        sp = td / "seed.json"
        if live is not None:
            lp.parent.mkdir(parents=True, exist_ok=True)
            lp.write_text(json.dumps(live, ensure_ascii=False, indent=2), encoding="utf-8")
        if seed is not None:
            sp.write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")
        return lp, sp

    def _rows(self):
        return [row("Lumo Air", creator="Acme"), row("Lumo Gale", creator="Acme"),
                row("Piko Arc", creator="Gamma"), row("Piko Rim", creator="Gamma")]

    def test_真身在状态目录而种子在代码树(self):
        import mr_paths
        self.assertEqual(F.CFG_PATH, mr_paths.CONFIG)
        self.assertEqual(F.CFG_SEED, mr_paths.CONFIG_SEED)
        base = str(mr_paths.SUPPORT) + os.sep
        self.assertTrue(str(mr_paths.CONFIG).startswith(base),
                        "可变真身必须落在状态目录下，否则定时任务会往 git 工作树里写")
        self.assertEqual(mr_paths.CONFIG_SEED, mr_paths.HERE / "config" / "watchlist.json")
        self.assertNotEqual(mr_paths.CONFIG, mr_paths.CONFIG_SEED)

    def test_夹具隔离表自动把真身纳进覆盖(self):
        """这条靠枚举，不靠手写清单：以后再加一个写进 SUPPORT 的文件也自动被纳管。"""
        import mr_paths
        with tempfile.TemporaryDirectory() as td:
            assert_support_fully_isolated(self, mr_paths, Path(td))
            names = {p.attribute for p in support_path_patches(mr_paths, Path(td))}
            self.assertIn("CONFIG", names, "真身没被夹具搬走 —— 跑单测会写进用户状态目录")

    def test_写词表时种子文件逐字节不变(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live, seed = self._paths(root, live=None, seed={"keyword_include": ["lumo"]},
                                     nested=True)
            before = seed.read_bytes()
            with unittest.mock.patch.object(F, "CFG_PATH", live), \
                 unittest.mock.patch.object(F, "CFG_SEED", seed):
                r = F.grow_watchlist(self._rows())     # Piko 这一族是新的，该补词
            self.assertIn("piko", r["added"], "夹具得真能触发一次写盘，否则这条判据在空转")
            self.assertEqual(seed.read_bytes(), before, "种子被写了 —— 工作树又会每天脏一次")
            self.assertTrue(live.is_file(), "真身应被创建（父目录不存在也要建出来）")
            self.assertEqual(json.loads(live.read_text(encoding="utf-8"))["keyword_include"],
                             ["lumo", "piko"])

    def test_首次没有真身时整份抄种子(self):
        with tempfile.TemporaryDirectory() as td:
            live, seed = self._paths(Path(td), live=None,
                                     seed={"keyword_include": ["lumo", "nexo", "piko"]})
            with unittest.mock.patch.object(F, "CFG_PATH", live), \
                 unittest.mock.patch.object(F, "CFG_SEED", seed):
                cfg = F.load_config()
            self.assertEqual(cfg["keyword_include"], ["lumo", "nexo", "piko"],
                             "升级/新装的第一轮不能把已攒下的覆盖丢掉")
            self.assertFalse(live.exists(), "只读不该顺手创建文件")

    def test_攒下来的词不被种子的缺失抹掉(self):
        with tempfile.TemporaryDirectory() as td:
            live, seed = self._paths(Path(td), live={"keyword_include": ["lumo", "claude"]},
                                     seed={"keyword_include": ["lumo"]})
            with unittest.mock.patch.object(F, "CFG_PATH", live), \
                 unittest.mock.patch.object(F, "CFG_SEED", seed):
                self.assertEqual(F.load_config()["keyword_include"], ["lumo", "claude"],
                                 "keyword_include 以真身为准：种子里没有 ≠ 该删")

    def test_人写的三段每次从种子并进来(self):
        """仓库里钉一个词，下一轮就该生效 —— 否则种子变成谎言（CLI 也照着这个路径提示用户）。"""
        with tempfile.TemporaryDirectory() as td:
            live, seed = self._paths(Path(td),
                                     live={"keyword_include": ["lumo"],
                                           "keyword_include_pinned": [],
                                           "vendor_brand_color_override": {"acme": "#111111"}},
                                     seed={"keyword_include": ["lumo"],
                                           "keyword_include_pinned": ["zeta"],
                                           "keyword_exclude": ["junk"],
                                           "vendor_brand_color_override": {
                                               "acme": "#222222", "beta": "#333333"}})
            with unittest.mock.patch.object(F, "CFG_PATH", live), \
                 unittest.mock.patch.object(F, "CFG_SEED", seed):
                cfg = F.load_config()
                self.assertEqual(cfg["keyword_include_pinned"], ["zeta"])
                self.assertEqual(cfg["keyword_exclude"], ["junk"])
                self.assertEqual(cfg["vendor_brand_color_override"],
                                 {"acme": "#111111", "beta": "#333333"},
                                 "配色：种子补空位，真身里已有的以真身为最终值（两处不互相覆盖）")
                r = F.grow_watchlist(self._rows())
            self.assertIn("zeta", r["new"], "钉住的词必须下一轮就进列表（只加不减同样保住它）")

    def test_坏掉的种子当场报错而不是当成没配(self):
        with tempfile.TemporaryDirectory() as td:
            live, seed = self._paths(Path(td), live={"keyword_include": ["lumo"]}, seed=None)
            seed.write_text("{这不是合法的 json", encoding="utf-8")
            before = live.read_bytes()
            with unittest.mock.patch.object(F, "CFG_PATH", live), \
                 unittest.mock.patch.object(F, "CFG_SEED", seed):
                with self.assertRaises(json.JSONDecodeError):
                    F.load_config()
                self.assertEqual(live.read_bytes(), before, "读失败不该顺手改写真身")

    def test_两个都不存在时返回空表(self):
        with tempfile.TemporaryDirectory() as td:
            live, seed = self._paths(Path(td))
            with unittest.mock.patch.object(F, "CFG_PATH", live), \
                 unittest.mock.patch.object(F, "CFG_SEED", seed):
                self.assertEqual(F.load_config(), {})

    def test_读写各只有一处(self):
        """两处各留一个口子：写点漏一条就会又有一个地方往代码树里写。"""
        src = inspect.getsource(F)
        self.assertEqual(src.count("_atomic_write(CFG_PATH"), 1,
                         "落盘口子必须只有 save_config 这一处")
        self.assertEqual(src.count("_atomic_write(CFG_SEED"), 0, "种子永远不该被写")
        self.assertIn('live, seed = _read_json(CFG_PATH), _read_json(CFG_SEED)', src)
        self.assertEqual(len(re.findall(r"CFG_SEED\.read_text", src)), 0,
                         "绕开 load_config 直接读种子 = 合并规则会有第二套说法")
        for fn in (F.grow_watchlist, F.refresh_watchlist):
            body = inspect.getsource(fn)
            self.assertIn("cfg = load_config()", body)
            self.assertIn("save_config(", body)


class TestDisplayDecimals(unittest.TestCase):
    """11-04 起速度/延迟改读 RSC 原始浮点，给人看的位数要维持旧表格里的形态。

    旧形态是量出来的，不是拍的：11-04 及以前每轮 80/80 档 `tok_s` 是整数、
    `ttft_s` 72/80 档两位、`total_s` 73/80 档两位；新链路给 14~16 位。
    台账与 series 仍存原始值 —— 这里只管显示，改一条位数就等于改口径，所以钉住。
    """

    def test_文案按字段收位(self):
        self.assertEqual(F._move_value("tok_s", 123.289456), "123")
        self.assertEqual(F._move_value("tok_s", 67.76316669225746), "68")
        self.assertEqual(F._move_value("ttft_s", 69.7192284307624), "69.72")
        self.assertEqual(F._move_value("first_answer_s", 331.7508709534717), "331.75")
        # 尾零不硬补：上游旧文本就是 "150.4"、"156.07"
        self.assertEqual(F._move_value("ttft_s", 150.40), "150.4")
        self.assertEqual(F._move_value("total_s", 156.07), "156.07")

    def test_没在表里的字段保持原写法(self):
        """apt_usd / time_per_task_s 的取数通道没换，动它们的显示就是无谓的口径改动。"""
        self.assertEqual(F._move_value("apt_usd", 7.76), "$7.76")
        self.assertEqual(F._move_value("time_per_task_s", 2392.79), "2392.79")
        self.assertEqual(F._move_value("index", 52), "52")

    def test_从到一段只带一个单位(self):
        self.assertEqual(F._move_span("tok_s", 172, 123.289456), "172→123 tok/s")
        self.assertEqual(F._move_span("ttft_s", 150.4, 69.7192), "150.4→69.72")

    def test_half_up_与看板逐字一致(self):
        # 109.75 在二进制里略小于 .75：floor(x*100+.5) 给 109.74，看板 toFixed 给 109.75
        self.assertEqual(F.display_num(109.75, 2), "109.75")
        self.assertEqual(F.display_num(0.125, 2), "0.13")
        # 整十/整千不能被尾零规则吃掉（dashboard 注释里记过 90→"9" 那次）
        self.assertEqual(F.display_num(90.0, 0), "90")
        self.assertEqual(F.display_num(1486.0, 0), "1486")

    def test_无效值给破折号(self):
        for bad in (None, "88x", float("nan"), float("inf"), True, False):
            self.assertEqual(F.display_num(bad, 0), "—", bad)
            self.assertEqual(F._move_value("tok_s", bad), "—", bad)

    def test_图表速度格与文案同一位数(self):
        self.assertEqual(R.speed_num({"v": 67.76316669225746}), "68")
        self.assertEqual(R.speed_num({"ve": 67.76316669225746}), "≈68")
        self.assertEqual(R.speed_num({"vb": [50.4, 120.6]}), "50–121")
        self.assertEqual(R.speed_num({}), "—")
        # 单档不能写成 "90~90"，且必须与区间同一格式器
        self.assertEqual(R.span([90.0], " tok/s", cell=R.speed_text), "90 tok/s")
        self.assertEqual(R.span([50.4, 120.6], " tok/s", cell=R.speed_text), "50~121 tok/s")
        # 其它字段沿用默认的 :g（指数/成本没换通道）
        self.assertEqual(R.span([0.15, 7.76], pre="$"), "$0.15~$7.76")

    def test_收位后相等的区间不能自问自答(self):
        """71.4 与 70.6 原始值不等、收位后都是 71 —— 按原始值判"是不是同一档"会写出 71~71。"""
        self.assertEqual(R.span([71.4, 70.6], " tok/s", cell=R.speed_text), "71 tok/s")
        self.assertEqual(R.speed_num({"vb": [70.6, 71.4]}), "71")
        # 默认 :g 那条老判据不许被顺手改掉
        self.assertEqual(R.span([70.6, 71.4], " tok/s"), "70.6~71.4 tok/s")

    def test_位数表只有一个来源(self):
        """爬虫发一张表，看板只消费；JS 里再出现一份字段→位数的字面量就是第二套口径。"""
        self.assertIn('"display_decimals": DISPLAY_DECIMALS', inspect.getsource(F.run))
        dash = (Path(F.__file__).resolve().parent / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("D.display_decimals", dash)
        self.assertIn("fmt(f.to, dpOf(f.field))", dash)
        self.assertIn("fmt(f.from, dpOf(f.field))", dash)
        self.assertNotRegex(dash, r"tok_s\s*:\s*\d", "看板里抄了第二份位数表")
        # 渲染面不写死位数，取的是同一张表
        self.assertIn('DISPLAY_DECIMALS["tok_s"]', inspect.getsource(R.speed_text))

    def test_表的内容就是量出来的旧形态(self):
        self.assertEqual(F.DISPLAY_DECIMALS, {"tok_s": 0, "ttft_s": 2, "total_s": 2,
                                              "first_answer_s": 2})


if __name__ == "__main__":
    unittest.main()
