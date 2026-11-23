"""公开站的历史靠发布产物自己续链，不靠 runner 本地台账。

每轮都换一台 runner，`.state` 不保留，所以上一版 `latest.json` 就是唯一可复算的状态。
用例锁定：上一版有、本轮没有的行要接回来；链条上的 `carried_asof` 要原样保留；
跨评测版本、超出窗口、with fallback 重复档都不接；上一版本轮不完整时整份不用。
"""
import unittest
import json
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

import fetch_model_data as F
import render_scatter_svg as R

V = "5.0.1"
NOW = "2026-11-23 09:00:00"


def _model(name, index=30, apt=5.0, tok=100.0, series="Acme Sol 5.1", tier="high",
           carried=None, ver=V):
    return {"name": name, "index": index, "apt_usd": apt, "tok_s": tok,
            "creator": "Acme", "evaluation_version": ver, "series": series,
            "series_key": series.lower().replace(".", "").replace(" ", ""),
            "tier": tier, **({"carried_asof": carried} if carried else {})}


def _previous(models, generated="2026-11-22 18:00:00", state="ok"):
    return {"data_state": state, "generated_at": generated,
            "evaluation_version": V, "models": models}


class CarryFromPayload(unittest.TestCase):
    def test_missing_row_comes_back_with_previous_time(self):
        models = [_model("Acme Sol 5.5 (high)")]
        n = F.carry_from_payload(models, _previous([_model("Acme Sol 5.1 (high)")]), NOW)
        self.assertEqual(n, 1)
        self.assertEqual(models[-1]["name"], "Acme Sol 5.1 (high)")
        self.assertEqual(models[-1]["carried_asof"], "2026-11-22 18:00:00")

    def test_chained_carry_keeps_original_date(self):
        models = [_model("Acme Sol 5.5 (high)")]
        previous = _previous([_model("Acme Sol 5.1 (high)", carried="2026-11-17 18:24:43")],
                             generated="2026-11-22 18:00:00")
        self.assertEqual(F.carry_from_payload(models, previous, NOW), 1)
        self.assertEqual(models[-1]["carried_asof"], "2026-11-17 18:24:43")

    def test_version_change_and_window_are_not_carried(self):
        models = [_model("Acme Sol 5.5 (high)")]
        self.assertEqual(F.carry_from_payload(
            models, _previous([_model("Old (high)", ver="4.9.0")]), NOW), 0)
        self.assertEqual(F.carry_from_payload(
            models, _previous([_model("Ancient (high)", carried="2026-09-01 08:00:00")],
                              generated="2026-09-01 08:00:00"), NOW), 0)

    def test_fallback_duplicate_and_present_rows_are_not_added(self):
        models = [_model("Acme Sol 5.5 (high)")]
        previous = _previous([_model("Acme Sol 5.5 (high)"),
                              _model("Acme Sol 5.1 (high with fallback)")])
        self.assertEqual(F.carry_from_payload(models, previous, NOW), 0)
        self.assertEqual([m["name"] for m in models], ["Acme Sol 5.5 (high)"])

    def test_unusable_previous_payload_is_ignored(self):
        models = [_model("Acme Sol 5.5 (high)")]
        for previous in (None, {}, _previous([_model("Acme Sol 5.1 (high)")], state="stale"),
                         {"data_state": "ok", "generated_at": "x", "models": "nope"}):
            self.assertEqual(F.carry_from_payload(models, previous, NOW), 0)
        self.assertEqual(len(models), 1)

    def test_deployed_snapshot_to_shadow_chart(self):
        old = [_model(f"Acme Sol 5.1 ({tier})", score, cost, tps, tier=tier)
               for tier, score, cost, tps in [("low", 20, 2, 50), ("medium", 40, 4, 80)]]
        models = [_model(f"Acme Sol 5.5 ({tier})", score, cost, tps, "Acme Sol 5.5", tier)
                  for tier, score, cost, tps in [("low", 23, 3, 60), ("medium", 49, 6, 100)]]
        models.append(_model("Acme Terra 5 (xhigh)", 38, 5, 65, "Acme Terra 5", "xhigh"))
        self.assertEqual(F.carry_from_payload(models, _previous(old), NOW), 2)
        bands = F.tier_ladder(models)
        F.annotate_efficiency(models, bands)
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "latest.json"
            snapshot.write_text(json.dumps({"models": models, "legend": F.build_legend(models, bands)}))
            with patch.object(R, "LATEST", snapshot):
                pts, front, order = R.load_points()
        self.assertNotIn("Acme Terra 5 (xhigh)", {p["n"] for p in front})
        svg = R.build_svg(pts, front, order)
        self.assertEqual(svg.count('data-history-for="acmesol55"'), 4)
        self.assertIn('[data-history-for]:not(.hi){display:none}', svg)
        self.assertIn('新版 Acme Sol 5.5 · 同档变化中位', svg)
        self.assertIn('智能指数 30 → 36 · +6 分（2 档）', svg)
        self.assertIn('>Acme Terra 5 (xhigh)</text>', svg)
        self.assertNotIn('>Acme Sol 5.1 (medium)</text>', svg)
        self.assertNotIn('effort-smooth', svg)
        gold = re.search(r'<polyline points="([^"]*)" fill="none" stroke="' + R.FRONT, svg)[1].split()
        coords = dict((name, f"{x},{y}") for name, x, y in
                      re.findall(r'data-n="([^"]+)" data-cx="([^"]+)" data-cy="([^"]+)"', svg))
        self.assertIn(coords['Acme Terra 5 (xhigh)'], gold)
        self.assertNotIn(coords['Acme Sol 5.1 (medium)'], gold)


if __name__ == "__main__":
    unittest.main()
