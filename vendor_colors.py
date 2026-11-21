#!/usr/bin/env python3
"""厂商配色：全部由代码派生，不再手维护「厂商 → 颜色」对照表。

取色优先级，每一步都是可度量的判据，来了新厂商不用改代码：
  1. config 里的显式覆盖（逃生舱，默认空，只用于人工纠错）；
  2. 该厂商 logo 的 SVG 里解析出的 theme —— 复用 extract_logo_palette，
     不在这里再写一遍读 SVG 的逻辑；
  3. SVG 没给出可用品牌色（纯黑白、或压根没有 logo 文件，如 Kimi）时按厂商名生成色相：
     crc32 定起点 + 黄金角步进，并与所有已定色保持 ΔE ≥ MIN_DE_GEN。
     「没有信号」不等于「跟大家一起变灰」。
  4. 收尾做一次撞脸拆分：两家真品牌色近到分不出（实测 CIELAB ΔE < 16，
     例如阿里 #FF5A00 与小米 #FF6900 只差 7.0）时，在同色相族里沿**明度**分档，
     按「挪动家数最少 → 总挪动量最小」整体分配。色相一律不动，还是它家的颜色；
     实测这一轮只需要挪小米和 Meta，其余厂商全部保住 SVG 里的真品牌色。
     用族内整体分配而不是按字母序贪心，是因为贪心会一路往暗的方向堆、把好几家都推出本色。

改前的样子：config/watchlist.json 里一张手写 vendor_brand_color 表，表里没列的
厂商全部落到 #64748B，11 家里 5 家同色（Google/Kimi/Meta/SpaceXAI/StepFun），
其中 Meta、SpaceXAI 就排在指数前 20 —— 图上跟「其他」根本分不开。
另外表里 Alibaba 记的是 #615CED，而 extract_logo_palette 自己的注释说阿里真色是橙
（手写表会记错，正是它该被派生取代的理由）。
"""
from __future__ import annotations

import itertools
import zlib
from colorsys import hls_to_rgb, rgb_to_hls

SAT_MIN = 0.20          # 低于这个饱和度 = logo 没给出品牌色信号（黑/白/灰）
LUM_MIN, LUM_MAX = 0.08, 0.90
MIN_DE = 16.0           # 两家真色的 CIELAB 距离小于它 → 判定撞脸，需要拆开
MIN_DE_GEN = 30.0       # 生成色本来就没有品牌包袱，要求更宽一点
GOLDEN = 137.50776      # 黄金角：连续生成的色相天然散开，不会挤成一坨
GEN_S, GEN_L = 0.62, 0.52
GRAY = "#64748B"

# 拆分候选的明度档位：同族厂商各占一档就分得开。
# 不用"每次偏移 ±0.18"的贪心写法 —— 那样会一路往暗的方向堆、把好几家都推出自己的色。
# 改成在档位里做整体最优分配：每家都优先留在自己的真实明度上（成本 0），
# 只有实在撞脸的那一家才被迫挪档。
LEVELS = (0.24, 0.39, 0.54, 0.69, 0.84)
FAMILY_HUE = 30.0        # 色相差小于它就归同族，同族里才需要检查撞脸并分档


def _rgb(hexc: str) -> tuple[int, int, int]:
    h = hexc.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _hex(r: int, g: int, b: int) -> str:
    return "#%02X%02X%02X" % (max(0, min(r, 255)), max(0, min(g, 255)), max(0, min(b, 255)))


def hsl(hexc: str) -> tuple[float, float, float]:
    """→ (hue 0..360, saturation 0..1, lightness 0..1)。"""
    r, g, b = (v / 255 for v in _rgb(hexc))
    hh, ll, ss = rgb_to_hls(r, g, b)
    return round(hh * 360, 1), round(ss, 3), round(ll, 3)


def to_hex(h: float, s: float, l: float) -> str:
    r, g, b = hls_to_rgb(h / 360, max(0.0, min(l, 1.0)), max(0.0, min(s, 1.0)))
    return _hex(round(r * 255), round(g * 255), round(b * 255))


def hue_dist(a: str, b: str) -> float:
    d = abs(hsl(a)[0] - hsl(b)[0]) % 360
    return min(d, 360 - d)


def lab(hexc: str) -> tuple[float, float, float]:
    """sRGB → CIELAB(D65)。用来判断「两个色在图上分不分得开」。"""
    def g(u: float) -> float:
        return u / 12.92 if u <= 0.04045 else ((u + 0.055) / 1.055) ** 2.4
    r, gg, b = (g(v / 255) for v in _rgb(hexc))
    x = (0.4124 * r + 0.3576 * gg + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * gg + 0.0722 * b
    z = (0.0193 * r + 0.1192 * gg + 0.9505 * b) / 1.08883
    f = lambda v: v ** (1 / 3) if v > 0.008856 else 7.787 * v + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a: str, b: str) -> float:
    l1, a1, b1 = lab(a)
    l2, a2, b2 = lab(b)
    return round(((l1 - l2) ** 2 + (a1 - a2) ** 2 + (b1 - b2) ** 2) ** 0.5, 1)


def usable(hexc: str) -> bool:
    """能不能直接当厂商标识色用：够艳、且不会在深/浅底上糊成一团。"""
    try:
        _, s, l = hsl(hexc)
    except (ValueError, AttributeError):
        return False
    return s >= SAT_MIN and LUM_MIN <= l <= LUM_MAX


def gen_color(slug: str, taken: list[str]) -> str:
    """按名字生成一个和已有色都拉开 MIN_DE_GEN 的色相（确定性）。

    必须确定性：用 random 或内置 hash() 的话每次抓取颜色都在跳
    （str 的 hash 每个进程都带随机盐），用户会以为数据坏了。

    原先只试 72 步、没解就**静默退回起点** —— 厂商一多就会出现两把几乎同色而调用方
    毫无察觉（在 TokenRadar 上实测撞出 ΔE=2.3 的一对）。现在换长周期 + 每三步换一档
    明度（固定 S/L 的色环只有约 12 个 ≥30 的槽位），没解时返回"最不撞"的那个，
    由 derive() 收尾统一标 collision，让调用方能报警。
    """
    start = zlib.crc32(slug.encode("utf-8")) % 360
    best: tuple[float, str] | None = None
    for k in range(216):
        h = (start + k * GOLDEN) % 360
        l = GEN_L + (0.0, 0.10, -0.10)[k % 3]
        cand = to_hex(h, GEN_S, min(max(l, 0.36), 0.68))
        worst = min([delta_e(cand, t) for t in taken], default=999.0)
        if worst >= MIN_DE_GEN:
            return cand
        if best is None or worst > best[0]:
            best = (worst, cand)
    return best[1] if best else to_hex(start, GEN_S, GEN_L)


def _theme_of(palette: dict, slug: str) -> tuple[str | None, str]:
    """从 logo 配色表取 theme，并说明它是从 SVG 解析出来的还是兜底表给的。"""
    ent = palette.get(slug) or {}
    theme = ent.get("theme")
    if not theme:
        return None, "none"
    seen = {str(c).upper() for c in ent.get("colors") or []}
    return theme, ("svg" if theme.upper() in seen else "curated")


def _clash(a: str, b: str) -> bool:
    """两个色在图上分不分得出 —— 用 CIELAB 距离，不用「色相差+明度差+饱和度差」三条阈值。

    换掉的原因是那套阈值会误判：Google #4285F4 与 DeepSeek #4D6BFE 色相只差 12°、
    明度只差 0.04，按阈值算「撞脸」，可实测 ΔE=26，肉眼分得很清；而阿里 #FF5A00 与
    小米 #FF6900 色相差 3.5°、ΔE 只有 7.0，那才是真撞脸。误判会白挪一家厂商的颜色。
    """
    return delta_e(a, b) < MIN_DE


def _same_family(a: str, b: str) -> bool:
    return hue_dist(a, b) < FAMILY_HUE


def _groups(slugs: list[str], color_of) -> list[list[str]]:
    """按「同色相族」做单链接聚类，只有同族才需要检查撞脸、分档位。

    用传递闭包而不是两两比对：A 撞 B、B 撞 C 时，把 B 挪走之后 A 和 C 可能仍然撞，
    必须一起安排。
    """
    seen: set[str] = set()
    out: list[list[str]] = []
    for a in slugs:
        if a in seen:
            continue
        stack, grp = [a], []
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            grp.append(x)
            stack.extend(y for y in slugs
                         if y not in seen and _same_family(color_of(x), color_of(y)))
        out.append(sorted(grp))
    return out


def _plan_group(members: list[str], own: dict[str, str],
                outside: list[str]) -> dict[str, str] | None:
    """同族内给每家分配明度，使总挪动量最小；分不开返回 None。

    候选档位含「自己的真实明度」，所以不撞脸的成员成本为 0、自然留在原位，
    只有被挤掉的那几家才会挪档 —— 这比"排在后面的都得改"更贴近品牌色。
    """
    hs = {s: hsl(own[s])[:2] for s in members}
    pools = []
    for s in members:
        l_own = hsl(own[s])[2]
        pools.append(sorted({round(l_own, 3)} | set(LEVELS)))
    best: tuple[tuple, dict[str, str]] | None = None
    for combo in itertools.product(*pools):
        cols = {s: to_hex(hs[s][0], hs[s][1], L) for s, L in zip(members, combo)}
        vals = list(cols.values())
        if any(_clash(c, o) for c in vals for o in outside):
            continue
        if any(_clash(vals[i], vals[j]) for i in range(len(vals)) for j in range(i + 1, len(vals))):
            continue
        deltas = sorted(round(abs(L - hsl(own[s])[2]), 3) for s, L in zip(members, combo))
        # 先少动人家：能只挪一家解决就别三家都挪 —— 保住的原色越多越像「厂牌色」。
        # 再比总挪动量、比逐项分布（避免按 slug 顺序偏置）；
        # 完全同分时优先让被挪的那家往**亮**走：这两张图默认都贴在深色底/深色 popover 上，
        # 往暗处挪会直接糊进背景里（阿里橙与小米橙只差 ΔE 7，谁挪都得挑一个方向）。
        key = (sum(1 for d in deltas if d > 1e-9), sum(deltas), deltas,
               -sum(L for s, L in zip(members, combo) if abs(L - hsl(own[s])[2]) > 1e-9))
        if best is None or key < best[0]:
            best = (key, cols)
    return best[1] if best else None


def derive(vendors: list[str], palette: dict,
           overrides: dict[str, str] | None = None) -> dict[str, dict]:
    """给每家厂商派生一个可区分的颜色。

    vendors: 图上要出现的 creator_slug 集合。
    返回 {creator_slug: {"color": "#RRGGBB", "source": "override|svg|curated|generated[/+split]"}}。

    三轮，顺序是刻意的：
      1. 先落真品牌色（覆盖表 / logo SVG）；
      2. 真色之间撞脸的沿明度拆开；
      3. 完全没有信号的（黑白 logo、没有 logo 文件）最后生成色相，
         这样它要避让的是已经定案的全部颜色 —— 早前一版边生成边定色，
         按 slug 排序时 kimi 排在 openai 前面，结果两家都落在 166° 的青绿上。
    """
    overrides = overrides or {}
    # 按 slug 排序处理：生成色只取决于已经定下的颜色，厂商集合变化时
    # 影响范围可控（不像按权重排那样每天重新分配）。
    order = sorted(set(vendors))
    out: dict[str, dict] = {}

    for slug in order:                       # 第 1 轮：真色
        if slug in overrides:
            out[slug] = {"color": overrides[slug].upper(), "source": "override"}
            continue
        theme, src = _theme_of(palette, slug)
        if theme and usable(theme):
            out[slug] = {"color": theme.upper(), "source": src}

    # 第 2 轮：同色相族内做一次整体明度分配（只有实在撞脸的才挪档）。
    for grp in (g for g in _groups(sorted(out), lambda s: out[s]["color"]) if len(g) > 1):
        own = {s: out[s]["color"] for s in grp}
        srcs = {s: out[s]["source"] for s in grp}
        outside = [v["color"] for k, v in out.items() if k not in own]
        plan = _plan_group(grp, own, outside)
        if plan is None:
            for s in grp:                    # 分不开就如实标出来，不硬改成谁都不像的色
                out[s]["source"] += "+collision"
        else:
            for s, col in plan.items():
                if col != own[s]:
                    out[s] = {"color": col, "source": srcs[s] + "+split"}

    for slug in order:                       # 第 3 轮：没有信号的按名生成
        if slug not in out:
            out[slug] = {"color": gen_color(slug, [v["color"] for v in out.values()]),
                         "source": "generated"}

    # 收尾统一核一遍：还有分不开的就在 source 上标 collision，让调用方能报警，
    # 而不是安静地给出两把几乎同色（生成色没解时只会退到"最不撞"的那个）。
    slugs = sorted(out)
    for i, a in enumerate(slugs):
        for b in slugs[i + 1:]:
            if _clash(out[a]["color"], out[b]["color"]):
                for s in (a, b):
                    if "collision" not in out[s]["source"]:
                        out[s]["source"] += "+collision"
    return out
