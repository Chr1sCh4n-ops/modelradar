#!/usr/bin/env python3
"""把看板主图「速度与成本性价比定位图」渲染成静态 SVG / 自包含 HTML。

数据来自 data/latest.json（爬虫产出），所以永远是最新的一版。

    python render_scatter_svg.py                   # 打到标准输出
    python render_scatter_svg.py -o chart.svg      # 写 SVG 文件
    python render_scatter_svg.py -o data/chart.html # 写自包含 HTML（状态栏组件用）
    python render_scatter_svg.py --json            # 只输出坐标数据，便于自己制图

约定（跟看板一致）：
  x = 每任务成本 apt_usd（对数轴）  y = 智能指数 index
  气泡**面积** 默认 ∝ tok_s（半径 = R_MAX·√x，下限只为看得见）  填充 = 厂商品牌色
  可切到「按任务耗时显示大小」：面积分数 .04+.96u，u 由 log10(耗时) 反比归一化（越快越大）
  无 tok_s 的点用比最慢真实点略小一号的整数半径占位（见 node_radius）
  同族同代次的档位（low/medium/high/xhigh/max）用厂商同色的细折线连成「能力线」，
   按推理档位固定顺序串联；黄色虚线 = Pareto 前沿（更便宜且更高分，不被任何点支配）

三种产物共用同一份坐标，避免「图对了但壳不对」。
"""
from __future__ import annotations


import argparse
from decimal import Decimal, ROUND_HALF_UP
import html
import json
import math
import os
import sys
from pathlib import Path

import mr_paths
import task_size
from task_size import (R_MAX, R_MIN, TASK_MIN_FRACTION, TASK_R_MIN, R_MISSING,
                       metric_number, valid_task_time, task_area_fraction,
                       task_radius)

HERE = Path(__file__).resolve().parent
LATEST = mr_paths.LATEST_JSON

W, H = 680, 452
PL, PR, PT, PB = 54, 20, 24, 46

# 气泡尺寸与任务耗时面积编码的常数、以及"全局低分收缩"的纯数学，都放在 task_size.py：
# 爬虫要按同一套公式拟合 λ，渲染面要按同一套公式画半径，抄两份必然漂。



def radius(tok, tmax: float, r_node: float = R_MIN) -> float:
    """按面积编码算气泡半径；tok 缺失时用 r_node 占位。"""
    if not tok:
        return r_node
    return round(max(R_MIN, R_MAX * math.sqrt(max(tok, 0) / (tmax or 1))), 1)


def node_radius(shown: list[float]) -> int:
    """无 tok 数据的占位半径：整数，且严格小于最小的真实气泡。

    写死的 R_MIN（2）在今天的数据上比最慢的真实点（4.7）小一半多，看着像"另一种
    标记"而不是"这个点没测到速度"。改成跟着最慢真实点走，占位点就永远落在刻度底端
    附近；取整 + ceil-1 是为了在最小值恰好是整数时仍然严格更小。
    """
    return max(1, math.ceil(min(shown)) - 1) if shown else 1

# 颜色一律走 CSS 变量 + 深色回退值：单独出 .svg 文件时没有宿主页面，取回退值；
# 塞进 HTML 时由 build_html 按系统外观给两套值 —— 这样同一份 SVG 在浅色/深色下都清晰，
# 而不是把深色值钉死（钉死的话浅色 popover 上就是白字糊白底）。
INK = "var(--mr-ink,#e8eaed)"
DIM = "var(--mr-dim,#8b95a5)"
GRID = "var(--mr-grid,#2b3648)"
AXIS = "var(--mr-axis,#39445a)"
FRONT = "#e3b341"

LEGEND_ROW_H = 17           # 一行的行距
LEGEND_MIN_PITCH = 100.0    # 一格的最小宽度：名字短时也别挤成一坨
LEGEND_DOT_TEXT_GAP = 9     # 圆点右缘到文字起点
LEGEND_CELL_PAD = 12        # 一格留给下一格圆点的气口
LEGEND_LABEL = "Pareto 前沿"    # 右上角那条标注：第一行图例要给它的 y 带让位
LEGEND_HEIGHT_FRACTION = 0.4    # 图例最多占绘图区高度的四成，超出的厂商才退回 "+N 家"
LEGEND_TEXT_HALF = 8    # 一行文字的视觉半高：11px 字的基线上下各约这么多像素
LEGEND_GAP = 6          # 气泡边缘到图例右界的硬气口


def text_w(s: str, fs: float = 11.0) -> float:
    """粗估一行字宽：CJK 按字号、其余按 0.6 字号。图例与提示卡都只要求"不压到别人"，不必精确。"""
    return sum(fs if ord(c) > 0x2e80 else fs * 0.6 for c in str(s))


def legend_layout(names: list[str], obstacles=()) -> dict:
    """按名字**实际宽度**排布图例：一格多宽、每行几格、最多几行、装得下几家。

    以前钉死"每行 4 格 × 3 行，多的写 +N 家"，厂商一多就只能省略。现在格宽与列数从名字量出来，
    列起点共用同一份网格：第一行与「Pareto 前沿」标注同带，必须停在标注左边界之前；
    下面各行按绘图区右边界算自己的最大列数，但**遇到气泡就提前收窄**（2026-11-06
    用户口径：每行用满允许宽度、又不压住右侧的点）。装不下就往下加行，不截断名字。

    障碍判据用圆的**包围盒**跟该行文字带比（`oy ± r` 对 `y ± LEGEND_TEXT_HALF`）。
    以前只比圆心（±8）：半径比 8 大的球，圆心落在两行之间时两条带都躲开它，球照样压字
    （2026-11-10 截图里那两处叠压就是这么漏的）。
    """
    max_w = max([text_w(n) for n in names], default=0.0)
    pitch = max(LEGEND_MIN_PITCH, LEGEND_DOT_TEXT_GAP + max_w + LEGEND_CELL_PAD)
    left = PL + 2
    label_left = (W - PR) - text_w(LEGEND_LABEL)

    rows_max = max(1, int((H - PB - PT) * LEGEND_HEIGHT_FRACTION) // LEGEND_ROW_H)
    base = PT + 6
    limits = [label_left - LEGEND_GAP] + [W - PR - 2] * (rows_max - 1)
    # 一个球只够得着少数几行（r ≤ R_MAX，行距 17，至多 3 行）：先求它覆盖的行区间，
    # 只累加那几行，不再拿每行去扫全部点。代价 O(点数 × 覆盖行数)，与厂商数无关。
    for ox, oy, r in obstacles:
        edge = ox - r - LEGEND_GAP
        lo = max(0, math.ceil((oy - r - LEGEND_TEXT_HALF - base) / LEGEND_ROW_H))
        hi = min(rows_max, math.floor((oy + r + LEGEND_TEXT_HALF - base) / LEGEND_ROW_H) + 1)
        for row in range(lo, hi):
            if edge < limits[row]:
                limits[row] = edge
    caps = []
    offset = 0
    for limit in limits:
        count = 0
        while True:
            width = text_w(names[offset + count]) if offset + count < len(names) else max_w
            right = left + count * pitch + LEGEND_DOT_TEXT_GAP + width
            if right > limit:
                break
            count += 1
        # 列距按最长名字保守留足，但每行末格只需容下自己的文字，不再预留下一格的气口。
        # 一格都放不下时这一行**让位**（count=0），条目顺延到下一行，不再硬画在球上。
        caps.append(count)
        offset += count
    if not any(caps):
        caps[0] = 1     # 每行都被压住时退回旧行为，图例不能整块消失
    return {"pitch": pitch, "cols": caps, "rows_max": rows_max,
            "cap": sum(caps), "label_left": label_left}

# 悬停高亮：一个档位被指到 → 整族（能力线 + 该族所有档位气泡）置顶 + 放大 + 加粗。
# 这两段是**唯一的一份实现**，dashboard.html 里必须逐字同步（verify_dashboard.mjs 会
# 把两边抠出来对比，防止"网页能高亮、菜单栏不能"这种漂移）。
HOVER_CSS = """  .mk{cursor:pointer}
  .mk.hi circle{stroke-width:2.2;stroke-opacity:1}
  /* 沿用回来的行平时半透明（读数旧、只是给个上下文），指到就恢复正常不透明度：
     高亮状态必须能读准，否则"点进去了还是灰的"会被当成没数据。 */
  .mk.carr{opacity:.6}
  .mk.carr.hi{opacity:1}
  .ln{transition:none}
  .ln.hi{stroke-width:2.4;stroke-opacity:1}
  /* 提示卡：默认不画，**卡片自己**带 .hi 才显（卡片是与气泡平级的兄弟，带同一组键）。
     为什么不是"父级 .hi"：整族高亮那步会给 .mk 加 scale(1.45)，卡片在子树里就会被一起放大成
     14.5px 的字；搬出来字号才是量出来的那一份。
     一屏贴不完：JS 按 data-pri（分数高的先）逐张试位，压到已贴的就不加 .hi ——
     全部数字都在图例那张聚合卡里列着，一条都不会少。
     pointer-events 必须关 —— 卡片一旦吃指针，事件的 target 就变成卡片自己，
     closest('[data-k]') 落空走延迟收起，指针一动就自激闪烁（2026-09-29 那个线上症状）。
     配色复用脚注浮层那三个变量，视觉上就是同一种东西，不再造第二套样式。 */
  .chip{display:none;pointer-events:none}
  .chip.hi{display:block}
  .chip rect{fill:var(--mr-bub);fill-opacity:.97;stroke:var(--mr-bub-line);stroke-width:1}
  .chip text{font:10px/1.4 -apple-system,"PingFang SC",sans-serif;fill:var(--mr-bub-ink)}
  .lgc{cursor:pointer}
  /* 只加粗图例那一行标签本体；卡片里的文字归 .chip text 管 */
  .lgc.hi>text{fill:var(--mr-ink);font-weight:600}"""

HOVER_JS = """(function(){
/* HOVER-BEGIN */
  // 选择器要"先找散点容器、再退回唯一 svg"：看板页面上还有一张 0 尺寸的裁剪 <svg>，
  // 它排在散点前面，直接 querySelector('svg') 会绑到那张空图上，高亮整个失灵。
  var svg = document.querySelector('#scatterwrap svg') || document.querySelector('svg');
  if (!svg) return;
  var top = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  top.setAttribute('class', 'toplayer');
  svg.appendChild(top);
  // 卡片单独一个组，且永远是 toplayer 的第一个孩子：SVG 按文档序绘制，
  // 这样卡片天然在气泡与能力线下面（用户口径：原点和连线悬浮到卡片上层），
  // 卡片内部谁压谁只取决于组内顺序，不用跟点线抢位置。
  var cards = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  cards.setAttribute('class', 'cards');
  top.insertBefore(cards, top.firstChild || null);
  var home = [], cur = null, pend = null;
  // 选择优先级：指针指着的组 > 仍有焦点的脚注 > 都不亮。
  // pointerFoot / hoverGroup 是指针态，focusFoot 是焦点态，分开记；清理时按优先级重算，
  // 不能无条件熄灯 —— 脚注 A 有焦点、指针掠过脚注 B 再离开时（或离开图区、空白处延时收起时），
  // A 的高亮必须留下。否则键盘用户只是被鼠标路过，焦点选择就被抹掉了。
  var pointerFoot = null, hoverGroup = null, focusFoot = null;
  var esc = function(k){ return (window.CSS && CSS.escape) ? CSS.escape(k) : k.replace(/[^a-z0-9]/gi,''); };
  function enlarge(el){
    // 用 getBBox 自己算缩放中心：CSS transform-box:fill-box 在部分 WebKit 上对 <g> 不生效，
    // 那种情况下缩放会绕 SVG 原点做，气泡直接飞出图外 —— 比不高亮更糟。
    var b;
    try { b = el.getBBox(); } catch (e) { return; }
    if (!b || !b.width) return;
    var cx = b.x + b.width / 2, cy = b.y + b.height / 2;
    el.setAttribute('transform',
      'translate(' + cx + ',' + cy + ') scale(1.45) translate(' + (-cx) + ',' + (-cy) + ')');
  }
  function fitCard(ch){
    // 塌缩：把背景矩形收到"文字实际并集 + 内边距"。作者侧的字宽是估的（各机器字体不同），
    // 内容少的时候卡底会空出一大块 —— 用户要的"内容不足时自动塌缩"就是这一步。
    // 塌缩只能朝卡片伸出去的那一侧收：tl 钉住左上角、br 钉住右下角，
    // 否则卡片会滑离自己的小球，读者认不出这张卡说的是哪个档。
    var r = ch.querySelector ? ch.querySelector('rect') : null;
    if (!r) return null;
    // 基准只用 data-r0（作者侧原始框），不用 rect 当前值：塌缩会改写 rect，
    // 拿当前值当基准就是"每次悬停再缩一次、再挪一次"
    var r0 = String(ch.getAttribute('data-r0') || '').split(' ');
    var ax = +r0[0], ay = +r0[1], aw = +r0[2], ah = +r0[3];
    if (!(ax >= 0) || !(aw > 0)) { ax = +r.getAttribute('x'); ay = +r.getAttribute('y');
                                   aw = +r.getAttribute('width'); ah = +r.getAttribute('height'); }
    var u = null;
    var ts = ch.querySelectorAll('text');
    for (var i = 0; i < ts.length; i++) {
      var b = null;
      try { b = ts[i].getBBox(); } catch (e) { b = null; }
      if (!b || !b.width) continue;
      if (!u) u = { x: b.x, y: b.y, x2: b.x + b.width, y2: b.y + b.height };
      else {
        u.x = Math.min(u.x, b.x); u.y = Math.min(u.y, b.y);
        u.x2 = Math.max(u.x2, b.x + b.width); u.y2 = Math.max(u.y2, b.y + b.height);
      }
    }
    if (!u) return [ax, ay, aw, ah];        // 量不到（假 DOM 桩、老引擎）就照作者侧那份尺寸用
    var w = (u.x2 - u.x) + 12, h = (u.y2 - u.y) + 8;
    var x = u.x - 6, y = u.y - 4;
    r.setAttribute('x', x.toFixed(1)); r.setAttribute('y', y.toFixed(1));
    r.setAttribute('width', w.toFixed(1)); r.setAttribute('height', h.toFixed(1));
    // 整组平移：只挪矩形会把文字留在原地（实测 7 行全部出框）。
    // 平移量按贴法钉住对应那一角：br 钉右下角（族卡对着球的那一角）、tl 钉左上角。
    var dx = 0, dy = 0;
    if ((ch.getAttribute('data-anchor') || 'tl') === 'br') { dx = ax + aw - (x + w); dy = ay + ah - (y + h); }
    else { dx = ax - x; dy = ay - y; }
    // 这里**不写** transform：写它的只有 cardsOf（它还要在同一段上叠退让量）。
    // 两处各写一遍，后写的会把钉角那段覆盖掉 —— 卡片整张滑离自己的小球。
    // 末尾两个数是塌缩的钉角偏移，cardsOf 拿它们算最终平移量。
    return [x + dx, y + dy, w, h, dx, dy];
  }
  // 绘图区四边从 svg 的 data-plot 读，不在 JS 里再抄一份几何常数（画布一改就分叉）
  var plot = String(svg.getAttribute('data-plot') || '54 24 660 406').split(' ').map(Number);
  function clampX(x, w){ return Math.min(Math.max(x, plot[0] + 2), Math.max(plot[0] + 2, plot[2] - 2 - w)); }
  function clampY(y, h){ return Math.min(Math.max(y, plot[1] + 2), Math.max(plot[1] + 2, plot[3] - 2 - h)); }
  /* 候选位 = 退让顺序：作者位 → 绕中心球换四角 → 上下左右错开一格。全都夹在绘图区内。
     先保"贴着球"的作者位，再试同样贴球但换一侧，最后才平移让路。 */
  function places(ch, box){
    var w = box[2], h = box[3], out = [[box[0], box[1]]];
    var b = String(ch.getAttribute('data-ball') || '').split(' ').map(Number);
    if (b.length === 3 && isFinite(b[0]) && b[0] > 0) {
      var bx = b[0], by = b[1], br = b[2], g = 4;
      out.push([clampX(bx + br + g, w), clampY(by + br + g, h)]);          // 球的右下
      out.push([clampX(bx - br - g - w, w), clampY(by + br + g, h)]);      // 球的左下
      out.push([clampX(bx + br + g, w), clampY(by - br - g - h, h)]);      // 球的右上
      out.push([clampX(bx - br - g - w, w), clampY(by - br - g - h, h)]);  // 球的左上（作者位同侧）
    }
    out.push([clampX(box[0], w), clampY(box[1] - h - 6, h)]);
    out.push([clampX(box[0], w), clampY(box[1] + h + 6, h)]);
    out.push([clampX(box[0] - w - 6, w), clampY(box[1], h)]);
    out.push([clampX(box[0] + w + 6, w), clampY(box[1], h)]);
    return out;
  }
  function cardsOf(list, cards){
    // 顶栏「▤ 卡片」关掉时一张都不显（高亮照旧）。undefined = 不干预，
    // 所以直接用浏览器打开 chart.html 或看板页面时行为不变。
    if (window.MR_CARDS === 0) return;
    var taken = [], sunk = [], up = [];
    list.sort(function(x, y){
      return (+x.getAttribute('data-pri')) - (+y.getAttribute('data-pri'));
    }).forEach(function(ch){
      var q = fitCard(ch);                  // 先塌缩再比位：拿真实尺寸比才不会白让位
      if (!q) return;
      // 候选位按退让顺序试：先试作者位，一有空位就定（这样"贴球"永远优先）；
      // 全都压着别人才取**压得最少**的那一个 —— 避让是"沉到底下但仍露出"，不是"就不显示"。
      var cands = places(ch, q), free = null, best = null, bestPen = Infinity;
      for (var i = 0; i < cands.length; i++) {
        var box = [cands[i][0], cands[i][1], q[2], q[3]], pen = 0;
        for (var j = 0; j < taken.length; j++) {
          var ox = Math.min(box[0] + box[2], taken[j][0] + taken[j][2]) - Math.max(box[0], taken[j][0]);
          var oy = Math.min(box[1] + box[3], taken[j][1] + taken[j][3]) - Math.max(box[1], taken[j][1]);
          if (ox > 0 && oy > 0) pen += ox * oy;
        }
        if (pen === 0) { free = box; break; }
        if (pen < bestPen) { bestPen = pen; best = box; }
      }
      if (free) up.push(ch); else { free = best; sunk.push(ch); }
      ch.classList.add('hi');
      // transform = 塌缩的钉角偏移 + 退让的偏移，两段必须一起写：fitCard 已经把矩形改成了
      // 局部坐标，这里只写退让量就会把塌缩那段丢掉，整张卡滑离它钉住的那一角（实测偏 162px）。
      ch.setAttribute('transform', 'translate(' + (q[4] + free[0] - q[0]).toFixed(1) + ','
                                    + (q[5] + free[1] - q[1]).toFixed(1) + ')');
      taken.push(free);
    });
    // 组内顺序 = 叠放顺序：无论沉底还是贴上位，一律**分数高的最后画**（压在上面）。
    // 沉底的那批若按处理序（分数高→低）画，就成了"赢家垫在最下、输家盖着它"，
    // 与"衬到底下"正好相反 —— 所以两组都要 pri 倒序。
    sunk.slice().reverse().concat(up.slice().reverse()).forEach(function(ch){ cards.appendChild(ch); });
  }
  function restack(){
    // 点与线之间也要层序：能力线在下、气泡在上、被指到的那格图例最上。
    // 名单先快照：appendChild 会就地改 children，边遍历活集合边搬会漏项（第一版就搬"乱"过）。
    var kids = [].slice.call(top.children);
    kids.forEach(function(el) { if (el.classList.contains('ln')) top.appendChild(el); });
    kids.forEach(function(el) { if (el.classList.contains('mk')) top.appendChild(el); });
    kids.forEach(function(el) { if (el.classList.contains('lgc')) top.appendChild(el); });
  }
  function remember(el){
    // 先记原位再搬：同父节点的多个元素如果边搬边记，后一个记到的下标已经被前一个腾走，
    // 恢复时就会错位（脚注按集合一次抬起多个点时最容易撞上）。
    home.push([el, el.parentNode, Array.prototype.indexOf.call(el.parentNode.children, el)]);
  }
  function light(k, a, force){
    if (!force && cur && cur.mode === 'group' && cur.a === a && cur.k === k) return;   // 同一组内的微移动必须完全不动 DOM
    dark();
    cur = {mode:'group', a:a, k:k};
    var chips = [];
    // 两种粒度共用一套搬运：data-k = 同族同代次（悬停一个气泡时），
    // data-c = 整个厂商（悬停图例格时）。图例格只有 data-c，气泡两个都有 ——
    // 所以落点在气泡上就必然走族，不会被厂商组盖掉。
    var hits = [].slice.call(svg.querySelectorAll('[' + a + '="' + esc(k) + '"]'));
    hits.forEach(remember);
    hits.forEach(function(el){
      if (el.classList.contains('chip')) { chips.push(el); cards.appendChild(el); }
      else {
        el.classList.add('hi');
        if (el.classList.contains('mk')) enlarge(el);
        top.appendChild(el);          // 置顶：SVG 按文档序绘制，不搬就是被压住
      }
    });
    restack();
    cardsOf(chips, cards);
  }
  // 脚注的三个数字是**精确子集**（哪些点没测到速度、哪些是推估、哪些是参照带），
  // 不能拿 data-k 整族去亮：一族里可能只有一档属于这个子集，整族抬起就多亮了。
  // 所以这里按 data-i 逐个认点；线只有在整族都被选中时才亮；卡片按命中的族复用 cardsOf。
  function lightIds(list, force){
    var ids = (list || []).map(String);
    var sig = ids.join(',');
    if (!force && cur && cur.mode === 'ids' && cur.sig === sig) return;
    dark();
    cur = {mode:'ids', ids: ids, sig: sig};
    var picked = {};
    ids.forEach(function(i){ picked[i] = 1; });
    var marks = [].slice.call(svg.querySelectorAll('.mk[data-i]')).filter(function(el){
      return picked[el.getAttribute('data-i')];
    });
    // 能力线是族级的：只有整族都在这个子集里才亮，避免"数了 3 个点、亮了一整族"。
    var fams = [], seen = {};
    marks.forEach(function(el){
      var k = el.getAttribute('data-k');
      if (k !== null && !seen[k]) { seen[k] = 1; fams.push(k); }
    });
    var lines = [].slice.call(svg.querySelectorAll('.ln[data-k]')).filter(function(el){
      var k = el.getAttribute('data-k'), mks = svg.querySelectorAll('.mk[data-k="' + esc(k) + '"]');
      if (!mks.length) return false;
      for (var i = 0; i < mks.length; i++) {
        if (!picked[mks[i].getAttribute('data-i')]) return false;
      }
      return true;
    });
    var chips = [];
    fams.forEach(function(k){
      [].forEach.call(svg.querySelectorAll('.chip[data-k="' + esc(k) + '"]'), function(ch){
        chips.push(ch);
      });
    });
    // 所有元素先在原位上记完再搬（见 remember 的理由）。
    marks.forEach(remember);
    lines.forEach(remember);
    chips.forEach(remember);
    marks.forEach(function(el){
      el.classList.add('hi');
      enlarge(el);
      top.appendChild(el);
    });
    lines.forEach(function(el){ el.classList.add('hi'); top.appendChild(el); });
    // 卡片按命中的族拿 data-k：只搬这几族，不全局清空别的族；MR_CARDS=0 时 cardsOf 会
    // 直接返回，卡片不显，但点的高亮照旧（见 cardsOf 开头）。
    chips.forEach(function(ch){ cards.appendChild(ch); });
    restack();
    cardsOf(chips, cards);
  }
  function restoreChip(el){
    // 卡片塌缩会改写 rect 的 x/y/w/h；只清 transform 会让它停在塌缩后的尺寸上。
    // 按 data-r0 还原作者侧几何（SIZE_JS 切换模式时也同步更新 data-r0）。
    if (!el.classList || !el.classList.contains('chip')) return;
    var r = el.querySelector ? el.querySelector('rect') : null;
    if (!r) return;
    var r0 = String(el.getAttribute('data-r0') || '').split(' ').map(Number);
    if (r0.length === 4 && isFinite(r0[0]) && isFinite(r0[2]) && r0[2] > 0) {
      r.setAttribute('x', r0[0].toFixed(1));
      r.setAttribute('y', r0[1].toFixed(1));
      r.setAttribute('width', r0[2].toFixed(1));
      r.setAttribute('height', r0[3].toFixed(1));
    }
  }
  function dark(){
    if (!home.length && !cur) return;
    // 归位按"原父节点分组 + 原下标升序"，不能按 home 的记录顺序逐个插：
    // 一次抬起同父的多个节点时，先插回去的那个会占掉下标，后一个再按旧下标插就前后颠倒
    // （实测整族含能力线时 mk6 会跑到 mk5 前面，连末尾的 toplayer 也跟着漂）。
    // 升序插入为什么对：下标更小的兄弟都已回位，下标更大的要么原位没动、要么还没回来，
    // 都在当前 children[原下标] 或更后面；所以此刻 children[原下标] 就是该插的位置。
    // 不用 nextSibling 引用：前一个节点被搬走会让后一个记的引用失效。
    var buckets = [];
    home.forEach(function(h){
      var b = null;
      for (var i = 0; i < buckets.length; i++) {
        if (buckets[i][0] === h[1]) { b = buckets[i]; break; }
      }
      if (!b) { b = [h[1], []]; buckets.push(b); }
      b[1].push(h);
    });
    buckets.forEach(function(b){
      b[1].sort(function(x, y){ return x[2] - y[2]; }).forEach(function(h){
        h[0].classList.remove('hi');
        h[0].removeAttribute('transform');
        restoreChip(h[0]);
        b[0].insertBefore(h[0], b[0].children[h[2]] || null);
      });
    });
    home = []; cur = null;
  }
  // 切换气泡尺寸模式后，若指针正停在某一组上，必须按新半径重新贴卡；
  // 不重贴就会沿用旧锚点与旧碰撞列表（卡片离球或压住别人）。只重放当前组，不新开一套几何。
  // 脚注子集也走同一个钩子重放，否则开关模式时卡片会停在旧几何上。
  svg.__mrRelayout = function(){
    if (!cur) return;
    var c = cur;
    if (c.mode === 'ids') lightIds(c.ids, true);
    else light(c.k, c.a, true);
  };
  // 只接 pointerover / pointerleave，**不接 pointerout**：搬动节点时浏览器会为
  // 被搬走的节点补发 pointerout，一接上就成了自己触发自己 —— 指针轻微移动就在
  // "搬上去/搬回来"之间来回抖（2026-09-29 线上闪烁就是这个）。
  // 同理 .toplayer 不能设 pointer-events:none：那样一置顶就不再命中，指针的 target
  // 立刻变成顶层下面的空白图区，同样进闪烁循环。
  // 一次清理、或一次指针/焦点变化后，都按同一优先级重算"现在该亮谁"：
  // 指针指着的组 > 仍有焦点的脚注 > 都不亮。清理不再无条件熄灯，
  // 否则脚注 A 有焦点、指针掠过脚注 B 再离开（或离开图区、空白处延时收起）时，
  // A 的高亮会被 B 的 pointerleave / svg 的 pointerleave 一起抹掉。
  function applySelection(){
    if (pointerFoot) { cancelDark(); lightIds(footIds(pointerFoot)); return; }
    if (hoverGroup) { cancelDark(); light(hoverGroup.k, hoverGroup.a); return; }
    if (focusFoot) { cancelDark(); lightIds(footIds(focusFoot)); return; }
    cancelDark(); dark();
  }
  // 指针停在空白处时的延迟收起：到点后同样走 applySelection，
  // 有焦点脚注就退回它，而不是把整张图清空。
  function scheduleDark(){ if (!pend) pend = setTimeout(function(){ pend = null; applySelection(); }, 80); }
  function cancelDark(){ if (pend){ clearTimeout(pend); pend = null; } }
  svg.addEventListener('pointerover', function(e){
    // 两种钩子分开找：气泡/能力线带 data-k（同族同代次，粒度细），图例格只带 data-c（整厂）。
    // 一个点两个属性都有 —— 所以先认 data-k，悬停单个档位亮它自己那一族，
    // 悬停图例格才把这家全部档位抬起来。用两次单选择器而不是 '[data-k],[data-c]'：
    // 语义一样，但假 DOM 桩的 closest 只解析单条选择器，写逗号就是在考桩子。
    var t = e.target.closest ? (e.target.closest('[data-k]') || e.target.closest('[data-c]')) : null;
    if (t) {
      var a = 'data-k', k = t.getAttribute('data-k');
      if (k === null) { a = 'data-c'; k = t.getAttribute('data-c'); }
      hoverGroup = {a:a, k:k};
      pointerFoot = null;          // 指针已进图区，脚注不再是指针态
      cancelDark(); light(k, a);
    }
    // 掠过金色前沿、网格线这类"非组节点"时**延迟**收起：同组两档之间移动必然穿过它们，
    // 立刻收起就会闪；停手 80ms 后确实还在空白处才收（有焦点脚注则退回它）。
    else {
      hoverGroup = null;
      scheduleDark();
    }
  });
  svg.addEventListener('pointerleave', function(){
    // 离开图区不等于熄灯：键盘用户可能还聚焦在某个脚注上，指针只是路过。
    hoverGroup = null;
    applySelection();
  });
  // 脚注三个计数：悬停/聚焦时按 data-index 抬起精确子集；离开/失焦按优先级回退
  // （指针 > 焦点 > 无），并按 dark() 还原 DOM 序、高亮与卡片几何。
  // 气泡由作者侧写进 .bub（名字已转义），不依赖原生 title。
  function footIds(el){
    return String(el.getAttribute('data-index') || '').split(',').filter(function(s){ return s !== ''; });
  }
  [].forEach.call(document.querySelectorAll('[data-foot-group]'), function(el){
    el.addEventListener('pointerenter', function(){
      pointerFoot = el;
      hoverGroup = null;           // 指针已到脚注上，图区悬停态结束
      cancelDark(); lightIds(footIds(el));
    });
    el.addEventListener('pointerleave', function(){
      if (pointerFoot === el) pointerFoot = null;
      // 另一个脚注还有焦点就立即退回它（焦点不该被鼠标路过抹掉）；
      // 没有焦点才走延迟收起，穿过脚注文字/气泡时不闪。
      if (focusFoot || hoverGroup) applySelection(); else scheduleDark();
    });
    el.addEventListener('focus', function(){
      focusFoot = el;
      // 指针仍指在组上时指针优先：焦点只登记，不抢高亮。
      if (pointerFoot || hoverGroup) return;
      cancelDark(); lightIds(footIds(el));
    });
    el.addEventListener('blur', function(){
      if (focusFoot === el) focusFoot = null;
      // 焦点在脚注之间移动时会先 blur 再 focus：这里只排队，focus 会取消它。
      if (pointerFoot || hoverGroup) applySelection(); else scheduleDark();
    });
    el.addEventListener('keydown', function(e){
      if (e.key === 'Escape') {
        // Escape 是显式清场：清高亮、清焦点，并把三种选择态一起清掉，
        // 防止随后的 blur 排队又把同一组点亮（cleanup no stale）。
        cancelDark(); pointerFoot = null; hoverGroup = null; focusFoot = null;
        dark(); el.blur();
      }
    });
  });
/* HOVER-END */
})();"""


SIZE_JS = """(function(){
/* SIZE-BEGIN */
  var svg = document.querySelector('#scatterwrap svg') || document.querySelector('svg');
  var box = document.getElementById('task-size');
  var penBox = document.getElementById('penalizelow');
  var penWrap = document.getElementById('penalty-wrap');
  var cap = document.getElementById('size-caption');
  if (!svg || !box) return;
  // 载荷带不带收缩块由渲染面钉成属性：缺块（旧快照）时开关禁用并在说明里讲明，
  // 绝不拿 factor=1 冒充"已收缩"——那会让读者以为低分档已经按分数压过了。
  var penReady = svg.getAttribute('data-task-pen') === '1';
  var plot = String(svg.getAttribute('data-plot') || '54 24 660 406').split(' ').map(Number);
  var nodes = [].slice.call(svg.querySelectorAll('.mk')).map(function(g){
    return {g:g, i:g.getAttribute('data-i'), r:+(g.getAttribute('data-r')||0),
            rt:+(g.getAttribute('data-r-task')||0),
            rp:g.getAttribute('data-r-task-pen') === null ? null
               : +(g.getAttribute('data-r-task-pen')||0)};
  });
  var byNote = {};
  [].forEach.call(svg.querySelectorAll('[data-note-for]'), function(t){
    byNote[t.getAttribute('data-note-for')] = {t:t, y:+(t.getAttribute('y')||0)};
  });
  var chips = [].slice.call(svg.querySelectorAll('.chip')).filter(function(ch){
    return ch.getAttribute('data-ball') !== null;
  }).map(function(ch){
    var rect = ch.querySelector('rect');
    var texts = [].slice.call(ch.querySelectorAll('text'));
    var btPen = ch.getAttribute('data-ball-task-pen');
    return {ch:ch, rect:rect, texts:texts,
            r0:String(ch.getAttribute('data-r0')||'').split(' ').map(Number),
            ball:String(ch.getAttribute('data-ball')||'').split(' ').map(Number),
            bt:String(ch.getAttribute('data-ball-task')||'').split(' ').map(Number),
            btp:btPen === null ? null : btPen.split(' ').map(Number),
            rectX:+(rect.getAttribute('x')||0), rectY:+(rect.getAttribute('y')||0),
            tx:texts.map(function(t){return [+(t.getAttribute('x')||0), +(t.getAttribute('y')||0)];})};
  });
  function put(el, a, v){ if (el) el.setAttribute(a, (+v).toFixed(1)); }
  function radiusOf(n, task, pen){
    if (!task) return n.r;
    return pen && n.rp !== null ? n.rp : n.rt;
  }
  function apply(task){
    // CSS 用 data-size-mode 隐藏 TPS 推估环带；JS 只改半径、锚点与提示文字位置。
    var pen = task && penReady && !!(penBox && penBox.checked);
    svg.setAttribute('data-size-mode', task ? 'task' : 'speed');
    box.setAttribute('aria-checked', task ? 'true' : 'false');
    // 开关只随任务耗时模式出现：速度模式下"低分惩罚"没有可乘的面积。
    if (penWrap) { if (task) penWrap.removeAttribute('hidden');
                   else penWrap.setAttribute('hidden', ''); }
    if (penBox) penBox.setAttribute('aria-checked', pen ? 'true' : 'false');
    nodes.forEach(function(n){
      var r = radiusOf(n, task, pen);
      put(n.g.querySelector('[data-role="node"]'), 'r', r);
      var nt = byNote[n.i];
      if (nt) put(nt.t, 'y', nt.y + (r - n.r));
    });
    chips.forEach(function(c){
      // 族卡锚在中心球左上：球半径变化多少，卡就沿同一方向退多少，仍贴着球。
      var anchor = (!task ? c.ball : (pen && c.btp !== null ? c.btp : c.bt));
      var dr = anchor[2] - c.ball[2];
      var ex = Math.min(Math.max(c.rectX - dr, plot[0] + 2),
                        Math.max(plot[0] + 2, plot[2] - 2 - c.r0[2]));
      var ey = Math.min(Math.max(c.rectY - dr, plot[1] + 2),
                        Math.max(plot[1] + 2, plot[3] - 2 - c.r0[3]));
      var dx = ex - c.rectX, dy = ey - c.rectY;
      c.ch.setAttribute('data-ball', anchor.join(' '));
      c.ch.setAttribute('data-r0', [ex, ey, c.r0[2], c.r0[3]].map(function(v){
        return (+v).toFixed(1); }).join(' '));
      put(c.rect, 'x', ex); put(c.rect, 'y', ey);
      c.texts.forEach(function(t, k){ put(t, 'x', c.tx[k][0] + dx); put(t, 'y', c.tx[k][1] + dy); });
    });
    if (cap) {
      cap.textContent = !task ? '气泡面积 = 输出速度'
        : (pen ? '气泡面积 = 任务耗时映射面积 × 低分惩罚'
               : '气泡面积 = 任务耗时（越快越大）')
          + (penReady ? '' : '（本次数据无收缩参数，按原比例）');
    }
    var tip = document.getElementById('fetch-tip');
    if (tip) tip.textContent = tip.getAttribute(task ? 'data-task-tip' : 'data-speed-tip') || '';
    if (svg.__mrRelayout) svg.__mrRelayout();
  }
  // 初值来自宿主：状态栏在 documentStart 注入 window.MR_SIZE_TASK / MR_SIZE_PENALTY。
  // 这里只恢复 UI 与几何，不回写偏好 —— 初始化不能触发保存，否则每次重载都会把
  // 刚读到的东西再写一遍，和用户真正的选择混成一个事件环。
  var wantTask = typeof window !== 'undefined'
    && (window.MR_SIZE_TASK === 1 || window.MR_SIZE_TASK === '1');
  var wantPen = typeof window !== 'undefined'
    && (window.MR_SIZE_PENALTY === 1 || window.MR_SIZE_PENALTY === '1');
  if (penBox) { penBox.checked = wantPen; penBox.disabled = !penReady; }
  box.checked = wantTask;
  apply(wantTask);
  function save(name, value){
    // 原生持久化：状态栏的 UserDefaults 是唯一真源；浏览器/测试环境没有桥就静默跳过。
    try {
      if (window.webkit && window.webkit.messageHandlers
          && window.webkit.messageHandlers[name]) {
        window.webkit.messageHandlers[name].postMessage(value ? 1 : 0);
      }
    } catch (e) {}
  }
  box.addEventListener('change', function(){
    var task = !!box.checked;
    apply(task);
    // macOS 的 WebKit 点击复选框只改值、不把键盘焦点交给它；随后按空格会落到 BODY，
    // 被 WKWebView 退回 AppKit 后触发 NSBeep。切换后显式聚焦，空格就能继续切换开关，
    // 而不是变成系统提示音（原生侧另有未消费按键兜底）。
    try { box.focus(); } catch (e) {}
    save('mrSizeTask', task);
  });
  if (penBox) penBox.addEventListener('change', function(){
    apply(!!box.checked);
    try { penBox.focus(); } catch (e) {}
    save('mrSizePenalty', !!penBox.checked);
  });
/* SIZE-END */
})();"""


CHIP_FS = 10.0            # 提示卡字号（比图例小一号：它是次要信息）
CHIP_PAD_X = 6.0
CHIP_PAD_Y = 4.0
CHIP_LH = 13.0            # 行距
CHIP_GAP = 4.0            # 卡片与气泡边缘的气口


def median(vals: list[float]) -> float:
    """偶数取中间两数平均、奇数取正中 —— 与 fetch 侧同族合并那条口径一致，不复用它是跨模块依赖。"""
    s = sorted(vals)
    k = len(s) // 2
    return s[k] if len(s) % 2 else (s[k - 1] + s[k]) / 2


def span(vals: list[float], unit: str = "", pre: str = "", cell=None, sep: str = "~") -> str:
    """区间写法：只有一档时别写成 "136.4~136.4" 那种自问自答。

    `cell` 换掉默认的 `:g`，让"给人看的位数"由调用点决定（速度走 `speed_text`）。
    判"是不是同一个数"要按**格式化之后**比：71.4 与 70.6 收位到整数都是 71，
    按原始值比会写成 "71~71" —— 自问自答那条老毛病正是被收位这一步重新引出来的。
    """
    fmt = cell or (lambda v: f'{v:g}')
    lo, hi = fmt(min(vals)), fmt(max(vals))
    return f'{pre}{lo}{unit}' if lo == hi else f'{pre}{lo}{sep}{pre}{hi}{unit}'


def speed_text(value) -> str:
    """速度读数给人看的那几位：整数，与爬虫文案同源。

    11-04 起 AA 把速度列换成"每任务分钟"，我们改读 RSC 的 `outputSpeed` —— 数值口径另说，
    显示形态先回到旧表格里那种整数（旧轮 80/80 档都是整数），不然悬停会念出
    `67.76316669225746 tok/s`。位数表只有一份，在 `fetch_model_data.DISPLAY_DECIMALS`。
    """
    from fetch_model_data import DISPLAY_DECIMALS, display_num
    return display_num(value, DISPLAY_DECIMALS["tok_s"])


def tier_of(p: dict) -> str:
    """这一档叫什么：有档位写档位（max/xhigh…），没档位写模型名。"""
    return p.get("t") or p["n"]


def tier_rank(tier: str) -> int:
    """固定推理投入顺序，忽略 with fallback 后缀；未知档位放最后。"""
    base = str(tier or "").lower().split(" with fallback")[0].strip()
    return {"non-reasoning": 0, "none": 0, "minimal": 1, "min": 1,
            "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}.get(base, 99)


def progress_num(p: dict):
    """这一档的 AA Progress 分；只认数字，null/缺失/布尔值一律算没有。

    只消费爬虫按「模型 × 推理档」精确匹配后写下的值，不按同族最高分补到别的档。
    """
    v = p.get("pi")
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def progress_label(ps: list[dict]) -> str:
    """Progress 的展示名：版本由载荷给，缺版本时不硬写成 v1。"""
    vs = sorted({"v" + str(p["pv"]).removeprefix("v") for p in ps if p.get("pv")})
    return f'历史参照{"/".join(vs)}' if vs else "历史参照"


def progress_cell(p: dict) -> str:
    """历史参照显示整数，缺分显示破折号；不改原始精度。"""
    v = progress_num(p)
    return f'{v:.0f}' if v is not None else "—"


def score_label(ps: list[dict]) -> str:
    versions = sorted({"v" + str(p["iv"]).removeprefix("v").removesuffix(".0")
                       for p in ps if p.get("iv")})
    return "智能指数" + "/".join(versions) + " / " + progress_label(ps)


def progress_vendor_line(vp: list[dict]) -> str:
    """厂商聚合：先按 series_key 取**该族最高的一条** Progress，再对族结果取中位/范围。

    直接把每档的分混在一起会被"某族有 5 个档、另一族只有 1 档"带偏；这里每条族只投一票。
    覆盖数同样按族算，标签写明这是按族最高档的 best-effort 口径。
    """
    fams: dict[str, list[dict]] = {}
    for p in vp:
        fams.setdefault(p["k"], []).append(p)
    idx = [max(p["y"] for p in g) for g in fams.values()]
    ref = [max(vals) for g in fams.values()
           if (vals := [v for v in (progress_num(p) for p in g) if v is not None])]
    hist_range = span([round(v) for v in ref]) if ref else "—"
    hist_mid = f'{median(ref):.0f}' if ref else "—"
    coverage = f'；历史覆盖 {len(ref)}/{len(fams)} 族' if len(ref) != len(fams) else ""
    return (f'{score_label(vp)}（各取族最高） {span(idx)} / {hist_range}'
            f'（中位 {median(idx):g} / {hist_mid}{coverage}）')


def speed_num(p: dict) -> str:
    """明细行的速度格：只给数字，单位写在表头。推估带 ≈、参照带给区间、没有给破折号。

    三种形状都走 `speed_text`：实测、推估、参照带必须是同一套位数，
    否则同一列里会同时出现「68」和「67.76316669225746」。
    """
    if p.get("v"):
        return speed_text(p["v"])
    if p.get("ve"):
        return f'≈{speed_text(p["ve"])}'
    if p.get("vb"):
        return span(p["vb"], sep="–", cell=speed_text)
    return "—"


def task_point(p: dict):
    """定大小用的任务耗时点：官方优先，官方缺失时用估计点；只有区间时返回 None。

    区间没有中心值，不能插值出一个数来当面积编码 —— 那等于把"别家多快"
    说成本档的速度。官方 0 是有效读数，但无法做对数面积编码，仍按缺失处理。
    """
    v = valid_task_time(p.get("tp"))
    return v if v is not None else valid_task_time(p.get("tpe"))


def task_pair(p: dict):
    """这一档的时间不确定度区间（升序二元组）；官方实测没有区间。

    估计点先取 `time_per_task_est_range`，只有速度参照带时取 `time_per_task_band`。
    两端必须是有限正数且升序，否则整条区间作废，不拿半条画环带。
    """
    for key in ("tpe_range", "tpb"):
        raw = p.get(key)
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            lo, hi = metric_number(raw[0]), metric_number(raw[1])
            if lo is not None and hi is not None and lo > 0 and hi >= lo:
                return (lo, hi)
    return None


def task_time_domain(pts: list[dict]) -> tuple[float, float] | None:
    """当前图上**正耗时**点的 [min,max]；没有有效耗时就是 None。

    只用已经过了 index/成本门槛、真的画到图上的 pts。把未上图的模型算进来，
    归一化就会跟着看不见的行漂移，同一份可见数据两次渲染可能得到不同大小。
    """
    vals = [t for p in pts if (t := task_point(p)) is not None]
    return (min(vals), max(vals)) if vals else None


def apply_task_radii(pts: list[dict], penalty=None) -> None:
    """给每个图上点补 r_task / task_missing；TPS 模式的 r 一个字节都不动。

    传了 `penalty`（载荷里的 task_size_penalty）就同时补 r_task_pen —— 收缩后
    真正要画的半径。两份都留：关掉开关时不必重算，且测试能直接比对。
    """
    domain = task_time_domain(pts)
    for p in pts:
        point = task_point(p)
        p["r_task"] = (task_size.task_radius(point, domain[0], domain[1]) if domain
                       else R_MISSING)
        p["task_missing"] = point is None
    if not isinstance(penalty, dict):
        return
    for p in pts:
        factor = task_size.size_factor(p.get("y"), penalty)
        p["task_factor"] = factor
        p["r_task_pen"] = (task_size.task_radius_penalized(
            task_point(p), domain[0], domain[1], factor) if domain else R_MISSING)


THINK_NEG_TOL_S = 1.0


def think_seconds(total_s, tok_s, first_answer_s=None):
    """由基准总响应与 500 token 生成时间反推首回答前等待。

    官方基准总响应 = 首回答等待 + 500 / tok_s，所以差值的物理含义是含输入、
    排队与思考的首回答前等待，不是纯思考时间。只用实测正速度，不能拿推估速度凑。
    11-04 及以前的轮里总响应与速度都是上游取整后的读数，两者相减最多差出 1 秒，
    所以差在 `THINK_NEG_TOL_S` 以内视为 0；差得更远说明两个口径对不上，
    宁可留空也不写一个负的或明显失真的数。（新版 `total_s` 不再发布，走下面那条兜底。）

    `total_s` 缺失时退回官方 `timeToFirstAnswerToken`（`first_answer_s`）：它就是
    首回答前等待本身，不需要再减生成时间。只有 `total_s` **完全缺失**才走这条；
    有值但无效或与速度矛盾时仍留空，不拿另一个口径掩盖对不上的数据。
    """
    total = metric_number(total_s)
    if total is None:
        # 只有**完全没给** total_s 才兜底；给了却不是有效数（NaN/字符串/布尔）
        # 说明上游有脏数据，留空而不是拿另一个口径盖住。
        if total_s is not None:
            return None
        first = metric_number(first_answer_s)
        return first if first is not None and first >= 0 else None
    tok = metric_number(tok_s)
    if total <= 0 or tok is None or tok <= 0:
        return None
    value = total - 500.0 / tok
    return None if value < -THINK_NEG_TOL_S else max(0.0, value)


def think_ratio(think_s, task_s):
    """思考时间 ÷ 任务耗时（百分数）。分子可为零，分母必须为正且两者都有限。

    这是两个不同基准的估计值之比，不是任务内部的时间分解；允许超过 100%，
    因为思考时间含输入/排队等待，任务耗时是官方解码生成耗时估计。
    """
    num = metric_number(think_s)
    den = metric_number(task_s)
    if num is None or num < 0 or den is None or den <= 0:
        return None
    value = num * 100.0 / den
    return value if math.isfinite(value) else None


def think_ratio_text(value) -> str:
    """比值的显示格：十进制 half-up 一位小数、去尾零；无效值给破折号（不带百分号）。

    用 Decimal 而不是浮点乘十，是因为 109.75 在二进制里略小于 .75，
    `floor(x*10+.5)` 会给出 109.7；看板的 toFixed(1) 给 109.8，两边必须逐字一致。
    """
    v = metric_number(value)
    if v is None or v < 0:
        return "—"
    one = Decimal(str(v)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return format(one, "f").rstrip("0").rstrip(".")


def think_ratio_value(p: dict):
    """先认点上的 think_ratio 键；旧夹具没这个键时按同一公式现算，不复制第二套口径。"""
    v = metric_number(p.get("think_ratio"))
    if v is None:
        v = metric_number(p.get("ratio"))
    return v if v is not None else think_ratio(p.get("think"), p.get("tp"))


def think_ratio_cell(p: dict) -> str:
    v = think_ratio_value(p)
    return "—" if v is None else think_ratio_text(v) + "%"


def _half_up_1(value: float) -> float:
    """非负秒数按 half-up 取一位小数。Python/JS 共用这一条，避免舍入分歧。"""
    return math.floor(value * 10.0 + 0.5) / 10.0


def time_cell(value) -> str:
    """时间格自带单位：不足 60 秒用 s，否则用 min；half-up 一位小数并去尾零。

    先对秒做 half-up，再用同一个已舍入秒值判断 60 秒门槛并换算分钟，
    所以 17.25→17.3s、59.95→1min 在两个运行时上逐字一致。
    负数、布尔、非有限数与字符串都算缺失，给破折号。只换显示，不改原始秒值。
    """
    v = metric_number(value)
    if v is None or v < 0:
        return "—"
    seconds = _half_up_1(v)
    if seconds < 60:
        return f"{seconds:.1f}".rstrip("0").rstrip(".") + "s"
    minutes = _half_up_1(seconds / 60.0)
    return f"{minutes:.1f}".rstrip("0").rstrip(".") + "min"


# 派生任务耗时的口径标签：与 fetch_model_data._EST_LABEL 逐字同步，另加
# "实测速度换算"（官方耗时缺失、但速度本身是实测的那一档）。两份必须一起改。
TASK_EST_LABEL = {"measured-speed": "实测速度换算",
                  "series-interp": "同族相邻档插值",
                  "series-anchor": "同族单侧锚点",
                  "family-anchor": "跨代次同词根中位"}


def task_cell(p: dict) -> str:
    """任务耗时展示格：官方原值 → 估计点带 ≈ → 只有区间写区间 → 破折号。

    官方 0 是有效读数，照样显示 0s；只有区间时没有中心值，绝不补一个数
    冒充测量结果 —— 那是把"别家多快"说成本档的速度。
    """
    off = metric_number(p.get("tp"))
    if off is not None and off >= 0:
        return time_cell(off)
    if (point := task_point(p)) is not None:
        return "≈" + time_cell(point)
    pair = task_pair(p)
    return span([pair[0], pair[1]], sep="–", cell=time_cell) if pair else "—"


def task_note(p: dict) -> str:
    """口径说明：估计点写依据（附换算区间），只有区间写参照区间；官方实测为空。

    文案刻意避开"（估计"与"（同级能力参照"这两种开头，免得与速度列的
    "估/带"标记判据互相误伤。
    """
    if metric_number(p.get("tp")) is not None:
        return ""
    if task_point(p) is not None:
        basis = TASK_EST_LABEL.get(p.get("tp_basis"), "估计")
        pair = task_pair(p)
        return basis + (f"，{span([pair[0], pair[1]], sep='–', cell=time_cell)}" if pair else "")
    return "同族或同级参照区间" if task_pair(p) else ""


def tip_time(label: str, value) -> str:
    return f'{label} {time_cell(value)}'


def point_tip(p: dict) -> str:
    """一个档位的人话一句：名字 · 厂商 · 分数 · 成本 · 速度 · Progress。

    `<title>`（浏览器里生效）与自绘卡片（菜单栏 popover 里生效）**共用这一份**：
    两处各写一遍，迟早说出两套数字。历史参照缺失时用破折号，不能留空 ——
    留空会被读成"这档的 Progress 是 0"或"忘了写"。

    思考时间/思考前摇取不到时整段不出现，不写破折号：上游没有这一项时，
    "—" 会被读成"这档实测为 0"。
    """
    ratio = think_ratio_value(p)
    think_bits = ""
    if metric_number(p.get("think")) is not None:
        think_bits += f' · {tip_time("思考时间", p.get("think"))}'
    if ratio is not None:
        think_bits += f' · 思考前摇 {think_ratio_text(ratio)}%'
    # 任务耗时可能是官方实测、派生估计点或只有一条区间：三种形态共用 task_cell，
    # 估计/区间再把口径写进紧随其后的括号，避免"≈"没有解释。
    note = task_note(p)
    # 成本与族卡行同一格式（`:g`）：`$4.0` 与 `$4` 是同一个数，两处写法不同会被读成两套口径。
    return (f'{p["n"]} · {p["c"]} · {score_label([p])}：{p["y"]:g} / {progress_cell(p)} · ${p["x"]:g}'
            f' · 任务耗时 {task_cell(p)}' + (f'（{note}）' if note else '')
            + f'{think_bits} · {p["tok"]}'
            + (f'（沿用 {str(p.get("carry") or "")[5:10]} 的实测值，已从榜单撤下、'
               f'评测版本未变）' if p.get("carry") else ""))


def vendor_stats(name: str, vp: list[dict], unplotted: list[dict] | None = None) -> str:
    """图例格那张范围统计：三项指标各给区间与中位，再按系列逐行给档位与最优档。

    "输出速度 实测 N 档"里只有实测值：推估/参照带不是测量结果，混进去会让"这家最慢 24 tok/s"
    这种话把估计当实测；图上本来就用虚线环把它们分开说了，卡片不能说反话。

    `unplotted` 是有指数但缺每任务成本、因此**没画到图上**的行。它们不参与梯队排序，
    只在盘点卡里点名（用户口径：排序忽略、悬停卡片提及），否则"56 分"会在别处露出却没有出处。
    """
    # 系列数按爬虫算好的 series 取（"Claude Opus 5.5"），点数按档位：
    # 名字里本来就带着档位（"… (max)"），用名字去重会得到"20 个模型 / 20 个档位"这种废话
    out = [f'{name} · {len({p["s"] for p in vp})} 个系列 / {len(vp)} 个档位']
    idx = [p["y"] for p in vp if p.get("y") is not None]
    apt = [p["x"] for p in vp if p.get("x") is not None]
    tok = [p["v"] for p in vp if p.get("v")]
    if idx:
        out.append(progress_vendor_line(vp))
    if apt:
        out.append(f'每任务成本 {span(apt, pre="$")}（中位 ${median(apt):g}）')
    if tok:
        out.append(f'输出速度 实测 {len(tok)} 档 {span(tok, " tok/s", cell=speed_text)}'
                   f'（中位 {speed_text(median(tok))}）')
    else:
        # 一家可能只有推估/参照带。这句必须出现，否则下面那行"只给范围 50~120"
        # 会被读成实测速度 —— 图上这两个形状本来就是一个实心一个空心环。
        out.append("输出速度 无实测档")
    # 时间指标按厂商给一行中位，不逐档展开（逐档已在族卡）。
    # 比值必须先逐档算再取中位；先取两个中位再相除会得到另一个数（基准不同）。
    tp = [p["tp"] for p in vp if p.get("tp") is not None]
    tpe = [v for p in vp if p.get("tp") is None and (v := task_point(p)) is not None]
    tpb = [v for p in vp if p.get("tp") is None and task_point(p) is None
           and (v := task_pair(p)) is not None]
    th = [p["think"] for p in vp if p.get("think") is not None]
    ratios = [v for p in vp if (v := think_ratio_value(p)) is not None]
    time_bits = []
    if tp:
        time_bits.append(f'任务耗时 {time_cell(median(tp))}')
    # 估计/参照区间只在这一家完全没有官方耗时时才顶上；混排会把估计说成实测。
    elif tpe:
        time_bits.append(f'任务耗时 ≈{time_cell(median(tpe))}（{len(tpe)} 档估计）')
    elif tpb:
        lo = min(v[0] for v in tpb)
        hi = max(v[1] for v in tpb)
        time_bits.append(f'任务耗时 {span([lo, hi], sep="–", cell=time_cell)}（参照区间）')
    if th:
        time_bits.append(f'思考时间 {time_cell(median(th))}')
    if ratios:
        time_bits.append(f'思考前摇 {think_ratio_text(median(ratios))}%'
                         f'（{len(ratios)}/{len(vp)} 档）')
    if time_bits:
        out.append("时间中位：" + " · ".join(time_bits))
    # 推估/参照带**不单独成行**（用户口径：这一类不进卡片）。它们仍按自己的档位出现在
    # 下面的逐档明细里，速度那格写 ≈900（推估）/ 56–106（参照带）—— 口径没丢，
    # 只是不再占两行解释。上面那行"实测 N 档"因此就是字面意思：只含实测。
    if unplotted:
        out.append("未上图：" + "、".join(
            f'{m["name"]} · {m["index"]} 分（缺每任务成本）'
            for m in sorted(unplotted, key=lambda z: -z["index"])))
    # 逐系列一行：档位选项、性能范围、最高帕累托性价比档位。
    # 不再逐档罗列 —— 每一档的详述现在在**该族那张合并弹窗**里，这里再列一遍就是两处一份数据。
    out.append("按系列：档位 · 双评分 · 最优档")
    out += [series_row(s, ps) for s, ps in sorted(by_series(vp).items(),
                                                   key=lambda kv: -max(p["y"] for p in kv[1]))]
    return "\n".join(out)


def by_series(vp: list[dict]) -> dict[str, list[dict]]:
    """按爬虫算好的 series 分组（"Claude Opus 5.5"）。悬停点亮按 series_key（同代次），
    综述按 series（可能含多个代次）—— 两个粒度各有用处，这里只保证不各算一套。"""
    out: dict[str, list[dict]] = {}
    for p in vp:
        out.setdefault(p["s"], []).append(p)
    return out


def hoist_suffix(ts: list[str]) -> tuple[list[str], str]:
    """把每个档位名**共有**的尾巴抽出来只标一次。

    "low with fallback / mid with fallback / … / max with fallback" 里那串
    " with fallback" 是整组一样的，逐档重复既啰嗦又把卡片撑爆（今天综述卡就是因为
    一行 540px 而一种形状都塞不下）。抽完必须仍然标着 —— 那是测量口径，不能丢。
    只有一档、或各档尾巴不同（真有不带 fallback 的档）时原样返回，不硬抽。
    """
    if len(ts) < 2:
        return ts, ""
    rev = os.path.commonprefix([s[::-1] for s in ts])[::-1].strip()
    if len(rev) < 4 or not all(s.endswith(rev) for s in ts):
        return ts, ""
    bare = [s[:-len(rev)].strip() for s in ts]
    if any(not b for b in bare):
        return ts, ""
    return bare, rev



def series_row(name: str, ps: list[dict]) -> str:
    """综述里的一行：系列名 · 档位选项 · 智能指数范围 · 最优档。

    最优档 = 该系列**在 Pareto 前沿上**那些档里分数最高的。**只有真在前沿上才标注**
    （10-13 用户口径）：整族被支配时那一档只是"这族里最高的"，写"（不在前沿）"会把
    要标的和不用标的拉成一样长，标注意义就没了。
    只有一个点就没有"族"可比：最优档**留空**（原先那句"单点不成族"是解释，不是数据）。
    共有尾巴（with fallback）在档位串后面标一次，最优档用抽完的短名，不重复。
    """
    order = sorted(ps, key=lambda z: (-tier_rank(z.get("t")), -z["y"], z["x"]))
    idx = [p["y"] for p in ps]
    if len(ps) == 1:
        # 没有档位标签时 tier_of 退化成模型名，与系列名是同一串 → 那一栏直接不写
        t = tier_of(ps[0]) if ps[0].get("t") else ""
        pair = f'{idx[0]:g} / {progress_cell(ps[0])}'
        return f'{name} · {t} · {pair}' if t else f'{name} · {pair}'
    bare, sfx = hoist_suffix([tier_of(p) for p in order])
    short = {id(p): b for p, b in zip(order, bare)}
    on = [p for p in ps if p.get("f")]
    pick = max(on or ps, key=lambda z: (z["y"], -z["x"]))
    tiers = "/".join(dict.fromkeys(bare)) + (f"（均 {sfx}）" if sfx else "")
    # 不写"指数 42~58"而只写数字：列名在小标题那行统一说了，每行重复一遍就把卡片撑到要折三行
    ref = [round(v) for v in (progress_num(p) for p in ps) if v is not None]
    return (f'{name} · {tiers} · {span(idx)} / {span(ref) if ref else "—"} · 最优 {short[id(pick)]}'
            + ("（前沿）" if on else "")
            + ("（沿用旧读数）" if any(pt.get("carry") for pt in ps) else ""))


def family_card_lines(ps: list[dict]) -> list[str]:
    """一族一张卡：表头是系列与档位数，下面**每一档一行详述**（数字与 <title> 同一批字段）。

    档位按固定推理顺序反序排，最高推理投入优先。共有尾巴（"with fallback"）在表头标一次，
    每档行只留档位 —— 与综述卡同一套写法，两处口径不会一个说一个不说。
    两种分数合并显示，不重复逐档明细或添加第二段汇总。
    """
    order = sorted(ps, key=lambda z: (-tier_rank(z.get("t")), -z["y"], z["x"]))
    bare, sfx = hoist_suffix([tier_of(p) for p in order])
    head = f'{ps[0]["s"]} · {len(ps)} 档' + (f'（均 {sfx}）' if sfx else "")
    # 整族都没有思考数据时，表头与每行都不留这两列：破折号会被读成实测 0。
    has_think = any(metric_number(p.get("think")) is not None for p in ps)
    has_ratio = any(think_ratio_value(p) is not None for p in ps)
    # 没有档位标签时 tier_of 退化成模型名，与表头是同一串 —— 只有一档就别重复一遍
    rows = [(("" if (not p.get("t") and len(ps) == 1) else b) +
             ("" if not b or (not p.get("t") and len(ps) == 1) else " · ") +
              f'{p["y"]:g} / {progress_cell(p)} · ${p["x"]:g}'
              f' · {task_cell(p)}'
             + (f' · {time_cell(p.get("think"))}' if has_think else "")
             + (f' · {think_ratio_cell(p)}' if has_ratio else "")
             + f' · {speed_num(p)}'
             + (f' · 沿用 {str(p.get("carry") or "")[5:10]}' if p.get("carry") else ""))
            for b, p in zip(bare, order)]
    # 列名分两行：六项指标挤一行会把卡撑到绘图区一半宽。分行的只是标题，数据仍一档一行。
    cols = "任务耗时(估)" + (" · 思考时间" if has_think else "") \
        + (" · 思考前摇" if has_ratio else "") + " · tok/s"
    return [head, score_label(ps) + " · $每任务", cols] + rows


CHIP_MAX_W = 380.0        # 一张卡最宽多少 px：六列指标（含 min 单位）+ 长档位名要放下，超过仍折行


def wrap_lines(text: str, max_w: float = CHIP_MAX_W) -> list[str]:
    """按估算字宽折行。推估/参照带的口径说明本来就长，**一个字都不删**，只换行。"""
    out, cur = [], ""
    for ch in str(text):
        if ch == "\n":
            out.append(cur); cur = ""
            continue
        if cur and text_w(cur + ch, CHIP_FS) > max_w and ch == " ":
            # 空格留在行尾：这样 "".join(行) 逐字等于原句，判据能真的验"卡片与 title 同一句"
            out.append(cur + " "); cur = ""
            continue
        # 中文串里连续汉字没有空格可断：超宽就硬换行，宁可一行短一点也不让卡片出画。
        # 但收尾标点（）】，。、）不能顶到下一行行首 —— 那会断出"…$1.58 / ）"这种半截括号，
        # 宁可这一行超出一点点也要把它留在上一行。
        if cur and text_w(cur + ch, CHIP_FS) > max_w and ord(ch) > 0x2e80:
            if ch in "）)】」』。，、；：！？" or cur[-1] in "（(【「『":
                # 收尾标点不顶到下一行行首，开口括号也不留在上一行行尾 ——
                # 两种都会把一对括号劈成两行，读起来像漏字
                cur += ch
                continue
            out.append(cur); cur = ch
            continue
        cur += ch
    if cur:
        out.append(cur)
    return out or [""]


def chip_box(cx: float, cy: float, r: float, lines: list[str]) -> dict:
    """量一张卡的尺寸（长行先折行，还没定它摆在哪）。"""
    rows = [w for ln in lines for w in wrap_lines(ln)]
    w = min(max([text_w(x, CHIP_FS) for x in rows], default=0.0) + 2 * CHIP_PAD_X, W - PL - PR - 4)
    h = len(rows) * CHIP_LH + 2 * CHIP_PAD_Y
    return {"cx": cx, "cy": cy, "r": r, "w": w, "h": h, "lines": rows}


def family_center(ps: list[dict], px, py) -> tuple[float, float, float, float, float]:
    """族的"中心点"= 按智能指数排序后**居中的那一档**（偶数取偏强的那档）。

    以前取"离像素质心最近的球"：两档的族里质心恰好在两点正中，距离并列，
    选哪颗球取决于浮点尾数与坐标四舍五入 —— 渲染侧与判据侧会各自认一颗，
    同一份数据两次渲染也可能贴到不同球上。按数据排序取中位是确定的，两侧算法一眼对得上。

    半径给三份：默认 TPS、任务耗时、任务耗时×低分收缩。三份都由**同一颗**中心球给出，
    开关切换只换锚点半径，不换卡片说的是哪一族。
    """
    order = sorted(ps, key=lambda z: (-z["y"], z["x"]))
    mid = order[(len(order) - 1) // 2]
    rt = mid.get("r_task", mid["r"])
    return (px(mid["x"]), py(mid["y"]), mid["r"], rt,
            mid.get("r_task_pen", rt))


def layout_family_card(ps: list[dict], px, py) -> dict:
    """一张族卡：贴在**中心点的左上角**（卡的右下角对着那颗球），一律这样、不按左右侧翻转。

    越界靠夹紧兜住：贴出去会出画时宁可压住自己的球，也不换到另一侧去
    —— 换侧就等于"卡片离小球太远"，读者认不出这张卡说的是哪一族。
    """
    cx, cy, rr, rrt, rrp = family_center(ps, px, py)
    box = chip_box(cx, cy, rr, family_card_lines(ps))
    w, h = box["w"], box["h"]
    x = min(cx - rr - CHIP_GAP - w, W - PR - 2 - w)
    y = min(cy - rr - CHIP_GAP - h, H - PB - 2 - h)
    return {"x": max(PL + 2, x), "y": max(PT + 2, y), "w": w, "h": h,
            "lines": box["lines"], "anchor": "br",
            "ball": (cx, cy, rr),      # 中心球：JS 绕它试翻转与偏移，不是随便平移
            "ball_task": (cx, cy, rrt),   # 同一中心在任务耗时模式下的半径
            "ball_task_pen": (cx, cy, rrp)}  # 再叠加全局低分收缩的半径


def chip_svg(box: dict, cls: str, keys: dict | None = None) -> str:
    """静态画一张卡：默认不显，带 .hi 才显。

    卡片是**与 .mk 平级的兄弟**、自己带 data-k/data-c/data-pri。放在 .mk 子树里会被
    高亮那步 scale(1.45) 一起放大成 14.5px 的字；搬出来之后搬运与恢复仍走同一套
    （home 记的就是父节点 + 下标），字号始终是量出来的那一份。
    """
    attr = "".join(f' data-{k}="{html.escape(str(v), quote=True)}"' for k, v in (keys or {}).items())
    # data-anchor 给 JS 塌缩时用：塌缩只能朝"卡片伸出去的那一侧"收，
    # 否则卡片会滑离自己的小球，读者认不出这张卡说的是哪个档
    # data-r0：作者侧的原始框。塌缩会改写 rect 的 x/y/w/h，没有这份原始值，
    # 第二次悬停就拿"已塌缩的框"当基准再塌一次（实测每悬停一次往右下漂十几 px）
    out = [f'<g class="chip {cls}" data-anchor="{box.get("anchor", "tl")}" '
           f'data-r0="{box["x"]:.1f} {box["y"]:.1f} {box["w"]:.1f} {box["h"]:.1f}"{attr}>',
           f'<rect x="{box["x"]:.1f}" y="{box["y"]:.1f}" width="{box["w"]:.1f}" '
           f'height="{box["h"]:.1f}" rx="6"/>']
    for i, t in enumerate(box["lines"]):
        out.append(f'<text x="{box["x"] + CHIP_PAD_X:.1f}" '
                   f'y="{box["y"] + CHIP_PAD_Y + CHIP_FS + i * CHIP_LH:.1f}" '
                   f'font-size="{CHIP_FS:g}">{html.escape(t, quote=True)}</text>')
    out.append("</g>")
    return "".join(out)


PANEL_ROWS_MAX = 24      # 一栏最多几行的起点（装不下就减行数、加栏数）
PANEL_H_MAX = 300.0      # 聚合卡最高：绘图区高 382，留 80 给别的点与节点卡（高度受绘图区限制，实际由装箱算）（20 档一栏就 300px 高，整块挡到看不清别家的点）
PANEL_LH = 12.5           # 明细行距比节点卡紧一点：这里行数多
PANEL_W_MAX = W - PL - PR - 4    # 整块面板最宽 = 绘图区宽（602）：以前钉 300，


PANEL_COL_W = 240.0      # 一栏最宽：超过就折行（"系列名 + 档位串 + 区间 + 最优档"一行能到 370px）


def panel_shapes(lines: list[str]) -> list[tuple[int, list[list[str]], list[float]]]:
    """枚举可摆的形状：每栏 per 行、切成若干栏，返回 (per, 各栏, 各栏宽)。

    先按栏宽把长行折成多行（折行只换行不删字，见 wrap_lines），再装箱 ——
    不折行的话一行 370px，怎么分栏都超可用宽，老写法会一路降到 4 行/栏然后
    被 min() 悄悄裁掉一半文字。栏数与落点都是**算出来的**，不是钉死的常数。
    """
    rows = [w for ln in lines for w in wrap_lines(ln, PANEL_COL_W - 2 * CHIP_PAD_X)]
    out = []
    for per in range(len(rows), 3, -1):
        cols = [rows[i:i + per] for i in range(0, len(rows), per)]
        widths = [max([text_w(v, CHIP_FS) for v in c], default=0.0) + 2 * CHIP_PAD_X for c in cols]
        out.append((per, cols, widths))
    return out


def hit(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[0] + a[2] <= b[0] or b[0] + b[2] <= a[0]
                or a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1])


def fit_count(panel: tuple[float, float, float, float],
              cards: list[tuple[float, float, float, float]]) -> int:
    """数一数这块落点上能贴下几张节点卡 —— 与共享 JS 同一个贪心：按 pri 顺序，压住就跳过。

    必须与 JS 的判据同构，否则"选出来的最优形状"与实际贴出来的张数是两回事。
    """
    taken, got = [panel], 0
    for c in cards:
        if not any(hit(c, q) for q in taken):
            taken.append(c)
            got += 1
    return got


def panel_svg(lines: list[str], cls: str, keys: dict,
              avoid: list[tuple[float, float, float, float]] | None = None,
              dots: list[tuple[float, float, float]] | None = None) -> str:
    """图例的聚合卡：按行高装箱分栏，栏宽取本栏最长行。

    为什么钉在左上而不是贴着图例格：明细最多能到几十行，贴着格子放要么压住图例自己
    那一行，要么被画布右边切掉。钉一个确定位置，读者每次都在同一个地方看到它。
    """
    # 每栏行数从"全放一栏"往下试，直到整块宽度装得下：栏数是内容的函数，不是拍死的常数。
    # 上限钉在绘图区宽而不是 300 —— 20 档那种厂商，300 宽的约束根本无解，
    # 老写法会一路降到 4 行/栏、排成 7 栏然后被 min() 悄悄裁掉一半文字。
    # 每栏行数往下试，第一个"宽度装得下、高度也没吃掉半张图"的方案就采用。
    # 高度这条约束是必须的：聚合卡贴着左上，一栏 24 行能盖到 y≈334，于是所有节点卡都被
    # 判成"压住了"不贴 —— 读者只剩一张大卡，反而看不到贴在各档上的提示。
    # 从一栏起往上试：第一个"宽不越可用宽、高不越可用高"的栏数就用。
    # 关键是 rect 的宽 = 各栏宽之和，不能再被 min() 截一次 —— 截了之后文字照旧排到
    # 更右边，出来一张"字在卡外面"的破卡（今天 Anthropic 那张溢到卡右 483px）。
    shapes = panel_shapes(lines)
    # 形状与落点一起选：目标不是"最好看"，而是**贴得上的节点卡最多**。
    # 以前这里钉死"最高 150/起始 24 行"两个常数，结果一换数据就失衡
    # （今天 OpenAI 的卡排成 492×296，把别家全挡掉，只剩 1 张贴得上）。
    best = None
    for per, cols, widths in shapes:
        w, h = sum(widths), per * PANEL_LH + 2 * CHIP_PAD_Y
        if w > PANEL_W_MAX or h > H - PB - PT - 4:
            continue
        # 用户口径：综述卡尽量放在图的**左下或右下**。两角里取压到点少的那一个 ——
        # 压住点等于把数据藏起来，比压住空白严重得多
        for x, y in [(PL + 2, H - PB - 2 - h), (W - PR - 2 - w, H - PB - 2 - h)]:
            # got = 这个落点下按作者位能贴上的族卡张数，只用来在左右两角之间择优。
            # 不再写进产物：以前 data-fit 是给判据当"JS 至少能贴上这么多"的下界，
            # 但 JS 现在会绕球换角、还会取"压得最少"的候选位，两边不同构，比不出来（实测 1 < 2）。
            got = fit_count((x, y, w, h), list(avoid or []))
            cover = sum(1 for cx, cy, r in (dots or []) if hit((x, y, w, h), (cx - r, cy - r, 2 * r, 2 * r)))
            score = (-cover, got)        # 压住的点越少越好，其次贴上的族卡越多越好
            if best is None or score > best[0]:
                # 必须把 cols/widths 一起留下：只留宽高的话，下面排文字用的就是
                # "最后一次尝试"的栏宽，字会排到卡外面（判据「栏宽之和 = 卡宽」抓到过）
                best = (score, x, y, w, h, cols, widths)
    if best is None:
        # 一种形状都放不下时**不能停**：一停 chart.html 就停在旧版，爬虫与组件每次重画都失败
        # （这是 build_svg 那条"空数据也不许崩"的同一条理由）。退而取"最窄的那版"，
        # 越界由夹紧处理，判据「栏宽之和 = 卡宽」与「都在界内」会把它报出来。
        per, cols, widths = shapes[-1]
        w, h = sum(widths), per * PANEL_LH + 2 * CHIP_PAD_Y
        best = (0, min(max(PL + 2, 0), W - PR - 2 - w), min(max(PT + 2, 0), H - PB - 2 - h), w, h, cols, widths)
    (_cover, _got), x, y, w, h, cols, widths = best

    attr = "".join(f' data-{k}="{html.escape(str(v), quote=True)}"' for k, v in keys.items())
    # data-cols：各栏宽度。判据要能当场抓住"rect 被 min() 截窄、文字仍排在截断之外"
    # 那类形状（2026-10-13 实测有一张卡的文字跑到卡右边 483px 外）—— 光看 rect 在不在界内看不出来。
    out = [f'<g class="chip {cls}" data-anchor="tl" '
           f'data-r0="{x:.1f} {y:.1f} {w:.1f} {h:.1f}" '
           f'data-cols="{",".join(f"{v:.1f}" for v in widths)}"{attr}>',
           f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="6"/>']
    cx = x
    for c, cw in zip(cols, widths):
        for i, ln in enumerate(c):
            out.append(f'<text x="{cx + CHIP_PAD_X:.1f}" y="{y + CHIP_PAD_Y + CHIP_FS + i * PANEL_LH:.1f}" '
                       f'font-size="{CHIP_FS:g}">{html.escape(ln, quote=True)}</text>')
        cx += cw
    out.append("</g>")
    return "".join(out)



def legend_rows(pts: list[dict], order: list[str]) -> list[tuple[str, str]]:
    """图上真正出现过的厂商，全部、不截断：[(颜色, 厂商名)]，顺序照爬虫算好的 `order`。

    以前这里钉死一张 8 家的表：新厂商（Google / Meta / SpaceXAI / StepFun）明明在图上，
    图例却没有；而且表里的颜色是手写的，跟爬虫派生出来的厂商色是两套，会互相打脸。

    排序判据（梯队 → 指数×log1p(平衡用量) 加权的离前沿距离）不在这里算：那份逻辑要在 fetch 侧算一次，
    状态栏与看板共用同一个名次，两边各算迟早说出两套话。这里只按 order 取色、取名字。
    图上有但 order 里没有的厂商（快照不一致）排在最后按名字，不让它静默消失。
    """
    agg: dict[str, dict] = {}
    for p in pts:
        name = (p["c"] or "其他").strip() or "其他"
        a = agg.setdefault(name, {"col": p["col"], "rank": len(order) + 1})
        if p["col"] and not str(a["col"] or "").strip():
            a["col"] = p["col"]
        try:
            a["rank"] = min(a["rank"], order.index(name))
        except ValueError:
            pass
    ranked = sorted(agg.items(), key=lambda kv: (kv[1]["rank"], kv[0]))
    return [(v["col"], k) for k, v in ranked]


def legend_items(rows: list[tuple[str, str]], lay: dict) -> tuple[list[tuple[str, str]], int]:
    """按**已量好的版面**切图例：装得下的画出来，装不下的报个数。

    切分用的 `cap` 与绘制用的 `pitch/cols` 必须来自同一份 `legend_layout(全量名字)`。
    以前两边各自调一次 `legend_layout`、各自取名字列表：一旦省略真的发生，绘制侧看到的
    名字变短 → 格子变窄 → 与算 `cap` 时那份布局就不是同一个网格了。
    """
    cap = lay["cap"]
    return rows[:cap], max(0, len(rows) - cap)


def legend_counts(cols: list[int], n: int) -> list[int]:
    """把 n 个条目均分到最少几行里，任何一行不超过自己的格数上限。

    只按上限顺序填会留下 "4/5/5/2" 这种尾巴：最后一行只剩两格，右半幅又空出来。
    行数先取最少，再在行间均分；超上限的部分顺延给还有空位的行。
    """
    used = min(n, sum(cols))
    if not used:
        return [0] * len(cols)
    k = next(i for i in range(1, len(cols) + 1) if sum(cols[:i]) >= used)
    counts = [used // k + (1 if i < used % k else 0) for i in range(k)]
    while True:
        over = sum(max(0, c - cap) for c, cap in zip(counts, cols[:k]))
        if not over:
            break
        counts = [min(c, cap) for c, cap in zip(counts, cols[:k])]
        for i in range(k):
            if not over:
                break
            add = min(cols[i] - counts[i], over)
            counts[i] += add
            over -= add
    return counts + [0] * (len(cols) - k)


# 图上直接标名字的点（其余靠 hover tooltip）
NOTES = {
    "Claude Opus 5.5 (max with fallback)": "Opus 5.5 max",
    "MiMo-V2.6-Pro": "MiMo Pro",
    "GPT-6 Luna (low)": "Luna low",
    "DeepSeek V4.1 Flash (max)": "DS Flash",
}


def load_points() -> tuple[list[dict], list[dict], list[str]]:
    D = json.loads(LATEST.read_text())
    ms = [m for m in D.get("models", []) if m.get("index") is not None and m.get("apt_usd")]
    # 前沿成员 / 梯队 / 图例顺序都由爬虫算（fetch_model_data.annotate_efficiency）。
    # 少了这些字段就是"拿旧快照渲染新代码"：静默画出一条没有金线的图太难查，直接停。
    if ms and not any("on_frontier" in m for m in ms):
        sys.exit(f"{LATEST} 里缺 on_frontier/cap_band 字段：先跑 fetch_model_data.py 再渲染")
    if ms and not D.get("legend"):
        sys.exit(f"{LATEST} 里缺 legend 列表：先跑 fetch_model_data.py 再渲染")
    order = [r["creator"] for r in D.get("legend") or []]
    # 规模上限要把推估值与参照带上界算进去：否则一个同族插值出来的高值会把真实点全压扁。
    tmax = max([(m.get("tok_s") or 0) for m in ms]
               + [m["tok_s_est"] for m in ms if m.get("tok_s_est")]
               + [m["tok_s_band"][1] for m in ms if m.get("tok_s_band")] or [0]) or 1
    # 占位尺寸跟着"最慢的真实点"算，所以先扫一遍已知 tok 的半径
    r_node = node_radius([radius(m["tok_s"], tmax) for m in ms if m.get("tok_s")])

    pts = []
    for m in ms:
        tok = m.get("tok_s")
        est = m.get("tok_s_est")
        band = m.get("tok_s_band")
        # 参照带是"前三档全落空"时的兜底：它只有一条区间，**没有中心值**，所以定半径时
        # 用带的上界（画成空心环 + 两条环带），绝不当成一个测量结果。
        rng = m.get("tok_s_est_range") or band or None
        shown = tok if tok else est
        pts.append({
            "x": m["apt_usd"], "y": m["index"],
            "r": radius(shown, tmax, r_node) if shown else r_node,
            "n": m["name"], "c": m.get("creator", ""),
            # 属性值里的厂商名要转义（名字里出现 & 或引号时不能把标记撕开）
            "c_esc": html.escape(str(m.get("creator", "")), quote=True),
            # 能力线的分组键：爬虫按"同族同代次"算好（split_series），这里只消费。
            # 旧快照没有这两个字段时退化成"每个模型自己一条"，即不画线，不画错线。
            "k": m.get("series_key") or m["name"],
            "s": m.get("series") or m["name"],
            "t": m.get("tier") or "",
            # 沿用回来的行（被撤下但没被全面优于）必须显式说它是旧读数、旧到哪天
            "carry": str(m.get("carried_asof") or "")[:10],
            "col": m.get("brand_color") or "#64748B",
            # 前沿成员由爬虫判一次（含"推估点/参照带不参与"那条规则），这里只消费
            "f": bool(m.get("on_frontier")),
            # null 必须显示成「无数据」而不是 0，否则看着像真的测出 0 tok/s；
            # 有同族推估时也要说清是估的，别让"环的大小"冒充实测值。
            "tok": ((f"{speed_text(tok)} tok/s" + (f"（沿用 {m['tok_s_asof']}）" if m.get("tok_s_asof") else "")) if tok
                    else (f"≈{speed_text(est)} tok/s（{m.get('tok_s_est_note', '估计')}）" if est
                          else (f"{span(band, ' tok/s', cell=speed_text, sep='–')}（{m.get('tok_s_band_note', '同级参照带')}）"
                                if band else "无数据"))),
            "est": bool(est) and not tok,
            "band": bool(band) and not tok and not est,
            # 统计要**数字**，不能回头解析上面那句给人看的：那句里混着 "≈"、"–"、
            # "（同级参照带）"，改写一次文案就把中位数算错
            "v": tok if tok else None,
            "ve": est if (est and not tok) else None,
            "vb": (band[0], band[1]) if (band and not tok and not est) else None,
            # AA Progress：爬虫按「模型 × 推理档」精确匹配后写入，渲染面只按行取值。
            # null 就是没匹配到，绝不拿同族最高分补到其它档。
            "pi": (m.get("progress_index") if isinstance(m.get("progress_index"), (int, float))
                   and not isinstance(m.get("progress_index"), bool) else None),
            "pv": (str(m.get("progress_version")) if m.get("progress_version") else None),
            "iv": m.get("evaluation_version"),
            # 展示派生，不写回 payload：任务耗时取官方估计；思考时间是基准总响应
            # 减去 500 token 的生成时间，得到的是含输入/排队的首回答前等待。
            # 只有 total_s 完全缺失时才用官方首回答延迟 first_answer_s 兜底。
            "tp": metric_number(m.get("time_per_task_s")),
            # 官方耗时缺失时的派生值：点估计、反比换算区间、只有速度参照带时的区间。
            # 三者都只做展示，不写回 payload，不参与历史比较。
            "tpe": metric_number(m.get("time_per_task_est_s")),
            "tpe_range": m.get("time_per_task_est_range"),
            "tpb": m.get("time_per_task_band"),
            "tp_basis": m.get("time_per_task_est_basis"),
            "think": (think := think_seconds(
                m.get("total_s"), m.get("tok_s"), m.get("first_answer_s"))),
            "think_ratio": think_ratio(think, m.get("time_per_task_s")),
            # 区间在半径上（tok 是面积编码，不是纵轴），所以渲染成同心环带。
            "r_lo": radius(rng[0], tmax, r_node) if rng else None,
            "r_hi": radius(rng[1], tmax, r_node) if rng else None,
        })
    # 前沿直接取爬虫的结论（pts 里带 on_frontier），这里不再自己扫一遍支配关系 ——
    # 同一条判据原先 Python / JS 各写一遍，改一处就得祈祷另一处别忘了跟上。
    front: list[dict] = sorted((p for p in pts if p["f"]), key=lambda z: (z["x"], -z["y"]))
    return pts, front, order


def build_svg(pts: list[dict], front: list[dict], order: list[str],
              unplotted: dict[str, list[dict]] | None = None,
              penalty: dict | None = None) -> str:
    if not pts:
        # 空数据是合法状态（AA 全挂且无快照时 latest.json 的 models 可以为 []）。
        # 宁可画一张写明原因的空图，也不能在下面的 min()/max() 上崩 —— 一崩，
        # chart.html 就停在旧版，爬虫和组件每次重画都失败。
        return ('<svg viewBox="0 0 480 140" width="100%" xmlns="http://www.w3.org/2000/svg" role="img">'
                '<text x="240" y="68" text-anchor="middle" font-size="14" fill="#8b95a5">'
                '没有可画的点：payload 里没有带智能指数与成本的模型</text></svg>')
    # 任务耗时模式的半径依赖**当前图上**的正耗时范围，必须在画点前一次算好：
    # 默认 TPS 半径 r 保持原样，只新增 r_task/task_missing 两个展示字段。
    apply_task_radii(pts, penalty)
    x0, x1 = math.log10(min(p["x"] for p in pts)), math.log10(max(p["x"] for p in pts))
    y0, y1 = min(p["y"] for p in pts), max(p["y"] for p in pts)
    # 单点 / 全部点同成本时 span 为 0，px/py 的除法会 ZeroDivisionError：先撑开一度。
    if x1 - x0 == 0:
        x0, x1 = x0 - 0.5, x1 + 0.5
    if y1 - y0 == 0:
        y0, y1 = y0 - 2, y1 + 2
    lx0, lx1 = x0 - (x1 - x0) * 0.06, x1 + (x1 - x0) * 0.06   # 两侧留 6% 免得气泡贴边被切
    ly0, ly1 = y0 - (y1 - y0) * 0.06, y1 + (y1 - y0) * 0.06

    px = lambda v: PL + (math.log10(v) - lx0) / (lx1 - lx0) * (W - PL - PR)
    py = lambda v: H - PB - (v - ly0) / (ly1 - ly0) * (H - PT - PB)

    o = [
        # data-plot：绘图区四边。共享 JS 做"偏移/翻转找空位"时要按它夹紧；
        # 把 54/24/660/406 抄进 JS 就是第二份几何常数 —— 画布一改两边就分叉
        # data-task-pen：载荷里是否有可用的收缩参数（1/0）。JS 据此禁用开关并说明，
        # 而不是让一个点了没反应的复选框假装已收缩。
        f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg" role="img" '
        f'data-plot="{PL} {PT} {W - PR} {H - PB}" '
        f'data-task-pen="{1 if task_size.penalty_ready(penalty) else 0}">',
        "<title>速度与成本性价比定位图</title>",
        "<desc>横轴为每任务成本（对数），纵轴为智能指数，气泡面积默认表示输出速度（可切换为任务耗时），"
        "同色细折线是同族同代次按推理档位固定顺序串起来的能力线"
        "（non-reasoning / minimal / low / medium / high / xhigh / max），黄色虚线为 Pareto 前沿。"
        "虚线描边且外套一圈同心环带的点是同族推估值（不是实测），环带宽度即参照实测散布；"
        "中心只给一层淡色填充、外套环带的是同级能力参照带：它只给出速度范围、不给中心值，"
        "是同族推估也算不出来时的兜底。这两类点都不参与 Pareto 前沿的计算。</desc>",
    ]

    for t in (20, 30, 40, 50, 60):
        y = py(t)
        o.append(f'<line x1="{PL}" y1="{y:.1f}" x2="{W-PR}" y2="{y:.1f}" stroke="{GRID}"/>')
        o.append(f'<text x="{PL-8}" y="{y+3.5:.1f}" text-anchor="end" font-size="11" fill="{DIM}">{t}</text>')
    for t in (0.001, 0.01, 0.1, 1, 10):
        x = px(t)
        # 只画数据范围附近的刻度：$10 虽然没越界，但数据最贵只到 $7.63，
        # 那个刻度下没有任何点，留着会让人以为那边有模型。
        # 636 这个数要跟着坐标轴缩放重算：阈值必须小于 px(10)=647.2 才挡得住。
        # 算错一点点（比如写成 648）就会静默漏过去，图表上凭空多个空刻度。
        if x < PL + 2 or x > 636:
            continue
        o.append(f'<line x1="{x:.1f}" y1="{PT}" x2="{x:.1f}" y2="{H-PB}" stroke="{GRID}"/>')
        o.append(f'<text x="{x:.1f}" y="{H-PB+16}" text-anchor="middle" font-size="11" '
                 f'fill="{DIM}">${t:g}</text>')
    o.append(f'<line x1="{PL}" y1="{H-PB}" x2="{W-PR}" y2="{H-PB}" stroke="{AXIS}"/>')
    o.append(f'<line x1="{PL}" y1="{PT}" x2="{PL}" y2="{H-PB}" stroke="{AXIS}"/>')

    # 按推理投入连线，保留加大推理后分数反而下降的真实折返。
    # 画在气泡**下面**且不描虚线，避免和 Pareto 前沿（唯一的虚线）混起来。
    by_s: dict[str, list[dict]] = {}
    for p in pts:
        by_s.setdefault(p["k"], []).append(p)
    for k in sorted(by_s):
        g = by_s[k]
        if len(g) < 2:
            continue
        g.sort(key=lambda z: (tier_rank(z.get("t")), z["y"], z["x"]))
        sp = " ".join(f"{px(z['x']):.1f},{py(z['y']):.1f}" for z in g)
        o.append(f'<polyline class="ln" data-k="{html.escape(g[0]["k"], quote=True)}" data-c="{g[0]["c_esc"]}" points="{sp}" fill="none" '
                 f'stroke="{g[0]["col"]}" stroke-width="1.1" stroke-opacity=".5" stroke-linejoin="round"/>')

    fp = " ".join(f"{px(p['x']):.1f},{py(p['y']):.1f}" for p in front)
    o.append(f'<polyline points="{fp}" fill="none" stroke="{FRONT}" stroke-width="1.6" '
             f'stroke-dasharray="5 4" stroke-opacity=".85" stroke-linejoin="round"/>')

    # 提示卡的位置要在画点之前就定好：卡片是**静态标记**，JS 只切 .hi
    by_v: dict[str, list[dict]] = {}
    for p in pts:
        by_v.setdefault(p["c"], []).append(p)
    by_k: dict[str, list[dict]] = {}
    for p in pts:
        by_k.setdefault(p["k"], []).append(p)

    for i, p in enumerate(pts):
        # 每个点包一层 <g class="mk" data-k>：悬停高亮要按族整组抬起来，
        # 而 SVG 的层序就是文档序 —— 光靠 CSS 抬不动被压住的点，必须能整组搬。
        # 推估点/参照带各带自己的标记（判据靠这些属性数点，别靠肉眼看虚线）。
        band = p.get("band")
        est_attr = ' data-est="1"' if p["est"] else (' data-band="1"' if band else '')
        cls = ("mk band" if band else ("mk est" if p["est"] else "mk")) + \
              (" carr" if p.get("carry") else "")
        dash = ' stroke-dasharray="3 2.5"' if (p["est"] or band) else ""
        # 参照带的中心保留**淡色填充**（比推估点再淡一档），不能空到比"没测到速度"的占位环
        # 还不显眼：它在图上的位置是实测的成本与分数，只有速度是范围。"这里没有中心值"
        # 由外套的两条环带 + tooltip 的 56–111 说，不靠把点掏空说。
        fo = ".28" if band else (".45" if p["est"] else ".82")
        # 切模式的契约：data-r 是默认 TPS 半径，data-r-task 是任务耗时半径，
        # data-task-missing 表示没有有效正耗时（任务模式画 2px 空心环，不拿 TPS 兜底）。
        # data-cx/cy 给看板侧按同一几何缩放 logo；data-i 让提示文字按点对齐。
        missing_attr = ' data-task-missing="1"' if p.get("task_missing") else ""
        # 第三份半径只在载荷带收缩块时出现：缺字段（旧快照）就让属性缺失，
        # JS 落到 r_task，绝不用 1.0 假装"已收缩"。
        pen_attr = (f' data-r-task-pen="{p["r_task_pen"]:.1f}"'
                    if p.get("r_task_pen") is not None else "")
        tp_attr = "" if p.get("tp") is None else f"{p['tp']:g}"
        # 派生值各有自己的属性：data-tp 永远是官方原值，估计点/区间不覆盖它，
        # 判据才能分开数"实测几档、估计几档、只有区间几档"。
        tp_est_attr = "" if (p.get("tp") is not None or task_point(p) is None) \
            else f' data-tp-est="{task_point(p):g}"'
        pair = task_pair(p)
        tp_band_attr = f' data-tp-band="{pair[0]:g} {pair[1]:g}"' if pair else ""
        g = [f'<g class="{cls}" data-k="{html.escape(p["k"], quote=True)}" '
             f'data-c="{p["c_esc"]}" data-i="{i}" data-n="{html.escape(p["n"], quote=True)}" '
             f'data-cx="{px(p["x"]):.1f}" data-cy="{py(p["y"]):.1f}" '
             f'data-r="{p["r"]:.1f}" data-r-task="{p["r_task"]:.1f}"{pen_attr}'
             f' data-tp="{tp_attr}"{tp_est_attr}{tp_band_attr}{missing_attr}{est_attr}'
             + (f' data-carry="{html.escape(str(p["carry"]), quote=True)}"' if p.get("carry") else "")
             + '>']
        if (p["est"] or band) and p["r_hi"] is not None:
            # 区间画成同心环带：tok 是**面积**编码，横向位置/纵向位置都不含不确定度，
            # 只有半径有。环带宽 = 参照集合的实测散布，一眼能看出这个估计有多粗。
            g.append(f'<circle data-band-ring="1" cx="{px(p["x"]):.1f}" cy="{py(p["y"]):.1f}" r="{p["r_hi"]}" '
                     f'fill="none" stroke="{p["col"]}" stroke-opacity=".38" stroke-width="1"/>')
            if p["r_lo"] is not None and p["r_lo"] != p["r_hi"]:
                g.append(f'<circle data-band-ring="1" cx="{px(p["x"]):.1f}" cy="{py(p["y"]):.1f}" r="{p["r_lo"]}" '
                         f'fill="none" stroke="{p["col"]}" stroke-opacity=".24" stroke-width="1"/>')
        g.append(f'<circle data-role="node" cx="{px(p["x"]):.1f}" cy="{py(p["y"]):.1f}" r="{p["r"]}" '
                 f'data-fill="{p["col"]}" fill="{p["col"]}" '
                 f'fill-opacity="{fo}" stroke="{p["col"]}"{dash}>'
                 f'<title>{html.escape(point_tip(p), quote=True)}</title></circle></g>')
        o.append("".join(g))

    # 一族一张合并弹窗，与 .mk 平级、带 data-k（族悬停）+ data-c（厂商悬停）。
    # .chip 必须 pointer-events:none —— 卡片若吃指针，事件的 target 就变成卡片自己，
    # closest('[data-k]') 落空走延迟收起，又回到 2026-09-29 那种"自己触发自己"的抖动。
    fams = []
    for k, ps in by_k.items():
        box = layout_family_card(ps, px, py)
        fams.append((ps[0]["c"], -max(p["y"] for p in ps), k, box, ps))
    fam_by_vendor: dict[str, list[dict]] = {}
    for i, (v, _ny, k, box, ps) in enumerate(sorted(fams, key=lambda z: (z[0], z[1], z[2]))):
        # pri 在同一厂商内排（1 = 该厂最强那族）：悬停图例时综述卡先占位，
        # 再按这个顺序贴族卡，压住已贴的就跳过 —— 与共享 JS 的贪心同一个顺序才数得对
        box["pri"] = len(fam_by_vendor.setdefault(v, [])) + 1
        fam_by_vendor[v].append(box)
        box["keys"] = {"k": k, "c": v, "pri": box["pri"],
                       "ball": '%.1f %.1f %.1f' % box["ball"],
                       "ball-task": '%.1f %.1f %.1f' % box["ball_task"],
                       "ball-task-pen": '%.1f %.1f %.1f' % box["ball_task_pen"]}
    for v in sorted(fam_by_vendor):
        for box in fam_by_vendor[v]:
            o.append(chip_svg(box, "fam", box["keys"]))

    for i, p in enumerate(pts):
        if p["n"] in NOTES:
            o.append(f'<text data-note-for="{i}" x="{px(p["x"]):.1f}" '
                     f'y="{py(p["y"])+p["r"]+12:.1f}" text-anchor="middle" '
                     f'font-size="11" fill="{INK}" fill-opacity=".9">{NOTES[p["n"]]}</text>')

    o.append(f'<text x="{PL}" y="{PT-8}" font-size="11" fill="{DIM}">智能指数 ↑</text>')
    o.append(f'<text x="{W-PR}" y="{H-10}" text-anchor="end" font-size="11" fill="{DIM}">每任务成本 →（对数）</text>')
    o.append(f'<text x="{W-PR}" y="{PT+10}" text-anchor="end" font-size="11" fill="{FRONT}">Pareto 前沿</text>')

    rows = legend_rows(pts, order)
    # 三种大小模式里取**最大**半径让位：任务耗时与低分收缩的半径由 SIZE_JS 在运行时换上，
    # 图例是静态的、切换时不重排；按 TPS 半径算就会在切过去之后被大球压住。
    obstacles = [(px(p["x"]), py(p["y"]),
                  max(p.get("r") or 0, p.get("r_task") or 0, p.get("r_task_pen") or 0))
                 for p in pts]
    lay = legend_layout([n for _c, n in rows], obstacles)
    items, extra = legend_items(rows, lay)
    pitch, cols = lay["pitch"], lay["cols"]
    counts = legend_counts(cols, len(items))

    def cell(i: int) -> tuple[float, float]:
        row = 0
        while row + 1 < len(counts) and i >= sum(counts[:row + 1]):
            row += 1
        col = i - sum(counts[:row])
        return PL + 2 + col * pitch, PT + 6 + row * LEGEND_ROW_H

    by_c: dict[str, list[dict]] = {}
    for p in pts:
        by_c.setdefault(p["c"], []).append(p)
    panels: list[str] = []
    for i, (c, n) in enumerate(items):
        x, y = cell(i)
        body = vendor_stats(n, by_c[n], (unplotted or {}).get(n)).split("\n")
        # pri=0：综述卡先占位，族卡压在它上面就不贴（每档详述在族卡里，卡不贴也看得见
        # 的是综述那几行 —— 所以顺序按"最强族先"，弱族被挤掉时损失最小）。
        panel = panel_svg(body, "lg", {"c": n, "pri": 0},
                          [(b["x"], b["y"], b["w"], b["h"]) for b in fam_by_vendor.get(n, [])],
                          dots=[(px(p["x"]), py(p["y"]), p["r"]) for p in pts])
        # 一格包成 <g class="lgc" data-c>：共享 JS 按 data-c 把整厂的点与这张聚合卡一起抬起来。
        # 卡本体是平级兄弟（见下面 panels），不塞在格子里 —— 塞进去会被格子的缩放带偏，
        # 而且"谁该显"由 pri 现场判定，兄弟关系才好在同一个顶层里排序。
        o.append(f'<g class="lgc" data-c="{html.escape(n, quote=True)}">'
                 f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{c}" fill-opacity=".9"/>'
                 f'<text x="{x + LEGEND_DOT_TEXT_GAP:.1f}" y="{y + 3.5:.1f}" '
                 f'font-size="11" fill="{DIM}">{html.escape(n, quote=True)}</text></g>')
        panels.append(panel)
    for pn in panels:
        o.append(pn)

    if extra:
        # 只有多到连"占绘图区四成"都装不下时才省略，并如实说明省了几家
        x, y = cell(len(items))
        o.append(f'<text x="{x:.1f}" y="{y + 3.5:.1f}" font-size="11" fill="{DIM}">+{extra} 家</text>')

    o.append("</svg>")
    return "\n".join(o)


def board_io(changes: list[dict]) -> dict:
    """榜单进出：新进榜与落榜**成对**给出（只读 `changes` 的 type，不另起一份数据）。

    只报"新上了谁"不报"下去了谁"，读者就会以为榜单只在往上长 —— 这两个是同一件事的两面。
    它们不进 `notable_moves`（那里要"有基数的幅度"，且每天常规换榜会把**通知**刷成噪音），
    但浮层是拉取式的、不吵，就该在这里说全。

    `carried` 单列：沿用行是 AA 本轮没有、按台账旧读数带回来的行，既不是新进榜也不是落榜。
    混进 `new` 会让浮层说"新进榜 9 个"，而图上那 9 个点正标着「沿用 MM-DD」。
    """
    def names(kind: str) -> list:
        return sorted({str(c.get("name")) for c in changes or []
                       if c.get("type") == kind and c.get("name")})
    return {"new": names("new"), "gone": names("gone"), "carried": names("carried")}


def foot_tip(D: dict, metric: str | None = None) -> str:
    """脚注"抓取于 …"的悬停提示：把通知那条结论留在原地，横幅划过也不会丢。

    文案全部来自爬虫算好的 `payload.alert`（kind/text/hint），这里不重新判断，
    免得"横幅说什么、悬停说另一套"。标题里的值来自远端模型名，必须转义。
    """
    if metric:
        view = (D.get("metric_views") or {}).get(metric)
        if view:
            D = {**D, **view}
        else:
            # 旧载荷只有不可拆分文本，不把它冒充当前模式的历史记录。
            from fetch_model_data import notable_moves, merged_move_lines, move_hint, alert_policy
            moves = notable_moves(D.get("changes") or [], metric)
            D = {**D, "moves": moves, "move_lines": merged_move_lines(moves),
                 "last_moves": {}, "prev_moves": {}, "cumulative": {}}
            D["alert"] = alert_policy(D)
            D["alert"]["hint"] = move_hint(metric)
            if moves:
                D["alert"]["last"] = {"steps": 0}
        la = D.get("last_alert") or {}
        rows = (la.get("metric_lines") or {}).get(metric) or []
        D = {**D, "last_alert": {**la, "lines": rows, "text": ""} if rows else {}}
    a = D.get("alert") or {}
    kind = str(a.get("kind") or D.get("data_state") or "fresh")
    label = str(a.get("label") or kind)
    lines = [f"本轮结论：{label}（{kind}）"]
    last = a.get("last") or {}
    steps = last.get("steps") if isinstance(last.get("steps"), int) else None
    text = str(a.get("text") or "").strip()
    cum = D.get("cumulative") or a.get("cumulative") or {}
    csteps = cum.get("steps") if isinstance(cum.get("steps"), int) else None
    clines = [str(x) for x in (cum.get("lines") or cum.get("items") or []) if x]
    have_cum = csteps is not None and csteps >= 2 and bool(clines)

    def block(head, rows, n_raw, n_src):
        """一条标题 + 逐条明细。少了一截有两种原因，得分开说：

        · 同族合并（原始 n_raw 条并成 n_src 行）—— 通知横幅那种"…等 N 项"的截断不属于这里；
        · 被上面某个块念过（n_src 行里有几行重复），那是去重，不是合并。
        """
        tail = ""
        if isinstance(n_raw, int) and n_raw != n_src:
            tail = f"，同族合并后 {n_src} 条"
        if n_src > len(rows):
            tail += f"（另 {n_src - len(rows)} 条上面已列）"
        return [head + tail] + [f"· {t}" for t in rows]

    # 达门槛的变动逐条列全，不写"…等 N 项"：那种截断只属于通知横幅（系统有长度限制）。
    # 故障类结论（源缺失 / 没跑）仍走 alert.text，不套列表。
    raw = [str(m.get("text")) for m in (D.get("moves") or [])
           if isinstance(m, dict) and m.get("text")]
    mlines = [str(x) for x in (D.get("move_lines") or raw) if x]
    litems = [str(x) for x in (last.get("lines") or last.get("items") or []) if x]
    shown: list[str] = []

    def emit(head, rows, n_raw, keep_head=False):
        # 同一批条目只念一遍：本轮列表、历史块、累计块三者可能内容相同。
        # rows 为空时也从这里掉出去 —— 只有时间、没有内容的记录不会变成一节空壳。
        # keep_head 的那几节不一样：标题里的"哪一轮、隔几轮、跟谁比"就是信息本体，
        # 明细与上面同批时要把明细折掉、标题留下。否则历史这一路等于没说 ——
        # prev_moves 是从同一叠台账快照里往前翻出来的，与累计块撞车是常态不是意外。
        fresh = [r for r in rows if r not in shown]
        if not fresh:
            if keep_head and rows:
                lines.append(head + "（明细与上面同批，不重列）")
            return
        shown.extend(fresh)
        lines.extend(block(head, fresh, n_raw, len(rows)))

    if kind == "moved" and steps == 0 and mlines:
        emit(f"本轮达门槛的变动 {len(raw)} 项（对比 {last.get('base') or '上一轮'}）",
             mlines, len(raw))
    elif kind == "moved-cumulative" and have_cum:
        emit(f"累计达门槛的 {cum.get('n', len(clines))} 项（相对 {cum.get('at')}，往前第 {csteps} 轮）",
             clines, cum.get("n"))
    else:
        lines.append(text if text else "这轮没有需要提醒的变动")

    # 榜单进出：新进榜与落榜成对列。门槛那条明确不收它们（每天常规换榜会把通知刷成噪音），
    # 但浮层是给人看明细的，只报"新上了谁"不报"下去了谁"就是把一面说成两面。
    io = board_io(D.get("changes"))
    renames = D.get("rename_lines") or []
    if renames:
        emit("名称调整（不计榜单进出）", renames, None)
    for key, head in (("new", "新进榜"), ("gone", "落榜"), ("carried", "沿用旧读数")):
        names = [str(x) for x in (io.get(key) or []) if x]
        if names:
            emit(f"{head} {len(names)} 个", names, None)

    # 上次提示：两个来源各说一遍，读者自己就能对上"上次弹的那条"和"上次真有变动的轮次"
    # 是不是同一回事。同一轮（时间相同）并成一条标题，不重复念两遍。
    la = D.get("last_alert") if isinstance(D.get("last_alert"), dict) else {}
    pm = D.get("prev_moves") if isinstance(D.get("prev_moves"), dict) else {}
    la_rows = [str(x) for x in (la.get("lines") or []) if x] or \
              ([str(la["text"])] if la.get("text") else [])
    la_at = str(la.get("at") or "")
    pm_at, pm_rows = str(pm.get("at") or ""), \
        [str(x) for x in (pm.get("lines") or pm.get("items") or []) if x]
    # "强制往前翻"（prev_moves）与"相邻那一路"（last_moves）经常落在同一轮：同一轮就只说一次，
    # 挂一个别名，别把同一句时间+轮距的话念两遍 —— 那读起来像两个不同的结论。
    last_at = str(last.get("at") or "")
    same_as_last = bool(pm_at) and last_at == pm_at and pm.get("steps") == steps
    # 「上次弹出去的那条」也常常就是「最近一次相邻达门槛」那一轮（10-15 线上实测：两条都只挂着
    # "明细不重列"）。同一个时间说两遍读起来像两条互相印证的结论，所以并成一条挂别名。
    la_is_last = (not same_as_last) and bool(la_at) and last_at == la_at and la.get("steps") == steps
    gap = lambda s: f"，往前第 {s} 轮" if isinstance(s, int) and s >= 1 else ""
    la_head = f"上次提示：{la_at}（{la.get('kind') or '?'}{gap(la.get('steps'))}）"
    if la_at and pm_at and la_at == pm_at and not pm.get("all_duplicated"):
        emit(la_head + "；也正是最近一次有达门槛变动的轮次", la_rows or pm_rows, None, keep_head=True)
    else:
        if la_at:
            emit(la_head + ("；也正是最近一次相邻达门槛的轮次" if la_is_last else ""),
                 la_rows, None, keep_head=True)
        if pm.get("all_duplicated"):
            # 一路往前全撞在同一批明细上：如实说"没有新的变化可报"，不硬找也不编一个轮次
            lines.append(f"上次有达门槛变动的轮次：往前翻了 {pm.get('all_duplicated')} 轮，"
                         f"都是同一批变动（最近一次是 {pm_at or '?'}），没有新的变化可报")
        elif pm_at:
            emit(f"上次有达门槛变动的轮次：{pm_at}"
                 + (f"（往前第 {pm.get('steps')} 轮，对比 {pm.get('base')}）"
                    if isinstance(pm.get("steps"), int) and pm.get("steps") >= 1 else "")
                 + (f"；中间 {pm.get('dup_skipped')} 轮是同一批变动，已跳过"
                    if isinstance(pm.get("dup_skipped"), int) and pm.get("dup_skipped") >= 1 else "")
                 + ("；相邻那一路也是这一轮" if same_as_last else ""),
                 pm_rows, pm.get("n"), keep_head=True)

    if steps is not None and steps >= 1 and not same_as_last and not la_is_last:
        emit(f"最近一次相邻达门槛：{last.get('at')}（往前第 {steps} 轮，对比 {last.get('base')}）",
             litems, last.get("n"), keep_head=True)
    elif steps == -1:
        lines.append(f"往前翻了 {last.get('scanned', 0)} 轮都没到过门槛"
                     f"（最多翻 {last.get('cap', '?')} 轮）")
    if have_cum and kind != "moved-cumulative":
        # 这一节**不**加 keep_head：它与本轮那块说的是同一批条目时整节不念，是早就定下的规矩
        # （有判据盯着），而本轮那节的标题已经带了"对比哪一轮"。历史那两节要留标题是因为
        # 它们的"哪一轮/隔几轮"在本轮没有第二次机会说出来。
        emit(f"累计对比：相对 {cum.get('at')}（往前第 {csteps} 轮）有 {cum.get('n', len(clines))} 项达门槛",
             clines, cum.get("n"))
    if a.get("hint"):
        lines.append(str(a["hint"]))
    return "\n".join(lines)


def build_html(svg: str, generated_at: str, stale: bool, n_node: int = 0,
               n_est: int = 0, n_band: int = 0, tip: str = "",
               evaluation_version: str = "",
               groups: dict[str, list[tuple[int, str]]] | None = None,
               task_tip: str = "", has_think: bool | None = None,
               n_task_est: int = 0) -> str:
    """把 SVG 包成一个**背景透明、颜色随系统外观切换**的自包含 HTML。

    透明是必须的：状态栏 popover 自己的窗口就是一层系统材质（NSVisualEffectView），
    页面只要不画底色，顶栏与图表就天然同底。以前这里钉死了 `background:#0f141c`，
    结果是"顶栏是系统材质、下面是深蓝矩形"两块明显不搭；而之所以敢钉死，是因为
    SVG 里的文字色只有深色一套回退值 —— 浅色系统下会白字糊白底。
    现在两套值都给出来（--mr-* 由 @media 切换），钉死深色这个理由就不成立了。

    `color-scheme: light dark` 让浏览器按系统外观报告 prefers-color-scheme，
    WKWebView 跟随系统，无需宿主额外设 overrideUserInterfaceStyle。

    `groups` 是脚注三个计数的**精确子集**映射：键为 node / est / band，值为
    `(图中 data-i, 模型名)` 列表。有它就渲染成可聚焦的脚注链接（悬停/聚焦抬起这些点），
    没有就退回原来的纯数字；不要拿整族 data-k 代替，否则"数了 3 个点、亮了一整族"。
    """
    def group_count(key: str, n: int, label: str) -> str:
        """脚注数字 + 自绘气泡：名字只列这个子集，不依赖原生 title。"""
        entries: list[tuple[int, str]] = []
        for e in (groups or {}).get(key) or []:
            try:
                if isinstance(e, dict):
                    i, name = e.get("i", e.get("index")), e.get("n", e.get("name", ""))
                else:
                    i, name = e[0], e[1]
                entries.append((int(i), str(name)))
            except (TypeError, ValueError, IndexError, KeyError):
                continue
        if not entries:
            return f"<b>{n}</b>"
        idx = ",".join(str(i) for i, _name in entries)
        names = [name for _i, name in entries]
        body = f"{n} 个：\n" + "\n".join(f"· {name}" for name in names)
        aria = f"{n} 个{label}：" + "、".join(names)
        return (f'<span class="fgrp" data-foot-group="{key}" data-index="{idx}" '
                f'tabindex="0" role="link" aria-label="{html.escape(aria, quote=True)}">'
                f'<b>{n}</b><span class="bub">{html.escape(body, quote=False)}</span></span>')
    tag = " 旧数据" if stale else ""
    # 只在真的有"没测到速度"的点时才提占位尺寸，否则读者会去找一个不存在的那一号
    node_note = (f" · {group_count('node', n_node, '最小气泡')} 个最小气泡是没测到 tok/s 的占位，不代表最慢"
                 if n_node else "")
    # 推估点单独说明：口径（怎么推的、区间多宽）写在这里，读者不必点开每个 tooltip 才知道它不是实测。
    est_note = (f" · 虚线环 {group_count('est', n_est, '同族推估值')} 个是同族推估值"
                "（环带宽度=参照实测散布，不参与前沿）" if n_est else "")
    # 兜底带要写明"只有范围"：一个空心点被读成"某个速度值"就全错了。
    # 窗口宽窄（±几分）由爬虫写进 tooltip，这里不重抄一遍常量——两处数字迟早对不上。
    band_note = (f" · 淡色小点 {group_count('band', n_band, '参照带')} 个连同族也推不出，"
                 "只给同级能力模型的实测速度范围"
                 "（外圈环带=范围，不给中心值，也不参与前沿，悬停看数字）" if n_band else "")
    version = str(evaluation_version).removeprefix("v")
    version = version[:-2] if version.endswith(".0") else version
    version_title = f" v{html.escape(version)}" if version else ""
    # 思考时间/思考前摇没有任何一档取到时，脚注不再声明这两项口径：
    # 留着会让读者去找一列根本不存在的指标。has_think 为 None = 调用方没给结论
    # （只包一层壳的测试），此时保留说明，不替它断言"整份都没有"。
    think_note = ("；思考时间含输入/排队等首回答前等待；思考前摇为不同基准估计值之比，非任务内时间分解"
                  if has_think is not False else "")
    # 派生任务耗时只在真的有这种点时说明；公式与区间来源写一次，读者不必逐点悬停。
    time_est_note = (" · ≈ 任务耗时 = 每任务输出量 ÷ 速度（官方缺失时才派生；"
                     "区间来自速度参照带，不写历史）"
                     if n_task_est else "")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>速度与成本性价比定位图{version_title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{{
    color-scheme: light dark;
    --mr-ink:#e8eaed; --mr-dim:#8b95a5; --mr-grid:#2b3648;
    --mr-axis:#39445a; --mr-foot:#5d6a7d;
    --mr-bub:#1b2230; --mr-bub-ink:#e8eaed; --mr-bub-line:#39445a;
  }}
  @media (prefers-color-scheme: light){{
    :root{{
      --mr-ink:#1d1d1f; --mr-dim:#6b7280; --mr-grid:#e3e7ee;
      --mr-axis:#b6bec9; --mr-foot:#8a94a6;
      --mr-bub:#ffffff; --mr-bub-ink:#1d1d1f; --mr-bub-line:#d6dbe4;
    }}
  }}
  html,body{{margin:0;padding:0;background:transparent}}
  .wrap{{padding:6px 6px 2px;display:flex;justify-content:center}}
  svg{{display:block;max-width:100%;height:auto}}
  .sizebar{{display:flex;align-items:center;gap:10px;padding:2px 12px 4px;
    font:11px/1.4 -apple-system,"PingFang SC",sans-serif;color:var(--mr-foot)}}
  .sizebar label{{display:inline-flex;align-items:center;gap:6px;cursor:pointer;color:var(--mr-dim)}}
  .sizebar input{{margin:0;accent-color:#e3b341}}
  .sizebar input:focus-visible{{outline:2px solid #e3b341;outline-offset:2px}}
  /* hidden 必须显式压过 label 的 inline-flex：否则"仅任务模式可见"那层包装会一直占位 */
  .sizebar [hidden]{{display:none}}
  .sizecap{{color:var(--mr-foot)}}
  /* 任务耗时模式：TPS 推估/参照环带没有速度含义，整类隐藏；缺耗时点画 2px 空心环。 */
  svg[data-size-mode="task"] [data-band-ring]{{display:none}}
  svg[data-size-mode="task"] .mk[data-task-missing="1"] circle[data-role="node"]{{fill:none;pointer-events:all}}
  .foot{{position:relative;padding:0 12px 6px;font:11px/1.5 -apple-system,"PingFang SC",sans-serif;color:var(--mr-foot)}}
  .foot b{{color:var(--mr-dim);font-weight:500}}
  /* 悬停浮层自己画，不用原生 title：状态栏 popover 的宿主是 .accessory 的 app，
     它永远激活不了（实测 NSApp.isActive 恒 false），而 AppKit 对非活跃 app 的
     tooltip 默认不画（实测新建窗口 allowsToolTipsWhenApplicationIsInactive = false）。
     走 :hover 这条路与图上圆点高亮同机制，不依赖系统行为。 */
  .foot .tip,.foot .fgrp{{border-bottom:1px dotted var(--mr-foot);cursor:help}}
  /* 脚注计数是键盘可聚焦的链接：气泡自己画，不依赖原生 title。 */
  .foot .fgrp{{color:var(--mr-dim)}}
  .foot .fgrp:focus-visible{{outline:2px solid #e3b341;outline-offset:2px}}
  /* 定位上下文必须是整条脚注：挂在 .tip 那个内联 span 上，绝对定位会按它的 ~140px
     宽度做 shrink-to-fit，四行文案被挤成十行竖条 */
  .foot .bub{{display:none;position:absolute;left:12px;bottom:calc(100% - 2px);z-index:5;
    max-width:calc(100% - 24px);max-height:calc(100vh - 96px);overflow:auto;
    padding:7px 9px;border-radius:7px;white-space:pre-line;
    text-align:left;font:11px/1.55 -apple-system,"PingFang SC",sans-serif;
    background:var(--mr-bub);color:var(--mr-bub-ink);border:1px solid var(--mr-bub-line);
    box-shadow:0 4px 14px rgba(0,0,0,.28)}}
  .foot .tip:hover .bub,.foot .fgrp:hover .bub,.foot .fgrp:focus .bub{{display:block}}
{HOVER_CSS}
</style></head>
<body>
<div class="sizebar">
  <label><input type="checkbox" id="task-size" role="switch"
  aria-checked="false" aria-describedby="size-caption">任务耗时（越快越大）</label>
  <span id="penalty-wrap" hidden><label><input type="checkbox" id="penalizelow"
  role="switch" aria-checked="false" aria-describedby="size-caption">全局低分惩罚</label></span>
  <span class="sizecap" id="size-caption" aria-live="polite">气泡面积 = 输出速度</span></div>
<div class="wrap">{svg}</div>
<div class="foot"><span class="tip">抓取于 <b>{generated_at}</b>{tag}<span class="bub" id="fetch-tip" data-speed-tip="{html.escape(tip, quote=True)}" data-task-tip="{html.escape(task_tip, quote=True)}">{html.escape(tip, quote=False)}</span></span> · 同色细线 = 同族同代次的能力线（按推理档位固定顺序）· 黄虚线 = Pareto 前沿 · 双评分 = 智能指数 / 历史参照（缺分为 —） · 任务耗时(估)为官方解码生成耗时估计，不含首块延迟（TTFT）与判分/PDF/OCR 等额外开销{time_est_note}{think_note}{node_note}{est_note}{band_note}</div>
<script>{HOVER_JS}</script>
<script>{SIZE_JS}</script>
</body></html>"""


def write_if_changed(path: Path, text: str) -> bool:
    """内容没变就一个字都不写；真要写时走"同盘临时文件 + os.replace"。返回是否落盘。

    两个理由都是这轮实测出来的：
    · 组件按 chart.html 的 **mtime** 决定重画。每日那轮里 python 内部和 crawl.sh 各渲染一次
      （2026-10-09 07:30 的 script.log/crawler.log 各有一条"已写出/已重画"），旧写法两次都重写
      同一个文件 → 两次 mtime → 图连着闪两下，而第二次内容与第一次完全相同。
    · `write_text` 是就地截断重写：组件正好在 tick 里读它时可能拿到半截 HTML，画出一张空图，
      下一次心跳再闪回来。latest.json 与 watchlist 早就是原子替换，产物不该是例外。
    """
    real = Path(os.path.realpath(path))
    if real.exists() and real.read_text(encoding="utf-8") == text:
        return False
    tmp = real.with_name(real.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, real)
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", help="写文件，缺省打印到标准输出")
    ap.add_argument("--json", action="store_true", help="只输出坐标数据")
    a = ap.parse_args()

    if not LATEST.exists():
        sys.exit(f"找不到 {LATEST}，先跑一次 fetch_model_data.py")

    D = json.loads(LATEST.read_text())
    pts, front, order = load_points()
    if a.json:
        print(json.dumps({"pts": pts, "front": front, "legend_order": order},
                         ensure_ascii=False, indent=1))
        return

    # 与 `load_points` 同一道门槛：有指数、缺每任务成本 → 不上图，只进综述卡点名。
    unplotted: dict[str, list[dict]] = {}
    for m in D.get("models", []):
        if m.get("index") is None or m.get("apt_usd"):
            continue
        unplotted.setdefault(m.get("creator", ""), []).append(m)

    svg = build_svg(pts, front, order, unplotted, penalty=D.get("task_size_penalty"))
    # 爬虫降级（AA 抓不到、回落旧快照）时会在 payload 里标 data_state，
    # 图角上要如实标出来，不能让 1 小时前的数据长得跟实时一样。
    stale = D.get("data_state") not in (None, "ok")
    # 「无数据」只算连同族参照都没有的；推估过的点单独报数，两者不能混成一个口径。
    # 脚注三个计数各自带**精确子集**：索引就是 build_svg 里那轮 enumerate(pts) 的 data-i，
    # 悬停/聚焦只抬这些点，不按 data-k 整族抬（族里可能只有一档属于这个子集）。
    groups = {
        "node": [(i, p["n"]) for i, p in enumerate(pts) if p["tok"] == "无数据"],
        "est": [(i, p["n"]) for i, p in enumerate(pts) if p["est"]],
        "band": [(i, p["n"]) for i, p in enumerate(pts) if p["band"]],
    }
    n_noref, n_est, n_band = (len(groups["node"]), len(groups["est"]), len(groups["band"]))
    payload = build_html(svg, D.get("generated_at", "—"), stale, n_noref, n_est, n_band,
                         tip=foot_tip(D, "tok_s"), task_tip=foot_tip(D, "time_per_task_s"),
                         evaluation_version=str(D.get("evaluation_version") or ""),
                         groups=groups,
                         has_think=any(metric_number(p.get("think")) is not None for p in pts),
                         n_task_est=sum(1 for p in pts
                                        if p.get("tpe") is not None or p.get("tpb")))

    if a.out:
        wrote = write_if_changed(Path(a.out), payload)
        note = "（旧数据）" if stale else ""
        print(f"{'已写出' if wrote else '内容未变，未重写'} {a.out}"
              f"（{len(payload)} 字符，{len(pts)} 个点，前沿 {len(front)} 点）{note}")
    else:
        print(payload if _looks_like_html(a.out) else svg)


def _looks_like_html(out: str | None) -> bool:
    """缺省-branch 判断：没有显式请求 .html 就只吐 SVG。"""
    if not out:
        return False
    return out.lower().endswith((".html", ".htm"))


if __name__ == "__main__":
    main()
