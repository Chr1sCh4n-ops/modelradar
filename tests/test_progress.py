"""2026-10-28 用户要求：真实 Progress 数据认证与精确档位匹配。"""
import gzip
import hashlib
import json
import sys
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_model_data as F
os.environ.setdefault("MODELRADAR_NO_HEADLESS", "1")  # 测试不启动 Chrome/钥匙串
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class ProgressData(unittest.TestCase):
    def test_progress_failure_and_stale_do_not_forge_history(self):
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
html=b'<table><thead><tr><th>Model</th><th>Context Window</th><th>Creator</th><th>Artificial Analysis Intelligence Index</th><th>Cost per Task USD</th><th>Output Speed (t/s)</th><th>Latency First Chunk (s)</th><th>Total Response (s)</th></tr></thead><tbody><tr><td>Qwen3.8</td><td>128k</td><td>Alibaba</td><td>50</td><td>1</td><td>100</td><td>0.1</td><td>1</td></tr></tbody></table>'
cfg=Path.home()/'watchlist.json'
cfg.write_text('{"keyword_include":["qwen"]}')
def fetched(url,**kw):
    if url==F.SOURCES[F.SRC_AA]:return html
    if url==F.AA_METHODOLOGY:return b'Artificial Analysis Intelligence Index v5.0'
    raise RuntimeError('Progress unavailable')
with patch.object(F,'CFG_PATH',cfg),patch.object(F,'fetch',fetched),patch.object(F,'notify_fresh'),patch.object(F,'maybe_summarize'),patch.object(F,'probe_palette',return_value={}):
    p=F.run([F.SRC_AA,F.SRC_PROGRESS],False)
    assert p['data_state']=='ok',p
    assert p['sources'][F.SRC_PROGRESS]['ok'] is True,p['sources']
    assert p['sources'][F.SRC_PROGRESS]['baseline_matched']==0
    assert p['sources'][F.SRC_PROGRESS]['estimated']==1
    assert p['models'] and all(m['progress_index'] is not None for m in p['models'])
    assert p['models'][0]['progress_estimated'] is True
    assert p['models'][0]['progress_source']=='calibrated'
    with patch.object(F,'fetch',side_effect=lambda url,**kw: b'ignored' if url==F.SOURCES[F.SRC_PROGRESS] else fetched(url,**kw)),patch.object(F,'parse_progress',return_value=([{'name':'Unknown','slug':'unknown','score':300}],'1')):
        unmatched=F.run([F.SRC_AA,F.SRC_PROGRESS],False)
    assert unmatched['sources'][F.SRC_PROGRESS]['ok'] is True,unmatched['sources']
    assert unmatched['sources'][F.SRC_PROGRESS]['official_matched']==0
    assert unmatched['sources'][F.SRC_PROGRESS]['estimated']==1
    assert unmatched['data_state']=='ok'
    con=sqlite3.connect(F.DB_PATH)
    assert con.execute("SELECT value FROM series WHERE field='total_s'").fetchone()[0]==1
    before=(con.execute('SELECT COUNT(*) FROM series').fetchone()[0],con.execute("SELECT COUNT(*) FROM snapshots WHERE source='diffbase'").fetchone()[0])
    con.close()
    with patch.object(F,'fetch',side_effect=RuntimeError('offline')):
        p=F.run([F.SRC_AA,F.SRC_PROGRESS],False)
    assert p['data_state']=='stale',p['data_state']
    con=sqlite3.connect(F.DB_PATH)
    after=(con.execute('SELECT COUNT(*) FROM series').fetchone()[0],con.execute("SELECT COUNT(*) FROM snapshots WHERE source='diffbase'").fetchone()[0])
    con.close()
    assert before==after,(before,after)
'''
            p = subprocess.run([sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[1])],
                               env=env, cwd=td, capture_output=True, text=True, timeout=20)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_authenticated_manifest_and_version(self):
        key = bytes(range(32))
        rows = [{"name": "Terra (Low)", "slug": "terra-low", "score": 316.64,
                 "release": {"id": "terra"}, "creator": {"id": "openai"}, "intelligenceIndex": 29}]
        raw = AESGCM(key).encrypt(hashlib.sha256(key).digest()[:12],
                                  gzip.compress(json.dumps({"models": rows}).encode()), None)
        text = 'Progress Index v1 ' + json.dumps({"manifest": {"path": "/data/abc123.txt", "key": key.hex()}}, separators=(",", ":"))
        page = '<script>self.__next_f.push(' + json.dumps([1, text]) + ')</script>'
        with patch.object(F, "fetch", return_value=raw):
            parsed, version = F.parse_progress(page)
        self.assertEqual((len(parsed), version, parsed[0]["score"]), (1, "1", 316.64))
        other_text = json.dumps({"manifest": {"path": "/data/other.txt", "key": key.hex()}}, separators=(",", ":"))
        other_page = '<script>self.__next_f.push(' + json.dumps([1, other_text]) + ')</script>' + page
        with patch.object(F, "fetch", side_effect=[RuntimeError("unrelated failed"), raw]):
            self.assertEqual(F.parse_progress(other_page)[0][0]["score"], 316.64)
        wrong = AESGCM(key).encrypt(hashlib.sha256(key).digest()[:12],
                                    gzip.compress(b'{"models":[{"name":"Wrong","slug":"wrong","score":999}]}'), None)
        with patch.object(F, "fetch", side_effect=[wrong, raw]):
            self.assertEqual(F.parse_progress(other_page)[0][0]["score"], 316.64)
        with patch.object(F, "fetch", return_value=raw[:-1] + bytes([raw[-1] ^ 1])):
            with self.assertRaises(Exception):
                F.parse_progress(page)
        empty = AESGCM(key).encrypt(hashlib.sha256(key).digest()[:12],
                                    gzip.compress(b'{"models":[]}'), None)
        with patch.object(F, "fetch", return_value=empty):
            with self.assertRaises(ValueError):
                F.parse_progress(page)

    def test_duplicate_manifest_pointers_collapse(self):
        """同一页把同一个包挂两次不算歧义；两个不同的包才算。"""
        key = bytes(range(32))
        rows = [{"name": "Terra (Low)", "slug": "terra-low", "score": 316.64,
                 "release": {"id": "terra"}, "creator": {"id": "openai"}, "intelligenceIndex": 29}]
        raw = AESGCM(key).encrypt(hashlib.sha256(key).digest()[:12],
                                  gzip.compress(json.dumps({"models": rows}).encode()), None)
        one = {"manifest": {"path": "/data/abc123.txt", "key": key.hex()}}
        text = 'Progress Index v1 ' + json.dumps(one, separators=(",", ":")) + json.dumps(one, separators=(",", ":"))
        page = '<script>self.__next_f.push(' + json.dumps([1, text]) + ')</script>'
        with patch.object(F, "fetch", return_value=raw):
            parsed, version = F.parse_progress(page)
        self.assertEqual((len(parsed), version), (1, "1"))

    def test_exact_variant_not_family_best(self):
        models = [{"name": "Terra (low)", "aa_slug": "terra-low"},
                  {"name": "Terra (non-reasoning)", "aa_slug": "terra-non-reasoning"},
                  {"name": "Terra (high)", "aa_slug": "terra-high"}]
        rows = [{"name": "Terra (Low)", "slug": "terra-low", "score": 316.64},
                {"name": "Terra (Non-reasoning)", "slug": "terra-non-reasoning", "score": 318.24}]
        self.assertEqual(F.attach_progress(models, rows, "1"), 2)
        self.assertEqual([m["progress_index"] for m in models], [316.64, 318.24, None])
        self.assertEqual(F.attach_progress(models, rows * 2, "1"), 0)

    def test_release_scoped_slug_matches_by_tier_without_broadcasting(self):
        """2026-11-15：aa_slug 变成每个 release 一个，五档共用一个 slug 时按档位定位。"""
        models = [{"name": f"Fable 5.2 ({t})", "aa_slug": "fable-5-2"}
                  for t in ("max", "high", "xhigh", "medium", "low")]
        rows = [
            {"slug": "fable-5-2", "name": "Fable 5.2 (Max, Default Fallback)",
             "release": {"slug": "fable-5-2"}, "effort": {"slug": "max"}, "score": 357.95},
            {"slug": "fable-5-2-high", "name": "Fable 5.2 (High, Default Fallback)",
             "release": {"slug": "fable-5-2"}, "effort": {"slug": "high"}, "score": 340.5},
            {"slug": "fable-5-2-low", "name": "Fable 5.2 (Low, Default Fallback)",
             "release": {"slug": "fable-5-2"}, "effort": {"slug": "low"}, "score": None},
        ]
        self.assertEqual(F.attach_progress(models, rows, "1"), 2)
        self.assertEqual([m["progress_index"] for m in models],
                         [357.95, 340.5, None, None, None])

    def test_tier_falls_back_to_name_when_effort_is_absent(self):
        """快照行没有 effort/suffix 时档位从名字括号取；缺档的行不能拿别档顶上。"""
        models = [{"name": "Terra (high)", "aa_slug": "terra"},
                  {"name": "Terra (low)", "aa_slug": "terra"}]
        rows = [{"slug": "terra", "name": "Terra (High)",
                 "release": {"slug": "terra"}, "score": 40.0}]
        self.assertEqual(F.attach_progress(models, rows, "1"), 1)
        self.assertEqual([m["progress_index"] for m in models], [40.0, None])

    def test_slug_is_authoritative_and_bad_scores_stay_missing(self):
        models = [{"name": "Same (Low)", "aa_slug": "same-low"}]
        rows = [{"name": "Same (Low)", "slug": "same-max", "score": 350}]
        self.assertEqual(F.attach_progress(models, rows, "1"), 0)
        for score in (None, True, float("nan"), float("inf"), 10**500):
            rows = [{"name": "Same (Low)", "slug": "same-low", "score": score}]
            self.assertEqual(F.attach_progress(models, rows, "1"), 0)
            self.assertIsNone(models[0]["progress_index"])

    def test_baseline_and_calibration_cover_new_variants(self):
        rows, model = F.load_progress_artifacts()
        self.assertGreater(len(rows), 700)
        self.assertEqual(model["model"], "quadratic")
        self.assertGreaterEqual(sum(1 for r in rows if r.get("anchor")), 28)
        known = {"name": "Known (high)", "release_slug": "known", "suffix": "high",
                 "score": 300, "evaluation_version": "v1", "anchor": True, "baseline": True}
        models = [
            {"name": "Known (high)", "aa_slug": "known", "index": 20},
            {"name": "Brand New Model (high)", "aa_slug": "brand-new-model", "index": 80},
        ]
        self.assertEqual(F.attach_progress(models, [known], "v1"), 1)
        self.assertEqual(models[0]["progress_index"], 300)
        self.assertEqual(F.calibrate_progress(models, model), 1)
        new = models[1]
        self.assertIsNotNone(new["progress_index"])
        self.assertTrue(new["progress_estimated"])
        self.assertEqual(new["progress_source"], "calibrated")
        self.assertGreaterEqual(new["progress_index"], 0)
        self.assertLessEqual(new["progress_index"], 400)

    def test_aa_link_preserves_variant_slug(self):
        page = ('<table><thead><tr><th>Model</th><th>Context Window</th><th>Creator</th>'
                '<th>Artificial Analysis Intelligence Index</th><th>Cost per Task USD</th>'
                '<th>Output Speed (t/s)</th><th>Latency First Chunk (s)</th>'
                '<th>Total Response (s)</th></tr></thead><tbody><tr>'
                '<td><a href="/models/gpt-6-1-terra-low">Terra (low)</a></td>'
                '<td>1M</td><td>OpenAI '
                '<a href="/models/gpt-6-1-terra-low/providers">Providers</a></td>'
                '<td>29</td><td>$0.07</td><td>91</td><td>1</td><td>2</td>'
                '</tr></tbody></table>')
        self.assertEqual(F.parse_aa(page)[0]["aa_slug"], "gpt-6-1-terra-low")


if __name__ == "__main__":
    unittest.main()
