#!/usr/bin/env python3
"""运行时路径单点解析。

代码真身（git 仓库）与可变状态（SQLite 历史 / latest.json / 生成物）分离：
仓库可被移动、重命名、重新 clone，历史台账不该跟着一起没。

    状态目录 = $MODELRADAR_DATA_DIR
             ?? ~/Library/Application Support/ModelRadar/data

仓库内 `data` 必须是**真目录**，不能是指向状态目录的软链：~/Library 是 0700，
沙箱化的浏览器（Safari）不会跟这条链进去，于是 dashboard.html 能打开、
而 <script src="data/latest.js"> 读不到，页面只能显示"没读到数据"。
所以浏览器要用的那几个产物在仓库里落一份真件（见 publish_for_browser），
权威状态（SQLite 历史 / latest.json / chart.html）仍只在状态目录一份。
"""
from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent

SUPPORT = Path(os.environ.get("MODELRADAR_SUPPORT")
               or Path.home() / "Library" / "Application Support" / "ModelRadar")

DATA = Path(os.environ.get("MODELRADAR_DATA_DIR") or (SUPPORT / "data"))

DB_PATH = DATA / "ModelRadar.db"
LATEST_JSON = DATA / "latest.json"
LATEST_JS = DATA / "latest.js"
CHART_HTML = DATA / "chart.html"
LOGOS_DIR = DATA / "logos"
RAW_DIR = DATA / "logos_raw"
LOGO_PALETTE = DATA / "logo_palette.json"

# 变动总结（可选增强）。没有本机总结命令时这两个文件压根不存在 ——
# 看板把"读不到 summary.js"当成正常状态照常显示变动表，而不是报错。
SUMMARY_JSON = DATA / "summary.json"
SUMMARY_JS = DATA / "summary.js"

# 状态栏组件读的壳脚本与去重戳都由 build 期生成在这里
BIN_DIR = SUPPORT / "bin"
RUNTIME_ENV = SUPPORT / "runtime.env"
NOTIFIED_STAMP = SUPPORT / ".last_notified"
# 上一次"判定为会出声"的那条通知正文（JSON，只有爬虫一侧写）。
# 与去重戳分开是因为戳文件只存键、且被 app/Python 两侧共写 —— 动它的格式就是拿
# "重复弹/漏弹"冒险，而这个文件只服务脚注浮层，写坏了最多是少一段话。
LAST_ALERT = SUPPORT / ".last_alert.json"

# 人工筛查过的不可信观测：遇到这类数据不删原始记录，只在比较与派生时剥离。
# 与词表同理分两段——仓库种子给"判据与字段说明"，真身给人筛过的那几条；
# 真身在状态目录，不跟着 git 走（两台机器各自筛各自的源问题）。
QUARANTINE = Path(os.environ.get("MODELRADAR_QUARANTINE") or (SUPPORT / "quarantine.json"))
QUARANTINE_SEED = HERE / "config" / "quarantine.example.json"

WIDGET_PID = SUPPORT / ".widget.pid"
BADGE_STATE = SUPPORT / ".badge_state.json"

LOG_DIR = Path.home() / "Library" / "Logs" / "ModelRadar"

# 故障日志：每次剥离都追加一行，供人工回看"哪条数据被判定不可信、依据是什么"。
# 只追加不改写——它是审计档，不是可回滚的状态。
FAULT_LOG = Path(os.environ.get("MODELRADAR_FAULT_LOG") or (LOG_DIR / "faults.jsonl"))

# 词表：**可变真身在状态目录，不在代码树里**。爬虫每轮都会改写它（`grow_watchlist`
# 只加不减），而安装副本的 `src/config/watchlist.json` 与仓库那份是同一个 inode ——
# 放在代码树里就等于让定时任务每天往 git 工作树里写一次（实测 10-17 加 claude、
# 10-20 加 minimax 各脏一次，而且看起来像"有人改了配置没提交"）。
CONFIG = Path(os.environ.get("MODELRADAR_CONFIG") or (SUPPORT / "config" / "watchlist.json"))
# 仓库里那份降级成**种子**：装到别人机器上（或本机第一次跑）时拿它当起点，
# 之后只读不写。人工要长期钉住的词（pinned）与配色 override 仍然写在这里，
# 由 `load_config` 每次并进来，所以改了它就下一轮生效，不必去动状态目录。
CONFIG_SEED = HERE / "config" / "watchlist.json"


def runtime_env() -> dict:
    """解析 runtime.env（`KEY="value"` 逐行）。

    为什么 Python 也要读这个文件：launchd 的爬虫任务是**直接以 python3 起**的（plist 的
    ProgramArguments 里没有 crawl.sh），拿不到 shell `.` 出来的变量，而组件读的就是这份
    文件。两边同源，才不会出现在"组件知道、爬虫不知道"的分裂。文件不存在返回空 dict。
    """
    out = {}
    try:
        lines = RUNTIME_ENV.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# 中枢通知出口（可选集成）。**路径不写死在代码里**：这份仓库要公开，而中枢所在目录是
# 开发者个人机器上的目录布局，写死就等于在公开树里留一个指向它的箭头。留空时通知直接走系统横幅 ——
# 功能不坏，只是少了审计落档与"点击打开看板"。
HUB_NOTIFY = os.environ.get("MR_HUB_NOTIFY") or runtime_env().get("MR_HUB_NOTIFY") or ""


def ensure_dirs() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


# 看板（file:// 页面）在仓库目录树内取文件，见模块头注释
# 仓库侧镜像可单独改落点：临时 HOME / 测试跑一次抓取时，默认会把**仓库里的**
# data/latest.js 覆盖成测试数据（实测就把带 logo 的正式镜像覆盖成了 --no-logos 的版本，
# 看板和 chart.html 从此对不上，判据当场报 4 条不一致）。测试请指到临时目录。
REPO_DATA = Path(os.environ.get("MODELRADAR_REPO_DATA") or HERE / "data")
REPO_LOGOS = REPO_DATA / "logos"


def _mirror(src: Path, dst: Path) -> bool:
    """src 与 dst 内容不同才写。返回是否真的写了。"""
    try:
        data = src.read_bytes()
    except OSError:
        return False
    try:
        if dst.exists() and dst.read_bytes() == data:
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        return True
    except OSError:
        return False


def publish_for_browser() -> list[str]:
    """把浏览器要用的产物镜像进仓库 data/，返回本次真正写入的文件名。

    只镜像 latest.js、summary.js（有才有）与 logos/：latest.json、chart.html、SQLite 都归
    组件和爬虫用，没必要出现在仓库里。同内容直接跳过，所以每次抓取调用它的代价只是几次读比对。
    """
    wrote: list[str] = []
    if _mirror(LATEST_JS, REPO_DATA / LATEST_JS.name):
        wrote.append(LATEST_JS.name)
    # summary.js 是可选的：没有就什么都不镜像（看板照常显示表格），
    # 但**存在过又被丢弃**时要把旧镜像删掉，否则一段已被判失败的总结会继续显示。
    if SUMMARY_JS.exists():
        if _mirror(SUMMARY_JS, REPO_DATA / SUMMARY_JS.name):
            wrote.append(SUMMARY_JS.name)
    elif (REPO_DATA / SUMMARY_JS.name).exists():
        try:
            (REPO_DATA / SUMMARY_JS.name).unlink()
            wrote.append("（清掉过期 " + SUMMARY_JS.name + "）")
        except OSError:
            pass
    if LOGOS_DIR.is_dir():
        for f in sorted(LOGOS_DIR.glob("*.svg")):
            if _mirror(f, REPO_LOGOS / f.name):
                wrote.append(f"logos/{f.name}")
    return wrote
