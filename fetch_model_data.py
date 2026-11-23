#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ModelRadar 抓取器
=================
多源并行抓取，合并成一张表，写 SQLite 历史 + latest.json / latest.js。

数据源
  1. AA 官方榜      https://artificialanalysis.ai/leaderboards/models   （服务端渲染真表格，可直接解析）
     字段：指数 / 每任务成本 USD / 中位 tok/s / TTFT / 总响应 / 上下文 / 厂商 / logo
  2. AA 每任务耗时  https://artificialanalysis.ai/models                 （公开加密 manifest，
     models[].intelligenceIndexTimePerTask，原始秒；仅此页有该字段）
  3. OpenRouter     https://openrouter.ai/api/v1/models                 （实时美元输入/输出单价）
  4. DataLearner    国内 AA 镜像 https://www.datalearner.com/leaderboards/external/aa-quality-index
                                                                        （国内站，更新滞后但可作交叉验证）
  5. OpenRouter 周用量
     https://openrouter.ai/api/frontend/v1/rankings/models?view=week
     字段：每模型周请求数 / prompt+completion token 量。非官方前端接口；
     抓不到时图例退回旧的指数加权，并在 data_state 标 degraded。

设计约束
  * 单源失败不影响整体：某源挂了就标记 status=error，沿用上一次的好数据并标注 stale。
  * 所有数值都可复算，不做任何缓存推断、不写死分数。
  * 输出 latest.json（状态栏组件读，用来判定"数据是否更新"并弹通知）与 latest.js
    （本地看板 file:// 直读，规避 CORS）。

用法
  python3 fetch_model_data.py                  # 抓一次
  python3 fetch_model_data.py --loop 3600      # 每 3600 秒抓一次（看板/状态栏用这个）
  python3 fetch_model_data.py --scheduled      # 按计划：每天一次 + 失败指数退避（launchd 用这个）
  python3 fetch_model_data.py --sources aa,aa_progress,aa_times,openrouter
  python3 fetch_model_data.py --no-logos
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import socket
import ssl
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import hashlib
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import html_tables
import mr_paths
import task_size
import vendor_colors

HERE = Path(__file__).resolve().parent
DATA = mr_paths.DATA
DB_PATH = mr_paths.DB_PATH
LATEST_JSON = mr_paths.LATEST_JSON
LATEST_JS = mr_paths.LATEST_JS
LOGOS_DIR = mr_paths.LOGOS_DIR
RAW_DIR = mr_paths.RAW_DIR      # AA 下发的原始 SVG，裁剪前的留底
CFG_PATH = mr_paths.CONFIG          # 可变真身（SUPPORT/config/watchlist.json）——爬虫只写这里
CFG_SEED = mr_paths.CONFIG_SEED     # 仓库里那份种子：首次落真身时抄它，之后只读
QUARANTINE_PATH = mr_paths.QUARANTINE          # 人筛过的不可信观测（真身，状态目录）
QUARANTINE_SEED = mr_paths.QUARANTINE_SEED     # 仓库里的种子：只写判据说明与字段名
FAULT_LOG = mr_paths.FAULT_LOG                 # 剥离留痕（只追加）


def _read_json(path: Path) -> dict:
    """读一个 JSON 配置：**只有"文件不在"才算空表**，坏掉的那份照原样抛。

    词表是抓取与菜单栏共用的输入，静默把坏 JSON 当"没有配置"会让整轮按空词表跑完
    （表现为"所有模型都不在关注里"），比当场炸掉难查得多 —— 这条有判据钉着。
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _union(seq_list: list) -> list:
    """按首次出现顺序去重并起来（种子在前、攒下来的在后）。"""
    out: list = []
    for seq in seq_list:
        for x in seq or []:
            if x not in out:
                out.append(x)
    return out


def load_config() -> dict:
    """词表读取的唯一入口：真身（SUPPORT）+ 种子里人写的那两段。

    为什么分两段：`keyword_include` 是**攒出来的状态**（只加不减），它属于可变真身；
    `keyword_include_pinned` / `keyword_exclude` 是人写的意图，`vendor_brand_color_override`
    也是 —— 那两段以仓库种子为准再并上真身里的，所以直接在仓库里加一个钉住的词，
    下一轮抓取就生效，不需要谁去同步两个文件。真身不存在时（新装的机器、或本机第一次跑）
    整份抄种子，覆盖一条不丢。
    """
    live, seed = _read_json(CFG_PATH), _read_json(CFG_SEED)
    if not live and not seed:
        return {}
    if not live:
        return dict(seed)
    out = dict(live)
    out["keyword_include_pinned"] = _union([seed.get("keyword_include_pinned"),
                                            live.get("keyword_include_pinned")])
    out["keyword_exclude"] = _union([seed.get("keyword_exclude"), live.get("keyword_exclude")])
    ov = dict(seed.get("vendor_brand_color_override") or {})
    ov.update(live.get("vendor_brand_color_override") or {})   # 真身里改过的算最终值
    out["vendor_brand_color_override"] = ov
    return out


def save_config(cfg: dict, text: str | None = None) -> None:
    """词表落盘的唯一出口：**永远只写真身**，绝不写代码树里那份种子。

    `text` 给定时按原样写（调用点要先拿这份字节自证一遍再落盘，见 `grow_watchlist`）。
    """
    CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(CFG_PATH, text if text is not None
                  else json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")


# --------------------------------------------------------------------------- 不可信观测隔离
# 为什么是"隔离"而不是"修数"：上游给错一个数时，我们既不该把它当真（会推假告警），
# 也不该改写它（那就没有证据可查了）。做法是**原始记录一字不动**，只在"拿来比较与派生"
# 的入口把这一条当没有 —— 历史序列仍是真值，人工回头能看见当时到底收到了什么。
#
# 一条隔离记录的形状（`permaslug` 优先，`name` 是档位全名）：
#   {"name": "...", "field": "tok_s", "from_ts": "...", "to_ts": "...",
#    "reason": "...", "source": "aa", "checked_at": "..."}
# `from_ts`/`to_ts` 用台账里那条观测的 ts（含端点，空=不限）。**必须给区间**：
# 只写模型名会把之后正常的数据一起屏蔽掉，而这正是这类工具最容易犯的错。
QUARANTINE_FIELDS = {"tok_s", "ttft_s", "total_s", "index", "apt_usd",
                     "in_per_1m_usd", "out_per_1m_usd", "time_per_task_s"}


def load_quarantine() -> list[dict]:
    """读人筛过的隔离清单：真身 + 种子两段并起来（种子只用于新机器/示例，正常为空）。

    坏 JSON 照原样抛（与 `_read_json` 同一立场）：静默按"没有隔离"跑，
    等于把已知脏数据重新当真，比当场停下更糟。
    """
    out: list[dict] = []
    for path in (QUARANTINE_SEED, QUARANTINE_PATH):
        for rec in (_read_json(path).get("quarantined") or []):
            if isinstance(rec, dict) and rec.get("name") and rec.get("field"):
                out.append(rec)
    return out


def _in_window(ts: str, rec: dict) -> bool:
    lo, hi = str(rec.get("from_ts") or ""), str(rec.get("to_ts") or "")
    return (not lo or ts >= lo) and (not hi or ts <= hi)


def quarantine_hits(rows: list[dict], ts: str, rules: list[dict] | None = None) -> list[dict]:
    """这批行里有哪些字段落进了隔离区间（不改 rows，只报命中）。

    判据只看"模型名 + 字段 + 轮次"，不看数值：数值相近不是同一件事的证据，
    而按数值匹配会在下一轮误伤（同一个坏值完全可能合法地再出现）。
    """
    rules = load_quarantine() if rules is None else rules
    hits = []
    for r in rows:
        name = r.get("name")
        if not name:
            continue
        for rec in rules:
            if rec.get("name") != name or not _in_window(ts, rec):
                continue
            f = rec["field"]
            if f in (r or {}):
                hits.append({"name": name, "field": f, "value": r.get(f),
                             "from_ts": rec.get("from_ts", ""), "to_ts": rec.get("to_ts", ""),
                             "reason": str(rec.get("reason") or "")})
    return hits


def apply_quarantine(rows: list[dict], ts: str, rules: list[dict] | None = None,
                     where: str = "", quiet: bool = False) -> int:
    """把命中隔离的字段置 None（行本身保留：其它字段仍然有效）。返回剥离的字段数。

    置 None 而不是删行：行里还有指数、价格这些没问题的字段，删掉会让模型整条掉出图；
    而 None 在下游已经有明确含义（"没有这个观测"），不会被当成 0 或推估。

    `quiet`：回溯会反复过基线（最多 40 轮 × 每条规则），逐次打屏会把当轮日志淹掉。
    留痕仍进 faults.jsonl（那里按轮次去重），只是不在 stdout 重复念同一句。
    """
    hits = quarantine_hits(rows, ts, rules)
    for h in hits:
        for r in rows:
            if r.get("name") == h["name"]:
                r[h["field"]] = None
    if hits:
        log_faults(hits, ts, where)
        if not quiet:
            log(f"隔离 {len(hits)} 个不可信观测（{where or '本轮'}）："
                + "；".join(f"{h['name']} {h['field']}" for h in hits[:4])
                + (" 等" if len(hits) > 4 else ""))
    return len(hits)


_FAULT_LOGGED: set[tuple[str, str, str]] = set()


def log_faults(hits: list[dict], ts: str, where: str = "") -> None:
    """把剥离留痕追加进故障日志。写失败只记一行，绝不因此让抓取失败。

    同一轮同一观测只记一次：回溯最多往前翻 40 轮、每轮都过一次基线，不去重的话
    一件事会被写成几十行，日志本身就失去可读性。
    """
    if not hits:
        return
    fresh = []
    for h in hits:
        key = (str(ts), str(h.get("name")), str(h.get("field")))
        if key in _FAULT_LOGGED:
            continue
        _FAULT_LOGGED.add(key)
        fresh.append(h)
    if not fresh:
        return
    try:
        FAULT_LOG.parent.mkdir(parents=True, exist_ok=True)
        at = now_str()
        with FAULT_LOG.open("a", encoding="utf-8") as fh:
            for h in fresh:
                fh.write(json.dumps({"at": at, "round_ts": ts, "stage": where,
                                     **h}, ensure_ascii=False) + "\n")
    except OSError as exc:
        log(f"⚠️ 故障日志写不进去（不影响本轮数据）：{exc}")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"

# 推理档位 / 变体后缀 —— 匹配时忽略。
# 注意：绝不能放 'pro' / 'max' / 'mini'，否则 "MiMo-V2.6-Pro" 和 "MiMo-V2.6-Pro-Ultraspeed"
# 会折叠成同一个 token 集，精确匹配就永远分不出哪个才是正主。
EFFORT_TOKENS = {
    "high", "medium", "low", "xhigh", "batch", "free", "latest",
    "ultraspeed", "ultra", "speed", "preview", "beta", "exp",
}

SRC_AA = "aa"
SRC_PROGRESS = "aa_progress"
SRC_AA_TIMES = "aa_times"
SRC_OR = "openrouter"
SRC_DL = "datalearner"
SRC_OR_USAGE = "openrouter_usage"
SRC_MODELS_DEV = "models_dev"
# diff 的基线单独存一份：**合并之后**的结果（只留要比的那几个字段），不是 AA 原始行。
# 用 AA 行当基线是个静默错法：in/out 单价来自 OpenRouter，AA 行里根本没有这两个键，
# 于是每次抓取都把 71 个模型的价格报成"从无到有"。以前这条被 diff 里的
# `a is None → continue` 吞掉，看不见；把它报出来之后就必须换成正确对象。
SRC_DIFF = "diffbase"
DIFF_FIELDS = ("index", "apt_usd", "in_per_1m_usd", "out_per_1m_usd", "tok_s", "ttft_s", "time_per_task_s")
HISTORY_FIELDS = DIFF_FIELDS + ("total_s",)
DIFF_LABEL = {"index": "智能指数", "apt_usd": "每任务成本", "in_per_1m_usd": "输入单价",
              "out_per_1m_usd": "输出单价", "tok_s": "输出速度", "ttft_s": "首块延迟",
              "time_per_task_s": "任务耗时(估)"}
# 单价来自 OpenRouter 目录里**匹配到的那一条**。以前基线只存数值不存 or_id，于是
# "输入单价 +531%" 这种变动没法就地分辨：是上游真变价，还是我们换到了另一个条目
# （本机实测：只能回去翻 snapshots 里的原始快照才敢下结论）。or_id 因此进基线，
# 但**不进 DIFF_FIELDS** —— 它不是数值，混进去会被算成百分比变动。
DIFF_ATTR = ("or_id", "aa_slug", "evaluation_version", "carried_asof")
PRICE_FIELDS = ("in_per_1m_usd", "out_per_1m_usd")

# 台账保留天数（约三个月）。看板只读最近一份 + 7 天前基线，趋势图最多用到 30 天，
# 再老的快照没人读，只是把 SQLite 越写越大（本机实测已 9.8MB）。
# 按天而不是按"次"：一次抓取会留下三个不同的 ts（AA / OpenRouter / 合并基线）。
KEEP_SNAPSHOT_DAYS = 90

SOURCES = {
    SRC_AA: "https://artificialanalysis.ai/leaderboards/models",
    SRC_PROGRESS: "https://artificialanalysis.ai/trends",
    # /models 页面下发的加密 manifest 里带 models[].intelligenceIndexTimePerTask（原始秒）。
    # 榜单页 / leaderboards 与趋势页 / trends 的 manifest 都没有这个字段。
    SRC_AA_TIMES: "https://artificialanalysis.ai/models",
    SRC_OR: "https://openrouter.ai/api/v1/models",
    SRC_DL: "https://www.datalearner.com/leaderboards/external/aa-quality-index",
    SRC_OR_USAGE: "https://openrouter.ai/api/frontend/v1/rankings/models?view=week",
    SRC_MODELS_DEV: "https://models.dev/api.json",
}

AA_METHODOLOGY = "https://artificialanalysis.ai/methodology/intelligence-benchmarking"


def evaluation_version(page: str) -> str:
    """只认官方当前方法标题，不从抓取日期或分数推断版本。"""
    pattern = r'Intelligence Index v(\d+(?:\.\d+)*)'
    doc = html_tables.parse(page)
    headings = [n.text() for n in doc.walk() if n.tag in ("h1", "h2", "h3")]
    versions = {m.group(1) for heading in headings
                for m in re.finditer(pattern, heading)}
    if not versions:
        versions = set(re.findall(pattern, page))
    if len(versions) != 1:
        raise ValueError("AA 当前评测版本未识别")
    return next(iter(versions))


def version_of(rows) -> str | None:
    versions = {r.get("evaluation_version") for r in rows}
    return next(iter(versions)) if len(versions) == 1 else None


def same_evaluation_major(a, b) -> bool:
    # 来源：2026-11-04 用户要求小版本连续比较，仅首位大版本隔离。
    # 两侧都未知（老快照没有该字段）按同一代处理：判不出差异就不该当成换代次而整段丢掉。
    if a == b:
        return True
    return bool(a and b and str(a).split(".")[0] == str(b).split(".")[0])


def _rsc_text(page: str) -> str:
    """把 Next RSC 的字符串分片拼回一份文本，供 manifest 发现复用。"""
    chunks = []
    for m in re.finditer(r'self\.__next_f\.push\((\[.*?\])\)</script>', page, re.S):
        item = json.loads(m.group(1))
        if item[0] == 1:
            chunks.append(item[1])
    return "\n".join(chunks)


def _decode_manifest(path: str, key_hex: str, raw_fetch=None) -> dict:
    """按官方公开 manifest 的 path/key 做 AES-GCM 解密，返回 JSON。"""
    import gzip
    import hashlib
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = bytes.fromhex(key_hex)
    raw = (raw_fetch or fetch)("https://artificialanalysis.ai" + path)
    decoded = AESGCM(key).decrypt(hashlib.sha256(key).digest()[:12], raw, None)
    return json.loads(gzip.decompress(decoded))


def parse_progress(page: str) -> tuple[list[dict], str]:
    """消费官网浏览器公开 manifest；完整性认证失败时不使用任何分数。"""
    text = _rsc_text(page)
    version = re.search(r'Progress Index v(\d+(?:\.\d+)*)', text)
    if not version:
        raise ValueError("Progress 版本未识别")
    candidates = []
    decode_errors = []
    for path, key_hex in re.findall(r'"manifest":\{"path":"([^"]+)","key":"([^"]+)"', text):
        if not re.fullmatch(r'/data/[a-zA-Z0-9]+\.txt', path):
            continue
        try:
            obj = _decode_manifest(path, key_hex)
        except Exception as exc:  # noqa: BLE001
            # 页面还含其它数据包，其失败不能遮住真正的 Progress 模型包；但全部失败时
            # 必须报出原因，否则「缺 cryptography」会被说成「未唯一识别」。
            decode_errors.append(exc)
            continue
        if isinstance(obj, dict) and isinstance(obj.get("models"), list):
            rows = obj["models"]
            if not rows or any(not isinstance(r, dict) or not r.get("slug") or not r.get("name")
                               or not isinstance(r.get("release"), dict)
                               or not isinstance(r.get("creator"), dict)
                               or "intelligenceIndex" not in r or "score" not in r for r in rows):
                continue
            if not any(isinstance(r.get("score"), (int, float)) and not isinstance(r.get("score"), bool)
                       and -1e6 < r["score"] < 1e6 for r in rows):
                continue
            for r in rows:
                r["evaluation_version"] = version.group(1)
            candidates.append(rows)
    if len(candidates) > 1:
        # 同一页偶尔把同一个数据包挂两次（2026-11-18 在 mini 上报过「未唯一识别」）。
        # 内容完全相同的候选先合并；合并后仍不唯一就交给上层重取一次页面，不替它挑一个。
        uniq, seen = [], set()
        for rows in candidates:
            fingerprint = hashlib.sha256(
                json.dumps(rows, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            uniq.append(rows)
        candidates = uniq
    if len(candidates) != 1:
        if not candidates and decode_errors:
            raise ValueError(f"Progress 数据包全部解密失败：{decode_errors[0]}")
        raise ValueError("Progress 模型数据包未唯一识别")
    return candidates[0], version.group(1)


def _row_release(row: dict) -> str | None:
    """快照行的 release 标识：Progress 包嵌在 `release.slug`，耗时包是扁平的 `release_slug`。"""
    release = row.get("release")
    if isinstance(release, dict) and release.get("slug"):
        return str(release["slug"])
    slug = row.get("release_slug")
    return str(slug) if slug else None


def _row_tier(row: dict) -> str:
    """快照行的档位键：Progress 包优先 `effort.slug`，耗时包用 `suffix`，再退回名字括号。

    2026-11-15 起 Progress 包按 release 一行，档位只在 `effort.slug` 里；旧快照两者都没有，
    只能从名字括号取，与 `split_series` 保持同一口径。
    """
    effort = row.get("effort")
    if isinstance(effort, dict) and effort.get("slug"):
        return norm_key(str(effort["slug"]))
    if row.get("suffix"):
        return _tier_key(str(row["suffix"]))
    return _tier_key(split_series(str(row.get("name") or ""))[1])


def _variant_index(rows: list[dict], version: str | None = None):
    """建 (release, 档位) → 行 与 slug → 行 两份索引；版本不一致的行不进来。"""
    by_key: dict[tuple[str, str], list[dict]] = {}
    by_slug: dict[str, list[dict]] = {}
    releases: set[str] = set()
    for r in rows:
        if version is not None and r.get("evaluation_version") not in (None, version):
            continue
        slug = r.get("slug")
        if slug:
            by_slug.setdefault(str(slug), []).append(r)
        release = _row_release(r)
        if release:
            releases.add(release)
            by_key.setdefault((release, _row_tier(r)), []).append(r)
    return by_key, by_slug, releases


def _match_variant(slug: str | None, tier: str, index) -> dict | None:
    """按 slug 定位到唯一一档。

    新格式的 slug 是 release，同 release 下多个档位共用一个 slug，必须再按档位定位；
    旧格式的 slug 本身就是每档一个，按它唯一匹配。候选不唯一时返回 None：宁可留空也不猜。
    """
    if not slug:
        return None
    by_key, by_slug, releases = index
    if slug in releases:
        candidates = by_key.get((slug, tier))
        if not candidates and not tier:
            # 没有档位后缀的行（如 Mistral Small 4）对应 slug 恰等于 release 的默认档。
            unique = by_slug.get(slug)
            candidates = unique if unique and len(unique) == 1 else None
    else:
        unique = by_slug.get(slug)
        candidates = unique if unique and len(unique) == 1 else None
    return candidates[0] if candidates and len(candidates) == 1 else None


def attach_progress(models: list[dict], rows: list[dict], version: str | None) -> int:
    """只匹配完整型号与档位，不把最佳档广播到同族。

    2026-11-15：榜单改成每个 release 一行后，`aa_slug` 只在同档位下唯一 ——
    只按 slug 命中会把默认档的分数写给同族所有档（实测 Claude Fable 5.2 五档全拿 Max 的
    357.95）。所以有 slug 时按 (release, 档位) 定位，旧格式回落为每档 slug 唯一匹配；
    没有 slug 时才按型号名唯一匹配，且不做模糊型号到全族的广播。
    """
    def identity(name):
        # 官网两种页面只在大小写、空白与连字符形式上有差异。
        return re.sub(r'[^a-z0-9]', '', str(name).lower())
    by_name: dict[str, list[dict]] = {}
    for r in rows:
        by_name.setdefault(identity(r.get("name")), []).append(r)
    index = _variant_index(rows, version)
    matched = 0
    for m in models:
        m["progress_index"], m["progress_version"] = None, version
        if m.get("carried_asof"):
            continue
        if m.get("aa_slug"):
            row = _match_variant(m["aa_slug"],
                                 _tier_key(split_series(m.get("name") or "")[1]), index)
        else:
            candidates = by_name.get(identity(m["name"]), [])
            # Anthropic 榜单用 (max with fallback)，manifest 用 (Max, Default Fallback)。
            if not candidates and " with fallback)" in m["name"].lower():
                name = re.sub(r' with fallback\)', ', Default Fallback)', m["name"], flags=re.I)
                candidates = by_name.get(identity(name), [])
            row = candidates[0] if len(candidates) == 1 else None
        if row is None:
            continue
        score = row.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool) and -1e6 < score < 1e6:
            m["progress_index"] = score
            matched += 1
    return matched


TASK_TIME_FIELD = "intelligenceIndexTimePerTask"
# 原始单位是秒，不做分钟换算。上限只挡明显的脏值（> 约 11.5 天/任务）。
TASK_TIME_MAX_SECONDS = 1_000_000


def task_time_seconds(value) -> float | None:
    """只接受有限、正数、合理范围内的秒值；null / bool / 负数 / inf / 超大值一律 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        v = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0 or v >= TASK_TIME_MAX_SECONDS:
        return None
    return v


def task_output_tokens(value) -> float | None:
    """AA 的每任务输出 token：新版是 {reasoning, answer, output}，只取 output 总量。"""
    if isinstance(value, dict):
        value = value.get("output")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if math.isfinite(v) and v > 0 else None


def parse_task_times(page: str, raw_fetch=None) -> list[dict]:
    """从 /models 的公开加密 manifest 读取每任务耗时（原始秒），并保留全部型号行。"""
    text = _rsc_text(page)
    raw_fetch = raw_fetch or fetch_task_manifest
    candidates = []
    fetch_errors = []
    for path, key_hex in re.findall(r'"manifest":\{"path":"([^"]+)","key":"([^"]+)"', text):
        if not re.fullmatch(r'/data/[a-zA-Z0-9]+\.txt', path):
            continue
        try:
            obj = _decode_manifest(path, key_hex, raw_fetch)
        except Exception as exc:  # noqa: BLE001
            # 下载失败和"数据包形状不对"必须分开：前者是网络问题，后者是上游改版。
            # 以前一律 continue，截断/超时最后都变成"未唯一识别"，排查时看不到真因。
            fetch_errors.append(exc)
            continue
        if not isinstance(obj, dict) or not isinstance(obj.get("models"), list):
            continue
        raw_rows = obj["models"]
        if not raw_rows or not any(isinstance(r, dict) and TASK_TIME_FIELD in r for r in raw_rows):
            continue
        rows = [
            {"slug": r.get("slug"), "name": r.get("name"),
             # 2026-11-15：榜单的 aa_slug 从"每档一个"变成"每个 release 一个"，
             # 只按 slug 匹配会让同一 release 的所有档位共用默认档的耗时。
             # 保留 release.slug 与 suffix，attach_task_times 用 (release, 档位) 精确定位。
             "release_slug": (r.get("release") or {}).get("slug"),
             "suffix": r.get("suffix"),
             "time_per_task_s": task_time_seconds(r.get(TASK_TIME_FIELD)),
             "output_tokens_per_task": task_output_tokens(
                 r.get("intelligenceIndexOutputTokensPerTask"))}
            for r in raw_rows
            if isinstance(r, dict) and r.get("slug") and r.get("name")
        ]
        if not any(r["time_per_task_s"] is not None for r in rows):
            continue
        candidates.append(rows)
    if len(candidates) != 1:
        # 一个候选都没有、且下载确实报过错时，把底层原因原样带出来。
        # 有候选（含多个）说明数据形状本身有歧义，仍按原错误报。
        if not candidates and fetch_errors:
            raise ValueError(f"AA 每任务耗时数据包下载失败：{fetch_errors[-1]}")
        raise ValueError("AA 每任务耗时数据包未唯一识别")
    return candidates[0]


def _tier_key(text: str | None) -> str:
    """把 'Max, Default Fallback' / 'High with fallback' / 'Non-reasoning' 归一成档位键。"""
    return norm_key(re.split(r"[,\s]", (text or "").strip(), maxsplit=1)[0])


def attach_task_times(models: list[dict], rows: list[dict], version: str | None) -> int:
    """按 (release slug, 档位) 精确匹配；旧格式回退到 aa_slug 唯一匹配。

    版本不一致、档位重复、值无效或沿用行一律留 None。
    2026-11-15：榜单改成每个 release 一行后，同一 aa_slug 对应多个档位；
    只按 slug 匹配会让所有档位共用默认档的耗时（实测 Claude Fable 5.2 的
    max/high/xhigh 都拿到了 Max 的 2672.85s）。task 行自带 release.slug 与
    suffix，用这两项定位到具体档位。
    """
    for m in models:
        m["time_per_task_s"] = None
        m["output_tokens_per_task"] = None
    if not version:
        return 0
    index = _variant_index(rows, version)
    matched = 0
    for m in models:
        if m.get("carried_asof") or m.get("evaluation_version") != version:
            continue
        row = _match_variant(m.get("aa_slug"),
                             _tier_key(split_series(m.get("name") or "")[1]), index)
        if row is None:
            continue
        value = task_time_seconds(row.get("time_per_task_s"))
        tokens = task_output_tokens(row.get("output_tokens_per_task"))
        if tokens is not None:
            m["output_tokens_per_task"] = tokens
        if value is not None:
            m["time_per_task_s"] = value
            matched += 1
    return matched


# 同轮窗口：一轮里 AA → Progress → 耗时三份快照各差几秒（整轮网络预算 240s）。
# 沿用点的 asof 是 AA 快照的 ts，Progress/耗时都在它**之后**数秒，所以同轮要往后找。
CARRY_ROUND_WINDOW_SECONDS = 300


def _round_rows(con: sqlite3.Connection, source: str, asof: str, cache: dict):
    """取与 asof 同一轮的那份快照；同轮找不到再退回 asof 之前最近一条。

    不能直接用 `ts<=asof`：Progress 与耗时都比 AA 晚几秒落地，那样会稳定取到上一轮。
    """
    key = (source, asof)
    if key in cache:
        return cache[key]
    row = con.execute(
        "SELECT ts, payload, evaluation_version FROM snapshots "
        "WHERE source=? AND ok=1 AND payload IS NOT NULL AND ts>=? "
        "AND ts<=datetime(?, ?) ORDER BY ts LIMIT 1",
        (source, asof, asof, f"+{CARRY_ROUND_WINDOW_SECONDS} seconds")).fetchone()
    if not row:
        row = con.execute(
            "SELECT ts, payload, evaluation_version FROM snapshots "
            "WHERE source=? AND ok=1 AND payload IS NOT NULL AND ts<=? "
            "ORDER BY ts DESC LIMIT 1", (source, asof)).fetchone()
    out = None
    if row:
        try:
            rows = json.loads(row[1])
        except (json.JSONDecodeError, TypeError):
            rows = None
        if isinstance(rows, list):
            out = (rows, version_of(rows) or row[2], row[0])
    cache[key] = out
    return out


def restore_carried_metrics(models: list[dict], con: sqlite3.Connection,
                            progress_version: str | None) -> tuple[int, int]:
    """给沿用行补回**那一轮**的历史参照与任务耗时，让沿用行的节点与现役行同形。

    为什么单独一步：`attach_progress` / `attach_task_times` 按规矩跳过 `carried_asof` 行
    （本轮的 Progress/耗时是给现役行比的，不能拿今天的值冒充旧读数）。沿用行于是只剩
    智能指数有值，历史参照与任务耗时空着 —— 读者以为这档没有这两项读数，其实那一轮有。

    口径与现役行一致，只换成同轮快照：
      · 版本门槛：Progress 只在与当前 `progress_version` 相同时回填；耗时只在快照行的
        `evaluation_version` 与沿用行相同时回填。版本不然就不回填（跨版不可比）。
      · 匹配用同一套 `(release, 档位)` 索引与唯一回退，不按族名广播。
      · 上游当期就是空值的档留空，不推算；耗时缺值交给后面的
        `derive_missing_task_times`（输出量已回填时才能派生）。
    只改这一份本轮用于绘制与比较的 models；台账（series）本来就跳过沿用行。
    """
    carried = [m for m in models if m.get("carried_asof")]
    if not carried:
        return 0, 0
    cache: dict = {}
    progress_n = time_n = 0
    for m in carried:
        asof = str(m["carried_asof"])
        if m.get("progress_index") is None and progress_version:
            snap = _round_rows(con, SRC_PROGRESS, asof, cache)
            if snap and snap[1] == progress_version:
                row = _match_variant(
                    m.get("aa_slug"),
                    _tier_key(split_series(m.get("name") or "")[1]),
                    _variant_index(snap[0], snap[1]))
                score = row.get("score") if row else None
                if (isinstance(score, (int, float)) and not isinstance(score, bool)
                        and -1e6 < score < 1e6):
                    m["progress_index"] = score
                    m["progress_version"] = progress_version
                    progress_n += 1
        snap = _round_rows(con, SRC_AA_TIMES, asof, cache)
        if snap and snap[1] == m.get("evaluation_version"):
            row = _match_variant(
                m.get("aa_slug"),
                _tier_key(split_series(m.get("name") or "")[1]),
                _variant_index(snap[0], snap[1]))
            if row is not None:
                tokens = task_output_tokens(row.get("output_tokens_per_task"))
                if m.get("output_tokens_per_task") is None and tokens is not None:
                    m["output_tokens_per_task"] = tokens
                value = task_time_seconds(row.get("time_per_task_s"))
                if m.get("time_per_task_s") is None and value is not None:
                    m["time_per_task_s"] = value
                    time_n += 1
    return progress_n, time_n


def version_change_summary(previous: dict, models: list[dict]) -> dict:
    """换版只报配对分差分布，不将它解释为能力退化。"""
    import statistics
    old, new = version_of(previous.values()), version_of(models)
    if not previous or not models or old == new or same_evaluation_major(old, new):
        return {}
    deltas = sorted(m["index"] - previous[m["name"]]["index"] for m in models
                    if old and new and m["name"] in previous and m.get("index") is not None
                    and previous[m["name"]].get("index") is not None)
    out = {"from": old, "to": new, "n": len(deltas)}
    if deltas:
        quartiles = statistics.quantiles(deltas, n=4, method="inclusive") if len(deltas) > 1 else deltas * 3
        out.update(mean_delta=round(statistics.mean(deltas), 2),
                   median_delta=round(statistics.median(deltas), 2),
                   delta_stddev=round(statistics.pstdev(deltas), 2),
                   q1_delta=round(quartiles[0], 2), q3_delta=round(quartiles[2], 2))
    out["text"] = (f"首次记录 AA 评测版本 {new or '未知'}，旧历史版本未知，已重建比较基线。"
                   if not old else f"AA 评测版本 {old} → {new or '未知'}，已重建比较基线。")
    if deltas:
        out["text"] += (f"{len(deltas)} 项同名同档评分差均值 {out['mean_delta']:+g}、"
                        f"中位数 {out['median_delta']:+g}，中间50% {out['q1_delta']:+g}～{out['q3_delta']:+g}，"
                        f"差值标准差 {out['delta_stddev']:g}。不是模型当日能力涨跌。")
    return out


def now_str() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_str()}] {msg}", flush=True)


# --------------------------------------------------------------------------- 网络就绪等待

# 来源：2026-11-07/08 实测。launchd 每日任务在 07 点唤醒窗口触发时，本机 DNS 解析
# 瞬落（curl rc=6 "Could not resolve host"、Python 侧 "nodename nor servname"），
# ModelRadar 一天 7 轮全 stale、TokenRadar 07–09 点全废，10 点后同样请求立刻成功。
# 根因是 system resolver 首选项指向 Tailscale MagicDNS（100.100.100.100，utun10，
# order 101800），唤醒初期 utun10 尚未就绪，低 order 的解析器先失败；
# 事发当时 `dscacheutil` 20/20 正常，说明故障只存在于唤醒瞬间。
#
# 因此：只对 scheduled 路径，在第一次抓取前先探测目标 host 能否解析。
# 不通就按 30s→60s→120s 退避等待（封顶 NET_READY_MAX_WAIT），
# 等待时间**不计入** run_scheduled 的 max_attempts —— 那是抓取失败的重试额度，
# 把"网络还没起来"混进去会白白烧掉当天的重试次数。
NET_READY_TIMEOUT = 20          # 单次探测的解析超时（秒）
NET_READY_MAX_WAIT = 300        # 就绪等待的总上限（秒），超过则照常开跑并让抓取自己报错
NET_READY_DELAYS = (30, 60, 120)


def net_ready_hosts(urls: list[str]) -> list[str]:
    """从 URL 列表里取出去重后的主机名，保持首次出现顺序。"""
    hosts: list[str] = []
    for url in urls:
        host = urllib.parse.urlsplit(url).hostname
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def _host_resolves(host: str) -> bool:
    """解析一次；超时用 socket 默认超时兜住卡死的 resolver。

    getaddrinfo 没有 timeout 参数，唤醒瞬间 resolver 可能既不返回也不报错，
    所以临时压低默认超时，结束后恢复，避免把等待预算耗在一次卡住的查询上。
    """
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(NET_READY_TIMEOUT)
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return True
    except OSError:
        return False
    finally:
        socket.setdefaulttimeout(previous)


def wait_for_network(urls: list[str], budgets: tuple[int, ...] = NET_READY_DELAYS,
                     max_wait: float = NET_READY_MAX_WAIT) -> bool:
    """等目标 host 全部可解析；返回是否在预算内就绪。

    只要有一个 host 解析失败就当作网络未就绪 —— 抓取要的是这些 host 都能连，
    只探通一个（例如本地缓存命中的域名）不代表 DNS 已经恢复。
    """
    hosts = net_ready_hosts(urls)
    if not hosts:
        return True
    # 2026-11-15：系统代理可用时目标域名由代理解析，本机 DNS 瞬落不再挡住抓取；
    # 代理不可达或没配时才回到逐 host 的 DNS 探测。
    # 2026-11-17：只有**每个** host 的隧道都真的带得起来才敢跳过。隧道不通的域名会
    # 回退直连（见 `proxy_curl_args`），那时仍要靠本机 DNS，跳过预检等于把失败推后。
    proxy = system_proxy()
    if proxy and all(proxy_tunnel_ok(proxy, h) for h in hosts):
        log("[net-ready] 系统代理对全部目标域名隧道可用，跳过 DNS 预检")
        return True
    started = time.monotonic()
    for attempt, delay in enumerate((0,) + tuple(budgets), 1):
        if delay:
            wait = min(delay, max_wait - (time.monotonic() - started))
            if wait <= 0:
                break
            log(f"[net-ready] 网络仍未就绪，{int(wait)}s 后重试解析（{'、'.join(hosts)}）")
            time.sleep(wait)
        if time.monotonic() - started > max_wait:
            break
        bad = [h for h in hosts if not _host_resolves(h)]
        if not bad:
            if attempt > 1:
                log(f"[net-ready] 第 {attempt} 次探测解析恢复，继续抓取")
            return True
        log(f"[net-ready] DNS 未就绪：{'、'.join(bad)}")
    log(f"[net-ready] 等待 {int(time.monotonic() - started)}s 仍未就绪，照常开跑（让抓取自己报错，不吞掉失败）")
    return False


# --------------------------------------------------------------------------- 抓取

# 来源：2026-10-29 用户要求修复部分抓取成功却被240秒外壳终止的问题。
# 网络整轮240秒、单源100秒；剩余60秒留给本地合并、发布和渲染。
# 2026-11-08 调整：原为整轮180秒 / 单源60秒。实测六个源正文合计约 3.85 MB
# （AA 榜单 1.68 MB、AA models 1.39 MB、AA trends 0.78 MB），本网络续传实测
# 约 22 KB/s，60 秒单源预算只能拿到 1.34 MB —— AA 主表一份就超了，整轮 180 秒
# 也容不下三个 AA 页面再加其余三个源。放宽到 240/100。
# Widget 外壳预算同步 240 → 300，保持"外壳 > 整轮"这条不变式（见 ChartWidget.swift）。
# curl与父进程双重截止覆盖DNS、响应头和慢滴正文，不遗留下载线程。
ROUND_NETWORK_BUDGET = 240
SOURCE_NETWORK_BUDGET = 100
FETCH_BUDGET = 30
FETCH_TOTAL_BUDGET = SOURCE_NETWORK_BUDGET
_network_deadline = ContextVar("network_deadline", default=float("inf"))


@contextmanager
def network_budget(seconds: float):
    token = _network_deadline.set(min(_network_deadline.get(), time.monotonic() + seconds))
    try:
        yield
    finally:
        _network_deadline.reset(token)


# 来源：2026-11-04 实测 AA 走 Vercel 的边缘在这个网络上握手会间歇性卡住。
# 同一 URL 连打 10 次：connect-timeout=4s 成功 4/10、8s 成功 7/10、15s 只成功 5/10 ——
# 握不上的那几次是干等到超时，等得越久越浪费预算，所以把"建连"这一步从"单次传满
# 预算"里拆出来单独封顶。正常站点握手都在 1s 内（github 0.38s、cloudflare 1.6s），不受影响。
FETCH_CONNECT_CAP = 8
# 续传阶段的单刀时长与"连续零增长收手"阈值：短刀才换得动握手，零增长说明这条路死了。
FETCH_RESUME_SLICE = 8
FETCH_RESUME_IDLE_SLICES = 3

# 2026-11-15：本机到 Vercel 的直连链路实测退化到 0.1–2 KB/s（AA 榜单 120s 只收
# 98 KB、models 只收 16 KB），同一时刻本机系统代理 24–48 KB/s、mini 168 KB/s；
# 页面正文没有变大（榜单 1.44 MB、models 1.28 MB、trends 0.77 MB，均不大于
# 2026-11-08 的实测）。这种退化放大超时没有意义 —— 1.4 MB 在 1 KB/s 下要 24 分钟，
# 所以外网请求跟随系统代理；代理不可达时才回退直连，健康网络的行为不变。
PROXY_CACHE_SECONDS = 60
PROXY_PROBE_TIMEOUT = 1.5
# 隧道探测（CONNECT + TLS 握手）要跨公网，1.5s 只够 TCP 建连；给到 6s，
# 仍低于单源预算 100s，一轮六个源最多多花几秒。
PROXY_TUNNEL_TIMEOUT = 6.0
# 零时间戳不能当空标记：新 Python 进程的 monotonic 时钟可能从接近零开始。
_proxy_cache: tuple[float, str | None] | None = None
_proxy_logged: str | None = None
# (proxy, host) -> (单调时钟时刻, 隧道是否可用)。按 host 记，不按代理整体记：
# 实测同一个代理对 apple.com 的隧道正常、对其余域名全部 SSL EOF，
# 整体判死会把还能用的出口一起关掉。
_tunnel_cache: dict[tuple[str, str], tuple[float, bool]] = {}


def _normalize_proxy(value: str) -> str:
    """把环境/系统代理统一成 curl -x 能吃的 URL；SOCKS 用 socks5h 让代理解析域名。"""
    if "://" not in value:
        return "http://" + value
    scheme, rest = value.split("://", 1)
    if scheme in {"socks", "socks5", "socks5h"}:
        return "socks5h://" + rest
    return value


# 显式要求直连：`MR_FETCH_PROXY=off`（也接受 none/direct/空串）。
# 存在的理由：跑在不装代理的机器上（CI/runner）时，macOS 的系统代理读数是残留配置，
# 拿它去连只会得到 SSL_ERROR_SYSCALL，必须能一句话关掉而不是靠"连不上自动回退"。
PROXY_OFF = "\x00direct"


def _proxy_from_env() -> str | None:
    for key in ("MR_FETCH_PROXY", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        if key not in os.environ:
            continue
        value = os.environ.get(key) or ""
        if value.strip().lower() in {"", "off", "none", "direct"}:
            return PROXY_OFF
        return _normalize_proxy(value)
    return None


def _proxy_usable(url: str) -> bool:
    """代理端口能不能连上。只是"端口在听"，不代表隧道能带穿流量。

    保留这个粗探测：代理进程没起时它是毫秒级的快速否定，不必等到隧道超时。
    隧道能不能真的带 TLS 由 `proxy_tunnel_ok()` 判 —— 两者都要过才用代理。
    """
    parts = urllib.parse.urlsplit(url)
    if not parts.hostname or not parts.port:
        return False
    try:
        with socket.create_connection((parts.hostname, parts.port),
                                      timeout=PROXY_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def _tunnel_handshake(proxy: str, host: str) -> bool:
    """用这个代理给 `host:443` 建隧道并完成 TLS 握手；任一步失败即不可用。

    2026-11-17 实测的故障：代理端口在听、`CONNECT` 还回 `200 Connection established`，
    但隧道之后的 TLS 阶段立刻 `SSL: UNEXPECTED_EOF_WHILE_READING`（curl 侧表现为
    `rc=35 SSL_ERROR_SYSCALL`）。同一时刻直连这两个域名都是 200。只测 TCP 的旧判据
    会把这种代理判成健康，于是整日抓取全挂在隧道上 —— 判据必须走到 TLS 之后。
    """
    parts = urllib.parse.urlsplit(proxy)
    if not parts.hostname or not parts.port:
        return False
    raw = None
    try:
        if parts.scheme.startswith("socks"):
            # SOCKS 代理没有 CONNECT 文本握手，直接交给 ssl 走它自己的代理协议栈。
            raw = socket.create_connection((parts.hostname, parts.port),
                                           timeout=PROXY_TUNNEL_TIMEOUT)
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                raw = None                   # with 已接管，别在 finally 里重复关
                return bool(tls.version())
        raw = socket.create_connection((parts.hostname, parts.port),
                                       timeout=PROXY_TUNNEL_TIMEOUT)
        raw.sendall(f"CONNECT {host}:443 HTTP/1.1\r\n"
                    f"Host: {host}:443\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = raw.recv(1024)
            if not chunk:
                return False
            buf += chunk
            if len(buf) > 8192:
                return False
        status = buf.split(b"\r\n", 1)[0]
        if b" 200" not in status:
            return False
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(raw, server_hostname=host) as tls:
            raw = None
            return bool(tls.version())
    except (OSError, ssl.SSLError, ValueError):
        return False
    finally:
        if raw is not None:
            try:
                raw.close()
            except OSError:
                pass


def proxy_tunnel_ok(proxy: str, host: str) -> bool:
    """这个代理能不能给 `host` 带起一条可用的 TLS 隧道；按 (代理, host) 缓存。

    缓存和 `system_proxy()` 同寿命（60 秒）：代理规则修好、VPN 换线之后，
    下一次探测就会重新走隧道，不需要重启进程。
    """
    key = (proxy, host)
    now = time.monotonic()
    hit = _tunnel_cache.get(key)
    if hit and now - hit[0] < PROXY_CACHE_SECONDS:
        return hit[1]
    ok = _tunnel_handshake(proxy, host)
    _tunnel_cache[key] = (now, ok)
    if not ok:
        log(f"[net] 代理 {proxy} 对 {host} 的 TLS 隧道不可用，这个域名回退直连")
    return ok


def system_proxy() -> str | None:
    """返回可用的外网代理；没有、连不上或只给本地地址时返回 None。

    读取顺序与系统工具一致：显式环境变量 → Python 的 getproxies()（在 macOS 上读
    系统网络配置，不另起 subprocess）。带 60 秒缓存，代理中途关掉或起来都能跟上。
    """
    global _proxy_cache, _proxy_logged
    now = time.monotonic()
    if _proxy_cache is not None:
        at, cached = _proxy_cache
        if now - at < PROXY_CACHE_SECONDS:
            return cached
    url = _proxy_from_env()
    if url == PROXY_OFF:
        _proxy_cache = (now, None)
        return None
    if not url:
        proxies = urllib.request.getproxies()
        for key in ("socks", "https", "http"):
            if proxies.get(key):
                url = _normalize_proxy(proxies[key])
                break
    if url and not _proxy_usable(url):
        if _proxy_logged != "":
            log(f"[net] 系统代理 {url} 连不上，回退直连")
            _proxy_logged = ""
        url = None
    elif url and _proxy_logged != url:
        log(f"[net] 外网走系统代理 {url}")
        _proxy_logged = url
    _proxy_cache = (now, url)
    return url


def _is_local_host(host: str | None) -> bool:
    if not host:
        return True
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        return True
    return bool(re.fullmatch(
        r"10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+"
        r"|169\.254\.\d+\.\d+", host))


def proxy_curl_args(url: str) -> list[str]:
    """给 curl 加 -x；本地地址、系统例外表与隧道不通的域名照旧直连。"""
    host = urllib.parse.urlsplit(url).hostname
    if _is_local_host(host):
        return []
    try:
        if urllib.request.proxy_bypass(host):
            return []
    except Exception:  # noqa: BLE001 —— 例外表读不到不该阻断抓取
        pass
    proxy = system_proxy()
    if not proxy:
        return []
    # 代理端口在听还不够：这个域名必须能真的带起 TLS 隧道，否则改走直连。
    if not proxy_tunnel_ok(proxy, host):
        return []
    return ["-x", proxy]


def _resume_total(header_path: Path) -> int | None:
    """从 curl -D 落的响应头里取正文总长；没有 Range/Content-Length 就是 None。"""
    try:
        text = header_path.read_text(encoding="iso-8859-1")
    except OSError:
        return None
    total = None
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "content-range" and "/" in value:
            tail = value.rsplit("/", 1)[1].strip()
            if tail.isdigit():
                total = int(tail)
        elif key == "content-length" and value.strip().isdigit():
            total = int(value.strip())
    return total


def fetch(url: str, timeout: int = 40, retries: int = 2,
          budget: float = FETCH_BUDGET, total_budget: float = FETCH_TOTAL_BUDGET) -> bytes:
    """抓一个 URL，全程受来源/整轮截止约束。两段式，只为修 2026-11-04 实测的慢滴上游。

    第一段与改动前完全一致（`--compressed` 一次性直传）：健康源（OpenRouter、
    DataLearner、GitHub…）一次就成，不付任何额外代价。

    只有第一段失败才进第二段：短切片 + 断点续传。依据是 2026-11-04 实测 AA（Vercel
    边缘）在本网络上的两个特征：
      * TLS 握手间歇性卡住 —— 同一 URL 打 10 次，connect-timeout 4s 成 4/10、8s 成 7/10、
        15s 只成 5/10。握不上就是干等到超时，等越久越亏预算，所以建连单独封顶。
      * 正文挤牙膏（约 0.5～3 KB/s，中间长时间零速）。原写法里 curl 被 `--max-time`
        掐掉后，已收到的几万字节随管道一起丢，重试等于从 0 再来；实测同样 60s 预算，
        续传累加拿到 460 KB，一次性长跑只有 47 KB。

    第二段用 identity 而非 `--compressed`：后者写进文件的是**解压后**字节，那个长度
    不是报文偏移，拿它做 Range 会错位拼出坏正文。源忽略 Range 时 curl 报 rc=33，丢弃
    半截重来。连续若干切片零增长即收手 —— 不把预算耗在已经死掉的连接上。

    外网请求带 `system_proxy()` 给出的 `-x`：本机到 Vercel 的直连会整段退化，
    而系统代理仍走健康出口；代理连不上时 `system_proxy()` 返回 None，行为回到直连。
    """
    deadline = min(time.monotonic() + total_budget, _network_deadline.get())
    last: Exception | None = None
    part: Path | None = None
    try:
        first_left = deadline - time.monotonic()
        if first_left <= 0:
            raise RuntimeError(f"抓取失败 {url}: 网络总时长上限已用尽，未再发起请求")
        limit = min(budget, first_left)
        try:
            result = subprocess.run(
                ["/usr/bin/curl", "--disable", "-f", "-sS", "-L", "--compressed",
                 "-A", UA, *proxy_curl_args(url),
                 "--connect-timeout", str(min(timeout, FETCH_CONNECT_CAP, limit)),
                 "--max-time", str(limit), url],
                capture_output=True, timeout=limit)
            if result.returncode == 0 and result.stdout:
                return result.stdout
            detail = result.stderr.decode("utf-8", "replace").strip()[-300:]
            last = RuntimeError(f"curl rc={result.returncode}: {detail}")
        except subprocess.TimeoutExpired:
            last = RuntimeError(f"首轮直传 {limit:.0f}s 未完成")
        except Exception as exc:  # noqa: BLE001
            last = exc

        if retries > 0 and deadline - time.monotonic() > 0:
            with tempfile.NamedTemporaryFile(prefix="modelradar-fetch-",
                                             suffix=".part", delete=False) as handle:
                part = Path(handle.name)
            headers = part.with_suffix(".hdr")
            idle = 0
            while deadline - time.monotonic() > 0 and idle < FETCH_RESUME_IDLE_SLICES:
                slice_limit = min(FETCH_RESUME_SLICE, deadline - time.monotonic())
                have = part.stat().st_size if part.exists() else 0
                args = ["/usr/bin/curl", "--disable", "-f", "-sS", "-L", "-A", UA,
                        *proxy_curl_args(url),
                        "--connect-timeout", str(min(timeout, FETCH_CONNECT_CAP, slice_limit)),
                        "-H", "Accept-Encoding: identity", "-D", str(headers),
                        "--max-time", str(slice_limit), "-o", str(part)]
                if have:
                    args += ["-C", "-"]
                args.append(url)
                try:
                    result = subprocess.run(args, capture_output=True, timeout=slice_limit)
                except subprocess.TimeoutExpired:
                    got = part.stat().st_size if part.exists() else 0
                    last = RuntimeError(f"续传切片 {slice_limit:.0f}s 未完成（已收 {got} B）")
                    idle += 1
                    continue
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    idle += 1
                    continue
                size = part.stat().st_size if part.exists() else 0
                # rc=18 = 源提前断流，但刀下的字节已经落盘：只要收满服务端自报的总长就是完整正文。
                total = _resume_total(headers)
                if size and (result.returncode == 0 or (total is not None and size >= total)):
                    body = part.read_bytes()
                    return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
                detail = result.stderr.decode("utf-8", "replace").strip()[-300:]
                last = RuntimeError(f"curl rc={result.returncode}: {detail}")
                if result.returncode == 33:
                    part.unlink(missing_ok=True)
                    idle += 1
                    continue
                if size > have:
                    idle = 0
                else:
                    idle += 1
        raise RuntimeError(f"抓取失败 {url}: {last}")
    finally:
        if part is not None:
            part.unlink(missing_ok=True)
            part.with_suffix(".hdr").unlink(missing_ok=True)


TASK_FETCH_CONNECT_TIMEOUT = 10
# 2026-11-08 调整：原为 45 秒。aa_times 一轮要抓两次（/models 页面约 1.46 MB
# + 加密 manifest 约 1.46 MB），两次都在同一个单源预算里，45 秒的单独封顶
# 会先于 SOURCE_NETWORK_BUDGET 生效，把慢网络下的第二次抓取截断；截断后的
# 正文再被 parse_task_times 当成"数据包未唯一识别"，真实网络失败被掩盖。
# 改成与单源预算对齐：总时长仍由共享 deadline 约束，不会超额。
TASK_FETCH_MAX_TIME = SOURCE_NETWORK_BUDGET


def fetch_task_manifest(url: str) -> bytes:
    """抓任务页面或manifest，共用来源及整轮截止，不另开回退预算。

    2026-11-15：系统代理到 Vercel 的 TLS 仍会偶尔瞬断（rc=35 SSL_ERROR_SYSCALL），
    原 retries=0 让一次瞬断就废掉整个 aa_times 来源。重试只吃同一份来源预算，
    不延长总时长。
    """
    return fetch(url, timeout=TASK_FETCH_CONNECT_TIMEOUT, retries=2,
                 budget=TASK_FETCH_MAX_TIME, total_budget=TASK_FETCH_MAX_TIME)


# --------------------------------------------------------------------------- 工具

def num(text: str | None):
    """'1M' -> 1000000 ; '$5.98' -> 5.98 ; '984k' -> 984000 ; '-' -> None"""
    if text is None:
        return None
    t = text.strip().replace(",", "").replace("$", "").replace("¥", "")
    if not t or t in {"-", "—", "N/A", "null"}:
        return None
    # 小数点最多一个：`^([0-9.]+)$` 会放过 "."、"1.2.3" 这类串，float() 当场抛
    # ValueError，而单元格文本是第三方页面内容 —— 一行脏数据不该炸掉整个 AA 源。
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([kKmM])?$", t)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    if unit == "k":
        v *= 1_000
    elif unit == "m":
        v *= 1_000_000
    return int(v) if v == int(v) else v


def norm_key(name: str) -> str:
    """'DeepSeek V4.1 Flash (max)' -> 'deepseekv41flashmax'。关键词用这个做子串匹配才不会漏。"""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _strip_parens(s: str) -> str:
    """只删「档位类」括号。'(max)' / '(high with fallback)' 要删，'(0902)' 这种日期得留下，
    否则 'Qwen3.8 Max (0902)' 会退化成 'Qwen3.8 Max'，永远对不上一模一样的 OR id。"""
    for _ in range(4):
        new = re.sub(r"\(([^)]*)\)",
                     lambda m: " " if (m.group(1).strip() in EFFORT_TOKENS
                                       or m.group(1).strip() == "with fallback") else f" {m.group(1)} ",
                     s)
        if new == s:
            break
        s = new
    return s


def tokens_list(name: str) -> list[str]:
    """有序 token 列表：'GLM-5.3 (max)' -> ['glm', '5.3']（版本号 '5.3' 必须整体保留）。"""
    s = _strip_parens(name.lower())
    s = re.sub(r"[^a-z0-9.]+", " ", s)
    out = []
    for raw in s.split():
        t = raw.strip(".")
        if t and t not in EFFORT_TOKENS:
            out.append(t)
    return out


def tokens(name: str) -> set[str]:
    return set(tokens_list(name))


# 档位词：出现在尾括号里表示"同一代次的不同档"，不是另一个模型。
# 用集合而不是正则枚举，因为 AA 的写法本身就是这几个词的组合。
TIER_WORDS = EFFORT_TOKENS | {"fallback", "with", "non", "reasoning", "thinking",
                              "xhigh", "none", "max", "min"}


def _is_tier(inner: str) -> bool:
    """括号里全是档位词、且不含数字，才算档位。'(0902)' / '(2.5)' 是版本或日期，必须留在族名里。"""
    toks = re.findall(r"[a-z0-9.]+", inner.lower())
    if not toks or any(re.search(r"\d", t) for t in toks):
        return False
    return all(t in TIER_WORDS for t in toks)


def split_series(name: str) -> tuple[str, str]:
    """把模型名拆成 (同代次族名, 档位)。

    族名保留厂商+型号+**版本号数字**：按用户口径，能力线只连"同族同代次"的档位，
    即 6 / 5.6 / 5.5 / 5.3 / 5.1 / 5 / 4.1 各自一条，5.5 与 5 不并成一条。
    例：'Claude Sonnet 5.5 (high with fallback)' → ('Claude Sonnet 5.5', 'high with fallback')
        'GPT-6 Luna (low)'                        → ('GPT-6 Luna', 'low')
        'Qwen3.8 Max (0902)'                      → ('Qwen3.8 Max (0902)', '')   # 日期不是档位
    """
    s = name or ""
    tiers = [m.strip() for m in re.findall(r"[\(（\[]([^\)）\]]*)[\)）\]]", s) if _is_tier(m)]
    base = re.sub(r"[\(（\[]([^\)）\]]*)[\)）\]]",
                  lambda m: " " if _is_tier(m.group(1)) else m.group(0), s)
    base = re.split(r"\s*[|·/]\s*", base)[0]
    base = re.sub(r"\s+", " ", base).strip().strip("-·—")
    return base, " ".join(tiers).strip()


# --------------------------------------------------------------------------- 源 1：AA

def _nonneg(value):
    """只接受有限、非负的数字；其余（含 bool 与字符串）一律 None。"""
    return (value if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0 else None)


def _round_half_up(value, digits: int = 0):
    """与页面显示口径一致：指数取整、成本保留两位，不用 Python 的银行家舍入。"""
    q = Decimal(str(value)).quantize(Decimal("1").scaleb(-digits), rounding=ROUND_HALF_UP)
    return int(q) if digits == 0 else float(q)


def _round_cost(value):
    """沿用榜单显示口径：小于 0.1 的成本保留四位，其余保留两位。"""
    return _round_half_up(value, 4 if value < 0.1 else 2)


def _fmt_ctx(tokens) -> str:
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens <= 0:
        return ""
    if tokens % 1_000_000 == 0:
        return f"{tokens // 1_000_000}M"
    if tokens % 1024 == 0:
        return f"{tokens // 1024}K"
    if tokens % 1000 == 0:
        return f"{tokens // 1000}K"
    return str(tokens)


def _aa_display(table, columns: dict[str, int], width: int, txt) -> dict[str, tuple[str, str]]:
    """从 HTML 表取每个 release 的上下文与 logo；新版按 release 一行，档位在 JSON 里。"""
    out: dict[str, tuple[str, str]] = {}
    for tr in html_tables.tbody_rows(table):
        tds = [n for n in tr.children if n.tag == "td"]
        if len(tds) != width:
            continue
        cells = [txt(td) for td in tds]
        values = {field: cells[i] for field, i in columns.items()}
        slug = next((n.attrs.get("href", "").split("/")[2]
                     for n in tr.find_all("a")
                     if re.fullmatch(r'/models/[^/]+', n.attrs.get("href", ""))), None)
        if not slug:
            continue
        logo = ""
        for td in tds:
            srcs = html_tables.img_srcs(td)
            if srcs and "logos/" in (srcs[0] or ""):
                logo = srcs[0]
                break
        out.setdefault(slug, (values.get("ctx") or "", logo))
    return out


def _parse_aa_models(html: str, table, columns: dict[str, int], width: int, txt) -> list[dict]:
    """新版榜单：HTML 只留一档做展示，完整档位在 initialModels 里，按 JSON 取数。

    2026-11-15 实测：页面从"每档一行"改成"每个 release 一行"，202 个 release 的
    290 个档位里只有 58 个出现在 HTML，其余 55 个有分数的档位只在 JSON 里。
    继续按 HTML 行取数会丢掉一半档位，所以新版以 initialModels 为准；
    HTML 只提供每个 release 的 ctx 与 logo（同 release 各档共用）。
    """
    text = _rsc_text(html)
    matches = list(re.finditer(r'"initialModels":', text))
    if len(matches) != 1:
        raise ValueError("AA 新版榜单缺少唯一模型数据，拒绝把任务分钟当输出速度")
    models, _ = json.JSONDecoder().raw_decode(text[matches[0].end():].lstrip())
    if not isinstance(models, list) or not models:
        raise ValueError("AA 模型数据不是非空列表")
    display = _aa_display(table, columns, width, txt)
    rows = []
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("AA 模型数据不是对象")
        release = model.get("release")
        slug = release.get("slug") if isinstance(release, dict) else None
        if not isinstance(slug, str) or not slug:
            raise ValueError("AA 模型数据缺少有效 slug")
        index = _nonneg(model.get("intelligenceIndex"))
        if index is None:          # 没有分数的档位与旧口径一样直接丢
            continue
        apt = _nonneg(model.get("intelligenceIndexCostPerTask"))
        ctx, logo = display.get(slug, ("", ""))
        rows.append({
            # shortName 与旧版可见名同形（Claude Fable 5.2 (max)），不用带
            # "Default Fallback" 的 name，避免同族档位被当成新族。
            "name": (model.get("shortName") or model.get("name") or "").strip(),
            "aa_slug": slug,
            "creator": model.get("modelCreatorName") or "",
            "ctx": ctx or _fmt_ctx(model.get("contextWindowTokens")),
            "index": _round_half_up(index),
            "apt_usd": _round_cost(apt) if apt is not None else None,
            "tok_s": _nonneg(model.get("outputSpeed")),
            "ttft_s": _nonneg(model.get("timeToFirstToken")),
            "total_s": None,       # 新版不再提供旧基准总响应，不能用首回答延迟冒充。
            "first_answer_s": _nonneg(model.get("timeToFirstAnswerToken")),
            "output_tokens_per_task": None,
            "logo": logo or (model.get("modelCreatorLogo") or ""),
        })
    if not rows:
        raise RuntimeError("AA 表格解析出 0 行")
    return rows


def parse_aa(html: str) -> list[dict]:
    doc = html_tables.parse(html)
    txt = html_tables.cell_text
    # 来源：2026-11-04 用户要求稳妥匹配。只认字段语义与单位，不按位置或数值猜列。
    aliases = {
        "model": "name", "contextwindow": "ctx", "creator": "creator",
        "artificialanalysisintelligenceindex": "index", "costpertaskusd": "apt_usd",
        "outputspeedts": "tok_s", "outputspeedtokenss": "tok_s",
        "latencyfirstchunks": "ttft_s", "firstchunks": "ttft_s",
        "totalresponses": "total_s", "timepertaskminutes": "task_minutes",
        "firstanswertokens": "first_answer_s",
    }
    candidates = []
    # 2026-11-15：新版榜单把 Creator 并进 Model 单元格（logo 的 alt），不再单独成列；
    # 语义列仍然必须齐全，缺了就拒绝解析，不按位置补。
    required = {"name", "ctx", "index", "apt_usd"}
    for table in html_tables.find_tables(doc):
        for tr in html_tables.rows_any(table):
            headers = [n for n in tr.children if n.tag == "th"]
            if not headers or any(n.attrs.get("colspan", "1") != "1"
                                  or n.attrs.get("rowspan", "1") != "1" for n in headers):
                continue
            columns = {}
            for i, th in enumerate(headers):
                field = aliases.get(norm_key(txt(th)))
                if field:
                    if field in columns:
                        raise ValueError(f"AA 表头字段重复：{field}")
                    columns[field] = i
            if required <= columns.keys():
                candidates.append((table, columns, len(headers)))
    if len(candidates) != 1:
        raise ValueError("AA 榜单表头未唯一识别，拒绝按旧列位置猜测")
    table, columns, width = candidates[0]
    task_layout = "task_minutes" in columns
    if "tok_s" not in columns and not task_layout:
        raise ValueError("AA 速度列语义或单位未知，拒绝解析")
    if task_layout:
        return _parse_aa_models(html, table, columns, width, txt)
    rows = []
    for tr in html_tables.tbody_rows(table):
        tds = [n for n in tr.children if n.tag == "td"]
        if len(tds) != width or any(n.attrs.get("colspan", "1") != "1" for n in tds):
            raise ValueError("AA 数据行与表头列数不一致，拒绝错列数据")
        cells = [txt(td) for td in tds]
        values = {field: cells[i] for field, i in columns.items()}
        name, ctx, creator = values["name"], values["ctx"], values.get("creator", "")
        index, apt = values["index"], values["apt_usd"]
        tps, ttft, total = (values.get(f) for f in ("tok_s", "ttft_s", "total_s"))
        # logo 挂在厂商那一格（td[2]），不是模型名那一格
        logo = ""
        for td in tds:
            srcs = html_tables.img_srcs(td)
            if srcs and "logos/" in (srcs[0] or ""):
                logo = srcs[0]
                break
        idx_v, apt_v, tps_v = num(index), num(apt), num(tps)
        if idx_v is None:          # 没有分数的行（多数是付费/不可用模型）直接丢
            continue
        rows.append({
            "name": name,
            "aa_slug": next((n.attrs.get("href", "").split("/")[2]
                             for n in tr.find_all("a")
                             if re.fullmatch(r'/models/[^/]+', n.attrs.get("href", ""))), None),
            "creator": creator,
            "ctx": ctx,
            "index": idx_v,
            "apt_usd": apt_v,
            "tok_s": tps_v,
            "ttft_s": num(ttft),
            "total_s": num(total),
            # 旧版榜单没有这一列；新版走 _parse_aa_models。
            "first_answer_s": num(values.get("first_answer_s")),
            # 每任务输出 token 由 attach_task_times 从同一份 manifest 填入。
            "output_tokens_per_task": None,
            "logo": logo,
        })
    if not rows:
        raise RuntimeError("AA 表格解析出 0 行")
    return rows


# --------------------------------------------------------------------------- 源 2：OpenRouter

def parse_openrouter(payload: bytes) -> dict:
    data = json.loads(payload)["data"]
    out = {}
    for m in data:
        p = m.get("pricing") or {}
        try:
            pin = float(p.get("prompt") or 0)
            pout = float(p.get("completion") or 0)
        except (TypeError, ValueError):
            continue
        if pin < 0 or pout < 0:
            continue
        arch = m.get("architecture") or {}
        out[m["id"]] = {
            "id": m["id"],
            "canonical_slug": m.get("canonical_slug"),
            "name": m.get("name"),
            "in_per_1m_usd": round(pin * 1_000_000, 6),
            "out_per_1m_usd": round(pout * 1_000_000, 6),
            "ctx": m.get("context_length"),
            "modalities": arch.get("input_modalities") or [],
            "created": m.get("created"),
        }
    if not out:
        raise RuntimeError("OpenRouter 返回 0 个模型")
    return out


def parse_models_dev(payload: bytes) -> dict[str, dict]:
    """按 canonical model identity 展开 models.dev provider 目录。"""
    try:
        providers = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(f"models.dev 目录不是合法 JSON：{exc}") from exc
    if not isinstance(providers, dict):
        raise RuntimeError("models.dev 目录不是 provider 对象")
    out: dict[str, dict] = {}
    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        for model in (provider.get("models") or {}).values():
            if not isinstance(model, dict):
                continue
            identity = model.get("canonical_model_id") or model.get("id")
            if not isinstance(identity, str) or "/" not in identity:
                continue
            limit = model.get("limit") or {}
            cost = model.get("cost") or {}
            context = limit.get("context")
            input_cost = cost.get("input")
            output_cost = cost.get("output")
            if not any(v is not None for v in (context, input_cost, output_cost)):
                continue
            out.setdefault(identity, {
                "canonical_model_id": identity,
                "ctx": context if isinstance(context, int) and context > 0 else None,
                "in_per_1m_usd": input_cost if isinstance(input_cost, (int, float)) else None,
                "out_per_1m_usd": output_cost if isinstance(output_cost, (int, float)) else None,
                "source": "models.dev",
            })
    if not out:
        raise RuntimeError("models.dev 目录没有可用模型")
    return out


def enrich_openrouter(or_models: dict, catalog: dict[str, dict]) -> int:
    """挂载参考元数据，但不替换 OpenRouter 的实时报价。"""
    matched = 0
    for info in or_models.values():
        keys = (info.get("canonical_slug"), info.get("id"))
        reference = next((catalog[k] for k in keys if k in catalog), None)
        if not reference:
            continue
        info["models_dev"] = reference
        if info.get("ctx") is None and reference.get("ctx") is not None:
            info["ctx"] = reference["ctx"]
        matched += 1
    return matched


def parse_openrouter_usage(payload: bytes) -> tuple[dict[str, dict], dict]:
    """解析 OpenRouter 周用量：按 permaslug 汇总请求数与 token 量。

    `/rankings` 页面的用量没有公开 API；这里用的是页面自己调用的前端接口
    （`/api/frontend/v1/rankings/models?view=week`），返回的是同一份周榜数据。
    它是非官方接口，前端改版就可能断 —— 所以上层失败时退回旧的指数加权，
    不把"没抓到用量"伪装成"这些模型没人用"。

    返回 ({permaslug: {requests, tokens, date}}, meta)。meta 里的总量是**全站**
    周合计（本函数收到的整份数据），不是 watchlist 子集；平衡用量要用它算占比。
    """
    try:
        data = json.loads(payload).get("data")
    except (json.JSONDecodeError, AttributeError) as exc:
        raise RuntimeError(f"OpenRouter 周用量不是合法 JSON：{exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError("OpenRouter 周用量缺少 data 列表")
    by_slug: dict[str, dict] = {}
    missing = 0
    for row in data:
        if not isinstance(row, dict):
            continue
        slug = row.get("model_permaslug")
        if not slug:
            continue
        if row.get("total_prompt_tokens") is None and row.get("total_completion_tokens") is None:
            missing += 1              # 格式漂移，不是 0 用量
            continue
        try:
            requests = int(row.get("count") or 0)
            tokens = int(row.get("total_prompt_tokens") or 0) + \
                int(row.get("total_completion_tokens") or 0)
        except (TypeError, ValueError):
            missing += 1
            continue
        a = by_slug.setdefault(slug, {"requests": 0, "tokens": 0, "date": None})
        a["requests"] += requests
        a["tokens"] += tokens
        day = row.get("date")
        if day and (a["date"] is None or str(day) > a["date"]):
            a["date"] = str(day)
    if not by_slug:
        raise RuntimeError("OpenRouter 周用量解析出 0 个模型")
    total_requests = sum(a["requests"] for a in by_slug.values())
    total_tokens = sum(a["tokens"] for a in by_slug.values())
    if total_requests <= 0 or total_tokens <= 0:
        raise RuntimeError("OpenRouter 周用量合计为 0（不能当有效输入）")
    meta = {
        "rows": len(by_slug),
        "missing": missing,
        "date": max((a["date"] or "") for a in by_slug.values()) or None,
        "total_requests": total_requests,
        "total_tokens": total_tokens,
    }
    return by_slug, meta


# --------------------------------------------------------------------------- 源 3：DataLearner

def parse_datalearner(html: str) -> list[dict]:
    doc = html_tables.parse(html)
    tables = html_tables.find_tables(doc)
    if not tables:
        return []
    out = []
    for tr in html_tables.rows_any(tables[0]):
        cells = [html_tables.cell_text(td) for td in html_tables.cells(tr)]
        if len(cells) < 4:
            continue
        rank, name, index, creator = cells[0], cells[1], cells[2], cells[3]
        idx_v = num(index)
        if idx_v is None:
            continue
        out.append({"rank": rank, "name": name, "index": idx_v, "creator": creator})
    return out


# --------------------------------------------------------------------------- 合并

# AA 榜单的厂商名 -> OpenRouter 的 provider 段，防止把 Kimi 的数据串到 Qwen 头上
PROVIDER_ALIAS = {
    "zai": "zai", "zai.ai": "zai", "moonshotai": "kimi", "kimi": "kimi",
    "alibaba": "alibaba", "deepseek": "deepseek", "xiaomi": "xiaomi",
    "anthropic": "anthropic", "openai": "openai", "minimax": "minimax",
    "tencent": "tencent",
}


def or_match(or_models: dict, aa_row: dict) -> dict | None:
    """把 AA 的模型名命中到 OpenRouter 的 id。

    规则（按顺序，先 wins）：
      1. 厂商必须对得上（PROVIDER_ALIAS），否则 OpenRouter 里同名不同家的直接排除；
      2. OpenRouter 的 token 列表必须是 AA 名字 token 列表的前缀（这样
         'glm-5' 抢不走 'GLM-5.3'、'claude-opus-5' 抢不走 'Claude Opus 5.5'）；
      3. 并列时取「最短的那个」——'-pro' 比 '-pro-ultraspeed' 更可能是正主。
    """
    aa_t = tokens(aa_row["name"])
    aa_list = tokens_list(aa_row["name"])
    cands = []
    for oid, info in or_models.items():
        parts = oid.split("/")
        if len(parts) < 2:
            continue
        provider, base = parts[0], parts[-1]
        if oid.startswith("~"):         # '~anthropic/claude-opus-latest' 这类 latest 别名，跳过
            continue
        # 厂商白名单里认得这家，且与 AA creator 对上，才算"厂商已核实"。
        # （原代码只有 alias 存在且不匹配才排除 —— 认不得的 provider 反而全放行，
        # 与 docstring 正好相反。）认不得的 provider 仍可当候选 —— 白名单不可能
        # 收全 OpenRouter 所有正规托管 —— 但排序上**永远排在已核实候选后面**：
        # 否则同名转售托管（reseller 命名的 provider 不少见）会靠字母序排到正主
        # 前面，把 OpenRouter 报价张冠李戴。
        alias = PROVIDER_ALIAS.get(norm_key(provider))
        if alias is not None and alias != norm_key(aa_row["creator"]):
            continue
        verified = alias is not None and alias == norm_key(aa_row["creator"])
        o_list = tokens_list(base)
        if not o_list or len(o_list) > len(aa_list):
            continue
        if o_list != aa_list[:len(o_list)] or (set(o_list) - (aa_t | tokens(aa_row["creator"]))):
            continue
        cands.append({"verified": 0 if verified else 1, "exact": 0 if o_list == aa_list else 1,
                      "n": len(o_list), "oid": oid, "info": info})

    if not cands:
        return None
    # 用命名字段 + key= 排序，不用位置下标：上一个版本这里是 (a,b,c,oid,info) 元组，
    # 加了一个排序键之后 `cands[0][3]` 就从 info 变成了 oid，merge() 里 orr.get(...) 直接
    # AttributeError —— 排序键还会再加，这类错位必须从结构上堵掉。
    # key 里带上 oid 做最终决胜：否则两个候选前几项全等时 Python 会去比第 5 个元素（dict），
    # 报 unorderable types。
    cands.sort(key=lambda d: (d["verified"], d["exact"], d["n"], d["oid"]))
    return cands[0]["info"]


def _usage_candidates(series: str, creator: str, pairs) -> list[tuple]:
    """把 AA 的族名对到 OpenRouter 的 (匹配用 slug, 返回用 slug)。

    与 `or_match` 的差别：先去掉档位括号（`split_series` 已经做了），并且**优先精确命中**。
    旧规则"并列取最短"会把 `DeepSeek V4.1 Flash 1017` 对到更老的
    `deepseek/deepseek-v4.1-flash`，用量与价格都会串到上一代；这里让带 `1017`
    的精确条目胜出。

    匹配用 slug 一般是 OpenRouter 目录的 id（`...-1017`），返回用 slug 是
    canonical_slug（`...-20261017`），因为用量榜只认后者。直接匹配用量 slug 时
    两者相同，日期尾巴会在匹配前去掉。
    """
    st = tokens_list(series)
    if not st:
        return []
    key = norm_key(creator)
    out = []
    for match_slug, return_slug in pairs:
        parts = str(match_slug).split("/")
        if len(parts) < 2:
            continue
        provider, base = parts[0], parts[-1]
        if provider.startswith("~"):
            continue
        base = base.split(":", 1)[0]
        base = re.sub(r"-\d{8}$", "", base)   # permaslug 的日期尾巴不参与族名匹配
        alias = PROVIDER_ALIAS.get(norm_key(provider))
        if alias is not None and alias != key:
            continue
        verified = alias is not None and alias == key
        bt = tokens_list(base)
        if not bt:
            continue
        if bt == st:
            rank = 0
        elif st[:len(bt)] == bt:
            rank = 1
        elif bt[:len(st)] == st:
            rank = 2
        else:
            continue
        out.append((rank, 0 if verified else 1, -len(bt), str(return_slug)))
    out.sort()
    return out


def or_usage_match(or_models: dict, aa_row: dict) -> str | None:
    """给一行 AA 档位找 OpenRouter 用量 slug（优先 canonical_slug）。"""
    series, _ = split_series(aa_row["name"])
    pairs = ((oid, info.get("canonical_slug") or oid)
             for oid, info in or_models.items())
    cands = _usage_candidates(series, aa_row["creator"], pairs)
    return cands[0][3] if cands else None


def usage_slug_match(series: str, creator: str, usage: dict[str, dict]) -> str | None:
    """目录里没有对应条目时，直接拿 permaslug 的族名兜底匹配。"""
    cands = _usage_candidates(series, creator, ((slug, slug) for slug in usage))
    return cands[0][3] if cands else None


def attach_usage(models: list[dict], or_models: dict, usage: dict[str, dict],
                 meta: dict) -> tuple[int, int]:
    """把 OpenRouter 周用量挂到 AA 行上，并算出图例排序用的平衡用量。

    OpenRouter 的用量是**模型级**（permaslug），AA 的每个档位是同一模型的推理档。
    同一个 permaslug 被映射到多行时，用量按行数均分 —— 否则同一份流量会被
    多个档位重复计入，等于按"档位数量"给厂商加权。

    平衡用量：
        token_share = tokens_i / Σ tokens
        balanced    = requests_i × token_share
                    = (tokens_i / Σ tokens) × (requests_i / Σ requests) × Σ requests
    即"token 占比 × 请求占比"的乘积，再乘全站请求数换成请求量级；
    这样 log1p 才有可解释的刻度。缺用量的行保持 None，不参与加权平均。
    """
    total_tokens = meta.get("total_tokens") or 0
    if not usage or total_tokens <= 0:
        for m in models:
            m["usage_week_requests"] = None
            m["usage_week_tokens"] = None
            m["usage_balanced"] = None
            m["usage_permaslug"] = None
        return 0, len(models)

    for m in models:
        series, _ = split_series(m["name"])
        slug = or_usage_match(or_models, m) or usage_slug_match(series, m["creator"], usage)
        u = usage.get(slug) if slug else None
        if u:
            m["usage_week_requests"] = u["requests"]
            m["usage_week_tokens"] = u["tokens"]
            m["usage_permaslug"] = slug
            m["usage_balanced"] = u["requests"] * (u["tokens"] / total_tokens)
        else:
            m["usage_week_requests"] = None
            m["usage_week_tokens"] = None
            m["usage_balanced"] = None
            m["usage_permaslug"] = None

    slug_rows: dict[str, int] = {}
    for m in models:
        if m.get("usage_permaslug"):
            slug_rows[m["usage_permaslug"]] = slug_rows.get(m["usage_permaslug"], 0) + 1
    matched = 0
    for m in models:
        if m.get("usage_balanced") is None:
            continue
        m["usage_balanced"] /= slug_rows[m["usage_permaslug"]]
        matched += 1
    return matched, len(models) - matched


def norm_runs(s: str) -> list[str]:
    """**带边界**的分词：小写，非 [a-z0-9.] 一律当分隔符，每段去掉首尾的点。

    为什么不用 norm_key 做匹配：它把标点直接删掉，边界信息随之没了 —— 'sol' 于是
    是 'Solar' 的子串、'ling' 是 'Inkling' 的子串、'mini' 是 'Gemini' 的子串，
    关键词会把隔壁厂商整族捞进表里，而图上看着完全正常。
    点保留在段内，是为了 'qwen3.8'、'3.1' 这种版本号仍是一个整体（不能退化成 '3'）。
    """
    return [t.strip(".") for t in re.split(r"[^a-z0-9.]+", (s or "").lower()) if t.strip(".")]


def runs_hit(kr: list[str], rs: list[str]) -> bool:
    """关键词的各段必须**整段**对上行名里连续的一段（段数也要相等）。

    空段列表恒不命中：'免费'、'' 这类规范化后为空的关键词，在旧写法里 '' in blob 恒真，
    一个排除词就能把整张表清空，而 data_state 仍是 ok —— 现在从结构上不可能。
    版本号尾巴要写全：'v4' 不再顺带命中 'V4.1'，那等于把两代模型并成一代。
    """
    n = len(kr)
    if not n or len(rs) < n:
        return False
    return any(rs[i:i + n] == kr for i in range(len(rs) - n + 1))


def want_row(row: dict, kw_inc: list[str], kw_exc: list[str]) -> bool:
    """watchlist 过滤，按整词边界匹配（判据见 runs_hit）。

    多段写法照样能用：'deepseek-v4' 对上 ['deepseek','v4']，命中 DeepSeek V4 Pro 0813。
    写错的代价从"顺带多捞几家"变成"一行都不命中"，所以派生/增量那边会专门把零命中的词报出来。
    """
    rs = norm_runs(f"{row['name']} {row['creator']}")
    if any(runs_hit(norm_runs(e), rs) for e in kw_exc):
        return False
    return any(runs_hit(norm_runs(w), rs) for w in kw_inc)


# 派生关键词的判据全部是「量出来的」，没有手写停用词表 —— 上一版靠一张
# DERIVE_STOPWORDS 硬列 flash/max/pro/with…，来了新厂商或新档位词就得回去补表。
MAX_FANOUT = 0.15         # 词命中的行数占全表比例上限（'gpt' 25/126、'claude' 21/126 都出局）
MAX_CREATOR_SPREAD = 2    # 词跨的厂商数上限（'max' 跨 9 家、'flash' 跨 7 家 → 通用词）
HEAD_RATIO = 0.5          # 词必须落在「命名主体」：括号前的产品名或厂商名，不是档位后缀
WATCHLIST_TOP_N = 50      # 派生名次线的**基础值**（CLI 默认值与抓取钩子共用这一个数；
                          # 实际生效线见 watchlist_boundary：历史厂商掉出后按最高名次扩边）


def rank_order(aa_rows: list[dict]) -> list[int]:
    """按智能指数降序给出页内下标序，同分保持页面先后。

    排序**必须键在分数上**：`aa_rows[:N]` 只有在页面本身就是降序时才是"前 N 名"，
    上游一旦乱序（解析顺序、并列块重排），前 N 名就会漏掉高分行、塞进低分行。
    没有分数的行（夹具或残缺快照）排在末尾，不改它们之间的相对次序。
    """
    def key(i: int) -> tuple:
        v = aa_rows[i].get("index")
        if isinstance(v, (int, float)):
            return (0, -v, i)
        return (1, 0, i)
    return sorted(range(len(aa_rows)), key=key)


def competition_ranks(aa_rows: list[dict]) -> list[int]:
    """与 `aa_rows` 对齐的并列名次，口径为竞赛式排名：同分同名次，下一档跳过被占名次
    （59,58,57,57 → 1,2,3,3,5）。

    选竞赛式而不是稠密式（1,2,3,3,4），因为"前 50 名"在榜单语境里指名次线：
    同分并列本该一起进或一起出，而稠密式会把两条同分行挤成一条、把后面的行提前。
    同分的行数不计入后续名次，所以并列处边界会多收几条同分行 —— 这正是不拆散并列。
    """
    order = rank_order(aa_rows)
    ranks = [0] * len(aa_rows)
    prev_score, prev_rank = object(), 0
    for pos, i in enumerate(order, 1):
        score = aa_rows[i].get("index")
        ranks[i] = prev_rank if score == prev_score and isinstance(score, (int, float)) else pos
        prev_score, prev_rank = score, ranks[i]
    return ranks


def watchlist_boundary(aa_rows: list[dict], base_n: int = WATCHLIST_TOP_N,
                       history_words=()) -> dict:
    """派生用的名次线：基础 base_n，再按**历史关注词命中的厂商**各自当前最高名次扩边。

    为什么不在 50 名处一刀切：词表只加不减，掉出前 50 的老关注厂商仍在表里，
    而派生只看前 50 就再也派生不出它们的词 —— 一旦整表重算，这些厂商会被静默剔除。
    扩边口径刻意取"该厂商当前最好的那一行"，而不是它任何一行的名次：
    同厂低档（Mistral Small 4 在 #85）会把整条线拖到榜尾，等于取消名次线。

    只认 `creator` 相同即同厂（AA 每行自带厂商），不做名称猜测。
    """
    words = sorted({w for w in (history_words or []) if w})
    ranks = competition_ranks(aa_rows)
    vendors: dict[str, dict] = {}
    if words and aa_rows:
        by_vendor: dict[str, list[int]] = {}
        for i in rank_order(aa_rows):
            by_vendor.setdefault(aa_rows[i].get("creator") or "", []).append(i)
        for vendor, idx in by_vendor.items():
            if not any(want_row(aa_rows[i], words, []) for i in idx):
                continue
            best = min(idx, key=lambda i: ranks[i])
            vendors[vendor] = {"best_rank": ranks[best],
                               "rows": len(idx), "name": aa_rows[best]["name"]}
    beyond = {v: d for v, d in vendors.items() if d["best_rank"] > base_n}
    effective = max([base_n] + [d["best_rank"] for d in vendors.values()])
    return {"base_n": base_n, "effective_top_n": effective, "history_words": words,
            "vendors": vendors,
            "expanded_by": sorted(beyond, key=lambda v: (beyond[v]["best_rank"], v))}


def rows_within_rank(aa_rows: list[dict], top_n: int = WATCHLIST_TOP_N) -> set[int]:
    """名次线以内（含并列）的行下标。派生取词与覆盖要求都用这一个集合，口径不会分裂。"""
    ranks = competition_ranks(aa_rows)
    return {i for i, r in enumerate(ranks) if r <= top_n}


def token_stats(aa_rows: list[dict], tokens: set[str]) -> dict[str, tuple[frozenset, int, float]]:
    """每个候选词命中哪些行、跨几家厂商、有多少落在命名主体里。

    匹配口径与 want_row 完全一致（都走 runs_hit 的整词边界），否则"派生时用一套匹配、
    过滤时另一套"会出现派生说命中了、实际抓不到的分裂；扇出/离散度也会把碎片串味
    当成"这个词很宽"来算，判据随之失真（'mini' 曾被 'Gemini' 撑到跨 6 家）。
    """
    keys = [norm_runs(f"{r['name']} {r['creator']}") for r in aa_rows]
    # 命名主体 = 第一个括号/竖线之前的部分 + 厂商名。厂商名算主体，是因为
    # 'kimi' 这种产品名与厂商同名的词，只匹配 creator 也照样是该族的身份词。
    heads = [norm_runs(re.split(r"[(\[|·]", r["name"], maxsplit=1)[0] + " " + r["creator"])
             for r in aa_rows]
    out: dict[str, tuple[frozenset, int, float]] = {}
    for t in tokens:
        tk = norm_runs(t)
        hits = frozenset(i for i, k in enumerate(keys) if runs_hit(tk, k))
        if not hits:
            continue
        spread = len({aa_rows[i]["creator"] for i in hits})
        in_head = sum(1 for i in hits if runs_hit(tk, heads[i])) / len(hits)
        out[t] = (hits, spread, round(in_head, 3))
    return out


def derive_watchlist(aa_rows: list[dict], top_n: int = WATCHLIST_TOP_N,
                     max_fanout: float = MAX_FANOUT,
                     max_spread: int = MAX_CREATOR_SPREAD,
                     head_ratio: float = HEAD_RATIO) -> list[str]:
    """从 AA 榜单前 top_n 名的模型名里派生关键词，四条度量判据 + 贪心覆盖。

    候选词与命中都走 runs_hit 的**整词边界**（见 norm_runs）：'sol' 量出来只覆盖 GPT-6 Sol
    一族，不再被 'Solar Mini 4' 撑宽，所以扇出/离散度这两条判据量的是真宽度。

    · 长度 ≥3 且含字母 —— 纯版本号 token（'6'、'5.5'）没有字母，本来就不是身份词；
      而 'sol'、'glm' 这种 3 字母产品名必须留下，用 ≥4 会把名次 #16/#25 的家族整个丢掉；
    · 行扇出 ≤ max_fanout —— 太宽的词（'gpt'/'claude'/'openai'）丢掉；
      注意**不按词性判断**，先前"排除厂商名"想把 'kimi' 整族误杀了，因为 Kimi 的
      creator 就叫 Kimi、产品名与厂商同名；
    · 厂商离散度 ≤ max_spread —— 'max'(9 家)、'flash'(6 家)、'pro'(3 家) 这类档位/通用词
      自然出局，不需要人列黑名单；
    · 命名主体占比 ≥ head_ratio —— 'fallback'/'with' 只出现在 "(high with fallback)"
      这种档位后缀里（主体占比 0.00），不该当关键词；
    · 贪心覆盖：宽词优先（族名比单个变体名活得久），且每个词必须至少**新覆盖**一条
      前 N 名的行，否则是冗余词。上一版按这个规则跑出来是 15 个词、匹配 77 行，
      与手写的 21 个词覆盖完全同一批行 —— 少掉的都是 'v2.6'/'a95b' 这类被族名盖住的变体词。

    AA 主表通常按智能指数降序，但**不靠页面顺序**：内在排序由 rank_order 按分数重排，
    并列按竞赛式名次整体纳入（见 competition_ranks），所以同分的模型要么一起进、要么一起出。
    `top_n` 是生效名次线，抓取与刷新都先经 watchlist_boundary 算过（历史厂商可扩边）。
    """
    if not aa_rows:
        return []
    within = rows_within_rank(aa_rows, top_n)
    cands = {t for i in within for t in tokens_list(aa_rows[i]["name"])
             if len(t) >= 3 and re.search(r"[a-z]", t)}
    stats = token_stats(aa_rows, cands)
    fan_limit = max(4, int(len(aa_rows) * max_fanout))
    allowed = {t: v for t, v in stats.items()
               if len(v[0]) <= fan_limit and v[1] <= max_spread and v[2] >= head_ratio}

    need = set(within)
    kept: list[str] = []
    for t in sorted(allowed, key=lambda x: (-len(allowed[x][0]), x)):
        new = allowed[t][0] & need
        if new:
            kept.append(t)
            need -= new
    # 判据太严导致有行没被覆盖时，用该行能用的最窄词兜住 —— 覆盖前 N 名是硬要求，
    # 宁可多一个宽词，也不能让某个模型静默掉出表（那正是这次要修的毛病）。
    for i in sorted(need):
        own = sorted((t for t in stats if i in stats[t][0]),
                     key=lambda t: (len(stats[t][0]), stats[t][1], t))
        if own:
            kept.append(own[0])
    return sorted(set(kept))


def _watchlist_meta(aa_rows: list[dict], top_n: int, derived: list[str],
                    mode: str, added: list[str], boundary: dict | None = None) -> dict:
    """把"这次派生用了什么判据、每个词命中多少行"一起落盘。

    判据阈值必须写进配置：下次词表里冒出出人意料的词时，能直接看出是哪一条卡的，
    不用回来读源码猜。mode 区分 full（整表替换，来自 --refresh-watchlist）与
    incremental（抓取时的只加不减），两者留痕不同，事后能分清某次改动是谁做的。

    `top_n` 写**生效名次线**（可能是被历史厂商扩过的边），`base_n` 与 `expanded_by`
    同时落盘，才能事后分清"这次是 50 线"还是"某厂商把它拉长了"。
    """
    meta = {
        "at": now_str(), "mode": mode, "top_n": top_n,
        "derived_count": len(derived), "added": added,
        "criteria": {"max_fanout": MAX_FANOUT, "max_creator_spread": MAX_CREATOR_SPREAD,
                     "head_ratio": HEAD_RATIO, "fan_limit": max(4, int(len(aa_rows) * MAX_FANOUT)),
                     "rows_total": len(aa_rows)},
        "words": {t: {"rows": len(h), "creators": sp, "head": hd}
                  for t, (h, sp, hd) in token_stats(aa_rows, set(derived)).items()},
    }
    if boundary is not None:
        meta["base_n"] = boundary["base_n"]
        meta["expanded_by"] = boundary["expanded_by"]
        meta["vendors"] = boundary["vendors"]
    return meta


def resolve_watchlist(aa_rows: list[dict], base_n: int = WATCHLIST_TOP_N,
                      history_words=()) -> dict:
    """抓取增量与手工刷新共用的派生入口：先算生效名次线，再派生。

    两者必须走同一个函数，否则"抓取按 50 派生、刷新按扩边派生"会产出两套词表，
    差异没人解释得清（这正是原先 top_n 单点来源那条判据要守的东西）。
    """
    boundary = watchlist_boundary(aa_rows, base_n, history_words)
    return {**boundary, "derived": derive_watchlist(aa_rows, boundary["effective_top_n"])}


def refresh_watchlist(aa_rows: list[dict], top_n: int = WATCHLIST_TOP_N) -> dict:
    """重算 keyword_include 并落盘。

    派生结果**替换**旧列表，但 `keyword_include_pinned` 里的词永远并入、不会被冲掉 ——
    否则每次刷新都会把用户手动钉住的掉榜模型（如名次滑出前 50 的 Hy3）静默删掉。

    生效名次线按旧词表（历史关注词）扩边：掉出前 50 的老厂商仍能凭当前最高名次
    留在派生范围内，整表重算不会把它们静默剔除（见 watchlist_boundary）。
    """
    cfg = load_config()
    old = list(cfg.get("keyword_include", []))
    pinned = list(cfg.get("keyword_include_pinned", []))
    r = resolve_watchlist(aa_rows, top_n, set(old) | set(pinned))
    derived = r["derived"]
    cfg["keyword_include_pinned"] = pinned
    cfg["keyword_include"] = sorted(set(derived) | set(pinned))
    cfg["_watchlist_derived"] = _watchlist_meta(
        aa_rows, r["effective_top_n"], derived, "full",
        sorted(set(cfg["keyword_include"]) - set(old)), r)
    save_config(cfg)
    return {"old": old, "derived": derived, "new": cfg["keyword_include"],
            "dropped": sorted(set(old) - set(cfg["keyword_include"])),
            "base_n": r["base_n"], "effective_top_n": r["effective_top_n"],
            "expanded_by": r["expanded_by"]}



def grow_watchlist(aa_rows: list[dict], top_n: int = WATCHLIST_TOP_N) -> dict:
    """抓到新家族就当场把增量派生词并进 keyword_include —— **只加不减**。

    为什么必须有这一步：词表是按 AA 前 N 名派生的一次性快照，新模型进榜时旧词一条
    都不命中，它就静默掉出图表 —— 而 data_state 仍是 ok、缺口告警也不响（告警只认
    "上一轮有、这一轮没了"，新家族本来就没在上一轮）。10-09 的 Haiku 5.5 就是这么
    漏掉的，用户看到的是"数据每天在更新，但新模型永远不出现"。

    只做增量、不做删减：删词意味着"这个族不再盯了"，那是判断不是度量，
    留给 --refresh-watchlist 和人工。新增词同样要过 derive_watchlist 的四条判据，
    所以不会因为来了个宽名就把 Grok/Muse 这类没关注的行整体拉回表里。
    """
    cfg = load_config()
    old = list(cfg.get("keyword_include", []))
    pinned = list(cfg.get("keyword_include_pinned", []))
    kw_exc = list(cfg.get("keyword_exclude", []))
    # 历史关注词 = 当前词表 + pinned：它们命中的厂商按当前最高名次扩宽名次线，
    # 与 refresh_watchlist 走同一个 resolve_watchlist，两条路才不会产出不同的词表。
    r = resolve_watchlist(aa_rows, top_n, set(old) | set(pinned))
    derived = r["derived"]
    # 括号不能省：Python 里 `-` 比 `|` 结合更紧，写歪一格就变成"只减 pinned 那一支"。
    added = sorted((set(derived) | set(pinned)) - set(old))
    rows_before = sum(1 for r in aa_rows if want_row(r, old, kw_exc))
    boundary = {"base_n": r["base_n"], "effective_top_n": r["effective_top_n"],
                "expanded_by": r["expanded_by"], "vendors": r["vendors"]}
    if not added:
        return {"added": [], "old": old, "new": old, "derived": derived,
                "rows_before": rows_before, "rows_after": rows_before, **boundary}

    new_list = sorted(set(old) | set(derived) | set(pinned))
    cfg["keyword_include"] = new_list
    cfg["keyword_include_pinned"] = pinned
    cfg["_watchlist_derived"] = _watchlist_meta(
        aa_rows, r["effective_top_n"], derived, "incremental", added, r)
    text = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    # 落盘前先自证这份文本读得回来、且新增词确实进了列表；写完再按文件实际内容复核一次
    # ——配置是抓取和菜单栏共用的输入，半截或错位的词表会污染后面所有轮次。
    back = json.loads(text)
    if not set(added) <= set(back.get("keyword_include", [])):
        raise RuntimeError("写回前自检失败：新增词没进 keyword_include")
    save_config(cfg, text)
    on_disk = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if set(on_disk.get("keyword_include", [])) != set(new_list):
        raise RuntimeError(f"落盘校验失败：文件里是 {on_disk.get('keyword_include')}")
    rows_after = sum(1 for r in aa_rows if want_row(r, new_list, kw_exc))
    return {"added": added, "old": old, "new": new_list, "derived": derived,
            "rows_before": rows_before, "rows_after": rows_after, **boundary}


def zero_hit_words(aa_rows: list[dict], kw_inc: list[str]) -> list[str]:
    """在当前榜单上一行都不命中的关键词。

    边界匹配把"写错的词"从"顺带多捞几家"换成了"静默一行不捞"，这个代价必须报出来。
    它不看 keyword_exclude：被排除词挡住的行仍算命中（那是两回事，排除本来就是压制的语义）。
    """
    board = [norm_runs(f"{r['name']} {r['creator']}") for r in aa_rows]
    return sorted({w for w in kw_inc if not any(runs_hit(norm_runs(w), rs) for rs in board)})


def merge(aa_rows: list[dict], or_models: dict, dl_rows: list[dict],
          cfg: dict) -> list[dict]:
    kw_inc = cfg.get("keyword_include", [])
    kw_exc = cfg.get("keyword_exclude", [])

    dl_index: dict[frozenset, float] = {}
    for r in dl_rows:
        dl_index[frozenset(tokens(r["name"]) | tokens(r["creator"]))] = r["index"]

    out: list[dict] = []
    for row in aa_rows:
        if not want_row(row, kw_inc, kw_exc):
            continue
        orr = or_match(or_models, row) or {}
        reference = orr.get("models_dev") or {}
        input_price = orr.get("in_per_1m_usd")
        output_price = orr.get("out_per_1m_usd")
        price_source = "openrouter" if input_price is not None or output_price is not None else None
        if input_price is None:
            input_price = reference.get("in_per_1m_usd")
            if input_price is not None:
                price_source = "models.dev"
        if output_price is None:
            output_price = reference.get("out_per_1m_usd")
            if output_price is not None and price_source is None:
                price_source = "models.dev"
        creator_slug = re.sub(r"[^a-z0-9]", "", row["creator"].lower()) or "other"
        series, tier = split_series(row["name"])
        rec = {
            "key": sorted(tokens(row["name"]) | tokens(row["creator"])),
            "name": row["name"],
            "aa_slug": row.get("aa_slug"),
            # 能力线（同族同代次的档位阶梯）按这两个字段分组；分组在 Python 侧算完，
            # 两个渲染面只消费，免得"网页连了线、菜单栏没连"。
            "series": series,
            "series_key": norm_key(series),
            "tier": tier,
            "creator": row["creator"],
            "creator_slug": creator_slug,
            # 标识色由 apply_vendor_colors() 在 logo 下载完、配色探测之后统一派生
            # （那一步才看得见 SVG）。这里留空是为了让"漏派生"显式暴露成 None，
            # 而不是悄悄用一张手写表的灰兜底。
            "brand_color": None,
            "logo": row.get("logo") or "",
            "ctx": row["ctx"] or orr.get("ctx") or reference.get("ctx"),
            "index": row["index"],
            "evaluation_version": row.get("evaluation_version"),
            "apt_usd": row["apt_usd"],
            "tok_s": row["tok_s"],
            "ttft_s": row["ttft_s"],
            "total_s": row["total_s"],
            # 旧基准总响应缺失时的官方兜底：首回答延迟（含输入/排队等）。
            # 只在 total_s 为 None 时被渲染层采用，两者不合并、不互相覆盖。
            "first_answer_s": row.get("first_answer_s"),
            # 每任务耗时在 merge 之后由 attach_task_times 按 aa_slug 唯一匹配填入。
            "time_per_task_s": row.get("time_per_task_s"),
            "output_tokens_per_task": row.get("output_tokens_per_task"),
            "in_per_1m_usd": input_price,
            "out_per_1m_usd": output_price,
            "price_source": price_source,
            "or_id": orr.get("id"),
            "or_modalities": orr.get("modalities") or [],
            "models_dev": orr.get("models_dev"),
            "dl_index": dl_index.get(frozenset(tokens(row["name"]) | tokens(row["creator"]))),
        }
        out.append(rec)

    out.sort(key=lambda r: (-(r["index"] or 0), r["name"]))
    return out


def _series_root(key: str) -> str:
    """同词根（跨代次）判定：把数字去掉，让 gpt6sol 与 gpt61sol 归到 gptsol。

    短于 4 个字符的词根没有区分度（"gpt" 会把所有 OpenAI 模型拉成一族），直接弃用。
    """
    root = re.sub(r"\d+", "", key)
    return root if len(root) >= 4 else ""


_EST_LABEL = {"series-interp": "同族相邻档插值",
              "series-anchor": "同族单侧锚点",
              "family-anchor": "跨代次同词根中位"}
# 第四档：同级能力参照带。**只在上面三档全落空时才算**，而且只给区间、不给中心值。
# 理由：前三档说的是"这个模型自己的产品线有多快"，这一档说的是"别家能力相当的模型多快"，
# 不是同一个命题；给个中位数会被读成测量结果，而它真正的意思是"别人是 56–111"。
PEER_WINDOW = 2         # 智能指数 ±2 分。用窗口而不是"k 个最近邻"：k 会随样本密度漂
PEER_MIN_ROWS = 3       # 窗内不足 3 个实测就别画带 —— 一两个点撑不起"区间"这个说法


def estimate_missing_tok_s(models: list[dict]) -> tuple[int, int]:
    """给缺 tok_s 的行补推估。返回 (给了点估计的行数, 只给了参照带的行数)。

    口径全部由数据推出来，没有手写档位表：
      · 同一族里智能指数是单调的推理投入档（实测 GPT-6 Sol：非推理 28 < low 34 <
        medium 40 < high 43 < xhigh 44 < max 48），所以"我落在哪两个已测档之间"
        由 index 决定 —— 不解析档位名，也就不必维护一张会过期的表。
      · 两侧都有已测档 → 按 index 线性插值（series-interp）。
      · 只有一侧 → 取那一侧（series-anchor）。
      · 同族一个已测都没有 → 跨代次同词根的中位（family-anchor）。
      · 前三档全落空 → 只画"同级能力参照带"（智能指数 ±PEER_WINDOW 分的实测范围），
        **不给中心值**，写进 tok_s_band 而不是 tok_s_est（见 PEER_WINDOW 上方注释）。
      · 连参照带都凑不出（窗内 <PEER_MIN_ROWS 行）→ 什么都不给，图上仍是「无数据」占位环。
    区间一律取**参照集合的实测散布**，不做人为收窄：点估计只是点，环带才是不确定度。
    族内只有一个参照时（同族散布退化成一点）改用同档位全体的散布。
    """
    measured = [m for m in models
                if isinstance(m.get("tok_s"), (int, float)) and m["tok_s"] > 0]
    if not measured:
        return 0, 0
    by_series: dict[str, list[dict]] = {}
    by_root: dict[str, list[dict]] = {}
    by_creator: dict[str, list[float]] = {}
    for m in measured:
        by_series.setdefault(str(m.get("series_key") or ""), []).append(m)
        by_root.setdefault(_series_root(str(m.get("series_key") or "")), []).append(m)
        by_creator.setdefault(str(m.get("creator") or ""), []).append(m["tok_s"])
    all_lo = min(m["tok_s"] for m in measured)
    all_hi = max(m["tok_s"] for m in measured)

    done = 0
    banded = 0
    for m in models:
        if isinstance(m.get("tok_s"), (int, float)) and m["tok_s"] > 0:
            continue
        idx = m.get("index")
        key = str(m.get("series_key") or "")
        sibs = by_series.get(key, [])
        est: float | None = None
        basis = ""
        n = 0
        lo: float | None = None
        hi: float | None = None
        band = ""
        if idx is not None and sibs:
            vals = [s["tok_s"] for s in sibs]
            lo, hi = min(vals), max(vals)
            band = "同族"
            below = [s for s in sibs if s["index"] is not None and s["index"] < idx]
            above = [s for s in sibs if s["index"] is not None and s["index"] > idx]
            if below and above:
                b = max(below, key=lambda s: s["index"])
                a = min(above, key=lambda s: s["index"])
                est = b["tok_s"] + (a["tok_s"] - b["tok_s"]) * (idx - b["index"]) / (a["index"] - b["index"])
                basis, n = "series-interp", 2
            else:
                side = below or above
                near = (max(side, key=lambda s: s["index"]) if below
                        else min(side, key=lambda s: s["index"]))
                est, basis, n = near["tok_s"], "series-anchor", 1
        else:
            root = _series_root(key)
            kin = sorted(x["tok_s"] for x in by_root.get(root, [])
                         if str(x.get("series_key")) != key)
            if root and kin:
                est = kin[len(kin) // 2]
                basis, n = "family-anchor", len(kin)
                lo, hi, band = kin[0], kin[-1], "同词根"
        if est is None:
            # 前三档全落空 → 退到"同级能力参照带"。只写 tok_s_band 这套字段，**不碰
            # tok_s_est**：所有按"有推估点"行事的判据（虚线环、表格「估」、前沿排除）
            # 都不该把一条区间当成一个值。
            if idx is not None:
                win = [x for x in measured if x.get("index") is not None
                       and abs(x["index"] - idx) <= PEER_WINDOW]
                vals = sorted(float(x["tok_s"]) for x in win)
                if len(vals) >= PEER_MIN_ROWS:
                    same = sum(1 for x in win if x.get("creator") == m.get("creator"))
                    m["tok_s_band"] = [vals[0], vals[-1]]
                    m["tok_s_band_n"] = len(vals)
                    m["tok_s_band_same_creator"] = same
                    m["tok_s_band_note"] = (
                        f"同级能力参照：智能指数 ±{PEER_WINDOW} 分的 {len(vals)} 个实测模型是 "
                        f"{display_num(vals[0], DISPLAY_DECIMALS['tok_s'])}–"
                        f"{display_num(vals[-1], DISPLAY_DECIMALS['tok_s'])} tok/s（其中同厂商 {same} 个）。"
                        "刻意不给中心值：这不是本族推估，是别家同级能力的实测范围。")
                    banded += 1
            continue
        # 区间回退顺序：同族实测散布 → 同厂商实测散布 → 全体实测散布。
        # ⚠️ 不能用 tier：那个字段是**推理投入档**（medium/high），而吞吐对 effort
        # 几乎不敏感，所以同 effort 的散布是个假窄区间，实测里就出现过
        # "区间 [46,76] 却把点估计 239 排除在外"的结果 —— 环带画在点里面，比没有更糟。
        # 任何情况下区间都必须含住点估计（下面强制收敛一次）。
        if lo is None or hi == lo or hi < lo:
            vals = by_creator.get(str(m.get("creator") or ""), [])
            if len(set(vals)) >= 2:
                lo, hi, band = min(vals), max(vals), "同厂商"
            else:
                lo, hi, band = all_lo, all_hi, "全体"
        lo = min(float(lo), float(est))
        hi = max(float(hi), float(est))
        m["tok_s_est"] = round(float(est), 1)
        m["tok_s_est_basis"] = basis
        m["tok_s_est_range"] = [round(float(lo), 1), round(float(hi), 1)]
        m["tok_s_est_n"] = n
        m["tok_s_est_note"] = f"估计：{_EST_LABEL[basis]}（{n} 个参照，区间取{band}实测散布）"
        done += 1
    return done, banded


def derive_missing_task_times(models: list[dict]) -> tuple[int, int]:
    """用 AA 的每任务输出量除以 TPS 结果，派生缺失的任务耗时。

    层级与 estimate_missing_tok_s 完全一致：
      · 有实测 tok_s → 输出量 / 实测速度；
      · 有同族插值/锚点/同词根锚点的 tok_s_est → 输出量 / 点估计，区间由 tok_s_est_range 反比换算；
      · 只有同级能力参照带 tok_s_band → 只给时间带，不给中心值。
    不读价格：总成本混了输入、缓存读/写与输出，除以输出单价会把输出量高估数倍。
    派生值单独打 time_per_task_est，AA 已给的 time_per_task_s 永不被覆盖。
    """
    done = banded = 0
    for m in models:
        if m.get("time_per_task_s") is not None:
            continue
        tokens = task_output_tokens(m.get("output_tokens_per_task"))
        if tokens is None:
            continue
        measured = m.get("tok_s")
        est = m.get("tok_s_est")
        if isinstance(measured, (int, float)) and not isinstance(measured, bool) and measured > 0:
            m["time_per_task_est_s"] = tokens / measured
            m["time_per_task_est"] = True
            m["time_per_task_est_basis"] = "measured-speed"
            done += 1
        elif isinstance(est, (int, float)) and not isinstance(est, bool) and est > 0:
            m["time_per_task_est_s"] = tokens / est
            m["time_per_task_est"] = True
            m["time_per_task_est_basis"] = m.get("tok_s_est_basis")
            rng = m.get("tok_s_est_range")
            if rng:
                m["time_per_task_est_range"] = [tokens / rng[1], tokens / rng[0]]
            done += 1
        elif isinstance(m.get("tok_s_band"), (list, tuple)) and len(m["tok_s_band"]) == 2:
            lo, hi = m["tok_s_band"]
            m["time_per_task_band"] = [tokens / hi, tokens / lo]
            banded += 1
    return done, banded


# ------------------------------------------------------------------- 梯队与性价比（图例排序）

TIER_TOLERANCE_POINTS = 1   # 梯队宽度：从最高分起，与**本梯队最高分**差 ≤1 的模型算同一梯队


def plotted_models(models: list[dict]) -> list[dict]:
    """图上真有位置的点：有分也有每任务成本。两张渲染面筛的是同一批，口径写在一处。"""
    return [m for m in models if m.get("index") is not None and m.get("apt_usd")]


def tier_ladder(models: list[dict]) -> list[dict]:
    """动态梯队：把去重分数从高到低排，从最高分开始往下"吃"，与本级最高分差 ≤1 的并进来，
    窗口用尽就从下一个分数重新起一级。返回 [{rank, high, low}]，rank 0 是最高梯队。

    窗口钉在**起级那个分数**上（不是滚动比较相邻两个分数）：滚动比较会让一条每分一档的
    连续阶梯被一路并成巨档（今天 54–38 全成一档），梯队就失去意义了。
    实测今天这份数据（85 模型 / 39 个去重分数）切出 22 级，多数级只有 1–2 分宽。
    """
    vals = sorted({m["index"] for m in models if m.get("index") is not None}, reverse=True)
    bands: list[dict] = []
    for v in vals:
        if bands and bands[-1]["high"] - v <= TIER_TOLERANCE_POINTS:
            bands[-1]["low"] = v
        else:
            bands.append({"rank": len(bands), "high": v, "low": v})
    return bands


def tier_of(index, bands: list[dict]):
    """给一个分数定档。分数本来就都来自这张表，落在档外/断档间隙里时就近归入，
    两边一样远时算高一档 —— 结果必须是确定的，不能取决于遍历顺序。"""
    if index is None or not bands:
        return None
    for b in bands:
        if b["low"] <= index <= b["high"]:
            return b["rank"]
    if index > bands[0]["high"]:
        return 0
    if index < bands[-1]["low"]:
        return bands[-1]["rank"]
    best, best_gap = None, None
    for b in bands:
        gap = min(abs(index - b["high"]), abs(index - b["low"]))
        if best_gap is None or gap < best_gap:      # 同距离时保留先出现的（更高档）
            best, best_gap = b["rank"], gap
    return best


def pareto_frontier(models: list[dict]) -> list[dict]:
    """Pareto 前沿：按成本升序扫，被「更便宜且分不低于自己」的点支配就出局。

    **推估点与参照带都不参与** —— 支配关系是这张图的硬结论，不能让一个插值出来的值
    或一条"别家同级能力的范围"去改写它（带连中心值都没有，没有比较资格）。

    这段判据原先在 `render_scatter_svg.py`（Python）和 `dashboard.html`（JS）各写一遍，
    同一份数据要靠两处代码保持一致；现在爬虫算完、两侧只消费同一个结论（与 series /
    brand_color / data_state 同一惯例）。
    """
    real = [m for m in plotted_models(models)
            if not m.get("tok_s_est") and not m.get("tok_s_band")]
    front: list[dict] = []
    for m in sorted(real, key=lambda z: (z["apt_usd"], -z["index"], z["name"])):
        if not any(q["index"] >= m["index"] for q in front):
            front.append(m)
    return front


def _seg_dist(p: tuple[float, float], a: tuple[float, float],
              b: tuple[float, float]) -> float:
    """点到线段 ab 的最短距离（退化成一个点时就是欧氏距离）。"""
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def annotate_efficiency(models: list[dict], bands: list[dict] | None = None) -> list[dict]:
    """给每个模型写 `on_frontier` / `dist_front` / `cap_band`，返回前沿点列表。

    距离口径（图例排序的第二指标）：
    · 先按这批可画点把两轴归一化 —— x 用 **log10(每任务成本)**（图就是 log 轴），
      y 用智能指数，各自 min–max 拉到 0–1；
    · 再取"到前沿折线的最短欧氏距离"：相邻前沿点连成线段，前沿上的点恒为 0。
      用折线而不是"到最近的前沿点"，是因为一个"分数与前沿持平、只是更贵"的模型
      应该按横向价差算，而不是被对角线上那个拐角拉远。
    · 归一化用的是**数据域**而不是像素轴：两张面的像素各带 padding，用像素会把同一个
      模型算出两个距离。

    `cap_band` 由 `tier_ladder` 从当轮全部分数里量出来（相邻分差 >3 才换档），
    只有距离需要成本：不可画的点 `dist_front` 为 None、`on_frontier` 为 False，
    它们在图例里不进距离排序、垫在最后。
    """
    ms = plotted_models(models)
    front = pareto_frontier(models)
    front_ids = {id(m) for m in front}
    bands = tier_ladder(models) if bands is None else bands
    if not ms:
        for m in models:
            m["on_frontier"] = False
            m["dist_front"] = None
            m["cap_band"] = tier_of(m.get("index"), bands)
        return front

    lx = [math.log10(m["apt_usd"]) for m in ms]
    xs, xe = min(lx), max(lx)
    ys, ye = min(m["index"] for m in ms), max(m["index"] for m in ms)
    span_x = (xs - xe) or 1.0      # 只有一个点时别让除法炸掉
    span_y = (ys - ye) or 1.0
    # 按 id 建索引：这些 dict 内容可能一模一样（同族同价同分），用 == 找位置会把两个点
    # 算成同一个，必须认对象不认内容。
    coord = {id(m): (((math.log10(m["apt_usd"]) - xs) / span_x),
                     ((m["index"] - ys) / span_y)) for m in ms}
    fpts = [coord[id(m)] for m in front]
    plotted_ids = set(coord)

    for m in models:
        m["cap_band"] = tier_of(m.get("index"), bands)
        if id(m) not in plotted_ids:
            m["on_frontier"] = False
            m["dist_front"] = None
            continue
        p = coord[id(m)]
        m["on_frontier"] = id(m) in front_ids
        if len(fpts) == 1:
            m["dist_front"] = round(math.hypot(p[0] - fpts[0][0], p[1] - fpts[0][1]), 4)
        else:
            m["dist_front"] = round(min(
                _seg_dist(p, fpts[i], fpts[i + 1]) for i in range(len(fpts) - 1)), 4)
    return front


def task_size_penalty(models: list[dict]) -> dict:
    """全局低分收缩 λ：爬虫侧算一次，看板与状态栏都只消费这一份。

    只拿**画得出来的点**（有分有成本）进拟合：算进看不见的行，归一化域与 λ
    就会跟着看不见的数据漂，同一批可见数据两次抓取能给出两个收缩强度。
    `family` 用 `series_key`（同族同代次），与能力线同一分组口径。

    明确不做推理的档位（non-reasoning / none）在拟合**之前**剔除：它们本来就不该
    被"分数低就缩小"这条同族规律解释。它们仍照常上图、照常吃同一个全局倍率。
    """
    rows = [{
        "family": m.get("series_key") or m.get("name") or "",
        "name": m.get("name") or "",
        "tier": m.get("tier") or "",
        "index": m.get("index"),
        "tok_s": m.get("tok_s"),
        # 面积编码的域与渲染面一致：官方优先，缺失才用派生估计点。
        "task_s": m.get("time_per_task_s"),
        "task_est_s": m.get("time_per_task_est_s"),
    } for m in plotted_models(models)]
    return task_size.fit_task_size_penalty(rows)


def build_legend(models: list[dict], bands: list[dict] | None = None,
                 usage_available: bool | None = None) -> list[dict]:
    """图例顺序：**梯队（rank 0 最高）升序 → 同梯队内加权离前沿距离升序 → 厂商名**。

    梯队取该厂**可画点**（有分也有每任务成本）的最高分所在档：一家厂商摆 20 个档位时，
    它"处在什么水平"由图上最能打的那个决定。AA 里存在有分但缺成本/吞吐的行（成本列是
    `--`，例如 `Muse Wonder Full (max)` 56 分），它们不在图上，不能拿 56 去抬厂商梯队 ——
    否则图例说第 1 梯队、图上最高点却是 52 分。全部行的最高分另存 `best_index_all` 供审计。
    档本身不写死宽度，由 `tier_ladder` 从当轮全部分数量出来（相邻分差 >3 才换档）。

    第二指标是**加权平均距离**，不是累加：
        Σ(index × log1p(平衡用量) × dist) / Σ(index × log1p(平衡用量))
    平衡用量由 `attach_usage` 算好（token 占比 × 请求占比）。指数保留"高分模型更受关注"
    的语义，log1p 压住单一爆款模型的用量寡头效应，最后除以权重和，结果仍是 0–1 的距离。

    `usage_available=True` 时，没有用量数据的行**不进分子也不进分母**；某厂一行都没有
    用量时 `dist_weighted=None`，在同梯队垫底。`usage_available=False`（用量源整体失败
    或调用方没启用）时退回旧的指数加权，保证图例仍有档内名次。None 表示按数据自动判断。
    """
    bands = tier_ladder(models) if bands is None else bands
    if usage_available is None:
        usage_available = any(m.get("usage_balanced") is not None for m in models)
    basis = {id(m) for m in plotted_models(models)}
    agg: dict[str, dict] = {}
    for m in models:
        name = (m.get("creator") or "其他").strip() or "其他"
        a = agg.setdefault(name, {"band": None, "best": None, "best_all": None,
                                  "n": 0, "np": 0,
                                  "sw": 0.0, "sd": 0.0, "du": [], "du_n": 0, "uw": 0})
        idx = m.get("index")
        if idx is not None and (a["best_all"] is None or idx > a["best_all"]):
            a["best_all"] = idx
        if id(m) in basis:
            a["np"] += 1
            if idx is not None and (a["best"] is None or idx > a["best"]):
                a["best"] = idx
                a["band"] = tier_of(idx, bands)
        d = m.get("dist_front")
        a["n"] += 1
        if d is not None and idx is not None:
            a["du"].append(d)
            a["du_n"] += 1
            if usage_available:
                usage = m.get("usage_balanced")
                if usage is None or usage <= 0:
                    continue
                weight = idx * math.log1p(usage)
                a["uw"] += 1
            else:
                weight = idx
            a["sw"] += weight
            a["sd"] += weight * d
    out = []
    for name, a in agg.items():
        out.append({
            "creator": name,
            "band": a["band"],
            "best_index": a["best"],
            "best_index_all": a["best_all"],
            "models": a["n"],
            "plotted": a["np"],
            "usage_rows": a["uw"],
            "dist_weighted": round(a["sd"] / a["sw"], 4) if a["sw"] > 0 else None,
            "dist_mean": round(sum(a["du"]) / a["du_n"], 4) if a["du_n"] else None,
        })
    # 没分的厂商（band=None）排到最后，按名字；rank 越小越高，所以直接正序
    return sorted(out, key=lambda r: (r["band"] is None,
                                      r["band"] if r["band"] is not None else 1 << 30,
                                      r["dist_weighted"] if r["dist_weighted"] is not None else 9e9,
                                      r["creator"]))


# --------------------------------------------------------------------------- SQLite 历史

def ensure_db() -> sqlite3.Connection:
    mr_paths.ensure_dirs()
    con = sqlite3.connect(DB_PATH)
    # 手动「立即抓取」与 launchd 每日任务可能并发写库：默认锁等待仅 5s，撞上就是
    # OperationalError、整次抓取作废。busy_timeout 拉长等待，WAL 让读写不互斥。
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            ts        TEXT NOT NULL,
            source    TEXT NOT NULL,
            n_rows    INTEGER,
            ok        INTEGER,
            err       TEXT,
            payload   TEXT,
            PRIMARY KEY (ts, source)
        )
    """)
    # 旧库是 `ts TEXT PRIMARY KEY`（单列）：一次抓取要写 AA / OpenRouter / 合并基线三份，
    # 同一秒落两条就会 INSERT OR REPLACE 把前一条悄悄删掉 —— 表现为"降级读不到上一次快照"，
    # 而且没有任何报错。这里做无损迁移；不做的话新加的 diffbase 会把撞车概率再抬高一份。
    ddl = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='snapshots'").fetchone()
    if ddl and "PRIMARY KEY (ts, source)" not in " ".join((ddl[0] or "").split()):
        con.executescript("""
            BEGIN;
            CREATE TABLE snapshots_new(
                ts TEXT NOT NULL, source TEXT NOT NULL, n_rows INTEGER,
                ok INTEGER, err TEXT, payload TEXT, PRIMARY KEY (ts, source));
            INSERT OR IGNORE INTO snapshots_new
                SELECT ts, source, n_rows, ok, err, payload FROM snapshots;
            DROP TABLE snapshots;
            ALTER TABLE snapshots_new RENAME TO snapshots;
            COMMIT;
        """)
        log("SQLite：snapshots 主键已从 (ts) 迁移到 (ts, source)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS series (
            ts        TEXT NOT NULL,
            name      TEXT NOT NULL,
            field     TEXT NOT NULL,
            value     REAL,
            PRIMARY KEY (ts, name, field)
        )
    """)
    # 来源：2026-10-28 用户要求按评测代次管理历史，原始 payload 不覆盖。
    for table in ("snapshots", "series"):
        columns = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if "evaluation_version" not in columns:
            con.execute(f"ALTER TABLE {table} ADD COLUMN evaluation_version TEXT")
    con.commit()
    return con


def save_snapshot(con: sqlite3.Connection, source: str, n_rows: int,
                  ok: bool, err: str | None, rows: list[dict] | None) -> None:
    con.execute("INSERT OR REPLACE INTO snapshots(ts,source,n_rows,ok,err,payload,evaluation_version) "
                "VALUES(?,?,?,?,?,?,?)",
                (now_str(), source, len(rows) if rows else 0, int(ok), err,
                 json.dumps(rows, ensure_ascii=False) if rows else None,
                 version_of(rows or [])))
    con.commit()


def prune_history(con: sqlite3.Connection, keep_days: int = KEEP_SNAPSHOT_DAYS) -> tuple[int, int]:
    """只保留最近 keep_days 天的快照与时间序列，其余删掉。

    为什么必须有：两张表都是**每次抓取整份复制**（AA 120 行 + OpenRouter 上千条 + 6 字段
    × 74 模型的 series），一天一跑，没有清理就是无限增长 —— 实测本机已 9.8MB。
    90 天够趋势图与"7 天前基线"用；再老的东西看板不读，只是占地方。

    按**天**而不是按"抓取次数"算：一次抓取会留下 AA / OpenRouter / 合并基线 三个不同的 ts
    （各差几秒），拿 ts 条数当次数会把"保留 90 次"悄悄缩成约 30 次（第一版就是这么写的）。
    不主动 VACUUM：那一下要把整库重写，在定时任务里跑不划算；要收缩文件请手工执行。
    """
    from datetime import datetime, timedelta
    newest = con.execute("SELECT MAX(ts) FROM snapshots").fetchone()[0]
    if not newest:
        return 0, 0
    try:
        cut = (datetime.strptime(newest, "%Y-%m-%d %H:%M:%S") - timedelta(days=keep_days)
               ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0, 0
    n1 = con.execute("DELETE FROM snapshots WHERE ts < ?", (cut,)).rowcount
    n2 = con.execute("DELETE FROM series WHERE ts < ?", (cut,)).rowcount
    con.commit()
    return n1, n2


def last_good_snapshot(con: sqlite3.Connection, source: str = SRC_AA) -> tuple[list[dict], str | None]:
    """按来源取最近一次成功的原始行列表与时间，不混用不同来源。"""
    cur = con.execute("SELECT ts, payload FROM snapshots WHERE ok=1 AND payload IS NOT NULL AND source=? "
                      "ORDER BY ts DESC LIMIT 1", (source,))
    row = cur.fetchone()
    if not row:
        return [], None
    try:
        rows = json.loads(row[1])
    except (json.JSONDecodeError, TypeError):
        return [], None
    return (rows if isinstance(rows, list) else []), row[0]


def save_diff_base(con: sqlite3.Connection, models: list[dict]) -> None:
    """把本次**合并结果**里要比的那几个字段存成下一轮的基线。

    只存 name + 6 个数值（74 行 ≈ 几 KB），不存整份 payload：diff 只需要这几个数，
    而 snapshots 里 AA/OpenRouter 两份原始快照已经够大了。
    """
    # creator 也要存：基线要按当前 watchlist 再筛一遍（否则历史上没关注的行会被报成「消失」），
    # 而筛选判据看的是 name + creator 的 token。
    rows = [{"name": m["name"], "creator": m.get("creator", ""),
             **{f: m.get(f) for f in DIFF_FIELDS},
             **{a: m.get(a) for a in DIFF_ATTR}} for m in models]
    con.execute("INSERT OR REPLACE INTO snapshots(ts,source,n_rows,ok,err,payload,evaluation_version) VALUES(?,?,?,?,?,?,?)",
                (now_str(), SRC_DIFF, len(rows), 1, None, json.dumps(rows, ensure_ascii=False),
                 version_of(models)))


def baseline_map(rows: list[dict], cfg: dict, ts: str = "",
                 rules: list[dict] | None = None) -> dict[str, dict]:
    """把一条 diffbase 快照变成 diff 用的基线，并按**当前词表**再筛一遍。

    历史库里可能有旧版本留下的未过滤行，也可能在词表变窄（--refresh-watchlist、改
    `keyword_include_pinned`）之后多出今天已经不关注的行；不筛就会被当成有基数可比，
    报出生产侧根本不会报的变动。`load_previous` 与"往前翻"必须同一份口径，所以只这一处。
    """
    prev = {r["name"]: r for r in rows if isinstance(r, dict) and r.get("name")}
    # 隔离必须在**这里**做：三条比较路径（本轮 diff、往前翻、累计）都从本函数拿基线，
    # 少做一处就会让同一条脏数据在另一条路径上继续算涨跌。
    # 剥在副本上：调用方还要拿原快照算整族视角，就地改写会连带把"整族一起动了多少"改错。
    if ts and prev:
        stripped = [{"name": k, **v} for k, v in prev.items()]
        if apply_quarantine(stripped, ts, rules, where="baseline", quiet=True):
            prev = {r["name"]: {f: val for f, val in r.items() if f != "name"}
                    for r in stripped}
    kw_inc = cfg.get("keyword_include", [])
    kw_exc = cfg.get("keyword_exclude", [])
    if kw_inc:
        prev = {k: v for k, v in prev.items()
                if want_row({"name": k, "creator": v.get("creator", "")}, kw_inc, kw_exc)}
    return prev


BACKTRACK_MAX_ROUNDS = 40      # 往前最多翻多少轮合并基线（找不到就如实说翻了多少轮）


def move_keys(moves: list[dict]) -> list:
    """一批变动里"同一件事"的指纹：**哪个模型 + 哪个字段**，不含具体数值（排序后给出，稳定可比）。

    历史那一轮要按这个判"有没有真的换了一件事"：同一个模型的速度跳变在相邻两轮里
    写成 -25.3% 和 -27.9% 是同一件事，逐字符比会把一次变更说成两次。
    """
    return sorted({f"{m.get('kind') or m.get('field') or ''}|{m.get('permaslug') or m.get('name') or ''}"
                   for m in (moves or []) if isinstance(m, dict) and m.get("text")})


def last_notable_round(con: sqlite3.Connection, moves: list[dict], cfg: dict,
                       skip_keys=(), metric: str | None = None) -> dict:
    """最近一次有达门槛变动的轮次；"哪一轮、跟哪一轮比"都取台账里那两条快照的 ts。

    调用点必须在**本轮基线落库之后**（`save_diff_base` 之后）：这样台账第 0 条就是本轮。
    本轮这一对**不重放** —— 生产已经用真榜算过一次（`load_previous` 还会按当前词表把旧基线
    再筛一遍，重放不一定同形），所以 `moves` 非空就直接报 steps=0，为空才从第 1 对往后翻。

    `skip_keys`：本轮浮层已经会念到的**事件**（`move_keys` 的指纹：模型 + 字段，不含数值）。
    翻到的轮次若事件全在其中就接着往前 —— "上次提示"要回答"上一次真动了什么"，把同一件事
    再说一遍等于没说；而同一件事的数值在两轮之间漂一点是常态，所以**比指纹不比字符串**。
    一路都撞在同一批事件上就如实报 `all_duplicated`，不硬找也不编。

    `moves` 非空（steps=0，就是本轮）这一支不挂整族尾巴：本轮的明细另有 `payload["move_lines"]`
    带尾巴地念一遍，而浮层只在 `steps>=1` 时才用这里的 lines —— 在这里再算一遍没有读者。
    """
    try:
        rows = con.execute("SELECT ts, payload FROM snapshots WHERE source=? AND ok=1 "
                           "AND payload IS NOT NULL ORDER BY ts DESC LIMIT ?",
                           (SRC_DIFF, BACKTRACK_MAX_ROUNDS)).fetchall()
    except sqlite3.Error:
        return {}
    if not rows:
        return {}
    if moves:
        return {"at": rows[0][0], "base": rows[1][0] if len(rows) > 1 else "", "steps": 0,
                "n": len(moves), "items": [m["text"] for m in moves if m.get("text")],
                "lines": merged_move_lines(moves)}
    skip = {str(x) for x in (skip_keys or ()) if x}
    dup = 0
    dup_at = ""
    for i in range(1, len(rows) - 1):        # i=0 那一对就是本轮，刚算过，不重放
        try:
            cur = json.loads(rows[i][1])
            prev = json.loads(rows[i + 1][1])
        except (json.JSONDecodeError, TypeError):
            continue                          # 坏行跳过，别把整条回溯带崩
        if (not same_evaluation_major(version_of(cur), version_of(prev))
                or (cfg.get("evaluation_version")
                    and not same_evaluation_major(version_of(cur), cfg["evaluation_version"]))):
            continue
        # 整族视角要的是这一对的**全部**档，不是过门槛的那几条
        cur = [{**value, "name": name} for name, value in
               baseline_map(cur, cfg, ts=str(rows[i][0])).items()]
        chg = diff_models(cur, baseline_map(prev, cfg, ts=str(rows[i + 1][0])))
        mv = notable_moves(chg, metric)
        if not mv:
            continue
        # items 逐条原样带出去：浮层要能列全，"…等 N 项"那种截断只属于通知横幅
        lines = [str(x) for x in (merged_move_lines(mv, chg) or
                                  [m["text"] for m in mv if m.get("text")]) if x]
        keys = move_keys(mv)
        if skip and keys and set(keys) <= skip:    # 全是本轮已经说过的那几件事
            dup += 1
            dup_at = dup_at or rows[i][0]
            continue
        return {"at": rows[i][0], "base": rows[i + 1][0], "steps": i,
                "n": len(mv), "items": [m["text"] for m in mv],
                "lines": lines, "dup_skipped": dup, "keys": keys}
    if dup:
        return {"at": dup_at, "base": "", "steps": None, "n": 0, "items": [], "lines": [],
                "all_duplicated": dup, "scanned": max(0, len(rows) - 2),
                "cap": BACKTRACK_MAX_ROUNDS}
    return {"at": "", "base": "", "steps": -1, "n": 0, "items": [],
            "scanned": max(0, len(rows) - 2), "cap": BACKTRACK_MAX_ROUNDS}


CUMULATIVE_MAX_ROUNDS = 12     # 累计对比最多往前比多少轮（相邻那轮由 moves 管，这里从第 1 轮后开始看）


def cumulative_moves(con: sqlite3.Connection, models: list[dict], cfg: dict,
                     cap: int | None = None, metric: str | None = None) -> dict:
    """本轮 vs 往前第 k 轮**逐个** diff，第一个过门槛的就停下并报出轮距。

    为什么要单独一路：门槛量的是"相邻两轮的差"，而单价、速度这类字段常是每轮动 3~8%
    的慢漂移 —— 相邻比永远不过门槛，攒三五轮其实已经变了一大截。这里固定用**生产算好的
    `models`** 当本轮（不重放，避免与真榜不同形），基线走 `baseline_map`（与 load_previous 同口径）。
    """
    cap = CUMULATIVE_MAX_ROUNDS if cap is None else cap
    try:
        rows = con.execute("SELECT ts, payload FROM snapshots WHERE source=? AND ok=1 "
                           "AND payload IS NOT NULL ORDER BY ts DESC LIMIT ?",
                           (SRC_DIFF, cap + 1)).fetchall()
    except sqlite3.Error:
        return {}
    for k in range(1, len(rows)):            # rows[0] 就是本轮（调用点在本轮落库之后）
        try:
            prev = baseline_map(json.loads(rows[k][1]), cfg, ts=str(rows[k][0]))
        except (json.JSONDecodeError, TypeError):
            continue
        if not same_evaluation_major(version_of(models), version_of(prev.values())):
            continue
        chg = diff_models(models, prev)                     # 同上：整族视角要全部档，不只过门槛的
        mv = notable_moves(chg, metric)
        if mv:
            # keys 也带出去：回溯要靠"哪件事"判重复，累计块念过的那几件事不能再当历史答案
            # lines_plain = 不带整族尾巴的那一份，只给通知横幅用（见 cumulative_text）
            return {"at": rows[k][0], "steps": k, "n": len(mv),
                    "items": [m["text"] for m in mv], "lines": merged_move_lines(mv, chg),
                    "lines_plain": merged_move_lines(mv),
                    "keys": move_keys(mv)}
    return {}


def cumulative_text(cum: dict) -> str:
    """累计变动的通知正文：先说清"跟哪一轮比、隔了几轮"，再列前 3 项。

    取 `lines_plain`（不带整族尾巴那份）：横幅有 200 字硬上限，尾巴一条上百字，
    带上就把"到底动了什么"挤出屏幕 —— 明细与"整族一起动了多少"属于浮层。
    """
    lines = cum.get("lines_plain") or cum.get("lines") or cum.get("items") or []
    shown = lines if len(lines) <= 3 else lines[:3] + [f"…等另 {len(lines) - 3} 条"]
    return (f"相对 {cum.get('at')}（往前第 {cum.get('steps')} 轮）累计有 {cum.get('n', len(lines))} 项达门槛："
            + "；".join(shown))[:200]


def load_previous(con: sqlite3.Connection) -> tuple[dict[str, dict], bool]:
    """取上一次合并结果里每个模型的字段，用于算变动。返回 (基线, 是否首次建基线)。

    基线缺失时**不能**返回空 map 就继续 diff —— 那会把全部模型报成「新进榜」，
    一眼看着像榜单大洗牌。这时本轮不报变动，只说明"没有可比的上一次"。
    """
    cur = con.execute("SELECT ts, payload FROM snapshots WHERE ok=1 AND payload IS NOT NULL AND source=? "
                      "ORDER BY ts DESC LIMIT 1", (SRC_DIFF,))
    row = cur.fetchone()
    if not row:
        return {}, True
    try:
        rows = json.loads(row[1])
    except (json.JSONDecodeError, TypeError):
        return {}, True
    # 历史库里可能是旧版本留下来的未过滤行，按当前 watchlist 再筛一遍，
    # 否则 Grok / Muse 这些没关注的行会被报成「消失」。
    # 传这一条快照自己的 ts：隔离区间是对**观测时间**的，不是对"现在"。
    return baseline_map(rows, load_config(), ts=str(row[0] or "")), False


def price_attribution(prev: dict, cur: dict) -> list[str]:
    """给一次单价变动找归因标签。**只作归因，不改变动本身**。

    两条判据都必须是"看得见的事实"，不能靠猜：
      · 匹配对象换了：两侧都拿到过 or_id 且不相等。老基线里根本没有 or_id 这个键
        （本轮之前存的），那种情况**什么都不说** —— 缺证据不等于"证明没换"。
      · 跨源口径分歧：单价变了但 AA 的每任务成本一分没动。同一模型两个源给出矛盾说法，
        这标签是提醒读者别把它读成"厂商涨价 6 倍"。
    """
    tags = []
    po, co = prev.get("or_id"), cur.get("or_id")
    if "or_id" in prev and po and co and po != co:
        tags.append(f"匹配对象换了：{po} → {co}")
    apt_p, apt_c = prev.get("apt_usd"), cur.get("apt_usd")
    if apt_p is not None and apt_c is not None and apt_p == apt_c:
        tags.append("AA 每任务成本未变")
    return tags


FALLBACK_WORDS = re.compile(r"\b(?:with|default)?\s*fallback\b", re.I)


def rename_identity(row: dict) -> tuple:
    """改名判据：厂商 + 代次 + 档位，档位里的 fallback 口径词不参与比较。

    只处理"同一个观测点换了写法"（如 AA 把 `(max with fallback)` 改成 `(max)`）。
    代次数字仍算身份：5.5 改名成 5.6 是换代，不是改名。
    """
    family, tier = split_series(str(row.get("name") or ""))
    return (norm_key(row.get("creator", "")), norm_key(family),
            norm_key(FALLBACK_WORDS.sub("", tier)))


INVERSE_TOL_PP = 5.0   # 判定「符合反比」的容差，单位是百分点（见 inverse_relation）


def inverse_relation(p: dict, r: dict, field: str, other: str) -> str:
    """本字段变化时另一指标的联动读数：一句话说完，不再逐句铺开。

    固定输出量下两项互为倒数，另一项涨 x% 就预期本项约 -x/(1+x)。这里只报
    「另一项怎么变、本项符合还是不符合反比、差几个百分点」，读者不必自己做除法。

    容差 `INVERSE_TOL_PP` 来自实测：1450 对同轮变化里偏差的中位数 0.0pp、p95 1.9pp，
    超过 5pp 的只有 1.6%，那部分是采证之外的真偏离，不是舍入噪声。
    非因果那句免责只说一次，挂在 `move_hint()` 上，不逐行重复。
    """
    a, b = p.get(field), r.get(field)
    a2, b2 = p.get(other), r.get(other)
    valid = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
    if not same_evaluation_major(p.get("evaluation_version"), r.get("evaluation_version")):
        return f"{DIFF_LABEL[other]}评测版本变了，无法比较"
    if not all(valid(v) for v in (a, b, a2, b2)):
        return f"{DIFF_LABEL[other]}数据不足"
    if a2 == b2:
        return f"{DIFF_LABEL[other]}未变"
    expected = (a2 / b2 - 1) * 100
    actual = (b / a - 1) * 100
    # 先舍到一位再判定：不然会出现"偏差 +5.0 个百分点"却被判不符合（真值 5.04）这种自相矛盾。
    # `-0.0` 归零：精确反比时实际与预期相等，符号位不该漏到文案里。
    dev = round(actual - expected, 1) + 0.0
    verdict = "符合反比" if abs(dev) <= INVERSE_TOL_PP else "不符合反比"
    # 单位写「个百分点」不写 %：这是两个百分比相减，写成 % 会和上面那两个百分比混淆。
    return (f"{DIFF_LABEL[other]} {_move_span(other, a2, b2)}（{(b2 / a2 - 1) * 100:+.1f}%），"
            f"{verdict}，偏差 {dev:+.1f} 个百分点")


def diff_models(cur: list[dict], prev_map: dict[str, dict]) -> list[dict]:
    pmap = prev_map
    current_names = {r["name"] for r in cur}
    # 2026-11-06 用户 case：AA 去掉 with fallback，不应报成 5 个新进榜 + 5 个落榜。
    # 配对要求本键唯一，且两侧都有的 aa_slug / or_id 必须一致；证据冲突就退回一进一出。
    old_groups, new_groups = {}, {}
    for name, row in pmap.items():
        if name not in current_names:
            old_groups.setdefault(rename_identity({**row, "name": name}), []).append(name)
    for row in cur:
        if row["name"] not in pmap:
            new_groups.setdefault(rename_identity(row), []).append(row)
    renames = {}
    for key, rows in new_groups.items():
        olds = old_groups.get(key, [])
        if not key[0] or len(rows) != 1 or len(olds) != 1:
            continue
        row, old = rows[0], pmap[olds[0]]
        shared = [f for f in ("aa_slug", "or_id") if row.get(f) and old.get(f)]
        if shared and all(row[f] == old[f] for f in shared):
            renames[row["name"]] = olds[0]
    changes = []
    for r in cur:
        old_name = renames.get(r["name"])
        p = pmap.get(old_name or r["name"])
        if p is None:
            # 沿用行不是"新上榜"：它是从台账按旧读数恢复回来的，AA 榜单本轮并没有它。
            # 记成 `new` 会让浮层报"新进榜 N 个"，而读者刚在图上看到这些行标着「沿用 MM-DD」。
            changes.append({"name": r["name"],
                            "type": "carried" if r.get("carried_asof") else "new",
                            "fields": []})
            continue
        fields = []
        for f in DIFF_FIELDS:
            if f == "time_per_task_s" and f not in p:
                continue  # 旧基线没有该观测，不把首次建基线当成变化。
            if (f in ("index", "apt_usd", "time_per_task_s")
                    and not same_evaluation_major(p.get("evaluation_version"), r.get("evaluation_version"))):
                continue
            label = DIFF_LABEL[f]
            a, b = p.get(f), r.get(f)
            if a == b:
                continue
            # 「没有」变成「有值」（和反过来）本身就是信息，以前被
            # `if a is None or b is None: continue` 一起吞掉了：AA 第一次测出 tok_s、
            # 价格第一次出现、或某个指标忽然缺失，变动卡全程静默。
            # 这类变动没有幅度可言（没基数算百分比），所以 delta/pct 留 None，
            # 由 kind 说明方向，渲染面按 kind 分支显示。
            if a is None or b is None:
                d = {"field": f, "label": label, "from": a, "to": b,
                     "delta": None, "pct": None,
                     "kind": "appeared" if a is None else "disappeared"}
            else:
                d = {"field": f, "label": label, "from": a, "to": b,
                     "delta": round(b - a, 6), "pct": round((b - a) / a * 100, 2) if a else None,
                     "kind": "changed"}
            if f in PRICE_FIELDS:
                tg = price_attribution(p, r)
                if tg:
                    d["tags"] = tg
            if (p.get("evaluation_version") != r.get("evaluation_version")
                    and same_evaluation_major(p.get("evaluation_version"), r.get("evaluation_version"))):
                d.setdefault("tags", []).append(
                    f"评测版本 {p['evaluation_version']} → {r['evaluation_version']}")
            if f in ("tok_s", "time_per_task_s"):
                d["relation"] = inverse_relation(p, r, f, "time_per_task_s" if f == "tok_s" else "tok_s")
            fields.append(d)
        if old_name:
            change = {"name": r["name"], "old_name": old_name,
                      "creator": r.get("creator", ""), "type": "renamed", "fields": fields}
            # "静默改名"只覆盖提醒门槛内的测量波动；报价、缺值和评测口径变化必须留痕。
            change["quiet"] = (
                p.get("evaluation_version") == r.get("evaluation_version")
                and not notable_moves([change])
                and not any(f.get("kind") != "changed" or f.get("field") in PRICE_FIELDS
                            or f.get("tags") for f in fields))
            changes.append(change)
        elif fields:
            changes.append({"name": r["name"], "type": "changed", "fields": fields})
    for name in pmap:
        if name not in current_names and name not in renames.values():
            changes.append({"name": name, "type": "gone", "fields": []})
    return changes


def rename_lines(changes: list[dict]) -> list[str]:
    """同族改名只说一次。保留原始差异，不把未过提醒门槛说成数值完全相同。"""
    groups = {}
    for c in changes or []:
        if c.get("type") == "renamed":
            family, _ = split_series(c["name"])
            groups.setdefault((c.get("creator", ""), family), []).append(c)
    lines = []
    for (_, family), rows in groups.items():
        dropped = all(FALLBACK_WORDS.search(split_series(c["old_name"])[1])
                      and not FALLBACK_WORDS.search(split_series(c["name"])[1]) for c in rows)
        action = "去掉 with fallback" if dropped else "名称写法调整"
        # 目录报价和缺值变化不能用提醒门槛掩盖；评测口径变化也不能称为纯改名。
        detail = ("提醒指标未达变化门槛" if all(c.get("quiet") for c in rows)
                  else "另有数值或口径变化，见明细")
        lines.append(f"{family}：{len(rows)} 档改名（{action}；{detail}）")
    return lines


# 值得单独提醒一声的变动门槛（用户定的口径，不是从数据里量的，所以写成显式规则表）：
#   智能指数变动 > 1 分；每任务成本变动 > 10%；输出速度变动 > 10%。
# "abs" 比的是绝对分差，"pct" 比的是百分比幅度（diff_models 已经算好 pct，没有基数就是 None）。
MOVE_INDEX_POINTS = 1
MOVE_PCT = 20    # 2026-10-12 用户把两条百分比门槛从 10% 提到 20%（指数量纲是分，仍是 >1 分）
MOVE_RULES = (("index", "abs", MOVE_INDEX_POINTS),
              ("apt_usd", "pct", MOVE_PCT),
              ("tok_s", "pct", MOVE_PCT),
              ("time_per_task_s", "pct", MOVE_PCT))
MOVE_ALERT_TEXT = "{when}有 {n} 项值得注意的变动：{list}"
MOVE_UNIT = {"index": "分", "apt_usd": "$", "tok_s": "tok/s"}

# 给人看的位数（与数据位数分开）：11-04 起 tok_s / ttft_s / first_answer_s 从"表格里已格式化
# 的显示文本"改读 RSC 原始浮点，读数本身没变，但旧文本是上游格式化过的 —— 11-04 及以前
# 80/80 档 tok_s 是整数、ttft_s 72/80 档两位、total_s 73/80 档两位；新文本带 14~16 位小数。
# 台账与 series 仍按原始值入库，这里只统一文案与图表上那几位。
# 三处共用同一张表：爬虫文案、render_scatter_svg 的格子、dashboard 的变动徽标
# （防漂移判据见 tools/test_rules.py 的「显示位数三处同源」与 verify_dashboard.mjs）。
DISPLAY_DECIMALS = {"tok_s": 0, "ttft_s": 2, "total_s": 2, "first_answer_s": 2}


def display_num(value, decimals: int) -> str:
    """half-up 到指定位数再去尾零：67.7632→68、150.40→150.4、无效值给破折号。

    用 `Decimal(str(v))` 而不是 `floor(v*10**n+0.5)`：109.75 在二进制里略小于 .75，
    乘十取整给 109.7，而看板 `toFixed` 给 109.8 —— 两个运行时必须逐字一致
    （同一个理由见 render_scatter_svg.think_ratio_text）。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if isinstance(value, bool) or not math.isfinite(v):
        return "—"
    q = Decimal(str(v)).quantize(Decimal("1").scaleb(-max(0, int(decimals))),
                                rounding=ROUND_HALF_UP)
    s = format(q, "f")
    return s.rstrip("0").rstrip(".") if decimals else s


def _move_value(field: str, v) -> str:
    """按字段的单位格式化一个数值，通知文案里不带内部字段名。"""
    if v is None:
        return "—"
    if field == "apt_usd":
        return f"${v:g}"
    if field in DISPLAY_DECIMALS:
        return display_num(v, DISPLAY_DECIMALS[field])
    return f"{v:g}"


def _move_span(field: str, a, b) -> str:
    """"从→到"的一段，单位只在末尾出现一次：`100→160 tok/s`、`$1→$1.12`、`41→43`。"""
    unit = " tok/s" if field == "tok_s" else " 秒" if field == "time_per_task_s" else ""
    return f"{_move_value(field, a)}→{_move_value(field, b)}{unit}"


CARRY_MAX_DAYS = 30        # 掉榜超过一个月就不再沿用旧读数：benchmark 口径会变，太久前的值不可比
CARRY_LOOK_ROUNDS = 60     # 往台账最多翻多少轮找"最后一次在榜时"的值


def is_fallback_dup(name: str) -> bool:
    """这一行是不是 `with fallback` 重复档：同一 release 同一档位的兜底视图。

    它和主档读同一批读数，画在图上就是一对完全重合的点 —— 保留它不增加信息，
    只会让悬停卡片和档位数虚高。判据看括号里是否出现 fallback 这个词，不改模型名。
    直接读括号而不复用 `split_series` 的档位：像 `(Max, Default Fallback)` 这种
    写法里 `default` 不在档位词表，档位会退回空串，靠档位判断会漏。
    """
    for inner in re.findall(r"[\(（\[]([^\)）\]]*)[\)）\]]", name or ""):
        if "fallback" in inner.lower():
            return True
    return False


def carry_forward(models: list, con, or_models: dict, dl_rows: list, cfg: dict,
                  now: str | None = None) -> int:
    """把被榜单撤下、但评测版本没变的模型，按最后一次实测值沿用回来。原地改。

    为什么需要：主源换版时会直接把旧模型从榜上撤掉，而"撤掉"不等于"没价值"。
    实测的那次：GPT-6.1 Sol 顶替 GPT-6 Sol，指数与每任务成本都更好，速度却从 74 tok/s
    掉到 51（逐档都是断崖）—— 被撤下来的旧行不该就这么消失。

    2026-11-17 用户改口径：判据是**评测版本没变**，不再要求"没有现役行全面优于它"。
    旧判据（`dominates`）会让被新一代全面压过的行静默消失，读者看不出"这个档位已经
    退出榜单"，只看到它凭空不见了。现在只要 AA 还在同一评测版本、且在最长窗口内见过，
    就带旧读数回来，由渲染侧显式标注它是沿用值与缓存到哪一天。
    `with fallback` 档不回来：它是主档的重复视图（同一 release 同一档位叠加兜底），
    带回来只会让图上多出一倍重合点，读不出新信息。

    补的是**原始 AA 行**、走的是同一个 `merge()`：派生字段（series/creator_slug/色/标）
    与现役行走同一条路。第一版在这里直接塞台账里存过的合并结果，`apply_vendor_colors`
    当场 KeyError: 'creator_slug' —— 台账只存要比的那几个字段，不存派生出来的那些。

    回来的是**旧读数**，所以必须带 `carried_asof`：渲染侧据此标「沿用 MM-DD」，
    不能把它混成今天的实测值。词表已经不再关注的行 `merge()` 会自己丢掉（不硬补）。
    """
    if not models:
        return 0
    current_version = version_of(models)
    if not current_version:
        return 0
    current = {m.get("name") for m in models}
    try:
        cutoff = (datetime.strptime(now or now_str(), "%Y-%m-%d %H:%M:%S")
                  - timedelta(days=CARRY_MAX_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        cutoff = ""                            # 时间戳读不出来就别设窗口
    rows = con.execute("SELECT ts, payload FROM snapshots WHERE source=? AND ok=1 "
                       "AND payload IS NOT NULL ORDER BY ts DESC LIMIT ?",
                       (SRC_AA, CARRY_LOOK_ROUNDS)).fetchall()
    last_seen: dict = {}
    for ts, pl in rows:                        # 从最近往旧翻：第一次遇到就是用到的那一轮
        try:
            past = json.loads(pl)
        except (json.JSONDecodeError, TypeError):
            continue
        for r in past:
            # 旧库未知版本同样不冒充当前代次。只在版本证据一致时沿用。
            if r.get("evaluation_version") != current_version:
                continue
            n = r.get("name")
            if n and n not in current:
                last_seen.setdefault(n, (ts, r))

    def stale_ok(ts: str) -> bool:
        return not cutoff or ts >= cutoff

    back = []
    for n, (ts, r) in sorted(last_seen.items()):
        if not stale_ok(ts) or is_fallback_dup(n):
            continue
        for rec in merge([r], or_models, dl_rows, cfg):     # 与现役行同一个构造口
            rec["carried_asof"] = ts
            back.append(rec)
    if back:
        models.extend(back)
    return len(back)


def carry_from_payload(models: list[dict], previous: dict | None,
                       now: str | None = None) -> int:
    """带回来**公开站上一版**有、本轮榜单已撤掉的行，给悬停历史对照用。

    公开站每轮都换一台 runner，本地 SQLite 留不住（cache 与 6 分钟一轮的体量都不合适）。
    上一版 `latest.json` 本身就是可复算的状态：它带每一行的实测值，沿用行还带
    `carried_asof`。这里把上一版有、本轮没有的行原样接回来，链条就靠发布产物自己续上。

    只在上一版是本轮可比的公开数据时接：评测版本必须一致（跨版不可比），
    沿用日期超过 CARRY_MAX_DAYS 就丢（与 carry_forward 同一条窗口），
    with fallback 重复档不回（同一 release 重复视图，接回来只是重合点）。
    """
    if not models or not isinstance(previous, dict) or previous.get("data_state") != "ok":
        return 0
    rows = previous.get("models") or []
    version = version_of(models)
    if not isinstance(rows, list) or not version:
        return 0
    try:
        end = datetime.strptime(now or now_str(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    generated = str(previous.get("generated_at") or "")
    names = {m.get("name") for m in models}
    added = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = r.get("name")
        if not isinstance(name, str) or not name or name in names or is_fallback_dup(name):
            continue
        if r.get("evaluation_version") != version:
            continue
        asof = str(r.get("carried_asof") or generated)
        try:
            observed = datetime.strptime(asof, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if not end - timedelta(days=CARRY_MAX_DAYS) <= observed <= end:
            continue
        models.append(dict(r, carried_asof=asof))
        names.add(name)
        added += 1
    return added


def family_rollup(changes: list[dict]) -> dict:
    """按 (族, 字段) 汇总**所有算得出幅度的档**，包含没到门槛的那些。

    合并行只念过门槛的档，读者于是看不见"这一族在这个指标上整体动了多少"：
    一轮里同族另有两档各动 -8% 也是同一次调价的一部分。门槛判据与 `notable_moves`
    同源（都读 MOVE_RULES），差别只是这里不把未到门槛的丢掉。
    """
    rules = {r[0]: r for r in MOVE_RULES}
    out: dict = {}
    for ch in changes or []:
        if ch.get("type") not in ("changed", "renamed"):
            continue
        fam, tier = split_series(ch.get("name", ""))
        for f in ch.get("fields") or []:
            field = f.get("field")
            r = rules.get(field)
            if r is None or f.get("kind") != "changed":
                continue
            amt = f.get("delta") if r[1] == "abs" else f.get("pct")
            if amt is None:
                continue
            g = out.setdefault((fam, field), {"label": f.get("label", field),
                                              "unit": "分" if r[1] == "abs" else "%",
                                              "tiers": []})
            g["tiers"].append((tier or str(ch.get("name", "?")), round(amt, 2), abs(amt) > r[2]))
    return out


def family_tail(ctx: dict, fam: str, field: str) -> str:
    """合并行的尾巴：同族未到门槛的档各是多少 + 整族在这个字段上的综合变化。

    整族都过了门槛时**不加尾巴**：那种情况下合并行说的就是全部，再念一遍是噪音。
    """
    g = (ctx or {}).get((fam, field))
    if not g:
        return ""
    tiers = g["tiers"]
    others = [x for x in tiers if not x[2]]
    if not others:
        return ""
    u = g["unit"]
    listed = "、".join(f"{x[0]} {x[1]:+.1f}{u}" for x in others[:3])
    more = f"…等 {len(others)} 档" if len(others) > 3 else ""
    amts = [x[1] for x in tiers]
    srt = sorted(amts)
    n = len(srt)
    mid = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2
    span = f"{min(amts):+.1f}~{max(amts):+.1f}{u}"
    drift = "有涨有跌" if min(amts) < 0 < max(amts) else "同向"
    return (f"；同族另有 {len(others)} 档未到门槛（{listed}{more}）"
            f"；整族 {n} 档{drift}，均值 {sum(amts) / n:+.1f}{u}、中位 {mid:+.1f}{u}，区间 {span}")


def merged_move_lines(moves: list[dict], changes: list[dict] | None = None) -> list[str]:
    """同族同字段的档位变动并成一条，给均值与中位数。

    一档一档列会把重点埋在重复里：实测一轮里 GPT-6 Astra 的 4 档、GPT-6.1 Sol 的 4 档
    各占 4 行，说的其实是同一件事。合并条件严格：
    · 同族（`split_series` 的族名，代次数字仍算不同族）+ 同字段；
    · **全部同向** —— 一族里有的档涨有的档跌，说明不是整体漂移，必须分开说；
    · 只有一档时原样输出，不硬套"1 档同向"这种话。
    档位顺序按幅度降序（与 moves 的入序一致），数字保留一位小数。

    给了 `changes` 时再挂一条"整族视角"的尾巴（`family_tail`）：同族没过门槛的档也提一嘴，
    并给整族在这个字段上的均值/中位/区间 —— 只念过门槛的档会把"整族一起调过价"说小了。
    """
    ctx = family_rollup(changes) if changes else {}
    groups: dict[tuple[str, str], list[dict]] = {}
    order: list[tuple[str, str]] = []
    for m in moves:
        fam, _tier = split_series(m.get("name", ""))
        key = (fam, str(m.get("field") or m.get("kind") or ""))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(m)
    out: list[str] = []
    for key in order:
        g = groups[key]
        amts = [x.get("amount") for x in g]
        tiers = [t for t in (split_series(x.get("name", ""))[1] for x in g) if t]
        tail = family_tail(ctx, key[0], key[1])
        relations = [x["relation"] for x in g if x.get("relation")]
        if len(g) > 1:
            # 合并行没有单一档位，关联读数得把档位名带上，否则不知道说的是哪一档。
            relations = [f"{x['name']} {x['relation']}" for x in g if x.get("relation")]
        if relations:
            tail += "；关联：" + "；".join(dict.fromkeys(relations))
        version_notes = list(dict.fromkeys(t for x in g for t in x.get("tags", [])
                                          if t.startswith("评测版本 ")))
        if version_notes:
            tail += "；" + "；".join(version_notes)
        if len(g) == 1 or any(a is None for a in amts) \
                or len({1 if a > 0 else -1 for a in amts}) > 1:
            # 逐档列时也把整族尾巴挂在最后一行上：这一族说的就是那件事，分开写会像另一条结论
            lines = [x.get("text") or x.get("name", "?") for x in g]
            out += lines[:-1] + [lines[-1] + tail]
            continue
        unit = str(g[0].get("unit") or "")
        mean = sum(amts) / len(amts)
        srt = sorted(amts)
        mid = (srt[len(srt) // 2] if len(srt) % 2
               else (srt[len(srt) // 2 - 1] + srt[len(srt) // 2]) / 2)
        label = str(g[0].get("label") or key[1])
        span = f"{min(amts):+.1f}~{max(amts):+.1f}" if min(amts) != max(amts) else f"{mean:+.1f}"
        out.append(f"{key[0]} {label} {len(g)} 档同向 {mean:+.1f}{unit}"
                   f"（中位 {mid:+.1f}{unit}，区间 {span}{unit}；档位 {'/'.join(tiers) or '—'}）"
                   + tail)
    return out


def moves_text(moves: list[dict], when: str = "这轮") -> str:
    """达门槛变动的正文：列前 3 项、截 200 字。

    通知与"往前翻最近一次变动"共用这一份，免得同一条变动在横幅里一个说法、悬停里另一个说法；
    只有"这轮/那轮"这个指代跟着走位变 —— 翻出来的那轮不是本轮。
    """
    # 横幅**不**带整族尾巴：它只有 200 字，尾巴是给浮层看明细用的（那里不截断）
    lines = merged_move_lines(moves)
    shown = lines if len(lines) <= 3 else lines[:3] + [f"…等另 {len(lines) - 3} 条"]
    return (MOVE_ALERT_TEXT.replace("{when}", when).replace("{n}", str(len(moves)))
            .replace("{list}", "；".join(shown))[:200])


def notable_moves(changes: list[dict], metric: str | None = None) -> list[dict]:
    """从变动列表里挑出"该弹一条文字通知"的那些（门槛见 MOVE_RULES）。

    只看 changed 行的这三个字段：新入榜 / 掉榜各有变动卡，整族消失另有 series-gone 告警，
    混进这条会让每天常规的换榜进出把通知刷成噪音；`appeared`/`disappeared`（没↔有）
    没有基数可比幅度，也不报。
    """
    out: list[dict] = []
    for ch in changes or []:
        if ch.get("type") not in ("changed", "renamed"):
            continue
        for f in ch.get("fields") or []:
            if metric and f.get("field") in ("tok_s", "time_per_task_s") and f["field"] != metric:
                continue
            rule = next((r for r in MOVE_RULES if r[0] == f.get("field")), None)
            if rule is None or f.get("kind") != "changed":
                continue
            how, thr = rule[1], rule[2]
            amt = f.get("delta") if how == "abs" else f.get("pct")
            if amt is None or abs(amt) <= thr:
                continue
            unit = "分" if how == "abs" else "%"
            field = f["field"]
            out.append({
                "name": ch.get("name", "?"), "field": field, "label": f.get("label", field),
                "from": f.get("from"), "to": f.get("to"),
                "amount": round(amt, 2), "unit": unit,
                **({"tags": f["tags"]} if f.get("tags") else {}),
                **({"relation": f["relation"]} if f.get("relation") else {}),
                "text": (f"{ch.get('name', '?')} {f.get('label', field)} "
                         f"{_move_span(field, f.get('from'), f.get('to'))}（{amt:+.0f}{unit}）"),
            })
    # 幅度大的先说：通知文案只有 200 字，排在后面的会被截掉
    return sorted(out, key=lambda m: (-abs(m["amount"]), str(m["name"])))


# 上一轮不足 3 档的族不报缺口：1–2 档的进出是常规换榜，变动卡逐条已经说过；
# 而"图上一条能力线整条不见了"至少要 3 档才看得出是结构缺口，不是换了个型号名。
GAP_MIN_ROWS = 3


def series_gap(prev_rows, cur_rows: list[dict]) -> list[dict]:
    """上一轮有 ≥GAP_MIN_ROWS 个档位、这一轮一档都不剩的族（= 图上整条能力线消失）。

    行数总量不是判据：AA 十天里就在 113–123 之间漂（实测台账），拿总数当阈值要么天天响、
    要么永远不响。真正会让人误读的是"某个族忽然全没了"，所以按族比。

    族名从**名字**现推（split_series），不从基线取字段：diff 基线只存要比的那几个数，
    没有 series_key，而族的定义本来就在名字里 —— 再存一份就是两个说法。
    """
    def group(rows) -> dict[str, dict]:
        g: dict[str, dict] = {}
        for r in rows:
            name = str(r.get("name") or "")
            if not name:
                continue
            series, _tier = split_series(name)
            k = norm_key(series)
            e = g.setdefault(k, {"key": k, "label": series,
                                 "creator": str(r.get("creator") or ""), "n": 0})
            e["n"] += 1
        return g

    prev_g, cur_g = group(prev_rows), group(cur_rows)
    out = [{"key": k, "label": p["label"], "creator": p["creator"],
            "n_prev": p["n"], "n_now": 0}
           for k, p in prev_g.items()
           if p["n"] >= GAP_MIN_ROWS and cur_g.get(k, {"n": 0})["n"] == 0]
    return sorted(out, key=lambda e: -e["n_prev"])


# --------------------------------------------------------------------------- 输出

_crop_warned = False


def _cropped(raw: Path) -> bytes | None:
    """返回把 viewBox 裁到内容外接正方形后的字节；裁不动返回 None。

    ⚠️ 以前裁剪失败是 `except: dest.write_bytes(原图)` —— 静默降级成"图标带一圈死边、
    填不满圆框"，而且因为下面那个 `raw.exists() and dest.exists()` 的短路，坏文件一旦落盘
    就永远不会被重裁。少装一个依赖（svgelements）就能让整个 logo 体系悄悄退化。
    """
    global _crop_warned
    try:
        import crop_logos
        return crop_logos.crop_bytes(raw.read_bytes())
    except Exception as exc:  # noqa: BLE001
        if not _crop_warned:
            _crop_warned = True
            # 提示要指到**真正缺的那个包**：requirements.txt 现在不含运行期依赖
            # （零依赖是刻意的），让人照它 pip 会装到空气。
            log(f"⚠️ logo 裁剪不可用（{type(exc).__name__}: {exc}）→ 图标会保留 SVG 自带内边距、"
                f"填不满圆框。补那一个包：{sys.executable} -m pip install svgelements")
        return None


def guard_logos(rows: list[dict], enabled: bool) -> None:
    if not enabled:
        return
    LOGOS_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for r in rows:
        src = r.get("logo") or ""
        if not src.startswith("/img/logos/"):
            continue
        slug = src.split("/")[-1]
        dest = LOGOS_DIR / slug
        raw = RAW_DIR / slug
        # 原件留一份：viewBox 是裁过的，下次覆盖前得先有原图才能重裁
        try:
            if not raw.exists():
                raw.write_bytes(fetch("https://artificialanalysis.ai" + src, timeout=15, retries=1))
        except Exception:  # noqa: BLE001
            r["logo_local"] = ""
            continue
        want = _cropped(raw) or raw.read_bytes()
        # 内容真的变了才写：既让"上次裁剪失败留下了原图"能自愈，
        # 也不至于每次抓取都把这几个文件重写一遍。
        if not dest.exists() or dest.read_bytes() != want:
            dest.write_bytes(want)
        r["logo_local"] = f"logos/{slug}"


def _atomic_write(path: Path, text: str) -> None:
    """先写同盘临时文件再 os.replace，避免并发读者（状态栏 app / 看板）读到半截文件。

    tmp 放在「真实父目录」（realpath 解软链），即便 path 本身是软链或某级父目录是软链，
    也不会把临时文件建到软链另一头的文件系统上，避免 os.replace 跨文件系统抛 OSError。
    """
    real = Path(os.path.realpath(path))
    tmp = real.with_name(real.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, real)


def probe_palette() -> dict:
    """解析全部 logo SVG 的配色。失败返回 {} —— 那时厂商色退到按名生成，图不坏。"""
    try:
        from extract_logo_palette import probe_all
        return probe_all()
    except Exception as exc:  # noqa: BLE001
        log(f"⚠️ logo 配色探测不可用（{type(exc).__name__}: {exc}）→ 厂商色按名生成")
        return {}


def apply_vendor_colors(models: list[dict], cfg: dict, palette: dict) -> dict[str, dict]:
    """派生每家厂商的标识色，写回 models 与 cfg（不落 config，派生物不进气动历史）。

    取代原先 config 里手写的 vendor_brand_color 表：那张表没列的厂商全部落 #64748B 灰，
    11 家里 5 家同色，其中 Meta / SpaceXAI 就在指数前 20。判据搬进 vendor_colors 之后，
    来新厂商不用改配置，也不会出现"表里记错色"（Alibaba 曾被记成紫色）。
    """
    if not models:
        return {}
    slugs = sorted({m["creator_slug"] for m in models})
    res = vendor_colors.derive(slugs, palette, cfg.get("vendor_brand_color_override") or {})
    for m in models:
        m["brand_color"] = (res.get(m["creator_slug"]) or {}).get("color") or vendor_colors.GRAY
    cfg["vendor_brand_color"] = {k: v["color"] for k, v in res.items()}
    cfg["vendor_brand_color_source"] = {k: v["source"] for k, v in res.items()}
    stuck = [k for k, v in res.items() if "collision" in v["source"]]
    if stuck:
        log(f"⚠️ {len(stuck)} 家厂商的品牌色在图上彼此分不开（{', '.join(stuck)}），已按原色保留")
    gen = [k for k, v in res.items() if v["source"] == "generated"]
    if gen:
        log(f"配色：{len(gen)} 家 logo 里没有可用品牌色，按厂商名生成 —— {', '.join(gen)}")
    return res


def write_outputs(payload: dict, palette: dict | None = None) -> None:
    mr_paths.ensure_dirs()
    # 来源：2026-11-04 用户要求坏轮仍保留上一份可信 TPS 圆面积。
    # 仅发布副本沿用，diffbase/series 已在此前落库，不能把旧值写成新观测。
    models = payload.get("models") or []
    ts = payload.get("diff_ts") or ""
    affected = {h["name"] for h in quarantine_hits(models, ts) if h["field"] == "tok_s"}
    if affected:
        with closing(sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)) as history:
            past = history.execute("SELECT ts,payload FROM snapshots WHERE source=? AND ok=1 "
                                   "AND ts<? ORDER BY ts DESC LIMIT ?",
                                   (SRC_DIFF, ts, BACKTRACK_MAX_ROUNDS)).fetchall()
        for observed, raw in past:
            previous = baseline_map(json.loads(raw), load_config(), ts=observed)
            for model in models:
                name = model.get("name")
                old = previous.get(name, {})
                value = old.get("tok_s")
                if (name in affected and isinstance(value, (int, float)) and value > 0
                        and same_evaluation_major(old.get("evaluation_version"), model.get("evaluation_version"))):
                    model["tok_s"] = value
                    model["tok_s_asof"] = observed
                    affected.remove(name)
            if not affected:
                break
        if any(m.get("tok_s_asof") for m in models):
            for m in models:
                for key in list(m):
                    if key.startswith("tok_s_est") or key.startswith("tok_s_band"):
                        del m[key]
                # 派生时间依赖速度；速度被沿用值替换后必须重算，不能留上一轮的旧推导。
                if m.get("time_per_task_est") or m.get("time_per_task_band"):
                    for key in ("time_per_task_est", "time_per_task_est_basis",
                                "time_per_task_est_s", "time_per_task_est_range", "time_per_task_band"):
                        m.pop(key, None)
            estimate_missing_tok_s(models)
            derive_missing_task_times(models)
            bands = tier_ladder(models)
            annotate_efficiency(models, bands)
            payload["tier_bands"] = bands
            payload["legend"] = build_legend(models, bands, usage_available=bool(payload.get("usage", {}).get("ok")))
            # 收缩强度吃速度和派生耗时，速度被沿用值替换后必须跟着重算；
            # 不重算就会发布一个按旧速度拟合出来、和图上半径对不上的 λ。
            payload["task_size_penalty"] = task_size_penalty(models)
            note = "TPS 圆面积沿用上一轮可信观测，坏轮不参与速度涨跌"
            if note not in payload.get("data_note", ""):
                payload["data_note"] = "；".join(filter(None, [payload.get("data_note"), note]))
            payload["content_hash"] = content_fingerprint(models)
    # 配色每次重探：logo 是抓取时现下的，缓存下来就会漏掉新厂商。
    # 十几个 SVG 的正则解析不到 10ms，每次抓完顺手做掉即可。
    # 由 run() 传进来时用的是**派生厂商色时同一份**快照，避免两处取色不一致。
    payload["logo_palette"] = probe_palette() if palette is None else palette

    _atomic_write(LATEST_JSON, json.dumps(payload, ensure_ascii=False, indent=1))
    _atomic_write(LATEST_JS, "window.MODEL_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n")

    # 看板是 file:// 页面，只能在仓库目录树内取文件（沙箱浏览器不跟软链进 ~/Library），
    # 所以每次写完立刻把浏览器要用的那份镜像进仓库 data/。放在这里而不是单独一步，
    # 是为了让"权威数据已更新、仓库那份还是旧的"没有机会出现。
    wrote = mr_paths.publish_for_browser()
    if wrote:
        log(f"已镜像给看板：{', '.join(wrote[:6])}{' 等' if len(wrote) > 6 else ''}")
    if not (mr_paths.REPO_DATA / LATEST_JS.name).is_file():
        # 镜像没落地 = 看板必然读成"没读到数据"。这条要显眼，别让它静默。
        log(f"⚠️ 仓库侧 {mr_paths.REPO_DATA / LATEST_JS.name} 不存在或不可写 → 看板会读不到数据。"
            f"若 {mr_paths.REPO_DATA} 是软链，重跑 widget/build_chart_widget.sh 会换成真目录")


# --------------------------------------------------------------------------- 新数据更新提示

# 内容指纹：只覆盖「用户看得见、且跨重抓稳定的内容字段」，刻意排除：
#   generated_at / sources(含 ts) / data_note / logo_local —— 每次都变或路径噪音；
#   changes —— 它是相对差分，重抓一次就变空，会让同内容被判成不同内容、去重失效。
# 由 Python 单点算出并写进 latest.json，app 只读同一串 —— 同一把键是结构保证，不靠约定。
MODEL_HASH_FIELDS = ("name", "creator", "index", "apt_usd", "tok_s", "ttft_s",
                     "total_s", "time_per_task_s", "ctx", "in_per_1m_usd", "out_per_1m_usd",
                     "or_id", "dl_index", "evaluation_version", "progress_index",
                     "progress_version", "carried_asof")


def maybe_summarize(payload: dict) -> None:
    """抓取成功后拉一个**分离的子进程**去写变动总结，主流程不等它回来。

    为什么不就地等：组件给"↻ 立即抓取"的超时是 240s，而一次模型来回可能要一分多钟；
    接在抓取尾巴上的症状是"脚本超时"，可数据其实早就写好了 —— 一个可选增强不该拖垮主链路。
    同理，这里任何异常都只记一行日志：总结失败 = 看板继续显示表格，不是抓取失败。
    """
    try:
        if not payload.get("changes"):
            return                                   # 没变动就没什么可总结
        if os.environ.get("MODELRADAR_NO_SUMMARY") == "1":
            return
        import summarize
        if not summarize.resolve_cmd():
            return                                   # 本机没配总结命令：安静地不做
        if mr_paths.SUMMARY_JSON.exists():
            try:
                prev = json.loads(mr_paths.SUMMARY_JSON.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                prev = {}
            if prev.get("content_hash") == payload.get("content_hash"):
                return                               # 这份内容已经总结过，别重复花钱/重复等
        logf = open(mr_paths.LOG_DIR / "summary.log", "a", encoding="utf-8")
        subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "summarize.py")],
                         stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                         start_new_session=True)     # 自成会话：抓取脚本退出后它还得活着
        log("已拉起后台变动总结（不等待，结果进 summary.log）")
    except Exception as exc:  # noqa: BLE001
        log(f"变动总结启动失败（不影响数据）：{type(exc).__name__}: {exc}")


def content_fingerprint(models: list[dict]) -> str:
    stable = [{k: m.get(k) for k in MODEL_HASH_FIELDS} for m in models]
    blob = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]   # 64bit 前缀，碰撞可忽略


def read_stamp() -> str:
    """现读去重戳。两侧（组件/爬虫）都会写，只信内存值就会重复弹。"""
    try:
        return mr_paths.NOTIFIED_STAMP.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return ""


def will_pop(alert: dict, stamped: str) -> bool:
    """这一条会不会真的出声 —— 与组件侧 `key != lastNotifiedKey && onDisk != key` 同一套判断
    （ChartWidget.notifyIfNewData）。fresh 只更角标不出声；键与戳相同 = 这一条已经响过。

    单独抽出来是因为"落一份上次提示记录"和"要不要弹"必须同一个口径，否则浮层会声称
    提示过一条其实被去重掉的通知。
    """
    kind = str((alert or {}).get("kind") or "fresh")
    key = str((alert or {}).get("key") or "")
    return bool(key) and kind != "fresh" and key != (stamped or "").strip()


def worth_remembering(alert: dict) -> bool:
    """这条值不值得留给浮层当"上次提示"。

    只留 `tone == "info"` 的那些（达门槛的变动 / 累计变动）：源降级、太久没跑、整族没了
    都是 `warn`，那些是"这一刻坏了"的告警，不是"上一次数据动了什么"的历史 —— 让它盖掉
    上一条真变动，浮层就会指着一次抓取失败说"上次提示过这个"，对看数据的人没有价值。
    判据复用 `alert_policy` 已经算好的语气，不再另列一份 kind 清单（两份清单迟早分叉）。
    """
    return str((alert or {}).get("tone") or "") == "info"


def should_record_alert(alert: dict, stamped: str) -> bool:
    """这一轮要不要落进 `.last_alert.json`：会出声、且出声的内容值得回头看。

    收成一处具名判据是有原因的：早先这个条件写在 run() 里、测试又自己复刻了一遍，
    于是"把写盘侧的挡去掉"这种变异改的是生产、测的却是测试，永远不会红。
    """
    return will_pop(alert, stamped) and worth_remembering(alert)


def alert_record(payload: dict, alert: dict) -> dict:
    """把"这一轮弹出去的那条"写成浮层能复述的形状：与 `last_moves` 同一套字段名。"""
    a = alert or {}
    moves = [m for m in (payload.get("moves") or []) if isinstance(m, dict) and m.get("text")]
    lines = [str(x) for x in (payload.get("move_lines") or []) if x]
    if not lines:
        lines = [str(m["text"]) for m in moves]
    return {"at": str(payload.get("generated_at") or ""),
            "ts": str(payload.get("diff_ts") or ""),      # 台账里那一轮的 ts，用来算轮距
            "kind": str(a.get("kind") or ""), "label": str(a.get("label") or ""),
            "tone": str(a.get("tone") or ""), "key": str(a.get("key") or ""),
            "text": str(a.get("text") or ""), "n": len(lines), "lines": lines,
            "metric_lines": {metric: (view.get("move_lines") or [] if a.get("kind") == "moved"
                                      else view.get("cumulative", {}).get("lines") or [])
                             for metric, view in (payload.get("metric_views") or {}).items()}}


def write_last_alert(rec: dict) -> None:
    try:
        p = mr_paths.LAST_ALERT
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(p, json.dumps(rec, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass    # 记不上只是浮层少一段话，不能把抓取带崩


def read_last_alert(this_ts: str) -> dict:
    """上一轮落下的"上次提示"记录；坏文件/缺文件当没有，本轮自己刚写的那条不算。

    配对用台账 ts；老记录没有 ts（那次改版之前写的），退回比 `at` —— 宁可少一段话，
    也不能把本轮自己刚弹的那条说成"上次提示"。
    读取侧也过一遍 `worth_remembering`：写盘那条判据是后来才加的，磁盘上可能还留着
    改版前落下的失败类记录（tone=warn），不挡就会立刻又把它端上来。
    """
    try:
        rec = json.loads(mr_paths.LAST_ALERT.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(rec, dict) or not str(rec.get("at") or ""):
        return {}
    if not worth_remembering(rec):
        return {}
    mine = str(rec.get("ts") or rec.get("at") or "")
    if mine == str(this_ts):
        return {}
    # 这条记录若是基于后来被判定不可信的观测算出来的，就不要再复述它：
    # 文件本身不改（历史原样留档），但浮层不该把一条已撤销的结论当成"上次提示"。
    if _mentions_quarantined(rec):
        log("上次提示引用了已隔离的观测，本轮不展示（记录仍留在 .last_alert.json）")
        return {}
    return rec


def _mentions_quarantined(rec: dict) -> bool:
    """这条提示的文字里是否点名了在册隔离的观测。按模型名 + 字段标签匹配。"""
    text = " ".join(str(x) for x in (rec.get("lines") or []) + [rec.get("text") or ""])
    if not text.strip():
        return False
    for q in load_quarantine():
        label = DIFF_LABEL.get(str(q.get("field")), "")
        if str(q.get("name")) in text and (not label or label in text):
            return True
    return False


def round_steps(con: sqlite3.Connection, ts: str) -> int | None:
    """那条提示在台账里往前数第几轮（本轮 = 0）。

    对的是 `snapshots.ts` 而不是 `generated_at`：两者各自调一次 `now_str()`，跨秒就错开一格。
    所以记录里两个都存（`ts` 用来配对、`at` 给人看）。翻不到（超出上限或被保留期清掉）→ None。
    """
    try:
        rows = con.execute("SELECT ts FROM snapshots WHERE source=? AND ok=1 ORDER BY ts DESC",
                           (SRC_DIFF,)).fetchall()
    except sqlite3.Error:
        return None
    order = [r[0] for r in rows]
    return order.index(ts) if ts in order else None


def _stamp_notified(key: str) -> None:
    """把已提示的内容指纹写入 app 同款戳文件，避免 app/Python 两侧对同一份数据各弹一次。

    走 _atomic_write：戳文件是唯一没走它的写入点，半截写入会让 app 把基线判丢、启动即重弹。
    """
    try:
        p = mr_paths.NOTIFIED_STAMP
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(p, key)
    except Exception:  # noqa: BLE001
        pass


def widget_running() -> bool:
    """pid 判活 + 进程名复核：单纯 kill -0 会把被复用的 pid 认成组件（漏兜底），

    ps 核进程名才稳。app 启动时写 .widget.pid（见 ChartWidget.swift）。

    ps 不可用时（部分受限环境会拦 ps）退回只信 kill-0，而不是整体判 False——
    否则会漏兜底、与 app 重复弹。
    """
    try:
        pid = int(mr_paths.WIDGET_PID.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)   # pid 活着
    except Exception:  # noqa: BLE001
        return False
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                           capture_output=True, text=True, timeout=5)
        return "ModelRadar" in r.stdout
    except Exception:  # noqa: BLE001
        return True   # kill-0 已过，ps 不可用时相信 pid 仍活着


# --------------------------------------------------------------------------- 通知策略
# 口径只写在这一处：组件（Swift）与爬虫兜底两条投递路径共用同一个判断，否则
# "什么时候该响"就有两份说法。判据全部来自载荷本身，没有手写表。
STALE_ALERT_HOURS = 26      # 定时任务每天一次；超过 26h 没有新载荷 = 那一脚没落地
STALE_ALERT_TEXT = "距上次成功抓取已 {h} 小时，定时任务疑似没跑"
GONE_ALERT_TEXT = "图上有 {n} 条能力线整条不见了：{list}（上一轮各自的档位这一轮一个都没抓到）"
_STATE_ALERT = {"degraded": "有源缺失，部分数字还是上一轮的",
                "stale": "主源本次没抓到，显示的是本地快照",
                "empty": "主源失败且本地没有可用快照，能力指标全部缺失"}
# 悬停提示/通知标题都取这一份，免得同一个 kind 在 Swift、HTML、Python 里各说一套
KIND_LABEL = {"fresh": "正常更新，没有达门槛的变动",
              "version-change": "AA 评测版本更新",
              "moved": "有达门槛的变动",
              "moved-cumulative": "有累计达门槛的变动（隔了几轮才看出来）",
              "series-gone": "有整族能力线消失",
              "not-running": "定时任务疑似没跑",
              "degraded": "有源缺失", "stale": "主源降级到旧快照", "empty": "没有可用数据"}
_TZ8 = timezone(timedelta(hours=8))

# 门槛说明**从 MOVE_RULES 现拼**，不是再抄一遍：改了常量文案不会说谎（悬停提示、通知都共用它）。
_MOVE_UNIT = {"abs": " 分", "pct": "%"}


def move_hint(metric: str | None = None) -> str:
    """门槛与关联口径各说一次，挂在浮层最后一行。

    关联读数逐条出现；若每条都带"同轮对比、不证明因果"，四档变动就是四遍同样的话，
    把数字挤没了。这句只在这里说一次，`inverse_relation` 只报数字。
    """
    return "门槛：" + " · ".join(f"{DIFF_LABEL[f]} >{thr}{_MOVE_UNIT[how]}"
                                for f, how, thr in MOVE_RULES
                                if not metric or f not in ("tok_s", "time_per_task_s") or f == metric) \
        + "（新入榜/掉榜不过这条门槛，浮层另列在「榜单进出」）" \
        + "；关联读数为同轮对比、非同比，基准口径不同，不证明因果"



def alert_policy(payload: dict, now: datetime | None = None) -> dict:
    """这轮载荷该不该出声。`kind == "fresh"` 表示只更角标、不弹通知。

    异常有三级，前一级吃掉后一级（一次抓取最多响一声）：源状态（degraded/stale/empty）
    > 整族缺口（`payload["series_gaps"]`，由 `series_gap` 在生产侧算好下发）> 太久没更新。

    去重键分开算：异常用 `alert:<kind>:<日期>`（同一种异常一天只响一次），正常更新用
    内容指纹（同内容重抓不重复），两条路径因此可以共用同一个戳文件。

    ⚠️ "太久没更新"这一条**只能在读取侧判**：爬虫调本函数时载荷是刚生成的、龄永远≈0，
    生产者侧这条分支实际永远不会触发（2026-10-07 实测：机器关机四天，10-04~10-06 一轮
    都没跑，而载荷里的 alert 一直停在 fresh）。所以除了 kind，还把阈值与文案模板一起下发
    （`stale_after_hours` / `stale_text`），组件心跳里拿 `generated_at` 做减法、复用同一口径。
    本函数仍保留该分支：别的调用方（测试、手工检查脚本）可以带着 `now` 来问同一个问题。
    """
    state = str((payload or {}).get("data_state") or "empty")
    gen = str(payload.get("generated_at") or "")
    today = (now or datetime.now(_TZ8)).strftime("%Y-%m-%d")
    if payload.get("version_change") and state in ("ok", "degraded"):
        kind = "version-change"
        text = payload["version_change"]["text"][:200]
        if state == "degraded":
            text += "；另有副源缺失，详情见数据状态"
        key = f"alert:version:{payload['version_change'].get('from')}:{payload['version_change'].get('to')}"
    elif state != "ok":
        note = str(payload.get("data_note") or "").strip()
        base = _STATE_ALERT.get(state, f"数据状态异常（{state}）")
        # 日期取"今天"而不是载荷的 generated_at：故障持续一周时，用载荷日期会算出同一个键、
        # 从此再也不提醒（单测抓到的就是这个）。按天 => 每天最多响一次，但不会永远沉默。
        kind = state
        text = (base + (f"：{note}" if note else ""))[:200]
        key = f"alert:{state}:{today}"
    else:
        gaps = [g for g in (payload.get("series_gaps") or []) if isinstance(g, dict) and g.get("label")]
        if gaps:
            # 一族全没了比"这轮有几行改价"值得知道得多，但去重仍按 kind+日期（与其余异常同一
            # 说法）：同一天里第二条族消失会被并进同一条通知的文案，不会另响一声。
            listed = "、".join(f"{g['label']}（{g.get('n_prev', '?')} 档）" for g in gaps[:3])
            more = f" 等 {len(gaps)} 条" if len(gaps) > 3 else ""
            kind = "series-gone"
            text = GONE_ALERT_TEXT.replace("{n}", str(len(gaps))).replace(
                "{list}", listed + more)[:200]
            key = f"alert:series-gone:{today}"
        else:
            moves = [m for m in (payload.get("moves") or []) if isinstance(m, dict) and m.get("text")]
            cum = payload.get("cumulative") or {}
            if moves:
                # 达门槛的变动：这是"好消息"不是故障，所以 tone=info（组件与爬虫据此选标题）。
                # 去重仍按 kind+日期：一天最多响一次，同一天里后续轮次并进同一条文案不另响。
                kind = "moved"
                text = moves_text(moves)
                key = f"alert:moved:{today}"
            elif cum and (cum.get("items") or []) and int(cum.get("steps") or 1) >= 2:
                # 相邻两轮都不过门槛、跟更早一轮比才过门槛：这是"攒出来的"变动，
                # 单独一个 kind（也单独一个去重键），不与 moved 抢同一天那一次响
                kind = "moved-cumulative"
                text = cumulative_text(cum)
                key = f"alert:moved-cumulative:{today}"
            else:
                age_h: float | None = None
                try:
                    age_h = ((now or datetime.now(_TZ8))
                             - datetime.strptime(gen, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_TZ8)
                             ).total_seconds() / 3600.0
                except ValueError:
                    pass                                # 时间戳读不出来就别猜龄
                # 算龄这条嵌在"没有族缺口、也没有达门槛变动"里面是有讲究的：三条都成立时
                # 靠外的已经定了结论，放出去共用一个 else 会把前面的结论覆回 fresh
                # （第一版就是这么写错的，单测抓到）。
                if age_h is not None and age_h > STALE_ALERT_HOURS:
                    kind = "not-running"
                    text = STALE_ALERT_TEXT.replace("{h}", str(int(age_h)))
                    key = f"alert:not-running:{today}"
                else:
                    kind, text, key = "fresh", "", str(payload.get("content_hash") or gen)
    tone = "info" if kind in ("fresh", "moved", "moved-cumulative", "version-change") else "warn"
    return {"kind": kind, "text": text, "key": key, "tone": tone,
            "label": KIND_LABEL.get(kind, kind), "hint": move_hint(),
            "last": payload.get("last_moves") or {},
            "stale_after_hours": STALE_ALERT_HOURS, "stale_text": STALE_ALERT_TEXT}


def esc(s: str) -> str:
    """把文本转义进 AppleScript 的双引号字面量：只处理反斜杠与双引号。

    通知兜底走 `osascript -e 'display notification "..."'`；消息里未转义的双引号会
    提前截断字符串，反斜杠会吃掉后一个字符。这里只做这一件事，不做 HTML 转义。
    """
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def notify_fresh(payload: dict) -> None:
    """爬虫侧的投递：**正常更新不出声**，只有异常（或组件没跑）才弹系统通知。

    组件在跑时角标由它自己亮，这里再弹就是重复；组件没跑时角标也不会有，所以
    "有更新"这件事至少说一次 —— 这是唯一不留静默空洞的写法。
    """
    if not payload:
        return
    a = payload.get("alert") or alert_policy(payload)
    kind = str(a.get("kind") or "fresh")
    key = str(a.get("key") or "")
    if not key:
        return
    # 与 app 共享去重戳（同一个 .last_notified 文件）：app 弹过并写戳后即便退出，
    # 本轮爬虫也不该对同一条内容再弹一次 osascript —— 否则跨会话重复骚扰。
    stamped = read_stamp()
    if stamped == key:
        log("内容指纹命中（.last_notified），跳过 osascript 兜底")
        return
    if kind == "fresh" and widget_running():
        log("通知策略：fresh 且组件在跑 → 不出声（角标由组件亮）")
        return
    n_models = len(payload.get("models", []))
    n_changes = len(payload.get("changes", []))
    at = payload.get("generated_at", "")
    if kind == "fresh":
        # 只有组件没跑才会走到这里：那时角标也不会动，不弹就什么都不剩
        title = "ModelRadar 数据已更新"
        msg = (f"{n_changes} 个模型有变动" if n_changes else f"已刷新 {n_models} 个模型") \
              + "（组件没在运行，角标也不会动）"
        sub = f"抓取于 {at}"
    else:
        # tone 由 alert_policy 给：moved（达门槛的正常变动）不是故障，标题不能说"数据异常"。
        # 旧载荷没有 tone 字段时按 kind 兜一个结论，不因字段缺失就退化成吓人的标题。
        tone = str(a.get("tone") or ("info" if kind in ("fresh", "moved") else "warn"))
        title = "ModelRadar 值得注意的变动" if tone == "info" else "ModelRadar 数据异常"
        msg = str(a.get("text") or f"数据状态异常（{kind}）")
        sub = f"抓取于 {at}" if at else f"状态 {kind}"

    # 通知出口按配置挑：配了中枢（`MR_HUB_NOTIFY`，来自 runtime.env）就走它 —— 中枢会落
    # 审计档、且装了 terminal-notifier 时通知可点击打开看板；没配就直接系统横幅。
    # 路径**不写死在代码里**：这份仓库要公开，而中枢在开发者个人机器的目录里，写死等于
    # 在公开树里留一个指向它的箭头。
    hub = mr_paths.HUB_NOTIFY
    dash = Path(__file__).resolve().parent / "dashboard.html"
    _stamp_notified(key)  # 先认领：宁可漏弹也不重复弹
    try:
        if hub and Path(hub).is_file():
            args = [hub, title, msg, "--source", "modelradar", "--subtitle", sub, "--no-sound"]
            if dash.exists():
                args += ["--open", dash.as_uri()]
            r = subprocess.run(args, capture_output=True, text=True, timeout=15)
            out_lines = (r.stdout or "").strip().splitlines()
            status_line = out_lines[-1] if out_lines else f"rc={r.returncode}"
        else:
            script = (f'display notification "{esc(msg)}" with title "{esc(title)}"'
                      f' subtitle "{esc(sub)}"')
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
            status_line = f"osascript rc={r.returncode}"
        log(f"通知 {status_line}（{kind}，去重键 {key[:8]}）")
        if "delivered=false" in status_line or r.returncode != 0:
            _stamp_notified(stamped)  # 失败回滚认领，别把这条内容永久吃掉
    except Exception as exc:  # noqa: BLE001
        _stamp_notified(stamped)  # 失败回滚认领
        log(f"通知失败：{exc}")


# --------------------------------------------------------------------------- 主流程

@network_budget(ROUND_NETWORK_BUDGET)
def run(sources: list[str], with_logos: bool, carry_from: dict | None = None) -> dict:
    cfg = load_config()
    # 来源：2026-10-30 用户截图，失败轮不能用残缺重算覆盖上一张可用图。
    try:
        published = json.loads(LATEST_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        published = {}
    if not isinstance(published, dict):
        published = {}

    status: dict[str, dict] = {}
    prev_con = ensure_db()
    prev_rows, first_base = load_previous(prev_con)   # 保存前先取，别把本次结果当成上一次
    prev_con.close()

    aa_rows: list[dict] = []
    or_models: dict[str, dict] = {}
    models_dev: dict[str, dict] = {}
    dl_rows: list[dict] = []
    usage: dict[str, dict] = {}
    usage_meta: dict = {}
    progress_rows: list[dict] = []
    progress_version = None
    task_rows: list[dict] = []
    con = ensure_db()

    if SRC_AA in sources:
        log("开始抓取 AA 榜单")
        try:
            with network_budget(SOURCE_NETWORK_BUDGET):
                page = fetch(SOURCES[SRC_AA]).decode("utf-8", "ignore")
                # 保留完整公开输入，未来可以按原轮次重解析，不用后来读数替代历史。
                save_snapshot(con, "aa_html", 1, True, None, [{"html": page}])
                con.commit()
                fetched_rows = parse_aa(page)
                version = evaluation_version(fetch(AA_METHODOLOGY).decode("utf-8", "ignore"))
            for r in fetched_rows:
                r["evaluation_version"] = version
            aa_rows = fetched_rows
            status[SRC_AA] = {"ok": True, "n": len(aa_rows), "ts": now_str(), "url": SOURCES[SRC_AA]}
            status[SRC_AA]["evaluation_version"] = version
            save_snapshot(con, SRC_AA, len(aa_rows), True, None, aa_rows)
            log(f"AA 榜单 OK：{len(aa_rows)} 行")
        except Exception as exc:  # noqa: BLE001
            status[SRC_AA] = {"ok": False, "err": str(exc), "ts": now_str(), "url": SOURCES[SRC_AA]}
            log(f"AA 抓取失败：{exc}")
            # 降级到最近一次成功的 AA 快照。AA 是主源，index / apt_usd / tok_s / ttft_s 全靠它，
            # 挂掉时绝不能退化成「只出 OpenRouter 价格行」——那会造出一批能力指标全为 None 的
            # 伪模型，散点图和图例因此全空、表格却塞满 70 多行没用的东西，比没数据更糟。
            stale_rows, stale_ts = last_good_snapshot(con)
            if stale_rows:
                aa_rows = stale_rows
                status[SRC_AA].update({"ok": True, "stale": True, "from": stale_ts, "n": len(stale_rows)})
                status[SRC_AA]["evaluation_version"] = version_of(stale_rows)
                log(f"AA 降级：沿用 {stale_ts} 的快照 {len(stale_rows)} 行")

    if SRC_PROGRESS in sources:
        log("开始抓取 AA Progress")
        try:
            with network_budget(SOURCE_NETWORK_BUDGET):
                page = fetch(SOURCES[SRC_PROGRESS]).decode("utf-8", "ignore")
                try:
                    progress_rows, progress_version = parse_progress(page)
                except ValueError as exc:
                    if "未唯一识别" not in str(exc):
                        raise
                    # 上游页面偶发同时挂出两个都能辨识的数据包。重取一次再判：
                    # 这是把「偶发页面形状」和「真的分不清」分开，不是替它挑一个。
                    log("Progress 数据包不唯一，重取一次页面再判")
                    page = fetch(SOURCES[SRC_PROGRESS]).decode("utf-8", "ignore")
                    progress_rows, progress_version = parse_progress(page)
            status[SRC_PROGRESS] = {"ok": True, "n": len(progress_rows), "ts": now_str(),
                                    "url": SOURCES[SRC_PROGRESS], "evaluation_version": progress_version}
            save_snapshot(con, SRC_PROGRESS, len(progress_rows), True, None, progress_rows)
            log(f"Progress v{progress_version} OK：{len(progress_rows)} 个型号档位")
        except Exception as exc:
            status[SRC_PROGRESS] = {"ok": False, "err": str(exc), "ts": now_str(), "url": SOURCES[SRC_PROGRESS]}
            log(f"Progress 抓取失败：{exc}（不使用旧分数补齐）")

    if SRC_AA_TIMES in sources:
        log("开始抓取 AA 每任务耗时")
        try:
            with network_budget(SOURCE_NETWORK_BUDGET):
                page = fetch_task_manifest(SOURCES[SRC_AA_TIMES]).decode("utf-8", "ignore")
                task_rows = parse_task_times(page)
            # 版本只认 AA 官方当前方法，不从 Progress 或抓取日期推断。
            task_version = status.get(SRC_AA, {}).get("evaluation_version")
            for r in task_rows:
                r["evaluation_version"] = task_version
            status[SRC_AA_TIMES] = {"ok": True, "n": len(task_rows), "ts": now_str(),
                                    "url": SOURCES[SRC_AA_TIMES], "evaluation_version": task_version}
            save_snapshot(con, SRC_AA_TIMES, len(task_rows), True, None, task_rows)
            log(f"AA 每任务耗时 OK：{len(task_rows)} 行（版本 {task_version or '未知'}）")
        except Exception as exc:  # noqa: BLE001
            status[SRC_AA_TIMES] = {"ok": False, "err": str(exc), "ts": now_str(),
                                    "url": SOURCES[SRC_AA_TIMES]}
            log(f"AA 每任务耗时抓取失败：{exc}（值留空，不估算）")

    if SRC_OR in sources:
        log("开始抓取 OpenRouter 报价")
        try:
            with network_budget(SOURCE_NETWORK_BUDGET):
                or_models = parse_openrouter(fetch(SOURCES[SRC_OR]))
            # 解析到 0 条不是"成功且没数据"：OpenRouter 的价目表不可能为空，
            # 真为空只可能是页面结构变了或返回了空壳。当成失败，别让看板亮绿显示"0 条"。
            if not or_models:
                raise RuntimeError("解析到 0 个模型（页面结构变了或返回空壳）")
            status[SRC_OR] = {"ok": True, "n": len(or_models), "ts": now_str(), "url": SOURCES[SRC_OR]}
            save_snapshot(con, SRC_OR, len(or_models), True, None, [
                {"id": k, **v} for k, v in list(or_models.items())[:2000]])
            log(f"OpenRouter OK：{len(or_models)} 个模型")
        except Exception as exc:  # noqa: BLE001
            status[SRC_OR] = {"ok": False, "err": str(exc), "ts": now_str(), "url": SOURCES[SRC_OR]}
            log(f"OpenRouter 抓取失败：{exc}")

    if SRC_MODELS_DEV in sources:
        log("开始抓取 models.dev 目录")
        try:
            with network_budget(SOURCE_NETWORK_BUDGET):
                models_dev = parse_models_dev(fetch(SOURCES[SRC_MODELS_DEV]))
            matched = enrich_openrouter(or_models, models_dev)
            status[SRC_MODELS_DEV] = {
                "ok": True, "n": len(models_dev), "matched": matched,
                "ts": now_str(), "url": SOURCES[SRC_MODELS_DEV],
            }
            save_snapshot(con, SRC_MODELS_DEV, len(models_dev), True, None,
                          list(models_dev.values())[:5000])
            log(f"models.dev OK：{len(models_dev)} 个模型，匹配 OpenRouter {matched} 个")
        except Exception as exc:
            status[SRC_MODELS_DEV] = {
                "ok": False, "err": str(exc), "ts": now_str(),
                "url": SOURCES[SRC_MODELS_DEV],
            }
            log(f"models.dev 抓取失败：{exc}（不覆盖 OpenRouter 数据）")

    if SRC_OR_USAGE in sources:
        log("开始抓取 OpenRouter 周用量")
        try:
            with network_budget(SOURCE_NETWORK_BUDGET):
                usage, usage_meta = parse_openrouter_usage(fetch(SOURCES[SRC_OR_USAGE]))
            status[SRC_OR_USAGE] = {
                "ok": True, "n": len(usage), "ts": now_str(), "url": SOURCES[SRC_OR_USAGE],
                "date": usage_meta.get("date"),
            }
            log(f"OpenRouter 周用量 OK：{len(usage)} 个模型"
                f"（{usage_meta.get('date') or '日期未知'}）")
            if usage_meta.get("missing"):
                log(f"⚠️ OpenRouter 周用量有 {usage_meta['missing']} 行缺 token 字段，已跳过")
        except Exception as exc:  # noqa: BLE001
            status[SRC_OR_USAGE] = {
                "ok": False, "err": str(exc), "ts": now_str(), "url": SOURCES[SRC_OR_USAGE],
            }
            log(f"OpenRouter 周用量抓取失败：{exc}（图例退回仅按指数加权）")

    if SRC_DL in sources:
        log("开始抓取 DataLearner")
        try:
            with network_budget(SOURCE_NETWORK_BUDGET):
                dl_rows = parse_datalearner(fetch(SOURCES[SRC_DL]).decode("utf-8", "ignore"))
            if not dl_rows:
                raise RuntimeError("解析到 0 行（表格没找到或整列是占位符）")
            status[SRC_DL] = {"ok": True, "n": len(dl_rows), "ts": now_str(), "url": SOURCES[SRC_DL]}
            log(f"DataLearner OK：{len(dl_rows)} 行")
        except Exception as exc:  # noqa: BLE001
            status[SRC_DL] = {"ok": False, "err": str(exc), "ts": now_str(), "url": SOURCES[SRC_DL]}
            log(f"DataLearner 抓取失败：{exc}")

    if (SRC_AA in status and (status[SRC_AA].get("stale") or not status[SRC_AA].get("ok"))
            and published.get("models")):
        con.close()
        # 不改变数据时间、模型或历史。失败时间只留在对应来源状态中。
        published["sources"] = {**published.get("sources", {}), **status}
        published["sources"][SRC_AA].update(stale=True, **{"from": published.get("generated_at")})
        published["data_state"] = "stale"
        published["data_note"] = (f"本次 AA 抓取失败，保留 {published.get('generated_at', '未知时间')} 的页面；"
                                  + str(status[SRC_AA].get("err", ""))[:200])
        for key in ("changes", "moves", "series_gaps", "move_lines"):
            published[key] = []
        published["version_change"] = {}
        published["alert"] = alert_policy(published)
        write_outputs(published, published.get("logo_palette", {}))
        log("AA 抓取失败，保留上次页面内容与数据时间，不写入新历史")
        notify_fresh(published)
        return published

    # 无已发布页面时，按各源台账重建旧图。原始AA行不含任务耗时和Progress。
    for source in (SRC_PROGRESS, SRC_AA_TIMES):
        if source not in status or status[source].get("ok"):
            continue
        rows, observed = last_good_snapshot(con, source)
        if not rows:
            continue
        if source == SRC_PROGRESS:
            progress_rows = rows
            # Progress有独立评测版本，不能从AA评测版本推断。
            row = con.execute("SELECT evaluation_version FROM snapshots WHERE source=? AND ts=? AND ok=1",
                              (source, observed)).fetchone()
            progress_version = row[0] if row else None
        else:
            task_rows = rows
        status[source].update(stale=True, **{"from": observed})

    # 词表增量：本轮 AA 榜单里有当前词表一条都不命中的家族，就当场把派生增量并进配置。
    # 必须放在 want_row 过滤**之前**，否则新词要到下一轮才生效 —— 这次 Haiku 漏的就是
    # "手工刷新词表"这条路，抓取的每一轮都拿着 09-29 的旧快照在筛。
    # AA 降级沿用旧快照时不碰配置：那批行派生不出新东西，还在故障期间改公共输入。
    if aa_rows and not status.get(SRC_AA, {}).get("stale"):
        try:
            growth = grow_watchlist(aa_rows)
            if growth["expanded_by"]:
                log(f"词表名次线：基础 {growth['base_n']} → 实际 {growth['effective_top_n']}"
                    f"（历史关注厂商当前最高名次在扩边：{growth['expanded_by']}）")
            if growth["added"]:
                cfg["keyword_include"] = growth["new"]
                log(f"词表增量：新增 {len(growth['added'])} 个派生词 {growth['added']}"
                    f"（AA {len(aa_rows)} 行 → 保留 {growth['rows_before']}→{growth['rows_after']} 行）")
            else:
                # 空转也要留一行：静默和"这一步没执行"在看板上长得一样，会掩盖钩子掉线。
                log(f"词表增量：本轮无未覆盖的新家族（{len(aa_rows)} 行 / {len(cfg.get('keyword_include', []))} 词）")
        except Exception as exc:  # noqa: BLE001
            log(f"词表增量失败，本轮沿用现有词表：{exc}")

    # 零命中词要出声：整词边界把"写错的词"从"顺带多捞几家"换成了"静默一行不捞"，
    # 不报出来的话，改名或写法失配的关键词会安静地不再关注那个族。
    if aa_rows:
        dead = zero_hit_words(aa_rows, cfg.get("keyword_include", []))
        if dead:
            log(f"⚠️ 词表有 {len(dead)} 个词本轮一行都没命中：{dead}"
                "（模型掉榜属正常；若是改名或写法失配，把它换对或挪进 keyword_include_pinned）")

    # 合并：AA 缺失时退化为「只出 OpenRouter 价格行」
    # 先按 watchlist 过滤再入库：否则基线里全是 Grok/Muse 这类没关注的行，
    # 每次 diff 都会把它们报成「消失」，变动列表永远是噪音。
    kw_inc = cfg.get("keyword_include", [])
    kw_exc = cfg.get("keyword_exclude", [])
    aa_rows = [r for r in aa_rows if want_row(r, kw_inc, kw_exc)]

    models: list[dict] = []
    if aa_rows:
        models = merge(aa_rows, or_models, dl_rows, cfg)
    cfg["evaluation_version"] = version_of(models)
    version_changed = bool(models and prev_rows
                           and not same_evaluation_major(version_of(prev_rows.values()), version_of(models)))
    if version_changed:
        first_base = True
        log("AA 评测版本切换或旧版本未知：重建比较基线，不发送跨版逐模型涨跌")
    # AA 不可用且库里没有可用快照时，models 就保持空。
    # 这里刻意不补「只出 OpenRouter 价格行」的伪模型：那种行 index/tok_s 全是 None，
    # 进不了散点图也不进图例，只会把表格撑满无关的条目，看板反而更难用。

    # 被撤下但没被"全面优于"的行沿用回来（判据见 carry_forward）：换版不等于旧模型没价值。
    n_carry = carry_forward(models, con, or_models, dl_rows, cfg, now_str())
    n_prev = carry_from_payload(models, carry_from, now_str()) if not status.get(SRC_AA, {}).get("stale") else 0
    if n_prev:
        log(f"沿用公开站上一版 {n_prev} 行：上一版有、本轮榜上已撤，评测版本一致才带回来")
    if n_carry:
        log(f"沿用 {n_carry} 行：被榜单撤下但评测版本没变，按最后一次实测值带回来，"
             f"显式标「沿用 日期」，最长 {CARRY_MAX_DAYS} 天（with fallback 重复档不带回）")

    progress_matched = attach_progress(models, progress_rows,
                                      progress_version)
    if models and not status.get(SRC_AA, {}).get("stale") and status.get(SRC_PROGRESS, {}).get("ok") and not progress_matched:
        status[SRC_PROGRESS].update(ok=False, err="Progress 数据包与当前模型零匹配")
    if SRC_PROGRESS in status:
        status[SRC_PROGRESS]["matched"] = progress_matched
    if SRC_PROGRESS in sources:
        log(f"Progress 精确匹配 {progress_matched}/{len(models)} 档；缺失不记零")

    task_matched = attach_task_times(
        models,
        task_rows,
        status.get(SRC_AA, {}).get("evaluation_version"))
    if SRC_AA_TIMES in status:
        status[SRC_AA_TIMES]["matched"] = task_matched
        # 包解析成功但一行都没匹配上，不是“成功且没数据”：源状态必须下来。
        if status[SRC_AA_TIMES].get("ok") and not task_matched:
            status[SRC_AA_TIMES].update(ok=False, err="每任务耗时数据包与当前模型零匹配")
    if SRC_AA_TIMES in sources:
        log(f"每任务耗时精确匹配 {task_matched}/{len(models)} 档；缺失留空，不估算")
        n_tokens = sum(1 for m in models if m.get("output_tokens_per_task"))
        log(f"每任务输出量匹配 {n_tokens}/{len(models)} 档；缺时间时用它 ÷ 速度派生")

    n_carry_progress, n_carry_time = restore_carried_metrics(models, con, progress_version)
    if n_carry_progress or n_carry_time:
        log(f"沿用行补回同轮旧读数：历史参照 {n_carry_progress} 行 / 任务耗时 {n_carry_time} 行"
             f"（取各自沿用日期那一轮快照，版本不符或当期缺值留空）")

    usage_matched, usage_missing = attach_usage(models, or_models, usage, usage_meta)
    if usage_meta:
        log(f"图例用量：{usage_matched} 行匹配到 OpenRouter 周用量，"
            f"{usage_missing} 行无用量（不参与加权平均）")

    # 不可信观测的隔离：必须在推估**之前**。顺序反了的话，被隔离的 tok_s 会以"缺失"
    # 的身份进入 estimate_missing_tok_s，于是一个假值变成一排推估值，问题反而被放大。
    # 这里只改本轮用于比较与绘制的那份；历史入库另行按原始值写（见下面 series 那段）。
    q_round_ts = now_str()
    raw_for_history = [dict(m) for m in models]
    n_quarantined = apply_quarantine(models, q_round_ts, where="round")
    if not n_quarantined:
        # 本轮干净也要说一句：不报的话，"今天没有隔离"和"隔离这套掉线了"长得一样。
        active = len(load_quarantine())
        log(f"不可信观测隔离：本轮无命中（清单 {active} 条在册）")

    n_est, n_band = estimate_missing_tok_s(models)
    if n_est:
        log(f"缺 tok_s 的 {n_est} 个模型给了同族推估（tok_s 仍为 null：图上虚线环 + 环带=区间，"
            f"表格标「估」，不参与 Pareto 前沿）")
    if n_band:
        log(f"另有 {n_band} 行前三档全落空，只给同级能力参照带（不写中心值、不参与任何判据）")

    n_time_est, n_time_band = derive_missing_task_times(models)
    if n_time_est:
        log(f"缺任务耗时的 {n_time_est} 个模型由输出量 ÷ 速度派生（标「≈」；"
            f"速度实测的用实测，速度推估的继承同一层级）")
    if n_time_band:
        log(f"另有 {n_time_band} 行只有速度参照带，只给时间带（不写中心值）")

    # 梯队 / 离前沿距离 / 前沿成员：一次算完，两张渲染面只消费（必须在推估之后，
    # 否则 est/band 标记还没落下来，会有插值点混进前沿）。
    bands = tier_ladder(models)
    front = annotate_efficiency(models, bands)
    penalty = task_size_penalty(models)
    log(f"Pareto 前沿 {len(front)} 点（爬虫侧算一次，状态栏与看板共用同一结论）；"
        f"梯队 {len(bands)} 级（±{TIER_TOLERANCE_POINTS} 分起级"
        + (f"，最高级 {bands[0]['high']:g}–{bands[0]['low']:g}" if bands else "") + "）")
    log(f"任务耗时低分收缩 λ={penalty['lambda']:.4f}（{penalty['points']} 点 / "
        f"{penalty['families']} 族，剔除明确不推理 {penalty['excluded_non_reasoning']} 档）")

    try:
        with network_budget(10):
            guard_logos(models, with_logos)
    except Exception as exc:  # noqa: BLE001
        log(f"Logo 处理失败，不阻止数据发布：{exc}")
    # 厂商标识色：必须在 logo 落地之后派生（取色靠的是 SVG 源码里的 fill）
    palette = probe_palette()
    apply_vendor_colors(models, cfg, palette)
    # models 为空时 diff 会把上一次的所有模型报成「掉榜」，一堆假变动。
    # 没有当前数据就不该有变动，直接给空。
    # 同理，第一次以合并结果为基线时也不能 diff：那会把 74 个模型全报成「新进榜」，
    # 看着像榜单大洗牌。这时如实说"没有可比的上一次"，本轮不报变动。
    if not models:
        changes = []
    elif first_base:
        changes = []
        log("本轮没有可比的上一次合并结果（首次以合并结果为基线），不报变动")
    else:
        changes = diff_models(models, prev_rows)

    # 整族缺口：上一轮 ≥GAP_MIN_ROWS 档、这一轮一档都不剩。没基线就别报（同 diff 的规矩，
    # 否则首轮会把"没有可比的上一次"说成"某个族没了"）。
    series_gaps = [] if first_base or not models else series_gap(prev_rows.values(), models)
    if series_gaps:
        log("整族缺口 " + "、".join(
            f"{g['label']}（{g['n_prev']} 档→0）" for g in series_gaps[:5])
            + f"，共 {len(series_gaps)} 条 → 交给 alert_policy 决定是否出声")

    # 达门槛的变动（指数 >1 分 / 每任务成本 >10% / 输出速度 >10%）→ 值得弹一条文字通知。
    moves = notable_moves(changes)
    if moves:
        log("值得提醒的变动 " + "；".join(
            f"{m['name']} {m['label']} {m['amount']:+.0f}{m['unit']}" for m in moves[:5])
            + f"，共 {len(moves)} 项 → 交给 alert_policy 决定是否出声")

    if not status.get(SRC_AA, {}).get("stale") and models:
        save_diff_base(con, models)      # 降级旧值不是新观测，不重复写入历史
    # 本轮在台账里的那条 ts：浮层算"上次提示是往前第几轮"要对它，而不是对 generated_at
    # （两者各自调一次 now_str()，跨秒就错开一格）
    diff_row = con.execute("SELECT ts FROM snapshots WHERE source=? AND ok=1 "
                           "ORDER BY ts DESC LIMIT 1", (SRC_DIFF,)).fetchone()
    diff_ts = str(diff_row[0]) if diff_row else now_str()
    # 基线刚落库就往回翻。顺序是判据（TestBacktrack.test_调用点在本轮落库之后）：
    # 台账第 0 条必须是本轮，否则本轮会被当成"上一轮"、把刚发生的变动报成几轮前
    # 累计块先算：它的 keys 要一起喂给下面两路回溯（同一件事不能既当"本轮"又当"上次"）。
    cumulative = cumulative_moves(con, models, cfg)
    # skip_keys 是本轮浮层已经会念到的**事件**（模型+字段，不含数值）：翻到同一件事就继续往前。
    # 比指纹不比字符串 —— 同一件事的百分比在相邻两轮里漂一点，那还是同一件事，不是两次变更。
    already_keys = set(move_keys(moves)) | {str(x) for x in (cumulative.get("keys") or []) if x}
    # 相邻那一路：本轮有变动时它停在 steps=0（就是本轮）；本轮安静时它往前翻，
    # 但**同样跳过与本轮同一件事的那些轮**（用户 10-15 指定：这一路也用事件指纹）。
    last_moves = last_notable_round(con, moves, cfg, skip_keys=already_keys)
    # 再一路：**强制**往前翻（不吃本轮 moves），得到"上一次有达门槛变动的轮次"。
    # 与 last_moves 的区别只在 moves 传空：本轮有变动时 last_moves 停在 steps=0，
    # 浮层于是只剩"本轮这一批"，读者看不到上一次那批是什么 —— prev_moves 补的就是这一格。
    prev_moves = last_notable_round(con, [], cfg, skip_keys=already_keys)
    metric_views = {}
    for metric in ("tok_s", "time_per_task_s"):
        selected = notable_moves(changes, metric)
        cum = cumulative_moves(con, models, cfg, metric=metric)
        keys = set(move_keys(selected)) | set(cum.get("keys") or [])
        metric_views[metric] = {
            "moves": selected, "move_lines": merged_move_lines(selected, changes),
            "cumulative": cum,
            "last_moves": last_notable_round(con, selected, cfg, skip_keys=keys, metric=metric),
            "prev_moves": last_notable_round(con, [], cfg, skip_keys=keys, metric=metric),
        }
    # 上一次真的判定为"会出声"的那条（只有爬虫一侧写，见 write_last_alert）
    last_alert = read_last_alert(diff_ts)
    if version_changed:
        last_alert = {}
    if last_alert.get("ts"):
        last_alert["steps"] = round_steps(con, str(last_alert["ts"]))
    series_ts = now_str()
    # 台账写**原始观测**，不写隔离后的值：隔离是"别拿它算涨跌"，不是"它没发生过"。
    # 反过来做的话，人工事后想查"当时到底收到什么"就永远查不到了。
    for r in raw_for_history:
        if status.get(SRC_AA, {}).get("stale") or r.get("carried_asof"):
            continue
        for f in HISTORY_FIELDS:
            if f == "time_per_task_s" and status.get(SRC_AA_TIMES, {}).get("stale"):
                continue
            if isinstance(r.get(f), (int, float)):
                con.execute("INSERT OR REPLACE INTO series(ts,name,field,value,evaluation_version) VALUES(?,?,?,?,?)",
                            (series_ts, r["name"], f, float(r[f]), r.get("evaluation_version")))
    con.commit()
    n_snap, n_series = prune_history(con)
    if n_snap or n_series:
        log(f"台账保留最近 {KEEP_SNAPSHOT_DAYS} 天：清掉 snapshots {n_snap} 行 / series {n_series} 行")
    con.close()

    aa_status = status.get(SRC_AA, {})
    # 副源缺失也必须反映到 data_state：以前只看 AA，于是"AA 正常、OpenRouter/DataLearner 全挂"
    # 这种降级运行被判成完全成功 —— 看板一路亮绿，定时任务也不再重试，而图上那些价其实是旧的。
    # 前端与渲染层对"非 ok"已有统一的标黄处理，这里只多给一个更准的状态名。
    missing = [s for s in (SRC_OR, SRC_DL, SRC_OR_USAGE, SRC_PROGRESS, SRC_AA_TIMES)
               if s in status and not status[s].get("ok")]
    SRC_LABEL = {SRC_OR: "OpenRouter 报价", SRC_DL: "DataLearner 镜像", SRC_PROGRESS: "AA Progress",
                 SRC_OR_USAGE: "OpenRouter 周用量", SRC_AA_TIMES: "AA 每任务耗时",
                 SRC_MODELS_DEV: "models.dev 目录"}
    sub_note = ("另有源缺失：" + "、".join(
                    f"{SRC_LABEL.get(s, s)}（{str(status[s].get('err', ''))[:40]}）"
                    for s in missing)) if missing else ""
    payload = {
        "generated_at": aa_status.get("from") if aa_status.get("stale") else now_str(),
        "sources": status,
        "models": models,
        "evaluation_version": version_of(models),
        "version_change": version_change_summary(prev_rows, models),
        "changes": changes,
        "rename_lines": rename_lines(changes),
        # 达门槛、值得弹一条文字通知的那几项（判据在 notable_moves，组件只消费结论）
        "moves": moves,
        "series_gaps": series_gaps,
        # 给人看的位数也随载荷发布：看板与状态栏读这一份，不在 JS 里再抄一张表。
        # 11-04 起速度/延迟改读 RSC 原始浮点（旧文本是上游格式化过的整数/两位），
        # 显示形态要维持旧口径，就必须只有一个来源，否则两面会一个显示 123、一个显示 123.289。
        "display_decimals": DISPLAY_DECIMALS,
        # 图例顺序在这里算完（梯队 + 指数×log(1+平衡用量) 加权的离前沿距离），
        # 两张渲染面只按这份列表排：JS 再算一遍就会和状态栏各说一套，距离口径也一样会漂。
        "legend": build_legend(models, bands, usage_available=bool(usage_meta)),
        # 用量口径与覆盖范围一并给出：读者能看出这轮图例到底用了多少真实流量，
        # 以及哪些行因为没有用量被排除在加权平均之外。
        "usage": {
            "source": SRC_OR_USAGE,
            "ok": bool(usage_meta),
            "window": "week",
            "date": usage_meta.get("date"),
            "total_requests": usage_meta.get("total_requests"),
            "total_tokens": usage_meta.get("total_tokens"),
            "matched_models": usage_matched,
            "missing_models": usage_missing,
            "err": None if usage_meta else str(status.get(SRC_OR_USAGE, {}).get("err", "")),
        },
        # 梯队是怎么切出来的也一起给出去：判据要能独立复算这个阶梯，
        # 渲染面/以后任何解释都无需再猜"这一档为什么是这一档"。
        "tier_bands": bands,
        "tier_tolerance_points": TIER_TOLERANCE_POINTS,
        # 任务耗时模式下"分数低就按比例缩小"的全局强度，判据与两面共用这一份。
        # 不逐模型发 factor：它可由 index/global_max_index 与 λ 直接算出，
        # 多发一份字段只会多一个能和 λ 对不上的地方。
        "task_size_penalty": penalty,
        "watchlist": cfg.get("keyword_include", []),
        "brand_color": cfg.get("vendor_brand_color", {}),
        # 取色来源（svg/curated/generated/override[/+split/+collision]）一并给出：
        # 看板与回归判据要能区分"这就是它家品牌色"和"没信号、按名生成的"。
        "brand_color_source": cfg.get("vendor_brand_color_source", {}),
        # 看板拿这几个字段决定要不要显示「数据陈旧 / 无数据」的提示。
        # 判据在 Python 端算好，前端只消费——避免同一套判断在两边各写一遍还不一致。
        "data_state": ("stale" if aa_status.get("stale")
                       else "empty" if not aa_status.get("ok")
                       else "degraded" if missing
                       else "ok"),
        "data_note": "；".join(x for x in (
                      (f"AA 本次抓取失败（{aa_status.get('err','')[:60]}），"
                       f"当前显示 {aa_status.get('from','')} 的快照，共 {len(models)} 个模型"
                       if aa_status.get("stale")
                       else "AA 源本次抓取失败，且本地没有可用快照，能力指标全部缺失"
                       if not aa_status.get("ok") and not models
                       else ""), sub_note) if x),
    }
    payload["last_moves"] = last_moves
    payload["prev_moves"] = prev_moves      # 强制往前翻那一路（本轮有变动时也看得到上次那批）
    payload["last_alert"] = last_alert      # 上一次判定为会出声的那条通知原文
    payload["diff_ts"] = diff_ts
    payload["cumulative"] = cumulative
    payload["move_lines"] = merged_move_lines(moves, changes)   # 浮层列合并后的，一档一行会淹没重点
    payload["content_hash"] = content_fingerprint(models)   # 内容指纹：同内容重抓不重复弹通知
    # 通知策略在 Python 侧算完，组件只消费同一个结论（见 alert_policy 上方注释）。
    payload["alert"] = alert_policy(payload)
    payload["metric_views"] = metric_views
    for metric, view in metric_views.items():
        view["alert"] = alert_policy({**payload, **view})
        view["alert"]["hint"] = move_hint(metric)
    write_outputs(payload, palette)
    # 渲染已经吃完 last_alert（上一轮的记录），现在才把本轮该不该出声落下去 ——
    # 反过来的话浮层会把刚弹的这条当成"上次提示"
    if should_record_alert(payload["alert"], read_stamp()):
        write_last_alert(alert_record(payload, payload["alert"]))
    log(f"已写出 {len(models)} 个模型（{payload['data_state']}），变动 {len(changes)} 条"
        f"，通知={payload['alert']['kind']} -> {LATEST_JSON.name}")
    notify_fresh(payload)   # 新数据更新提示（兜底：app 没运行时弹系统通知）
    maybe_summarize(payload)   # 可选：拉一个后台进程把变动写成一段话（不等待、失败不影响数据）
    return payload


def run_scheduled(sources: list[str], with_logos: bool) -> None:
    """按计划模式（launchd 每日触发一次）运行带失败退避的抓取。

    跑一次；若结果不是 fresh-ok（AA 主源失败/陈旧/空，或整轮抛异常），
    按指数退避重试：60s → 120 → 240 → … → 封顶 3600s（1h），到封顶后
    持续以 1h 为间隔重复，直到拿到 fresh 数据或当日窗口耗尽。

    对应需求：正常每天一次即可；失败后从 1 分钟起指数退避、封顶 1 小时级重复。
    """
    import random
    attempt = 0
    max_attempts = 24
    deadline = time.time() + 20 * 3600  # 当日窗口上限，避免跨日吊死一个进程
    # 来源：2026-11-07/08 早晨 DNS 瞬落实测。只在 scheduled 路径、第一次抓取之前等一次，
    # 等待不消耗 attempt 预算；就绪后不再重复探测（任何时候都可能有新的短暂抖动，
    # 那种抖动交给抓取失败后的退避重试处理，不必在每轮前面都加一道门）。
    wait_for_network([SOURCES[SRC_AA], SOURCES[SRC_OR]])
    while True:
        attempt += 1
        try:
            payload = run(sources, with_logos)
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            payload = None
        state = (payload or {}).get("data_state")
        # 重试门只看主源（AA）拿没拿到：degraded 意为"图画得出来，只是某个副源缺"，
        # 为它把当天余下的重试全用完没意义（副源常常连续几天都是坏的）。
        if state in ("ok", "degraded"):
            log(f"[scheduled] 第 {attempt} 次拿到 fresh 数据（{state}），退出")
            if not render_chart_for_app():
                # 数据已落盘（write_outputs 在 run() 内完成），但图表没画出来。
                # 非零退出码 3 让 launchd 日志/用户能察觉「抓到却没渲染」；app 仍会凭
                # latest.json 的 mtime 弹更新通知（通知走 latest.json，不依赖 chart.html）。
                log("[scheduled] 数据已落盘但图表重画失败 -> 退出码 3 标记故障")
                sys.exit(3)
            return
        if attempt >= max_attempts or time.time() >= deadline:
            log(f"[scheduled] 已重试 {attempt} 次仍未拿到 fresh 数据，放弃本轮（次日再试）")
            sys.exit(2)  # 非零退出，便于 launchd 日志/用户察觉当日抓取全失败
        # ±10% 抖动放在 min() 之内，封顶恒为 3600s（原先 += 会让封顶变 3600~3960 且单边）
        delay = min(60 * (2 ** (attempt - 1)) * random.uniform(0.9, 1.1), 3600)
        delay = min(delay, max(0.0, deadline - time.time()))  # 不要跨过当日窗口
        log(f"[scheduled] 第 {attempt} 次 data_state={state}，{int(delay)}s 后重试（封顶 3600s）")
        time.sleep(delay)


def render_chart_for_app() -> bool:
    """每日定时抓取成功后，重画状态栏 app 用的 chart.html。

    手动抓取走 crawl.sh（内含 render）；但 launchd 定时路径直达本脚本、不含 render。
    若不补这一句，app 侧的 chart.html mtime 永不变 -> tick 不重画、notifyIfNewData 不触发，
    '新数据更新提示'在每日自动路径上整条失效。

    返回 True/False 供调用方决定退出码——render 失败必须显式暴露（非零退出），
    不能静默吞掉后仍以 0 退出，否则 launchd 日志与用户都看不到「抓到了但没画出来」。
    """
    out = mr_paths.CHART_HTML
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([sys.executable, str(HERE / "render_scatter_svg.py"), "-o", str(out)],
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            log(f"[scheduled] 重画 app 图表失败（exit {r.returncode}）："
                f"{(r.stderr or b'').decode('utf-8', 'ignore')[-300:]}")
            return False
        log(f"[scheduled] 已重画 app 图表 -> {out}")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"[scheduled] 重画 app 图表失败（不影响数据落盘）：{exc}")
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description="ModelRadar 抓取器")
    ap.add_argument("--loop", type=int, default=0, help="间隔秒数循环抓取，0=只跑一次")
    ap.add_argument("--scheduled", action="store_true",
                   help="按计划运行：跑一次；非 fresh-ok 时按 1m→…→1h 指数退避重试，"
                        "直到拿到新数据或当日重试额度耗尽")
    ap.add_argument("--sources", default="aa,aa_progress,aa_times,openrouter,models_dev,datalearner,openrouter_usage",
                    help="要抓的源（默认含 AA 每任务耗时与 OpenRouter 周用量）")
    ap.add_argument("--no-logos", action="store_true", help="不下载厂商 logo")
    ap.add_argument("--refresh-watchlist", action="store_true",
                    help="只做一件事：抓 AA 榜单，按前 N 名派生 keyword_include 写回 "
                         "config/watchlist.json，然后退出（不写 latest.json、不画图表）。"
                         "N 是基础值，历史关注厂商掉出后按它当前最高名次扩边")
    ap.add_argument("--watchlist-top", type=int, default=WATCHLIST_TOP_N,
                    help=f"派生关键词的基础名次线，默认 {WATCHLIST_TOP_N}"
                        "（历史关注厂商当前最高名次更靠后时会按它扩边，只增不减）")
    ap.add_argument("--quarantine-list", action="store_true",
                    help="只读：列出在册的不可信观测与故障日志条数，然后退出（不抓取、不写盘）")
    ap.add_argument("--carry-from", metavar="LATEST.json",
                    help="上一版已发布的 latest.json：把上一版有、本轮已撤的行接回来当悬停历史"
                         "（公开站每轮换 runner，没有本地台账时靠它续链）")
    args = ap.parse_args()

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    carry_from = None
    if args.carry_from:
        if args.scheduled or args.loop:
            ap.error("--carry-from 只用于单轮抓取")
        try:
            carry_from = json.loads(Path(args.carry_from).read_text(encoding="utf-8"))
            if (not isinstance(carry_from, dict) or carry_from.get("data_state") != "ok"
                    or not isinstance(carry_from.get("models"), list) or not carry_from["models"]):
                ap.error("上一版载荷缺少完整数据")
        except (OSError, ValueError) as exc:
            ap.error(f"读不到上一版载荷：{exc}")
    if args.quarantine_list:
        rules = load_quarantine()
        if not rules:
            print("隔离清单为空：所有观测都会被当成可信数据。")
        for i, q in enumerate(rules, 1):
            print(f"{i}. {q.get('name')} · {q.get('field')}"
                  f" · {q.get('from_ts') or '-∞'} ~ {q.get('to_ts') or '+∞'}")
            print(f"   源 {q.get('source') or '?'}，筛查于 {q.get('checked_at') or '?'}")
            print(f"   理由：{q.get('reason') or '（未写）'}")
        n = 0
        if FAULT_LOG.exists():
            with FAULT_LOG.open(encoding="utf-8") as fh:
                n = sum(1 for line in fh if line.strip())
        print(f"\n在册 {len(rules)} 条；故障日志 {n} 行 -> {FAULT_LOG}")
        print(f"清单真身 -> {QUARANTINE_PATH}")
        return
    if args.refresh_watchlist:
        # 派生只看 AA 主表名次，所以不必惊动另外两个源
        aa_rows = parse_aa(fetch(SOURCES[SRC_AA]).decode("utf-8", "ignore"))
        r = refresh_watchlist(aa_rows, args.watchlist_top)
        log(f"watchlist 已按 AA 前 {r['effective_top_n']} 名重算"
            f"（基础 {r['base_n']} 名"
            + (f"，历史关注厂商在扩边：{r['expanded_by']}" if r["expanded_by"] else "")
            + f"）：派生 {len(r['derived'])} 词 + pinned → 共 {len(r['new'])} 词")
        log("  " + " ".join(r["new"]))
        if r["dropped"]:
            log(f"⚠️ 相比上一版去掉了 {len(r['dropped'])} 个词：{r['dropped']}")
            log("   若其中有你想长期盯的（哪怕它掉出前 N 名），加进 config/watchlist.json 的"
                " keyword_include_pinned，下次刷新不会被冲掉")
        kept = [x["name"] for x in aa_rows
                if want_row(x, r["new"], load_config().get("keyword_exclude", []))]
        log(f"  影响：AA {len(aa_rows)} 行 → 保留 {len(kept)} 行")
        dead = zero_hit_words(aa_rows, r["new"])
        if dead:
            log(f"⚠️ 其中 {len(dead)} 个词在榜单上零命中：{dead}"
                "（多半是 pinned 的掉榜模型；若是写法失配，整词边界下它会静默不再捞任何行）")
        return
    if args.scheduled:
        run_scheduled(sources, not args.no_logos)
        return
    while True:
        try:
            payload = run(sources, not args.no_logos, carry_from)
            if not args.loop and payload.get("data_state") in ("empty", "stale"):
                sys.exit(2)
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            if not args.loop:
                sys.exit(1)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
