"""2026-11-02 用户要求：上游给出可疑值时剥离数据、记故障日志，供人工筛查。

判据只认三件事，缺一条这个机制就会变成"悄悄改数据"：
1. 命中区间的字段被剥离，区间外（尤其之后正常的轮次）一个都不碰；
2. 原始观测仍按真值进历史序列 —— 剥离是"别拿它算涨跌"，不是"它没发生"；
3. 每次剥离留有故障日志，能回看到底剥了什么、依据是什么。
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_model_data as F
os.environ.setdefault("MODELRADAR_NO_HEADLESS", "1")  # 测试不启动 Chrome/钥匙串

NAME = "DeepSeek V4.1 Flash 1017 (non-reasoning)"


def rule(**kw):
    base = {"name": NAME, "field": "tok_s", "from_ts": "2026-10-31 08:24:37",
            "to_ts": "2026-11-02 07:30:18", "reason": "test"}
    base.update(kw)
    return base


class QuarantineMatch(unittest.TestCase):
    def test_window_is_inclusive_and_does_not_leak(self):
        rules = [rule()]
        rows = [{"name": NAME, "tok_s": 1486.0, "index": 19}]
        for ts in ("2026-10-31 08:24:37", "2026-11-02 07:30:18"):
            self.assertEqual(len(F.quarantine_hits(rows, ts, rules)), 1, ts)
        for ts in ("2026-10-31 08:24:36", "2026-11-02 07:30:19", "2026-11-02 11:00:31"):
            self.assertEqual(F.quarantine_hits(rows, ts, rules), [], ts)

    def test_only_listed_field_and_model_are_stripped(self):
        rules = [rule()]
        rows = [{"name": NAME, "tok_s": 1486.0, "index": 19, "ttft_s": 1.15},
                {"name": "Other Model", "tok_s": 1486.0}]
        n = F.apply_quarantine(rows, "2026-11-01 07:30:19", rules, where="unit")
        self.assertEqual(n, 1)
        self.assertIsNone(rows[0]["tok_s"], "被隔离字段应置 None")
        self.assertEqual(rows[0]["index"], 19, "同行其它字段不该被牵连")
        self.assertEqual(rows[0]["ttft_s"], 1.15)
        self.assertEqual(rows[1]["tok_s"], 1486.0, "别的模型不该被牵连")

    def test_baseline_is_not_mutated_by_caller(self):
        """baseline_map 不能改调用方传进来的快照：那个对象还要拿去算整族视角。"""
        raw = [{"name": NAME, "tok_s": 1486.0, "index": 19}]
        cfg = {"keyword_include": []}
        with patch.object(F, "load_quarantine", return_value=[rule()]):
            out = F.baseline_map(raw, cfg, ts="2026-11-01 07:30:19")
        self.assertIsNone(out[NAME]["tok_s"])
        self.assertEqual(raw[0]["tok_s"], 1486.0, "原始快照必须一字不动")

    def test_fault_log_is_deduped_per_observation(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "faults.jsonl"
            hits = [{"name": NAME, "field": "tok_s", "value": 1486.0,
                     "from_ts": "a", "to_ts": "b", "reason": "r"}]
            with patch.object(F, "FAULT_LOG", log), \
                 patch.object(F, "_FAULT_LOGGED", set()):
                F.log_faults(hits, "2026-11-01 07:30:19", where="unit")
                F.log_faults(hits, "2026-11-01 07:30:19", where="unit")
            lines = log.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1, "同一轮同一观测只能记一行")
            rec = json.loads(lines[0])
            self.assertEqual((rec["round_ts"], rec["name"], rec["field"]),
                             ("2026-11-01 07:30:19", NAME, "tok_s"))
            self.assertEqual(rec["stage"], "unit")


class QuarantineEndToEnd(unittest.TestCase):
    def test_history_strips_both_sides_of_a_bad_round(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE snapshots(ts TEXT,source TEXT,ok INT,payload TEXT)")
        for ts, speed in (("1", 100), ("2", 1), ("3", 100)):
            con.execute("INSERT INTO snapshots VALUES(?,?,1,?)", (ts, F.SRC_DIFF, json.dumps([
                {"name": NAME, "tok_s": speed, "evaluation_version": "5.0.1"}])))
        with tempfile.TemporaryDirectory() as td, \
             patch.object(F, "load_quarantine", return_value=[rule(from_ts="2", to_ts="2")]), \
             patch.object(F, "FAULT_LOG", Path(td) / "faults.jsonl"):
            result = F.last_notable_round(con, [], {"evaluation_version": "5.0.1"})
        con.close()
        self.assertEqual(result["n"], 0)

    def test_full_run_strips_comparison_but_keeps_raw_history(self):
        with tempfile.TemporaryDirectory() as td:
            env = {**os.environ, "HOME": td, "MODELRADAR_SUPPORT": td + "/support",
                   "MODELRADAR_DATA_DIR": td + "/data", "MODELRADAR_REPO_DATA": td + "/browser",
                   "MR_HUB_NOTIFY": "", "MODELRADAR_NO_SUMMARY": "1"}
            code = r'''
import json,sqlite3,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
import fetch_model_data as F

support=Path.home()/"support"
(support).mkdir(parents=True,exist_ok=True)
q=support/"quarantine.json"
q.write_text(json.dumps({"quarantined":[{
 "name":"Qwen3.8","field":"tok_s","from_ts":"0000","to_ts":"9999","reason":"test"}]}))

def html(tps):
    return ('<table><thead><tr><th>Model</th><th>Context Window</th><th>Creator</th>'
            '<th>Artificial Analysis Intelligence Index</th><th>Cost per Task USD</th>'
            '<th>Output Speed (t/s)</th><th>Latency First Chunk (s)</th>'
            '<th>Total Response (s)</th></tr></thead><tbody><tr><td>Qwen3.8</td><td>128k</td><td>Alibaba</td>'
            f'<td>50</td><td>1</td><td>{tps}</td><td>0.1</td><td>1</td></tr></tbody></table>').encode()

def fetched(url,**kw):
    if url==F.SOURCES[F.SRC_AA]: return html(1486)
    if url==F.AA_METHODOLOGY: return b'Artificial Analysis Intelligence Index v5.0'
    raise RuntimeError("source off")

def run_once(tps):
    with patch.object(F,"CFG_PATH",support/"watchlist.json"), \
         patch.object(F,"QUARANTINE_PATH",q), \
         patch.object(F,"QUARANTINE_SEED",support/"none.json"), \
         patch.object(F,"fetch",lambda url,**kw: html(tps) if url==F.SOURCES[F.SRC_AA] else fetched(url,**kw)), \
         patch.object(F,"notify_fresh"),patch.object(F,"maybe_summarize"), \
         patch.object(F,"probe_palette",return_value={}),patch.object(F,"guard_logos"):
        return F.run([F.SRC_AA],False)

# 第一轮建基线（此时也已被隔离，tok_s 不进图/不进比较）
p1=run_once(1486)
m=p1["models"][0]
assert m["tok_s"] is None, ("被隔离的 tok_s 不该出现在本轮模型里", m.get("tok_s"))
# 原始观测必须仍进历史
con=sqlite3.connect(F.DB_PATH)
raw={v for (v,) in con.execute("select value from series where name='Qwen3.8' and field='tok_s'")}
assert 1486.0 in raw, ("历史必须保留原始观测", raw)
con.close()
# 第二轮换成正常值：因为基线里该字段是 None，不该冒出"从无到有"以外的假门槛告警
p2=run_once(190)
assert p2["models"][0]["tok_s"] is None, "隔离区间覆盖到第二轮，应继续剥离"
print("OK")
'''
            p = subprocess.run([sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[1])],
                               env=env, cwd=td, capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
