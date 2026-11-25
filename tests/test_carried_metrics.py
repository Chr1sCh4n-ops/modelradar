"""2026-11-18 用户要求：沿用行的历史参照与任务耗时补回**它自己那一轮**的旧读数。

沿用行（`carried_asof`）按规矩跳过本轮的 Progress/耗时匹配，于是节点上只剩智能指数，
读者以为这档没有那两项读数。回填必须满足三件事：取与 asof 同一轮的快照（Progress 与
耗时都比 AA 晚几秒落地，`ts<=asof` 会稳定错一轮）、按 (release, 档位) 精确匹配不广播、
版本不一致或当期缺值就留空。
"""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_model_data as F

ASOF = "2026-11-17 18:24:43"
V = "5.0.1"
PV = "1"


def _carried(name, tier_slug, asof=ASOF, ver=V):
    return {"name": name, "aa_slug": "fable-5-2", "evaluation_version": ver,
            "carried_asof": asof, "progress_index": None, "progress_version": None,
            "time_per_task_s": None, "output_tokens_per_task": None}


def _progress(name, effort, score, ver=PV):
    return {"slug": "fable-5-2", "name": name, "release": {"slug": "fable-5-2"},
            "effort": {"slug": effort}, "score": score, "evaluation_version": ver}


def _times(name, suffix, secs, tokens, ver=V):
    return {"slug": f"fable-5-2-{suffix.lower()}", "name": name,
            "release_slug": "fable-5-2", "suffix": suffix,
            "time_per_task_s": secs, "output_tokens_per_task": tokens,
            "evaluation_version": ver}


class RestoreCarried(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(
            "CREATE TABLE snapshots (ts TEXT, source TEXT, n_rows INT, ok INT,"
            " err TEXT, payload TEXT, evaluation_version TEXT)")

    def tearDown(self):
        self.con.close()

    def _snapshot(self, ts, source, rows, version=None):
        self.con.execute("INSERT INTO snapshots VALUES (?,?,?,1,NULL,?,?)",
                         (ts, source, len(rows), json.dumps(rows), version))
        self.con.commit()

    def test_same_round_snapshots_fill_both_metrics(self):
        """Progress 与耗时都在 asof 之后数秒 —— 它们是同一轮，不是下一轮。"""
        self._snapshot("2026-11-17 18:24:47", F.SRC_PROGRESS,
                       [_progress("Fable 5.2 (Max, Default Fallback)", "max", 357.95),
                        _progress("Fable 5.2 (High, Default Fallback)", "high", 340.5)], PV)
        self._snapshot("2026-11-17 18:24:50", F.SRC_AA_TIMES,
                       [_times("Fable 5.2 (Max)", "Max", 1222.3, 51973.2),
                        _times("Fable 5.2 (High)", "High", 538.2, 23133.4)], V)
        models = [_carried("Fable 5.2 (max)", "max"), _carried("Fable 5.2 (high)", "high")]
        self.assertEqual(F.restore_carried_metrics(models, self.con, PV), (2, 2))
        self.assertEqual([m["progress_index"] for m in models], [357.95, 340.5])
        self.assertEqual([m["time_per_task_s"] for m in models], [1222.3, 538.2])
        self.assertEqual([m["output_tokens_per_task"] for m in models], [51973.2, 23133.4])
        self.assertTrue(all(m["progress_version"] == PV for m in models))

    def test_no_broadcast_across_tiers(self):
        """同 release 只标了默认档分数：其它档必须留空，不能拿 max 顶上。"""
        self._snapshot("2026-11-17 18:24:47", F.SRC_PROGRESS,
                       [_progress("Fable 5.2 (Max, Default Fallback)", "max", 357.95)], PV)
        models = [_carried("Fable 5.2 (max)", "max"), _carried("Fable 5.2 (low)", "low")]
        self.assertEqual(F.restore_carried_metrics(models, self.con, PV)[0], 1)
        self.assertEqual([m["progress_index"] for m in models], [357.95, None])

    def test_version_mismatch_stays_missing(self):
        """Progress 版本变了不回填；耗时版本变了也不回填。跨版不可比。"""
        self._snapshot("2026-11-17 18:24:47", F.SRC_PROGRESS,
                       [_progress("Fable 5.2 (Max, Default Fallback)", "max", 357.95, ver="2")], "2")
        self._snapshot("2026-11-17 18:24:50", F.SRC_AA_TIMES,
                       [_times("Fable 5.2 (Max)", "Max", 1222.3, 51973.2, ver="4.3.2")], V)
        models = [_carried("Fable 5.2 (max)", "max")]
        self.assertEqual(F.restore_carried_metrics(models, self.con, PV), (0, 0))
        self.assertIsNone(models[0]["progress_index"])
        self.assertIsNone(models[0]["time_per_task_s"])
        self.assertIsNone(models[0]["output_tokens_per_task"])

    def test_upstream_null_time_keeps_tokens_only(self):
        """当期上游就没有耗时：只回填输出量，时间留空交给派生步骤，不推算。"""
        self._snapshot("2026-11-17 18:24:50", F.SRC_AA_TIMES,
                       [_times("Fable 5.2 (Low)", "Low", None, 35860.2)], V)
        models = [_carried("Fable 5.2 (low)", "low")]
        self.assertEqual(F.restore_carried_metrics(models, self.con, PV), (0, 0))
        self.assertIsNone(models[0]["time_per_task_s"])
        self.assertEqual(models[0]["output_tokens_per_task"], 35860.2)

    def test_falls_back_to_latest_snapshot_before_asof(self):
        """asof 那轮没有该源快照时，退回 asof 之前最近一条，不拿之后的轮次。"""
        self._snapshot("2026-11-17 10:00:00", F.SRC_PROGRESS,
                       [_progress("Fable 5.2 (Max, Default Fallback)", "max", 300.0)], PV)
        self._snapshot("2026-11-18 08:15:19", F.SRC_PROGRESS,
                       [_progress("Fable 5.2 (Max, Default Fallback)", "max", 999.0)], PV)
        models = [_carried("Fable 5.2 (max)", "max")]
        self.assertEqual(F.restore_carried_metrics(models, self.con, PV)[0], 1)
        self.assertEqual(models[0]["progress_index"], 300.0)

    def test_snapshot_outside_round_window_is_not_same_round(self):
        """窗口外（如整轮早就跑完、隔了一小时）的快照不算同轮，仍按 asof 之前取。"""
        self._snapshot("2026-11-17 10:00:00", F.SRC_PROGRESS,
                       [_progress("Fable 5.2 (Max, Default Fallback)", "max", 300.0)], PV)
        self._snapshot("2026-11-17 19:24:47", F.SRC_PROGRESS,
                       [_progress("Fable 5.2 (Max, Default Fallback)", "max", 999.0)], PV)
        models = [_carried("Fable 5.2 (max)", "max")]
        F.restore_carried_metrics(models, self.con, PV)
        self.assertEqual(models[0]["progress_index"], 300.0)

    def test_no_snapshot_leaves_everything_missing(self):
        models = [_carried("Fable 5.2 (max)", "max")]
        self.assertEqual(F.restore_carried_metrics(models, self.con, PV), (0, 0))
        self.assertIsNone(models[0]["progress_index"])
        self.assertIsNone(models[0]["time_per_task_s"])

    def test_active_rows_are_untouched(self):
        """只补沿用行；现役行的值来自本轮匹配，回填不能覆盖它们。"""
        self._snapshot("2026-11-17 18:24:47", F.SRC_PROGRESS,
                       [_progress("Fable 5.2 (Max, Default Fallback)", "max", 357.95)], PV)
        active = {"name": "Fable 5.2 (max)", "aa_slug": "fable-5-2",
                  "evaluation_version": V, "progress_index": 100.0,
                  "progress_version": PV, "time_per_task_s": None,
                  "output_tokens_per_task": None}
        self.assertEqual(F.restore_carried_metrics([active], self.con, PV), (0, 0))
        self.assertEqual(active["progress_index"], 100.0)


class RestoreWiring(unittest.TestCase):
    """回填的位置是判据：晚于本轮匹配，早于推估与派生。"""

    def setUp(self):
        import inspect
        self.src = inspect.getsource(F.run)

    def test_runs_after_both_attach_steps(self):
        call = self.src.index("restore_carried_metrics(models")
        self.assertGreater(call, self.src.index("attach_progress(models"))
        self.assertGreater(call, self.src.index("attach_task_times("))

    def test_runs_before_estimates_so_derived_time_sees_tokens(self):
        call = self.src.index("restore_carried_metrics(models")
        self.assertLess(call, self.src.index("estimate_missing_tok_s(models"))
        self.assertLess(call, self.src.index("derive_missing_task_times(models"))
        self.assertLess(call, self.src.index("annotate_efficiency(models"))

