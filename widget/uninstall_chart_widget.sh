#!/bin/bash
# 卸载 ModelRadar 状态栏组件。
#
#   ./uninstall_chart_widget.sh --dry        # 只打印将执行什么，不动任何东西
#   ./uninstall_chart_widget.sh              # 卸组件 + 注销自启，**保留数据**
#   ./uninstall_chart_widget.sh --purge-data # 连运行时数据目录一起进废纸篓（含 SQLite 历史）
#
# 纪律：
#   · 一律走废纸篓，误操作可恢复；不做 rm -rf。
#   · 默认**不删** ~/Library/Application Support/ModelRadar/data —— 里面的 ModelRadar.db
#     是抓取历史，唯一不可再生。要真删必须显式 --purge-data，且仍进废纸篓。
#   · 源码目录本身不动（重装只需再装一次）；但**发行安装**放在 ~/Applications/ModelRadar/src
#     的那份会被一起收进废纸篓 —— 它不是"仓库"，留着就是没人认领的孤儿。
set -uo pipefail

DRY=0; PURGE=0
for a in "$@"; do
  [ "$a" = "--dry" ] && DRY=1
  [ "$a" = "--purge-data" ] && PURGE=1
done

APP="$HOME/Applications/ModelRadar.app"
SUPPORT="$HOME/Library/Application Support/ModelRadar"
OLD_SUPPORT="$HOME/Library/Application Support/ModelRadarChart"
DATA="$SUPPORT/data"
LAUNCH="$HOME/Library/LaunchAgents"
STAMP="$(date +%s)"
RECLAIM=0

run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else eval "$*"; fi; }
size() { [ -e "$1" ] && du -sk "$1" 2>/dev/null | cut -f1 || echo 0; }

echo "ModelRadar 状态栏组件卸载"
echo "=========================="
[ "$DRY" = 1 ] && echo "[dry-run 模式，只打印不执行]"

echo
echo "== 1. 退出进程 =="
if [ "$DRY" != 1 ]; then
  osascript -e 'tell application "ModelRadar" to quit' >/dev/null 2>&1 || true
  sleep 2
  # 模式必须锚定行尾：否则会匹配到发起这条命令的 shell 自己，永远"杀不完"
  pgrep -f 'MacOS/ModelRadar$' >/dev/null 2>&1 && { pkill -f 'MacOS/ModelRadar$' 2>/dev/null; sleep 1; }
fi
pgrep -f 'MacOS/ModelRadar$' >/dev/null 2>&1 \
  && echo "  ⚠️ 仍在运行（受限会话里 pkill 可能够不到 GUI 进程，请在旧图标 ⋯ 菜单里选退出）" \
  || echo "  ok 进程已退出"

echo
echo "== 2. 注销两个自启项 =="
for label in com.modelradar.chart com.modelradar.crawler; do
  if [ -f "$LAUNCH/$label.plist" ]; then
    run "launchctl bootout 'gui/$(id -u)/$label' >/dev/null 2>&1 || true"
    run "launchctl unload '$LAUNCH/$label.plist' >/dev/null 2>&1 || true"
    run "mv '$LAUNCH/$label.plist' '$HOME/.Trash/$label.plist.$STAMP'"
    echo "  ok  ${label}（plist 进废纸篓）"
  else
    echo "  --  $label 无 plist"
  fi
done

echo
echo "== 3. 组件与壳脚本进废纸篓 =="
if [ -d "$APP" ]; then
  RECLAIM=$((RECLAIM + $(size "$APP")))
  run "mv '$APP' '$HOME/.Trash/ModelRadar.app.$STAMP'"
  echo "  ok  ${APP}（$(size "$APP") KB）"
fi
if [ -d "$SUPPORT" ]; then
  # bin/ 与 runtime.env 等是 build 生成的可再生物，**无论是否 --purge-data 都该清**：
  # 以前整段被 `[ "$PURGE" != 1 ]` 门住，结果 --purge-data 反而留下指向已删目录的 runtime.env。
  for p in bin runtime.env .widget.pid .last_notified .last_alert.json .badge_state.json; do
    [ -e "$SUPPORT/$p" ] || continue
    run "mv '$SUPPORT/$p' '$HOME/.Trash/ModelRadar.$(basename "$p").$STAMP'"
    echo "  ok  $SUPPORT/$p"
  done
  [ "$PURGE" != 1 ] && echo "  保留 ${DATA}（抓取历史，重装后接着用）"
fi

# 发行安装（dmg）把源码放在 ~/Applications/ModelRadar/src，仓库不在本机 —— 上面那段
# 按"源码在别处"设计，会把这个目录留下成孤儿。认 marker 再动，避免误删同名无关目录。
PKG_DIR="$HOME/Applications/ModelRadar"
WAS_PKG=0
if [ -f "$PKG_DIR/src/fetch_model_data.py" ]; then
  WAS_PKG=1
  RECLAIM=$((RECLAIM + $(size "$PKG_DIR")))
  run "mv '$PKG_DIR' '$HOME/.Trash/ModelRadar-src.$STAMP'"
  echo "  ok  ${PKG_DIR}（发行安装的源码目录，$(size "$PKG_DIR") KB）"
fi
LOGS="$HOME/Library/Logs/ModelRadar"
if [ -d "$LOGS" ]; then
  run "mv '$LOGS' '$HOME/.Trash/ModelRadar-logs.$STAMP'"
  echo "  ok  ${LOGS}（日志）"
fi

echo
echo "== 4. 旧布局残留 =="
if [ -d "$OLD_SUPPORT" ]; then
  echo "  发现旧布局 ${OLD_SUPPORT}（$(size "$OLD_SUPPORT") KB）"
  run "mv '$OLD_SUPPORT' '$HOME/.Trash/ModelRadarChart.$STAMP'"
  echo "  ok  已进废纸篓"
else
  echo "  --  无"
fi

if [ "$PURGE" = 1 ]; then
  echo
  echo "== 5. --purge-data：运行时数据整体进废纸篓 =="
  if [ -d "$DATA" ]; then
    echo "  将删除 ${DATA}（$(size "$DATA") KB），含 ModelRadar.db 抓取历史"
    run "mv '$DATA' '$HOME/.Trash/ModelRadar-data.$STAMP'"
    echo "  ok  已进废纸篓（要恢复请从废纸篓取回）"
  fi
fi

echo
echo "== 验证 =="
if [ "$DRY" != 1 ]; then
  [ -d "$APP" ] && echo "  ⚠️ app 仍在（可能没移成功）" || echo "  ok app 已移除"
  [ -d "$OLD_SUPPORT" ] && echo "  ⚠️ 旧布局仍在" || echo "  ok 旧布局已清"
  echo "  （废纸篓内容无法在沙箱内确认，请在 Finder 里清倒）"
fi

echo
echo "保留项："
echo "  · $DATA —— SQLite 历史与 latest.json（重装后直接接着用）"
echo "  · 仓库源文件不删（要重装只需再装一次）"
if [ "${WAS_PKG:-0}" = 1 ]; then
  # 发行安装（dmg/zip）没带 swiftc 之外的东西：再跑 build_chart_widget.sh 会先 rm -rf 掉
  # 已装 app、再因为没装 Xcode 命令行工具而退出，留下"app 没了但 plist 还在"的状态。
  echo "  · 重装：把 dmg/zip 里的 install.command 再双击一次（用包内预编译 bundle，不需要编译器）"
else
  echo "  · 重装：$(cd "$(dirname "$0")" && pwd)/build_chart_widget.sh"
fi
[ "$PURGE" != 1 ] && echo "  · 连数据一起清：$0 --purge-data"
