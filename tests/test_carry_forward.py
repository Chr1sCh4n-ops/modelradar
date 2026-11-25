"""2026-11-17 用户口径：撤榜的行只要评测版本没变就保留，不再要求"没被现役行全面优于"。

旧判据让被新一代全面压过的档位静默消失，读者只看到它凭空不见，看不出"这个档位
已经退出榜单"。用例锁定三件事：被全面优于也要回来、with fallback 重复档不回来、
评测版本变了不回来（跨版不可比）。
"""
import json
import sqlite3
import unittest
from unittest.mock import patch

import fetch_model_data as F

V = "5.0.1"
NOW = "2026-11-17 10:00:00"


def _row(name, slug, index=30, apt=5.0, tok=100.0, creator="Acme", ver=V):
    """一条最小但完整的 AA 行：merge() 会读到的字段都要在，否则是假数据不是用例。"""
    return {"name": name, "aa_slug": slug, "index": index, "apt_usd": apt,
            "tok_s": tok, "creator": creator, "evaluation_version": ver,
            "ctx": 200000, "ttft_s": 1.0, "total_s": 2.0, "first_answer_s": 1.0,
            "logo": "", "output_tokens_per_task": None, "time_per_task_s": None}


class CarryForward(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(
            "CREATE TABLE snapshots (ts TEXT, source TEXT, n_rows INT, ok INT,"
            " err TEXT, payload TEXT, evaluation_version TEXT)")
        # carry_forward 走 merge()，而 merge() 会按 watchlist 关键词过滤。
        # 这里测的是沿用判据本身，不是词表，所以让过滤恒放行。
        self._want = patch.object(F, "want_row", return_value=True)
        self._want.start()

    def tearDown(self):
        self._want.stop()
        self.con.close()

    def _snapshot(self, ts, rows, source=None):
        self.con.execute(
            "INSERT INTO snapshots VALUES (?,?,?,1,NULL,?,?)",
            (ts, source or F.SRC_AA, len(rows), json.dumps(rows), V))
        self.con.commit()

    def _carry(self, current, now=NOW):
        models = [dict(m) for m in current]
        n = F.carry_forward(models, self.con, {}, [], {}, now)
        return n, models

    def test_dominated_row_is_still_carried(self):
        """被现役行三项全部压过的撤榜行也回来 —— 这正是 2026-11-17 之前会丢的那类。"""
        self._snapshot("2026-11-16 08:00:00",
                       [_row("Old (high)", "old", index=31, apt=7.77, tok=116)])
        current = [_row("New (high)", "new", index=51, apt=1.87, tok=181)]
        n, models = self._carry(current)
        self.assertEqual(n, 1)
        names = [m["name"] for m in models]
        self.assertIn("Old (high)", names)
        back = next(m for m in models if m["name"] == "Old (high)")
        self.assertEqual(back["carried_asof"], "2026-11-16 08:00:00")

    def test_fallback_duplicate_is_not_carried(self):
        self._snapshot("2026-11-16 08:00:00",
                       [_row("X (high)", "x", index=40, apt=1.0, tok=100),
                        _row("X (high with fallback)", "x", index=40, apt=1.0, tok=100)])
        current = [_row("Other (high)", "other", index=10, apt=9.0, tok=10)]
        n, models = self._carry(current)
        self.assertEqual(n, 1)
        self.assertIn("X (high)", [m["name"] for m in models])
        self.assertNotIn("X (high with fallback)", [m["name"] for m in models])

    def test_version_change_is_not_carried(self):
        self._snapshot("2026-11-16 08:00:00",
                       [_row("Old (high)", "old", ver="4.3.2")])
        current = [_row("New (high)", "new")]
        n, models = self._carry(current)
        self.assertEqual(n, 0)
        self.assertEqual([m["name"] for m in models], ["New (high)"])

    def test_row_still_on_board_is_not_duplicated(self):
        self._snapshot("2026-11-16 08:00:00", [_row("Keep (high)", "keep")])
        current = [_row("Keep (high)", "keep")]
        n, models = self._carry(current)
        self.assertEqual(n, 0)
        self.assertEqual(len(models), 1)

    def test_shared_release_slug_no_longer_hides_siblings(self):
        """aa_slug 现在是每个 release 一份：同族幸存档位不能连带把被撤档位一起跳过。"""
        self._snapshot("2026-11-16 08:00:00",
                       [_row("Family (low)", "family", index=20, apt=2.0, tok=90),
                        _row("Family (medium)", "family", index=30, apt=6.26, tok=118),
                        _row("Family (high)", "family", index=31, apt=7.77, tok=116)])
        current = [_row("Family (low)", "family", index=20, apt=2.0, tok=90)]
        n, models = self._carry(current)
        self.assertEqual(n, 2)
        names = sorted(m["name"] for m in models)
        self.assertEqual(names, ["Family (high)", "Family (low)", "Family (medium)"])

    def test_beyond_window_is_not_carried(self):
        self._snapshot("2026-09-01 08:00:00", [_row("Ancient (high)", "ancient")])
        current = [_row("New (high)", "new")]
        n, models = self._carry(current)
        self.assertEqual(n, 0)

    def test_last_seen_wins_when_row_vanished_later(self):
        self._snapshot("2026-11-10 08:00:00",
                       [_row("Gone (high)", "gone", index=99, apt=1.0, tok=999)])
        self._snapshot("2026-11-16 08:00:00",
                       [_row("Gone (high)", "gone", index=42, apt=3.0, tok=120)])
        current = [_row("New (high)", "new")]
        n, models = self._carry(current)
        self.assertEqual(n, 1)
        back = next(m for m in models if m["name"] == "Gone (high)")
        self.assertEqual(back["carried_asof"], "2026-11-16 08:00:00")
        self.assertEqual(back["index"], 42)

    def test_is_fallback_dup_only_matches_tier_suffix(self):
        self.assertTrue(F.is_fallback_dup("Claude Opus 5.5 (high with fallback)"))
        self.assertTrue(F.is_fallback_dup("Claude Opus 5.5 (Max, Default Fallback)"))
        self.assertFalse(F.is_fallback_dup("Claude Opus 5.5 (high)"))
        self.assertFalse(F.is_fallback_dup("Qwen3.8 Max (0902)"))


if __name__ == "__main__":
    unittest.main()
