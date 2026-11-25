"""2026-11-08 用户要求：每日任务触发时若 DNS 还没起来，先等待而不是把当天额度烧光。

对应实测：2026-11-07/08 的 07–09 点，ModelRadar 每轮都栽在
"nodename nor servname provided, or not known"，10 点后同样的请求立刻成功。
根因是 system resolver 首选 Tailscale MagicDNS（100.100.100.100 / utun10），
唤醒初期 utun10 未就绪。本用例锁定：
  · 全部 host 可解析时立即返回，不睡、不多探；
  · 任一 host 解析失败即视为未就绪，按 30/60/120 退避，恢复后立刻放行；
  · 等待有总上限，超时也放行 —— 只等不抓会把整天拖没，抓取失败要能被看见；
  · 等待不计入 run_scheduled 的 24 次重试额度。
"""
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fetch_model_data as F


class NetReady(unittest.TestCase):
    def test_hosts_are_deduped_in_first_seen_order(self):
        self.assertEqual(
            F.net_ready_hosts(["https://a.example/x", "https://b.example/y", "https://a.example/z"]),
            ["a.example", "b.example"])

    def test_hosts_without_scheme_host_are_ignored(self):
        self.assertEqual(F.net_ready_hosts(["", "not a url"]), [])

    def test_empty_host_list_is_ready_without_probing(self):
        with patch.object(F, "_host_resolves") as probe, patch.object(F.time, "sleep") as sleep:
            self.assertTrue(F.wait_for_network([]))
        probe.assert_not_called()
        sleep.assert_not_called()

    def test_ready_network_returns_immediately(self):
        with patch.object(F, "system_proxy", return_value=None), \
                patch.object(F, "_host_resolves", return_value=True) as probe, \
                patch.object(F.time, "sleep") as sleep:
            self.assertTrue(F.wait_for_network(["https://a.example/x"]))
        self.assertEqual(probe.call_count, 1)
        sleep.assert_not_called()

    def test_usable_proxy_skips_dns_probe(self):
        """代理能解析目标域名时，本机 DNS 瞬落不该再拦住抓取（2026-11-15 实测）。

        2026-11-17：跳过预检的前提收紧为「该域名的 TLS 隧道真的带得起来」。
        端口在听不算 —— 那个故障态下隧道全断，必须回退直连，DNS 预检不能省
        （见 test_fetch_proxy.ProxyTunnelProbe）。
        """
        with patch.object(F, "system_proxy", return_value="http://127.0.0.1:7897"), \
                patch.object(F, "proxy_tunnel_ok", return_value=True), \
                patch.object(F, "_host_resolves") as probe, \
                patch.object(F.time, "sleep") as sleep:
            self.assertTrue(F.wait_for_network(["https://a.example/x"]))
        probe.assert_not_called()
        sleep.assert_not_called()

    def test_any_failing_host_blocks_until_all_resolve(self):
        # 三轮结果：①两个都失败 ②a 好了、b 还没 ③全好。每轮按 host 顺序各探一次。
        rounds = [(False, False), (True, False), (True, True)]
        seen = []

        def probe(host):
            round_index = len(seen) // 2
            position = 0 if host == "a.example" else 1
            seen.append(host)
            return rounds[round_index][position]

        with patch.object(F, "system_proxy", return_value=None), \
                patch.object(F, "_host_resolves", side_effect=probe), \
                patch.object(F.time, "sleep") as sleep:
            self.assertTrue(F.wait_for_network(["https://a.example/x", "https://b.example/y"]))
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [30, 60])

    def test_waiting_gives_up_at_max_wait_and_still_runs(self):
        clock = {"t": 0.0}

        def tick():
            return clock["t"]

        def sleep(seconds):
            clock["t"] += seconds

        with patch.object(F, "system_proxy", return_value=None), \
                patch.object(F, "_host_resolves", return_value=False), \
                patch.object(F.time, "monotonic", side_effect=tick), \
                patch.object(F.time, "sleep", side_effect=sleep):
            self.assertFalse(F.wait_for_network(["https://a.example/x"], max_wait=100))
        self.assertLessEqual(clock["t"], 100)

    def test_scheduled_waits_before_first_attempt_and_does_not_spend_retries(self):
        calls = []

        def fake_run(sources, with_logos):
            calls.append(1)
            return {"data_state": "ok"}

        with patch.object(F, "wait_for_network", return_value=True) as gate, \
                patch.object(F, "run", side_effect=fake_run), \
                patch.object(F, "render_chart_for_app", return_value=True):
            F.run_scheduled([F.SRC_AA], False)
        gate.assert_called_once()
        self.assertEqual(len(calls), 1)

    def test_unready_network_still_attempts_the_fetch(self):
        """门只负责等，不负责判死：等不到就照常跑，让失败按原路径暴露。"""
        calls = []
        with patch.object(F, "wait_for_network", return_value=False) as gate, \
                patch.object(F, "run", side_effect=lambda *a, **k: calls.append(1) or {"data_state": "ok"}), \
                patch.object(F, "render_chart_for_app", return_value=True):
            F.run_scheduled([F.SRC_AA], False)
        gate.assert_called_once()
        self.assertEqual(len(calls), 1)


class ResolverProbe(unittest.TestCase):
    def test_unknown_host_does_not_resolve(self):
        self.assertFalse(F._host_resolves("nonexistent.invalid"))

    def test_probe_restores_previous_default_timeout(self):
        before = socket.getdefaulttimeout()
        F._host_resolves("nonexistent.invalid")
        self.assertEqual(socket.getdefaulttimeout(), before)


if __name__ == "__main__":
    unittest.main()
