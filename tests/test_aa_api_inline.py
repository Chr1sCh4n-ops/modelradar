"""2026-11-25：AA 旧 /data/*.txt 与 /api/v2/data/* 都退场，时间改从 V2 API 或榜单页内嵌读数取。"""
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


def run_env(td: str) -> dict:
    return {**os.environ, "HOME": td, "MODELRADAR_SUPPORT": td + "/support",
            "MODELRADAR_DATA_DIR": td + "/data", "MODELRADAR_REPO_DATA": td + "/browser",
            "MR_HUB_NOTIFY": "", "MODELRADAR_NO_SUMMARY": "1"}


def _aa_row(time_s: float, name: str = "GLM-5.3 (max)", suffix: str = "Max") -> dict:
    return {
        "name": name, "aa_slug": "glm-5-3", "creator": "Z AI", "ctx": "128k",
        "index": 31, "apt_usd": 1.0, "tok_s": 100, "ttft_s": 0.1,
        "total_s": None, "first_answer_s": 1.0, "output_tokens_per_task": None,
        "logo": "", "inline_suffix": suffix, "inline_time_per_task_s": time_s,
        "inline_output_tokens_per_task": None,
    }


def inline_code(version: str, time_s: float) -> str:
    return r'''
import json,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
import fetch_model_data as F
cfg=Path.home()/'watchlist.json'
cfg.write_text('{"keyword_include":["glm"]}')
aa_rows=[%s]
def fetched(url,**kw):
    if url==F.SOURCES[F.SRC_AA]: return b'<html></html>'
    if url==F.AA_METHODOLOGY: return b'Artificial Analysis Intelligence Index v%s'
    raise RuntimeError('unexpected '+url)
with patch.object(F,'CFG_PATH',cfg),patch.object(F,'fetch',fetched),\
     patch.object(F,'parse_aa',return_value=aa_rows),\
     patch.object(F,'aa_api_key',return_value=None),\
     patch.object(F,'fetch_task_manifest',side_effect=AssertionError('legacy manifest must not be used')),\
     patch.object(F,'notify_fresh'),patch.object(F,'maybe_summarize'),\
     patch.object(F,'probe_palette',return_value={}):
    p=F.run([F.SRC_AA,F.SRC_AA_TIMES],False)
''' % (repr(_aa_row(time_s)), version)


API_CODE = r'''
import json,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
import fetch_model_data as F
cfg=Path.home()/'watchlist.json'
cfg.write_text('{"keyword_include":["glm"]}')
aa_rows=[%s]
API=%s
def fetched(url,**kw):
    if url==F.SOURCES[F.SRC_AA]: return b'<html></html>'
    if url==F.AA_METHODOLOGY: return b'Artificial Analysis Intelligence Index v5.0.2'
    if url.startswith(F.AA_API_MODELS):
        assert kw.get('http_headers',{}).get('x-api-key')=='test-key',kw
        return API
    raise RuntimeError('unexpected '+url)
with patch.object(F,'CFG_PATH',cfg),patch.object(F,'fetch',fetched),\
     patch.object(F,'parse_aa',return_value=aa_rows),\
     patch.object(F,'aa_api_key',return_value='test-key'),\
     patch.object(F,'fetch_task_manifest',side_effect=AssertionError('legacy manifest must not be used')),\
     patch.object(F,'notify_fresh'),patch.object(F,'maybe_summarize'),\
     patch.object(F,'probe_palette',return_value={}):
    p=F.run([F.SRC_AA,F.SRC_AA_TIMES],False)
''' % (repr(_aa_row(1655.8)), repr(json.dumps({
    "tier": "pro", "intelligence_index_version": 5,
    "pagination": {"page": 1, "page_size": 200, "total_pages": 1, "has_more": False},
    "data": [{"slug": "glm-5-3", "name": "GLM-5.3 (Max)",
              "performance_v3": {"time_per_task_seconds": 1655.8,
                                 "mean_output_tokens_per_second": 62.36}}],
}).encode()))


class InlineTaskTime(unittest.TestCase):
    def test_inline_rows_match_release_and_tier_and_keep_version(self):
        rows = F.inline_task_rows([_aa_row(1655.8)], "5.0.2")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evaluation_version"], "5.0.2")
        models = [{"name": "GLM-5.3 (max)", "aa_slug": "glm-5-3",
                   "evaluation_version": "5.0.2"}]
        self.assertEqual(F.attach_task_times(models, rows, "5.0.2"), 1)
        self.assertEqual(models[0]["time_per_task_s"], 1655.8)
        # 内嵌没有独立 output token 字段时，按 时间×该源速度 补回（_aa_row tok_s=100）。
        self.assertEqual(models[0]["output_tokens_per_task"], 165580.0)

    def test_inline_rows_do_not_cross_versions(self):
        rows = F.inline_task_rows([_aa_row(1655.8)], "5.0.2")
        models = [{"name": "GLM-5.3 (max)", "aa_slug": "glm-5-3",
                   "evaluation_version": "5.0.3"}]
        self.assertEqual(F.attach_task_times(models, rows, "5.0.3"), 0)
        self.assertIsNone(models[0]["time_per_task_s"])

    def test_api_pro_tier_parses_time_per_task(self):
        doc = {"tier": "pro", "intelligence_index_version": 5,
               "pagination": {"has_more": False},
               "data": [{"slug": "glm-5-3", "name": "GLM-5.3 (Max)",
                         "performance_v3": {"time_per_task_seconds": 1655.8,
                                            "mean_output_tokens_per_second": 62.36},
                         "artificial_analysis_intelligence_index_token_counts": {
                             "output_tokens": 135067089}}]}
        calls = []
        def fake_fetch(url, **kw):
            calls.append((url, kw))
            return json.dumps(doc).encode()
        with patch.object(F, "aa_api_key", return_value="k"), \
             patch.object(F, "fetch", side_effect=fake_fetch):
            rows, tier, api_version = F.fetch_aa_api_models("5.0.2")
        self.assertEqual(tier, "pro")
        self.assertEqual(api_version, 5)
        self.assertEqual(rows[0]["time_per_task_s"], 1655.8)
        self.assertEqual(rows[0]["evaluation_version"], "5.0.2")
        self.assertAlmostEqual(rows[0]["output_tokens_per_task"], 1655.8 * 62.36, places=6)
        self.assertEqual(calls[0][1]["http_headers"], {"x-api-key": "k"})

    def test_api_free_tier_is_rejected(self):
        doc = {"tier": "free", "intelligence_index_version": 5,
               "pagination": {"has_more": False},
               "data": [{"slug": "glm-5-3", "name": "GLM-5.3 (Max)",
                         "performance_v3": {"mean_output_tokens_per_second": 62.36}}]}
        with patch.object(F, "aa_api_key", return_value="k"), \
             patch.object(F, "fetch", return_value=json.dumps(doc).encode()):
            with self.assertRaises(RuntimeError) as ctx:
                F.fetch_aa_api_models("5.0.2")
        self.assertIn("不含 time per task", str(ctx.exception))


class RunSources(unittest.TestCase):
    def _run(self, code: str) -> dict:
        with tempfile.TemporaryDirectory() as td:
            p = subprocess.run([sys.executable, "-I", "-c", code + r'''
import json
print(json.dumps({"state":p["data_state"],
                  "src":p["sources"].get(F.SRC_AA_TIMES),
                  "time":p["models"][0].get("time_per_task_s") if p["models"] else None,
                  "note":p.get("data_note","")},ensure_ascii=False))
''', str(ROOT)], env=run_env(td), cwd=td, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            return json.loads(p.stdout.strip().splitlines()[-1])

    def test_no_key_uses_inline_and_does_not_touch_legacy_manifest(self):
        out = self._run(inline_code("5.0.2", 1655.8))
        self.assertEqual(out["state"], "ok")
        self.assertEqual(out["src"]["source"], "inline")
        self.assertTrue(out["src"]["partial"])
        self.assertEqual(out["time"], 1655.8)
        self.assertIn("内嵌", out["src"]["note"])

    def test_pro_api_wins_over_inline(self):
        out = self._run(API_CODE)
        self.assertEqual(out["state"], "ok")
        self.assertEqual(out["src"]["source"], "api")
        self.assertEqual(out["src"]["tier"], "pro")
        self.assertEqual(out["time"], 1655.8)


if __name__ == "__main__":
    unittest.main()
