#!/usr/bin/env python3
"""气泡"任务耗时面积"编码与全局低分收缩：爬虫与渲染面共用的纯数学。

两件事写在一处，是因为它们必须在**同一份输入**上得出同一个数：

  · 任务耗时半径 —— 面积分数 .04+.96u，u 由 log10 耗时反比归一（越快越大）；
  · 全局低分收缩 λ —— 同族内"分数掉多少、面积就该跟着掉多少"的一次最小二乘。

爬虫算一次 λ 发布到载荷，两张渲染面只消费；各自再算一遍就会出现
"状态栏收缩了、看板没收缩"这种只差几个像素、没人会当场发现的漂移。

来源：2026-11-06 用户要求任务耗时模式下低分档不再按同一条耗时曲线显得过大，
可用一个开关整体收缩；contract 见 README「气泡大小模式 · 低分收缩」。
"""
from __future__ import annotations

import math
import re

# 气泡尺寸：**面积**正比于输出速度，所以半径取 sqrt。
#   r = R_MAX · √(tok/tmax)，再用 R_MIN 兜住"看不见/点不到"的极端小值。
# 以前写的是 R_MIN + R_MAX·√x（4 + 11√x）—— 那个加性常数把面积编码破坏了：
# x=0.25 时面积占比 40%，而真比例应是 25%，中低值被系统性夸大约 1.6 倍。
# 纯 √ 之后只有落在下限以下的极小值才被兜底抬一下，误差可忽略。
# dashboard.html 里的两处半径公式必须与这里同构（各自的 R_MAX 随画布尺寸不同）。
R_MAX = 15.0
R_MIN = 2.0
# 按任务耗时模式的面积编码：最慢仍有 4% 面积，最快 100%。u 先做 log10 反比归一
# （与 x 轴成本轴同一种对数压缩），面积直接线性跟上 u，不再乘 u² 做对比曲线；
# 下限 .2·R_MAX（15px 画布上为 3px）让最慢点与「没测到耗时」的 2px 空心环仍分得开。
TASK_MIN_FRACTION = 0.04
TASK_R_MIN = 0.2 * R_MAX
R_MISSING = R_MIN

# 明确"不做推理"的档位。`minimal` / `min` / `low` 是**真实推理档**，必须留在拟合里；
# 归一口径：小写、去掉 `with fallback`、非字母数字切词。
_NO_REASONING_TIERS = {("non", "reasoning"), ("none",)}


def metric_number(value):
    """只认有限实数；布尔、NaN、Infinity 和字符串都算缺失。"""
    return (value if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) else None)


def valid_task_time(value):
    """按任务耗时定大小只认有限正秒数；0、负值、布尔与非有限数都算缺失。"""
    v = metric_number(value)
    return v if v is not None and v > 0 else None


def task_area_fraction(value, tmin, tmax) -> float | None:
    """任务耗时 → 面积分数（.04–1）；任一输入无效返回 None。

    u = clamp((log10(tmax) − log10(t)) / (log10(tmax) − log10(tmin)), 0, 1)
    面积分数 = .04 + .96u。tmin == tmax（或域退化）时 u = .5，不拿 0 或 1 偏袒任何一档。
    任一同一底数的对数结果相同：ln 与 log10 只差常数倍，在比值里约掉。
    """
    t = valid_task_time(value)
    lo, hi = metric_number(tmin), metric_number(tmax)
    if t is None or lo is None or hi is None or lo <= 0 or hi <= 0:
        return None
    if hi <= lo:
        u = 0.5
    else:
        u = (math.log10(hi) - math.log10(t)) / (math.log10(hi) - math.log10(lo))
        u = min(1.0, max(0.0, u))
    return TASK_MIN_FRACTION + (1.0 - TASK_MIN_FRACTION) * u


def task_radius(value, tmin, tmax) -> float:
    """按任务耗时编码面积：越快越大。缺耗时给 R_MISSING（由 JS 画成空心环）。

    r = R_MAX·√面积分数，夹在 [.2·R_MAX, R_MAX]。保留一位小数：
    这是**画出来的像素**，两位以上在小图上没有意义，还会让两面的属性串永远对不上。
    """
    fraction = task_area_fraction(value, tmin, tmax)
    if fraction is None:
        return R_MISSING
    return round(min(R_MAX, max(TASK_R_MIN, R_MAX * math.sqrt(fraction))), 1)


def task_radius_penalized(value, tmin, tmax, factor: float = 1.0) -> float:
    """任务耗时半径再乘全局低分倍率（乘在**面积**上，所以半径乘 √factor）。

    factor 夹在 [0, 1]：收缩只允许让低分档变小，不把任何档放大。
    下限仍是 TASK_R_MIN —— 它保证"最慢的真实点"与缺失点的空心环分得开；
    收缩把面积压到下限以下时半径停在下限，不再继续缩，这是可见性取舍。
    """
    fraction = task_area_fraction(value, tmin, tmax)
    if fraction is None:
        return R_MISSING
    f = metric_number(factor)
    f = 1.0 if f is None else min(1.0, max(0.0, f))
    return round(min(R_MAX, max(TASK_R_MIN, R_MAX * math.sqrt(fraction * f))), 1)


def is_no_reasoning_tier(tier) -> bool:
    """明确不做推理的档位（`non-reasoning` / `none`）。

    空档位**不**算：名字里没写档位时无从判断，按"是推理档"参与拟合，
    宁可多含一个真实档，也不把无标注的普通模型踢出校准。
    """
    s = str(tier or "").lower().replace("with fallback", " ")
    toks = tuple(t for t in re.split(r"[^a-z0-9]+", s) if t)
    return toks in _NO_REASONING_TIERS


def _anchor_max(rows: list[dict], family: str) -> dict:
    """同族锚点：分数最高那档；同分取**最慢**，再同取名字最大的。

    三条键全用上是为了确定：同分同耗时的一族里，选谁当锚点不能取决于列表顺序，
    否则两份输入相同、顺序不同的数据会算出两个 λ。
    """
    members = [r for r in rows if r["family"] == family]
    return max(members, key=lambda r: (r["index"], r["task_s"], r["name"]))


def fit_task_size_penalty(rows: list[dict]) -> dict:
    """全局低分收缩 λ 的闭式最小二乘。

    输入行（画得出来的点）至少要有 family / name / index / tok_s / task_s / task_est_s。
    `task_s` 是官方每任务耗时（决定 y 用的面积），`task_est_s` 只参与面积编码的
    定义域 —— 与渲染面 `taskTimeOf` 的取值顺序一致（官方优先、缺失才用估计），
    否则同一份数据在爬虫侧与画面上会按两套域缩放，λ 就不再是画面误差的最小二乘。

    x = ln(锚点分数 / 本档分数)
    y = ln((本档面积 / 锚点面积) / (本档 tok/s / 锚点 tok/s))
    λ = max(0, Σxy / Σx²)，分母为 0 或无可用数据时 λ = 0（= 关闭收缩）

    λ ≥ 0 是刻意的：它只允许"分数低就缩小"，不允许反过来把低分档放大成
    看起来比高分档更大的球 —— 那是数据没支持的结论。

    面积用**面积分数**（.04–1）而不是画出来的半径平方：像素半径保留一位小数，
    那是显示层的取整；拿它进拟合会把 R_MAX 不同的两张画布算出两个 λ。
    用分数之后面积比里连 R_MAX 都约掉了，同一份数据在两面必然同一个 λ。
    """
    # 与渲染面 task_point 的取值顺序逐项对齐：官方有效正数优先，**无效或缺失**
    # 才退到派生估计。只判 `is None` 会让官方的 0 秒吞掉估计值，域就和画面不一致。
    domain_vals = []
    for r in rows:
        t = valid_task_time(r.get("task_s"))
        if t is None:
            t = valid_task_time(r.get("task_est_s"))
        if t is not None:
            domain_vals.append(t)
    dom = (min(domain_vals), max(domain_vals)) if domain_vals else None

    indices = [v for r in rows if (v := metric_number(r.get("index"))) is not None and v > 0]
    global_max_index = max(indices) if indices else 0.0

    numeric_ok = [r for r in rows
                  if metric_number(r.get("index")) is not None and r["index"] > 0
                  and valid_task_time(r.get("tok_s")) is not None
                  and valid_task_time(r.get("task_s")) is not None]
    eligible = [r for r in numeric_ok if not is_no_reasoning_tier(r.get("tier"))]
    excluded = len(numeric_ok) - len(eligible)

    def area(row: dict) -> float:
        fraction = task_area_fraction(row["task_s"], dom[0], dom[1]) if dom else None
        return fraction if fraction is not None else TASK_MIN_FRACTION

    families = sorted({r["family"] for r in eligible if r["family"]})
    terms: list[tuple[float, float]] = []
    for family in families:
        anchor = _anchor_max(eligible, family)
        anchor_area = area(anchor)
        anchor_tps = anchor["tok_s"]
        for row in eligible:
            if row["family"] != family:
                continue
            x = math.log(anchor["index"] / row["index"])
            y = math.log((area(row) / anchor_area) / (row["tok_s"] / anchor_tps))
            terms.append((x, y))

    denominator = sum(x * x for x, _ in terms)
    lam = max(0.0, sum(x * y for x, y in terms) / denominator) if denominator else 0.0

    return {
        "lambda": lam,
        "global_max_index": global_max_index,
        "points": len(eligible),
        "families": len(families),
        "excluded_non_reasoning": excluded,
        # 公式随载荷发布：任何一面（含以后新加的）都不必再猜乘在哪一层。
        "formula": "factor = (index / global_max_index) ** lambda",
    }


def penalty_ready(block) -> bool:
    """载荷里这块能否真的产生收缩：是字典且 λ 为有限正数。

    旧快照没有这块、或拟合出 λ=0（没有可解释的同族规律）时都算"不可用"：
    界面据此禁用开关并明说，不拿 factor=1 冒充一次已经做过的收缩。
    """
    if not isinstance(block, dict):
        return False
    lam = metric_number(block.get("lambda"))
    gmax = metric_number(block.get("global_max_index"))
    return lam is not None and lam > 0 and gmax is not None and gmax > 0


def size_factor(index, block) -> float:
    """该分数在全局收缩里的面积倍率，落在 (0, 1]。

    载荷缺这一块（改动前生成的旧快照）、λ=0、或分数无效时返回 1 ——
    旧载荷照常渲染，只是没有收缩，不因为缺字段就整页失败。
    """
    if not isinstance(block, dict):
        return 1.0
    lam = metric_number(block.get("lambda"))
    gmax = metric_number(block.get("global_max_index"))
    value = metric_number(index)
    if lam is None or gmax is None or value is None or lam <= 0 or gmax <= 0 or value <= 0:
        return 1.0
    return min(1.0, (value / gmax) ** lam)
