#!/usr/bin/env python3
"""把一次抓取的变动写成一段人话总结。

分工是刻意的，而且不可交换：

  · Python 算事实 —— 谁变了、从几到几、幅度多少、几个新上榜、哪些值第一次出现。
  · 模型只许把这张"事实卡"改写成 2~4 句中文，**不许自己算任何数**。
  · 回读校验 —— 文本里出现的每个数字都必须在事实卡里找得到；有一个对不上就整段丢弃，
    退回"只显示表格"。这条是整个方案的支点：一段编得很顺的错误总结比没有总结更糟，
    而它看上去和正确总结完全一样。

为什么代码里没有写死任何 CLI 名字：命令行来自**本机运行时配置**
（`~/Library/Application Support/ModelRadar/summary_cmd` 或环境变量
MODELRADAR_SUMMARY_CMD），所以①发出去的源码不会暴露开发者用过哪个工具，
②换机器/换工具不用改代码，③没有配置就是"没有总结"，看板照常显示表格，不是报错。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import mr_paths

TIMEOUT_SEC = 90           # 一次总结的时间预算；超了就当"这次没有总结"
MAX_CHARS = 600            # 状态栏那一眼看完的长度
MIN_CHARS = 24             # 短到这个数基本等于模型在说废话，不如不显示

# 事实卡里会出现的数字来源：from/to/delta/pct + 计数。校验以"值"比而不是以字符串比，
# 否则 24.0 与 24 会被判成两个数（那样判据天天误杀，最后没人信它）。
_NUM = re.compile(r'(?<![\w.])\d+(?:\.\d+)?(?![\w.])')


def numbers_in(s: str) -> set[str]:
    """文本里的阿拉伯数字（原样字符串）。"""
    return set(_NUM.findall(s))


def _floats(s: str) -> set[float]:
    return {float(x) for x in _NUM.findall(s)}


def check_numbers(text: str, facts: str) -> list[str]:
    """返回文本里"事实卡给不出"的数字（字符串形式）。空列表 = 没有一个数是编的。

    只容忍同一数值的正当写法：四舍五入（9.3 写成 9）、去掉末尾零（24.0 写成 24）。
    **不留任何相对容差**：留了 2% 的话，"100%"被写成"102%"就查不出来了，
    而这条判据存在的全部意义就是"模型不许造数"。
    """
    have = _floats(facts)
    bad = []
    for tok in sorted(numbers_in(text), key=float):
        v = float(tok)
        dec = len(tok.split('.')[1]) if '.' in tok else 0
        if v in have or any(round(h, dec) == v for h in have) or any(h == v for h in have):
            continue
        bad.append(tok)
    return bad


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def build_factsheet(changes: list[dict], generated_at: str,
                    content_hash: str = "", weekly: list[dict] | None = None) -> str:
    """把变动压成一张给模型看的事实卡。没有变动就返回空串（不去打扰模型）。

    只写算完的结论，不写原始 JSON：模型看到的每一个数都必须是可直接搬运的。
    weekly 是"近 7 天"那一段（由爬虫从 series 表算好传进来）—— 单日 diff 常常只有
    零点几个点，读者真正想知道的是这一周往哪走，所以这一栏才是总结的主体。
    """
    if not changes:
        return ""
    from fetch_model_data import rename_lines
    renamed = [c for c in changes if c.get("type") == "renamed"]
    changed = [c for c in changes if c.get("type") == "changed"
               or (c.get("type") == "renamed" and not c.get("quiet"))]
    new = [c for c in changes if c.get("type") == "new"]
    gone = [c for c in changes if c.get("type") == "gone"]
    appear = [(c["name"], f) for c in changed for f in c["fields"] if f.get("kind") == "appeared"]
    vanish = [(c["name"], f) for c in changed for f in c["fields"] if f.get("kind") == "disappeared"]

    # 幅度最大的几条优先：事实卡要短，模型才不会被诱导去补它没看到的条目。
    movers = sorted(changed, key=lambda c: -max((abs(f["pct"]) for f in c["fields"]
                                                if f.get("pct") is not None), default=0))
    lines = []
    if weekly:
        lines.append(f"抓取时间 {generated_at}。近 7 天变化最大的 {len(weekly)} 项（这是读者最该知道的一段）：")
        for w in weekly[:6]:
            if w.get("tags"):
                lines.append("；".join(w["tags"]))
            lines.append(f"{w['name']}：{w['label']} {_fmt(w['from'])}→{_fmt(w['to'])}（{w['pct']:+.1f}%）")
        lines.append(f"本次（相对上一次抓取）有变动 {len(changes)} 个模型"
                     f"（数值变化 {len(changed)}、改名 {len(renamed)}、新上榜 {len(new)}、掉榜 {len(gone)}；改名可同时有数值变化），其中幅度最大的几条：")
        movers = movers[:5]
    else:
        lines.append(f"抓取时间 {generated_at}；本次有变动 {len(changes)} 个模型"
                     f"（数值变化 {len(changed)}、改名 {len(renamed)}、新上榜 {len(new)}、掉榜 {len(gone)}；改名可同时有数值变化）。")
        movers = movers[:8]
    for c in movers:
        for f in c["fields"]:
            if f.get("tags"):
                lines.append("；".join(f["tags"]))
            if f.get("pct") is None and f.get("kind") == "changed":
                continue
            if f.get("kind") == "appeared":
                lines.append(f"{c['name']}：{f['label']} 第一次有值 = {_fmt(f['to'])}")
            elif f.get("kind") == "disappeared":
                lines.append(f"{c['name']}：{f['label']} 这次缺失（原为 {_fmt(f['from'])}）")
            else:
                lines.append(f"{c['name']}：{f['label']} {_fmt(f['from'])}→{_fmt(f['to'])}"
                             f"（{f['delta']:+g}，{f['pct']:+.1f}%）")
    lines.extend(rename_lines(changes))
    if new:
        lines.append("新上榜：" + "、".join(c["name"] for c in new[:6]))
    if gone:
        lines.append("掉榜：" + "、".join(c["name"] for c in gone[:6]))
    if appear:
        lines.append(f"第一次出现实测值的字段共 {len(appear)} 处。")
    if vanish:
        lines.append(f"这次转为缺失的字段共 {len(vanish)} 处。")
    if content_hash:
        lines.append(f"内容指纹 {content_hash}。")
    return "\n".join(lines)


def build_argv(template: str, workdir: Path, outfile: Path, timeout: int,
               prompt: str) -> list[str]:
    """配置模板 + 提示词 → argv。**提示词永远是一个独立 argv，绝不拼进 shell 字符串。**

    事实卡里带着从远端页面抓来的模型名（今天是 "GPT-6 Sol (medium)"，明天可能有人把模型
    命名成 `$(rm -rf …)`）：只要把它拼进 `bash -c "…"` 就成了命令注入的入口。
    模板本身也只经 shlex 拆成参数、不经过 shell 执行，所以配置里写 `bash <脚本>` 也安全。
    """
    argv = []
    for tok in shlex.split(template):
        tok = tok.replace("{dir}", str(workdir)).replace("{out}", str(outfile)) \
                 .replace("{timeout}", str(timeout))
        argv.append(os.path.expanduser(tok))
    return argv + [prompt]


def cli_argv(template: str, workdir: Path, outfile: Path, timeout: int) -> list[str]:
    """只要 argv 的前半段（不含提示词），给判据与调试用。"""
    return build_argv(template, workdir, outfile, timeout, "")[:-1]


def resolve_cmd() -> str:
    """本机总结命令：环境变量优先，其次运行时配置文件。都没有 = 不做总结。"""
    env = os.environ.get("MODELRADAR_SUMMARY_CMD", "").strip()
    if env:
        return env
    p = Path(os.path.expanduser(str(mr_paths.SUPPORT / "summary_cmd")))
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        pass
    return ""


PROMPT = """你在给一个菜单栏工具写"今天模型榜单的变化"摘要。

硬规则：
1. 只许搬运下面事实卡里出现过的数字。不许相加、平均、换算、外推，不许引用任何外部知识。
2. 事实卡没说的原因不要猜（例如不要写"因为某厂发布了新版本所以…"）。
3. 2~4 句中文，先说最重要的变化，不超过 200 字。不要标题、不要列表符号、不要寒暄。
4. 不要输出"根据提供的数据"这类话，直接说结论。
5. 同族改名只说一句，不逐档复述，不说成新上榜或掉榜。未达提醒门槛不等于所有数值完全没变。

事实卡（唯一数据源）：
"""


def week_moves(con, days: int = 7, limit: int = 6) -> list[dict]:
    """从 series 表算"近 N 天变化最大"的几条。

    为什么要有这一段：台账里的 series 表每天写 ~440 行却没有任何读者，那就是纯写入放大；
    而单日 diff 常常只有零点几个点，读者真正想知道的是这一周往哪走。这一段就是它的读者。

    基线按 **(模型, 字段)** 各自取窗口内最早一条，而不是全局最早那一轮：窗口里可能同时
    躺着旧代次和当前代次的观测，全局最早那条会让所有模型都失去当前代次的基线。
    智能指数与每任务成本只接受与最新观测**同大版本**的基线（版本缺失或大版本不同一律不报）；
    速度、价格、首块延迟与评测版本无关，取窗口内最早一条即可。
    最新一轮里没有更早基线（首装、或换版后同版本只有一条）的 (模型, 字段) 直接跳过。
    沿用行与降级快照不应写入 series（抓取侧负责），这里不再识别。
    旧库没有 evaluation_version 列时查询报错，按现状返回空列表。
    """
    import sqlite3
    from datetime import datetime, timedelta
    from fetch_model_data import load_quarantine, _in_window
    rules = load_quarantine()
    con.create_function("mr_quarantined", 3, lambda name, field, ts: int(any(
        q.get("name") == name and q.get("field") == field and _in_window(ts, q)
        for q in rules)))
    try:
        t1 = con.execute("SELECT MAX(ts) FROM series").fetchone()[0]
        if not t1:
            return []
        since = (datetime.strptime(t1, "%Y-%m-%d %H:%M:%S") - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = con.execute(
            "SELECT c.name, c.field, s.value, c.value, s.evaluation_version, c.evaluation_version FROM series c "
            "JOIN series s ON s.name=c.name AND s.field=c.field "
            "WHERE c.ts=? AND c.value IS NOT NULL AND s.value IS NOT NULL "
            "AND NOT mr_quarantined(c.name,c.field,c.ts) "
            "AND s.ts = ("
            "  SELECT MIN(x.ts) FROM series x "
            "  WHERE x.name=c.name AND x.field=c.field "
            "  AND x.ts>=? AND x.ts<? AND x.value IS NOT NULL "
            "  AND NOT mr_quarantined(x.name,x.field,x.ts) "
            "  AND (c.field NOT IN ('index','apt_usd') "
            "       OR (c.evaluation_version IS NOT NULL "
            "           AND x.evaluation_version IS NOT NULL "
            "           AND substr(x.evaluation_version||'.',1,instr(x.evaluation_version||'.','.')-1) "
            "               =substr(c.evaluation_version||'.',1,instr(c.evaluation_version||'.','.')-1)))"
            ")", (t1, since, t1)).fetchall()
    except (sqlite3.Error, ValueError):
        return []
    label = {"index": "智能指数", "apt_usd": "每任务成本", "tok_s": "输出速度",
             "in_per_1m_usd": "输入单价", "out_per_1m_usd": "输出单价", "ttft_s": "首块延迟"}
    out = []
    for name, field, v0, v1, old_version, new_version in rows:
        if field not in label or not v0:
            continue
        out.append({"name": name, "field": field, "label": label[field],
                    "from": v0, "to": v1, "pct": (v1 - v0) / v0 * 100,
                    "tags": ([f"评测版本 {old_version} → {new_version}"]
                             if old_version and new_version and old_version != new_version else [])})
    out.sort(key=lambda d: -abs(d["pct"]))
    return out[:limit]


def run_summary(payload: dict, log=print) -> dict:
    """生成总结并落盘。永远不抛异常：总结是增强项，挂了不能拖累抓取。"""
    out = {"written": False, "state": "none", "why": "", "text": ""}
    changes = payload.get("changes") or []
    weekly: list[dict] = []
    try:
        import sqlite3
        with sqlite3.connect(str(mr_paths.DB_PATH)) as con:
            weekly = week_moves(con)
    except Exception as exc:  # noqa: BLE001
        log(f"  周变化读取失败（只用当日 diff 写事实卡）：{type(exc).__name__}: {exc}")
    facts = build_factsheet(changes, payload.get("generated_at", ""),
                            payload.get("content_hash", ""), weekly=weekly)
    if not facts:
        out["why"] = "没有变动，无需总结"
        return out
    cmd = resolve_cmd()
    if not cmd:
        out["state"] = "no-cli"
        out["why"] = "本机未配置总结命令（MODELRADAR_SUMMARY_CMD 或 summary_cmd 文件），只显示表格"
        log(f"  总结跳过：{out['why']}")
        return out

    data_dir = mr_paths.DATA
    data_dir.mkdir(parents=True, exist_ok=True)
    work = data_dir / ".summary_work"
    work.mkdir(exist_ok=True)
    outfile = work / "out.md"
    argv = build_argv(cmd, work, outfile, TIMEOUT_SEC, PROMPT + facts)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_SEC + 20,
                           stdin=subprocess.DEVNULL, cwd=str(work))
    except subprocess.TimeoutExpired:
        out["state"] = "timeout"
        out["why"] = f"总结命令超过 {TIMEOUT_SEC}s 没返回，只显示表格"
        log(f"  总结跳过：{out['why']}")
        return out
    except (OSError, ValueError) as exc:
        out["state"] = "spawn-failed"
        out["why"] = f"总结命令起不来（{type(exc).__name__}: {exc}），只显示表格"
        log(f"  总结跳过：{out['why']}")
        return out

    text = ""
    if outfile.exists():
        text = outfile.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        text = (r.stdout or "").strip()
    try:
        outfile.unlink()
    except OSError:
        pass
    if r.returncode != 0 or not text:
        out["state"] = "cli-failed"
        out["why"] = f"总结命令退出码 {r.returncode}、输出 {len(text)} 字符，只显示表格"
        log(f"  总结跳过：{out['why']}")
        return out

    # 只取正文第一段：模型偶尔会追加"以上就是……"或把事实卡复述一遍，那都不该进状态栏。
    text = re.sub(r'^[>\-\*\s]+', '', text.split("\n\n")[0]).strip()[:MAX_CHARS]
    bad = check_numbers(text, facts)
    if bad:
        out["state"] = "numbers-mismatch"
        out["why"] = f"文本里有事实卡给不出的数字 {bad[:5]}，整段丢弃，只显示表格"
        log(f"  总结丢弃：{out['why']}")
        return out
    if len(text) < MIN_CHARS:
        out["state"] = "too-short"
        out["why"] = f"总结只有 {len(text)} 字，不如直接看表格"
        return out

    rec = {"text": text, "at": payload.get("generated_at", ""),
           "content_hash": payload.get("content_hash", ""), "src": "ai",
           "facts_lines": facts.count("\n") + 1}
    for p, blob in ((mr_paths.SUMMARY_JSON, json.dumps(rec, ensure_ascii=False)),
                    (mr_paths.SUMMARY_JS, "window.SUMMARY = " + json.dumps(rec, ensure_ascii=False) + ";\n")):
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(blob, encoding="utf-8")
        os.replace(tmp, p)          # 原子替换：看板读到的永远是完整的一份
    out.update(written=True, state="ok", text=text)
    log(f"  总结已生成（{len(text)} 字，事实卡 {rec['facts_lines']} 行）")
    return out


def main() -> int:
    """独立入口：抓取结束时被 detached 调用（不阻塞主流程）。"""
    import fetch_model_data as F
    try:
        payload = json.loads(F.LATEST_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"读不到 latest.json，跳过总结：{exc}", file=sys.stderr)
        return 0
    res = run_summary(payload, log=lambda *a: print(*a, file=sys.stderr, flush=True))
    return 0 if res["state"] in ("ok", "none", "no-cli", "timeout", "cli-failed",
                                 "spawn-failed", "numbers-mismatch", "too-short") else 1


if __name__ == "__main__":
    sys.exit(main())
