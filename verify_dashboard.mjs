#!/usr/bin/env node
/**
 * dashboard.html 的渲染回归（零依赖，Node 跑）
 *
 * 做法：把内联 IIFE 抽出来，在最小 DOM 桩里跑一遍，统计各容器产物。
 * 要的是「渲染逻辑有没有被改坏」的回归基线，不是像素级校验。
 *
 * 六个场景都跑（每个场景都是**摆出来的输入**，不是"今天恰好抓到了什么"）：
 *   1. normal    —— 一切正常（sources/data_state 强制钉回 ok：2026-10-08 早上 OR 超时，
 *                   拿真实载荷当"正常"基线会让"不弹横幅/胶囊全绿"因数据降级而变红）
 *   2. degraded  —— AA 正常但副源缺失：必须标出来，但数据照常渲染
 *   3. stale     —— AA 抓取失败，回落到旧快照（数据照常渲染 + 必须warn）
 *   4. empty     —— AA 抓取失败，本地也没快照（必须出空状态，不能留白屏）
 *   5. nopayload —— <script src="data/latest.js"> 根本没加载成功（文件缺失/软链断/不可读）
 *                   必须说"没读到文件"，禁止替 AA 下"抓取失败"的结论
 *   6. bandplant —— 手工摆一个必然支配全场的参照带点/推估点：真实载荷里这类点常被别家支配，
 *                   "忘了把它排除出前沿"不改变任何输出，判据就空转。这个场景专门把它顶出来。
 *
 * 用法：node verify_dashboard.mjs
 */
import { readFileSync, existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(HERE, 'dashboard.html'), 'utf8');
// data/ 不入库：fresh clone 与发行包里都没有 latest.js。存在性守卫要显式做
// （existsSync 早就 import 了却一直没用上），否则开箱即炸一个 ENOENT 堆栈，
// 读起来像"判据坏了"，其实是"还没抓过数据"。
if (!existsSync(join(HERE, 'data/latest.js'))) {
  console.error('✗ 找不到 data/latest.js —— 判据需要真实载荷。先抓一次数据：');
  console.error('  bash ~/Library/Application\\ Support/ModelRadar/bin/crawl.sh （或点组件 ↻ 立即抓取）');
  process.exit(2);
}
const dataFile = readFileSync(join(HERE, 'data/latest.js'), 'utf8');
const D0 = JSON.parse(dataFile.slice(dataFile.indexOf('{'), dataFile.lastIndexOf('}') + 1));

// 从实际气泡坐标重建期望折线，逐顶点校验，不再要求分数单调。
function tierLinesMatch(svg, groups) {
  const rank = t => ['non-reasoning', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
    .indexOf(String(t || '').replace(/ with fallback$/, '').replace(/^none$/, 'non-reasoning').replace(/^min$/, 'minimal'));
  const key = m => [rank(m.tier) < 0 ? 99 : rank(m.tier), m.index, m.apt_usd];
  return Object.entries(groups).filter(([, g]) => g.length > 1).every(([k, g]) => {
    const attrs = [...svg.matchAll(/<polyline\b([^>]*)>/g)]
      .find(m => m[1].includes(`data-k="${k}"`))?.[1] || '';
    const actual = (attrs.match(/points="([^"]*)"/) || [, ''])[1].trim().split(/\s+/);
    const coords = new Map();
    for (const m of svg.matchAll(/<circle\b([^>]*)><title>([\s\S]*?)<\/title>/g)) {
      const name = g.find(p => m[2].startsWith(p.name + ' ·'))?.name;
      if (name) coords.set(name, [+(m[1].match(/cx="([^"]*)"/) || [])[1],
                                 +(m[1].match(/cy="([^"]*)"/) || [])[1]]);
    }
    const expected = [...g].sort((a, b) => {
      const x = key(a), y = key(b);
      return x[0] - y[0] || x[1] - y[1] || x[2] - y[2];
    });
    return actual.length === expected.length && expected.every((m, i) => {
      const xy = coords.get(m.name), got = actual[i].split(',').map(Number);
      return xy && got.every((v, j) => Math.abs(v - xy[j]) < 0.11);
    });
  });
}

// 夹具就是今天的真实载荷。上游整域挂掉时 models 会是 0，下面那堆内容判据会集体变红，
// 看着像看板坏了、其实是"没有东西可判"（姊妹仓 2026-10-08 就是这样：OpenRouter 连续
// 读超时后跑出十几条 FAIL）。这种情况单独立刻退出，rc=2 明确写"无法判定"——
// 既不能让它等于"通过"，也不该混进"判据失败"里。
if (!(D0.models || []).length) {
  console.error(`✗ 载荷里没有模型（data_state=${D0.data_state}）：内容判据无从跑起。`);
  console.error('  这不是判据失败，是"无法判定"。先抓一轮数据（组件 ↻ 或 bin/crawl.sh）再跑本脚本。rc=2');
  process.exit(2);
}

const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (scripts.length !== 1) throw new Error(`期望 1 段内联脚本，实际 ${scripts.length}`);
const code = scripts[0];

/** 在 DOM 桩里跑一遍看板脚本，返回各容器产物。
 *  summary 传对象时等于"本机装了总结 CLI 并已写出 summary.js"；
 *  不传就是没装（window.SUMMARY 缺席）—— 两条分支都得跑到，否则"没 CLI 时显示什么"
 *  这种最常见的状态反而没人验证过。 */
function render(D, noPayload = false, summary = null, localStorage = null) {
  const sinks = {};
  const mk = id => (sinks[id] = { id, innerHTML: '', textContent: '',
                                 style: {}, dataset: {}, attrs: {}, classList: { add() {}, remove() {} },
                                 setAttribute(k, v) { this.attrs[k] = String(v); },
                                 removeAttribute(k) { delete this.attrs[k]; },
                                 getAttribute(k) { return this.attrs[k] ?? null; },
                                 querySelector: () => ({ set onclick(v) {} }),
                                 onclick: null, oninput: null, onchange: null });
  const document = {
    getElementById: id => sinks[id] || mk(id),
    // querySelectorAll 必须按**真实 HTML 里该 class 出现几次**造同样多个容器。
    // 桩以前直接返回空数组，于是"两个容器只填了一个"这类问题在这儿完全看不见
    // （顶栏与"数据源状态"卡片曾经共用同一个 id，getElementById 只命中第一个）。
    querySelectorAll: sel => {
      // 表头按真实 HTML 造桩：排序断言要能点到 <th data-k=think_ratio>，否则"排序改用派生率"
      // 这条只能在源码里看得见，而源码看起来对、产物行为错正是上一版的坑。
      // 页面里既用 querySelectorAll('th') 也按 'th[data-k]' 筛（隐藏缺数据列），
      // 两种都要返回**同一批**桩对象，否则 hidden 写在一个没人读的副本上。
      if (sel === 'th' || sel.startsWith('th[')) {
        return [...html.matchAll(/<th\b([^>]*)>/g)].map((m, i) => {
          const id = `th#${i}`;
          if (!sinks[id]) {
            const o = mk(id);
            o.dataset = { k: (m[1].match(/data-k="([^"]+)"/) || [])[1] || '' };
            sinks[id] = o;
          }
          return sinks[id];
        });
      }
      if (sel[0] !== '.') return [];
      const cls = sel.slice(1);
      const n = (html.match(new RegExp(`class="[^"]*\\b${cls}\\b[^"]*"`, 'g')) || []).length;
      return Array.from({ length: n }, (_, i) => sinks[`${cls}#${i}`] || mk(`${cls}#${i}`));
    },
    querySelector: () => null,
    createElement: () => ({ style: {}, className: '', textContent: '', replaceWith() {} }),
  };
  // latest.js 里是 `window.MODEL_DATA = {...}`，把 window 当参数传进去，
  // 数据脚本和内联脚本就共用同一个 window 对象了。
  // noPayload=true 模拟的是「<script src=data/latest.js> 根本没加载成功」——
  // 文件缺失 / data 软链断 / 不可读，这时 window 上压根没有 MODEL_DATA。
  // __MR_HARNESS__ 让页面把**真实** timeCell 挂到 window.__mrTest；普通页面没有这个开关。
  const win = noPayload
    ? { __MR_HARNESS__: true }
    : Object.assign({ MODEL_DATA: D, __MR_HARNESS__: true },
                    summary ? { SUMMARY: summary } : {});
  new Function('window', 'document', 'location', 'setInterval', 'localStorage', code)(
    win, document, { reload() {} }, () => {}, localStorage);
  sinks.__mrWindow = win;
  return sinks;
}

let FAILED = [];
const ok = (label, cond, detail) => {
  if (!cond) FAILED.push(label);
  console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? ' : ' + detail : ''}`);
};

/* ---------- 静态结构判据（与场景无关）---------- */
// id 重复时 getElementById 只返回文档里第一个，第二块区域会永远空白且不报错 ——
// 这次"数据源状态卡片从不填充"就是这么来的。渲染层断言看不见它（它们只查第一个节点），
// 所以按"出事的那个面"补一条源码级判据。
{
  const ids = [...html.matchAll(/\sid="([^"]+)"/g)].map(m => m[1]);
  const dup = [...new Set(ids.filter((v, i) => ids.indexOf(v) !== i))];
  ok('HTML 里没有重复 id', dup.length === 0, dup.join(','));
  // 每个 getElementById 的目标都必须在文档里存在（拼错/改名后忘同步会静默失效）
  const wanted = [...html.matchAll(/getElementById\(['"]([^'"]+)['"]\)/g)].map(m => m[1]);
  const missing = [...new Set(wanted)].filter(x => !ids.includes(x));
  ok('每个 getElementById 的目标都存在', missing.length === 0, missing.join(','));
  ok('看板标题节点带 id=scatter-title（脚本能原地更新版本）',
     /<h2 id="scatter-title">速度与成本性价比定位图<\/h2>/.test(html));
  // 三项时间指标必须插在「每任务成本」与「输出 tok/s」之间，不能漂到别的列后面。
  const heads = [...html.matchAll(/<th data-k="([^"]+)"/g)].map(m => m[1]);
  const wantHeads = ['name', 'index', 'apt_usd', 'time_per_task_s', 'think_s', 'think_ratio',
                     'tok_s', 'ttft_s', 'in_per_1m_usd', 'out_per_1m_usd', 'ctx', 'or_id'];
  ok('完整数据表把任务耗时/思考时间/思考前摇 插在每任务成本与输出 tok/s 之间',
     JSON.stringify(heads) === JSON.stringify(wantHeads), heads.join(' / '));
  ok('旧 first500 列/表头已彻底移除',
     !heads.includes('tps500') && !/前\s*500/.test(html) && !/tps500/.test(html), '');
  const ratioTh = html.match(/<th data-k="think_ratio" title="([^"]*)">([^<]*)<\/th>/);
  ok('思考前摇列名与悬停说明表明「不同评测口径之比、非任务内分解、可超过 100%」',
     !!ratioTh && ratioTh[2] === '思考前摇'
       && /不同评测口径|不同基准/.test(ratioTh[1])
       && /不是同一任务内/.test(ratioTh[1])
       && /超过\s*100%/.test(ratioTh[1]),
     ratioTh ? `${ratioTh[2]}｜${ratioTh[1]}` : '找不到 think_ratio 表头');
  // 可见表头必须自带「(估)」，且悬停说明要诚实：官方只估解码，不含 TTFT 与判分/PDF/OCR 开销。
  ok('任务耗时表头显式标(估)，悬停说明不含 TTFT 与判分/PDF/OCR',
     /<th data-k="time_per_task_s" title="[^"]*(?:TTFT|首块延迟)[^"]*(?:判分|PDF|OCR)[^"]*">任务耗时\(估\)<\/th>/
       .test(html));
  const sizeLabel = html.match(/<label[^>]*>\s*<input[^>]*id="sizebytask"[^>]*\/?>\s*([^<]*)<\/label>/);
  ok('看板大小开关写明「圆面积：TPS / 任务耗时」，id 仍是 sizebytask',
     !!sizeLabel && sizeLabel[1].includes('圆面积：TPS')
       && sizeLabel[1].includes('任务耗时（越快越大）')
       && /id="sizebytask"/.test(html) && !/按任务耗时显示大小/.test(html),
     sizeLabel ? sizeLabel[1].trim() : '找不到 sizebytask 开关');
}

/* ---------- 梯队阶梯的**独立复算**（判据自己的实现，不复用爬虫代码） ----------
   规则：去重分数从高到低，与"本级起级分"差 ≤tol 的并进本级，否则另起一级（rank 0 = 最高）。
   滚动比较（跟上一个分数比）会把每分一档的连续阶梯一路并成巨档，所以窗口必须钉在起级分上。 */
function ladderOf(vals, tol) {
  const vs = [...new Set(vals.filter(v => typeof v === 'number' && Number.isFinite(v)))]
    .sort((a, b) => b - a);
  const b = [];
  for (const v of vs) {
    if (b.length && b[b.length - 1].high - v <= tol) b[b.length - 1].low = v;
    else b.push({ rank: b.length, high: v, low: v });
  }
  return b;
}
function rankOf(v, b) {
  if (!b.length || typeof v !== 'number') return null;
  const hit = b.find(x => v <= x.high && v >= x.low);
  if (hit) return hit.rank;
  let best = null, gap = null;
  for (const x of b) {
    const d = Math.abs(v - (v > x.high ? x.high : (v < x.low ? x.low : v)));
    if (gap === null || d < gap) { best = x.rank; gap = d; }
  }
  return best;
}

/* ---------- 场景 1/2 用真实数据，场景 3 造一份 ---------- */
function scenario(kind) {
  // 每个场景的 sources 都必须**摆全**：D0 是今天的真实载荷，哪个源恰好挂了它就带哪个
  // 挂的状态进场景。2026-10-08 早上 OR 读超时，"stale 场景胶囊 warn1 bad0"就因为它
  // 继承了真实的 OR 失败而多出一个 bad —— 判据红的是"今天的数据"，不是代码。
  function pinSources(D, overrides = {}) {
    const keys = ['aa', 'openrouter', 'datalearner', ...Object.keys(D.sources || {})];
    D.sources = {};
    for (const k of new Set(keys)) {
      D.sources[k] = { ok: true, n: 100, ts: D0.generated_at, ...(overrides[k] || {}) };
    }
    return D;
  }
  if (kind === 'normal') {
    // 场景 1 必须**钉住"一切正常"这个输入**，不能直接拿今天的真实载荷当基线：
    // 2026-10-08 早上 OpenRouter 读超时，latest.json 变成 degraded，于是"正常时不弹横幅 /
    // 胶囊全绿"这三条判据当场变红 —— 红的是"今天的数据不正常"，而不是代码坏了。
    // 判据要能被反复执行才有意义，所以这里把 sources 与 data_state 强制摆回 ok。
    const D = structuredClone(D0);
    D.data_state = 'ok';
    D.data_note = '';
    return pinSources(D);
  }
  if (kind === 'degraded') {
    // 主源正常、副源缺失：这是今天真实发生的状态。数据照常渲染，但必须标出来 ——
    // 以前 data_state 只看 AA，"AA 好、OpenRouter/DataLearner 全挂"被当成完全成功。
    const D = structuredClone(D0);
    D.data_state = 'degraded';
    D.data_note = '另有源缺失：OpenRouter 报价（抓取失败 The read operation timed out）'
      + '、DataLearner 镜像（403 Forbidden）';
    return pinSources(D, {
      aa: { n: 127 },
      openrouter: { ok: false, err: 'The read operation timed out' },
      datalearner: { ok: false, err: '403 Forbidden' },
    });
  }
  if (kind === 'empty') {
    // AA 挂、本地无快照：models 应为空，且不许造「只有价位」的伪模型
    const D = structuredClone(D0);
    D.models = [];
    D.changes = [];
    // 爬虫侧 models 为空时 legend 也是空的，场景要照实摆，否则"图例判据"会拿旧名单去核对空表
    D.legend = [];
    D.data_state = 'empty';
    D.data_note = 'AA 源本次抓取失败，且本地没有可用快照，能力指标全部缺失';
    return pinSources(D, {
      aa: { ok: false, err: '403 Forbidden' },
      openrouter: { n: 452 },
    });
  }
  if (kind === 'stale') {
    // AA 挂、但库里有旧快照：爬虫会回落，数据照常渲染，只是标记 stale
    const D = structuredClone(D0);
    D.data_state = 'stale';
    D.data_note = 'AA 本次抓取失败，当前显示 2026-09-28 03:06:22 的快照';
    return pinSources(D, {
      aa: { ok: true, stale: true, from: '2026-09-28 03:06:22', n: 113, err: '403 Forbidden' },
    });
  }
  if (kind === 'bandplant') {
    // 判据得能被证明会拦。真实载荷里当天的带点/推估点常常恰好在"成本-分数"两轴上被别家
    // 支配，那时渲染器"忘了排除"既不改变金线的点数、也不改变金线坐标 —— 输出级判据全程空转
    // （变异实测就是这样漏掉两条前沿判据的）。这里把两个点摆到**必然支配全场**的位置
    // （最便宜 + 最高分），让"排除"这件事真的有机会被观察到。
    const D = structuredClone(D0);
    const xs = D.models.filter(m => m.apt_usd > 0).map(m => m.apt_usd);
    const ys = D.models.filter(m => m.index !== null).map(m => m.index);
    const src = D.models.find(m => m.index !== null && m.apt_usd > 0 && m.tok_s > 0);
    const band = structuredClone(src);
    Object.assign(band, { name: 'Planted Band', series_key: 'plantedband', series: 'Planted Band',
                          apt_usd: Math.min(...xs) / 3, index: Math.max(...ys) + 3, tok_s: null });
    for (const k of ['tok_s_est', 'tok_s_est_range', 'tok_s_est_basis', 'tok_s_est_n', 'tok_s_est_note',
                     'time_per_task_est', 'time_per_task_est_s', 'time_per_task_est_range',
                     'time_per_task_est_basis', 'time_per_task_band'])
      delete band[k];
    band.tok_s_band = [40, 90];
    band.tok_s_band_n = 3;
    band.tok_s_band_same_creator = 0;
    // 前沿成员/梯队/距离现在由爬虫算，planted 行是克隆来的、带着原行的旧标记。
    // 场景必须按"生产者会怎么标"把它摆对：推估与参照带一律不进前沿（这条判据正在被测）。
    band.on_frontier = false;
    band.cap_band = rankOf(band.index, ladderOf(
      D.models.filter(m => m.index != null).map(m => m.index).concat(band.index),
      D.tier_tolerance_points || 1));
    band.dist_front = 0.5;
    band.tok_s_band_note = '同级能力参照：智能指数 ±2 分的 3 个实测模型是 40–90 tok/s'
      + '（其中同厂商 0 个）。刻意不给中心值：这不是本族推估，是别家同级能力的实测范围。';
    const est = structuredClone(band);
    Object.assign(est, { name: 'Planted Est', series_key: 'plantedest', series: 'Planted Est' });
    for (const k of ['tok_s_band', 'tok_s_band_n', 'tok_s_band_same_creator', 'tok_s_band_note'])
      delete est[k];
    Object.assign(est, { tok_s_est: 66, tok_s_est_range: [40, 90], tok_s_est_basis: 'series-interp',
                         tok_s_est_n: 2,
                         tok_s_est_note: '估计：同族相邻档插值（2 个参照，区间取同族实测散布）' });
    D.models = D.models.concat([band, est]);
    // 固定样本验证推估展示与前沿排除，不继承真实载荷的降级状态。
    D.data_state = 'ok';
    D.data_note = '';
    return pinSources(D);
  }
  return D0;
}

const LABEL = { normal: '【场景 1】AA 正常（sources 与 data_state 被钉回 ok，不跟着今天的真实数据漂）',
                degraded: '【场景 2】AA 正常、副源缺失 → 必须说但数据照常',
                stale: '【场景 3】AA 失败→回落旧快照',
                empty: '【场景 4】AA 失败→本地无快照',
                nopayload: '【场景 5】latest.js 没加载成功（文件缺失/软链断/不可读）',
                bandplant: '【场景 6】人为摆一个必然进前沿的带点/推估点（顶出排除判据）' };

{
  const D = scenario('normal');
  const base = D.models[0];
  D.models = [['A', 'low', 10, 1, true], ['A', 'high', 20, 2, true],
              ['A', 'max', 25, 4, false], ['B', 'max', 30, 3, true]]
    .map(([series, tier, index, apt_usd, on_frontier]) => ({...base, series, series_key: series,
      name: `${series} (${tier})`, tier, index, apt_usd, on_frontier}));
  const svg = render(D).scatterwrap.innerHTML;
  const notes = [...svg.matchAll(/<text data-frontier-note=""[^>]*>([^<]+)<\/text>/g)].map(m => m[1]);
  ok('散点图只标同代次前沿最高档，非前沿 max 不替代', notes.join('|') === 'A (high)|B (max)');
}

for (const kind of ['normal', 'degraded', 'stale', 'empty', 'nopayload', 'bandplant']) {
  const noPayload = kind === 'nopayload';
  const D = noPayload ? null : scenario(kind);
  const s = render(D, noPayload);
  const L = s.legend?.innerHTML ?? '';
  const S = s.scatterwrap?.innerHTML ?? '';
  const B = s.tb?.innerHTML ?? '';
  // .srcs 有两个容器（顶栏 + 数据源状态卡片）。分开取，才能判"是不是每个都被填了"；
  // 但 warn/bad 的计数只看第一个容器 —— 拼起来会让每个计数翻倍，把老断言挤掉。
  const SRCN = Object.keys(s).filter(k => k.startsWith('srcs#')).sort().map(k => s[k].innerHTML);
  const SRC = SRCN[0] ?? '';
  const BAN = s.banner?.innerHTML ?? '';
  const CHG = s.changes?.innerHTML ?? '';

  const rings   = (L.match(/class="ring"/g) || []).length;
  const limgs   = (L.match(/<img /g) || []).length;
  const circles = (S.match(/<circle /g) || []).length;
  const simgs   = (S.match(/<image /g) || []).length;
  const dash    = (S.match(/stroke-dasharray/g) || []).length;
  const rows    = (B.match(/<tr>/g) || []).length;
  const warns   = (SRC.match(/class="src warn"/g) || []).length;
  const bads    = (SRC.match(/class="src bad"/g) || []).length;
  // 任务耗时列现在也会出现 ≈（派生估计点）与区间，所以"速度列的 ≈/估/带"这类计数
  // 必须只在速度列里数：整表数会把两个指标混成一锅，任一侧加了标记就会互相打红。
  // 列位置由行宽反推：tok_s 之后固定还有 ttft/in/out/ctx/or_id 五列。
  const rowCells = B.split('<tr>').slice(1).map(tr =>
    [...tr.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map(m => m[1]));
  const iTokCell = rowCells.length ? rowCells[0].length - 6 : -1;
  const speedCells = rowCells.map(r => r[iTokCell] ?? '');

  console.log(`\n${LABEL[kind]}`);
  console.log(`  图例圆 ${rings} · 散点气泡 ${circles} · 表格行 ${rows} · 变动卡 ${CHG.length} 字符`);

  if (kind === 'normal' || kind === 'nopayload') {
    const ver = noPayload ? '' : String(D.evaluation_version || '')
      .replace(/^v/, '').replace(/\.0$/, '');
    const want = '速度与成本性价比定位图' + (ver ? ` v${ver}` : '');
    ok('标题按评测版本动态标注（5.0→v5，其他小数原样）',
       (s['scatter-title']?.textContent || '') === want,
       `${JSON.stringify(s['scatter-title']?.textContent)} vs ${JSON.stringify(want)}`);
  }

  ok('每个源状态容器都被填（顶栏 + 数据源状态卡片）',
     SRCN.length >= 2 && SRCN.every(h => h.length > 0),
     `${SRCN.length} 个容器，空的 ${SRCN.filter(h => !h).length} 个`);

  if (kind === 'empty') {
    ok('models 为空时表格不渲染行',      rows === 0, String(rows));
    ok('散点区出空状态而不是空白',        /class="void"/.test(S));
    ok('空状态说明了原因',               /没有可绘制的数据|AA/.test(S));
    ok('不伪造「只有价位」的伪模型',      rows === 0 && !/in_per_1m/.test(B));
    ok('变动卡不谎报「没有变动」',        /没有当前数据可比/.test(CHG));
    ok('出现降级横幅',                   BAN.length > 0);
  } else if (kind === 'nopayload') {
    // 这次的线上事故：latest.js 没加载成功时，页面硬说"AA 官方榜本次抓取失败"——
    // 它对抓取结果零证据却替 AA 下结论，把人往错方向带。这三条就是防它回归。
    ok('不替没证据的事下结论（不提 AA 失败）', !/AA 官方榜本次抓取失败/.test(S), S.slice(0, 60));
    ok('明说没读到数据文件',              /没读到|未读到/.test(S) && /latest\.js/.test(S));
    ok('源状态条不留空（空看着像没配源）', /未读到 data\/latest\.js/.test(SRC));
    ok('给出处置动作',                    /crawl\.sh|立即抓取/.test(S));
    ok('仍然出空状态而不是白屏',          /class="void"/.test(S));
  } else {
    // 数"气泡"要数 <g class="mk…> 而不是 <circle>：推估点外面多了一圈区间环，
    // 数圆会把一个气泡数成 2~3 个（这条判据以前一直是数圆的，加了推估就必然变红）。
    const bubbles = (S.match(/<g class="mk[ "]/g) || []).length;
    ok('散点气泡 = 参与算图的模型数',   bubbles === D.models.filter(m => m.index !== null && m.apt_usd > 0).length,
       `${bubbles} 个气泡`);
    ok('图例圆 = 图例口径内的模型数',   rings === D.models.filter(m => m.tok_s > 0).length);
    ok('图例圆都填满 icon 或兜底',      limgs + (rings - D.models.filter(m => m.tok_s > 0 && m.logo_local).length) === rings);
    ok('散点 icon 数 = 有 logo 的气泡数', simgs === D.models.filter(m => m.index !== null && m.apt_usd > 0 && m.logo_local).length);
    // 前沿那条虚线要单独数：推估气泡也用 stroke-dasharray（虚线=非实测），
    // 所以"整张图只有 1 处 dash"这个老判据在加入推估后必然变假 —— 判据要跟着口径改，
    // 而不是让新功能去迁就一个把"虚线=只有前沿"当公理的断言。
    const frontDash = (S.match(/<polyline[^>]*stroke-dasharray/g) || []).length;
    ok('Pareto 前沿存在（且只有它一条折线是虚线）', frontDash === 1, `折线虚线 ${frontDash} 条`);
    // 前沿口径必须与 Python 渲染面一致：推估点（有 tok_s_est 且无实测 tok_s）与参照带行
    // （前三档全落空、只有 tok_s_band）都不参与支配计算。
    // 判据只数"有一条金线"的话，看板把推估点算进前沿也不会红 —— 而那正是
    // "同一份数据、两张图、两条不一样的金线"（违反"判据单点定义"）的形态。
    const isEstM = m => !!m.tok_s_est && !m.tok_s;
    const isBandM = m => !m.tok_s && !m.tok_s_est && !!m.tok_s_band;
    const expectFront = D.models
      .filter(m => m.index !== null && m.apt_usd > 0 && !isEstM(m) && !isBandM(m))
      .slice().sort((a, b) => a.apt_usd - b.apt_usd)
      .reduce((acc, m) => (acc.some(q => q.index >= m.index) ? acc : acc.concat(m)), []);
    const goldPts = ((S.match(/<polyline points="([^"]*)" fill="none" stroke="#e3b341"/) || [])[1] || '');
    const drawn = (goldPts.match(/[\d.]+,[\d.]+/g) || []).length;
    ok('前沿点集 = Python 口径（推估点与参照带都不参与）', drawn === expectFront.length,
       `图上 ${drawn} 点，应为 ${expectFront.length} 点`);

    // ↓ 前沿与梯队现在由爬虫算一次、两张渲染面只消费。"只消费"这条改动会把任何算错的金线
    //   一路放过，所以这里必须**独立复算**来验它的标记与字段，而不是信它自己。
    const key = m => `${m.name}〔${m.creator}〕`;
    const flagFront = D.models.filter(m => m.on_frontier).map(key).sort();
    ok('payload 的 on_frontier = 独立复算的前沿集合',
       flagFront.join('|') === expectFront.map(key).sort().join('|'),
       `标了 ${flagFront.length} 点，独立复算 ${expectFront.length} 点`);
    const plottable = D.models.filter(m => m.index !== null && m.apt_usd > 0);
    // 场景人为加的行不在爬虫算过的名单里（渲染后才塞进来），复算梯队/聚合值必须排除它们，
    // 否则会把"场景加的"当成"爬虫漏算的"。
    const PRODUCED = m => !/^(plantedband|plantedest)$/.test(String(m.series_key || ''));
    const TOL = D.tier_tolerance_points ?? null;
    const expLadder = ladderOf(D.models.filter(PRODUCED).map(m => m.index), TOL ?? 1);
    ok('可画点都带 cap_band / dist_front（缺字段=渲染面无序可消费）',
       plottable.length === 0 || plottable.every(m => Number.isInteger(m.cap_band)
                               && typeof m.dist_front === 'number'),
       plottable.length ? `首个 ${JSON.stringify(plottable[0] &&
         { band: plottable[0].cap_band, d: plottable[0].dist_front })}` : '没有可画点');
    ok('梯队阶梯 = 独立复算（±1 窗口钉在起级分，逐级 high/low 逐项对齐）',
       TOL === 1 && JSON.stringify(D.tier_bands) === JSON.stringify(expLadder),
       `爬虫 ${JSON.stringify((D.tier_bands || []).slice(0, 3))} vs 复算 ${JSON.stringify(expLadder.slice(0, 3))}`);
    ok('每个模型的 cap_band = 它分数在阶梯里的级号',
       D.models.filter(m => PRODUCED(m) && m.index != null)
          .every(m => m.cap_band === rankOf(m.index, expLadder)),
       D.models.filter(m => PRODUCED(m) && m.index != null &&
         m.cap_band !== rankOf(m.index, expLadder)).slice(0, 3)
         .map(m => `${m.name}:${m.index}→${m.cap_band}(应 ${rankOf(m.index, expLadder)})`).join(' '));
    ok('梯队确实由分数分布切出：级内不超窗口、级间确实超出窗口、且铺满所有分数',
       (D.tier_bands || []).every(b => b.high - b.low <= TOL)
       && (D.tier_bands || []).every((b, i) => i === 0 || expLadder[i - 1].high - b.high > TOL)
       && (D.tier_bands || []).length > 1
       && (D.tier_bands || []).reduce((s, b) => s + 1, 0) === expLadder.length,
       // 断口只存在于"起级分之间"：级内可以宽到 tol，所以下一级的起级分必须比上一级的
       // **起级分**低超过 tol —— 拿上一级的最低分去比会误报（今天 54–53 与 52–51 就只差 1）。
       `级数 ${D.tier_bands.length}；${(D.tier_bands || []).slice(0, 5).map(b => `${b.low}–${b.high}`).join(' | ')}`);
    ok('前沿点距离恒为 0，非前沿距离 ≥ 0 且确有 >0 的',
       plottable.every(m => (m.on_frontier ? m.dist_front === 0 : m.dist_front >= 0))
       && (!plottable.length || plottable.some(m => !m.on_frontier && m.dist_front > 0)),
       `最小正距离 ${Math.min(...plottable.filter(m => !m.on_frontier).map(m => m.dist_front || 1e9))}`);
    ok('不可画的点（缺分或缺成本）dist 为 null、不算前沿',
       D.models.filter(m => m.index === null || !(m.apt_usd > 0))
              .every(m => m.dist_front === null || m.dist_front === undefined
                            ? !m.on_frontier : false));

    // ↓ 图例的两级排序：payload 自己得说得通，看板 DOM 也得按它排
    // 用量源可用时口径是 Σ(index×log1p(平衡用量)×dist)/Σ(index×log1p(平衡用量))；
    // 缺用量的行分子分母都不进。只有整源失败（usage.ok=false）才回退到指数加权。
    const useUsage = !!(D.usage && D.usage.ok);
    const agg = (D.legend || []).map(r => {
      const ms = D.models.filter(m => m.creator === r.creator && m.index != null
                                      && typeof m.dist_front === 'number' && PRODUCED(m));
      const used = ms.map(m => {
        if (!useUsage) return [m.index, m];
        const u = m.usage_balanced;
        return (typeof u === 'number' && u > 0) ? [m.index * Math.log1p(u), m] : null;
      }).filter(Boolean);
      const sw = used.reduce((s, [w]) => s + w, 0);
      const sd = used.reduce((s, [w, m]) => s + w * m.dist_front, 0);
      const plotted = D.models.filter(m => m.creator === r.creator && m.index != null
                                           && m.apt_usd > 0 && PRODUCED(m));
      const bests = plotted.map(m => m.index);
      const bestsAll = D.models.filter(m => m.creator === r.creator && m.index != null
                                            && PRODUCED(m)).map(m => m.index);
      return { creator: r.creator, band: r.band, orig: r.dist_weighted,
               w: sw ? +(sd / sw).toFixed(4) : null, uw: used.length,
               usage_rows: r.usage_rows, plotted: r.plotted,
               best: bests.length ? Math.max(...bests) : null,
               best_all: bestsAll.length ? Math.max(...bestsAll) : null,
               best_field: r.best_index, best_all_field: r.best_index_all };
    });
    ok('图例的加权距离 = 用该厂每个点的 指数×log1p(平衡用量) 独立复算',
       agg.every(r => r.w === null || r.w === r.orig),
       agg.filter(r => r.w !== r.orig).map(r => `${r.creator} ${r.orig}≠${r.w}`).join(' '));
    ok('图例的 usage_rows = 实际进入加权平均的行数（缺用量的行不算）',
       agg.every(r => r.usage_rows === r.uw),
       agg.filter(r => r.usage_rows !== r.uw)
          .map(r => `${r.creator} ${r.usage_rows}≠${r.uw}`).join(' '));
    ok('图例的梯队 = 该厂最高分在阶梯里的级号',
       agg.every(r => r.band === (r.best === null ? null : rankOf(r.best, expLadder))),
       agg.filter(r => r.band !== (r.best === null ? null : rankOf(r.best, expLadder)))
          .map(r => `${r.creator} band=${r.band} best=${r.best}`).join(' '));
    ok('图例梯队只认可画点：不可画的高分不能抬档，但仍记在 best_index_all',
       agg.every(r => r.best_field === r.best && r.best_all_field === r.best_all
                      && r.plotted === D.models.filter(m => m.creator === r.creator
                           && m.index != null && m.apt_usd > 0 && PRODUCED(m)).length),
       agg.filter(r => r.best_field !== r.best || r.best_all_field !== r.best_all)
          .map(r => `${r.creator} best=${r.best_field}/${r.best} all=${r.best_all_field}/${r.best_all}`).join(' '));
    const lgSorted = (() => {
      const L0 = D.legend || [];
      for (let i = 1; i < L0.length; i++) {
        const a = L0[i - 1], b = L0[i];
        const ka = [a.band == null ? 1 : 0, a.band ?? 1e9,
                    a.dist_weighted ?? 9e9], kb = [b.band == null ? 1 : 0, b.band ?? 1e9,
                    b.dist_weighted ?? 9e9];
        for (let j = 0; j < 3; j++) { if (ka[j] < kb[j]) break; if (ka[j] > kb[j]) return i; }
      }
      return -1;
    })();
    ok('图例列表本身按「梯队升序（0=最高）→ 档内加权距离升序」排', lgSorted < 0,
       lgSorted < 0 ? '' : `第 ${lgSorted + 1} 条与前一条倒挂`);
    const escJ = s => String(s).replace(/[&<>"']/g,
                       c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const expLegend = D.models.filter(m => m.tok_s > 0).slice().sort((a, b) =>
        ((a.cap_band ?? 1e9) - (b.cap_band ?? 1e9))
        || ((a.dist_front ?? 9e9) - (b.dist_front ?? 9e9))
        || String(a.name).localeCompare(String(b.name))).map(m => escJ(m.name));
    const domLegend = [...L.matchAll(/class="lg" data-k="([^"]*)"/g)].map(m => m[1]);
    ok('看板图例 DOM 顺序 = 梯队↑（0=最高）+ 离前沿距离↑（逐项对齐，不是只数个数）',
       expLegend.length === 0 || (domLegend.length === expLegend.length
         && domLegend.join('|') === expLegend.join('|')),
       `DOM 前 3 ${domLegend.slice(0, 3).join(' / ') || '—'} vs 期望 ${expLegend.slice(0, 3).join(' / ') || '—'}`);

    // ↓ 推估这一整套判据：以前"缺 tok_s 就画占位环"没被任何断言覆盖，
    //   于是"图上看着有个圆"与"它到底是不是实测"完全对不上账。
    const estModels = D.models.filter(m => !m.tok_s && m.tok_s_est);
    const plottableEst = estModels.filter(m => m.index !== null && m.apt_usd > 0);
    ok('推估行：tok_s 保持为空（没被估计值顶掉）',
       estModels.every(m => m.tok_s === null || m.tok_s === undefined));
    ok('推估行：都带 basis / 参照数 / 区间 / 说明',
       estModels.every(m => m.tok_s_est_basis && m.tok_s_est_n >= 1
                            && Array.isArray(m.tok_s_est_range) && m.tok_s_est_range.length === 2
                            && typeof m.tok_s_est_note === 'string' && m.tok_s_est_note.length > 4));
    ok('推估行：区间含住点估计（环带不能画在点外面）',
       estModels.every(m => m.tok_s_est_range[0] <= m.tok_s_est && m.tok_s_est <= m.tok_s_est_range[1]),
       estModels.map(m => `${m.tok_s_est}∈[${m.tok_s_est_range}]`).join(' '));
    const estGroups = (S.match(/data-est="1"/g) || []).length;
    ok('图上推估点 = 可画的推估行数', estGroups === plottableEst.length, `${estGroups} vs ${plottableEst.length}`);
    // 每个 data-est 组里必须同时有：虚线描边 + 至少一圈区间环 + tooltip 写明是估计
    const estBlocks = [...S.matchAll(/<g class="mk est(?: [^"]+)?"[^>]*>([\s\S]*?)<\/g>/g)].map(m => m[1]);
    ok('每个可画推估点都有虚线 + 区间环，零样本不伪造推估点',
       estBlocks.length === plottableEst.length && estBlocks.every(b =>
         /stroke-dasharray/.test(b) && /stroke-opacity="\.38"/.test(b)),
       `${estBlocks.length} vs ${plottableEst.length}`);
    ok('推估点 tooltip 明说「≈…（估计…」', estBlocks.every(b => /≈[^<]*t\/s（估计/.test(b)));
    const speedEstTags = speedCells.filter(c => c.includes('>估<')).length;
    ok('表格速度列「估」的个数 = 推估行数（不多不少）',
       speedEstTags === estModels.length,
       `${speedEstTags} vs ${estModels.length}`);
    if (kind === 'bandplant') {
      const plantedMark = estBlocks.find(b => b.includes('<title>Planted Est ·'));
      const plantedRow = rowCells.find(cells => cells[0]?.includes('Planted Est'));
      const plantedSpeed = plantedRow?.[iTokCell] ?? '';
      ok('固定推估样本画虚线与区间环，速度列显示 ≈66 和「估」',
         !!plantedMark && /stroke-dasharray/.test(plantedMark)
           && /stroke-opacity="\.38"/.test(plantedMark)
           && /≈\s*66/.test(plantedSpeed) && plantedSpeed.includes('>估<'), plantedSpeed);
    }
    ok('实测行不会被标成「估」（tok_s>0 的行不含 ≈）',
       estModels.every(m => m.tok_s === null || m.tok_s === undefined)
       && speedCells.filter(c => c.includes('≈')).length === estModels.length,
       `速度列 ≈ 出现 ${speedCells.filter(c => c.includes('≈')).length} 次`);
    const carrModels = D.models.filter(m => m.carried_asof);
    // 只有画得出来的行才该有标记：沿用行也可能缺成本/指数（与 est 那几条同一套可画判据）
    const carrPlottable = carrModels.filter(m => m.index !== null && m.apt_usd > 0);
    const carrBlocks = S.split('<g class="mk').filter(b => /data-carry="/.test(b));
    ok('沿用行在图上都挂了 data-carry（撤下不等于消失，但必须看得出是旧读数）',
       carrBlocks.length === carrPlottable.length, `${carrBlocks.length} vs ${carrPlottable.length}`);
    // split 把 `<g class="mk` 吃掉了，所以块头剩下的就是类名尾巴：判 ` est carr"` / ` carr"` 这种开头
    // 状态栏那张 chart.html 是另一个渲染面：它掉标记的话这条看不见（上一版就是这样）
    ok('沿用点都带 carr 类（淡一档但仍画得见，不靠隐藏说事）',
       carrBlocks.every(b => /^\s*[a-z ]*carr"/.test(b)),
       carrBlocks.length ? carrBlocks.map(b => b.slice(0, 12).trim()).join(' ') : '本轮没有沿用行');
    ok('沿用点的 tooltip 明说「沿用 MM-DD 的实测值」',
       carrBlocks.every(b => /（沿用 \d\d-\d\d 的实测值/.test(b.split('</g>')[0])),
       carrBlocks.length ? '每点都标' : '本轮没有沿用行');
    ok('沿用点仍按实测值定位（不是 est/band 那套虚线语义）',
       carrModels.every(m => m.tok_s === null || m.tok_s_est === undefined || m.tok_s),
       carrModels.map(m => `${m.name}:${m.tok_s ?? 'null'}`).join(' '));
    // ↓ 第四档「同级能力参照带」：前三档全落空才有它，只有范围、没有中心值。
    //   这一套判据在 payload 里没有带行时是空转的（every 对空集恒真），所以配一条
    //   "没有带就不许出现带的标记"的反向断言，防止图上有幽灵带而数据里没有。
    const bandModels = D.models.filter(isBandM);
    const plottableBand = bandModels.filter(m => m.index !== null && m.apt_usd > 0);
    ok('带行：既无实测也无点估计（第四档只在前三档落空时才算）',
       bandModels.every(m => !m.tok_s && !m.tok_s_est));
    ok('带行：区间是两元升序，且参照数够 PEER_MIN_ROWS',
       bandModels.every(m => Array.isArray(m.tok_s_band) && m.tok_s_band.length === 2
                            && m.tok_s_band[0] <= m.tok_s_band[1]
                            && m.tok_s_band_n >= 3 && typeof m.tok_s_band_note === 'string'
                            && m.tok_s_band_note.length > 4));
    ok('带行：绝不写进 tok_s_est（一条区间不能冒充一个值）',
       bandModels.every(m => m.tok_s_est === null || m.tok_s_est === undefined));
    const bandGroups = (S.match(/data-band="1"/g) || []).length;
    ok('图上带点数 = 可画的带行数', bandGroups === plottableBand.length,
       `${bandGroups} vs ${plottableBand.length}`);
    ok('没有可画的带行时图上不冒带标记（防幽灵）',
       (plottableBand.length > 0) === (bandGroups > 0)
       && (bandModels.length > 0 || !B.includes('>带<')),
       `带行 ${bandModels.length}（可画 ${plottableBand.length}），标记 ${bandGroups}`);
    const bandBlocks = S.split('<g class="mk band"').slice(1).map(b => b.split('</g>')[0]);
    // 中心必须**有淡填充**（不能掏空到比"没测到速度"的占位环还不显眼），又要明显淡于
    // 推估点与实测点：三档浓淡排序就是这条判据，写成数字比较才拦得住"手滑填成 .55"。
    const foOf = b => { const m = b.match(/fill-opacity="([\d.]+)"/); return m ? +m[1] : null; };
    ok('每个带点都画了同心环带',
       bandBlocks.every(b => /stroke-opacity="\.38"[^>]*fill="none"|fill="none"[^>]*stroke-opacity="\.38"/.test(b)),
       `${bandBlocks.length} 个`);
    ok('带点中心淡于推估点、推估淡于实测（三档浓淡可分辨）',
       estBlocks.every(b => { const fo = foOf(b); return fo !== null && fo <= 0.4; })
       && bandBlocks.every(b => { const fo = foOf(b); return fo !== null && fo > 0 && fo < 0.4; }),
       `带 ${bandBlocks.map(foOf).join('/')} 估 ${estBlocks.map(foOf).join('/')}`);
    ok('带点 tooltip 是范围而不是中心值',
       bandBlocks.every(b => !/≈/.test(b) && /（同级能力参照/.test(b)), '');
    const speedBandTags = speedCells.filter(c => c.includes('>带<')).length;
    ok('表格速度列把参照带标成「带」，个数 = 带行数',
       speedBandTags === bandModels.length,
       `${speedBandTags} vs ${bandModels.length}`);
    ok('速度列的带行不含 ≈（不会被读成一个值）',
       speedCells.filter(c => c.includes('>带<') && c.includes('≈')).length === 0,
       `${speedCells.filter(c => c.includes('>带<') && c.includes('≈')).length} 行带 ≈`);
    // ↓ 前沿排除必须按**几何**比，不能只比点数：当天的带点/推估点恰好在成本-分数两轴上
    //   被别家支配时，"漏排除"不会改变金线的长度 —— 变异实测就是这样逃过了点条数判据。
    const goldSet = new Set(goldPts.match(/[\d.]+,[\d.]+/g) || []);
    const centerOf = b => {
      const m = b.match(/cx="([\d.eE+-]*)" cy="([\d.eE+-]*)"/);
      return m ? `${m[1]},${m[2]}` : '';
    };
    const onGold = [...estBlocks, ...bandBlocks].map(centerOf).filter(c => goldSet.has(c));
    ok('金线上不含任何推估/参照带点的坐标', onGold.length === 0,
       `${estBlocks.length} 推估 + ${bandBlocks.length} 带，命中金线 ${onGold.join(' ')}`);
    ok('表格行数 = 模型总数',           rows === D.models.length, `${rows} vs ${D.models.length}`);
    // 三项派生指标独立复算。时间格与页面同一条规则：先 half-up 秒到一位小数，
    // 再用同一个已舍入秒值判断 <60s，换算分钟也走 half-up；排序仍按原始秒。
    {
      const unesc = v => String(v == null ? '' : v)
        .replace(/&amp;/g, '&').replace(/&quot;/g, '"')
        .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&#39;/g, "'");
      const metricNum = v => (typeof v === 'number' && Number.isFinite(v)) ? v : null;
      const thinkOf = m => {
        const total = metricNum(m.total_s);
        if (total === null) {
          const first = metricNum(m.first_answer_s);
          return first !== null && first >= 0 ? first : null;
        }
        const tok = metricNum(m.tok_s);
        if (total <= 0 || tok === null || tok <= 0) return null;
        const value = total - 500 / tok;
        return value < -1 ? null : Math.max(0, value);
      };
      const halfUp1 = v => Math.floor(v * 10 + 0.5) / 10;
      const timeCell = v => {
        if (typeof v !== 'number' || !Number.isFinite(v) || v < 0) return '—';
        const seconds = halfUp1(v);
        if (seconds < 60) return String(+(seconds).toFixed(1)) + 's';
        return String(+(halfUp1(seconds / 60)).toFixed(1)) + 'min';
      };
      const timeSpanCell = (lo, hi) => {
        const a = timeCell(lo), b = timeCell(hi);
        return a === b ? a : a + '–' + b;
      };
      const taskPairOf = v => {
        if (!Array.isArray(v) || v.length !== 2) return null;
        const lo = metricNum(v[0]), hi = metricNum(v[1]);
        return (lo !== null && hi !== null && lo > 0 && hi >= lo) ? [lo, hi] : null;
      };
      // 与看板 taskTextOf 同一优先级：官方（允许 0）→ 估计点（≈）→ 只有区间。
      const taskTextOf = m => {
        const off = metricNum(m.time_per_task_s);
        if (off !== null && off >= 0) return timeCell(off);
        const est = metricNum(m.time_per_task_est_s);
        if (est !== null && est > 0) return '≈' + timeCell(est);
        const band = taskPairOf(m.time_per_task_est_range) || taskPairOf(m.time_per_task_band);
        return band ? timeSpanCell(band[0], band[1]) : '—';
      };
      // 思考前摇 = 思考时间 / 任务耗时 × 100：只做除法，不夹到 100%，
      // 任一侧缺失/非有限/任务 <= 0 就留空；显示 half-up 一位小数并带 %。
      const ratioOf = m => {
        const think = thinkOf(m), task = metricNum(m.time_per_task_s);
        if (think === null || task === null || task <= 0) return null;
        const v = think / task * 100;
        return Number.isFinite(v) ? v : null;
      };
      // trim .0：与 Python think_ratio_text 一致（50.0→50%，0.0→0%），非零小数保留一位。
      const ratioCell = v => v === null ? '—' : halfUp1(v).toFixed(1).replace(/\.0$/, '') + '%';
      // 思考两列在整份数据都取不到时整列不渲染（而不是留一列破折号），
      // 所以列位置要跟着算，不能写死 4/5/7。
      const hasThink = D.models.some(m => thinkOf(m) !== null);
      const hasRatio = D.models.some(m => ratioOf(m) !== null);
      const iTask = 3;
      const iThink = hasThink ? 4 : -1;
      const iRatio = hasRatio ? 4 + (hasThink ? 1 : 0) : -1;
      const iTtft = 4 + (hasThink ? 1 : 0) + (hasRatio ? 1 : 0) + 1;
      const parsed = B.split('<tr>').slice(1).map(tr => {
        const tds = [...tr.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map(m => m[1]);
        const name = (tds[0] || '').match(/class="nm">([\s\S]*?)<\/span>/);
        return { name: name ? unesc(name[1]) : '', tds };
      });
      const metricBad = [];
      for (const m of D.models) {
        const row = parsed.find(r => r.name === m.name);
        if (!row) { metricBad.push(`${m.name}：找不到表格行`); continue; }
        const wantTask = taskTextOf(m);
        if (row.tds[iTask].replace(/<[^>]*>/g, '') !== wantTask)
          metricBad.push(`${m.name}：任务耗时 ${row.tds[iTask]} ≠ ${wantTask}`);
        if (hasThink) {
          const wantThink = timeCell(thinkOf(m));
          if (row.tds[iThink] !== wantThink)
            metricBad.push(`${m.name}：思考时间 ${row.tds[iThink]} ≠ ${wantThink}`);
        }
        if (hasRatio) {
          const wantRatio = ratioCell(ratioOf(m));
          if (row.tds[iRatio] !== wantRatio)
            metricBad.push(`${m.name}：思考前摇 ${row.tds[iRatio]} ≠ ${wantRatio}`);
        }
        const wantTtft = timeCell(metricNum(m.ttft_s));
        if (row.tds[iTtft] !== wantTtft)
          metricBad.push(`${m.name}：首块延迟 ${row.tds[iTtft]} ≠ ${wantTtft}`);
      }
      ok('完整数据表时间指标独立复算一致（秒/分钟自带宽、缺数/负值留空、思考前摇按 think÷task×100 派生）',
         metricBad.length === 0, metricBad.slice(0, 3).join(' | '));
    }
    ok('滚动条四层深色 + html 有底色',  /html\s*\{[^}]*background:/.test(html)
        && ['::-webkit-scrollbar ', '::-webkit-scrollbar-track', '::-webkit-scrollbar-thumb']
             .every(x => html.includes(x)));
    ok('图例 icon 撑满容器',              /\.lg \.ring img\{[^}]*width:100%[^}]*height:100%/.test(html));
    // ↓ 两条以前都没有覆盖到，所以"散点 icon 边长只给了 r（直径的 50%）"和
    //   "ringInk 被裸插成非法属性"都能一路绿灯。表层的 CSS 规则在，不代表每个渲染面都对。
    // 圆里现在带了 <title>（与 Python 渲染面同构），所以圆与 image 之间允许出现 title；
    // 但仍要求 image 紧跟在这个 <g> 里，不能跨组匹配（跨组会张冠李戴）。
    const bub = [...S.matchAll(/<circle [^>]*r="([\d.eE+-]+)"[^>]*>(?:<title>[^<]*<\/title>)?<\/circle>\s*<image [^>]*width="([\d.eE+-]+)" height="([\d.eE+-]+)"([^>]*)>/g)];
    const undersized = bub.filter(([, r, w, h]) =>
      Math.abs(+w - 2 * +r) > 1e-6 || Math.abs(+h - 2 * +r) > 1e-6);
    ok('散点 icon 边长 = 气泡直径（2r）', bub.length > 0 && undersized.length === 0,
      `${bub.length - undersized.length}/${bub.length} 达标`);
    ok('散点 icon 有圆形裁剪且不裸插属性', bub.every(m =>
      /clip-path="url\(#bubclip\)"/.test(m[4]) && !/\sfilter:/.test(m[4]) && /style="/.test(m[4])));
    ok('表格 icon 随字号动态而非写死 px', /\.mcell img\{[^}]*width:[\d.]+em/.test(html));
    // ↓ 显示层不能篡改数值。曾经 fmt 在 d=0 时把**有效**尾零也吃掉（90→"9"、100→"1"），
    //   底层数值和排序都是对的，只有渲染错 —— 所以判据必须看渲染产物，不能只测函数存在。
    const tokCells = [...B.matchAll(/<td>([^<]*)<\/td>/g)].map(m => m[1]);
    const roundTok = D.models.filter(m => m.tok_s >= 10 && m.tok_s < 1000)
                            .map(m => String(Math.round(m.tok_s)));
    const lostZero = roundTok.filter(s => s.length > 1 && s.endsWith('0') && !tokCells.includes(s));
    ok('整十的 tok/s 未被截尾零', lostZero.length === 0,
      lostZero.length ? `缺 ${lostZero.slice(0,4).join(',')}（被截成个位）` : `${roundTok.length} 个 tok 单元格逐一对上`);
    const aptCells = [...B.matchAll(/<td>\$([^<]*)<\/td>/g)].map(m => m[1]);
    ok('极小成本未塌成 0', D.models.filter(m => m.apt_usd > 0 && m.apt_usd < 0.01)
      .every(m => aptCells.some(s => parseFloat(s) > 0)),
      `最便宜 ${Math.min(...D.models.map(m => m.apt_usd))}`);

    // ↓ 无 tok/s 的点用「占位尺寸」：以前落到恒定 R_MIN=2，比最慢的真实气泡小一半多，
    //   看着像另一种标记；现在要求它紧跟最小真实气泡之下、且为整数。
    //   判据读渲染出来的 r，不读源码常量 —— 常量对、画错的情况上次就是靠这个抓出来的。
    // 判据不写死标签嵌套（气泡外面现在包了一层 <g class="mk" data-k>）。
    // 圆与 title 必须**同一个元素内相邻**：以前用 [\s\S]*? 跨元素配对，推估点加了
    // 区间环之后会把环的半径配上气泡的 title —— 半径张冠李戴而判据照样绿灯。
    const marks = [...S.matchAll(/<circle[^>]*\br="([\d.eE+-]+)"[^>]*><title>([^<]*)<\/title>/g)];
    const isNode = t => /占位尺寸/.test(t);
    const isEst  = t => /（估计/.test(t);
    const isBandT = t => /（同级能力参照/.test(t);
    const nodeR = marks.filter(([, , t]) => isNode(t)).map(([, r]) => +r);
    const bandR = marks.filter(([, , t]) => isBandT(t)).map(([, r]) => +r);
    const realR = marks.filter(([, , t]) => !isNode(t) && !isEst(t) && !isBandT(t)).map(([, r]) => +r);
    const nNodeExp = D.models.filter(m => m.index !== null && m.apt_usd > 0
                                        && !m.tok_s && !m.tok_s_est && !m.tok_s_band).length;
    ok('占位气泡数 = 无 tok/s 的可绘制模型数', nodeR.length === nNodeExp,
      `${nodeR.length} vs ${nNodeExp}`);
    ok('占位气泡没反过来比最慢的真实气泡大', realR.length === 0 ||
      nodeR.every(r => r < Math.min(...realR)),
      `占位 ${[...new Set(nodeR)].join('/')} vs 最小真实 ${realR.length ? Math.min(...realR).toFixed(2) : '—'}`);
    // 独立复算一遍页面该算出的那一号（14 是这个画布的 SCAT_R_MAX，2 是 R_MIN 兜底）
    const tpsAll = D.models.map(m => m.tok_s || 0).filter(v => v > 0);
    const tmaxAll = Math.max(...tpsAll, 1);
    const expNode = tpsAll.length
      ? Math.max(1, Math.ceil(Math.min(...tpsAll.map(v => Math.max(2, 14 * Math.sqrt(v / tmaxAll))))) - 1) : 1;
    ok('占位尺寸是「最小真实气泡再低一号」的整数，不是恒定的 2',
      nodeR.every(r => Number.isInteger(r) && r === expNode), `期望 ${expNode}`);
    // 带点的半径也必须等于占位那一号：它没有任何速度值可依据，
    // 一旦被按区间定径，"环带 = 不确定度"就变成"气泡大小 = 又一个假实测值"。
    ok('带点用占位尺寸（没被任何速度值定过半径）',
      bandR.length === plottableBand.length
      && (bandR.length === 0 || bandR.every(r => r === expNode)),
      `带 ${[...new Set(bandR)].join('/')} vs 占位 ${expNode}`);
    // ↓ 描边 = 厂商标识色。原先描边取 logo 自己的 theme，黑白 logo 的厂商（SpaceXAI、
    //   StepFun、Z AI）theme 就是灰，图上跟「其他」分不开；现在消费爬虫派生的 brand_color。
    const strokes = [...S.matchAll(/<circle[^>]*\bstroke="(#[0-9A-Fa-f]{6})"/g)].map(m => m[1].toUpperCase());
    const derived = new Set(Object.values(D0.brand_color || {}));
    ok('散点描边用的是派生厂商色', strokes.length > 0 && strokes.every(c => derived.has(c)),
      `${new Set(strokes).size} 种颜色`);
    ok('散点描边没有通用灰', !strokes.includes('#64748B'), strokes.filter(c => c === '#64748B').length + ' 个灰边');

    // ↓ 能力线：同族同代次的档位阶梯。判据读渲染出来的折线，不读分组函数是否存在。
    const drawable = D.models.filter(m => m.index !== null && m.apt_usd > 0);
    const grp = {};
    drawable.forEach(m => { (grp[m.series_key || m.name] = grp[m.series_key || m.name] || []).push(m); });
    const expLines = Object.values(grp).filter(g => g.length > 1);
    // 折线现在带 class/data-k，属性顺序不再固定 → 取整段属性串自己解析
    const polys = [...S.matchAll(/<polyline\b([^>]*)>/g)].map(m => {
      const a = m[1];
      return [(a.match(/points="([^"]*)"/) || [, ''])[1],
              (a.match(/stroke="([^"]*)"/) || [, ''])[1].toUpperCase(),
              /stroke-dasharray/.test(a)];
    });
    const gold = '#e3b341';
    const ser = polys.filter(p => !p[2] && p[1]);   // 没有 stroke-dasharray 的都是能力线
    ok('能力线条数 = 有 ≥2 个档位的可绘制族数', ser.length === expLines.length,
      `${ser.length} 条 vs ${expLines.length} 族`);
    ok('金色虚线只有 Pareto 前沿一条',
       polys.filter(p => p[2]).length === 1 && polys.filter(p => p[1] === gold).length <= 1, '');
    // 顶点数按多重集比（渲染顺序不保证和数据顺序一致）
    const vCount = ser.map(p => p[0].trim().split(/\s+/).length).sort((a, b) => a - b);
    const eCount = expLines.map(g => g.length).sort((a, b) => a - b);
    ok('每条能力线的顶点数 = 该族档位数',
      vCount.join() === eCount.join(), `${vCount.join('/')} vs ${eCount.join('/')}`);
    ok('能力线按固定推理档位逐顶点连接', tierLinesMatch(S, grp), '');
    ok('能力线用厂商色而不是金色（不与前沿混）',
      ser.every(p => derived.has(p[1])), [...new Set(ser.map(m => m[1]))].join(','));
    // 虚线现在有两种含义：Pareto 前沿（折线）与推估气泡（虚线描边 + 环带），
    // 所以"整张图只有 1 处 dash"不再成立，也不能靠放宽到 4 蒙过去 —— 只数**折线**。
    ok('只有 Pareto 前沿这条折线是虚线', (S.match(/<polyline[^>]*stroke-dasharray/g) || []).length === 1,
       `折线虚线 ${(S.match(/<polyline[^>]*stroke-dasharray/g) || []).length} 条`);
  }

  if (kind === 'stale') {
    ok('数据照常渲染（图没空）',        circles > 0 && rings > 0, `气泡 ${circles} 图例 ${rings}`);
    ok('胶囊标黄 warn 而不是红 bad',    warns === 1 && bads === 0, `warn ${warns} bad ${bads}`);
    ok('横幅写明用的是哪个快照',        /03:06:22/.test(BAN));
    ok('横幅可关闭',                   /class="x"/.test(BAN));
  }
  if (kind === 'degraded') {
    // 主源好、副源挂：数据必须照常渲染，但要**看得见地**说不是完全成功（P-28 的静默面）。
    ok('降级时数据照常渲染（图没空）', circles > 0 && rings > 0, `气泡 ${circles} 图例 ${rings}`);
    ok('降级的源胶囊如实标红（挂了就是挂了，不装绿）', bads === 2 && warns === 0,
       `warn ${warns} bad ${bads}`);
    ok('降级横幅弹出来并点名是哪个源', BAN.length > 0 && /OpenRouter/.test(BAN), BAN.slice(0, 60));
    ok('降级横幅可关闭', /class="x"/.test(BAN));
  }
  if (kind === 'normal') {
    ok('一切正常时不弹横幅',            BAN.length === 0);
    ok('胶囊全绿',                     warns === 0 && bads === 0);
  }
}

/* ---------- 显示位数：速度整数、延迟两位，位数表来自载荷（不靠今天恰好有变动） ---------- */
{
  const D = scenario('normal');
  D.changes = [{
    name: 'Precision Probe (max)', type: 'changed',
    fields: [
      {field: 'tok_s', label: '输出速度', from: 172, to: 123.289456, pct: -28.3, delta: -48.7},
      {field: 'ttft_s', label: '首块延迟', from: 150.4, to: 69.7192284307624, pct: -53.6, delta: -80.7},
      {field: 'apt_usd', label: '每任务成本', from: 1.0, to: 1.5, pct: 50, delta: 0.5},
    ],
  }, {
    name: 'Precision Probe (low)', type: 'changed',
    fields: [
      // 「这次缺失」那条走 from，尾零规则要同样成立：150.40 显示成 150.4
      {field: 'ttft_s', label: '首块延迟', kind: 'disappeared', from: 150.40, to: null, pct: null, delta: null},
      {field: 'tok_s', label: '输出速度', kind: 'appeared', from: null, to: 67.76316669225746, pct: null, delta: 67.76},
    ],
  }];
  const CHG = (render(D).changes?.innerHTML ?? '');
  ok('徽标把速度收成整数（不是 123.29，更不是 123.289456）',
     /123 \(/.test(CHG) && !/123\.\d/.test(CHG), CHG.slice(0, 240));
  ok('徽标把延迟给两位', /69\.72 \(/.test(CHG), CHG.slice(0, 240));
  ok('缺失那条也按位数表收（原 150.4，不写 150.40）', /原 150\.4）/.test(CHG), CHG.slice(0, 240));
  ok('新增那条同样收位（新增 68）', /新增 68</.test(CHG), CHG.slice(0, 240));
  ok('没进位数表的字段保持原写法', /1\.5 \(/.test(CHG), CHG.slice(0, 240));
  const D2 = structuredClone(D);
  delete D2.display_decimals;          // 改动前生成的旧快照：退回两位，与改动前逐字一致
  ok('载荷没这张表时退回旧写法（不报错、不显示 —）',
     /123\.29 \(/.test(render(D2).changes?.innerHTML ?? ''));
}

/* ---------- 思考前摇：独立公式、边界与数值排序（不靠真实数据恰好有值） ---------- */
{
  const src = D0.models.find(m => m.index !== null && m.apt_usd > 0 && m.tok_s > 0);
  const plant = (name, task, total, tok = 100, firstAnswer) => {
    const m = structuredClone(src);
    Object.assign(m, { name, series_key: 'ratio-' + name, series: name,
                       time_per_task_s: task, total_s: total, tok_s: tok, ttft_s: 0 });
    // 克隆源可能带着真实载荷的 first_answer_s，那会让"缺 total_s 就留空"的 case 走兜底，
    // 判据测的就不是它想测的分支了。这里一律先清掉，真要兜底的 case 再显式传值。
    for (const k of ['tok_s_est', 'tok_s_est_range', 'tok_s_est_basis', 'tok_s_est_n',
                     'tok_s_est_note', 'tok_s_band', 'tok_s_band_n', 'tok_s_band_same_creator',
                     'tok_s_band_note', 'first_answer_s', 'first_answer_s_recovered_from',
                     'time_per_task_est', 'time_per_task_est_s', 'time_per_task_est_range',
                     'time_per_task_est_basis', 'time_per_task_band'])
      delete m[k];
    if (firstAnswer !== undefined) m.first_answer_s = firstAnswer;
    return m;
  };
  // think = total − 500/tok（tok=100 即 −5）；ratio = think/task×100。
  const cases = [
    ['Ratio ZeroThink', 100, 5, 100, '0%'],            // think 0 有效；.0 去掉
    ['Ratio Ninet', 100, 960, 100, '955%'],            // >100% 不夹到 100
    ['Ratio HalfUp', 100, 6.25, 100, '1.3%'],          // 1.25 half-up 到 1.3
    ['Ratio Half', 200, 105, 100, '50%'],              // 50.0→50，与 Python 对齐
    ['Ratio MissingThink', 100, null, 100, '—'],
    ['Ratio MissingTask', null, 105, 100, '—'],
    ['Ratio ZeroTask', 0, 105, 100, '—'],
    ['Ratio NegTask', -5, 105, 100, '—'],
    ['Ratio NaNTask', NaN, 105, 100, '—'],
    ['Ratio InfTask', Infinity, 105, 100, '—'],
  ];
  const D = structuredClone(D0);
  D.models = D.models.concat(cases.map(([n, t, total, tok]) => plant(n, t, total, tok)));
  D.data_state = 'ok';
  D.data_note = '';
  const s = render(D, false);
  const cells = new Map();
  for (const tr of (s.tb?.innerHTML ?? '').split('<tr>').slice(1)) {
    const tds = [...tr.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map(m => m[1]);
    const nm = (tds[0] || '').match(/class="nm">([\s\S]*?)<\/span>/);
    if (nm) cells.set(nm[1], tds[5]);
  }
  const bad = cases.filter(([n, , , , want]) => cells.get(n) !== want)
    .map(([n, , , , want]) => `${n}: ${cells.get(n)} ≠ ${want}`);
  ok('思考前摇 = think÷task×100：0 有效、>100 不夹、half-up 一位小数且 .0 与 Python 同去尾零；缺任一侧/任务<=0 留空',
     bad.length === 0, bad.join(' | '));
  // 排序按原始百分比降序：955 > 50 > 1.3 > 0，缺失项一律排最后。
  const thRatio = Object.values(s).find(o => o && o.dataset && o.dataset.k === 'think_ratio');
  const posOf = () => {
    const names = (s.tb?.innerHTML ?? '').split('<tr>').slice(1)
      .map(tr => (tr.match(/class="nm">([\s\S]*?)<\/span>/) || [])[1]).filter(Boolean);
    return n => names.indexOf(n);
  };
  let sortOk = false, sortDetail = '找不到 think_ratio 表头或没绑 onclick';
  if (thRatio && typeof thRatio.onclick === 'function') {
    // 第一次点 think_ratio 是升序（sortAsc=false 的既有语义），再点一次切到降序。
    thRatio.onclick();
    thRatio.onclick();
    const pos = posOf();
    const order = ['Ratio Ninet', 'Ratio Half', 'Ratio HalfUp', 'Ratio ZeroThink'];
    const valid = order.map(pos);
    sortOk = valid.every((p, i) => p >= 0 && (i === 0 || p > valid[i - 1]))
      && ['Ratio MissingThink', 'Ratio MissingTask', 'Ratio ZeroTask',
          'Ratio NegTask', 'Ratio NaNTask', 'Ratio InfTask']
           .every(n => pos(n) > valid[valid.length - 1]);
    sortDetail = order.map((n, i) => `${n}@${valid[i]}`).join(' ');
  }
  ok('按思考前摇排序用原始百分比（955>50>1.3>0），缺失项排在最后', sortOk, sortDetail);
  ok('旧 first500 排序键已移除（think_ratio 存在，tps500 不存在）',
     !!thRatio && !Object.values(s).some(o => o && o.dataset && o.dataset.k === 'tps500'), '');
}

/* ---------- 思考两列：total_s 缺失退回首回答延迟；都没有时整列不渲染 ---------- */
{
  const src = D0.models.find(m => m.index !== null && m.apt_usd > 0 && m.tok_s > 0);
  const plant = (name, task, total, first, tok = 100) => {
    const m = structuredClone(src);
    Object.assign(m, { name, series_key: 'fb-' + name, series: name,
                       time_per_task_s: task, total_s: total, first_answer_s: first,
                       tok_s: tok });
    for (const k of ['tok_s_est', 'tok_s_est_range', 'tok_s_est_basis', 'tok_s_est_n',
                     'tok_s_est_note', 'tok_s_band', 'tok_s_band_n', 'tok_s_band_same_creator',
                     'tok_s_band_note',
                     // 派生任务耗时也一起清掉：不清的话克隆行会带着真实载荷的估计，
                     // "官方缺失→留空"这类 case 就悄悄走了另一条分支。
                     'time_per_task_est', 'time_per_task_est_s', 'time_per_task_est_range',
                     'time_per_task_est_basis', 'time_per_task_band'])
      delete m[k];
    return m;
  };
  const cellsOf = inner => {
    const out = new Map();
    for (const tr of inner.split('<tr>').slice(1)) {
      const tds = [...tr.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map(m => m[1]);
      const nm = (tds[0] || '').match(/class="nm">([\s\S]*?)<\/span>/);
      if (nm) out.set(nm[1], tds);
    }
    return out;
  };

  // 只有 first_answer_s：它就是首回答前等待本身，不能再减 500/tok（24.51 不是 19.51）。
  const Dfb = structuredClone(D0);
  Dfb.models = [plant('FbOnly', 100, null, 24.51)];
  Dfb.data_state = 'ok';
  Dfb.data_note = '';
  const sfb = render(Dfb, false);
  const fbc = cellsOf(sfb.tb?.innerHTML ?? '').get('FbOnly') || [];
  ok('total_s 缺失时思考时间 = first_answer_s 原值（不误减 500/tok）',
     fbc[4] === '24.5s', `tds[4]=${fbc[4]}`);
  ok('首回答延迟兜底后思考前摇 = 24.51 ÷ 100', fbc[5] === '24.5%', `tds[5]=${fbc[5]}`);
  ok('有兜底值时两列仍渲染、列数不缩水', fbc.length > 7, `${fbc.length} 列`);

  // 都没有：两列整列消失，首块延迟前移，脚注也不再讲思考口径。
  const Dnone = structuredClone(D0);
  Dnone.models = D0.models.map(m => {
    const c = structuredClone(m);
    delete c.total_s; delete c.first_answer_s;
    return c;
  });
  Dnone.data_state = 'ok';
  Dnone.data_note = '';
  const snone = render(Dnone, false);
  const firstRow = cellsOf(snone.tb?.innerHTML ?? '').values().next().value || [];
  const headsShown = Object.values(snone)
    .filter(o => o && o.dataset && typeof o.dataset.k === 'string')
    .filter(o => o.dataset.k === 'think_s' || o.dataset.k === 'think_ratio')
    .map(o => ({ k: o.dataset.k, hidden: o.hidden === true }));
  ok('没有任何思考数据时，思考两列的表头都被隐藏',
     headsShown.length >= 2 && headsShown.every(h => h.hidden), JSON.stringify(headsShown));
  ok('没有任何思考数据时，表格行也不含这两列（列数比有数据时少 2）',
     firstRow.length === 10, `${firstRow.length} 列`);
}

/* ---------- 时间格单位：秒/分钟自带宽，排序仍按原始秒 ---------- */
{
  const src = D0.models.find(m => m.index !== null && m.apt_usd > 0 && m.tok_s > 0);
  const plant = (name, tp, ttft) => {
    const m = structuredClone(src);
    Object.assign(m, { name, series_key: 'timing-' + name, series: name,
                       time_per_task_s: tp, ttft_s: ttft });
    for (const k of ['tok_s_est', 'tok_s_est_range', 'tok_s_est_basis', 'tok_s_est_n',
                     'tok_s_est_note', 'tok_s_band', 'tok_s_band_n', 'tok_s_band_same_creator',
                     'tok_s_band_note',
                     // 派生任务耗时也一起清掉：不清的话克隆行会带着真实载荷的估计，
                     // "官方缺失→留空"这类 case 就悄悄走了另一条分支。
                     'time_per_task_est', 'time_per_task_est_s', 'time_per_task_est_range',
                     'time_per_task_est_basis', 'time_per_task_band'])
      delete m[k];
    return m;
  };
  const cellOf = (inner, name) => {
    for (const tr of inner.split('<tr>').slice(1)) {
      const tds = [...tr.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map(m => m[1]);
      const nm = (tds[0] || '').match(/class="nm">([\s\S]*?)<\/span>/);
      if (nm && nm[1] === name) return tds;
    }
    return null;
  };
  // 显示边界：59.9 秒仍是秒，60 秒整切分钟，600 秒是 10 分钟，0 是 0s；
  // half-up：17.25→17.3s、59.95→1min、3599.9→60min；负数与非有限数留破折号。
  const cases = [
    ['Timing Zero', 0, 0, '0s', '0s'],
    ['Timing Sec', 59.9, 59.9, '59.9s', '59.9s'],
    ['Timing HalfUpSec', 17.25, 17.25, '17.3s', '17.3s'],
    ['Timing HalfUpMin', 59.95, 59.95, '1min', '1min'],
    ['Timing Minute', 60, 60, '1min', '1min'],
    ['Timing TenMin', 600, 600, '10min', '10min'],
    ['Timing NearHour', 3599.9, 3599.9, '60min', '60min'],
    ['Timing Negative', -1, -1, '—', '—'],
    ['Timing NonFinite', NaN, Infinity, '—', '—'],
  ];
  const D = structuredClone(D0);
  D.models = D.models.concat(cases.map(([n, tp, ttft]) => plant(n, tp, ttft)));
  D.data_state = 'ok';
  D.data_note = '';
  const s = render(D, false);
  // 思考两列在没有数据时整列不渲染，首块延迟的列位置随之前移。
  const hasThinkCol = D.models.some(m => {
    const total = typeof m.total_s === 'number' && Number.isFinite(m.total_s) ? m.total_s : null;
    if (total === null) {
      return typeof m.first_answer_s === 'number' && Number.isFinite(m.first_answer_s)
        && m.first_answer_s >= 0;
    }
    return total > 0 && typeof m.tok_s === 'number' && m.tok_s > 0;
  });
  const ttftIdx = 7 - (hasThinkCol ? 0 : 2);
  const bad = [];
  for (const [n, , , wantTask, wantTtft] of cases) {
    const tds = cellOf(s.tb?.innerHTML ?? '', n);
    if (!tds) { bad.push(`${n}：找不到表格行`); continue; }
    if (tds[3] !== wantTask) bad.push(`${n}：任务耗时 ${tds[3]} ≠ ${wantTask}`);
    if (tds[ttftIdx] !== wantTtft) bad.push(`${n}：首块延迟 ${tds[ttftIdx]} ≠ ${wantTtft}`);
  }
  ok('时间格按 <60s→秒、>=60s→分钟显示并自带单位；half-up 的 17.25/59.95/3599.9 与无效值逐一对上',
     bad.length === 0, bad.join(' | '));

  // 排序不受显示单位影响：仍按原始秒降序，10min 行在 1min 行之前，而不是按字符串排。
  const order = ['Timing TenMin', 'Timing Minute', 'Timing Sec', 'Timing Zero'];
  const D2 = structuredClone(D0);
  D2.models = order.map((n, i) => plant(n, [600, 60, 59.9, 0][i], [600, 60, 59.9, 0][i]));
  D2.data_state = 'ok';
  D2.data_note = '';
  const s2 = render(D2, false);
  const th = Object.values(s2).find(o => o && o.dataset && o.dataset.k === 'time_per_task_s');
  let sorted = [], sortOk = false;
  if (th && typeof th.onclick === 'function') {
    th.onclick();   // 第一次点：sortAsc=false，按既有语义是升序
    th.onclick();   // 再点一次才是降序
    sorted = (s2.tb?.innerHTML ?? '').split('<tr>').slice(1)
      .map(tr => (tr.match(/class="nm">([\s\S]*?)<\/span>/) || [])[1]).filter(Boolean);
    sortOk = JSON.stringify(sorted) === JSON.stringify(order);
  }
  ok('按任务耗时排序仍用原始秒（600/60/59.9/0 降序），只换显示单位', sortOk, sorted.join(' > '));
}

/* ---------- 时间格跨运行时：真 Python 函数 vs 页面真函数 ---------- */
{
  const win = render(D0, false).__mrWindow;
  const jsTimeCell = win.__mrTest && win.__mrTest.timeCell;
  const PY = existsSync(join(HERE, '.venv/bin/python'))
    ? join(HERE, '.venv/bin/python') : 'python3';
  const PY_TIME = 'import json,sys\n'
    + 'sys.path.insert(0, sys.argv[1])\n'
    + 'import render_scatter_svg as R\n'
    + 'sys.stdout.write(json.dumps([R.time_cell(v) for v in json.load(sys.stdin)]))';
  const values = [17.25, 59.95, 60, 600, 0, 3599.9];
  const want = ['17.3s', '1min', '1min', '10min', '0s', '60min'];
  let py = null, pyErr = '';
  try {
    py = JSON.parse(execFileSync(PY, ['-B', '-c', PY_TIME, HERE],
                                { input: JSON.stringify(values), encoding: 'utf8', timeout: 60000 }));
  } catch (e) {
    pyErr = String(e && e.message || e).replace(/\n/g, ' ').slice(0, 160);
  }
  const js = typeof jsTimeCell === 'function' ? values.map(v => jsTimeCell(v)) : [];
  ok('时间格跨运行时逐字一致：真 Python time_cell vs 页面真 timeCell（half-up、秒阈值、分钟同源）',
     py !== null && JSON.stringify(py) === JSON.stringify(js) && JSON.stringify(js) === JSON.stringify(want),
     pyErr || `py=${JSON.stringify(py)} js=${JSON.stringify(js)}`);
}

/* ---------- AA Progress：节点悬停必须给分或明说不可用 ---------- */
{
  const D = scenario('normal');
  const drawable = D.models.filter(m => m.index !== null && m.apt_usd > 0);
  const withScore = drawable[0];
  const without = drawable.find(m => m !== withScore);
  withScore.progress_index = 42;
  withScore.progress_version = 'v1';
  if (without) { without.progress_index = null; without.progress_version = 'v1'; }
  const s = render(D, false);
  const S = s.scatterwrap?.innerHTML ?? '';
  const esc = v => String(v).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const titles = [...S.matchAll(/<title>([^<]*)<\/title>/g)].map(m => m[1]);
  const missingTip = without ? titles.find(t => t.startsWith(esc(without.name) + ' ·')) || '' : '';
  const labelOf = m => {
    const iv = String(m.evaluation_version || D.evaluation_version || '')
      .replace(/^v/, '').replace(/\.0$/, '');
    const pv = String(m.progress_version || '').replace(/^v/, '');
    return `智能指数${iv ? 'v'+iv : ''} / 历史参照${pv ? 'v'+pv : ''}`;
  };
  const withLabel = labelOf(withScore);
  const okTip = `${withLabel}：${withScore.index} / ${Math.round(withScore.progress_index)}`;
  ok('看板节点悬停按「智能指数 / 历史参照」成对显示分数',
     S.includes(okTip), `${okTip} · ${withScore.name}`);
  const missPrefix = without ? `${labelOf(without)}：${without.index} / ` : '';
  ok('看板节点悬停对缺分写破折号，不借用别档也不写 0',
     !!missingTip && missingTip.includes(missPrefix + '—')
       && !missingTip.includes(missPrefix + '0'),
     missingTip || '没有第二个可画点');
}

/* ---------- 配色与降级字段 ---------- */
const PAL = D0.logo_palette || {};
const palOf = m => PAL[(m.logo_local||'').split('/').pop().replace(/\.(svg|png)$/i,'')] || null;
const withLogo = D0.models.filter(m => m.logo_local);
const whiteRings = withLogo.filter(m => (palOf(m)?.ring_bg || '').toUpperCase() === '#FFFFFF');
const badContrast = Object.entries(PAL).filter(([, p]) =>
  p.invert_icon && (p.contrast_icon ?? 99) < 3.0);
const noRingBg = Object.entries(PAL).filter(([, p]) => !p.ring_bg);

console.log('\n== 圆底配色（防回归） ==');
ok('没有纯白圆底（会糊在深色背景里）', whiteRings.length === 0, whiteRings.map(m=>m.name).join(',') || '无');
ok('反色 icon 与圆底对比度 >= 3.0',   badContrast.length === 0,
   badContrast.map(([k,p])=>`${k} ${p.contrast_icon}`).join(', ') || '无');
ok('每个 palette 都有 ring_bg',       noRingBg.length === 0, noRingBg.map(([k])=>k).join(',') || '无');

/* ---------- 厂商配色：由 vendor_colors 派生，不再手写表 ----------
   判据用 CIELAB ΔE 复算一遍，不 import Python 那份 —— 两边口径独立才有意义。 */
const rgb = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
const labOf = h => {
  const f = u => (u <= 0.04045 ? u / 12.92 : ((u + 0.055) / 1.055) ** 2.4);
  const [r, g, b] = rgb(h).map(v => f(v / 255));
  const x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047;
  const y = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  const z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883;
  const t = v => (v > 0.008856 ? Math.cbrt(v) : 7.787 * v + 16 / 116);
  const [fx, fy, fz] = [x, y, z].map(t);
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
};
const deltaE = (a, b) => {
  const [l1, a1, b1] = labOf(a), [l2, a2, b2] = labOf(b);
  return Math.hypot(l1 - l2, a1 - a2, b1 - b2);
};
const VCOLOR = D0.brand_color || {};
const VSRC = D0.brand_color_source || {};
const vSlugs = [...new Set(D0.models.map(m => m.creator_slug))].sort();
const vMissing = vSlugs.filter(s => !VCOLOR[s]);
const vGray = vSlugs.filter(s => (VCOLOR[s] || '').toUpperCase() === '#64748B');
const vPairs = [];
for (let i = 0; i < vSlugs.length; i++)
  for (let j = i + 1; j < vSlugs.length; j++)
    vPairs.push([deltaE(VCOLOR[vSlugs[i]], VCOLOR[vSlugs[j]]), vSlugs[i], vSlugs[j]]);
const vNear = vPairs.filter(([d]) => d < 16).sort((a, b) => a[0] - b[0]);
const top20Slugs = [...new Set(D0.models.filter(m => m.index !== null)
  .sort((a, b) => b.index - a.index).slice(0, 20).map(m => m.creator_slug))];

console.log('\n== 厂商标识色（自动派生，防回归） ==');
ok('每家厂商都有派生色',                vMissing.length === 0, vMissing.join(',') || '无');
ok('没有厂商落在通用灰 #64748B',        vGray.length === 0, vGray.join(',') || '无');
ok('厂商色两两 ΔE ≥ 16（图上分得出）',  vNear.length === 0,
   vNear.length ? vNear.slice(0, 3).map(([d, a, b]) => `${a}/${b} ΔE=${d.toFixed(1)}`).join(', ')
                : `最近的一对 ΔE=${Math.min(...vPairs.map(p => p[0])).toFixed(1)}`);
ok('指数前 20 的厂商都在配色表里',      top20Slugs.every(s => VCOLOR[s]), top20Slugs.join(','));
ok('多数厂商的色来自 logo 而非生成',    Object.values(VSRC).filter(s => s.startsWith('svg') || s === 'curated').length
                                       >= Math.ceil(vSlugs.length / 2),
   JSON.stringify(VSRC));

/* ---------- watchlist 派生词必须盖住图上每个模型 ---------- */
const norm = s => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
const KW = D0.watchlist || [];
const hitsOf = k => D0.models.filter(m => norm(k).length && norm(`${m.name} ${m.creator}`).includes(norm(k)));
const uncovered = D0.models.filter(m => !KW.some(k => hitsOf(k).includes(m)));
const deadWords = KW.filter(k => hitsOf(k).length === 0);
const rowsPerWord = KW.length ? D0.models.length / KW.length : Infinity;

console.log('\n== watchlist 派生词覆盖（防"整族静默掉表"） ==');
ok('词表非空',                          KW.length > 0, `${KW.length} 个词`);
ok('图上每个模型都被某个关键词命中',    uncovered.length === 0,
   uncovered.map(m => m.name).join(', ') || '无');
// 2026-09-29 这里原本断言「词表 ≤24 个」，拿长度当"派生质量"的代理。
// 之后 grow_watchlist 改成**只加不减**的累积增量（新家族当轮进表、删词只有
// --refresh-watchlist 才做），任何固定字数上限都必然在某天失败，而失败说明不了
// 任何质量问题 —— 2026-11-17 就撞到这条：25 个词，全是真在覆盖的行，没有一个是垃圾。
// 改成两条尺度无关的判据，仍能抓到真退化（派生出一堆不匹配的碎片、或退化成一词一行）：
ok('没有零命中的死词',                  deadWords.length === 0,
   deadWords.join(', ') || `0 个（${KW.length} 词全部命中图上行）`);
ok('词表没有退化成一词一行',            rowsPerWord >= 2.5,
   `${rowsPerWord.toFixed(2)} 行/词（${D0.models.length} 行 / ${KW.length} 词；一词一行会让派生失去压缩意义）`);

/* ---------- 能力线的分组键由爬虫单点算好 ---------- */
const noSeries = D0.models.filter(m => !m.series || !m.series_key);
console.log('\n== 能力线分组键（series / series_key / tier） ==');
ok('每个模型都带同代次族名', noSeries.length === 0, noSeries.map(m => m.name).join(', ') || '无');
{
  const g = {};
  D0.models.filter(m => m.index !== null && m.apt_usd).forEach(m => { (g[m.series_key] = g[m.series_key] || []).push(m.name); });
  const multi = Object.entries(g).filter(([, v]) => v.length > 1);
  const ver = n => (n.match(/\d+(?:\.\d+)*/g) || []).join(',');
  const mixed = multi.filter(([, v]) => new Set(v.map(m => ver(m))).size > 1);
  ok('族名保留代次（5.5 与 5 不并成一条）', mixed.length === 0,
     mixed.map(([k, v]) => `${k}: ${[...new Set(v.map(m => ver(m)))].join(' vs ')}`).join(' | ')
     || multi.map(([k, v]) => `${k}×${v.length}`).join(', '));
  ok('族内不混入别的型号',
     multi.every(([k, v]) => v.every(n => norm(n).includes(k.slice(0, 6)))), '');
}

/* ---------- 状态栏那张图（render_scatter_svg.py 的产物）必须同一套尺寸口径 ----------
   dashboard.html 和 chart.html 是两条独立实现，只测一边就会「网页对了、菜单栏还是旧的」。
   这里读运行时真产物，不读源码常量。 */
console.log('\n== 状态栏图 chart.html（尺寸口径一致性） ==');
const ENV = join(homedir(), 'Library/Application Support/ModelRadar/runtime.env');
let chartPath = null;
try {
  const m = readFileSync(ENV, 'utf8').match(/^MR_DATA="?([^"\n]+)"?/m);
  chartPath = join(m[1], 'chart.html');
} catch { /* 没装过组件就跳过 */ }
if (chartPath && existsSync(chartPath)) {
  const CH = readFileSync(chartPath, 'utf8');
  // 状态栏那张图是另一个渲染面：它掉标记的话，看板那几条判据看不见（上一版就是这样）
  const carrCh = D0.models.filter(m => m.carried_asof && m.index !== null && m.apt_usd > 0).length;
  const carryCh = (CH.match(/data-carry="/g) || []).length;
  ok('状态栏 chart.html 里的沿用点也带 data-carry（两个渲染面同一套标记）',
     !carrCh || carryCh === carrCh, `chart.html ${carryCh} 处 / 可画沿用行 ${carrCh}`);
  const ch = [...CH.matchAll(/<circle[^>]*\br="([\d.eE+-]+)"[^>]*><title>([^<]*)<\/title>/g)];
  // 四分类：占位（无数据）/ 推估（≈…（估计…））/ 参照带（…–…（同级能力参照…））/ 实测。
  // 推估与参照带都不是实测，混进"最小真实气泡"的统计就把占位尺寸的判据带歪了。
  const isChBand = t => /（同级能力参照/.test(t);
  const chBand = ch.filter(([, , t]) => isChBand(t)).map(([, r]) => +r);
  const chNode = ch.filter(([, , t]) => /无数据/.test(t) && !isChBand(t)).map(([, r]) => +r);
  const chReal = ch.filter(([, , t]) => !/无数据/.test(t) && !/（估计/.test(t) && !isChBand(t)).map(([, r]) => +r);
  const expCh = chReal.length ? Math.max(1, Math.ceil(Math.min(...chReal)) - 1) : 1;
  // 必须再套一层 load_points 的过滤：散点两个轴都得名（有 index 且有成本）才画得出来。
  // 只按载荷数会被新数据打脸 —— 2026-10-13 就有两条带点没成本，闸说 4、图只能画 2。
  const drawable = m => m.index !== null && m.apt_usd;
  const chBandModels = D0.models.filter(m => !m.tok_s && !m.tok_s_est && !!m.tok_s_band && drawable(m));
  ok('状态栏图：带点数 = payload 里的带行数',
     (CH.match(/data-band="1"/g) || []).length === chBandModels.length,
     `${(CH.match(/data-band="1"/g) || []).length} vs ${chBandModels.length}`);
  ok('状态栏图：带点外套环带、中心有淡填充但不是实色',
     CH.split('<g class="mk band"').slice(1).map(b => b.split('</g>')[0]).every(b => {
       const m = b.match(/fill-opacity="([\d.]+)"/);
       return /stroke-opacity="\.38"/.test(b) && !/≈/.test(b)
         && m && +m[1] > 0 && +m[1] <= 0.3;
     }), CH.split('<g class="mk band"').slice(1).length + ' 个');
  ok('状态栏图：脚注按带数报数（没有带就不报；数字可包在精确子集链接里）',
     chBandModels.length ? new RegExp('淡色小点\\s*(?:<span class="fgrp"[^>]*>)?<b>'
                                      + chBandModels.length + '</b>').test(CH)
                         : !/淡色小点/.test(CH),
     `带 ${chBandModels.length} 个`);
  // 金线也必须同口径：两张图用的是同一份 payload，若一边把带/推估算进前沿，
  // 用户就会看到"菜单栏的线和看板的线不一样长"而没人报警。
  const chHistoryKeys = new Set([...CH.matchAll(/<g class="mk[^>]*data-k="([^"]+)"[^>]*data-history-for=/g)].map(m => m[1]));
  const chFrontExp = D0.models
    .filter(m => !chHistoryKeys.has(m.series_key))
    .filter(m => m.index !== null && m.apt_usd && !(m.tok_s_est && !m.tok_s)
                 && !(m.tok_s_band && !m.tok_s && !m.tok_s_est))
    .slice().sort((a, b) => a.apt_usd - b.apt_usd)
    .reduce((acc, m) => (acc.some(q => q.index >= m.index) ? acc : acc.concat(m)), []);
  const chGold = (CH.match(/<polyline points="([^"]*)" data-effort-view="discrete" fill="none" stroke="#e3b341"/) || [, ''])[1];
  ok('状态栏图前沿点集 = 同一份 payload 的 Python 口径',
     (chGold.match(/[\d.]+,[\d.]+/g) || []).length === chFrontExp.length,
     `图上 ${(chGold.match(/[\d.]+,[\d.]+/g) || []).length} 点，应为 ${chFrontExp.length} 点`);
  // 同上：金线要按几何比。带/推估点当天被支配时，只比点数会漏掉"忘了排除"这种变异。
  const chNonReal = [...CH.split('<g class="mk band"').slice(1), ...CH.split('<g class="mk est"').slice(1)]
    .map(b => { const m = b.slice(0, b.indexOf('</g>')).match(/cx="([\d.]+)" cy="([\d.]+)"/); return m ? `${m[1]},${m[2]}` : ''; });
  const chOnGold = chNonReal.filter(c => (chGold.match(/[\d.]+,[\d.]+/g) || []).includes(c));
  ok('状态栏图金线上不含任何推估/参照带点的坐标', chOnGold.length === 0,
     `${chNonReal.length} 个非实测点，命中金线 ${chOnGold.join(' ')}`);
  ok('占位气泡数 = 图里既无实测也无推估/参照带的点', chNode.length ===
     D0.models.filter(m => m.index !== null && m.apt_usd && !m.tok_s && !m.tok_s_est && !m.tok_s_band).length,
     `${chNode.length} 个`);
  ok('占位 = 最小真实气泡再低一号的整数',
     chNode.every(r => Number.isInteger(r) && r === expCh) &&
     chNode.every(r => r < Math.min(...chReal)),
     `占位 ${[...new Set(chNode)].join('/')} vs 最小真实 ${chReal.length ? Math.min(...chReal).toFixed(1) : '—'}（期望 ${expCh}）`);
  ok('脚注说明了占位那一号', chNode.length === 0 || /没测到 tok\/s/.test(CH));
  // 脚注"抓取于 …"的悬停浮层：结论必须与 payload.alert 一字不差。
  // 浮层是页面自己画的（原生 title 在非活跃的 .accessory app 里不弹），所以判据也判 CSS。
  // tipMirror 是 foot_tip 的第二份实现：产物那份比"屏上文字 == 镜像"，摆出来那份比
  // "镜像 == 真 Python 的输出"（下面 PY 那一段），两边都咬得住才叫判据。
  {
    const escT = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const tipMirror = D => {
      const A = D.alert || {}, L = A.last || {};
      const raw = (D.moves || []).map(x => x && x.text).filter(Boolean);
      const mlines = (D.move_lines || raw).filter(Boolean);
      const litems = (L.lines || L.items || []).filter(Boolean);
      const CU = D.cumulative || {};
      const clines = (CU.lines || CU.items || []).filter(Boolean);
      const csteps = CU.steps;
      const haveCum = typeof csteps === 'number' && csteps >= 2 && clines.length > 0;
      const lines = [`本轮结论：${A.label || A.kind}（${A.kind}）`];
      const shown = [];
      const emit = (head, rows, nRaw, keepHead) => {
        const fresh = rows.filter(r => !shown.includes(r));
        if (!fresh.length) {
          // 与 foot_tip.emit 同一套：历史那几节的标题带"哪一轮/隔几轮"，明细同批也要留标题
          if (keepHead && rows.length) lines.push(head + '（明细与上面同批，不重列）');
          return;
        }
        fresh.forEach(r => shown.push(r));
        // 少一截的两个原因要分开说（与 foot_tip.block 同一套措辞）：同族合并 vs 被上面念过
        let tail = (typeof nRaw === 'number' && nRaw !== rows.length)
          ? `，同族合并后 ${rows.length} 条` : '';
        if (rows.length > fresh.length) tail += `（另 ${rows.length - fresh.length} 条上面已列）`;
        lines.push(head + tail);
        fresh.forEach(r => lines.push('\u00b7 ' + r));
      };
      if (A.kind === 'moved' && L.steps === 0 && mlines.length) {
        emit(`本轮达门槛的变动 ${raw.length} 项（对比 ${L.base || '上一轮'}）`, mlines, raw.length);
      } else if (A.kind === 'moved-cumulative' && haveCum) {
        emit(`累计达门槛的 ${CU.n} 项（相对 ${CU.at}，往前第 ${csteps} 轮）`, clines, CU.n);
      } else {
        lines.push(A.text || '这轮没有需要提醒的变动');
      }
      if ((D.rename_lines || []).length) emit('名称调整（不计榜单进出）', D.rename_lines, null);
      // 榜单进出：新进榜与落榜成对列（与 foot_tip 同一顺序）
      // board_io 的镜像：只读 changes 的 type（与 render_scatter_svg.board_io 同一份映射）
      const uniq = a => [...new Set(a)].sort();
      const IO = { new: uniq((D.changes || []).filter(x => x && x.type === 'new' && x.name)
                            .map(x => String(x.name))),
                   gone: uniq((D.changes || []).filter(x => x && x.type === 'gone' && x.name)
                              .map(x => String(x.name))),
                   // 沿用行既不是新进榜也不是落榜（与 render_scatter_svg.board_io 同步）
                   carried: uniq((D.changes || []).filter(x => x && x.type === 'carried' && x.name)
                                 .map(x => String(x.name))) };
      for (const [k, head] of [['new', '新进榜'], ['gone', '落榜'], ['carried', '沿用旧读数']]) {
        if (IO[k].length) emit(`${head} ${IO[k].length} 个`, IO[k], null);
      }
      // 「上次提示」两路：真弹过的那条（落盘）+ 台账派生的上次达门槛轮次
      const LA = D.last_alert && typeof D.last_alert === 'object' ? D.last_alert : {};
      const PM = D.prev_moves && typeof D.prev_moves === 'object' ? D.prev_moves : {};
      const laLines = (LA.lines || []).filter(Boolean).map(String);
      const laRows = laLines.length ? laLines : (LA.text ? [String(LA.text)] : []);
      const laAt = String(LA.at || '');
      // 空 rows 由 emit 自己挡掉（与 foot_tip 同一处），两边必须一样：
      // 只在这边加条件，遇到"有 at 没明细"的载荷就会两路并条与单路成节走岔。
      const pmRows = (PM.lines || PM.items || []).filter(Boolean).map(String);
      const pmAt = String(PM.at || '');
      // 强制往前翻与相邻那一路常常落在同一轮：同一轮只说一次并挂别名（与 foot_tip 同判据）
      const sameAsLast = !!pmAt && String(L.at || '') === pmAt && PM.steps === L.steps;
      // 与 foot_tip 同判据：「上次提示」那一轮常常就是「相邻达门槛」那一轮，只说一次
      const laIsLast = !sameAsLast && !!laAt && String(L.at || '') === laAt && LA.steps === L.steps;
      const gap = s => (typeof s === 'number' && s >= 1) ? `，往前第 ${s} 轮` : '';
      const laHead = `上次提示：${laAt}（${LA.kind || '?'}${gap(LA.steps)}）`;
      if (laAt && pmAt && laAt === pmAt && !PM.all_duplicated) {
        emit(laHead + '；也正是最近一次有达门槛变动的轮次', laRows.length ? laRows : pmRows, null, true);
      } else {
        if (laAt) emit(laHead + (laIsLast ? '；也正是最近一次相邻达门槛的轮次' : ''), laRows, null, true);
        if (PM.all_duplicated) {
          // 一路往前全撞在同一批明细上：如实说没有新的可报，不硬找也不编轮次
          lines.push(`上次有达门槛变动的轮次：往前翻了 ${PM.all_duplicated} 轮，`
                     + `都是同一批变动（最近一次是 ${pmAt || '?'}），没有新的变化可报`);
        } else if (pmAt) {
          emit(`上次有达门槛变动的轮次：${pmAt}`
               + (typeof PM.steps === 'number' && PM.steps >= 1
                  ? `（往前第 ${PM.steps} 轮，对比 ${PM.base}）` : '')
               + (typeof PM.dup_skipped === 'number' && PM.dup_skipped >= 1
                  ? `；中间 ${PM.dup_skipped} 轮是同一批变动，已跳过` : '')
               + (sameAsLast ? '；相邻那一路也是这一轮' : ''), pmRows, PM.n, true);
        }
      }
      if (typeof L.steps === 'number' && L.steps >= 1 && !sameAsLast && !laIsLast) {
        emit(`最近一次相邻达门槛：${L.at}（往前第 ${L.steps} 轮，对比 ${L.base}）`, litems, L.n, true);
      } else if (L.steps === -1) {
        lines.push(`往前翻了 ${L.scanned} 轮都没到过门槛（最多翻 ${L.cap} 轮）`);
      }
      if (haveCum && A.kind !== 'moved-cumulative') {
        emit(`累计对比：相对 ${CU.at}（往前第 ${csteps} 轮）有 ${CU.n} 项达门槛`, clines, CU.n);
      }
      if (A.hint) lines.push(A.hint);
      return { text: lines.join('\n'), shown: shown.length };
    };
    const m = /<span class="tip">抓取于 <b>[^<]*<\/b>[\s\S]*?<span class="bub"[^>]*>([\s\S]*?)<\/span><\/span>/.exec(CH);
    ok('抓取时间挂着自绘悬停浮层（.tip>.bub）', !!m);
    ok('不再依赖原生 title 属性（popover 里不弹）', !/class="tip" title=/.test(CH));
    ok('浮层默认隐藏、:hover/:focus 才显（否则一直挡图或永远不出现）',
       /\.foot \.bub\{[^}]*display:none/.test(CH) &&
       /\.foot \.tip:hover \.bub,?\.foot \.fgrp:hover \.bub,?\.foot \.fgrp:focus \.bub\{display:block\}/
         .test(CH));
    ok('浮层的定位上下文是整条脚注（挂回内联 .tip 会挤成竖条）',
       /\.foot\{position:relative/.test(CH) && !/\.foot \.tip\{[^}]*position:relative/.test(CH) &&
       /max-width:calc\(100% - 24px\)/.test(CH));
    ok('浮层往时间戳上方定位且不换行吞掉四行',
       /\.bub\{[^}]*position:absolute/.test(CH) && /\.bub\{[^}]*bottom:calc\(100%/.test(CH) &&
       /white-space:pre-line/.test(CH));
    if (m) {
      const python = existsSync(join(HERE, '.venv/bin/python')) ? join(HERE, '.venv/bin/python') : 'python3';
      const selected = JSON.parse(execFileSync(python, ['-B', '-c', [
        'import json,sys',
        'import fetch_model_data as F',
        'd=json.load(sys.stdin)',
        'v=(d.get("metric_views") or {}).get("tok_s")',
        'if v:',
        ' d={**d,**v}',
        'else:',
        ' moves=F.notable_moves(d.get("changes") or [], "tok_s")',
        ' d={**d,"moves":moves,"move_lines":F.merged_move_lines(moves),"last_moves":{},"prev_moves":{},"cumulative":{}}',
        ' d["alert"]=F.alert_policy(d)',
        ' d["alert"]["hint"]=F.move_hint("tok_s")',
        ' if moves: d["alert"]["last"]={"steps":0}',
        'la=d.get("last_alert") or {}',
        'rows=(la.get("metric_lines") or {}).get("tok_s") or []',
        'd["last_alert"]={**la,"lines":rows,"text":""} if rows else {}',
        'print(json.dumps(d))',
      ].join('\n')], {cwd: HERE, input: JSON.stringify(D0), encoding: 'utf8', timeout: 60000}));
      const tip = m[1], mir = tipMirror(selected);
      ok('整段提示与 payload 逐行等值（三块分工 + 合并行都算在内）', tip === escT(mir.text),
         tip.split('\n').length + ' 行');
      // 「…等另 N 条」是条目截断（浮层必须列全）；「…等 N 档」是同族尾巴里的档位计数，
      // 属于刻意保留的口径（见 family_tail），不能一起禁。
      ok('每条明细单独成行、没有「…等另」省略', !tip.includes('…等另'), `${mir.shown} 条`);
      ok('同一批条目只念一遍（三块内容重叠时去重）',
         new Set(tip.split('\n').filter(l => l.startsWith('\u00b7'))).size ===
         tip.split('\n').filter(l => l.startsWith('\u00b7')).length);
      /* 同一句「N 档同向」在浮层里是两句话，不能共用一个全局正则：
         · 合并行  `<族> <字段> N 档同向 +x%（中位 …，区间 …；档位 a/b）` —— 必须点名被并进来的档位；
         · 整族尾巴 `…；整族 N 档同向，均值 …、中位 …，区间 …` —— 只报整族方向，档位名在前一段
           `（max +5.1%…）` 里且刻意可截断（见 family_tail），不适用合并行的档位要求。
         今天的真实形状就是这两句同处一行（10-29 07:31：输出速度只有 low 一档过门槛，尾巴报
         「整族 5 档同向」）。按 `；整族 ` 切开各判，合并行那条一个字不放松。 */
      const driftViolations = t => {
        const bad = [];
        for (const line of t.split('\n')) {
          const cut = line.indexOf('；整族 ');
          const merged = cut < 0 ? line : line.slice(0, cut);
          const tail = cut < 0 ? '' : line.slice(cut);
          if (/\d+ 档同向/.test(merged) &&
              !(/中位 /.test(merged) && /区间 /.test(merged) && /档位 /.test(merged))) bad.push(merged);
          if (tail && !(/中位 /.test(tail) && /区间 /.test(tail))) bad.push(tail);
        }
        return bad;
      };
      ok('同族各档并成一条：合并行的"N 档同向"必带中位数、区间与档位名（整族尾巴另判中位/区间）',
         driftViolations(tip).length === 0, driftViolations(tip).join('｜').slice(0, 120));
      // 判据自身要能红：合并行少"档位"必须被抓到；只有整族尾巴的"N 档同向"不算合并行，
      // 但尾巴缺中位/区间仍要红 —— 不然这条只是"整段不含某几个词"的恒真式。
      const plantMerge = '· Planted Fam 输出速度 2 档同向 +5.0%（中位 +5.0%，区间 +4.0~+6.0%）';
      const plantTail = '· Planted Solo 输出速度 53→67 tok/s（+26%）；整族 5 档同向，'
                        + '均值 +9.8%、中位 +5.1%，区间 +1.4~+26.4%';
      ok('判据能红：合并行没有档位名会被抓到',
         driftViolations(plantMerge).length === 1);
      ok('判据不误伤尾巴、也不放过尾巴缺数：整族"N 档同向"不算合并行，缺中位仍红',
         driftViolations(plantTail).length === 0 &&
         driftViolations(plantTail.replace('中位 +5.1%', '')).length === 1);
      ok('不再展示去重键（内部键，读者不需要）', !tip.includes('去重键'));
      ok('条目变多时浮层可滚而不是被裁掉',
         /\.bub\{[^}]*max-height:calc\(100vh/.test(CH) && /\.bub\{[^}]*overflow:auto/.test(CH));
    }
    /* 「上次提示」这一节今天的真实载荷还带不出来（要等下一轮抓取落下 .last_alert.json），
       只比"产物 == 镜像"就没有可观察后果。按 bandplant 那套自己摆一份：同一份载荷分别喂给
       真 Python 的 foot_tip 与这里的镜像，逐字相同才算两边同口径。 */
    const PY = existsSync(join(HERE, '.venv/bin/python'))
      ? join(HERE, '.venv/bin/python') : 'python3';
    const PY_SRC = 'import json,sys\n'
      + 'sys.path.insert(0, sys.argv[1])\n'
      + 'import render_scatter_svg as R\n'
      + 'sys.stdout.write(R.foot_tip(json.load(sys.stdin)))\n';
    {
      const plant = { generated_at: D0.generated_at, data_state: D0.data_state,
                      alert: D0.alert, moves: D0.moves, move_lines: D0.move_lines,
                      cumulative: D0.cumulative,
                      rename_lines: ['Planted Fam：5 档改名（去掉 with fallback；提醒指标未达变化门槛）'],
                      changes: (D0.changes || []).concat(
                        [{ name: 'Planted New A', type: 'new' }, { name: 'Planted New B', type: 'new' },
                         { name: 'Planted Gone C', type: 'gone' },
                         { name: 'Planted Renamed New', old_name: 'Planted Renamed Old',
                           type: 'renamed', quiet: true, fields: [] }]),
                      last_alert: { at: '2026-10-12 11:40:05', ts: '2026-10-12 11:40:03',
                                    kind: 'moved', steps: 1,
                                    lines: ['Planted Est 每任务成本 $0.4→$0.6 (+50%)'] },
                      prev_moves: { at: '2026-10-11 09:12:00', base: '2026-10-10 09:11:00',
                                    steps: 3, n: 2,
                                    lines: ['Planted Band 输出速度 90→120 (+33%)',
                                            'Planted Est 智能指数 40→43（+3 分）'] } };
      let py;
      try {
        py = execFileSync(PY, ['-B', '-c', PY_SRC, HERE],
                          { input: JSON.stringify(plant), encoding: 'utf8', timeout: 60000 });
      } catch (e) {
        py = '<<Python 跑不起来：' + String(e && e.message).replace(/\n/g, ' ').slice(0, 120) + '>>';
      }
      const want = tipMirror(plant).text;
      // 个数从载荷自己算，不写死：真实 changes 里本来就有行（沿用回来的那些也算新进榜），
      // 写死 2 会让这条判据在"多一个真变动"的日子假红 —— 而它要盯的是"摆进去的那三个名字在不在"。
      const plantNew = (plant.changes || []).filter(c => c && c.type === 'new' && c.name).length;
      const plantGone = (plant.changes || []).filter(c => c && c.type === 'gone' && c.name).length;
      ok('摆一份载荷：Python 的 foot_tip 与 JS 镜像逐字相同（「上次提示」两路都真有后果）',
         py === want && py.includes('上次提示：2026-10-12 11:40:05（moved，往前第 1 轮）')
         && py.includes('上次有达门槛变动的轮次：2026-10-11 09:12:00')
         && py.includes(`新进榜 ${plantNew} 个`) && py.includes('· Planted New A')
         && py.includes(`落榜 ${plantGone} 个`) && py.includes('· Planted Gone C')
         && py.includes('名称调整（不计榜单进出）') && py.includes('· Planted Fam：5 档改名')
         && !py.includes('· Planted Renamed New'),
         (() => { const a = py.split('\n'), b = want.split('\n');
                  const k = a.findIndex((x, i) => x !== b[i]);
                  return py === want ? `${a.length} 行两边一致`
                                      : `第 ${k + 1} 行起分叉｜py: ${a[k]}｜镜像: ${b[k]}`; })());
    }
    {
      /* 第二份载荷摆的是 2026-10-15 早上的真实形状：历史那一轮的明细与本轮累计块**完全撞车**。
         这是常态不是意外（prev_moves 就是从同一叠台账快照里往前翻出来的），当时整节被行去重
         吞掉，用户看到的就是"浮层没有历史变动"。所以这一份必须把"标题留下、明细折掉"钉住。 */
      const DUP = 'Planted Dup 每任务成本 $0.4→$0.6 (+50%)';
      const plant2 = { generated_at: D0.generated_at, data_state: D0.data_state, changes: [],
                       alert: { kind: 'moved-cumulative', label: '有累计达门槛的变动', text: '',
                                hint: D0.alert && D0.alert.hint,
                                last: { at: '2026-10-14 07:30:23', base: '2026-10-13 15:06:43',
                                        steps: 1, n: 1, lines: [DUP] } },
                       moves: [], move_lines: [],
                       cumulative: { at: '2026-10-13 15:06:43', steps: 2, n: 2,
                                     lines: ['Planted Dup MiMo 输出速度 49→65 (+33%)', DUP] },
                       prev_moves: { at: '2026-10-14 07:30:23', base: '2026-10-13 15:06:43',
                                     steps: 1, n: 1, lines: [DUP] } };
      let py2;
      try {
        py2 = execFileSync(PY, ['-B', '-c', PY_SRC, HERE],
                           { input: JSON.stringify(plant2), encoding: 'utf8', timeout: 60000 });
      } catch (e) {
        py2 = '<<Python 跑不起来：' + String(e && e.message).replace(/\n/g, ' ').slice(0, 120) + '>>';
      }
      const want2 = tipMirror(plant2).text;
      const dupRows = (py2.match(/· Planted Dup 每任务成本/g) || []).length;
      ok('摆一份"历史与本轮同批"的载荷：标题仍在、明细不重列、两路同轮只说一次',
         py2 === want2
         && py2.includes('上次有达门槛变动的轮次：2026-10-14 07:30:23')
         && py2.includes('（明细与上面同批，不重列）')
         && py2.includes('相邻那一路也是这一轮')
         && !py2.includes('最近一次相邻达门槛：')
         && dupRows === 1,
         `明细出现 ${dupRows} 次${py2 === want2 ? '' : '；镜像与 Python 不一致'}`);
    }
  }
  // 图例必须是数据驱动的：点色 = 爬虫派生出的厂商色，条目数 = 图上真出现的厂商数
  const leg = [...CH.matchAll(/<circle cx="[\d.]+" cy="[\d.]+" r="4" fill="(#[0-9A-Fa-f]{6})" fill-opacity="\.9"\/>/g)]
    .map(m => m[1].toUpperCase());
  const chVendors = [...new Set((CH.match(/<title>([^<]*)<\/title>/g) || [])
    .map(t => t.slice(7, -8).split(' · ')[1]).filter(Boolean))];
  ok('图例色都在派生色表里（不再是手写 8 家）',
     leg.every(c => Object.values(D0.brand_color || {}).includes(c)), [...new Set(leg)].join(','));
  ok('图例色互不重复', new Set(leg).size === leg.length, `${leg.length} 条`);
  /* ---- 图例排布：列数按名字宽度量出来、第一行给右上角标注让位、厂商全列不省略 ---- */
  {
    const dots = [...CH.matchAll(/<circle cx="([\d.]+)" cy="([\d.]+)" r="4" fill="(#[0-9A-Fa-f]{6})" fill-opacity="\.9"\/>/g)]
      .map(m => ({ cx: +m[1], cy: +m[2] }));
    const texts = [...CH.matchAll(/<text x="([\d.]+)" y="([\d.]+)" font-size="11" fill="var\(--mr-dim[^"]*">([^<]+)<\/text>/g)]
      .map(m => ({ x: +m[1], y: +m[2], s: m[3] }))
      .filter(o => o.y > 20 && o.y < 130 && !/^[智能每]/.test(o.s));
    const x0 = Math.min(...dots.map(d => d.cx)), y0 = Math.min(...dots.map(d => d.cy));
    const xs = [...new Set(dots.map(d => d.cx))].sort((a, b) => a - b);
    const pitch = xs.length > 1 ? Math.min(...xs.slice(1).map((v, i) => v - xs[i])) : 100;
    ok('图例落在等距网格上（格宽 = 名字量出来的 pitch，行距 17）',
       pitch >= 100 && dots.every(d => Math.abs(((d.cx - x0) % pitch)) < 0.6
                         && Math.abs(((d.cy - y0) % 17)) < 0.6), `pitch ${pitch.toFixed(1)}`);
    ok('厂商全列进图例，没有省略（也没有 "+N 家" 尾巴）',
       leg.length === chVendors.length && !/\+\d+ 家/.test(CH), `${leg.length} 条 / ${chVendors.length} 家`);
    ok('图例名字不截断', !texts.some(o => o.s.endsWith('…')), texts.map(o => o.s).join(','));
    // 只有与标注同带的第一行避让，下面各行用绘图区宽度；列起点共用网格。
    const lab = /<text x="([\d.]+)" y="([\d.]+)" text-anchor="end" font-size="11"[^>]*>Pareto 前沿<\/text>/.exec(CH);
    const labY = lab ? +lab[2] : 0, labLeft = lab ? +lab[1] - 75 : Infinity;
    const sameBand = texts.filter(o => Math.abs(o.y - labY) < 8);
    const bandRight = sameBand.length ? Math.max(...sameBand.map(o => o.x + o.s.length * 6.6)) : 0;
    ok('与「Pareto 前沿」同一行的图例文字不压到标注',
       !!lab && sameBand.length > 0 && bandRight < labLeft,
       `同行最右 ${bandRight.toFixed(0)} vs 标注左 ${labLeft.toFixed(0)}`);
    const allRight = texts.length ? Math.max(...texts.map(o => o.x + o.s.length * 6.6)) : 0;
    ok('图例每行文字不越过绘图区右边界',
       texts.length > 0 && allRight <= 660, `全块最右 ${allRight.toFixed(0)} vs 绘图区右 660`);
    const rowYs = [...new Set(dots.map(d => d.cy))].sort((a, b) => a - b);
    const row0 = [...new Set(dots.filter(d => d.cy === rowYs[0]).map(d => d.cx))].sort((a, b) => a - b);
    const sameGrid = rowYs.every(y => {
      const xs = [...new Set(dots.filter(d => d.cy === y).map(d => d.cx))].sort((a, b) => a - b);
      return xs.every((v, i) => Math.abs(v - (x0 + i * pitch)) < 0.6);
    });
    ok('每行列数可不同，列起点仍共用同一份网格',
       rowYs.length > 1 && sameGrid, `${rowYs.length} 行 × 首行 ${row0.length} 列`);
    const bubbles = [...CH.matchAll(/<circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"/g)]
      .map(m => ({ cx: +m[1], cy: +m[2], r: +m[3] })).filter(b => b.r > 4);
    const hit = texts.filter(t => bubbles.some(b =>
      b.cx > t.x - 4 && b.cx < t.x + t.s.length * 6.6 + 4 && Math.abs(b.cy - t.y) < 9));
    ok('图例文字不压住右侧气泡', texts.length > 0 && hit.length === 0,
       hit.slice(0, 2).map(o => o.s).join(','));
    const below = texts.filter(o => o.y > labY + 8);
    const maxY = Math.max(...dots.map(d => d.cy));
    ok('图例高度不超过绘图区的四成',
       (maxY - y0) <= (680 - 20 - 24 - 46) * 0.4 + 17, `图例纵向跨 ${(maxY - y0).toFixed(0)}px`);
  }
  // ↓ 状态栏图例的顺序现在也是爬虫算的：比**颜色序列**（厂商色一一对应），
  //   只数条目数放不过"排序被改回按最高分/按名字"这类变异。
  const chPlottable = {};
  D0.models.filter(m => m.index !== null && m.apt_usd).forEach(m => { chPlottable[m.creator] = 1; });
  // 厂商色取模型上那份（payload.brand_color 的键是 slug，不是显示名，按名字取会全取空）
  const colOf = c => (D0.models.find(m => m.creator === c && m.brand_color) || {}).brand_color || '';
  const chExpOrder = (D0.legend || []).filter(r => chPlottable[r.creator])
    .slice(0, leg.length)                                    // 画了几条就比几条（溢出时少一条给 "+N"）
    .map(r => String(colOf(r.creator) || '').toUpperCase());
  ok('状态栏图例顺序 = payload 的梯队+加权距离顺序（逐格颜色对齐）',
     chExpOrder.length > 0 && leg.length === chExpOrder.length
     && leg.join(',') === chExpOrder.join(','),
     `图上 ${leg.slice(0, 3).join(' ')} / 应为 ${chExpOrder.slice(0, 3).join(' ')}`);
  // 状态栏图也要有同一套能力线：两条实现各查一遍，否则"网页有线、菜单栏没有"没人拦得住
  const chLines = [...CH.matchAll(/<polyline\b([^>]*)>/g)]
    .filter(m => /data-effort-view="discrete"/.test(m[1]) && /stroke-width="1\.1"/.test(m[1])
      && !m[1].includes('data-history-for='))
    .map(m => (m[1].match(/points="([^"]*)"/) || [, ''])[1]);
  const chGrp = {};
  D0.models.filter(m => m.index !== null && m.apt_usd && !chHistoryKeys.has(m.series_key))
    .forEach(m => { (chGrp[m.series_key] = chGrp[m.series_key] || []).push(m); });
  const chExp = Object.values(chGrp).filter(v => v.length > 1).length;
  ok('状态栏图能力线只含默认可见族', chLines.length === chExp, `${chLines.length} 条 vs ${chExp} 族`);
   ok('状态栏图能力线按固定推理档位逐顶点连接', tierLinesMatch(CH, chGrp), '');
  ok('脚注交代了能力线', /能力线/.test(CH));
   ok('两条实现明确固定推理档位顺序', /按推理档位固定顺序/.test(CH));
} else {
  console.log('  skip  没找到 chart.html（未跑过 build/crawl），跳过状态栏侧校验');
}

console.log('\n== 厂商配色表（派生色 = 描边/图例；ring_bg 只做圆底） ==');
const bySlug = {};
D0.models.forEach(m => { bySlug[m.creator_slug] = bySlug[m.creator_slug] || m; });
Object.keys(bySlug).sort((a, b) => String(a).localeCompare(String(b)))
  .forEach(slug => {
    const m = bySlug[slug], p = palOf(m);
    console.log(`  ${slug.padEnd(11)} 色=${VCOLOR[slug] || '缺!'}  ${String(VSRC[slug] || '缺!').padEnd(14)}`
      + `圆底=${p ? p.ring_bg : '—'}  icon=${p ? (p.invert_icon ? '白化' : '原色') : '无 logo'}  `
      + `对比=${p ? p.contrast_icon : '—'}`);
  });

/* ---------- 悬停高亮：整族置顶 + 放大 ----------
   两块都要真验到：① 两个渲染面的标记里挂了同一套钩子（class/data-k）；
   ② 那段共享 JS 在**解析出来的真 SVG 树**上跑一遍，确认它确实把整族搬进顶层、
      加 .hi、放大，并且离开时搬回去。只看 CSS 规则在不在是给不了这个保证的。 */
console.log('\n== 悬停高亮（整族置顶 + 放大） ==');

class El {
  constructor(tag, attrs = {}) {
    this.tag = tag; this.attrs = { ...attrs }; this.children = [];
    this.parentNode = null; this.nextSibling = null; this.listeners = {}; this.text = '';
    this._cls = new Set((attrs.class || '').split(/\s+/).filter(Boolean));
  }
  get classList() {
    const s = this._cls;
    return { add: c => s.add(c), remove: c => s.delete(c), contains: c => s.has(c) };
  }
  get className() { return [...this._cls].join(' '); }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'class') String(v).split(/\s+/).filter(Boolean).forEach(c => this._cls.add(c)); }
  getAttribute(k) { return this.attrs[k] === undefined ? null : String(this.attrs[k]); }
  removeAttribute(k) { delete this.attrs[k]; }
  _link() { this.children.forEach((c, i) => { c.nextSibling = this.children[i + 1] || null; }); }
  _drop(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; c.nextSibling = null; this._link(); }
  appendChild(c) { if (c.parentNode) c.parentNode._drop(c); this.children.push(c); c.parentNode = this; this._link(); return c; }
  insertBefore(c, ref) {
    if (c.parentNode) c.parentNode._drop(c);
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    c.parentNode = this; this._link(); return c;
  }
  walk(fn) { for (const c of this.children) { fn(c); c.walk(fn); } }
  $$attr(name, val) { const out = []; this.walk(e => { if (e.attrs[name] !== undefined && (val === undefined || e.attrs[name] === val)) out.push(e); }); return out; }
  $$tag(tag) { const out = []; this.walk(e => { if (e.tag === tag) out.push(e); }); return out; }
  get firstChild() { return this.children[0] || null; }
  // 只支持单段复合选择器：tag? .class* [attr(=value)]*。
  // 被测代码用 g.mk / g.chip[data-ball] / circle[data-role="node"] 这类真实写法，
  // 桩若只会解析单条选择器，就会把"选择器写对了但节点没被搬"误判成通过。
  matches(sel) {
    const m = String(sel).trim().match(
      /^([a-zA-Z][\w:-]*)?((?:\.[\w-]+)*)((?:\[[\w:.-]+(?:="[^"]*")?\])*)$/);
    if (!m) return false;
    if (m[1] && this.tag !== m[1]) return false;
    for (const c of m[2].match(/\.[\w-]+/g) || []) if (!this._cls.has(c.slice(1))) return false;
    for (const a of m[3].matchAll(/\[([\w:.-]+)(?:="([^"]*)")?\]/g)) {
      if (this.attrs[a[1]] === undefined) return false;
      if (a[2] !== undefined && String(this.attrs[a[1]]) !== a[2]) return false;
    }
    return true;
  }
  // 被测代码用的是标准 DOM 接口，所以桩也要按选择器给（不是只给内部 $$attr）
  querySelectorAll(sel) {
    const out = [];
    this.walk(e => { if (e.matches(sel)) out.push(e); });
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) {
    let n = this;
    while (n) { if (n.matches && n.matches(sel)) return n; n = n.parentNode; }
    return null;
  }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  // 真实 DOM 的 focus() 只移动键盘焦点，不改值；桩记录一次，供"切换后焦点在开关上"判据使用。
  focus() { this.focused = true; }
  getBBox() {
    // <text>：按 x/y/font-size 与字符宽度造一个包围盒（CJK 按字号、其余 0.6 字号）。
    // 没有这条，塌缩后所有卡都缩到同一个原点，"贴位"判据测的就是桩子的形状而不是代码。
    if (this.tag === 'text') {
      const fs = +(this.attrs['font-size'] || 11);
      const t = (this.children || []).map(c => c.text || '').join('') || this.text || '';
      // 故意与作者侧的估算**不一致**（ASCII 0.55、CJK 1.05）：桩子若照抄 text_w，
      // 塌缩就等于没塌缩，"只挪矩形不挪字""基准用当前 rect"这类错在桩里永远测不出来。
      let w = 0;
      for (const ch of t) w += ch.codePointAt(0) > 0x2e80 ? fs * 1.05 : fs * 0.55;
      return { x: +(this.attrs.x || 0), y: +(this.attrs.y || 0) - fs, width: w || 1, height: fs * 1.2 };
    }
    const c = this.tag === 'circle' ? this : (this.$$tag('circle')[0] || this);
    const cx = +(c.attrs.cx || 0), cy = +(c.attrs.cy || 0), r = +(c.attrs.r || 1);
    return { x: cx - r, y: cy - r, width: 2 * r, height: 2 * r };
  }
}
function parseSvg(src) {
  const root = new El('#root');
  const stack = [root];
  const re = /<(\/?)([a-zA-Z][\w:-]*)((?:\s+[\w:.-]+="[^"]*")*)\s*(\/?)>/g;
  let last = 0;
  // 标签之间的文本要收进当前节点：判据要看卡片里到底写了什么字，光有标签与属性不够
  for (const m of src.matchAll(re)) {
    const gap = src.slice(last, m.index);
    if (gap) { const cur = stack[stack.length - 1]; cur.text = (cur.text || '') + gap; }
    last = m.index + m[0].length;
    const [, close, tag, attrStr, selfClose] = m;
    if (close) { if (stack.length > 1) stack.pop(); continue; }
    const attrs = {};
    for (const a of (attrStr || '').matchAll(/([\w:.-]+)="([^"]*)"/g)) attrs[a[1]] = a[2];
    const el = new El(tag, attrs);
    stack[stack.length - 1].appendChild(el);
    if (!selfClose && !['title'].includes(tag)) stack.push(el);
    else if (!selfClose) stack.push(el);
  }
  return root;
}

// ① 两个面的钩子
const s1 = render(D0, false);
const S1 = s1.scatterwrap.innerHTML;
const gOf = {};
D0.models.filter(m => m.index !== null && m.apt_usd > 0)
  .forEach(m => { const k = m.series_key || norm(m.name); (gOf[k] = gOf[k] || []).push(m); });
const multiKeys = Object.keys(gOf).filter(k => gOf[k].length > 1);
// 族钩子要覆盖**三类**气泡组（普通 / 推估 / 参照带），并且跟"组数"比而不是"圆数"比 ——
// 推估与参照带都多一圈区间环，数圆会多算。
// 类别名要在这里列全：漏了 band 的话，带点就从这条钩子覆盖率里悄悄消失（这次就是）。
// 类名可能叠着写（mk / mk est / mk band / mk carr / mk est carr），枚举必漏一种：直接放宽到属性形状
const mkAttr = (S1.match(/<g class="mk[^"]*" data-k="/g) || []).length;
const mkGroups = (S1.match(/<g class="mk[ "]/g) || []).length;
const lnAttr = (S1.match(/<polyline class="ln" data-k="/g) || []).length;
ok('看板：每个气泡都带族钩子', mkAttr === mkGroups && mkGroups > 0, `${mkAttr} 钩子 / ${mkGroups} 组`);
ok('看板：每条能力线都带同一个族钩子', lnAttr === multiKeys.length, `${lnAttr} vs ${multiKeys.length}`);

// ② 共享实现没有分叉：两边 HOVER-BEGIN/END 之间的正文必须逐字一致
const PY = readFileSync(join(HERE, 'render_scatter_svg.py'), 'utf8');
const between = s => (s.match(/\/\* HOVER-BEGIN \*\/([\s\S]*?)\/\* HOVER-END \*\//) || [, ''])[1];
const flat = s => s.replace(/\s+/g, ' ').trim();
const pyBody = flat(between(PY));
const htmlBody = flat(between(html));
ok('两面用的是同一份高亮实现（防只改一边）',
   pyBody.length > 400 && pyBody === htmlBody, `py ${pyBody.length} 字 / html ${htmlBody.length} 字`);

// ③ 行为：把真产物里的散点 SVG 解析成树，跑这段共享 JS，模拟 pointer 事件。
//    DOM 形状照抄看板页面：页面级裁剪 <svg width=0> 排在散点**前面** —— 这样"绑错 svg"
//    这种失灵会被测出来（曾经就是 querySelector('svg') 命中了那张空图）。
const tree = parseSvg(S1);
const svg = tree.$$tag('svg')[0];
const decoy = new El('svg', { width: '0', height: '0' });
const wrap = new El('div', { id: 'scatterwrap' });
wrap.appendChild(svg);
const page = new El('#page');
page.appendChild(decoy); page.appendChild(wrap);
const hoverBody = `(function(){\n${between(PY)}\n})();`;
const doc = { querySelector: sel => (sel === '#scatterwrap svg' ? svg
                                     : sel === 'svg' ? decoy : null),
              querySelectorAll: () => [],
              createElementNS: (ns, tag) => new El(tag) };
// CSS 在浏览器里是全局（window.CSS === CSS），所以桩要同时给 window 上的和裸全局名，
// 否则会在这里抛 ReferenceError —— 那是桩的问题，不是被测代码的问题。
const cssStub = { escape: k => String(k) };
new Function('document', 'window', 'CSS', hoverBody)(doc, { CSS: cssStub }, cssStub);
const top = svg.children.find(g => (g.attrs.class || '').includes('toplayer'));
ok('顶层组挂在散点 svg 上，不是页面里那张裁剪空图', !!top && decoy.children.length === 0,
   top ? 'ok' : '没找到 .toplayer');
const key = multiKeys.find(k => gOf[k].length >= 3) || multiKeys[0];
const fam = svg.$$attr('data-k', key);
const before = fam.map(e => e.parentNode);
// 顺序签名覆盖整棵树：**节点对象身份** + 在父节点 children 里的**下标**。
// 只比 parentNode 会漏掉"父子都对、但兄弟顺序漂了"；只比属性串又会漏掉
// 两个描述相同的节点互换，所以这里用 WeakMap 给每个真实节点分配稳定编号。
const nodeIds = new WeakMap();
let nodeSeq = 0;
const nodeId = el => {
  if (!nodeIds.has(el)) nodeIds.set(el, ++nodeSeq);
  return nodeIds.get(el);
};
function childSig(root) {
  const rows = [];
  const rec = p => p.children.forEach((c, i) => { rows.push(`${nodeId(c)}@${i}`); rec(c); });
  rec(root);
  return rows.join('|');
}
const pristineOrder = childSig(svg);
const mark0 = fam.find(e => e._cls.has('mk'));
svg.listeners.pointerover[0]({ target: mark0 });
const inTop = fam.filter(e => e.parentNode === top).length;
ok('悬停一个档位 → 整族（含能力线）搬进顶层', top && inTop === fam.length,
   `${inTop}/${fam.length} 个节点，族 ${key}`);
ok('整族都加了 .hi 高亮类', fam.every(e => e._cls.has('hi')), '');
ok('族内每个气泡都被放大（transform 含 scale）',
   fam.filter(e => e._cls.has('mk')).every(e => /scale\(1\.45\)/.test(e.getAttribute('transform') || '')), '');
ok('能力线本身不放大（只加粗）',
   fam.filter(e => e._cls.has('ln')).every(e => !e.getAttribute('transform')), '');

// ↓ 闪烁回归：指针在同一个气泡内轻微移动（事件打在 <circle>/<image> 上）时，
//   绝不能重排 DOM —— 重排就是"抖"的来源。
const snap = top.children.slice();
const circle0 = mark0.$$tag('circle')[0] || mark0;
svg.listeners.pointerover[0]({ target: circle0 });
ok('同族内微移动不重排 DOM（置顶成员原封不动）',
   top.children.length === snap.length && top.children.every((c, i) => c === snap[i]),
   `顶层 ${top.children.length} 个节点`);
ok('没有 pointerout 监听（搬动节点会补发 out，接上就自激）',
   !svg.listeners.pointerout, Object.keys(svg.listeners).join(','));

// 浏览器仍会为被搬走的节点补发 out 事件：模拟一次，高亮必须不受影响
if (svg.listeners.pointerout) svg.listeners.pointerout[0]({ target: mark0, relatedTarget: null });
ok('收到迟到的 pointerout 也不收起（不是闪烁）', fam.every(e => e._cls.has('hi')), '');

// 掠过非族节点（金色前沿/网格线）**不该立刻**收起：同族两档之间移动必然穿过它们，
// 立刻收起就是闪烁。
const gold = svg.$$tag('polyline').find(p => (p.attrs.stroke || '').toUpperCase() === '#E3B341');
svg.listeners.pointerover[0]({ target: gold || svg.$$tag('line')[0] });
ok('掠过金色前沿时高亮不立刻收起（延迟收起）', fam.every(e => e._cls.has('hi')), '');
// 延迟窗口内落到同族另一档 → 取消收起，且顶层成员一字不动
const mark1 = fam.filter(e => e._cls.has('mk'))[1] || mark0;
svg.listeners.pointerover[0]({ target: mark1 });
ok('延迟窗口内回到族内 → 取消收起且 DOM 不动',
   fam.every(e => e._cls.has('hi')) && top.children.length === snap.length
   && top.children.every((c, i) => c === snap[i]), `顶层 ${top.children.length} 个`);
// 停在空白处超过延迟窗口 → 收起并搬回原位
svg.listeners.pointerover[0]({ target: svg.$$tag('line')[0] });
await new Promise(r => setTimeout(r, 140));
ok('停在空白处 → 节点搬回原来的父节点', fam.every((e, i) => e.parentNode === before[i]), '');
ok('停在空白处 → 高亮与缩放都清掉了', fam.every(e => !e._cls.has('hi') && !e.getAttribute('transform')), '');
svg.listeners.pointerover[0]({ target: mark0 });
svg.listeners.pointerleave[0]({});
ok('pointerleave 立即收起并搬回原位',
   fam.every(e => !e._cls.has('hi') && !e.getAttribute('transform')) &&
   fam.every((e, i) => e.parentNode === before[i]), '');

// 顺序回归（真数据 / current payload）：整族含能力线与卡片反复抬起-收起后，
// 所有节点必须按"身份 + 原下标"逐个复原。
// 2026-10-29 的缺陷就是只按 home 记录顺序插回：同父的 mk6 会插到 mk5 前面，
// 连末尾 toplayer 也跟着漂；只比 parentNode 的旧判据完全看不见。
ok('真数据：一轮悬停/收起后全树子节点身份/下标逐个复原（含 toplayer）',
   childSig(svg) === pristineOrder,
   childSig(svg) === pristineOrder ? `最后一项 ${svg.children[svg.children.length - 1].attrs.class}`
                                   : '顺序与悬停前不一致');
let driftCycle = '';
for (let cycle = 1; cycle <= 3; cycle++) {
  svg.listeners.pointerover[0]({ target: mark0 });
  svg.listeners.pointerleave[0]({});
  if (childSig(svg) !== pristineOrder) { driftCycle = `第 ${cycle} 轮`; break; }
}
ok('真数据：整族悬停 3 轮后子节点身份/下标仍逐个复原（重复周期不累积漂移）',
   !driftCycle, driftCycle || '3 轮稳定');

/* ---------- 脚注三个计数：精确 data-index 子集、卡片开关、焦点/键盘、模式切换重排 ----------
 * 共享 HOVER 里脚注走 lightIds()，与"整族 data-k"是两条路。只对比源码字符串会漏掉
 * "选择器写对了但搬错节点"这类错，所以在带 data-foot-group 的 DOM 上真跑一遍：
 * 一族两档只选中一档时能力线不许亮，整族都选中才亮；MR_CARDS=0/1 只影响卡片；
 * pointerleave / focus / Escape 都要把高亮、卡片几何与 DOM 顺序还回去。 */
console.log('\n== 脚注精确子集（悬停 / 焦点 / 键盘 / 模式重排） ==');
{
  const FOOT_SVG = `<svg data-plot="54 24 660 406">`
    + `<g class="ln" data-k="f1"></g>`
    + `<g class="mk" data-k="f1" data-c="V" data-i="0" data-n="A&amp;&lt;B">`
    + `<circle cx="100" cy="100" r="5"><title>A&amp;&lt;B</title></circle></g>`
    + `<g class="mk" data-k="f1" data-c="V" data-i="1" data-n="C">`
    + `<circle cx="120" cy="120" r="5"><title>C</title></circle></g>`
    + `<g class="ln" data-k="f2"></g>`
    + `<g class="mk" data-k="f2" data-c="V" data-i="2" data-n="D">`
    + `<circle cx="140" cy="140" r="5"><title>D</title></circle></g>`
    + `<g class="chip fam" data-k="f1" data-c="V" data-pri="1" data-anchor="br"`
    + ` data-r0="300 100 90 24" data-ball="100 100 5">`
    + `<rect x="300" y="100" width="90" height="24" rx="6"/>`
    + `<text x="306" y="112" font-size="10">A&amp;&lt;B</text></g>`
    + `<g class="chip fam" data-k="f2" data-c="V" data-pri="2" data-anchor="br"`
    + ` data-r0="340 140 70 24" data-ball="140 140 5">`
    + `<rect x="340" y="140" width="70" height="24" rx="6"/>`
    + `<text x="346" y="152" font-size="10">D</text></g>`
    // 全树顺序判据需要"不是族节点"的兄弟与末尾图例，toplayer 由共享 JS 追加在最后：
    // 只摆点线卡的话，"插回时把 toplayer 顶走"这种漂移看不见。
    + `<text data-note-for="0" font-size="11">N</text>`
    + `<g class="lgc" data-c="V"><text x="10" y="10">V</text></g>`
    + `</svg>`;

  function bootFoot(winExtra) {
    const root = parseSvg(FOOT_SVG);
    root.attrs['data-plot'] = '54 24 660 406';
    const grp = (key, idx, text) => {
      const el = new El('span', { class: 'fgrp', 'data-foot-group': key,
                                  'data-index': idx, tabindex: '0' });
      const bub = new El('span', { class: 'bub' });
      bub.text = text;
      el.appendChild(bub);
      return el;
    };
    const groups = {
      node: grp('node', '0', 'A&lt;&amp;B'),
      est: grp('est', '0,1', 'A&lt;&amp;B\n· C'),
      band: grp('band', '2', 'D'),
    };
    const doc = {
      querySelector: sel => (sel === '#scatterwrap svg' || sel === 'svg') ? root : null,
      querySelectorAll: sel => sel === '[data-foot-group]' ? Object.values(groups) : [],
      createElementNS: (_ns, tag) => new El(tag),
      activeElement: null,
    };
    for (const g of Object.values(groups)) g.blur = () => { doc.activeElement = null; };
    new Function('document', 'window', 'CSS', hoverBody)(
      doc, Object.assign({ CSS: cssStub }, winExtra || {}), cssStub);
    return { root, groups, doc };
  }
  const gOf = (root, tag, pred) => root.$$tag(tag).find(pred);
  const markOf = (root, i) => gOf(root, 'g', g => g._cls.has('mk') && g.attrs['data-i'] === i);
  const lineOf = (root, k) => gOf(root, 'g', g => g._cls.has('ln') && g.attrs['data-k'] === k);
  const chipOf = (root, k) => gOf(root, 'g', g => g._cls.has('chip') && g.attrs['data-k'] === k);
  const lit = el => !!(el && el._cls.has('hi'));
  const orderOf = root => {
    const out = new Map();
    root.walk(el => { if (el.parentNode) out.set(el, [el.parentNode, el.parentNode.children.indexOf(el)]); });
    return out;
  };

  const a = bootFoot({ MR_CARDS: 1 });
  const orig = orderOf(a.root);
  a.groups.node.listeners.pointerenter[0]();
  ok('脚注悬停按 data-index 精确子集：只亮命中那一档，不抬整族',
     lit(markOf(a.root, '0')) && !lit(markOf(a.root, '1')) && !lit(markOf(a.root, '2')),
     `亮 ${['0', '1', '2'].filter(i => lit(markOf(a.root, i))).join(',') || '无'}`);
  ok('脚注子集：能力线只在整族都被选中时才亮（还差一档，f1/f2 都不许亮）',
     !lit(lineOf(a.root, 'f1')) && !lit(lineOf(a.root, 'f2')), '');
  ok('脚注子集：命中族的那张卡显出来，未命中的族不显',
     lit(chipOf(a.root, 'f1')) && !lit(chipOf(a.root, 'f2')), '');
  ok('脚注子集：标记与卡片里的转义名字原样保留（& < 不被二次转义/还原）',
     markOf(a.root, '0').attrs['data-n'] === 'A&amp;&lt;B'
       && chipOf(a.root, 'f1').$$tag('text')[0].text === 'A&amp;&lt;B'
       && a.groups.node.querySelector('.bub').text === 'A&lt;&amp;B',
     `data-n=${markOf(a.root, '0').attrs['data-n']}`);

  a.groups.est.listeners.pointerenter[0]();
  ok('脚注整族子集：两档都命中时该族能力线才亮，另一族仍不亮',
     lit(markOf(a.root, '0')) && lit(markOf(a.root, '1')) && !lit(markOf(a.root, '2'))
       && lit(lineOf(a.root, 'f1')) && !lit(lineOf(a.root, 'f2')),
     `f1 线 ${lit(lineOf(a.root, 'f1'))} / f2 线 ${lit(lineOf(a.root, 'f2'))}`);

  a.groups.est.listeners.pointerleave[0]();
  await new Promise(r => setTimeout(r, 140));
  const plBadParent = [...orig.keys()].filter(el => el.parentNode !== orig.get(el)[0]);
  const plRect = chipOf(a.root, 'f1').$$tag('rect')[0].attrs;
  const plR0W = String(chipOf(a.root, 'f1').attrs['data-r0']).split(' ')[2];
  ok('脚注 pointerleave 延迟收起：高亮/线/卡全部还原，节点回到原父级且卡片几何复位',
     !['0', '1', '2'].some(i => lit(markOf(a.root, i))) && !lit(lineOf(a.root, 'f1'))
       && !lit(chipOf(a.root, 'f1')) && !chipOf(a.root, 'f1').attrs.transform
       && plBadParent.length === 0 && +plRect.width === +plR0W,
     `父级错 ${plBadParent.length} 个；rect 宽 ${plRect.width}/${plR0W}`);

  a.doc.activeElement = a.groups.band;
  a.groups.band.listeners.focus[0]();
  ok('脚注 focus 与悬停走同一精确子集（只亮 i=2）',
     lit(markOf(a.root, '2')) && !lit(markOf(a.root, '0')) && !lit(markOf(a.root, '1')), '');
  a.groups.band.listeners.pointerleave[0]();
  await new Promise(r => setTimeout(r, 140));
  ok('脚注仍有焦点时 pointerleave 不收起（键盘用户不会因鼠标划过而丢高亮）',
     lit(markOf(a.root, '2')) && lit(chipOf(a.root, 'f2')), '');
  a.groups.band.listeners.keydown[0]({ key: 'Escape' });
  const escBadParent = [...orig.keys()].filter(el => el.parentNode !== orig.get(el)[0]);
  ok('脚注 Escape：立即还原、清掉高亮、卡片几何复位并 blur',
     !lit(markOf(a.root, '2')) && !lit(chipOf(a.root, 'f2')) && a.doc.activeElement === null
       && escBadParent.length === 0
       && !chipOf(a.root, 'f2').attrs.transform,
     `activeElement=${a.doc.activeElement}；父级错 ${escBadParent.length} 个`);

  const z = bootFoot({ MR_CARDS: 0 });
  z.groups.est.listeners.pointerenter[0]();
  ok('MR_CARDS=0：脚注子集与能力线照旧，卡片一张都不显',
     lit(markOf(z.root, '0')) && lit(markOf(z.root, '1')) && lit(lineOf(z.root, 'f1'))
       && !lit(chipOf(z.root, 'f1')) && !lit(chipOf(z.root, 'f2')),
     `亮 ${['0', '1'].filter(i => lit(markOf(z.root, i))).join(',')}；卡 ${lit(chipOf(z.root, 'f1'))}`);

  const r = bootFoot({ MR_CARDS: 1 });
  r.groups.est.listeners.focus[0]();
  const rc = chipOf(r.root, 'f1');
  const beforeT = rc.attrs.transform;
  // 模拟 SIZE_JS 切换模式：更新作者侧锚点后调用共享重排钩子。
  rc.attrs['data-r0'] = '320 120 90 24';
  rc.attrs['data-ball'] = '420 260 9';
  r.root.__mrRelayout();
  ok('模式切换重排（ids）：脚注命中的子集不变，卡片按新的 data-r0/data-ball 重新贴位',
     lit(markOf(r.root, '0')) && lit(markOf(r.root, '1')) && !lit(markOf(r.root, '2'))
       && lit(lineOf(r.root, 'f1')) && lit(rc)
       && rc.attrs.transform && rc.attrs.transform !== beforeT,
     `${beforeT} → ${rc.attrs.transform}`);

  // ---- 顺序回归（合成整族：能力线 + 多档 + 卡片 + 非族兄弟 + 末尾 lgc + toplayer）----
  // 两个入口都测：脚注 lightIds（精确子集）与整族 light（data-k）。
  // 每轮按"身份 + 原下标"整树比对，而不是只看 parentNode —— 只比父级会漏掉
  // "mk6 插到 mk5 前面、toplayer 跟着漂"这一类 2026-10-29 的真实缺陷。
  const o = bootFoot({ MR_CARDS: 1 });
  const oSig = childSig(o.root);
  let idsDrift = '';
  for (let cycle = 1; cycle <= 3; cycle++) {
    o.groups.est.listeners.pointerenter[0]();
    o.groups.est.listeners.pointerleave[0]();
    await new Promise(res => setTimeout(res, 140));
    if (childSig(o.root) !== oSig) { idsDrift = `ids 第 ${cycle} 轮`; break; }
  }
  ok('合成整族：脚注子集（含能力线）3 轮悬停/收起后全树身份/下标复原（含 toplayer）',
     !idsDrift, idsDrift || `最后一项 ${o.root.children[o.root.children.length - 1].attrs.class}`);

  let grpDrift = '';
  for (let cycle = 1; cycle <= 3; cycle++) {
    o.root.listeners.pointerover[0]({ target: markOf(o.root, '0') });
    o.root.listeners.pointerleave[0]({});
    if (childSig(o.root) !== oSig) { grpDrift = `整族 第 ${cycle} 轮`; break; }
  }
  ok('合成整族：data-k 悬停 3 轮后全树身份/下标复原（与脚注入口共用 dark 归位）',
     !grpDrift, grpDrift || '3 轮稳定');

  // ---- 焦点/指针优先级 ----
  // 2026-10-29 缺陷：焦点 est 后指针掠过 band 再离开，band 的 pointerleave 把 est 也抹掉。
  // 期望：指针优先；指针离开后回退仍有焦点的脚注（不清空、不 stale）。
  const fp = bootFoot({ MR_CARDS: 1 });
  const fpSig = childSig(fp.root);
  const note = fp.root.$$tag('text').find(t => t.attrs['data-note-for']);
  fp.doc.activeElement = fp.groups.est;
  fp.groups.est.listeners.focus[0]();
  fp.groups.band.listeners.pointerenter[0]();
  ok('焦点 est 后悬停 band：指针优先，只亮 band 子集',
     lit(markOf(fp.root, '2')) && !lit(markOf(fp.root, '0')) && !lit(markOf(fp.root, '1')), '');
  fp.groups.band.listeners.pointerleave[0]();
  await new Promise(res => setTimeout(res, 140));
  ok('指针离开 band 但 est 仍有焦点：保留 est 子集，不熄灭',
     lit(markOf(fp.root, '0')) && lit(markOf(fp.root, '1')) && !lit(markOf(fp.root, '2'))
       && fp.doc.activeElement === fp.groups.est,
     `亮 ${['0', '1', '2'].filter(i => lit(markOf(fp.root, i))).join(',') || '无'}`);

  // 空白处延时收起 / 离开图区：两条清理路径都必须回退焦点选择。
  fp.root.listeners.pointerover[0]({ target: note });
  await new Promise(res => setTimeout(res, 140));
  ok('有焦点脚注时空白处延时收起：回退 est 而不是清空',
     lit(markOf(fp.root, '0')) && lit(markOf(fp.root, '1')) && !lit(markOf(fp.root, '2')), '');
  fp.root.listeners.pointerleave[0]({});
  ok('离开图区但 est 仍有焦点：保留 est 子集',
     lit(markOf(fp.root, '0')) && lit(markOf(fp.root, '1')) && !lit(markOf(fp.root, '2')), '');

  fp.groups.est.listeners.keydown[0]({ key: 'Escape' });
  ok('Escape 显式清场：高亮/卡片/几何清掉且 blur',
     !['0', '1', '2'].some(i => lit(markOf(fp.root, i)))
       && !lit(chipOf(fp.root, 'f1')) && !chipOf(fp.root, 'f1').attrs.transform
       && fp.doc.activeElement === null,
     `active=${fp.doc.activeElement}`);
  ok('Escape 清场后全树身份/下标复原',
     childSig(fp.root) === fpSig, childSig(fp.root) === fpSig ? '' : '顺序与初始不一致');
  fp.root.listeners.pointerover[0]({ target: note });
  await new Promise(res => setTimeout(res, 140));
  ok('Escape 之后迟到的空白清理不会复亮（焦点态无 stale）',
     !['0', '1', '2'].some(i => lit(markOf(fp.root, i))), '');
}

/* ---------- 气泡大小切换：真产物 + 真 JS 行为（两面各跑一遍） ----------
 * 只查源码里有没有 data-r-task 说明不了行为：切换后半径、图例直径、logo 尺寸、
 * 命中区、环带、悬停卡重贴、键盘语义、切回后的 TPS 复原都必须真的发生。
 * 渲染面直接用运行产物 chart.html 里的两段脚本；看板面用带真 SVG 解析的 DOM 桩。 */
console.log('\n== 气泡大小切换（任务耗时面积编码） ==');

function nodeOfMark(g) {
  return g.$$tag('circle').find(c => c.attrs['data-role'] === 'node')
      || g.$$tag('circle').find(c => !c.attrs['data-band-ring'] && c.$$tag('title').length)
      || g.$$tag('circle')[0];
}
function taskAreaExpected(r, rmax) { return (r / rmax) ** 2; }

if (!chartPath || !existsSync(chartPath)) {
  ok('状态栏真产物可用于切换模拟', false, '找不到 chart.html，root 先跑一次渲染');
} else {
  const CH = readFileSync(chartPath, 'utf8');
  const chartSvg = parseSvg(CH.slice(CH.indexOf('<svg'), CH.indexOf('</svg>') + 6))
    .$$tag('svg')[0];
  const box = new El('input', { id: 'task-size', type: 'checkbox', role: 'switch',
                               'aria-checked': 'false' });
  const cap = new El('span', { id: 'size-caption' });
  const effort = new El('input', { id: 'effort-smooth', 'aria-checked': 'false' });
  const effortCap = new El('span', { id: 'effort-caption' });
  const effortWrites = [];
  const wrap = new El('div', { id: 'scatterwrap' });
  wrap.appendChild(chartSvg);
  const doc = {
    querySelector: sel => (sel === '#scatterwrap svg' || sel === 'svg') ? chartSvg : null,
    querySelectorAll: () => [],
    getElementById: id => ({'task-size': box, 'size-caption': cap,
      'effort-smooth': effort, 'effort-caption': effortCap})[id] || null,
    createElementNS: (_ns, tag) => new El(tag),
    activeElement: null,
  };
  const scripts = [...CH.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
  let scriptErr = '';
  try {
    for (const src of scripts) new Function('document', 'window', 'CSS', src)(
      doc, { CSS: { escape: k => String(k) }, MR_EFFORT_SMOOTH: 1,
        webkit: { messageHandlers: { mrEffortSmooth: { postMessage: v => effortWrites.push(v) } } } },
      { escape: k => String(k) });
  } catch (e) { scriptErr = String(e && e.message || e).slice(0, 160); }
  ok('状态栏切换脚本能在真产物上跑起来（悬停 + 尺寸两段）',
     !scriptErr && scripts.length >= 2, scriptErr || `${scripts.length} 段`);

  const marks = chartSvg.$$tag('g').filter(g => g._cls.has('mk'));
  const chips = chartSvg.$$tag('g').filter(g => g._cls.has('chip') && g.attrs['data-ball'] !== undefined);
  const snap = () => ({
    mode: chartSvg.getAttribute('data-size-mode'),
    aria: box.getAttribute('aria-checked'),
    cap: cap.textContent,
    nodes: marks.map(g => [g.attrs['data-n'], nodeOfMark(g)?.attrs.r]),
    chips: chips.map(g => {
      const rect = g.querySelector('rect');
      return {
        k: g.attrs['data-k'], ball: g.attrs['data-ball'], r0: g.attrs['data-r0'],
        rect: rect ? [rect.attrs.x, rect.attrs.y] : null,
        texts: g.querySelectorAll('text').map(t => [t.attrs.x, t.attrs.y]),
      };
    }),
  });
  const base = snap();
  const positions0 = marks.map(g => {
    const c = nodeOfMark(g);
    return [c.attrs.cx, c.attrs.cy];
  });
  const lines0 = chartSvg.$$tag('polyline').map(p => p.attrs.points);
  const switchEffort = on => {
    effort.checked = on;
    (effort.listeners.change || []).forEach(fn => fn());
    return chartSvg.getAttribute('data-effort-mode');
  };
  ok('状态栏 Effort 刷新首帧恢复宿主偏好且不重复写入',
     effort.checked && chartSvg.getAttribute('data-effort-mode') === 'smooth'
       && effortCap.textContent === 'Effort 插值连线' && effortWrites.length === 0);
  ok('状态栏 Effort 开关切换前沿与标签的同一模式，并可恢复离散点位',
     switchEffort(true) === 'smooth' && effortCap.textContent === 'Effort 插值连线'
       && effort.getAttribute('aria-checked') === 'true'
       && switchEffort(false) === 'discrete' && effortCap.textContent === '离散点位连线'
       && effortWrites.join(',') === '1,0');
  const fire = () => (box.listeners.change || []).forEach(fn => fn({ target: box }));

  ok('状态栏默认 TPS：模式 speed、aria-checked=false、半径=data-r',
     base.mode === 'speed' && base.aria === 'false'
       && marks.every(g => nodeOfMark(g).attrs.r === g.attrs['data-r']),
     `${marks.length} 个气泡`);
  const bands0 = chartSvg.$$attr('data-band-ring').length;

  // 键盘语义：原生 checkbox 在 Space 上先翻 checked 再发 change。
  box.checked = true;
  fire();
  const task = snap();
  ok('键盘/开关切换后 aria-checked=true、模式 task、标题跟着换',
     task.mode === 'task' && task.aria === 'true'
       && box.checked === true && cap.textContent === '气泡面积 = 任务耗时（越快越大）',
     `${task.mode}｜${task.aria}｜${cap.textContent}`);
  ok('任务模式每个气泡半径 = data-r-task（缺耗时=2 且带 data-task-missing）',
     marks.every(g => {
       const c = nodeOfMark(g);
       return c.attrs.r === (+g.attrs['data-r-task']).toFixed(1)
         && (!g.attrs['data-task-missing'] || (+c.attrs.r === 2));
     }), `r_task 覆盖 ${marks.filter(g => g.attrs['data-r-task']).length}/${marks.length}`);

  // data-tp 只放官方原值；官方缺失但有派生点时，点仍在任务模式下按 data-tp-est 定大小，
  // 所以边界判据要把这两档都算进来，否则"最快/最慢"会漏掉估计点。
  const tpVal = g => {
    const raw = g.attrs['data-tp'];
    if (raw !== undefined && raw !== '' && Number.isFinite(+raw)) return +raw;
    const est = g.attrs['data-tp-est'];
    return (est !== undefined && est !== '' && Number.isFinite(+est)) ? +est : null;
  };
  const tp = marks.map(g => ({ g, tp: tpVal(g) }))
    .filter(x => x.tp !== null && x.tp > 0);
  if (tp.length >= 2) {
    const lo = Math.min(...tp.map(x => x.tp)), hi = Math.max(...tp.map(x => x.tp));
    const fast = tp.find(x => x.tp === lo), slow = tp.find(x => x.tp === hi);
    const rf = +nodeOfMark(fast.g).attrs.r, rs = +nodeOfMark(slow.g).attrs.r;
    ok('任务面积边界：最快 100%（r=15）、最慢 4%（r=3），log10 反比归一、面积随 u 线性',
       Math.abs(taskAreaExpected(rf, 15) - 1) < 1e-9
       && Math.abs(taskAreaExpected(rs, 15) - 0.04) < 1e-9,
       `fast ${lo}s r=${rf}→${taskAreaExpected(rf, 15).toFixed(3)}；slow ${hi}s r=${rs}→${taskAreaExpected(rs, 15).toFixed(3)}`);
  }
  ok('任务模式不删环带节点（只靠 CSS 隐藏，切回能原样恢复）',
     chartSvg.$$attr('data-band-ring').length === bands0,
     `${chartSvg.$$attr('data-band-ring').length} 个环带`);
  ok('任务模式不改点坐标 / 前沿 / 能力线（只换半径）',
     JSON.stringify(marks.map(g => {
       const c = nodeOfMark(g);
       return [c.attrs.cx, c.attrs.cy];
     })) === JSON.stringify(positions0)
       && JSON.stringify(chartSvg.$$tag('polyline').map(p => p.attrs.points))
          === JSON.stringify(lines0),
     `${marks.length} 点`);

  box.checked = false;
  fire();
  const back = snap();
  ok('TPS 精确往返：切回 speed 后半径/族卡锚点/矩形/文字逐属性回到初始值',
     JSON.stringify(back) === JSON.stringify(base),
     JSON.stringify(back) === JSON.stringify(base) ? '' : '属性串不一致');

  // 悬停处理器在切换后仍可用；且悬停中的卡片要按新半径重贴而不是沿用旧锚点。
  // 必须挑一个两张模式半径真的不同的族，否则"锚点变了"这条判据会空转。
  const radiusOf = s => +(String(s || '').split(' ')[2] || NaN);
  const chipDiff = chips.find(g => Math.abs(radiusOf(g.attrs['data-ball'])
                                          - radiusOf(g.attrs['data-ball-task'])) > 1e-9);
  const famKey = chipDiff ? chipDiff.attrs['data-k']
                          : marks.find(g => g.attrs['data-k'])?.attrs['data-k'];
  const mark = marks.find(g => g.attrs['data-k'] === famKey);
  const chip = chips.find(g => g.attrs['data-k'] === famKey);
  if (mark && chartSvg.listeners.pointerover?.length && chartSvg.listeners.pointerleave?.length) {
    box.checked = true; fire();
    chartSvg.listeners.pointerover[0]({ target: mark });
    const top = chartSvg.$$tag('g').find(g => g._cls.has('toplayer'));
    ok('切换后悬停处理器仍有效：整族进顶层并加 .hi',
       !!top && chartSvg.$$attr('data-k', famKey).every(e => e._cls.has('hi')),
       top ? `${chartSvg.$$attr('data-k', famKey).length} 个族节点` : '没有 toplayer');
    const chipNow = chips.find(g => g.attrs['data-k'] === famKey);
    const anchorOk = !!chipNow && ['0', '1', '2'].every(i => Math.abs(
      +(chipNow.attrs['data-ball'] || '').split(' ')[i]
      - +(chipNow.attrs['data-ball-task'] || '').split(' ')[i]) < 1e-9);
    ok('悬停中的族卡按任务半径重贴（data-ball 的锚点/半径等于 data-ball-task）',
       !!chipDiff && anchorOk,
       `${chipDiff ? '有半径差族' : '没有半径差族'}｜${chipNow ? chipNow.attrs['data-ball'] + ' vs ' + chipNow.attrs['data-ball-task'] : '该族没有卡'}`);
    if (chartSvg.listeners.pointerleave?.length) chartSvg.listeners.pointerleave[0]({});
    box.checked = false; fire();
    ok('悬停测试后能切回 speed 且模式恢复',
       chartSvg.getAttribute('data-size-mode') === 'speed' && box.getAttribute('aria-checked') === 'false',
       '');
  } else {
    ok('状态栏切换后仍有悬停处理器', false, '缺少 pointerover/pointerleave');
  }
}

/* ---------- 大小模式偏好：状态栏原生注入 + 看板 localStorage ----------
 * 状态栏的持久化真源是 UserDefaults，页面只读 documentStart 注入的 MR_SIZE_TASK，
 * 用户切换时把选择回传给宿主。这里用桥桩模拟"宿主保存 → 重载/新实例"，
 * 并确认初始化不回传（否则每次重载都把刚读到的值再写一遍）。 */
console.log('\n== 大小模式持久化（状态栏 / 看板） ==');

function bootChartHtml(source, pref, bridge = true) {
  const svg = parseSvg(source.slice(source.indexOf('<svg'), source.indexOf('</svg>') + 6))
    .$$tag('svg')[0];
  const box = new El('input', { id: 'task-size', type: 'checkbox', role: 'switch',
                               'aria-checked': 'false' });
  const cap = new El('span', { id: 'size-caption' });
  const wrap = new El('div', { id: 'scatterwrap' });
  wrap.appendChild(svg);
  const doc = {
    querySelector: sel => (sel === '#scatterwrap svg' || sel === 'svg') ? svg : null,
    querySelectorAll: () => [],
    getElementById: id => id === 'task-size' ? box : id === 'size-caption' ? cap : null,
    createElementNS: (_ns, tag) => new El(tag),
    activeElement: null,
  };
  const sent = [];
  const win = { CSS: { escape: k => String(k) } };
  if (pref !== undefined) win.MR_SIZE_TASK = pref;
  if (bridge) {
    win.webkit = { messageHandlers: { mrSizeTask: { postMessage: v => sent.push(v) } } };
  }
  const scripts = [...source.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
  for (const src of scripts) new Function('document', 'window', 'CSS', src)(doc, win, win.CSS);
  return {
    svg, box, cap, sent,
    marks: svg.$$tag('g').filter(g => g._cls.has('mk')),
    nodeR: g => (g.$$tag('circle').find(c => c.attrs['data-role'] === 'node')
                 || g.$$tag('circle')[0]).attrs.r,
    fire: () => (box.listeners.change || []).forEach(fn => fn({ target: box })),
  };
}

function memoryStorage(seed = {}) {
  const map = new Map(Object.entries(seed));
  const calls = { set: 0, get: 0 };
  return {
    getItem: k => { calls.get++; return map.has(k) ? map.get(k) : null; },
    setItem: (k, v) => { calls.set++; map.set(k, String(v)); },
    _map: map, _calls: calls,
  };
}

const SIZE_MODE_KEY = 'modelradar.dashboard.sizeMode.v1';

if (!chartPath || !existsSync(chartPath)) {
  ok('状态栏持久化夹具可用', false, '找不到 chart.html，root 先跑一次渲染');
} else {
  const PCH = readFileSync(chartPath, 'utf8');

  const none = bootChartHtml(PCH, undefined);
  ok('状态栏无注入初值：默认速度、开关未勾选、初始化不回传',
     none.svg.getAttribute('data-size-mode') === 'speed'
       && none.box.checked === false && none.box.getAttribute('aria-checked') === 'false'
       && /输出速度/.test(none.cap.textContent) && none.sent.length === 0,
     `${none.svg.getAttribute('data-size-mode')}｜checked=${none.box.checked}｜sent=${none.sent.length}`);

  const restored = bootChartHtml(PCH, 1);
  ok('状态栏新实例注入 1：首帧即任务模式（checked/aria/caption/半径全恢复），且初始化不回传',
     restored.svg.getAttribute('data-size-mode') === 'task'
       && restored.box.checked === true && restored.box.getAttribute('aria-checked') === 'true'
       && /任务耗时/.test(restored.cap.textContent)
       && restored.marks.every(g => restored.nodeR(g) === (+g.attrs['data-r-task']).toFixed(1))
       && restored.sent.length === 0,
     `${restored.svg.getAttribute('data-size-mode')}｜sent=${restored.sent.length}｜${restored.marks.length} 点`);

  // 用户切换 → 宿主保存（桥桩）→ 重载/新实例
  const store = { pref: 0 };
  const u1 = bootChartHtml(PCH, store.pref);
  u1.box.checked = true; u1.fire();
  ok('用户切换后焦点留在开关上（随后按空格继续切换，不会落回 AppKit）',
     u1.box.focused === true, `focused=${u1.box.focused}`);
  store.pref = u1.sent[0];
  const u2 = bootChartHtml(PCH, store.pref);
  ok('状态栏用户切到任务：只回传一次 1，宿主保存后重载仍是 task 且不重复回传',
     u1.sent.length === 1 && u1.sent[0] === 1 && store.pref === 1
       && u2.box.checked === true && u2.svg.getAttribute('data-size-mode') === 'task'
       && u2.sent.length === 0,
     `sent=${JSON.stringify(u1.sent)}｜pref=${store.pref}｜reload=${u2.svg.getAttribute('data-size-mode')}`);

  u2.box.checked = false; u2.fire();
  store.pref = u2.sent[0];
  const u3 = bootChartHtml(PCH, store.pref);
  ok('状态栏切回速度同样持久化：重载后恢复 speed',
     u2.sent.length === 1 && u2.sent[0] === 0 && u3.box.checked === false
       && u3.svg.getAttribute('data-size-mode') === 'speed'
       && u3.box.getAttribute('aria-checked') === 'false',
     `sent=${JSON.stringify(u2.sent)}｜reload=${u3.svg.getAttribute('data-size-mode')}`);

  const noBridge = bootChartHtml(PCH, 0, false);
  let bridgeErr = '';
  try { noBridge.box.checked = true; noBridge.fire(); }
  catch (e) { bridgeErr = String(e && e.message || e).slice(0, 120); }
  ok('状态栏没有脚本桥（浏览器/测试）：切换照常生效、不抛错、不误写',
     !bridgeErr && noBridge.svg.getAttribute('data-size-mode') === 'task'
       && noBridge.box.getAttribute('aria-checked') === 'true' && noBridge.sent.length === 0,
     bridgeErr || `mode=${noBridge.svg.getAttribute('data-size-mode')}`);

  const beforeTask = [...PCH.matchAll(/data-r-task="([^"]+)"/g)].map(m => +m[1]);
  const changedHTML = PCH.replace(/data-r-task="([^"]+)"/g,
    (_, v) => `data-r-task="${(Number(v) + 1).toFixed(1)}"`);
  const changed = bootChartHtml(changedHTML, 1);
  const changedTask = changed.marks.map(g => +g.attrs['data-r-task']);
  const stale = changed.marks.filter(g =>
    changed.nodeR(g) !== (+g.attrs['data-r-task']).toFixed(1));
  ok('数据更新后注入 1：半径按新 data-r-task 重算，不沿用上一版缓存',
     changedTask.length > 0 && changedTask.some((v, i) => v !== beforeTask[i])
       && stale.length === 0 && changed.svg.getAttribute('data-size-mode') === 'task',
     `改值 ${changedTask.length} 个；沿用旧值 ${stale.length} 个`);
}

// 看板侧：真 onchange + 真 SVG 解析。渲染重写 innerHTML 后必须在新 svg 上重绑悬停。
function renderDom(D, localStorage = null) {
  const sinks = {};
  const mk = id => {
    if (sinks[id]) return sinks[id];
    const attrs = id === 'sizebytask'
      ? { id, type: 'checkbox', role: 'switch', 'aria-checked': 'false' }
      : { id };
    const el = new El(id === 'sizebytask' ? 'input' : 'div', attrs);
    el.id = id;
    el.dataset = {}; el.style = {}; el.checked = false;
    el.onclick = null; el.oninput = null; el.onchange = null;
    el._html = '';
    Object.defineProperty(el, 'innerHTML', {
      get() { return this._html; },
      set(v) {
        this._html = String(v);
        this.children = [];
        if (this.id === 'scatterwrap' && /<svg\b/.test(this._html)) {
          const sv = parseSvg(this._html).$$tag('svg')[0];
          if (sv) { sv.parentNode = this; this.children = [sv]; this._link(); }
        }
        // 真 DOM 里 innerHTML 会建子节点，关闭按钮的 querySelector('.x') 才拿得到。
        // 桩原先只解析散点 SVG，于是页面的 data_state='stale' 时横幅一写就抛 null.onclick。
        for (const m of this._html.matchAll(/<span\b([^>]*)>([\s\S]*?)<\/span>/g)) {
          const attrs = {};
          for (const a of m[1].matchAll(/([\w:-]+)="([^"]*)"/g)) attrs[a[1]] = a[2];
          const span = new El('span', attrs);
          span.textContent = m[2];
          this.appendChild(span);
        }
      },
    });
    sinks[id] = el;
    return el;
  };
  const ths = [...html.matchAll(/<th\b([^>]*)>/g)].map((m, i) => {
    const el = mk(`th#${i}`);
    el.dataset = { k: (m[1].match(/data-k="([^"]+)"/) || [])[1] || '' };
    return el;
  });
  const document = {
    getElementById: id => mk(id),
    querySelectorAll: sel => {
      if (sel === 'th') return ths;
      if (sel === '.srcs') return [mk('srcs#0'), mk('srcs#1')];
      return [];
    },
    querySelector: sel => {
      if (sel === '#scatterwrap svg' || sel === 'svg') {
        const sw = sinks.scatterwrap;
        return sw ? (sw.children.find(c => c.tag === 'svg') || null) : null;
      }
      return null;
    },
    createElementNS: (_ns, tag) => new El(tag),
    createElement: () => ({ style: {}, className: '', textContent: '', replaceWith() {} }),
    documentElement: { dataset: {} },
  };
  const win = Object.assign({ MODEL_DATA: D, __MR_HARNESS__: true },
                            { CSS: { escape: k => String(k) } });
  new Function('window', 'document', 'location', 'setInterval', 'CSS', 'localStorage', code)(
    win, document, { reload() {} }, () => {}, win.CSS, localStorage);
  sinks.__mrWindow = win;
  return sinks;
}

if (D0.models.length) {
  const D = structuredClone(D0);
  // 摆一个缺任务耗时的真实可画点：缺值时必须是 2px 空心，不许拿 TPS 兜底。
  const srcM = D.models.find(m => m.index !== null && m.apt_usd > 0);
  const miss = structuredClone(srcM);
  Object.assign(miss, { name: 'Planted No Task', series_key: 'planted-no-task',
                        series: 'Planted No Task', time_per_task_s: null });
  for (const k of ['time_per_task_est', 'time_per_task_est_s', 'time_per_task_est_range',
                   'time_per_task_est_basis', 'time_per_task_band'])
    delete miss[k];
  D.models.push(miss);
  // 派生任务耗时的两条新分支也各摆一个真点：只用区间时必须仍是空心（不许凭空造中值），
  // 有估计点时必须按估计点定大小并在表/悬停里标 ≈。
  const cloneOf = (n, key, patch, drop) => {
    const c = structuredClone(srcM);
    Object.assign(c, { name: n, series_key: key, series: n }, patch);
    for (const k of drop) delete c[k];
    return c;
  };
  const estM = cloneOf('Planted Est Task', 'planted-est-task',
    { time_per_task_s: null, time_per_task_est: true, time_per_task_est_s: 240,
      time_per_task_est_range: [200, 300], time_per_task_est_basis: 'series-interp' },
    ['time_per_task_band']);
  const bandM = cloneOf('Planted Band Task', 'planted-band-task',
    { time_per_task_s: null, time_per_task_est: true, time_per_task_band: [900, 1800] },
    ['time_per_task_est_s', 'time_per_task_est_range', 'time_per_task_est_basis']);
  D.models.push(estM, bandM);
  const s = renderDom(D);
  const sw = s.scatterwrap, toggle = s.sizebytask, caption = s['size-caption'];
  const svg0 = sw.children.find(c => c.tag === 'svg');
  const initialHtml = sw.innerHTML;
  const initialLegend = s.legend.innerHTML;
  const fireToggle = () => toggle.onchange({ target: toggle });
  const marksOf = sv => sv.$$attr('class').filter(g => g._cls.has('mk'));
  const circleOf = g => g.$$tag('circle').find(c => !c.attrs['data-band-ring'] && c.$$tag('title').length)
    || g.$$tag('circle')[0];

  ok('看板默认 TPS：sizebytask 关闭、role=switch、caption=输出速度',
     toggle.checked === false && toggle.getAttribute('role') === 'switch'
       && toggle.getAttribute('aria-checked') === 'false'
       && /输出\s*tok\/s/.test(caption.textContent),
     `${toggle.getAttribute('role')}｜${toggle.getAttribute('aria-checked')}｜${caption.textContent}`);

  toggle.checked = true;
  fireToggle();
  const svg1 = sw.children.find(c => c.tag === 'svg');
  const marks1 = marksOf(svg1);
  const dom1 = { min: +(svg1.getAttribute('data-task-min') || NaN),
                 max: +(svg1.getAttribute('data-task-max') || NaN) };
  ok('看板键盘/开关切换：aria-checked=true、mode task、caption 换任务说明',
     toggle.getAttribute('aria-checked') === 'true'
       && svg1.getAttribute('data-size-mode') === 'task'
       && /任务耗时/.test(caption.textContent)
       && /任务越快/.test(caption.textContent),
     `${svg1.getAttribute('data-size-mode')}｜${caption.textContent}`);

  const validTask = marks1.map(g => ({ g, t: g.getAttribute('data-task') }))
    .filter(x => x.t !== null && x.t !== '' && +x.t > 0);
  const geomBad = [];
  for (const { g, t } of validTask) {
    const v = +t, lo = dom1.min, hi = dom1.max;
    const u = hi > lo ? Math.min(1, Math.max(0, (Math.log10(hi) - Math.log10(v))
                                            / (Math.log10(hi) - Math.log10(lo)))) : 0.5;
    const want = 14 * Math.sqrt(0.04 + 0.96 * u);
    const got = +(circleOf(g).getAttribute('r'));
    if (Math.abs(got - want) > 0.02) geomBad.push(`${g.getAttribute('data-n')}: ${got} ≠ ${want}`);
  }
  ok('看板任务半径逐点复算：r=14·√(.04+.96u)，u 由当前图上有效耗时 log10 反比归一',
     validTask.length > 0 && geomBad.length === 0,
     `${validTask.length} 个有效点；${geomBad.slice(0, 2).join(' | ')}`);

  if (validTask.length >= 2) {
    const loT = Math.min(...validTask.map(x => +x.t));
    const hiT = Math.max(...validTask.map(x => +x.t));
    const fast = validTask.find(x => +x.t === loT), slow = validTask.find(x => +x.t === hiT);
    const rf = +circleOf(fast.g).getAttribute('r');
    const rs = +circleOf(slow.g).getAttribute('r');
    ok('看板任务面积边界：最快 100%（r=14）、最慢 4%（r=2.8）',
       Math.abs(taskAreaExpected(rf, 14) - 1) < 1e-9
       && Math.abs(taskAreaExpected(rs, 14) - 0.04) < 1e-9,
       `fast ${loT}s r=${rf}；slow ${hiT}s r=${rs}`);
  }
  const missMark = marks1.find(g => g.getAttribute('data-task-missing') === '1');
  ok('看板缺任务耗时：data-task-missing、类 taskmiss、r=2、fill=none、且组内没有 <image>（空心，不拿 TPS/logo 兜底）',
     !!missMark && missMark._cls.has('taskmiss')
       && +circleOf(missMark).getAttribute('r') === 2
       && circleOf(missMark).getAttribute('fill') === 'none'
       && missMark.$$tag('image').length === 0,
     missMark ? `r=${circleOf(missMark).getAttribute('r')} fill=${circleOf(missMark).getAttribute('fill')} images=${missMark.$$tag('image').length}` : '没有缺耗时点');

  // 派生任务耗时：估计点要按估计值定大小并标 ≈；只有区间时必须仍是空心环（不凭空造中值），
  // 但区间照旧写进悬停与表格 —— 描小了不等于把数藏起来。
  // 散点 <g> 只带 data-k（族键），族键就是 series_key。
  const markOf = key => marks1.find(g => g.getAttribute('data-k') === key) || null;
  const estTaskMark = markOf('planted-est-task');
  const bandTaskMark = markOf('planted-band-task');
  const tipTextOf = g => {
    const t = circleOf(g).$$tag('title')[0];
    return t ? t.text || '' : '';
  };
  const estR = estTaskMark ? +circleOf(estTaskMark).getAttribute('r') : NaN;
  const estU = dom1.max > dom1.min
    ? Math.min(1, Math.max(0, (Math.log10(dom1.max) - Math.log10(240))
      / (Math.log10(dom1.max) - Math.log10(dom1.min)))) : 0.5;
  ok('看板任务估计点：按 estimate 定半径、带 data-task-est、悬停标 ≈ 与口径',
     !!estTaskMark && Math.abs(estR - 14 * Math.sqrt(0.04 + 0.96 * estU)) <= 0.02
       && estTaskMark.getAttribute('data-task-est') === '240'
       && !estTaskMark._cls.has('est') && !estTaskMark._cls.has('band')
       && /任务耗时 ≈4min/.test(tipTextOf(estTaskMark))
       && /同族相邻档插值/.test(tipTextOf(estTaskMark)),
     estTaskMark ? `r=${estR} est=${estTaskMark.getAttribute('data-task-est')}｜${tipTextOf(estTaskMark).slice(0, 80)}` : '没有估计点标记');
  ok('看板任务只有区间：仍画 2px 空心环、带 data-task-band，悬停给区间且不含 ≈',
     !!bandTaskMark && bandTaskMark._cls.has('taskmiss')
       && bandTaskMark.getAttribute('data-task-missing') === '1'
       && +circleOf(bandTaskMark).getAttribute('r') === 2
       && circleOf(bandTaskMark).getAttribute('fill') === 'none'
       && bandTaskMark.getAttribute('data-task-band') === '900 1800'
       && bandTaskMark.getAttribute('data-task-est') === null
       && /任务耗时 15min–30min/.test(tipTextOf(bandTaskMark))
       && /同族或同级参照区间/.test(tipTextOf(bandTaskMark))
       && !/≈/.test(tipTextOf(bandTaskMark)),
     bandTaskMark ? `band=${bandTaskMark.getAttribute('data-task-band')}｜${tipTextOf(bandTaskMark).slice(0, 80)}` : '没有区间任务标记');
  const tbHtml = s.tb?.innerHTML ?? '';
  const taskCells = tbHtml.split('<tr>').slice(1)
    .map(tr => [...tr.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map(m => m[1]));
  const cellOfName = n => taskCells.find(r => r[0] && r[0].includes(n));
  const estCell = cellOfName('Planted Est Task'), bandCell = cellOfName('Planted Band Task');
  ok('表格任务列：估计点显示 ≈4min，只有区间的显示 15min–30min 且不带「估」/「带」标签',
     !!estCell && estCell[3].replace(/<[^>]*>/g, '') === '≈4min'
       && !/估/.test(estCell[3]) && !/带/.test(estCell[3])
       && !!bandCell && bandCell[3].replace(/<[^>]*>/g, '') === '15min–30min'
       && !/估/.test(bandCell[3]) && !/带/.test(bandCell[3]) && !/≈/.test(bandCell[3]),
     `est=${estCell ? estCell[3] : '—'} band=${bandCell ? bandCell[3] : '—'}`);
  const logoBad = marks1.filter(g => g.$$tag('image').length).filter(g => {
    const r = +circleOf(g).getAttribute('r'), im = g.$$tag('image')[0];
    const cx = +circleOf(g).getAttribute('cx'), cy = +circleOf(g).getAttribute('cy');
    return Math.abs(+im.getAttribute('width') - 2 * r) > 0.02
        || Math.abs(+im.getAttribute('height') - 2 * r) > 0.02
        || Math.abs(+im.getAttribute('x') - (cx - r)) > 0.02
        || Math.abs(+im.getAttribute('y') - (cy - r)) > 0.02;
  });
  ok('看板 logo 尺寸随任务半径：边长=2r 且中心与气泡同心',
     marks1.some(g => g.$$tag('image').length) && logoBad.length === 0,
     `${marks1.filter(g => g.$$tag('image').length).length} 个 logo；坏 ${logoBad.length}`);
  const tipCovered = marks1.filter(g => {
    const im = g.$$tag('image')[0];
    const c = circleOf(g);
    return im && (im.getAttribute('pointer-events') !== 'none'
                  || c.$$tag('title').length === 0);
  });
  ok('看板有效 logo 不挡 tooltip：<image pointer-events="none">，<title> 仍在气泡圆内',
     marks1.some(g => g.$$tag('image').length) && tipCovered.length === 0,
     `挡住的 ${tipCovered.length} 个`);
  ok('看板任务模式隐藏 TPS 推估/参照带（不把旧环带留在新尺寸上）',
     marks1.every(g => g.getAttribute('data-band') === null && g.getAttribute('data-est') === null),
     '');
  // 图例圆也用同一 u/面积映射（LEG_R_MAX=34），不是只改散点。
  const legendBad = [];
  const legendTask = s.legend.innerHTML.split('<div class="lg"').slice(1).map(b => {
    const k = (b.match(/data-k="([^"]*)"/) || [])[1] || '';
    const w = +((b.match(/width:(\d+)px/) || [])[1]);
    const m = D.models.find(x => x.name === k.replace(/&amp;/g, '&').replace(/&quot;/g, '"'));
    if (!m) return null;
    // 图例与散点同一优先级：官方 > 0，否则用派生估计点（只看区间仍算缺失，画空心环）。
    const num = v => (typeof v === 'number' && Number.isFinite(v) ? v : null);
    const off = num(m.time_per_task_s);
    const est = num(m.time_per_task_est_s);
    const t = (off !== null && off > 0) ? off : (est !== null && est > 0 ? est : null);
    if (t === null) {
      if (w !== 4) legendBad.push(`${k}: 缺失应 4px，实际 ${w}`);
      return w;
    }
    const lo = dom1.min, hi = dom1.max;
    const u = hi > lo ? Math.min(1, Math.max(0, (Math.log10(hi) - Math.log10(t))
                                            / (Math.log10(hi) - Math.log10(lo)))) : 0.5;
    const want = Math.round(2 * 34 * Math.sqrt(0.04 + 0.96 * u));
    if (Math.abs(w - want) > 1) legendBad.push(`${k}: ${w} ≠ ${want}`);
    return w;
  }).filter(v => v !== null);
  ok('看板图例直径 = 2·34·√(.04+.96u)，缺耗时=4px 空心',
     legendTask.length > 0 && legendBad.length === 0,
     `${legendTask.length} 格；${legendBad.slice(0, 2).join(' | ')}`);
  const missLegend = s.legend.innerHTML.split('<div class="lg"').slice(1)
    .find(b => /data-task-missing="1"/.test(b));
  ok('看板图例缺任务耗时不画 logo（空心环，不拿图标兜底）',
     !!missLegend && !/<img\b/.test(missLegend),
     missLegend ? (/<img\b/.test(missLegend) ? '仍有 <img>' : '无 <img>') : '没有缺耗时图例');

  const lines0 = [...initialHtml.matchAll(/<polyline\b([^>]*)>/g)].map(m => m[1]);
  const lines1 = [...sw.innerHTML.matchAll(/<polyline\b([^>]*)>/g)].map(m => m[1]);
  ok('看板前沿/能力线位置与点数不因切换改变',
     JSON.stringify(lines0) === JSON.stringify(lines1), `${lines0.length} vs ${lines1.length}`);
  const names0 = [...initialHtml.matchAll(/<g class="mk[^"]*"[^>]*data-k="([^"]*)"/g)].map(m => m[1]);
  const names1 = [...sw.innerHTML.matchAll(/<g class="mk[^"]*"[^>]*data-k="([^"]*)"/g)].map(m => m[1]);
  ok('看板排序/点序不因切换改变', JSON.stringify(names0) === JSON.stringify(names1), '');

  // 悬停处理器必须绑在重渲染后的新 svg 上，不是旧节点。
  if (svg1.listeners.pointerover?.length) {
    const mark = marks1.find(g => g.getAttribute('data-k'));
    svg1.listeners.pointerover[0]({ target: mark });
    const top = svg1.$$tag('g').find(g => g._cls.has('toplayer'));
    ok('看板重渲染后悬停仍有效：新 svg 的处理器把整族搬进顶层',
       !!top && svg1.$$attr('data-k', mark.getAttribute('data-k')).every(e => e._cls.has('hi')),
       top ? `${svg1.$$attr('data-k', mark.getAttribute('data-k')).length} 个族节点` : '没有 toplayer');
    if (svg1.listeners.pointerleave?.length) svg1.listeners.pointerleave[0]({});
  } else {
    ok('看板重渲染后仍绑着悬停处理器', false, '新 svg 没有 pointerover');
  }

  toggle.checked = false;
  fireToggle();
  ok('看板 TPS 精确往返：切回后散点标记逐字回到初始 HTML（环带/描边/logo 一并恢复）',
     sw.innerHTML === initialHtml,
     sw.innerHTML === initialHtml ? '' : '重渲染串不一致');
  ok('看板图例也逐字回到初始 HTML（圆径/任务说明一并恢复）',
     s.legend.innerHTML === initialLegend, '');
  ok('看板切回后 aria-checked=false 且 caption 回到输出速度',
     toggle.getAttribute('aria-checked') === 'false' && /输出\s*tok\/s/.test(caption.textContent), '');

  // ---- 持久化：带版本键的 localStorage；初始化只读，用户切换才写 ----
  const emptyStore = memoryStorage();
  const sd = renderDom(structuredClone(D0), emptyStore);
  const sdsvg = sd.scatterwrap.children.find(c => c.tag === 'svg');
  ok('看板存储为空：默认 speed，初始化不回写',
     sd.sizebytask.checked === false && sd.sizebytask.getAttribute('aria-checked') === 'false'
       && sdsvg.getAttribute('data-size-mode') === 'speed'
       && /输出\s*tok\/s/.test(sd['size-caption'].textContent)
       && emptyStore._calls.set === 0,
     `checked=${sd.sizebytask.checked}｜set=${emptyStore._calls.set}`);

  const taskStore = memoryStorage({ [SIZE_MODE_KEY]: 'task' });
  const st = renderDom(structuredClone(D0), taskStore);
  const stsvg = st.scatterwrap.children.find(c => c.tag === 'svg');
  ok('看板新实例从带版本键恢复 task，且初始化不回写',
     st.sizebytask.checked === true && st.sizebytask.getAttribute('aria-checked') === 'true'
       && stsvg.getAttribute('data-size-mode') === 'task'
       && /任务耗时/.test(st['size-caption'].textContent)
       && taskStore._calls.set === 0,
     `set=${taskStore._calls.set}｜${st['size-caption'].textContent}`);
  const stDom = { min: +(stsvg.getAttribute('data-task-min') || NaN),
                  max: +(stsvg.getAttribute('data-task-max') || NaN) };
  const stValid = marksOf(stsvg).map(g => ({ g, t: g.getAttribute('data-task') }))
    .filter(x => x.t !== null && x.t !== '' && +x.t > 0);
  const stBad = stValid.filter(({ g, t }) => {
    const v = +t;
    const u = stDom.max > stDom.min
      ? Math.min(1, Math.max(0, (Math.log10(stDom.max) - Math.log10(v))
                              / (Math.log10(stDom.max) - Math.log10(stDom.min))))
      : 0.5;
    return Math.abs(+circleOf(g).getAttribute('r') - 14 * Math.sqrt(0.04 + 0.96 * u)) > 0.02;
  });
  ok('看板从存储恢复时半径按当前数据集重算（不是只恢复开关文案）',
     stValid.length > 0 && stBad.length === 0,
     `${stValid.length} 点，错 ${stBad.length}`);

  const writeStore = memoryStorage();
  const w = renderDom(structuredClone(D0), writeStore);
  w.sizebytask.checked = true;
  w.sizebytask.onchange({ target: w.sizebytask });
  const reloaded = renderDom(structuredClone(D0), writeStore);
  const reloadedSvg = reloaded.scatterwrap.children.find(c => c.tag === 'svg');
  ok('看板用户切换只写一次带版本键，重载（新实例）恢复 task 且不重复写',
     writeStore._calls.set === 1 && writeStore._map.get(SIZE_MODE_KEY) === 'task'
       && reloaded.sizebytask.checked === true
       && reloadedSvg.getAttribute('data-size-mode') === 'task'
       && /任务耗时/.test(reloaded['size-caption'].textContent)
       && writeStore._calls.set === 1,
     `set=${writeStore._calls.set}｜${JSON.stringify([...writeStore._map])}`);

  const blocked = {
    getItem() { throw new Error('SecurityError: storage disabled'); },
    setItem() { throw new Error('SecurityError: storage disabled'); },
  };
  let blockedErr = '';
  let bd = null;
  try { bd = renderDom(structuredClone(D0), blocked); }
  catch (e) { blockedErr = String(e && e.message || e).slice(0, 120); }
  const bdsvg = bd && bd.scatterwrap.children.find(c => c.tag === 'svg');
  ok('看板禁用存储：读取失败不抛错，退回默认 speed',
     !blockedErr && !!bd && bd.sizebytask.checked === false
       && bdsvg.getAttribute('data-size-mode') === 'speed',
     blockedErr || '');
  if (bd) {
    bd.sizebytask.checked = true;
    try { bd.sizebytask.onchange({ target: bd.sizebytask }); }
    catch (e) { blockedErr = String(e && e.message || e).slice(0, 120); }
  }
  const bdsvg2 = bd && bd.scatterwrap.children.find(c => c.tag === 'svg');
  ok('看板禁用存储：切换仍生效、caption 正常，只是不记忆',
     !!bd && !blockedErr && bd.sizebytask.checked === true
       && bdsvg2.getAttribute('data-size-mode') === 'task'
       && /任务耗时/.test(bd['size-caption'].textContent),
     blockedErr || '');

  // 数据换了（这里把所有可画点调成同一耗时）仍要从存储恢复 task，
  // 并且半径按**新**数据域重算：min=max → u=.5 → r=14√(.04+.96×.5)。
  const D2 = structuredClone(D0);
  for (const m of D2.models) {
    // 现在还有"官方耗时缺失、只有派生估计点"的行；夹具要让所有可画点同域，
    // 两条来源都得压成 500，否则估计点会把 min/max 拉回真实数据。
    if ((typeof m.time_per_task_s === 'number' && m.time_per_task_s > 0)
        || (typeof m.time_per_task_est_s === 'number' && m.time_per_task_est_s > 0)) {
      m.time_per_task_s = 500;
    }
  }
  const dynStore = memoryStorage({ [SIZE_MODE_KEY]: 'task' });
  const dyn = renderDom(D2, dynStore);
  const dynSvg = dyn.scatterwrap.children.find(c => c.tag === 'svg');
  const dynDom = { min: +(dynSvg.getAttribute('data-task-min') || NaN),
                   max: +(dynSvg.getAttribute('data-task-max') || NaN) };
  const dynValid = marksOf(dynSvg).map(g => ({ g, t: g.getAttribute('data-task') }))
    .filter(x => x.t !== null && x.t !== '' && +x.t > 0);
  const dynBad = dynValid.filter(({ g }) =>
    Math.abs(+circleOf(g).getAttribute('r')
             - 14 * Math.sqrt(0.04 + 0.96 * 0.5)) > 0.02);
  ok('数据更新后 caption 仍按存储的模式显示，不回落速度',
     dyn.sizebytask.checked === true && dynSvg.getAttribute('data-size-mode') === 'task'
       && /任务耗时/.test(dyn['size-caption'].textContent), '');
  ok('数据更新后半径按新数据域重算：全部同耗时 → u=.5 → r=14√.52',
     dynDom.min === 500 && dynDom.max === 500 && dynValid.length > 0 && dynBad.length === 0,
     `域 ${dynDom.min}~${dynDom.max}；${dynValid.length} 点，错 ${dynBad.length}`);
}

/* ---------- 跨运行时几何/比值：真 Python 函数 vs 页面真函数 ---------- */
{
  const win = render(D0, false).__mrWindow;
  const T = win.__mrTest || {};
  const PY = existsSync(join(HERE, '.venv/bin/python'))
    ? join(HERE, '.venv/bin/python') : 'python3';
  const PY_TASK = 'import json,sys\n'
    + 'sys.path.insert(0, sys.argv[1])\n'
    + 'import render_scatter_svg as R\n'
    + 'p = json.load(sys.stdin)\n'
    + 'sys.stdout.write(json.dumps({\n'
    + '  "radius": [R.task_radius(v, p["min"], p["max"]) for v in p["values"]],\n'
    + '  "equal": R.task_radius(p["equal"], p["equal"], p["equal"]),\n'
    + '  "area_min": (R.task_radius(p["min"], p["min"], p["max"]) / R.R_MAX) ** 2,\n'
    + '  "area_max": (R.task_radius(p["max"], p["min"], p["max"]) / R.R_MAX) ** 2\n'
    + '}))';
  const payload = { min: 10, max: 1000, equal: 100,
                    values: [10, 50, 100, 500, 1000, 2000, 5, null, 0, -1] };
  let py = null, pyErr = '';
  try {
    py = JSON.parse(execFileSync(PY, ['-B', '-c', PY_TASK, HERE],
                                { input: JSON.stringify(payload), encoding: 'utf8', timeout: 60000 }));
  } catch (e) { pyErr = String(e && e.message || e).replace(/\n/g, ' ').slice(0, 160); }
  const dom = { min: payload.min, max: payload.max };
  const jsR = payload.values.map(v => (T.taskScatR ? T.taskScatR(v, dom) : null));
  // 画布 R_MAX 不同（Python 15 / 看板 14），比面积分数而不是绝对像素。
  const areaBad = [];
  if (py) {
    payload.values.forEach((v, i) => {
      const ja = jsR[i] === null ? null : (jsR[i] / 14) ** 2;
      const pa = (py.radius[i] / 15) ** 2;
      if (ja === null || Math.abs(ja - pa) > 0.005)
        areaBad.push(`${v}: jsArea=${ja === null ? '—' : ja.toFixed(4)} pyArea=${pa.toFixed(4)}`);
    });
  }
  ok('Python task_radius 与看板 taskScatR 逐探针同构（log10 反比、夹紧、缺失→2；面积分数误差≤0.005）',
     !!py && areaBad.length === 0, pyErr || areaBad.slice(0, 3).join(' | '));
  ok('相等耗时边界两面都取 u=.5，面积分数 .52（不是旧 u² 的 .28）',
     !!py && Math.abs((T.taskUOf(payload.equal, { min: 100, max: 100 }) ?? NaN) - 0.5) < 1e-12
       && Math.abs((py.equal / 15) ** 2 - (T.taskAreaOf?.(0.5) ?? NaN)) < 0.005
       && Math.abs(T.taskAreaOf(0.5) - 0.52) < 1e-12,
     py ? `py r=${py.equal} · js u=${T.taskUOf(payload.equal, { min: 100, max: 100 })}` : pyErr);
  const decadeAreas = [10, 100, 1000].map(t => (T.taskScatR(t, { min: 10, max: 1000 }) / 14) ** 2);
  ok('面积 .04→1 边界两面一致，且 log10 等步：10/100/1000 → 1/.52/.04',
     !!py && Math.abs(py.area_min - 1) < 1e-9 && Math.abs(py.area_max - 0.04) < 1e-9
       && Math.abs(T.taskAreaOf(0) - 0.04) < 1e-12 && Math.abs(T.taskAreaOf(1) - 1) < 1e-12
       && decadeAreas.every((a, i) => Math.abs(a - [1, 0.52, 0.04][i]) < 0.005),
     py ? `py ${py.area_min.toFixed(3)}/${py.area_max.toFixed(3)}；js ${decadeAreas.map(a => a.toFixed(3)).join('/')}` : pyErr);
  // 看板真函数：>100 不夹、0 有效、half-up；与 Python 的数值口径一致。
  // 看板 thinkRatioOf 从 total_s/tok_s 现算 think；tok=100 时 think=total−5。
  const ratioProbes = [
    { think: 0, task: 100, want: 0 },
    { think: 1.25, task: 100, want: 1.25 },
    { think: 950, task: 100, want: 950 },
    { think: 5, task: 0, want: null },
    { think: null, task: 100, want: null },
  ];
  const ratioBad = ratioProbes.filter(p => {
    const model = { total_s: p.think === null ? null : p.think + 5,
                    tok_s: 100, time_per_task_s: p.task };
    const v = T.thinkRatioOf ? T.thinkRatioOf(model) : undefined;
    return (p.want === null && v !== null) || (p.want !== null && Math.abs(v - p.want) > 1e-12);
  });
  ok('看板 thinkRatioOf 数值口径：0 有效、>100 不夹、缺任一侧/任务<=0 为 null',
     typeof T.thinkRatioOf === 'function' && ratioBad.length === 0,
     ratioBad.map(p => `${p.think}/${p.task}`).join(' | '));

  // 显示文本也必须同源：真 Python think_ratio_cell 对真 JS thinkRatioCell。
  const PY_RTEXT = 'import json,sys\n'
    + 'sys.path.insert(0, sys.argv[1])\n'
    + 'import render_scatter_svg as R\n'
    + 'vals = json.load(sys.stdin)\n'
    + 'sys.stdout.write(json.dumps([R.think_ratio_cell({"think": v, "tp": 100}) for v in vals]))';
  const rtextValues = [65, 5, 0, 109.75];
  let pyText = null, pyTextErr = '';
  try {
    pyText = JSON.parse(execFileSync(PY, ['-B', '-c', PY_RTEXT, HERE],
                                    { input: JSON.stringify(rtextValues), encoding: 'utf8', timeout: 60000 }));
  } catch (e) { pyTextErr = String(e && e.message || e).replace(/\n/g, ' ').slice(0, 160); }
  const jsText = rtextValues.map(v => T.thinkRatioCell
    ? T.thinkRatioCell({ total_s: v + 5, tok_s: 100, time_per_task_s: 100 }) : null);
  const wantText = ['65%', '5%', '0%', '109.8%'];
  ok('思考前摇显示文本跨运行时逐字一致（真 Python think_ratio_cell vs 真 JS thinkRatioCell；65/5/0/109.75→109.8）',
     !!pyText && JSON.stringify(pyText) === JSON.stringify(jsText)
       && JSON.stringify(jsText) === JSON.stringify(wantText),
     pyTextErr || `py=${JSON.stringify(pyText)} js=${JSON.stringify(jsText)}`);

  // 任务耗时的三种形态（官方 0 也要显示、派生点带 ≈、只有区间给区间）必须两运行时逐字一致。
  const PY_CELL = 'import json,sys\n'
    + 'sys.path.insert(0, sys.argv[1])\n'
    + 'import render_scatter_svg as R\n'
    + 'rows = json.load(sys.stdin)\n'
    + 'sys.stdout.write(json.dumps([{"c": R.task_cell(r), "n": R.task_note(r)} for r in rows]))';
  // 同一批探针用两种键名喂两侧：Python 侧是 load_points 之后的短键，JS 侧是 payload 原始键。
  const cellProbes = [
    { time_per_task_s: 0 },
    { time_per_task_s: 17.25 },
    { time_per_task_s: 3599.9 },
    { time_per_task_s: null, time_per_task_est_s: 90, time_per_task_est_basis: 'measured-speed',
      time_per_task_est_range: [60, 140] },
    { time_per_task_s: null, time_per_task_est_s: 120, time_per_task_est_basis: 'series-interp' },
    { time_per_task_s: null, time_per_task_band: [56, 111] },
    { time_per_task_s: null, time_per_task_band: [111, 56] },
    { time_per_task_s: null, time_per_task_est_s: 0, time_per_task_band: [30, 90] },
    { time_per_task_s: null },
  ];
  const toPyRow = r => ({
    tp: r.time_per_task_s ?? null,
    tpe: r.time_per_task_est_s ?? null,
    tp_basis: r.time_per_task_est_basis ?? null,
    tpe_range: r.time_per_task_est_range ?? null,
    tpb: r.time_per_task_band ?? null,
  });
  let pyCell = null, pyCellErr = '';
  try {
    pyCell = JSON.parse(execFileSync(PY, ['-B', '-c', PY_CELL, HERE],
      { input: JSON.stringify(cellProbes.map(toPyRow)), encoding: 'utf8', timeout: 60000 }));
  } catch (e) { pyCellErr = String(e && e.message || e).replace(/\n/g, ' ').slice(0, 160); }
  const jsCell = cellProbes.map(r => ({
    c: T.taskTextOf ? T.taskTextOf(r) : null,
    n: T.taskNoteOf ? T.taskNoteOf(r) : null,
  }));
  const wantCell = [
    { c: '0s', n: '' },
    { c: '17.3s', n: '' },
    { c: '60min', n: '' },
    { c: '≈1.5min', n: '实测速度换算，1min–2.3min' },
    { c: '≈2min', n: '同族相邻档插值' },
    { c: '56s–1.9min', n: '同族或同级参照区间' },
    { c: '—', n: '' },
    { c: '30s–1.5min', n: '同族或同级参照区间' },
    { c: '—', n: '' },
  ];
  ok('任务耗时三态（官方含 0 / 派生点带 ≈ / 只有区间）跨运行时逐字一致',
     !!pyCell && JSON.stringify(pyCell) === JSON.stringify(jsCell)
       && JSON.stringify(jsCell) === JSON.stringify(wantCell),
     pyCellErr || `py=${JSON.stringify(pyCell)} js=${JSON.stringify(jsCell)}`);
  // 口径说明不能撞上速度列的判据子串，否则两列的"估/带"计数互相误伤。
  ok('任务耗时口径说明不含「（估计」「（同级能力参照」这类速度列判据子串',
     jsCell.every(x => !x.n.includes('（估计') && !x.n.includes('（同级能力参照')), '');
}

/* ---------- 图例 / 族卡联动悬停（判据全部落在**运行产物**上） ----------
 * 形状（2026-10-13 改版）：一族一张**合并弹窗**（每档一行详述），贴在**该族中心点的左上角**
 * （卡右下角对着那颗球）；图例悬停弹"综述卡（按系列一行）+ 各系列族卡"。
 * 出不出卡由顶栏「▤ 卡片」开关统一控制（window.MR_CARDS）。
 * 判据都读运行产物 chart.html 并把**产物这张 SVG** 喂给共享 JS 模拟 pointer ——
 * 共享代码逐字一致只说明"两边同一份实现"，不说明 popover 的标记里真带了这些钩子。 */
{
  const CX = readFileSync(chartPath, 'utf8');
  const unesc = s => String(s == null ? '' : s).replace(/&amp;/g, '&').replace(/&quot;/g, '"')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&#x27;/g, "'");
  const csvg = parseSvg(CX.slice(CX.indexOf('<svg'), CX.indexOf('</svg>') + 6));
  const w0Of = new Map();          // 产物里写死的卡宽，塌缩后拿来对比
  const textsOf = n => n.$$tag('text').map(x => x.text || '');
  const gs = cls => { const need = cls.split(/\s+/);
                      return csvg.$$tag('g').filter(g => need.every(c => g._cls.has(c))); };
  const mkG = gs('mk'), fam = gs('chip fam'), lgc = gs('lgc'), lg = gs('chip lg');
  const boxOf = g => {
    const r = g.$$tag('rect')[0]; if (!r) return null;
    const b = [ +r.attrs.x, +r.attrs.y, +r.attrs.width, +r.attrs.height ];
    if (!b.every(Number.isFinite)) throw new Error(`卡片矩形属性读不出数字：${JSON.stringify(r.attrs)}`);
    return b;
  };
  // 屏上真正占的位 = 作者位 rect 再叠加祖先的 transform（塌缩与退让都是整组 translate）。
  // 判据若直接拿 rect 属性比，卡片一挪位就会既比错重叠、又检不出出界。
  const screenBox = g => {
    let dx = 0, dy = 0, n = g;
    while (n && n !== csvg) {
      const m = String(n.attrs.transform || '').match(/translate\(\s*([-\d.]+)[ ,]+([-\d.]+)\s*\)/);
      if (m) { dx += +m[1]; dy += +m[2]; }
      n = n.parentNode;
    }
    const b = boxOf(g);
    return b && [ b[0] + dx, b[1] + dy, b[2], b[3] ];
  };
  const [g6, med] = [v => String(Number(v.toPrecision(6))),
    a => { const s = [...a].sort((x, y) => x - y), k = s.length >> 1;
           return s.length % 2 ? s[k] : (s[k - 1] + s[k]) / 2; }];
  // 速度读数按**载荷里那张位数表**算（fetch_model_data.DISPLAY_DECIMALS → display_decimals）。
  // 判据若自己抄一份 6 位有效数字，生产一改显示位数就红 —— 那红的是"判据的口径旧了"，
  // 不是渲染错了（2026-11-06 收位到整数时就是这样红的两条 popover）。
  // 表缺失（改动前生成的旧载荷）时退回 g6，与改动前逐字一致。
  const DP = D0.display_decimals || {};
  const spd = v => (DP.tok_s === undefined ? g6(v) : String(Number(Number(v).toFixed(DP.tok_s))));
  [...fam, ...lg].forEach(c => { const r = c.$$tag('rect')[0]; if (r) w0Of.set(c, +r.attrs.width); });

  // ① 一族一张、每档一行
  const keys = [...new Set(mkG.map(g => g.attrs['data-k']))];
  ok('popover：每个族恰好一张合并卡（不再逐点贴卡）',
     fam.length === keys.length && fam.length > 0
       && mkG.every(m => m.attrs['data-c'] !== undefined || m.attrs['data-history-for']),
     `${fam.length} 张 / ${keys.length} 个族；节点卡 ${gs('chip nt').length} 张（应为 0）`);
  ok('popover：族卡一律 anchor=br（卡右下角对中心点，不按左右侧翻转）',
     fam.every(c => c.attrs['data-anchor'] === 'br'), `${fam.length} 张`);
  const rowsBad = [];
  for (const k of keys) {
    const ps = mkG.filter(m => m.attrs['data-k'] === k);
    const card = fam.find(c => c.attrs['data-k'] === k);
    const lines = textsOf(card).map(unesc);
    // 布局：表头 + 两行列名 + 每档一行（智能指数 / Progress 成对），不再有汇总或逐档尾行。
    if (card.attrs['data-history-for']) {
      if (!lines[0]?.startsWith('旧版 ') || !lines[0].includes('沿用 ')
          || !lines[1]?.startsWith('新版 ') || !lines[1].includes('同档变化中位')
          || lines.slice(2).some(l => /^(low|medium|high|xhigh|max) ·/.test(l)))
        rowsBad.push(`${k}：历史卡必须写明沿用日期和新版变化中位，不展开档位`);
      continue;
    }
    if (lines.length !== ps.length + 3)
      rowsBad.push(`${k}：${lines.length} 行 ≠ ${ps.length} 档 + 3 表头/列名`);
    // 列名跟着数据走：整族都没有思考时间/前摇时，生产端故意不写这两列
    //（写"—"会被读成实测 0）。所以期望值按该族 <title> 里到底有没有这两项来定，
    // 而不是钉死一份"永远六列"的串。
    const famTips = ps.map(p => unesc(p.$$tag('title')[0]?.text || '')).join('\n');
    const wantCols = '任务耗时(估)'
      + (/思考时间/.test(famTips) ? ' · 思考时间' : '')
      + (/思考前摇/.test(famTips) ? ' · 思考前摇' : '') + ' · tok/s';
    if (!/^智能指数(?:v[\d.]+(?:\/v[\d.]+)*)? \/ 历史参照(?:v[\d.]+)? · \$每任务$/
        .test(lines[1] || '')
        || lines[2] !== wantCols)
      rowsBad.push(`${k}：列名不是「智能指数… / 历史参照… · $每任务」+「${wantCols}」→「${lines[1]} | ${lines[2]}」`);
    const metricRows = lines.slice(3);
    if (metricRows.some(l => /Progress| · P(?:—|[\d.]+)$/.test(l)))
      rowsBad.push(`${k}：仍带旧的 Progress 汇总/明细段`);
    for (const p of ps) {
      const t = p.$$tag('title')[0]; if (!t) continue;
      const tip = unesc(t.text || '');
      const m = /· [^·]+：([\d.]+) \/ (—|[\d.]+) · \$([\d.]+) ·/.exec(tip);
      if (m && !metricRows.some(l => l.includes(` · ${m[1]} / ${m[2]} · $${m[3]} ·`)
                          || l.startsWith(`${m[1]} / ${m[2]} · $${m[3]} ·`)))
        rowsBad.push(`${k}：某档的成对分数没进族卡（${tip.slice(0, 40)}）`);
    }
  }
  ok('popover：族卡把该族每一档的数都并进来（每档一行、一行不漏）',
     rowsBad.length === 0, rowsBad.slice(0, 2).join(' | '));
  // 用户删掉的是"同族推估 N 档…""同级参照带 N 档…"那两行**汇总解释**，逐档行必须留着，
  // 并且留不住就等于把推估当实测。这条判据读的是 payload，不读文案（改文案不会让它变绿）。
  const markBad = [], need = [];
  for (const m of D0.models) {
    if (m.index === null || !m.apt_usd || m.tok_s) continue;
    if (m.tok_s_est) { need.push([m, `≈${spd(m.tok_s_est)}`]); continue; }
    if (m.tok_s_band) {
      const [lo, hi] = [spd(m.tok_s_band[0]), spd(m.tok_s_band[1])];
      need.push([m, lo === hi ? lo : `${lo}–${hi}`]);
    }
  }
  for (const [m, mark] of need) {
    const card = fam.find(c => c.attrs['data-k'] === (m.series_key || m.name));
    if (!card) { markBad.push(`${m.name}：找不到它那一族的卡`); continue; }
    if (!textsOf(card).map(unesc).join('\n').includes(mark))
      markBad.push(`${m.name}：逐档行里没有「${mark}」`);
  }
  ok('popover：推估/参照带在自己的逐档行里仍带 ≈ / 区间标记（删的只是汇总解释，不是口径）',
     need.length > 0 && markBad.length === 0, `${need.length} 个待标档；${markBad.slice(0, 2).join(' | ')}`);

  // ② 中心点落点：卡右下角 = 该族"离像素质心最近那颗球"的左上外侧
  const placeBad = [];
  for (const c of fam) {
    const ps = mkG.filter(m => m.attrs['data-k'] === c.attrs['data-k']);
    // 中心点 = 按智能指数排序后居中的那一档（与 render_scatter_svg.family_center 同一条规则）。
    // 分数从各组的 <title> 里读（"… · 39 分 · …"），不靠像素距离 —— 像素距离在两档族里会并列。
    const scored = ps.map(m => {
      const cc = m.$$tag('circle').pop();
      return { cc, y: -(+cc.attrs.cy), x: +cc.attrs.cx, name: m.attrs['data-n'] };
    }).sort((a, b0) => b0.y - a.y || a.x - b0.x || a.name.localeCompare(b0.name));
    const mid = scored[(scored.length - 1) >> 1].cc;
    const [x, y, w, h] = boxOf(c);
    const wantR = +mid.attrs.cx - +mid.attrs.r - 4, wantB = +mid.attrs.cy - +mid.attrs.r - 4;
    // 贴不住时允许**夹紧**：球靠上沿、卡片比上面的空隙还高，就只能顶到边界上
    // （此时卡的下边必然低于 wantB —— 那是夹紧的必然后果，不是换侧）。
    // 不能退让的是方向：卡的下边不能越过球往右/往下跑，那才叫换到另一侧。
    const atTop = y <= 27.5, atLeft = x <= 56.5;
    // 只有"没被那条边夹住"时才要求不越过球的左上：球太靠左时卡片放不下，
    // 只能顶在左边界上、右缘越过锚点 —— 那是夹紧的必然后果，方向没变（仍然在球的左上侧起手）
    const past = (!atLeft && x + w > wantR + 0.6) || (!atTop && y + h > wantB + 0.6);
    if (past || (!atTop && !atLeft && (Math.abs(x + w - wantR) > 0.6 || Math.abs(y + h - wantB) > 0.6)))
      placeBad.push(`${unesc(c.attrs['data-k'])}：卡右下 (${(x + w).toFixed(0)},${(y + h).toFixed(0)}) 对不上中心点左上 (${wantR.toFixed(0)},${wantB.toFixed(0)})`);
  }
  ok('popover：族卡右下角对到"该族中心点"那颗球的左上（越界时夹紧）',
     placeBad.length === 0, placeBad.slice(0, 2).join(' | '));

  // ③ 综述卡：每厂一张、按系列一行、数字与最优档都独立复算
  const byV = {};
  const historyKeys = new Set(mkG.filter(m => m.attrs['data-history-for']).map(m => m.attrs['data-k']));
  const popFront = new Set(D0.models.filter(m => m.index != null && m.apt_usd > 0
    && !historyKeys.has(m.series_key) && !m.tok_s_est && !m.tok_s_band)
    .sort((a,b) => a.apt_usd - b.apt_usd || b.index - a.index || a.name.localeCompare(b.name))
    .reduce((points, m) => points.length && points.at(-1).index >= m.index ? points : [...points, m], [])
    .map(m => m.name));
  D0.models.filter(m => m.index !== null && m.apt_usd > 0 && !historyKeys.has(m.series_key)).forEach(m => {
    const c = m.creator || '其他';
    (byV[c] = byV[c] || []).push(m);
  });
  ok('popover：图例格数 = 厂商数 = 综述卡数（pri=0 先占位）',
     lgc.length > 0 && lgc.length === Object.keys(byV).length && lg.length === lgc.length
     && lg.every(c => c.attrs['data-pri'] === '0'), `${lgc.length} 格 / ${lg.length} 卡`);
  // 区间端点先各自按调用点的位数格式化，再判"是不是同一个数"：
  // 71.4 与 70.6 原始值不等、收位后都是 71，按原始值比会期望 "71~71" 那种自问自答。
  const spanS = (v, pre = '', cell = g6) => {
    const lo = cell(Math.min(...v)), hi = cell(Math.max(...v));
    return (v.length > 1 && lo !== hi) ? `${pre}${lo}~${pre}${hi}` : `${pre}${cell(v[0])}`;
  };
  const bad = [];
  for (const card of lg) {
    const name = unesc(card.attrs['data-c']);
    const lines = textsOf(card).map(unesc), txt = lines.join('\n');
    const ms = byV[name] || [];
    if (!ms.length) { bad.push(`${name}：图上没点却有卡`); continue; }
    const idx = ms.map(m => m.index), apt = ms.map(m => m.apt_usd);
    const tok = ms.filter(m => m.tok_s).map(m => m.tok_s);
    const ser = {};
    ms.forEach(m => { (ser[m.series || m.name] = ser[m.series || m.name] || []).push(m); });
    const best = Object.values(ser).map(ps => ps.reduce((a, b) =>
      (b.index > a.index || (b.index === a.index && b.apt_usd < a.apt_usd)) ? b : a));
    const bestIdx = best.map(m => m.index);
    const bestProg = Object.values(ser).map(ps => {
      const v = ps.filter(m => typeof m.progress_index === 'number'
        && Number.isFinite(m.progress_index)).map(m => Math.round(m.progress_index));
      return v.length ? Math.max(...v) : null;
    }).filter(v => v !== null);
    const flat = lines.join('').replace(/\s+/g, ' ');
    const escRe = s => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const progSpan = ps => {
      const v = ps.filter(m => typeof m.progress_index === 'number'
        && Number.isFinite(m.progress_index)).map(m => Math.round(m.progress_index));
      return v.length ? spanS(v) : '—';
    };
    const bestPair = `${spanS(bestIdx)} / ${bestProg.length ? spanS(bestProg) : '—'}`;
    const bestMed = `${g6(med(bestIdx))} / ${bestProg.length ? '[\\d.]+' : '—'}`;
    if (!new RegExp(`${escRe(bestPair)}\\s*（中位 ${bestMed}`).test(flat)
        || !flat.includes('（各取族最高）'))
      bad.push(`${name}：各取族最高后指数/Progress 成对区间或中位对不上 →「${lines.slice(1, 3).join('')}」`);
    // 折行会把这些长句断成几段，逐字比对必须用拼回一行的 flat；
    // 否则"卡片多了一列 → 变宽 → 刚好折行"会红成数据错。
    if (!flat.includes(`每任务成本 ${spanS(apt, '$')}（中位 $${g6(med(apt))}）`))
      bad.push(`${name}：成本区间/中位对不上 →「${lines[2]}」`);
    if (tok.length) {
      // 这一句是**逐字**比区间端点的，端点只可能来自 tok —— 推估顶进来就会红，
      // 不需要再单独查"实测区间含不含推估"（老写法用 txt.includes(~推估值) 反而出假阳性：
      // 指数区间里可能恰好有同一个数）。
      if (!flat.includes(`实测 ${tok.length} 档 ${spanS(tok, '', spd)} tok/s（中位 ${spd(med(tok))}）`))
        bad.push(`${name}：实测速度对不上 →「${(flat.match(/输出速度[^\n]*/) || [''])[0]}」`);
    } else if (!flat.includes('输出速度 无实测档')) bad.push(`${name}：无实测却没说`);
    // 用户口径（10-13）：推估/参照带**不再各占一行解释**，只在自己的逐档行里带 ≈ / 区间标记。
    // 这条断言反向钉住它：汇总行一旦回来，"实测/推估混算"那个口径问题就跟着回来。
    if (/同族推估|同级参照带/.test(txt)) bad.push(`${name}：综述卡里又出现推估/参照带汇总行`);
    if (!flat.includes(`${ms.length} 个档位`)) bad.push(`${name}：档位数 ${ms.length} 没写对`);
    // 按系列：行数 = 系列数；每行的双评分区间与最优档独立复算
    if (!flat.includes('按系列：档位 · 双评分 · 最优档')) bad.push(`${name}：缺按系列的小标题`);
    // 一行可能被折成几段：谁以某个系列名开头，谁就是一行的头，其余拼回去。
    // 这样判据只依赖"行头是系列名"，不依赖折行算法本身。
    const names = Object.keys(ser).sort((a, b) => b.length - a.length);
    const rows = [];
    for (const l of lines) {
      if (names.some(n => l.startsWith(n + ' · '))) rows.push(l);
      else if (rows.length) rows[rows.length - 1] += l;
    }
    if (rows.length !== Object.keys(ser).length)
      bad.push(`${name}：系列行 ${rows.length} ≠ 系列数 ${Object.keys(ser).length}`);
    for (const [s, ps] of Object.entries(ser)) {
      // 必须带「 · 」边界：'DeepSeek V4.1 Flash' 是 'DeepSeek V4.1 Flash 1017' 的前缀，
      // 裸 startsWith 会把后者的行当成前者的行（MiniMax-M3 / M3.1-Flash 同形）。
      const row = rows.find(l => l.startsWith(s + ' · '));
      if (!row) { bad.push(`${name}/${s}：没有这一行`); continue; }
      const ii = ps.map(m => m.index);
      if (!row.includes(`${spanS(ii)} / ${progSpan(ps)}`))
        bad.push(`${name}/${s}：指数/Progress 成对范围对不上 →「${row}」`);
      if (ps.length === 1) {
        // 只有一个点就没有可比的兄弟档：最优档那一栏**留空**（用户口径），
        // 既不写"最优 X"，也不写"（不在前沿）"或"单点不成族"那种解释。
        if (/最优|前沿|单点不成族/.test(row))
          bad.push(`${name}/${s}：单点系列该把最优档留空 →「${row}」`);
        const t = ps[0].tier || ps[0].name;
        if (!row.includes(`${ps[0].index} / ${progSpan(ps)}`) || !row.includes(t))
          bad.push(`${name}/${s}：单点系列仍要报出档位与双评分 →「${row}」`);
        continue;
      }
      const on = ps.filter(m => popFront.has(m.name));
      const pick = (on.length ? on : ps).reduce((a, b0) => (b0.index > a.index ||
        (b0.index === a.index && b0.apt_usd < a.apt_usd)) ? b0 : a, on[0] || ps[0]);
      const tier = (pick.tier || pick.name).replace(/ with fallback$/, '');
      // 只有真在前沿上才标注；被支配的那一档不给任何后缀（"（不在前沿）"已按用户口径去掉）
      if (!row.includes(`最优 ${tier}${on.length ? '（前沿）' : ''}`)
          || (!on.length && /前沿/.test(row)))
        bad.push(`${name}/${s}：最优档与悬浮窗默认前沿不一致 →「${row}」`);
    }
    // 双评分厂商行紧随表头；不再单列 Progress 汇总/明细段。
    if (!lines[1] || !lines[1].includes('（各取族最高）'))
      bad.push(`${name}：厂商双评分行应紧随表头 →「${lines[1]}」`);
    if (lines.slice(1).some(l => /^Progress /.test(l)))
      bad.push(`${name}：仍有旧的 Progress 汇总行`);
  }
  ok('popover：综述卡数字全部按生产口径独立复算（区间/中位/实测与推估分行/最优档与 on_frontier 一致）',
     bad.length === 0, bad.slice(0, 3).join(' | '));

  // ④ 共有尾巴：抽成一次，但一个字都不能丢
  const tailBad = [];
  for (const [v, ms] of Object.entries(byV)) {
    const ts = ms.map(m => m.tier || '').filter(Boolean);
    if (ts.length < 2 || ts.some(t => !t.endsWith('with fallback'))) continue;
    const card = lg.find(c => unesc(c.attrs['data-c']) === v);
    if (!textsOf(card).map(unesc).join('\n').includes('（均 with fallback）'))
      tailBad.push(`${v}：整组都带 with fallback，卡里却没标`);
  }
  ok('popover：档位共有的 "with fallback" 只标一次、且没有丢', tailBad.length === 0, tailBad.join(' | '));

  // ⑤ 静态几何与样式
  const outBox = [...fam, ...lg].map(boxOf).filter(Boolean)
    .filter(([x, y, w, h]) => x < 53.5 || y < 23 || x + w > 660.5 || y + h > 406.5);
  ok('popover：所有卡（族卡与综述卡）都在绘图区内，不被切半张',
     outBox.length === 0 && fam.length + lg.length > 0,
     outBox.length ? `越界 ${outBox.length} 张：${outBox[0].map(v => v.toFixed(0))}` : `${fam.length + lg.length} 张都在界内`);
  // 综述卡固定左下角或右下角（用户口径），族卡仍贴各自的小球
  const cornerBad = [];
  for (const c of lg) {
    const [x, y, w, h] = boxOf(c);
    const bottom = Math.abs((y + h) - 404) < 0.6;
    const left = Math.abs(x - 56) < 0.6, right = Math.abs((x + w) - 658) < 0.6;
    if (!bottom || !(left || right))
      cornerBad.push(`${unesc(c.attrs['data-c'])}：(${x.toFixed(0)},${y.toFixed(0)},${w.toFixed(0)},${h.toFixed(0)}) 不在左下/右下角`);
  }
  ok('popover：综述卡贴在表格的左下角或右下角（不压帕累托右上角）',
     cornerBad.length === 0, cornerBad.slice(0, 2).join(' | '));
  const shapeBad = [];
  for (const c of lg) {
    const [x, y, w] = boxOf(c);
    const colsW = String(c.attrs['data-cols'] || '').split(',').map(Number);
    if (!colsW.length || !colsW.every(Number.isFinite)) { shapeBad.push('data-cols 缺失'); continue; }
    if (Math.abs(colsW.reduce((a, b0) => a + b0, 0) - w) > 0.6)
      shapeBad.push(`${unesc(c.attrs['data-c'])}：栏宽合计 ≠ 卡宽`);
    for (const tx of c.$$tag('text')) {
      const sx = +tx.attrs.x;
      if (sx < x - 0.5 || sx > x + w - 4) shapeBad.push(`${unesc(c.attrs['data-c'])}：某栏起点不在卡内`);
    }
  }
  ok('popover：综述卡栏宽之和 = 卡宽、每栏文字起点都在卡内（宽度不许被二次截断）',
     shapeBad.length === 0, shapeBad.slice(0, 2).join(' | '));
  ok('popover：卡片默认不显、不吃指针（配色复用脚注浮层变量）',
     /\.chip\{display:none;pointer-events:none\}/.test(CX) && /\.chip\.hi\{display:block\}/.test(CX)
     && /\.lgc\{cursor:pointer\}/.test(CX) && /--mr-bub\b/.test(CX) && !/\.hi \.chip\{/.test(CX), '');

  /* ---- ⑥ 行为：产物这张 SVG 喂给共享 JS，模拟 pointer 事件 ---- */
  const cdoc = { querySelector: sel => (sel === '#scatterwrap svg' || sel === 'svg' ? csvg : null),
                 querySelectorAll: () => [],
                 createElementNS: (ns, tag) => new El(tag) };
  new Function('document', 'window', 'CSS', hoverBody)(cdoc, { CSS: cssStub }, cssStub);
  const ctop = csvg.children.find(x => (x.attrs.class || '').includes('toplayer'));
  const vBig = unesc(Object.entries(byV).sort((a, b) => b[1].length - a[1].length)[0][0]);
  const vMk = mkG.filter(m => unesc(m.attrs['data-c']) === vBig);
  const vFam = fam.filter(c => unesc(c.attrs['data-c']) === vBig);
  const vPanel = lg.find(c => unesc(c.attrs['data-c']) === vBig);
  const cell0 = lgc.find(x => unesc(x.attrs['data-c']) === vBig);
  const home0 = [...vMk, ...vFam, vPanel].map(e => e.parentNode);
  csvg.listeners.pointerover[0]({ target: cell0 });
  ok(`行为：悬停「${vBig}」图例 → 整厂 ${vMk.length} 档全亮并进顶层`,
     vMk.length > 1 && ctop.children.filter(e => e._cls.has('mk')).length === vMk.length
     && vMk.every(e => e._cls.has('hi')), `${ctop.children.filter(e => e._cls.has('mk')).length}/${vMk.length}`);
  // 层序：卡片必须在点与线的**下面**（SVG 按文档序绘制）。用户口径"原点和连线悬浮到卡片上层"，
  // 否则一张贴得好的卡正好把它说的那颗球盖住，等于自己挡自己。
  // 卡片不直属顶层：全在顶层第一个孩子 <g class="cards"> 里 —— 判据若还按 flat children 找
  // chip，就会读出"卡 0 张"（那是判据旧了，不是代码坏了）。
  const clash = (a, b) => !(a[0] + a[2] <= b[0] || b[0] + b[2] <= a[0]
                            || a[1] + a[3] <= b[1] || b[1] + b[3] <= a[1]);
  const cardsG = ctop.children.find(e => e._cls.has('cards'));
  {
    const rank = e => e._cls.has('cards') ? 0 : e._cls.has('ln') ? 1 : e._cls.has('mk') ? 2 : 3;
    const seq = ctop.children.map(rank);
    const ordered = seq.every((x, i) => i === 0 || seq[i - 1] <= x);
    const lines = seq.filter(x => x === 1).length, dots = seq.filter(x => x === 2).length;
    ok('行为：卡片自成一组、排在顶层最前（点与线天然浮在它上面）',
       !!cardsG && ctop.children[0] === cardsG && cardsG.children.length > 0
       && lines > 0 && dots > 0 && ordered,
       cardsG ? `卡 ${cardsG.children.length} / 线 ${lines} / 点 ${dots}，顺序${ordered ? '单调' : '乱'}`
              : '顶层里没有 .cards 组');
    // 卡片之间：pri 小的（分数高）后画 → 压在上面，后者避让前者。沉底那批也必须一起排，
    // 否则就成了"赢家垫底、输家盖着它"—— 与"衬到底下"正好相反（10-13 实测到的错法）。
    const chipPri = cardsG.children.map(e => +e.attrs['data-pri']);
    ok('行为：卡片按分数高→低叠放（pri 小的最后画，压在别的卡上面）',
       chipPri.every((x, i) => i === 0 || chipPri[i - 1] > x), chipPri.join(','));
  }
  ok('行为：不牵连别家的点',
     mkG.filter(m => unesc(m.attrs['data-c']) !== vBig).every(m => !m._cls.has('hi') && m.parentNode !== ctop), '');
  const shown = cardsG ? cardsG.children.filter(e => e._cls.has('hi')) : [];
  const shownFam = shown.filter(e => e._cls.has('fam'));
  // 作者位（data-r0，塌缩与退让都不改它）与屏上位一比就知道这张卡被挪过没有
  const moved = shown.filter(c => { const r0 = String(c.attrs['data-r0'] || '').split(' ').map(Number);
    const s = screenBox(c); return Math.abs(s[0] - r0[0]) > 0.6 || Math.abs(s[1] - r0[1]) > 0.6; });
  if (process.env.DBG) {
    console.log('DBG', JSON.stringify({ fam: vFam.length, shownFam: shownFam.length,
      moved: moved.map(c => [unesc(c.attrs['data-k'] || c.attrs['data-c']),
                              screenBox(c).map(n => Math.round(n))]),
      pri: shown.map(c => c.attrs['data-pri']),
      movedN: moved.length, r0: String(vPanel.attrs['data-r0']) }));
  }
  ok('行为：综述卡一定显、该厂每一张族卡也都显（避让=衬到底下，不是"就不显示"）',
     vPanel._cls.has('hi') && vFam.length > 0 && shownFam.length === vFam.length,
     `综述 + ${shownFam.length}/${vFam.length} 张族卡，其中 ${moved.length} 张被挪位让路`);
  {
    const pairBad = [], inBad = [];
    for (let i = 0; i < shown.length; i++) {
      const s = screenBox(shown[i]);
      if (s[0] < 53.5 || s[1] < 23 || s[0] + s[2] > 660.5 || s[1] + s[3] > 406.5)
        inBad.push(unesc(shown[i].attrs['data-k'] || shown[i].attrs['data-c']));
      for (let j = 0; j < i; j++) {                  // j 画在先 = 在下
        if (!clash(s, screenBox(shown[j]))) continue;
        if (+shown[i].attrs['data-pri'] > +shown[j].attrs['data-pri'])
          pairBad.push(`${unesc(shown[j].attrs['data-k'] || shown[j].attrs['data-c'])} 盖住了分数更高的`
                       + ` ${unesc(shown[i].attrs['data-k'] || shown[i].attrs['data-c'])}`);
      }
    }
    ok('行为：重叠只许"分数低的衬在分数高的底下"，且每张卡的屏上位置仍在绘图区内',
       pairBad.length === 0 && inBad.length === 0,
       `${pairBad.slice(0, 2).join(' | ')}${inBad.length ? ' | 出界 ' + inBad.join(',') : ''}`);
  }
  // 作者位两两相压时，避让必须真的**挪位**（偏移/翻转），而不是原地重叠着算了。
  // 只在"确实有得让"的前提下断言，否则数据不拥挤时这条会空红。
  const authored = shown.map(c => { const r0 = String(c.attrs['data-r0'] || '').split(' ').map(Number);
    const s = screenBox(c), br = (c.attrs['data-anchor'] || 'tl') === 'br';
    return [br ? r0[0] + r0[2] - s[2] : r0[0], br ? r0[1] + r0[3] - s[3] : r0[1], s[2], s[3]]; });
  const crowd = shown.some((c, i) => shown.some((d, j) => j < i && clash(authored[i], authored[j])));
  ok('行为：作者位互相压到时真的有卡片被挪位让路（偏移/翻转这条支路不是空转）',
     !crowd || moved.length > 0,
     crowd ? `作者位相压，${moved.length} 张被挪开` : '作者位本就不相压（本批数据无需避让）');
  /* ---- 避让的两条支路：真数据里它们**互为备胎**（删掉一条另一条也能摆开），
     所以拿当天数据测不出"支路被废"。这里按 bandplant 那套做法自己摆一张必命中的假 SVG：
     三张卡作者位完全相同 → 第一张占住作者位；第二张带 data-ball（只有绕球换角能救）；
     第三张不带球（只有上下左右错开能救）。两条支路各自"非它不可"的落点逐张钉死。 */
  {
    const SYN = `<svg>`
      + `<g class="mk" data-k="zz" data-c="ZZ"><circle cx="100" cy="100" r="5"><title>zz</title></circle></g>`
      + [1, 2, 3].map(p => `<g class="chip fam" data-k="zz" data-c="ZZ" data-pri="${p}" data-anchor="br"`
        + ` data-r0="300 100 120 40"${p === 2 ? ' data-ball="120 60 5"' : ''}>`
        + `<rect x="300" y="100" width="120" height="40" rx="6"/>`
        + `<text x="306" y="110" font-size="10">abc def</text></g>`).join("")
      + `</svg>`;
    const ssvg = parseSvg(SYN);
    // parseSvg 交回的是包着 <svg> 的根节点，而共享 JS 拿到的"svg"就是它：
    // data-plot 必须挂在它身上，否则 clamp 走的是 JS 里那份兜底常数，这条属性就没被测到。
    ssvg.attrs['data-plot'] = '54 24 660 406';
    new Function('document', 'window', 'CSS', hoverBody)(
      { querySelector: () => ssvg, querySelectorAll: () => [],
        createElementNS: (ns, tag) => new El(tag) }, { CSS: cssStub }, cssStub);
    ssvg.listeners.pointerover[0]({ target: ssvg.$$tag('g').find(e => e._cls.has('mk')) });
    const sc = ssvg.children.find(x => x._cls.has('toplayer'));
    const sg = sc && sc.children.find(e => e._cls.has('cards'));
    const got = sg ? sg.children.map(e => ({ pri: +e.attrs['data-pri'], hi: e._cls.has('hi'),
                                             box: screenBox(e) })) : [];
    const at = p => (got.find(x => x.pri === p) || {}).box;
    // 塌缩后的作者位：br 钉右下角 → 左上角随塌缩后的宽高回推（与 cardsOf 用的是同一把尺）
    const qOf = p => { const b = at(p); if (!b) return null;
                       return [300 + 120 - b[2], 100 + 40 - b[3], b[2], b[3]]; };
    const cX = (v, w) => Math.min(Math.max(v, 56), Math.max(56, 658 - w));
    const cY = (v, h) => Math.min(Math.max(v, 26), Math.max(26, 404 - h));
    const badS = [];
    const q1 = qOf(1), q2 = qOf(2), q3 = qOf(3);
    if (!q1 || !q2 || !q3) badS.push(`三张卡没都显出来：${got.filter(x => x.hi).length}/3`);
    if (q1 && at(1).slice(0, 2).join() !== [q1[0], q1[1]].join())
      badS.push('第一张不该让位（它先贴，作者位本来就是空的）');
    if (q2) {   // 只有"绕球换角"这一条支路能救它：落点必须是球右下那一格
      const want = [cX(120 + 5 + 4, q2[2]), cY(60 + 5 + 4, q2[3])];
      if (Math.abs(at(2)[0] - want[0]) > 0.9 || Math.abs(at(2)[1] - want[1]) > 0.9)
        badS.push(`第二张没翻到球的右下（在 ${at(2)[0].toFixed(0)},${at(2)[1].toFixed(0)}，`
                  + `该在 ${want[0].toFixed(0)},${want[1].toFixed(0)}）`);
    }
    if (q3) {   // 没有球可翻，只能上下左右错开一格
      const [x, y, w, h] = q3;
      const nudges = [[x, cY(y - h - 6, h)], [x, cY(y + h + 6, h)], [cX(x - w - 6, w), y], [cX(x + w + 6, w), y]];
      if (!nudges.some(([nx, ny]) => Math.abs(at(3)[0] - nx) <= 0.9 && Math.abs(at(3)[1] - ny) <= 0.9))
        badS.push(`第三张没被平移错开（在 ${at(3)[0].toFixed(0)},${at(3)[1].toFixed(0)}）`);
    }
    ok('行为（摆一个必命中的场景）：翻转与平移两条避让支路各自都真的在跑、且一张都不藏',
       badS.length === 0 && got.length === 3 && got.every(x => x.hi), badS.slice(0, 2).join(' | '));
  }
  const vSecond = unesc(Object.entries(byV).sort((a, b) => b[1].length - a[1].length)[1][0]);
  // 塌缩后的两条硬要求：文字必须还在框里（只挪框不挪字就会出框），以及重复悬停不再继续缩/漂
  const outText = [...shownFam, vPanel].filter(c => {
    const r = c.$$tag('rect')[0]; if (!r) return true;
    const [rx, ry, rw, rh] = [+r.attrs.x, +r.attrs.y, +r.attrs.width, +r.attrs.height];
    // 量文字一律走桩的 getBBox（与塌缩用的是同一把尺）；判据自己再估一遍字宽，
    // 就是拿另一套度量去验这套度量的结果 —— 桩一改系数，判据就自己红
    return c.$$tag('text').some(tx => {
      const b = tx.getBBox();
      return b.x < rx - 0.5 || b.x + b.width > rx + rw - 2
        || b.y < ry - 0.5 || b.y + b.height > ry + rh + 0.5;
    });
  });
  // 塌缩 + 避让之后，卡片的合法落点只有两类：作者位（钉角仍压在 data-r0 那一角上）或
  // 候选位（绕那颗球换四角 / 从作者位上下左右错开一格）。屏上位置落在这两类之外就是飘了 ——
  // 老 bug 是 cardsOf 把 fitCard 的钉角偏移覆盖掉，卡片整张滑离自己的小球（实测偏 162px），
  // 那种位置既不是作者位也不是任何候选位，这条当场红。
  const PLOT = String(csvg.attrs['data-plot'] || '54 24 660 406').split(' ').map(Number);
  const clX = (v, w) => Math.min(Math.max(v, PLOT[0] + 2), Math.max(PLOT[0] + 2, PLOT[2] - 2 - w));
  const clY = (v, h) => Math.min(Math.max(v, PLOT[1] + 2), Math.max(PLOT[1] + 2, PLOT[3] - 2 - h));
  const drift = [];
  for (const c of shown) {
    const r0 = String(c.attrs['data-r0'] || '').split(' ').map(Number);
    const [sx, sy, w, h] = screenBox(c);
    const br = (c.attrs['data-anchor'] || 'tl') === 'br';
    const ax = br ? r0[0] + r0[2] - w : r0[0], ay = br ? r0[1] + r0[3] - h : r0[1];
    const cand = [[ax, ay], [ax - w - 6, ay], [ax + w + 6, ay],
                  [ax, ay - h - 6], [ax, ay + h + 6]];
    const b = String(c.attrs['data-ball'] || '').split(' ').map(Number);
    if (b.length === 3 && isFinite(b[0]) && b[0] > 0) {
      const [bx, by, rr] = b, g = 4;
      cand.push([clX(bx + rr + g, w), clY(by + rr + g, h)], [clX(bx - rr - g - w, w), clY(by + rr + g, h)],
                [clX(bx + rr + g, w), clY(by - rr - g - h, h)], [clX(bx - rr - g - w, w), clY(by - rr - g - h, h)]);
    }
    const near = (a, x, y) => Math.abs(a[0] - x) <= 0.9 && Math.abs(a[1] - y) <= 0.9;
    if (!cand.some(p => near(p, sx, sy) || near([clX(p[0], w), clY(p[1], h)], sx, sy)))
      drift.push(`${unesc(c.attrs['data-k'] || c.attrs['data-c'])}: 落在 (${sx.toFixed(0)},${sy.toFixed(0)})，`
                 + `既不是作者位 (${ax.toFixed(0)},${ay.toFixed(0)}) 也不是任何候选位`);
  }
  ok('行为：塌缩+避让后每张卡仍只待在合法落点里（作者位或候选位，不许滑离自己的小球）',
     drift.length === 0, drift.slice(0, 2).join(' | '));
  ok('行为：塌缩后每一行文字仍在框内（只挪矩形不挪字就会出框）',
     outText.length === 0, outText.length ? `${outText.length} 张卡的字出框` : `${[...shownFam, vPanel].length} 张都包得住`);
  const before = [...shownFam, vPanel].map(c => { const r = c.$$tag('rect')[0];
    return `${r.attrs.x},${r.attrs.y},${r.attrs.width},${r.attrs.height}|${c.attrs.transform || ''}`; });
  csvg.listeners.pointerover[0]({ target: lgc.find(x => unesc(x.attrs['data-c']) === vSecond) });
  csvg.listeners.pointerover[0]({ target: cell0 });
  const after = [...shownFam, vPanel].map(c => { const r = c.$$tag('rect')[0];
    return `${r.attrs.x},${r.attrs.y},${r.attrs.width},${r.attrs.height}|${c.attrs.transform || ''}`; });
  ok('行为：重复悬停塌缩不累积（第二次与第一次逐字相同，基准取 data-r0 而不是当前 rect）',
     before.join('#') === after.join('#'),
     before.filter((b, i) => b !== after[i]).length ? `变了 ${before.filter((b, i) => b !== after[i]).length} 张` : '一致');
  csvg.listeners.pointerover[0]({ target: cell0 });
  const shownFam2 = ctop.children.filter(e => e._cls.has('chip') && e._cls.has('fam') && e._cls.has('hi'));
  ok('行为：塌缩的代码路径跑到了（悬停后 rect 被按实际字宽重设）',
     [...shownFam2, lg.find(x => unesc(x.attrs['data-c']) === vBig)].every(c => { const r = c.$$tag('rect')[0];
       return r && Number.isFinite(+r.attrs.width) && +r.attrs.width <= (w0Of.get(c) || Infinity) + 0.5; }),
     `${shownFam2.length + 1} 张量过`);

  // 先验"换一家"的复位：上一家（含族卡与综述卡）必须原样搬回去、不留高亮
  csvg.listeners.pointerover[0]({ target: lgc.find(x => unesc(x.attrs['data-c']) === vSecond) });
  ok('行为：换一家图例 → 上一家的点与族卡、综述卡全部搬回原位且不留高亮',
     [...vMk, ...vFam, vPanel].every((e, i) => e.parentNode === home0[i] && !e._cls.has('hi'))
     && ctop.children.filter(e => e._cls.has('mk')).length
        === mkG.filter(m => unesc(m.attrs['data-c']) === vSecond).length,
     `残留 ${[...vMk, ...vFam, vPanel].filter(e => e._cls.has('hi')).length} 个`);
  csvg.listeners.pointerover[0]({ target: cell0 });   // 回到第一家，后面的族悬停才有比较基准
  const one = vMk[0], famKey = one.attrs['data-k'];
  const linked = mkG.filter(m => m.attrs['data-history-for'] === famKey);
  const linkedLines = csvg.$$attr('data-history-for', famKey).filter(e => e._cls.has('ln'));
  const linkedCards = fam.filter(c => c.attrs['data-history-for'] === famKey);
  ok('悬停厂商图例不展开历史气泡、连线或卡片',
     [...linked, ...linkedLines, ...linkedCards].every(e => !e._cls.has('hi')));
  const famSize = mkG.filter(m => m.attrs['data-k'] === famKey).length;
  csvg.listeners.pointerover[0]({ target: one });
  ok(`行为：悬停新版 → 亮现役 ${famSize} 档和对应历史 ${linked.length} 档`,
     ctop.children.filter(e => e._cls.has('mk')).length === famSize + linked.length && famSize < vMk.length
     && linked.every(e => e._cls.has('hi'))
     && linkedLines.every(e => e._cls.has('hi')) && linkedCards.every(c => c._cls.has('hi'))
     && csvg.$$attr('data-k', famKey).filter(e => e._cls.has('chip fam')).every(e => e._cls.has('hi'))
     && !vPanel._cls.has('hi'),
     `亮 ${ctop.children.filter(e => e._cls.has('mk')).length} 个，族 ${unesc(famKey)}`);
  csvg.listeners.pointerleave[0]({});
  ok('离开新版收起历史气泡、连线和卡片',
     [...linked, ...linkedLines, ...linkedCards].every(e => !e._cls.has('hi')));
  // 顶栏开关：MR_CARDS=0 时高亮照旧、一张卡都不显。
  // 用**另一棵重新解析的树**跑：上面那棵已经绑了一份读默认值的闭包，
  // 在它身上再绑一份会让两次 pointerover 都触发（第一份照样把卡贴出来），测不到开关。
  const svg2 = parseSvg(CX.slice(CX.indexOf('<svg'), CX.indexOf('</svg>') + 6));
  new Function('document', 'window', 'CSS', hoverBody)(
    { querySelector: sel => (sel === '#scatterwrap svg' || sel === 'svg' ? svg2 : null),
      querySelectorAll: () => [],
      createElementNS: (ns, tag) => new El(tag) },
    { MR_CARDS: 0, CSS: cssStub }, cssStub);
  const mk2 = svg2.$$tag('g').filter(g => g._cls.has('mk'));
  const cell2 = svg2.$$tag('g').find(x => x._cls.has('lgc') && unesc(x.attrs['data-c']) === vBig);
  svg2.listeners.pointerover[0]({ target: cell2 });
  const top2 = svg2.children.find(x => (x.attrs.class || '').includes('toplayer'));
  const litMk2 = mk2.filter(m => m._cls.has('hi')).length;
  const litCard2 = top2.children.filter(e => e._cls.has('chip') && e._cls.has('hi')).length;
  ok('行为：顶栏「▤ 卡片」关掉后高亮照旧、一张卡都不显',
     litMk2 === mk2.filter(m => unesc(m.attrs['data-c']) === vBig).length && litCard2 === 0,
     `高亮 ${litMk2} 个、卡 ${litCard2} 张`);
  csvg.listeners.pointerleave[0]({});
  ok('行为：pointerleave 后全部收回（下次悬停从零开始试位）',
     [...fam, ...lg].every(c => !c._cls.has('hi')) && mkG.every(m => !m._cls.has('hi')), '');
}

// 两面的 CSS 都不能给 .toplayer 设 pointer-events:none —— 那会让置顶后不再命中
ok('两面都没把 .toplayer 设成不可命中',
   !/\.toplayer\s*\{[^}]*pointer-events\s*:\s*none/.test(html) && !/\.toplayer\s*\{[^}]*pointer-events\s*:\s*none/.test(PY), '');

/* ---------- 场景 5/6：本机有没有装总结 CLI ----------
 * 两条分支都得跑到：「没有 CLI」才是绝大多数机器的常态，如果只测有总结的那条，
 * 退化成表格的路径就是从没验证过的代码。 */
{
  const D = structuredClone(D0);
  D.content_hash = 'deadbeef00000000';
  D.changes = [
    { name: 'Alpha 3', type: 'changed', fields: [
      { field: 'index', label: '智能指数', kind: 'changed', from: 43, to: 47, delta: 4, pct: 9.3 }] },
    { name: 'Beta', type: 'new', fields: [] },
    { name: 'Gamma', type: 'changed', fields: [
      { field: 'tok_s', label: '输出速度', kind: 'appeared', from: null, to: 88, delta: null, pct: null },
      // 归因标签（Python 侧算好）：单价暴涨到底是上游变价还是换到了另一个目录条目。
      { field: 'in_per_1m_usd', label: '输入单价', kind: 'changed', from: 0.2219, to: 1.4,
        delta: 1.1781, pct: 531.0,
        tags: ['AA 每任务成本未变', '匹配对象换了：z-ai/glm-5.3 → z-ai/glm-5.3-flash'] }] },
    // P-22：平铺那条分支（没有总结时才是常态）此前是裸插。名字与字段标签都来自远端页面，
    // 三种 kind 各给一条，免得只覆盖到一个分支就以为全覆盖了。
    { name: '<img src=x onerror=alert(1)>XssNew', type: 'gone', fields: [] },
    { name: '<script>alert(2)</script>XssChg', type: 'changed', fields: [
      { field: 'price', label: '价格 <b>粗</b>', kind: 'changed', from: 1, to: 2, delta: 1, pct: 100,
        tags: ['<img src=x onerror=alert(9)>'] },   // 标签里带 or_id 之类的远端串，同样要转义
      { field: 'tok_s', label: '速度 <i>斜</i>', kind: 'disappeared', from: 90, to: null, delta: null, pct: null }] },
  ];
  const noSum = render(D).changes?.innerHTML ?? '';
  const renameText = 'Claude Opus 5.5：5 档改名（去掉 with fallback；提醒指标未达变化门槛）';
  const renameData = {...D, rename_lines: [renameText], changes: [
    {name: 'Claude Opus 5.5 (max)', old_name: 'Claude Opus 5.5 (max with fallback)',
     type: 'renamed', quiet: true, fields: []}]};
  const renamed = render(renameData).changes?.innerHTML ?? '';
  ok('整族改名只显示一句，不显示新进榜、掉榜或完全一致',
     renamed.includes(renameText) && !/新进榜|掉榜|完全一致/.test(renamed)
     && (renamed.match(/chgbadge/g) || []).length === 1);
  const renameXss = render({...renameData, rename_lines: ['<img src=x onerror=alert(1)> 改名']}).changes?.innerHTML ?? '';
  ok('改名说明中的远端文本经过转义', !/<img src=x/.test(renameXss) && /&lt;img/.test(renameXss));
  const withSum = render(D, false, { content_hash: D.content_hash,
      text: '本周 Alpha 3 智能指数从 43 升到 47，涨幅 9.3%；Beta 首次进榜。' }).changes?.innerHTML ?? '';
  const stale = render(D, false, { content_hash: '另一次抓取', text: '昨天那段话' }).changes?.innerHTML ?? '';

  ok('没有总结时：变动卡照常平铺逐条表格（不是空白、不是报错）',
     /chgbadge/.test(noSum) && !/sumline/.test(noSum));
  ok('有总结时：显示一段话，明细收进悬停、不再平铺表格',
     /class="sumline"/.test(withSum) && !/chgbadge/.test(withSum));
  ok('有总结时：悬停 title 里带着逐条明细', /title="[^"]*Alpha 3 智能指数/.test(withSum));
  ok('总结标成机器生成，不当成实测结论', /机器生成/.test(withSum));
  ok('内容指纹不匹配的旧总结一律不显示（退回表格）',
     !/昨天那段话/.test(stale) && /chgbadge/.test(stale));
  // 总结文本源自远端页面喂进去的模型名，不转义就等于把 innerHTML 开放给远端内容
  const xss = render(D, false, { content_hash: D.content_hash,
      text: '名字里带 <img src=x onerror=alert(1)> 的模型' }).changes?.innerHTML ?? '';
  ok('模型文本里的 HTML 被转义（不能注入标签）',
     !/<img src=x onerror/.test(xss) && /&lt;img/.test(xss));
  ok('「新增/缺失」这类无基数变动不会渲染成 NaN%',
     /新增 88/.test(noSum) && !/NaN/.test(noSum));

  /* P-22 的正面判据：平铺分支（没有 CLI 时的常态路径）此前一直是裸插。
   * 判据必须按**块**取源码：整文件 grep escT 会被同区块 SUM 路径的 escT(plain) 蒙过去，
   * 那正好是"看着有保护其实没覆盖到这条路"的形状。 */
  const blk = (html.match(/const rows = renameRows[\s\S]*?\.join\(''\) : ''\);/) || [''])[0];
  ok('平铺分支取到了源码块（判据本身别是空的）', blk.length > 200, `块长 ${blk.length}`);
  ok('平铺分支：模型名与字段标签都过 escT',
     /escT\(c\.name/.test(blk) && /escT\(f\.label/.test(blk));
  ok('平铺分支：没有剩下的裸插形态（${c.name} / ${f.label}）',
     !/\$\{c\.name/.test(blk) && !/\$\{f\.label/.test(blk));
  ok('平铺分支：带标签的模型名不会变成活标签',
     !/<img src=x/.test(noSum) && !/<script>alert\(2\)/.test(noSum)
     && /&lt;img src=x onerror=/.test(noSum) && /&lt;script&gt;/.test(noSum));
  ok('平铺分支：字段标签里的标签同样被转义',
     /价格 &lt;b&gt;粗/.test(noSum) && /速度 &lt;i&gt;斜/.test(noSum));
  ok('单价归因标签渲染出来（换了条目 / AA 成本未变 看得见，不再逼读者自己判断）',
     /class="tg"/.test(noSum) && /AA 每任务成本未变/.test(noSum)
     && /匹配对象换了：z-ai\/glm-5\.3 → z-ai\/glm-5\.3-flash/.test(noSum));
  ok('归因标签里的远端串也被转义，不成为注入面',
     !/<img src=x onerror=alert\(9\)>/.test(noSum)
     && /&lt;img src=x onerror=alert\(9\)&gt;/.test(noSum));
  ok('有总结时：归因标签跟着进悬停明细',
     /〔[^〕]*AA 每任务成本未变/.test(withSum), withSum.slice(0, 160));
}

/* ---------- 全局低分惩罚：真实看板处理器与存储 ---------- */
{
  const data = JSON.parse(JSON.stringify(D0));
  data.task_size_penalty = {lambda: 2, global_max_index: 100};
  const writes = [], stored = new Map();
  const storage = {getItem: k => stored.get(k) ?? null,
    setItem(k, v) { stored.set(k, v); writes.push([k, v]); }};
  const s = render(data, false, null, storage);
  const api = s.__mrWindow.__mrTest;
  ok('全局惩罚默认关闭且 TPS 模式隐藏附加开关',
     !api.getLowPenalty() && s.penaltywrap.getAttribute('hidden') !== null && writes.length === 0);
  s.sizebytask.onchange({target: {checked: true}});
  const unpenalized = s.scatterwrap.innerHTML;
  ok('任务模式才显示全局惩罚开关', s.penaltywrap.getAttribute('hidden') === null
     && !s.penalizelow.disabled);
  s.penalizelow.onchange({target: {checked: true}});
  ok('同分跨族使用相同全局面积倍率，最高分不缩',
     api.sizeFactorOf({index: 50, series_key: 'a'}) === .25
     && api.sizeFactorOf({index: 50, series_key: 'b'}) === .25
     && api.sizeFactorOf({index: 100}) === 1);
  ok('惩罚开关改变任务气泡且持久化独立偏好',
     s.scatterwrap.innerHTML !== unpenalized
     && stored.get('modelradar.dashboard.lowPenalty.v1') === 'on'
     && stored.get('modelradar.dashboard.sizeMode.v1') === 'task');
  s.sizebytask.onchange({target: {checked: false}});
  ok('切回 TPS 隐藏惩罚开关但保留偏好，倍率不生效',
     s.penaltywrap.getAttribute('hidden') !== null && api.getLowPenalty()
     && api.penaltyFactorFor({index: 50}) === 1);
  s.sizebytask.onchange({target: {checked: true}});
  ok('返回任务模式恢复惩罚，新页面从独立存储恢复且初始化不写回',
     api.penaltyFactorFor({index: 50}) === .25);
  const before = writes.length;
  const restored = render(data, false, null, storage);
  ok('重新加载恢复任务和惩罚两项偏好且无额外写入',
     restored.__mrWindow.__mrTest.getLowPenalty()
     && restored.__mrWindow.__mrTest.getSizeMode() === 'task' && writes.length === before);
  s.penalizelow.onchange({target: {checked: false}});
  ok('关闭惩罚后任务图逐字恢复，坐标与排序不变', s.scatterwrap.innerHTML === unpenalized);
  delete data.task_size_penalty;
  const legacy = render(data, false, null, storage);
  legacy.sizebytask.onchange({target: {checked: true}});
  ok('旧快照缺参数时禁用惩罚而非伪造拟合', legacy.penalizelow.disabled
     && !legacy.__mrWindow.__mrTest.penaltyReady());
}

/* ---------- 收尾：退出码 ---------- */
// 以前这里从不 exit：FAIL 再多，`node verify_dashboard.mjs` 也是 0，
// 于是打包/复核脚本里"跑过判据"这句话完全不代表判据真的通过了。
if (FAILED.length) {
  console.log(`\n✗ ${FAILED.length} 条判据未通过：`);
  FAILED.forEach(f => console.log(`    · ${f}`));
  process.exit(1);
}
console.log('\n全部判据通过');
