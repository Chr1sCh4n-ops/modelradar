#!/bin/bash
# 3.9 兼容闸：收件人机器上多半只有 Xcode CLT 自带的 /usr/bin/python3（3.9），
# 而本机跑的是 .venv 里的新版本 —— 只有新版本能过的代码会在别人机器上崩。
# 我自己就在导出实测里撞过一次（render_scatter_svg 的 `X | None` 注解在 3.9 报
# TypeError，因为那个文件当时少了 `from __future__ import annotations`）。
#
# 两道一起跑：
#   · import 仓库根的运行期模块 —— 抓"注解在 def 时被求值"这类 py_compile 看不到的问题；
#   · 对**全部** .py 做语法编译 —— 覆盖不在 import 链上的开发期脚本与夹具。
# 名单一律由目录扫出来，不手写：手写那份已经漏过一次（parse_parity），加新模块还会再漏。
# 可选增强（lxml 解析参考实现、svgelements 裁 logo）允许缺失，它们只降级不致命。
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="${1:-/usr/bin/python3}"
[ -x "$PY" ] || { echo "  跳过（没有 ${PY}）"; exit 0; }
V="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
FAIL=0
# 变量一律带 ${}：echo 文案里中文/全角标点紧贴 $VAR 时，C locale 下的 bash 会把
# UTF-8 高位字节并进变量名（${PY}（ → "PY\xef…"），set -u 直接 unbound variable 崩闸。
echo "== 兼容闸：用 ${PY}（${V}）逐个 import 运行期模块 =="
MODS="$(cd "$HERE" && for f in *.py; do printf '%s\n' "${f%.py}"; done | sort)"
while IFS= read -r m; do
  [ -n "$m" ] || continue
  # 路径经环境变量进 python，不拼进 -c 源码字符串：仓库路径含空格/引号时
  # 字符串拼接会炸出难懂的语法错误。
  out=$(HERE="$HERE" "$PY" -c "
import sys, os; sys.path.insert(0, os.environ['HERE'])
try:
    __import__('$m')
except ModuleNotFoundError as e:
    if e.name not in ('lxml', 'svgelements'):
        print(e); sys.exit(1)
except Exception as e:
    print(type(e).__name__, e); sys.exit(1)
" 2>&1) || { echo "  ✗ $m: $out"; FAIL=1; }
done <<< "$MODS"

while IFS= read -r f; do
  "$PY" -m py_compile "$f" 2>/dev/null || { echo "  ✗ ${V} 语法不兼容：${f#"$HERE"/}"; FAIL=1; }
done < <(find "$HERE" -name '*.py' -not -path '*/.venv/*' -not -path '*/__pycache__/*' | sort)
if [ "$FAIL" = 1 ]; then
  echo "  ✗ 存在 ${V} 不兼容的东西 —— 不要分发"
  exit 1
fi
echo "  ✓ 运行期模块在 ${V} 下可 import，且全部 .py 语法兼容"
