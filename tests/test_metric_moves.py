"""2026-11-01 用户需求：变化记录随尺寸指标切换，并对照另一项指标。"""
import json
import sqlite3
import subprocess
import unittest

import fetch_model_data as F
import render_scatter_svg as R


class MetricMoves(unittest.TestCase):
    def changes(self, speed=150, task=60, old_task=100):
        old = {"name": "Model", "tok_s": 100, "time_per_task_s": old_task,
               "evaluation_version": "5.0"}
        return F.diff_models([{**old, "tok_s": speed, "time_per_task_s": task}], {"Model": old})

    def test_shared_threshold_and_metric_filter(self):
        rules = {f: threshold for f, _, threshold in F.MOVE_RULES}
        self.assertEqual(rules["tok_s"], rules["time_per_task_s"])
        for field in ("tok_s", "time_per_task_s"):
            for pct, count in ((F.MOVE_PCT, 0), (F.MOVE_PCT + 0.01, 1)):
                changes = [{"name": "M", "type": "changed", "fields": [
                    {"field": field, "kind": "changed", "from": 100, "to": 121, "pct": pct}]}]
                self.assertEqual(len(F.notable_moves(changes, field)), count)
        changes = self.changes()
        self.assertEqual([m["field"] for m in F.notable_moves(changes, "tok_s")], ["tok_s"])
        self.assertEqual([m["field"] for m in F.notable_moves(changes, "time_per_task_s")], ["time_per_task_s"])

    def test_cross_metric_explanations(self):
        """关联读数一句话说完：另一项怎么变、符不符合反比、差几个百分点。

        名义反比是 `期望 = a₂/b₂ - 1`，`偏差 = 实际 - 期望` 的单位是百分点，
        不是百分比本身 —— 两个百分比相减后再标 `%` 会让人读成相对变化。
        """
        cases = (
            (150, 60, "不符合反比，偏差 -6.7 个百分点"),      # 反方向但幅度差 6.7pp
            (140, 71.4286, "符合反比，偏差 +0.0 个百分点"),   # 固定输出量下的精确反比
            (150, 150, "不符合反比，偏差 +83.3 个百分点"),    # 同向，违背反比
            (100, 150, "输出速度未变"),
            (None, 150, "输出速度数据不足"),
        )
        for speed, task, expected in cases:
            with self.subTest(speed=speed, task=task):
                lines = "\n".join(F.merged_move_lines(
                    F.notable_moves(self.changes(speed, task), "time_per_task_s")))
                self.assertIn(expected, lines)
                self.assertNotIn("关联说明", lines, "只挂一次「关联：」，不重复模型名")
                self.assertNotIn("不证明因果", lines, "免责只说一次，挂在门槛那行")

    def test_inverse_tolerance_boundary(self):
        """容差是 5pp：恰好 5.0pp 判符合，5.1pp 就判不符合。

        构造方式：tok_s 100→200 时期望耗时 -50%；`time_per_task_s` 取 100/1.95 = 51.2821，
        实际 -48.7%，偏差正好 +5.0pp；再抬高一点到 51.3084 就是 +5.1pp。
        """
        base = {"evaluation_version": "5.0", "tok_s": 100, "time_per_task_s": 100}
        for other, verdict in ((100 / 1.95, "符合反比"), (100 / 1.949, "不符合反比")):
            rel = F.inverse_relation(base, {**base, "tok_s": 200, "time_per_task_s": other},
                                     "tok_s", "time_per_task_s")
            self.assertIn(verdict, rel)
        self.assertEqual(F.INVERSE_TOL_PP, 5.0)

    def test_relation_states_do_not_mention_causality(self):
        """逐条里不再带免责长句；那句话由 `move_hint` 统一说一次。"""
        self.assertIn("不证明因果", F.move_hint())
        self.assertNotIn("不证明因果", F.move_hint().split("；")[0])

    def test_cross_metric_span_keeps_units(self):
        lines = "\n".join(F.merged_move_lines(F.notable_moves(self.changes(), "tok_s")))
        self.assertIn("任务耗时(估) 100→60 秒", lines)

    def test_old_baseline_does_not_create_task_change(self):
        changes = F.diff_models([{"name": "M", "time_per_task_s": 80}], {"M": {"name": "M"}})
        self.assertEqual(changes, [])

    def test_history_filters_before_selecting_round(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE snapshots(ts TEXT, source TEXT, ok INT, payload TEXT)")
        for ts, speed, task in (("1", 100, 100), ("2", 100, 60), ("3", 200, 60), ("4", 200, 60)):
            con.execute("INSERT INTO snapshots VALUES(?,?,1,?)", (ts, F.SRC_DIFF, json.dumps([
                {"name": "M", "tok_s": speed, "time_per_task_s": task,
                 "evaluation_version": "5.0"}])))
        speed = F.last_notable_round(con, [], {}, metric="tok_s")
        task = F.last_notable_round(con, [], {}, metric="time_per_task_s")
        self.assertEqual(speed["at"], "3")
        self.assertEqual(task["at"], "2")
        self.assertIn("输出速度未变", task["lines"][0])
        con.close()

    def test_footer_selects_whole_view_and_excludes_legacy_history(self):
        D = {"data_state": "ok", "last_alert": {"at": "old", "lines": ["旧TPS记录"]}, "metric_views": {}}
        for metric, name in (("tok_s", "TPS记录"), ("time_per_task_s", "任务记录")):
            D["metric_views"][metric] = {
                "moves": [{"text": name}], "move_lines": [name],
                "alert": {"kind": "moved", "last": {"steps": 0}, "hint": F.move_hint(metric)},
                "prev_moves": {"at": "before", "steps": 1, "lines": [name + "历史"]}}
        tip = R.foot_tip(D, "time_per_task_s")
        self.assertIn("任务记录", tip)
        self.assertIn("任务记录历史", tip)
        self.assertNotIn("TPS记录", tip)
        self.assertNotIn("输出速度 >", tip)

    def test_actual_size_js_switches_tip_and_restores_initial_mode(self):
        script = """
const assert = require('node:assert/strict');
let changed;
const tip = {textContent:'', getAttribute:k => k === 'data-task-tip' ? '任务记录' : 'TPS记录'};
const box = {setAttribute(){}, addEventListener(k, f){changed=f}, focus(){}};
const svg = {getAttribute(){return ''}, querySelectorAll(){return []}, setAttribute(){}};
global.window = {MR_SIZE_TASK:1};
global.document = {querySelector(){return svg}, getElementById(k){return k==='task-size'?box:k==='fetch-tip'?tip:null}};
""" + R.SIZE_JS + """
assert.equal(tip.textContent, '任务记录');
box.checked=false; changed(); assert.equal(tip.textContent, 'TPS记录');
box.checked=true; changed(); assert.equal(tip.textContent, '任务记录');
"""
        subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
