"""2026-10-28 用户要求：历史保留、跨评测代次隔离。"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fetch_model_data as F
os.environ.setdefault("MODELRADAR_NO_HEADLESS", "1")  # 测试不启动 Chrome/钥匙串


class EvaluationVersions(unittest.TestCase):
    def test_task_minutes_never_become_tps_or_first_chunk_latency(self):
        row = ('<table><thead><tr><th>Model</th><th>Context Window</th><th>Creator</th>'
               '<th>Artificial Analysis Intelligence Index</th><th>Cost per Task USD</th>'
               '<th>Time per Task Minutes</th><th>First Answer Token (s)</th>'
               '<th>Further Analysis</th></tr></thead><tbody><tr>'
               '<td><a href="/models/model-high">Model</a></td><td>1M</td><td>Acme</td>'
               '<td>50</td><td>$1</td><td>6.5</td><td>25</td><td>Model Providers</td>'
               '</tr></tbody></table>')
        model = {"release": {"slug": "model-high"}, "name": "Model",
                 "shortName": "Model", "intelligenceIndex": 50,
                 "intelligenceIndexCostPerTask": 1, "modelCreatorName": "Acme",
                 "contextWindowTokens": 1000000, "outputSpeed": 57.43,
                 "timeToFirstToken": 0.8, "timeToFirstAnswerToken": 25}
        text = '1:{"initialModels":' + json.dumps([model]) + '}'
        page = row + '<script>self.__next_f.push(' + json.dumps([1, text]) + ')</script>'
        parsed = F.parse_aa(page)[0]
        self.assertEqual(parsed["tok_s"], 57.43)
        self.assertEqual(parsed["ttft_s"], 0.8)
        self.assertEqual(parsed["first_answer_s"], 25)
        self.assertIsNone(parsed["total_s"], "首回答延迟不能顶成旧基准总响应")
        with self.assertRaises(ValueError):
            F.parse_aa(row)

    def test_first_answer_token_rejects_invalid_values(self):
        # 非有限/负数/布尔都不是有效实测，留空而不是写一个假的首回答延迟。
        for bad in (-1, float("nan"), float("inf"), True, "25", None):
            model = {"release": {"slug": "model-high"}, "name": "Model",
                     "shortName": "Model", "intelligenceIndex": 50,
                     "intelligenceIndexCostPerTask": 1, "modelCreatorName": "Acme",
                     "contextWindowTokens": 1000000, "outputSpeed": 57.43,
                     "timeToFirstToken": 0.8, "timeToFirstAnswerToken": bad}
            row = ('<table><thead><tr><th>Model</th><th>Context Window</th><th>Creator</th>'
                   '<th>Artificial Analysis Intelligence Index</th><th>Cost per Task USD</th>'
                   '<th>Time per Task Minutes</th><th>First Answer Token (s)</th>'
                   '<th>Further Analysis</th></tr></thead><tbody><tr>'
                   '<td><a href="/models/model-high">Model</a></td><td>1M</td><td>Acme</td>'
                   '<td>50</td><td>$1</td><td>6.5</td><td>25</td><td>Model Providers</td>'
                   '</tr></tbody></table>')
            text = '1:{"initialModels":' + json.dumps([model]) + '}'
            page = row + '<script>self.__next_f.push(' + json.dumps([1, text]) + ')</script>'
            self.assertIsNone(F.parse_aa(page)[0]["first_answer_s"], bad)

    def test_minor_versions_keep_diffs_and_label_both_versions(self):
        for version in ("5.0.1", "5.1"):
            old = {"A": {"name": "A", "index": 45, "apt_usd": 1,
                         "time_per_task_s": 100, "tok_s": 100, "evaluation_version": "5.0"}}
            new = [{**old["A"], "index": 50, "apt_usd": 2, "time_per_task_s": 150,
                    "evaluation_version": version}]
            changes = F.diff_models(new, old)
            self.assertEqual({f["field"] for f in changes[0]["fields"]},
                             {"index", "apt_usd", "time_per_task_s"})
            self.assertEqual(F.version_change_summary(old, new), {})
            lines = F.merged_move_lines(F.notable_moves(changes), changes)
            self.assertIn(f"评测版本 5.0 → {version}", "；".join(lines))

    def test_major_and_unknown_versions_remain_isolated(self):
        for version in ("4.3.2", "6.0", None):
            self.assertFalse(F.same_evaluation_major(version, "5.0.1"))
            old = {"A": {"name": "A", "index": 45, "evaluation_version": version}}
            self.assertEqual(F.diff_models(
                [{"name": "A", "index": 50, "evaluation_version": "5.0.1"}], old), [])

    def test_week_moves_reuses_minor_baseline_but_not_previous_major(self):
        import summarize
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE series(ts TEXT,name TEXT,field TEXT,value REAL,evaluation_version TEXT)")
        for ts, version, value in (("2026-11-01 10:00:00", "4.3", 10),
                                   ("2026-11-02 10:00:00", "5.0", 40),
                                   ("2026-11-04 10:00:00", "5.0.1", 50)):
            con.execute("INSERT INTO series VALUES(?, 'A', 'index', ?, ?)", (ts, value, version))
        weekly = summarize.week_moves(con)
        con.close()
        self.assertEqual(weekly[0]["from"], 40)
        self.assertEqual(weekly[0]["to"], 50)
        self.assertEqual(weekly[0]["tags"], ["评测版本 5.0 → 5.0.1"])

    def test_week_moves_excludes_quarantined_latest_observation(self):
        import summarize
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE series(ts TEXT,name TEXT,field TEXT,value REAL,evaluation_version TEXT)")
        con.executemany("INSERT INTO series VALUES(?,'A','tok_s',?,'5.0.1')", [
            ("2026-11-03 10:00:00", 100), ("2026-11-04 10:00:00", 1)])
        with patch.object(F, "load_quarantine", return_value=[{
                "name": "A", "field": "tok_s", "from_ts": "2026-11-04", "to_ts": "2026-11-05"}]):
            self.assertEqual(summarize.week_moves(con), [])
        con.close()

    def test_version_requires_evidence(self):
        self.assertEqual(F.evaluation_version("Artificial Analysis Intelligence Index v5.0"), "5.0")
        with self.assertRaises(ValueError):
            F.evaluation_version("Model scores 2026-10-28")
        self.assertEqual(F.evaluation_version(
            'Intelligence Index v4.3 <h3>Artificial Analysis Intelligence Index v5.0</h3>'), "5.0")
        with self.assertRaises(ValueError):
            F.evaluation_version('Intelligence Index v4.3 Intelligence Index v5.0')

    def test_migration_preserves_original_payload(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "history.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE snapshots(ts TEXT,source TEXT,n_rows INTEGER,ok INTEGER,err TEXT,payload TEXT,PRIMARY KEY (ts, source))")
            raw = json.dumps([{"name": "Old", "index": 45}])
            con.execute("INSERT INTO snapshots VALUES('2026-10-01','aa',1,1,NULL,?)", (raw,))
            con.commit()
            con.close()
            with patch.object(F, "DB_PATH", db):
                con = F.ensure_db()
            self.assertEqual(con.execute("SELECT payload,evaluation_version FROM snapshots").fetchone(), (raw, None))
            con.close()

    def test_cross_version_diff_and_summary(self):
        old = {"A": {"name": "A", "index": 45, "apt_usd": 1, "evaluation_version": "4.3.2"}}
        new = [{"name": "A", "index": 32, "apt_usd": 2, "evaluation_version": "5.0"}]
        self.assertEqual(F.diff_models(new, old), [])
        summary = F.version_change_summary(old, new)
        self.assertEqual(summary["mean_delta"], -13)
        self.assertEqual(summary["median_delta"], -13)
        self.assertEqual(summary["delta_stddev"], 0)
        self.assertEqual(F.alert_policy({"data_state": "ok", "version_change": summary})["kind"], "version-change")

    def test_multi_model_summary_uses_paired_deltas(self):
        old = {
            "A": {"name": "A", "index": 45, "evaluation_version": "4.3.2"},
            "B": {"name": "B", "index": 50, "evaluation_version": "4.3.2"},
            "C": {"name": "C", "index": 60, "evaluation_version": "4.3.2"},
        }
        new = [
            {"name": "A", "index": 32, "evaluation_version": "5.0"},
            {"name": "B", "index": 58, "evaluation_version": "5.0"},
            {"name": "C", "index": 40, "evaluation_version": "5.0"},
        ]
        summary = F.version_change_summary(old, new)
        self.assertEqual(summary["from"], "4.3.2")
        self.assertEqual(summary["to"], "5.0")
        self.assertEqual(summary["n"], 3)
        self.assertEqual(summary["mean_delta"], -8.33)
        self.assertEqual(summary["median_delta"], -13)
        self.assertEqual(summary["delta_stddev"], 11.9)
        self.assertEqual(summary["q1_delta"], -16.5)
        self.assertEqual(summary["q3_delta"], -2.5)
        self.assertIn("3 项同名同档评分差", summary["text"])
        self.assertIn("不是模型当日能力涨跌", summary["text"])

    def test_same_version_summary_is_empty(self):
        old = {"A": {"name": "A", "index": 45, "evaluation_version": "5.0"}}
        new = [{"name": "A", "index": 32, "evaluation_version": "5.0"}]
        self.assertEqual(F.version_change_summary(old, new), {})

    def test_unknown_old_version_summary_has_no_statistical_deltas(self):
        old = {
            "A": {"name": "A", "index": 45},
            "B": {"name": "B", "index": 50},
        }
        new = [
            {"name": "A", "index": 32, "evaluation_version": "5.0"},
            {"name": "B", "index": 58, "evaluation_version": "5.0"},
        ]
        summary = F.version_change_summary(old, new)
        self.assertIsNone(summary["from"])
        self.assertEqual(summary["to"], "5.0")
        self.assertEqual(summary["n"], 0)
        for key in ("mean_delta", "median_delta", "delta_stddev", "q1_delta", "q3_delta"):
            self.assertNotIn(key, summary)
        self.assertIn("首次记录 AA 评测版本 5.0", summary["text"])
        self.assertIn("旧历史版本未知", summary["text"])
        self.assertNotIn("均值", summary["text"])
        self.assertNotIn("标准差", summary["text"])


class BaselineReplayVersionIsolation(unittest.TestCase):
    def test_minor_versions_restore_cumulative_and_historical_comparison(self):
        with tempfile.TemporaryDirectory() as td:
            con = self._con(td)
            self._snapshot(con, "2026-11-01 10:00:00", "5.0", 20)
            self._snapshot(con, "2026-11-02 10:00:00", "5.0.1", 50)
            self._snapshot(con, "2026-11-03 10:00:00", "5.0.1", 50)
            models = [{"name": "A", "index": 50, "evaluation_version": "5.0.1"}]
            cfg = {"evaluation_version": "5.0.1"}
            last = F.last_notable_round(con, [], cfg)
            cumulative = F.cumulative_moves(con, models, cfg)
            con.close()
        self.assertEqual(last["at"], "2026-11-02 10:00:00")
        self.assertEqual(cumulative["at"], "2026-11-01 10:00:00")
        self.assertIn("评测版本 5.0 → 5.0.1", "；".join(last["lines"]))
        self.assertIn("评测版本 5.0 → 5.0.1", "；".join(cumulative["lines"]))

    def _con(self, td):
        db = Path(td) / "history.db"
        with patch.object(F, "DB_PATH", db):
            con = F.ensure_db()
        return con

    def _snapshot(self, con, ts, version, index):
        rows = [{"name": "A", "creator": "Acme", "index": index,
                 "evaluation_version": version}]
        con.execute(
            "INSERT INTO snapshots(ts,source,n_rows,ok,err,payload,evaluation_version) "
            "VALUES(?,?,?,?,?,?,?)",
            (ts, F.SRC_DIFF, 1, 1, None, json.dumps(rows), version))
        con.commit()

    def test_last_notable_and_cumulative_skip_cross_version_baselines(self):
        ts0 = "2026-10-27 10:00:05"
        ts1 = "2026-10-27 10:00:04"
        ts2_cross = "2026-10-27 10:00:03"
        ts3_same = "2026-10-27 10:00:02"
        ts4_same = "2026-10-27 10:00:01"
        with tempfile.TemporaryDirectory() as td:
            con = self._con(td)
            self._snapshot(con, ts4_same, "5.0", 20)
            self._snapshot(con, ts3_same, "5.0", 50)
            self._snapshot(con, ts2_cross, "4.3.2", 10)
            self._snapshot(con, ts1, "5.0", 50)
            self._snapshot(con, ts0, "5.0", 50)
            models = [{"name": "A", "index": 50, "evaluation_version": "5.0"}]
            cfg = {"evaluation_version": "5.0"}

            last = F.last_notable_round(con, [], cfg)
            cumulative = F.cumulative_moves(con, models, cfg)
            con.close()

        self.assertEqual(last.get("at"), ts3_same)
        self.assertEqual(last.get("base"), ts4_same)
        self.assertNotEqual(last.get("at"), ts2_cross)
        self.assertEqual(cumulative.get("at"), ts4_same)
        self.assertEqual(cumulative.get("steps"), 4)
        self.assertNotEqual(cumulative.get("at"), ts2_cross)


class RunMetadataFetchFailure(unittest.TestCase):
    def test_parsed_rows_are_not_used_when_version_metadata_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            data.mkdir()
            chart = data / "chart.html"
            chart.write_text("old chart")
            env = {**os.environ, "HOME": td, "MODELRADAR_SUPPORT": str(root / "support"),
                   "MODELRADAR_DATA_DIR": str(data), "MODELRADAR_REPO_DATA": str(root / "browser"),
                   "MR_HUB_NOTIFY": "", "MODELRADAR_NO_SUMMARY": "1"}
            code = r'''
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import fetch_model_data as F

HTML = b"<table><thead><tr><th>Model</th><th>Context Window</th><th>Creator</th><th>Artificial Analysis Intelligence Index</th><th>Cost per Task USD</th><th>Output Speed (t/s)</th><th>Latency First Chunk (s)</th><th>Total Response (s)</th></tr></thead><tbody><tr><td>Qwen3.8</td><td>128k</td><td>Alibaba</td><td>50</td><td>1</td><td>100</td><td>0.1</td><td>1</td></tr></tbody></table>"
assert F.parse_aa(HTML.decode("utf-8")), "fixture must parse"
calls = []
def fetched(url, **kw):
    calls.append(url)
    if url == F.SOURCES[F.SRC_AA]:
        return HTML
    if url == F.AA_METHODOLOGY:
        raise RuntimeError("metadata unavailable")
    raise AssertionError("unexpected fetch " + url)
config = Path.home() / "watchlist.json"
config.write_text('{"keyword_include": ["qwen"]}')
args = ["fetch_model_data.py", "--sources", "aa", "--no-logos"]
with patch.object(F, "CFG_PATH", config), patch.object(F, "fetch", fetched), \
     patch.object(F, "notify_fresh"), patch.object(F, "maybe_summarize"), \
     patch.object(F, "probe_palette", return_value={}), patch.object(sys, "argv", args):
    try:
        F.main()
    finally:
        payload = json.loads(F.LATEST_JSON.read_text())
        assert payload["sources"]["aa"]["ok"] is False, payload["sources"]
        assert "metadata unavailable" in payload["sources"]["aa"]["err"], payload["sources"]
        assert payload["models"] == [], payload["models"]
        assert payload.get("evaluation_version") is None, payload.get("evaluation_version")
        assert payload["data_state"] == "empty", payload["data_state"]
        con = sqlite3.connect(F.DB_PATH)
        ok_aa = con.execute("SELECT COUNT(*) FROM snapshots WHERE source=? AND ok=1",
                            (F.SRC_AA,)).fetchone()[0]
        con.close()
        assert ok_aa == 0, ok_aa
        assert calls.count(F.SOURCES[F.SRC_AA]) == 1, calls
        assert calls.count(F.AA_METHODOLOGY) == 1, calls
'''
            result = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], cwd=td, env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(chart.read_text(), "old chart")


if __name__ == "__main__":
    unittest.main()
