#!/usr/bin/env python3
"""从 logo 的 SVG 源码解析真实配色，输出 data/logo_palette.json。

为什么从源码解析而不是对位图做颜色聚类：
AA 榜单给的这些 SVG 是矢量单文件，fill 属性里直接写着品牌色（#FF5A00 / #4D6BFE / #0052D9），
解析源码拿到的是精确值；转成 PNG 再聚类反而会被抗锯齿边缘和透明通道污染。

产出字段（每个 logo）：
  bg        SVG 自带的背景色，透明底记 transparent。前端优先用它当圆底，
            这样「白底黑字」这类自带对比的图标不会反色丢细节。
  soft_bg   bg 的柔化版：纯黑/纯白向中性灰靠一点，避免在看板深底上刺眼。
            前端实际用这个。
  theme     主题色 —— 出现最多且够饱和的颜色，做外圈描边/高亮。
  ink       图形实际笔画色。SVG 一个颜色都不写（zai）时记 #000000。
  invert_icon  前端是否要给 icon 加 filter 反色成白。
              ★ 判据刻意做成确定性的「透明底 且 ink 不是白」，
                不用亮度阈值——绝对亮度阈值会让 #FF5A00 和 #FF6900 这种
                几乎同色的橙给出相反结论，那是会翻车的脆弱判据。
  contrast_theme   theme 与 soft_bg 的对比度，只作参考不参与决策。
  fg_on_bg  圆底为该图标配色时，中心文字该用什么颜色（#fff / #111）。
            ★ 由这里算好交给前端，前端不要再自己写一遍亮度阈值——
              同一套判据散落在两处，改一处忘另一处就是静默翻车。
  colors    所有出现过的颜色，按出现次数降序
  brand     厂商品牌色兜底（SVG 里没写颜色时）
"""
from __future__ import annotations

import json
import re
import sys
import zlib
from collections import Counter
from datetime import datetime
from pathlib import Path

import mr_paths

HERE = Path(__file__).resolve().parent
DATA = mr_paths.DATA
LOGOS = DATA / "logos"
OUT = DATA / "logo_palette.json"

# 厂商品牌色兜底表。SVG 里没写颜色（zai）、或全是黑白（openai/anthropic）
# 时会退到这里取色。
# 注意 alibaba 之前误记成紫 #615CED，阿里实际品牌色是橙；这张表只影响
# 探测失败时的兜底观感，错了不容易发现，所以列在这里留档。
BRAND_FALLBACK = {
    "alibaba": "#FF5A00", "anthropic": "#D97757", "deepseek": "#4D6BFE",
    "minimax": "#F59E0B", "openai": "#10A37F", "tencent": "#0052D9",
    "xiaomi": "#FF6900", "zai": "#37474F", "z-ai": "#37474F",
    "moonshotai": "#16A34A", "kimi": "#16A34A", "qwen": "#615CED",
    "xai": "#111827", "google": "#1A73E8", "meta": "#0866FF",
    "mistral": "#FF7000", "bytedance": "#0077FF", "deepseek-ai": "#4D6BFE",
    "other": "#64748B",
}

# 对比度下限：低于这个数，icon 贴上去就是糊的
MIN_CONTRAST = 3.0
# 圆底亮度上限：亮到这个程度以上就别当圆底了，白 icon 贴上去必瞎
MAX_BG_LUMA = 0.85

HEX = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b")
RGB_FN = re.compile(r"rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)", re.I)
STOP_COLOR = re.compile(r'stop-color\s*[:=]\s*["\']?(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\))', re.I)
STYLE_ATTR = re.compile(r"style\s*=\s*[\"']([^\"']*)[\"']", re.I)
TAG_RE = re.compile(r"<[a-zA-Z][^>]*>")
GRAD_TAG = re.compile(r"<(?:linear|radial)Gradient\b[^>]*?>", re.I)
STOP_TAG = re.compile(r"<stop\b[^>]*>", re.I)
FILL_ATTR = re.compile(r'\bfill\s*=\s*["\']([^"\']*)["\']', re.I)
STROKE_ATTR = re.compile(r'\bstroke\s*=\s*["\']([^"\']*)["\']', re.I)
ID_ATTR = re.compile(r'\bid\s*=\s*["\']([^"\']+)["\']', re.I)
STYLE_DECL = re.compile(r"(?:fill|stroke)\s*:\s*([^;'\"]+)", re.I)
URL_REF = re.compile(r"url\(#([^)]+)\)")

# SVG 里表示「不用这个颜色」的写法
NEUTRAL = {"none", "transparent", "currentcolor", "inherit", "initial", "unset"}
NAMED = {"white": "#FFFFFF", "black": "#000000", "red": "#FF0000",
         "silver": "#C0C0C0", "gray": "#808080", "grey": "#808080"}


def norm_hex(h: str) -> str | None:
    h = h[1:]
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) == 8:  # #RRGGBBAA，丢掉 alpha
        h = h[:6]
    if len(h) != 6:
        return None
    return "#%06X" % int(h, 16)


def parse_color(raw: str, grads: dict[str, list[str]]) -> str | None:
    raw = (raw or "").strip().lower()
    if not raw or raw in NEUTRAL:
        return None
    if raw.startswith("#"):
        return norm_hex(raw)
    m = RGB_FN.search(raw)
    if m:
        r, g, b = (int(float(x)) for x in m.groups())
        return "#%02X%02X%02X" % (r, g, b)
    if raw.startswith("url("):
        m2 = URL_REF.search(raw)
        if m2 and m2.group(1) in grads:
            # 渐变取第一个 stop 当主色，前端也渲染不了真正的渐变
            return grads[m2.group(1)][0]
        return None
    return NAMED.get(raw)


def collect_grads(svg: str) -> dict[str, list[str]]:
    """gradient id -> stop 颜色列表

    stop 只在**本渐变标签到下一个渐变标签之间**收集。原先对每个渐变都全篇扫
    `<stop>`：双渐变 logo 里 gA/gB 拿到同一份全文档 stop 列表，url(#gB) 会解析成
    gB 的"第一个 stop"其实是 gA 的 —— 渐变做主色的厂商 logo 因此会串色。
    """
    grads: dict[str, list[str]] = {}
    tags = list(GRAD_TAG.finditer(svg))
    for i, m in enumerate(tags):
        gid = ID_ATTR.search(m.group(0))
        if not gid:
            continue
        seg_end = tags[i + 1].start() if i + 1 < len(tags) else len(svg)
        colors = []
        for st in STOP_TAG.findall(svg, m.start(), seg_end):
            sc = STOP_COLOR.search(st)
            if sc:
                c = parse_color(sc.group(1), grads)
                if c:
                    colors.append(c)
        if colors:
            grads[gid.group(1)] = colors
    return grads


def color_table(svg: str, attr_pattern: re.Pattern, grads: dict[str, list[str]]) -> Counter:
    """统计某属性（fill/stroke）的颜色出现次数。style 里写的也算。"""
    c: Counter = Counter()
    for tag in TAG_RE.findall(svg):
        for m in attr_pattern.finditer(tag):
            col = parse_color(m.group(1), grads)
            if col:
                c[col] += 1
    for blob in STYLE_ATTR.findall(svg):
        for m in STYLE_DECL.finditer(blob):
            col = parse_color(m.group(1), grads)
            if col:
                c[col] += 1
    return c


def _rgb(hexc: str) -> tuple[int, int, int]:
    """只接受 #RRGGBB。传 'transparent' 之类的名字进来会让它去解析 'ra'，
    抛出的 'invalid literal for int()' 极具误导性，所以在这里拦掉。"""
    if not isinstance(hexc, str) or len(hexc) != 7 or not hexc.startswith("#"):
        raise ValueError(f"需要 #RRGGBB 形式的颜色，收到 {hexc!r}")
    return (int(hexc[1:3], 16), int(hexc[3:5], 16), int(hexc[5:7], 16))


def luminance(hexc: str) -> float:
    r, g, b = (c / 255 for c in _rgb(hexc))
    f = lambda u: u / 12.92 if u <= 0.03928 else ((u + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def saturation(hexc: str) -> float:
    r, g, b = _rgb(hexc)
    mx, mn = max(r, g, b), min(r, g, b)
    return 0.0 if mx == 0 else (mx - mn) / mx


def contrast(a: str, b: str) -> float:
    l1, l2 = luminance(a), luminance(b)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def hsl_to_hex(h: float, s: float, l: float) -> str:
    """HSL -> #RRGGBB。s/l 固定取值，保证随机出来的也是「艳且够深」的圆盘底色。"""
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = l - c / 2
    r, g, b = ((c, x, 0), (x, c, 0), (0, c, x),
               (0, x, c), (x, 0, c), (c, 0, x))[int(h // 60) % 6]
    return "#%02X%02X%02X" % tuple(round((v + m) * 255) for v in (r, g, b))


def darken(hexc: str, factor: float) -> str:
    """把颜色整体调暗。品牌色对比度不够时宁可压暗，也不要换成无关的随机色——
    压暗后仍是厂商的那个色相，随机色看着像另一个品牌。"""
    r, g, b = (int(v * factor) for v in _rgb(hexc))
    return "#%02X%02X%02X" % (min(r, 255), min(g, 255), min(b, 255))


def deterministic_color(key: str) -> str:
    """确定性伪随机色：同一个 key 永远得到同一个颜色。

    必须是确定性的——用 random 的话每次刷新页面厂商颜色都在跳，
    用户会以为数据坏了。固定 s/l 是为了避开「随机出一片灰白」的坑，
    灰色当圆底既难看又最容易和 icon 糊在一起。
    """
    h = zlib.crc32(key.encode("utf-8"))
    return hsl_to_hex(h % 360, s=0.55, l=0.42)


def probe_one(name: str, svg: str, brand: str) -> dict:
    grads = collect_grads(svg)
    fills = color_table(svg, FILL_ATTR, grads)
    strokes = color_table(svg, STROKE_ATTR, grads)

    allc: Counter = Counter()
    for tbl in (fills, strokes):
        allc.update(tbl)

    # —— 背景：出现次数最多，且明显是纯黑/纯白那种「板色」 ——
    bg = "transparent"
    if allc:
        top, topn = allc.most_common(1)[0]
        lum = luminance(top)
        if (lum > 0.9 or lum < 0.05) and topn >= max(1, (sum(allc.values()) - topn)):
            bg = top

    # —— ink：背景之外的主色 ——
    ink_candidates = [c for c, _ in allc.most_common() if c != bg]
    # SVG 一个颜色都不写的时候（zai 就是），笔画是默认黑
    ink = ink_candidates[0] if ink_candidates else "#000000"

    # —— theme：出现最多的饱和色；没有就退回 ink，再没有就靠 brand ——
    theme = next((c for c, _ in allc.most_common() if saturation(c) > 0.25), None)
    if theme is None and saturation(ink) > 0.1:
        theme = ink
    if theme is None:
        theme = brand

    is_white = luminance(ink) > 0.75
    # 反色判据：只有「单色剪影」才需要反色贴在彩色圆底上。
    # 多色 SVG 说明图形自带背景（xiaomi 是橙圆白字、anthropic 是深底浅字），
    # 原样显示就行；这类 logo 一旦反色，它自带的底色会被一起拍白，
    # 上面的图形跟着变白 —— 白图形叠白底，直接隐形。
    # 唯一的例外是 openai：白铺满整块画布，那块白必须反掉。
    invert_icon = len(allc) == 0 or (len(allc) <= 1) or (bg == "#FFFFFF")

    soft = {"#FFFFFF": "#E9ECF1", "#000000": "#222831"}
    soft_bg = soft.get(bg, bg) if bg != "transparent" else "transparent"

    # 图标自带纯白底（如 OpenAI 白底黑字）：白圆在看板上既扎眼，又不像「厂商色」。
    # 换成探测到的非白主题色当圆底，并把 icon 反色让它在彩色底上可读。
    if bg == "#FFFFFF" and luminance(theme) < 0.9:
        bg = theme
        soft_bg = theme

    # 图标本身就是纯白图形时（白 icon 在白底上等于隐形），
    # theme 必须换成非白的，否则圆底还是白的。
    if luminance(theme) > 0.9:
        theme = brand if luminance(brand) <= 0.9 else deterministic_color(name)

    # ---- 收尾：定出 ring_bg（真正铺在圆框里的底色） ----
    # 反色的 icon 最终是纯白剪影，不反色的保持 ink 原样。
    icon_final = "#FFFFFF" if invert_icon else ink
    # icon 自带背景时（多色 SVG）它跟谁都搭，圆底不必受对比度约束；
    # 被反成白剪影的才必须「够深且跟白比得清」。
    needs_readable = invert_icon
    bg_source = "probe"

    def acceptable(cand: str) -> bool:
        if luminance(cand) > MAX_BG_LUMA:
            return False
        return (contrast(cand, icon_final) >= MIN_CONTRAST) if needs_readable else True

    ring_bg = soft_bg if bg != "transparent" else theme
    if not acceptable(ring_bg):
        # 兜底按可信度排序：硬编码品牌色 > 把品牌色调暗到合格 > 随机色。
        # 调暗这一层是为了留住色相——直接换成随机色等于认输了。
        if acceptable(brand):
            ring_bg = brand
            bg_source = "brand"
        elif needs_readable:
            for f in (0.8, 0.65, 0.5, 0.4):
                cand = darken(brand, f)
                if acceptable(cand):
                    ring_bg = cand
                    bg_source = "brand_dark"
                    break
        if not acceptable(ring_bg):
            ring_bg = deterministic_color(name)
            bg_source = "random"

    mate = ring_bg

    # 圆底上 center 文字的颜色：底浅用深字，底深用白字。
    # 透明底的情况留到前端：那种圆底是 theme，icon 又会被反色成白，固定白字。
    if bg == "transparent":
        fg_on_bg = "#FFFFFF"
    else:
        fg_on_bg = "#111111" if luminance(soft_bg) > 0.5 else "#FFFFFF"

    return {
        "bg": bg,
        "soft_bg": soft_bg,
        "theme": theme,          # 描边/高亮用，保持品牌感，不参与可读性计算
        "ring_bg": ring_bg,      # 真正铺在圆框里的底色，已过可读性约束
        "bg_source": bg_source,  # probe / brand / random —— 便于自查为什么是这个色
        "ink": ink,
        "invert_icon": invert_icon,
        "fg_on_bg": fg_on_bg,
        "contrast_theme": round(contrast(theme, mate), 2),
        "contrast_icon": round(contrast(icon_final, ring_bg), 2),
        "colors": [c for c, _ in allc.most_common()],
        "counts": dict(allc),
        "brand": brand,
    }


def probe_all() -> dict[str, dict]:
    """给 data/logos 下所有 SVG 出一个配色表。单个坏文件不拖垮整体。"""
    out: dict[str, dict] = {}
    for f in sorted(LOGOS.glob("*.svg")):
        brand = BRAND_FALLBACK.get(f.stem, BRAND_FALLBACK["other"])
        try:
            out[f.stem] = probe_one(f.stem, f.read_text(encoding="utf-8", errors="ignore"), brand)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {f.name} 解析失败：{e}", file=sys.stderr)
            out[f.stem] = {"bg": "transparent", "soft_bg": "transparent", "theme": brand,
                           "ink": "#000000", "invert_icon": True, "contrast_theme": 1.0,
                           "colors": [], "counts": {}, "brand": brand}
    return out


def main() -> int:
    out = probe_all()
    if not out:
        print(f"没找到 logo：{LOGOS}", file=sys.stderr)
        return 1

    payload = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "logos": out}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"已写出 {OUT.name}（{len(out)} 个 logo）")
    print(f"  {'logo':<11} {'ring_bg':<9} {'来源':<10} {'icon对比':>7}  {'invert':<6} theme     colors")
    for k, v in out.items():
        print(f"  {k:<11} {v['ring_bg']:<9} {v['bg_source']:<10} {v['contrast_icon']:>7}  "
              f"{str(v['invert_icon']):<6} {v['theme']:<9} {v['colors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
