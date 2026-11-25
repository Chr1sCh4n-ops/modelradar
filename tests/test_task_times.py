"""2026-10-28 用户要求：AA 每任务耗时（原始秒）独立采集、精确匹配、失败不装绿。"""
import gzip
import hashlib
import json
import os
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
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY = bytes(range(32))


def encrypt(obj: dict) -> bytes:
    return AESGCM(KEY).encrypt(hashlib.sha256(KEY).digest()[:12],
                               gzip.compress(json.dumps(obj).encode()), None)


def manifest_text(path: str) -> str:
    return json.dumps({"manifest": {"path": path, "key": KEY.hex()}},
                      separators=(",", ":"))


def rsc_page(*texts: str) -> str:
    return "".join(f"<script>self.__next_f.push({json.dumps([1, text])})</script>"
                   for text in texts)


def payload(*rows: dict) -> bytes:
    return encrypt({"models": list(rows)})


class TaskTimeManifest(unittest.TestCase):
    def test_encrypted_manifest_keeps_raw_seconds_and_skips_wrong_package(self):
        correct = payload(
            {"slug": "terra-low", "name": "Terra (Low)", "intelligenceIndex": 29,
             "intelligenceIndexTimePerTask": 656.65,
             "intelligenceIndexOutputTokensPerTask": {"reasoning": 100, "answer": 200, "output": 300}},
            {"slug": "terra-high", "name": "Terra (High)", "intelligenceIndex": 40,
             "intelligenceIndexTimePerTask": None,
             "intelligenceIndexOutputTokensPerTask": {"output": 0}},
        )
        wrong = payload({"slug": "wrong", "name": "Wrong", "intelligenceIndex": 1})
        raws = {'/data/wrong.txt': wrong, '/data/task.txt': correct}
        page = rsc_page(manifest_text('/data/wrong.txt'), manifest_text('/data/task.txt'))

        def fetched(url):
            return raws[url.rsplit("artificialanalysis.ai", 1)[1]]

        with patch.object(F, "fetch_task_manifest", side_effect=fetched):
            rows = F.parse_task_times(page)

        self.assertEqual([r["slug"] for r in rows], ["terra-low", "terra-high"])
        # 官方字段就是秒：656.65 不能乘 60，也不能被换算成分钟。
        self.assertEqual(rows[0]["time_per_task_s"], 656.65)
        self.assertEqual(rows[0]["output_tokens_per_task"], 300)
        self.assertIsNone(rows[1]["time_per_task_s"])
        self.assertIsNone(rows[1]["output_tokens_per_task"])

        with patch.object(F, "fetch_task_manifest", return_value=wrong):
            with self.assertRaises(ValueError):
                F.parse_task_times(rsc_page(manifest_text('/data/wrong.txt')))


class AttachTaskTime(unittest.TestCase):
    def test_exact_unique_slug_and_units(self):
        models = [
            {"name": "Terra (Low)", "aa_slug": "terra-low", "evaluation_version": "5.0"},
            {"name": "Same (Low)", "aa_slug": "same-low", "evaluation_version": "5.0"},
            {"name": "No Slug", "aa_slug": None, "evaluation_version": "5.0"},
        ]
        rows = [
            {"slug": "terra-low", "name": "Terra (Low)", "time_per_task_s": 656.65,
             "evaluation_version": "5.0"},
            # 同一 slug 出现两次就不唯一：宁可留空，也不猜哪条才对。
            {"slug": "same-low", "name": "Same (Low)", "time_per_task_s": 10.0,
             "evaluation_version": "5.0"},
            {"slug": "same-low", "name": "Same (Low)", "time_per_task_s": 20.0,
             "evaluation_version": "5.0"},
            # 同名不同 slug 不参与匹配。
            {"slug": "same-max", "name": "Same (Low)", "time_per_task_s": 30.0,
             "evaluation_version": "5.0"},
        ]
        self.assertEqual(F.attach_task_times(models, rows, "5.0"), 1)
        self.assertEqual(models[0]["time_per_task_s"], 656.65)
        self.assertIsNone(models[1]["time_per_task_s"])
        self.assertIsNone(models[2]["time_per_task_s"])

    def test_tiers_do_not_share_the_default_tier_time(self):
        """2026-11-15：新版榜单同一 release 多档共用一个 aa_slug，必须按档位区分。"""
        models = [
            {"name": "Fable (max)", "aa_slug": "fable", "evaluation_version": "5.0"},
            {"name": "Fable (xhigh)", "aa_slug": "fable", "evaluation_version": "5.0"},
            {"name": "Fable (low)", "aa_slug": "fable", "evaluation_version": "5.0"},
        ]
        rows = [
            {"slug": "fable", "name": "Fable (Max)", "release_slug": "fable",
             "suffix": "Max, Default Fallback", "time_per_task_s": 100,
             "evaluation_version": "5.0"},
            {"slug": "fable-xhigh", "name": "Fable (Xhigh)", "release_slug": "fable",
             "suffix": "Xhigh", "time_per_task_s": 50, "evaluation_version": "5.0"},
        ]
        self.assertEqual(F.attach_task_times(models, rows, "5.0"), 2)
        self.assertEqual(models[0]["time_per_task_s"], 100)
        self.assertEqual(models[1]["time_per_task_s"], 50)
        self.assertIsNone(models[2]["time_per_task_s"], "缺档位时不能拿默认档顶上")

    def test_untiered_model_matches_the_default_release_row(self):
        """Mistral Small 4 这类没有档位后缀的行，默认档就是 slug 等于 release 的那条。"""
        models = [{"name": "Mistral Small 4", "aa_slug": "mistral-small-4",
                   "evaluation_version": "5.0"}]
        rows = [
            {"slug": "mistral-small-4", "release_slug": "mistral-small-4",
             "suffix": "Reasoning", "time_per_task_s": 101.5,
             "evaluation_version": "5.0"},
            {"slug": "mistral-small-4-non-reasoning", "release_slug": "mistral-small-4",
             "suffix": "Non-reasoning", "time_per_task_s": None,
             "evaluation_version": "5.0"},
        ]
        self.assertEqual(F.attach_task_times(models, rows, "5.0"), 1)
        self.assertEqual(models[0]["time_per_task_s"], 101.5)

    def test_output_tokens_attach_even_when_time_missing(self):
        models = [{"name": "A", "aa_slug": "a", "evaluation_version": "5.0"}]
        rows = [{"slug": "a", "time_per_task_s": None,
                 "output_tokens_per_task": 1234, "evaluation_version": "5.0"}]
        self.assertEqual(F.attach_task_times(models, rows, "5.0"), 0)
        self.assertEqual(models[0]["output_tokens_per_task"], 1234)
        self.assertIsNone(models[0]["time_per_task_s"])

        # 陈旧轮次不能把上一轮的输出量留在模型上
        self.assertEqual(F.attach_task_times(models, [], "5.0"), 0)
        self.assertIsNone(models[0]["output_tokens_per_task"])

    def test_invalid_values_stay_null(self):
        for bad in (None, True, False, -1, 0, float("nan"), float("inf"),
                    float("-inf"), 10 ** 500, F.TASK_TIME_MAX_SECONDS, "123"):
            models = [{"name": "A", "aa_slug": "a", "evaluation_version": "5.0"}]
            rows = [{"slug": "a", "time_per_task_s": bad, "evaluation_version": "5.0"}]
            self.assertEqual(F.attach_task_times(models, rows, "5.0"), 0, bad)
            self.assertIsNone(models[0]["time_per_task_s"], bad)
        models = [{"name": "A", "aa_slug": "a", "evaluation_version": "5.0"}]
        rows = [{"slug": "a", "time_per_task_s": 0.5, "evaluation_version": "5.0"}]
        self.assertEqual(F.attach_task_times(models, rows, "5.0"), 1)
        self.assertEqual(models[0]["time_per_task_s"], 0.5)

    def test_mixed_versions_and_carried_rows_stay_null(self):
        models = [
            {"name": "A", "aa_slug": "a", "evaluation_version": "5.0"},
            {"name": "B", "aa_slug": "b", "evaluation_version": "4.3.2"},
            {"name": "C", "aa_slug": "c", "evaluation_version": "5.0",
             "carried_asof": "2026-10-01"},
        ]
        rows = [{"slug": s, "time_per_task_s": 10, "evaluation_version": "5.0"}
                for s in ("a", "b", "c")]
        self.assertEqual(F.attach_task_times(models, rows, "5.0"), 1)
        self.assertEqual(models[0]["time_per_task_s"], 10)
        self.assertIsNone(models[1]["time_per_task_s"])
        self.assertIsNone(models[2]["time_per_task_s"])

        models = [{"name": "A", "aa_slug": "a", "evaluation_version": "5.0"}]
        rows = [{"slug": "a", "time_per_task_s": 10, "evaluation_version": "4.3.2"}]
        self.assertEqual(F.attach_task_times(models, rows, "5.0"), 0)
        self.assertIsNone(models[0]["time_per_task_s"])

        # 陈旧快照 / 本轮没有可用行时，旧值不能留在模型上。
        models = [{"name": "A", "aa_slug": "a", "evaluation_version": "5.0",
                   "time_per_task_s": 99}]
        self.assertEqual(F.attach_task_times(models, [], "5.0"), 0)
        self.assertIsNone(models[0]["time_per_task_s"])


class DeriveTaskTime(unittest.TestCase):
    def test_same_hierarchy_as_tps(self):
        models = [
            # 实测速度：输出量 ÷ 实测
            {"output_tokens_per_task": 300, "tok_s": 100},
            # 同族插值/锚点：输出量 ÷ 点估计，区间按速度区间反比换算
            {"output_tokens_per_task": 300, "tok_s": None, "tok_s_est": 50,
             "tok_s_est_basis": "series-interp", "tok_s_est_range": [40, 60]},
            # 只有同级参照带：只给时间带，不给中心值
            {"output_tokens_per_task": 300, "tok_s": None, "tok_s_band": [25, 50]},
            # 没有输出量：什么都不派生
            {"output_tokens_per_task": None, "tok_s": 100},
            # AA 已给官方值：永不覆盖
            {"output_tokens_per_task": 300, "tok_s": 100, "time_per_task_s": 9.0},
        ]
        done, banded = F.derive_missing_task_times(models)
        self.assertEqual((done, banded), (2, 1))
        self.assertEqual(models[0]["time_per_task_est_s"], 3.0)
        self.assertTrue(models[0]["time_per_task_est"])
        self.assertEqual(models[0]["time_per_task_est_basis"], "measured-speed")
        self.assertEqual(models[1]["time_per_task_est_s"], 6.0)
        self.assertEqual(models[1]["time_per_task_est_basis"], "series-interp")
        self.assertEqual(models[1]["time_per_task_est_range"], [5.0, 7.5])
        self.assertIsNone(models[2].get("time_per_task_est_s"))
        self.assertEqual(models[2]["time_per_task_band"], [6.0, 12.0])
        self.assertIsNone(models[3].get("time_per_task_est_s"))
        self.assertEqual(models[4]["time_per_task_s"], 9.0)
        self.assertNotIn("time_per_task_est", models[4])

    def test_invalid_speed_or_tokens_stay_empty(self):
        models = [
            {"output_tokens_per_task": 300, "tok_s": 0, "tok_s_est": -1},
            {"output_tokens_per_task": 0, "tok_s": 100},
            {"output_tokens_per_task": True, "tok_s": 100},
        ]
        self.assertEqual(F.derive_missing_task_times(models), (0, 0))
        self.assertTrue(all(m.get("time_per_task_est_s") is None for m in models))


def run_env(td: str) -> dict:
    return {**os.environ, "HOME": td, "MODELRADAR_SUPPORT": td + "/support",
            "MODELRADAR_DATA_DIR": td + "/data", "MODELRADAR_REPO_DATA": td + "/browser",
            "MR_HUB_NOTIFY": "", "MODELRADAR_NO_SUMMARY": "1"}


FIXTURE = r'''
import gzip,hashlib,json
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
KEY=bytes(range(32))
def manifest_text(path):
    return json.dumps({"manifest":{"path":path,"key":KEY.hex()}},separators=(",",":"))
def task_page(rows):
    raw=AESGCM(KEY).encrypt(hashlib.sha256(KEY).digest()[:12],gzip.compress(json.dumps({"models":rows}).encode()),None)
    text=manifest_text('/data/task.txt')
    page='<script>self.__next_f.push('+json.dumps([1,text])+')</script>'
    return page.encode(),{'/data/task.txt':raw}
'''

SUCCESS_CODE = FIXTURE + r'''
import sqlite3,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
import fetch_model_data as F
html=b'<table><thead><tr><th>Model</th><th>Context Window</th><th>Creator</th><th>Artificial Analysis Intelligence Index</th><th>Cost per Task USD</th><th>Output Speed (t/s)</th><th>Latency First Chunk (s)</th><th>Total Response (s)</th></tr></thead><tbody><tr><td><a href="/models/terra-low">Terra (low)</a></td><td>128k</td><td>OpenAI</td><td>29</td><td>1</td><td>100</td><td>0.1</td><td>1</td></tr></tbody></table>'
cfg=Path.home()/'watchlist.json'
cfg.write_text('{"keyword_include":["terra"]}')
page,task_raws=task_page([{"slug":"terra-low","name":"Terra (Low)","intelligenceIndex":29,"intelligenceIndexTimePerTask":656.65}])
def fetched(url,**kw):
    if url==F.SOURCES[F.SRC_AA]:return html
    if url==F.AA_METHODOLOGY:return b'Artificial Analysis Intelligence Index v5.0'
    raise RuntimeError('unexpected '+url)
def task_fetch(url):
    if url==F.SOURCES[F.SRC_AA_TIMES]:return page
    return task_raws[url.rsplit('artificialanalysis.ai',1)[1]]
with patch.object(F,'CFG_PATH',cfg),patch.object(F,'fetch',fetched),patch.object(F,'fetch_task_manifest',task_fetch),patch.object(F,'notify_fresh'),patch.object(F,'maybe_summarize'),patch.object(F,'probe_palette',return_value={}):
    p=F.run([F.SRC_AA,F.SRC_AA_TIMES],False)
assert p['data_state']=='ok',p
assert p['sources'][F.SRC_AA_TIMES]['ok'] is True,p['sources'][F.SRC_AA_TIMES]
assert p['sources'][F.SRC_AA_TIMES]['matched']==1,p['sources'][F.SRC_AA_TIMES]
assert p['models'][0]['time_per_task_s']==656.65,p['models'][0]
assert 'time_per_task_s' in F.DIFF_FIELDS
assert not p['moves'],p['moves']  # 首次建基线没有可比变化。
assert set(p['metric_views'])=={'tok_s','time_per_task_s'}
assert 'time_per_task_s' in F.HISTORY_FIELDS
con=sqlite3.connect(F.DB_PATH)
v,e=con.execute("SELECT value,evaluation_version FROM series WHERE field='time_per_task_s'").fetchone()
assert (v,e)==(656.65,'5.0'),(v,e)
raw=json.loads(con.execute("SELECT payload FROM snapshots WHERE source=? AND ok=1 ORDER BY ts DESC LIMIT 1",(F.SRC_AA_TIMES,)).fetchone()[0])
assert raw[0]['time_per_task_s']==656.65 and raw[0]['evaluation_version']=='5.0',raw
con.close()
'''

FAILURE_CODE = FIXTURE + r'''
import sqlite3,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
import fetch_model_data as F
html=b'<table><thead><tr><th>Model</th><th>Context Window</th><th>Creator</th><th>Artificial Analysis Intelligence Index</th><th>Cost per Task USD</th><th>Output Speed (t/s)</th><th>Latency First Chunk (s)</th><th>Total Response (s)</th></tr></thead><tbody><tr><td><a href="/models/terra-low">Terra (low)</a></td><td>128k</td><td>OpenAI</td><td>29</td><td>1</td><td>100</td><td>0.1</td><td>1</td></tr></tbody></table>'
cfg=Path.home()/'watchlist.json'
cfg.write_text('{"keyword_include":["terra"]}')
page,task_raws=task_page([{"slug":"other","name":"Other","intelligenceIndex":29,"intelligenceIndexTimePerTask":99}])
def fetched(url,**kw):
    if url==F.SOURCES[F.SRC_AA]:return html
    if url==F.AA_METHODOLOGY:return b'Artificial Analysis Intelligence Index v5.0'
    raise RuntimeError('unexpected '+url)
def task_fetch(url):
    if url==F.SOURCES[F.SRC_AA_TIMES]:return page
    return task_raws[url.rsplit('artificialanalysis.ai',1)[1]]
with patch.object(F,'CFG_PATH',cfg),patch.object(F,'fetch',fetched),patch.object(F,'fetch_task_manifest',task_fetch),patch.object(F,'notify_fresh'),patch.object(F,'maybe_summarize'),patch.object(F,'probe_palette',return_value={}):
    p=F.run([F.SRC_AA,F.SRC_AA_TIMES],False)
assert p['sources'][F.SRC_AA_TIMES]['ok'] is False,p['sources']
assert p['sources'][F.SRC_AA_TIMES]['matched']==0,p['sources']
assert p['data_state']=='degraded',p['data_state']
assert p['models'] and all(m['time_per_task_s'] is None for m in p['models'])
with patch.object(F,'CFG_PATH',cfg),patch.object(F,'fetch',fetched),patch.object(F,'fetch_task_manifest',side_effect=RuntimeError('offline')),patch.object(F,'notify_fresh'),patch.object(F,'maybe_summarize'),patch.object(F,'probe_palette',return_value={}):
    p=F.run([F.SRC_AA,F.SRC_AA_TIMES],False)
assert p['sources'][F.SRC_AA_TIMES]['ok'] is False,p['sources']
assert p['data_state']=='degraded',p['data_state']
con=sqlite3.connect(F.DB_PATH)
assert con.execute("SELECT COUNT(*) FROM series WHERE field='time_per_task_s'").fetchone()[0]==0
assert con.execute("SELECT COUNT(*) FROM snapshots WHERE source=? AND ok=1",(F.SRC_AA_TIMES,)).fetchone()[0]==1
con.close()
'''


class TaskTimeRun(unittest.TestCase):
    def test_missing_published_page_recovers_task_time_from_ledger(self):
        code = SUCCESS_CODE + r'''
F.LATEST_JSON.unlink()
con=F.ensure_db()
F.save_snapshot(con,F.SRC_PROGRESS,1,True,None,[{'name':'Terra (low)','slug':'terra-low','score':316.64,'evaluation_version':'1'}])
con.close()
with patch.object(F,'CFG_PATH',cfg),patch.object(F,'fetch',side_effect=RuntimeError('offline')),patch.object(F,'fetch_task_manifest',side_effect=RuntimeError('offline')),patch.object(F,'notify_fresh'),patch.object(F,'maybe_summarize'),patch.object(F,'probe_palette',return_value={}):
    p=F.run([F.SRC_AA,F.SRC_AA_TIMES,F.SRC_PROGRESS],False)
assert p['data_state']=='stale',p
assert p['models'][0]['time_per_task_s']==656.65,p['models']
assert p['generated_at']==p['sources'][F.SRC_AA]['from']
assert p['sources'][F.SRC_AA_TIMES]['stale'] is True
assert p['models'][0]['progress_index']==316.64,p['models']
assert p['models'][0]['progress_version']=='1'
assert p['sources'][F.SRC_PROGRESS]['stale'] is True
'''
        with tempfile.TemporaryDirectory() as td:
            p = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)],
                               env=run_env(td), cwd=td, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_failed_round_preserves_page_time_and_task_bubbles(self):
        code = SUCCESS_CODE + r'''
saved=json.loads(F.LATEST_JSON.read_text())
con=sqlite3.connect(F.DB_PATH)
before=con.execute('SELECT COUNT(*) FROM series').fetchone()[0]
con.close()
for _ in range(2):
    with patch.object(F,'CFG_PATH',cfg),patch.object(F,'fetch',side_effect=RuntimeError('offline')),patch.object(F,'fetch_task_manifest',side_effect=RuntimeError('offline')),patch.object(F,'notify_fresh'),patch.object(F,'maybe_summarize'),patch.object(F,'probe_palette',return_value={}):
        p=F.run([F.SRC_AA,F.SRC_AA_TIMES],False)
    assert p['data_state']=='stale',p
    assert p['models']==saved['models']
    assert p['generated_at']==saved['generated_at']
    assert p['models'][0]['time_per_task_s']==656.65
    assert p['sources'][F.SRC_AA]['stale'] is True
    assert p['alert']['tone']=='warn'
    assert 'offline' in p['data_note']
    con=sqlite3.connect(F.DB_PATH)
    assert con.execute('SELECT COUNT(*) FROM series').fetchone()[0]==before
    con.close()
'''
        with tempfile.TemporaryDirectory() as td:
            p = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)],
                               env=run_env(td), cwd=td, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_task_source_failure_reuses_old_values_without_new_time_history(self):
        code = SUCCESS_CODE + r'''
con=sqlite3.connect(F.DB_PATH)
before=con.execute("SELECT COUNT(*) FROM series WHERE field='time_per_task_s'").fetchone()[0]
con.close()
with patch.object(F,'CFG_PATH',cfg),patch.object(F,'fetch',fetched),patch.object(F,'fetch_task_manifest',side_effect=RuntimeError('offline')),patch.object(F,'notify_fresh'),patch.object(F,'maybe_summarize'),patch.object(F,'probe_palette',return_value={}):
    p=F.run([F.SRC_AA,F.SRC_AA_TIMES],False)
assert p['data_state']=='degraded',p
assert p['models'][0]['time_per_task_s']==656.65,p['models']
assert p['sources'][F.SRC_AA_TIMES]['ok'] is False
assert p['sources'][F.SRC_AA_TIMES]['stale'] is True
con=sqlite3.connect(F.DB_PATH)
assert con.execute("SELECT COUNT(*) FROM series WHERE field='time_per_task_s'").fetchone()[0]==before
con.close()
'''
        with tempfile.TemporaryDirectory() as td:
            p = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)],
                               env=run_env(td), cwd=td, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_run_success_records_time_snapshot_and_series(self):
        with tempfile.TemporaryDirectory() as td:
            p = subprocess.run([sys.executable, "-I", "-c", SUCCESS_CODE, str(ROOT)],
                               env=run_env(td), cwd=td, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_zero_match_and_fetch_failure_do_not_go_green(self):
        with tempfile.TemporaryDirectory() as td:
            p = subprocess.run([sys.executable, "-I", "-c", FAILURE_CODE, str(ROOT)],
                               env=run_env(td), cwd=td, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
