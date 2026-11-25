#!/usr/bin/env python3
"""新旧解析器逐字段等价对比（lxml 版 vs 标准库 html_tables 版）。

2026-11-15 起 AA 榜单改成"每个 release 一行 + 档位在 initialModels 里"，
本工具只覆盖旧的 8 列布局，不能用来核对新布局。


为什么需要它：把 AA 与 DataLearner 的解析从 lxml 换成标准库，是为了让别人的机器
零 pip 依赖就能装。但这是**真实代码改动**，一旦文本拼接细节差一个空格，
分词、watchlist 命中、厂商匹配全会跟着偏，而且不会有人报错 —— 只会静默少模型。
所以先拿同一份真页面让两解跑一遍，字段级完全一致才允许换。

lxml 在这里只是**参考实现**：运行时不依赖它，缺了就提示装一下再跑本工具。

用法：.venv/bin/python tools/parse_parity.py          # 抓线上真页面
      .venv/bin/python tools/parse_parity.py f.html   # 用本地存档比对（离线）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fetch_model_data as F      # noqa: E402
import html_tables as HT          # noqa: E402

try:
    from lxml import html as LH    # type: ignore
except ImportError:
    sys.exit("参考侧需要 lxml：.venv/bin/pip install lxml（只有本工具用它，运行时不用）")

KEYS = ("name", "creator", "ctx", "index", "apt_usd", "tok_s", "ttft_s", "total_s", "logo")


def aa_lxml(html: str) -> list[dict]:
    """换掉之前的实现，逐行照抄，只作参考基准。"""
    doc = LH.fromstring(html)
    table = doc.xpath("//table")
    if not table:
        raise RuntimeError("AA 页面里没找到 <table>")
    rows = []

    def txt(el) -> str:
        return " ".join(el.itertext()).strip()

    for tr in table[0].xpath(".//tbody/tr"):
        tds = tr.xpath(".//td")
        if len(tds) < 8:
            continue
        cells = [txt(td) for td in tds]
        name, ctx, creator = cells[0], cells[1], cells[2]
        index, apt, tps, ttft, total = cells[3], cells[4], cells[5], cells[6], cells[7]
        logo = ""
        for td in tds:
            srcs = td.xpath(".//img/@src")
            if srcs and "logos/" in (srcs[0] or ""):
                logo = srcs[0]
                break
        idx_v, apt_v, tps_v = F.num(index), F.num(apt), F.num(tps)
        if idx_v is None:
            continue
        rows.append({"name": name, "creator": creator, "ctx": ctx, "index": idx_v,
                     "apt_usd": apt_v, "tok_s": tps_v, "ttft_s": F.num(ttft),
                     "total_s": F.num(total), "logo": logo})
    return rows


def dl_lxml(html: str) -> list[dict]:
    doc = LH.fromstring(html)
    tables = doc.xpath("//table")
    if not tables:
        return []
    out = []
    for tr in tables[0].xpath(".//tr"):
        cells = [" ".join(td.itertext()).strip() for td in tr.xpath(".//td")]
        if len(cells) < 4:
            continue
        rank, name, index, creator = cells[0], cells[1], cells[2], cells[3]
        idx_v = F.num(index)
        if idx_v is None:
            continue
        out.append({"rank": rank, "name": name, "index": idx_v, "creator": creator})
    return out


def compare(label: str, a: list[dict], b: list[dict], keys: tuple[str, ...]) -> int:
    bad = 0
    if len(a) != len(b):
        print(f"  ✗ {label}: 行数不等 lxml={len(a)} stdlib={len(b)}")
        return 1
    for i, (x, y) in enumerate(zip(a, b)):
        for k in keys:
            if x.get(k) != y.get(k):
                bad += 1
                if bad <= 8:
                    print(f"  ✗ {label} 第{i}行 字段 {k}: lxml={x.get(k)!r} stdlib={y.get(k)!r}")
    print(f"  {'✓' if not bad else '✗'} {label}: {len(a)} 行 × {len(keys)} 字段"
          + ("，完全一致" if not bad else f"，{bad} 处不一致"))
    return bad


def main() -> int:
    if len(sys.argv) > 1:
        aa_html = Path(sys.argv[1]).read_text(encoding="utf-8", errors="ignore")
        dl_html = ""
        print(f"用本地存档：{sys.argv[1]}")
    else:
        aa_html = F.fetch(F.SOURCES["aa"]).decode("utf-8", "ignore")
        dl_html = F.fetch(F.SOURCES["datalearner"]).decode("utf-8", "ignore")

    fails = compare("AA 主表", aa_lxml(aa_html), F.parse_aa(aa_html), KEYS)
    if dl_html:
        fails += compare("DataLearner", dl_lxml(dl_html), F.parse_datalearner(dl_html),
                         ("rank", "name", "index", "creator"))
    print("解析器等价：通过" if not fails else "解析器等价：不通过 —— 不要发布")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
