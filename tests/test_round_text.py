#!/usr/bin/env python3
"""重述工具的判据：按台账重算冻结文本，数值与结构一个字都不能变。

背景：`latest.json` 与 `.last_alert.json` 里的文案是**当时那轮代码**写死的，
改措辞后要等下一轮抓取才会更新。两个工具负责按同一轮快照重算，但重算是"再算一遍"，
天然有走样的风险 —— 若重放与已发布那份在数字上不一致，就必须停下，不能发布半新半旧的一份。
"""
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fetch_model_data as F
from tools.restate_payload_text import _strip
from tools.round_text import alert_lines, alert_text, rewrite_alert, round_changes, round_view


def row(name, speed, task, version="5.0"):
    return {"name": name, "creator": "Acme", "tok_s": speed,
            "time_per_task_s": task, "evaluation_version": version}


class RoundText(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute("""CREATE TABLE snapshots(ts TEXT NOT NULL, source TEXT NOT NULL,
                            n_rows INTEGER, ok INTEGER, err TEXT, payload TEXT,
                            evaluation_version TEXT, PRIMARY KEY (ts, source))""")
        self.cfg = {"keyword_include": [], "keyword_exclude": []}
        self._snap("1", [row("A", 100, 100), row("B", 100, 100)])
        self._snap("2", [row("A", 150, 66.6667), row("B", 100, 100)])
        self._snap("3", [row("A", 150, 66.6667), row("B", 100, 100)])

    def _snap(self, ts, rows):
        self.con.execute("INSERT INTO snapshots(ts,source,n_rows,ok,err,payload,"
                         "evaluation_version) VALUES(?,?,?,1,NULL,?,NULL)",
                         (ts, F.SRC_DIFF, len(rows), json.dumps(rows)))

    def test_replay_uses_current_wording_and_keeps_numbers(self):
        view = round_view(self.con, "2", self.cfg)
        # A 的速度与耗时都过门槛：字段各算一条，合并行按字段分组
        self.assertEqual(sorted(m["field"] for m in view["moves"]),
                         ["time_per_task_s", "tok_s"])
        self.assertTrue(all(m["name"] == "A" for m in view["moves"]))
        line = "\n".join(view["move_lines"])
        self.assertIn("关联：", line)
        self.assertIn("偏差", line)
        self.assertNotIn("关联说明", line)
        self.assertNotIn("不证明因果", line, "免责由 move_hint 统一说一次")
        self.assertIn(150.0, [f["to"] for c in view["changes"]
                              for f in c["fields"] if f["field"] == "tok_s"])

    def test_missing_round_is_none_not_an_empty_record(self):
        """找不到就报 None：空记录会被当成"这轮没变动"，把历史说反。"""
        self.assertIsNone(round_view(self.con, "9999", self.cfg))
        self.assertIsNone(round_changes(self.con, "9999", self.cfg))

    def test_last_round_without_a_baseline_is_none(self):
        self.assertIsNone(round_view(self.con, "1", self.cfg), "最早一轮没有可比的基线")

    def test_cumulative_alert_keeps_its_lines(self):
        """`moved-cumulative` 鸣响时那轮是安静的：正文来自累计块，不是本轮变动。

        早先按 `moved` 处理，重算出来是空列表，等于把那条记录的内容抹掉。
        """
        view = round_view(self.con, "2", self.cfg)
        rec = {"at": "2", "ts": "2", "kind": "moved-cumulative",
               "text": "旧文案", "lines": [], "metric_lines": {}}
        new = rewrite_alert(rec, view)
        self.assertEqual(new["lines"], alert_lines(view, "moved-cumulative"))
        self.assertTrue(new["lines"], "累计那一路必须留下明细")
        self.assertEqual(new["text"], alert_text(view, "moved-cumulative"))
        self.assertEqual(new["at"], "2", "身份字段不动")
        self.assertEqual(new["kind"], "moved-cumulative")

    def test_rewrite_does_not_touch_identity_fields(self):
        view = round_view(self.con, "2", self.cfg)
        rec = {"at": "2", "ts": "2", "kind": "moved", "label": "有达门槛的变动",
               "tone": "info", "key": "alert:moved:X", "text": "", "n": 0,
               "lines": [], "metric_lines": {}}
        new = rewrite_alert(rec, view)
        for field in ("at", "ts", "kind", "label", "tone", "key"):
            self.assertEqual(new[field], rec[field])
        self.assertEqual(new["n"], len(new["lines"]))

    def test_strip_removes_only_derived_text(self):
        """护栏只忽略派生文本与它们的行数复述；真实观测的 `n` 必须留下。"""
        payload = {"sources": {"aa": {"n": 106}},
                   "moves": [{"text": "x", "name": "A"}],
                   "last_alert": {"n": 4, "lines": ["x"], "ts": "2"},
                   "changes": [{"name": "A", "fields": [{"field": "tok_s",
                                                         "from": 100, "to": 150,
                                                         "relation": "e"}]}]}
        out = _strip(payload)
        self.assertEqual(out["sources"]["aa"]["n"], 106, "源里模型数是真实观测，不能被忽略")
        self.assertNotIn("n", out["last_alert"], "记录里的 n 只是行数复述")
        self.assertEqual(out["changes"][0]["fields"][0]["from"], 100)
        self.assertNotIn("relation", out["changes"][0]["fields"][0])
        self.assertNotIn("text", out["moves"][0])

    def test_strip_detects_a_numeric_drift(self):
        """护栏的用处：数字变了必须能被发现。"""
        a = {"changes": [{"name": "A", "fields": [{"field": "tok_s", "to": 150}]}]}
        b = {"changes": [{"name": "A", "fields": [{"field": "tok_s", "to": 151}]}]}
        self.assertNotEqual(_strip(a), _strip(b))


if __name__ == "__main__":
    unittest.main()
