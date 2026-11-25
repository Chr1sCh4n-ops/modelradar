"""2026-11-15 用户报抓取整日失败。根因不是页面变大或超时太短，而是本机到 Vercel 的
直连链路退化到 0.1–2 KB/s（AA 榜单 120s 只收 98 KB），同一时刻系统代理 24–48 KB/s。
本用例锁定修复：外网请求跟随系统代理，代理不可达才回退直连，本地地址永不走代理。

2026-11-17 补充：代理端口在听、CONNECT 也回 200，但隧道之后 TLS 立刻 EOF，
旧判据（只测 TCP）把这种代理判成健康，整日抓取全挂在隧道上。判据因此升级为
"该域名的 TLS 隧道真的带得起来"，按 (代理, 域名) 缓存，失败只对这个域名回退直连。
"""
import os
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fetch_model_data as F


def listening_port() -> tuple[int, socket.socket]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock.getsockname()[1], sock


def closed_port() -> int:
    port, sock = listening_port()
    sock.close()
    return port


class ProxySelection(unittest.TestCase):
    def setUp(self):
        F._proxy_cache = None
        F._proxy_logged = None

    def tearDown(self):
        F._proxy_cache = None
        F._proxy_logged = None

    def test_explicit_env_wins_and_bare_host_port_gets_http_scheme(self):
        port, sock = listening_port()
        self.addCleanup(sock.close)
        with patch.dict(F.os.environ, {"MR_FETCH_PROXY": f"127.0.0.1:{port}"}, clear=False):
            self.assertEqual(F.system_proxy(), f"http://127.0.0.1:{port}")

    def test_mr_fetch_proxy_off_forces_direct(self):
        with patch.dict(os.environ, {"MR_FETCH_PROXY": "off"}, clear=False):
            self.assertIsNone(F.system_proxy())

    def test_socks_scheme_is_normalized_for_remote_dns(self):
        port, sock = listening_port()
        self.addCleanup(sock.close)
        with patch.dict(F.os.environ, {"MR_FETCH_PROXY": f"socks5://127.0.0.1:{port}"}, clear=False):
            self.assertEqual(F.system_proxy(), f"socks5h://127.0.0.1:{port}")

    def test_unreachable_proxy_falls_back_to_direct(self):
        with patch.dict(F.os.environ, {"MR_FETCH_PROXY": f"127.0.0.1:{closed_port()}"}, clear=False):
            self.assertIsNone(F.system_proxy())

    def test_local_hosts_never_use_proxy(self):
        with patch.object(F, "system_proxy", return_value="http://127.0.0.1:7897"):
            self.assertEqual(F.proxy_curl_args("http://127.0.0.1:9000/x"), [])
            self.assertEqual(F.proxy_curl_args("http://192.168.1.10/x"), [])

    def test_public_host_gets_proxy_args(self):
        with patch.object(F, "system_proxy", return_value="http://127.0.0.1:7897"), \
                patch.object(F.urllib.request, "proxy_bypass", return_value=False), \
                patch.object(F, "proxy_tunnel_ok", return_value=True):
            self.assertEqual(F.proxy_curl_args("https://artificialanalysis.ai/models"),
                             ["-x", "http://127.0.0.1:7897"])

    def test_system_bypass_list_is_respected(self):
        with patch.object(F, "system_proxy", return_value="http://127.0.0.1:7897"), \
                patch.object(F.urllib.request, "proxy_bypass", return_value=True):
            self.assertEqual(F.proxy_curl_args("https://artificialanalysis.ai/models"), [])

    def test_fetch_passes_proxy_to_curl(self):
        captured = {}

        def transport(command, **kwargs):
            captured["command"] = command
            return F.subprocess.CompletedProcess(command, 0, stdout=b"body", stderr=b"")

        with patch.object(F, "system_proxy", return_value="http://127.0.0.1:7897"), \
                patch.object(F.urllib.request, "proxy_bypass", return_value=False), \
                patch.object(F, "proxy_tunnel_ok", return_value=True), \
                patch.object(F.subprocess, "run", side_effect=transport):
            self.assertEqual(F.fetch("https://artificialanalysis.ai/models", retries=0), b"body")
        self.assertIn("-x", captured["command"])
        self.assertIn("http://127.0.0.1:7897", captured["command"])

    def test_cache_avoids_reprobing_within_ttl(self):
        with patch.object(F, "_proxy_from_env", return_value=None) as env, \
                patch.object(F.urllib.request, "getproxies", return_value={}) as system:
            self.assertIsNone(F.system_proxy())
            self.assertIsNone(F.system_proxy())
        self.assertEqual(env.call_count, 1)
        self.assertEqual(system.call_count, 1)

    def test_fresh_zero_monotonic_clock_does_not_hide_environment_proxy(self):
        with patch.dict(F.os.environ, {"MR_FETCH_PROXY": "http://127.0.0.1:7897"}, clear=False), \
                patch.object(F.time, "monotonic", return_value=0.0), \
                patch.object(F, "_proxy_usable", return_value=True):
            self.assertEqual(F.system_proxy(), "http://127.0.0.1:7897")


class ModelsDev(unittest.TestCase):
    def test_catalog_flattens_canonical_models_and_rejects_empty(self):
        payload = {
            "provider": {"models": {
                "vendor/model": {
                    "id": "vendor/model", "canonical_model_id": "vendor/model",
                    "limit": {"context": 128000},
                    "cost": {"input": 1.0, "output": 4.0},
                },
                "missing": {"id": "missing"},
            }},
        }
        catalog = F.parse_models_dev(__import__("json").dumps(payload).encode())
        self.assertEqual(catalog["vendor/model"]["ctx"], 128000)
        with self.assertRaises(RuntimeError):
            F.parse_models_dev(b'{"provider":{"models":{}}}')

    def test_catalog_never_overwrites_openrouter_observations(self):
        models = {"vendor/model": {
            "id": "vendor/model", "canonical_slug": "vendor/model",
            "ctx": 64000, "in_per_1m_usd": 2.0, "out_per_1m_usd": 8.0,
        }}
        catalog = {"vendor/model": {
            "canonical_model_id": "vendor/model", "ctx": 128000,
            "in_per_1m_usd": 1.0, "out_per_1m_usd": 4.0,
        }}
        self.assertEqual(F.enrich_openrouter(models, catalog), 1)
        self.assertEqual(models["vendor/model"]["ctx"], 64000)
        self.assertEqual(models["vendor/model"]["in_per_1m_usd"], 2.0)

    def test_merge_uses_catalog_only_for_missing_openrouter_values(self):
        aa = [{"name": "Vendor Model", "aa_slug": "vendor", "creator": "Vendor",
               "ctx": "", "index": 50, "evaluation_version": "v1",
               "apt_usd": 1.0, "tok_s": 10, "ttft_s": 1, "total_s": 2,
               "first_answer_s": None, "time_per_task_s": None,
               "output_tokens_per_task": None}]
        models = {"vendor/model": {
            "id": "vendor/model", "canonical_slug": "vendor/model",
            "ctx": None, "in_per_1m_usd": None, "out_per_1m_usd": None,
            "models_dev": {"ctx": 128000, "in_per_1m_usd": 1.0,
                           "out_per_1m_usd": 4.0},
        }}
        with patch.object(F, "or_match", return_value=models["vendor/model"]):
            row = F.merge(aa, models, [], {"keyword_include": ["vendor"],
                                           "keyword_exclude": []})[0]
        self.assertEqual((row["ctx"], row["in_per_1m_usd"], row["price_source"]),
                         (128000, 1.0, "models.dev"))


class ProxyTunnelProbe(unittest.TestCase):
    """2026-11-17：代理端口在听、CONNECT 也回 200，但隧道之后 TLS 立刻 EOF。

    旧判据只测 TCP，于是把这种代理判成健康，整日抓取全挂在隧道上
    （curl rc=35 SSL_ERROR_SYSCALL），而同一时刻直连是 200。
    """

    def setUp(self):
        F._proxy_cache = None
        F._proxy_logged = None
        F._tunnel_cache.clear()

    def tearDown(self):
        F._proxy_cache = None
        F._proxy_logged = None
        F._tunnel_cache.clear()

    def test_tunnel_failure_sends_that_host_direct(self):
        with patch.object(F, "system_proxy", return_value="http://127.0.0.1:7897"), \
                patch.object(F.urllib.request, "proxy_bypass", return_value=False), \
                patch.object(F, "proxy_tunnel_ok", return_value=False):
            self.assertEqual(F.proxy_curl_args("https://artificialanalysis.ai/models"), [])

    def test_tunnel_success_keeps_proxy(self):
        with patch.object(F, "system_proxy", return_value="http://127.0.0.1:7897"), \
                patch.object(F.urllib.request, "proxy_bypass", return_value=False), \
                patch.object(F, "proxy_tunnel_ok", return_value=True):
            self.assertEqual(F.proxy_curl_args("https://artificialanalysis.ai/models"),
                             ["-x", "http://127.0.0.1:7897"])

    def test_per_host_verdict_keeps_working_exit_for_other_hosts(self):
        # 同一个代理：apple 的隧道正常、AA 的不正常。判据必须按域名分开，
        # 整体判死会把还能用的出口一起关掉。
        with patch.object(F, "system_proxy", return_value="http://127.0.0.1:7897"), \
                patch.object(F.urllib.request, "proxy_bypass", return_value=False), \
                patch.object(F, "_tunnel_handshake",
                             side_effect=lambda p, h: h == "www.apple.com"):
            self.assertTrue(F.proxy_curl_args("https://www.apple.com/x"))
            self.assertEqual(F.proxy_curl_args("https://artificialanalysis.ai/models"), [])

    def test_tunnel_verdict_is_cached_per_host(self):
        with patch.object(F, "_tunnel_handshake", return_value=False) as probe:
            self.assertFalse(F.proxy_tunnel_ok("http://127.0.0.1:7897", "a.example"))
            self.assertFalse(F.proxy_tunnel_ok("http://127.0.0.1:7897", "a.example"))
            self.assertEqual(probe.call_count, 1)
            F.proxy_tunnel_ok("http://127.0.0.1:7897", "b.example")
            self.assertEqual(probe.call_count, 2)

    def test_tunnel_cache_expires_so_recovery_is_picked_up(self):
        with patch.object(F, "_tunnel_handshake", side_effect=[False, True]) as probe:
            self.assertFalse(F.proxy_tunnel_ok("http://127.0.0.1:7897", "a.example"))
            key = ("http://127.0.0.1:7897", "a.example")
            at, verdict = F._tunnel_cache[key]
            F._tunnel_cache[key] = (at - F.PROXY_CACHE_SECONDS - 1, verdict)
            self.assertTrue(F.proxy_tunnel_ok("http://127.0.0.1:7897", "a.example"))
            self.assertEqual(probe.call_count, 2)

    def test_nontcp_reply_to_connect_is_a_failure(self):
        # 代理回 403/502 之类的 CONNECT 拒绝：没有隧道，必须判不可用。
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]

            def serve():
                conn, _ = srv.accept()
                with conn:
                    conn.recv(4096)
                    conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")

            threading.Thread(target=serve, daemon=True).start()
            self.assertFalse(F._tunnel_handshake(f"http://127.0.0.1:{port}", "example.com"))

    def test_silent_close_after_connect_is_a_failure(self):
        # 这就是本次故障的样子：CONNECT 200，然后对端直接关连接（TLS 阶段 EOF）。
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]

            def serve():
                conn, _ = srv.accept()
                with conn:
                    conn.recv(4096)
                    conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                # 立刻关闭：模拟隧道建立后 TLS 握不上

            threading.Thread(target=serve, daemon=True).start()
            self.assertFalse(F._tunnel_handshake(f"http://127.0.0.1:{port}", "example.com"))

    def test_net_ready_still_probes_dns_when_tunnel_is_down(self):
        # 隧道不通 -> 该域名改走直连 -> DNS 预检不能省。
        with patch.object(F, "system_proxy", return_value="http://127.0.0.1:7897"), \
                patch.object(F, "proxy_tunnel_ok", return_value=False), \
                patch.object(F, "_host_resolves", return_value=True) as resolves:
            self.assertTrue(F.wait_for_network(["https://artificialanalysis.ai/models"],
                                               budgets=(), max_wait=5))
        self.assertEqual(resolves.call_count, 1)

    def test_net_ready_skips_dns_only_when_every_host_tunnels(self):
        with patch.object(F, "system_proxy", return_value="http://127.0.0.1:7897"), \
                patch.object(F, "proxy_tunnel_ok", return_value=True), \
                patch.object(F, "_host_resolves") as resolves:
            self.assertTrue(F.wait_for_network(["https://artificialanalysis.ai/models"],
                                               budgets=(), max_wait=5))
        self.assertEqual(resolves.call_count, 0)


if __name__ == "__main__":
    unittest.main()
