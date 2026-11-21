"""把 logo 的 viewBox 裁到内容实际边界，让 icon 真正铺满圆形容器。

为什么需要：AA 下发的 SVG 普遍带内边距（alibaba 内容只占 y 8.5~39.5，
上下各空 8.5），图标塞进正方形圆框后靠 object-fit:contain 缩放，
那些空白会原样保留，圆里就一圈死边。裁掉即可。

三个必须注意的点：

1. **必须裁成正方形**。容器是正方形圆框，viewBox 若是 48×31，
   contain 仍按短边贴齐，上下照旧留白。所以取 max(w,h) 做边长、
   把内容居中放进去，两个方向才都能撑满。

2. **全画布背景矩形要跟着扩**。anthropic / openai 有 `<rect width=48 height=48>`
   当底，只改 viewBox 的话这个 rect 会缩到新边界中间，露出没铺到的底色。
   凡是 bbox 几乎等于原 viewBox 的 rect，一律重写成铺满新 viewBox。

3. **原件必须留一份**，存 logos_raw/。否则下次抓取覆盖掉裁剪版之后，
   没人知道原图长什么样，也没法重裁。

依赖 svgelements 算 bbox。注意它的 `SVG.parse()` 只认**文件路径或 file object**，
传 bytes 会被当路径丢给 xml 解析器，报一个极具误导性的 FileNotFoundError。
"""
from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from svgelements import SVG

SVG_NS = "http://www.w3.org/2000/svg"
TAIL = re.compile(r"\s+")

# 不注册的话 ET.tostring 会把标签写成 ns0:svg，能用但难看
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")


def _num(v) -> float:
    return float(v)



def shape_bbox(path: str) -> tuple[float, float, float, float] | None:
    """图形内容的包围盒，排除铺满画布的背景矩形。

    算整个内容的 bbox 会被那层垫底的 rect 骗过去：它等于整个 viewBox，
    算进去等于没裁。所以先把「bbox 几乎等于 viewBox 的 Rect」踢掉。
    """
    svg = SVG.parse(path, on_error="raise")
    vb = svg.viewbox
    if vb is None:
        return None
    ow = float(getattr(vb, "width", 0) or 0)
    oh = float(getattr(vb, "height", 0) or 0)

    boxes = []
    for el in svg.elements():
        if type(el).__name__ == "SVG":
            continue
        b = el.bbox()
        if not b:
            continue
        if type(el).__name__ == "Rect" and abs((b[2] - b[0]) - ow) < 0.6 \
           and abs((b[3] - b[1]) - oh) < 0.6:
            continue            # 全画布背景，不是图形
        boxes.append(tuple(float(x) for x in b))
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def crop_box(path: str, pad: float = 0.0) -> tuple[str, str] | None:
    """算出裁剪后的 viewBox 字符串，以及原 viewBox。

    返回的 viewBox 是紧贴图形外接的**正方形**：容器是正方形圆框，
    只有 viewBox 也是正方形时 contain 才能 100% 填满。
    若只裁到图形的矩形边界（比如 48×31），contain 会按短边贴齐，
    上下照样留白——白裁。

    返回 (new_viewBox, origin_viewBox)，算不出来返回 None。
    """
    svg = SVG.parse(path, on_error="raise")
    vb = svg.viewbox
    if vb is None:
        return None
    # Viewbox 是对象不是元组，它没有 __iter__，只能逐个取属性
    ox, oy = float(vb.x), float(vb.y)
    ow = float(getattr(vb, "width", 0) or 0)
    oh = float(getattr(vb, "height", 0) or 0)
    if ow <= 0 or oh <= 0:
        return None
    origin = f"{ox} {oy} {ow} {oh}"

    bb = shape_bbox(path)
    if bb is None:
        return None
    x0, y0, x1, y1 = bb
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return None

    size = max(w, h) + pad * 2
    nx = x0 - (size - w) / 2
    ny = y0 - (size - h) / 2
    return (f"{round(nx, 3)} {round(ny, 3)} {round(size, 3)} {round(size, 3)}", origin)


def crop_bytes(raw: bytes) -> bytes | None:
    """把一份 SVG 源码裁掉内边距；失败返回 None（调用方应保留原件）。"""
    text = raw.decode("utf-8", "ignore")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if root.tag != f"{{{SVG_NS}}}svg":
        return None

    old_vb = root.get("viewBox")
    if not old_vb:
        return None
    try:
        ox, oy, ow, oh = (float(v) for v in TAIL.split(old_vb.strip()))
    except ValueError:
        return None

    # 算 bbox 必须走文件路径：svgelements 对 bytes 会报 FileNotFoundError
    import os
    import tempfile
    fd, tmp_path = tempfile.mkstemp(suffix=".svg")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        got = crop_box(tmp_path)
    finally:
        os.unlink(tmp_path)
    if got is None:
        return None
    new_vb, _ = got

    root.set("viewBox", new_vb)
    # width/height 跟着改，免得和外框比例对不上被浏览器多留白
    root.set("width", f"{round(float(new_vb.split()[2]), 3)}")
    root.set("height", f"{round(float(new_vb.split()[3]), 3)}")

    k = float(new_vb.split()[2]) / ow if ow else 1.0
    for child in root:
        tag = child.tag.split("}")[-1]
        if tag != "rect":
            continue
        # 铺满原 viewBox 的底：扩到新 viewBox，否则会缩成中间一小块
        rx, ry, rw, rh = (child.get(kk) for kk in ("x", "y", "width", "height"))
        try:
            if (rx is None or _num(rx) == ox) and (ry is None or _num(ry) == oy) \
               and abs(_num(rw) - ow) < 0.5 and abs(_num(rh) - oh) < 0.5:
                child.set("x", f"{round(float(new_vb.split()[0]), 3)}")
                child.set("y", f"{round(float(new_vb.split()[1]), 3)}")
                child.set("width", f"{round(float(new_vb.split()[2]), 3)}")
                child.set("height", f"{round(float(new_vb.split()[3]), 3)}")
                # 圆角半径没跟着缩的话，40×40 里的 rx=24 会变成近圆，
                # 外观和裁剪前对不上。
                for attr in ("rx", "ry"):
                    v = child.get(attr)
                    if v:
                        child.set(attr, f"{round(_num(v) * k, 3)}")
        except (TypeError, ValueError):
            continue

    return ET.tostring(root, encoding="unicode").encode("utf-8")
