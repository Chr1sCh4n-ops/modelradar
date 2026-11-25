#!/bin/bash
# 安装两个 LaunchAgent（开机/按计划，无需人工干预）：
#   1) com.modelradar.chart   —— 登录时把菜单栏组件拉起来
#   2) com.modelradar.crawler —— 每天定点抓一次；失败由 fetch_model_data.py --scheduled
#      自己按 1m→…→1h 指数退避重试
#
#   ./install_launchd.sh          # 写 plist + 装载 + 报告真实状态
#   ./install_launchd.sh --now    # 额外立刻踢一次 crawler，用来验证"每日路径"真能跑通
#
# 路径一律来自 ~/Library/Application Support/ModelRadar/runtime.env（由 build 生成），
# 本脚本不再自己拼任何绝对路径 —— 以前 plist 里写死了仓库和 venv 位置，
# 那两处任一变动都会让"每日抓取"静默失效。
#
# ⚠️ 每日任务的 ProgramArguments 是 **/bin/bash + bin/crawl.sh scheduled**，不是 venv 里的
# python。原因（2026-10-08 实测）：macOS 26 对 ~/Library/LaunchAgents 这类 legacy agent 施加
# AMFI "Launch Constraint"，按**被 exec 的那个二进制的审批记录（LWCR）**放行。plist 直接指
# Homebrew 的 python 时，`brew upgrade python@3.14` 一换 keg（3.14.5→3.14.8，cdhash 变了），
# 记录就不匹配，进程在 spawn 那一刻被杀：
#   AMFI: Launch Constraint Violation (enforcing) … /opt/homebrew/…/python3.14
#   xpcproxy exited due to OS_REASON_CODESIGNING
#   launchd: removing service since it exited with consistent failure
# 全程不打日志、`launchctl print` 只留 last exit reason，看起来就像"定时没跑"。
# 走 /bin/bash（Apple 二进制，不受第三方约束）再由 crawl.sh 从 runtime.env 取解释器，
# brew 怎么升级都不受影响。
#
# ⚠️ 判据教训：以前用 `launchctl list | grep com.modelradar` 判断装载成功，
# 但 macOS 会把**正在运行的 GUI 应用**也登记成 `application.com.modelradar.chart.*`，
# 于是组件一跑起来这条 grep 就命中，plist 到底加载没加载根本判不出来 ——
# 结果"每日自动抓取"实际从未装载（日志文件压根没被创建），而脚本一直报成功。
# 现在改用 `launchctl print gui/<uid>/<label>` 精确查 label。
set -uo pipefail

# MR_SKIP_LAUNCHD=1 → 只写 plist、不碰 launchctl。
# 用途：在假 HOME / CI / 沙箱会话里验证安装链路，又不能把真机上已装载的同名任务顶掉
# （launchd 的 gui 域按 uid 走，不看 HOME，装了假路径就会把真任务换掉）。

HERE="$(cd "$(dirname "$0")" && pwd)"
LAUNCH="$HOME/Library/LaunchAgents"
ENV_FILE="$HOME/Library/Application Support/ModelRadar/runtime.env"
LOGDIR="$HOME/Library/Logs/ModelRadar"
UID_NUM="$(id -u)"

if [ ! -f "$ENV_FILE" ]; then
  echo "缺 $ENV_FILE —— 先跑一次 widget/build_chart_widget.sh 生成它，再回来装 launchd。"
  exit 1
fi
# shellcheck disable=SC1090
. "$ENV_FILE"
for k in MR_REPO MR_DATA MR_PYTHON; do
  eval "v=\${$k:-}"
  [ -n "$v" ] || { echo "runtime.env 里 $k 为空，请重跑 build"; exit 1; }
done
# app 落点由调用方决定（build 脚本可能把 bundle 装到别处，如 --no-install / 调试目录）。
# 以前这里写死 $HOME/Applications/ModelRadar.app，而 build 传了自定义目录，plist 就指向
# 一个不存在的 app —— 装完当场有图标，下次登录后 LaunchAgent 打不开，菜单栏静默消失。
APP="${MR_APP:-$HOME/Applications/ModelRadar.app}"
# 每日任务的执行体：bin/crawl.sh 与 runtime.env 同一个 SUPPORT 目录下，落点由 ENV_FILE
# 的位置推出来（不另写一份绝对路径，否则 SUPPORT 一挪这里就静默指到别处）。
SUPPORT_DIR="$(dirname "$ENV_FILE")"
CRAWL_SH="$SUPPORT_DIR/bin/crawl.sh"
if [ ! -f "$CRAWL_SH" ]; then
  echo "缺 $CRAWL_SH —— 先跑一次 widget/build_chart_widget.sh 生成壳脚本，再装 launchd。"
  echo "（每日任务必须由 /bin/bash 调它：plist 直接指 Homebrew 的 python 会被 macOS 26"
  echo "  的 Launch Constraint 在 spawn 时杀掉，brew 一升级 python 定时就静默失效。）"
  exit 1
fi
mkdir -p "$LAUNCH" "$LOGDIR"

echo "== 写 plist（数据目录 ${MR_DATA}，执行体 /bin/bash + ${CRAWL_SH}）=="
cat > "$LAUNCH/com.modelradar.crawler.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.modelradar.crawler</string>
  <key>ProgramArguments</key>
  <array>
    <!-- /bin/bash 而不是 ${MR_PYTHON}：见文件头的 Launch Constraint 说明。
         解释器仍由 crawl.sh 从 runtime.env 取，路径只有一个来源。 -->
    <string>/bin/bash</string>
    <string>${CRAWL_SH}</string>
    <string>scheduled</string>
  </array>
  <key>WorkingDirectory</key><string>${MR_REPO}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>$HOME</string>
    <key>MODELRADAR_DATA_DIR</key><string>${MR_DATA}</string>
  </dict>
  <!-- 每天 08:15 本地时间（TokenRadar 用 08:20，两条爬虫留 5 分钟错开）。
       用 StartCalendarInterval 而不是 StartInterval：后者是"距上次装载满 86400s"，
       机器睡过去就顺延，永远对不上"每天一次"的直觉。
       原为 07:30：2026-11-07/08 实测 07–09 点唤醒窗口内 Tailscale MagicDNS 还没就绪，
       DNS 解析瞬落（nodename nor servname / Could not resolve host），整天抓取全废。 -->
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>15</integer></dict>
  <key>StandardOutPath</key><string>${LOGDIR}/crawler.log</string>
  <key>StandardErrorPath</key><string>${LOGDIR}/crawler.err</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST

cat > "$LAUNCH/com.modelradar.chart.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.modelradar.chart</string>
  <!-- open 带绝对路径：`open -a ModelRadar` 是按名字问 LaunchServices，
       废纸篓里留过同名 .app（卸载脚本就是丢进废纸篓的）时可能启错一份。 -->
  <key>ProgramArguments</key>
  <array><string>/usr/bin/open</string><string>${APP}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>ProcessType</key><string>UI</string>
</dict>
</plist>
PLIST

# lint 失败必须当场停：plist 语法坏时 bootstrap 只会给你一句模糊的"未装载"，
# 沿着它排查会绕很远。别只打印 lint 输出装饰一下就往下走。
for p in "$LAUNCH/com.modelradar.crawler.plist" "$LAUNCH/com.modelradar.chart.plist"; do
  if ! plutil -lint "$p" | sed 's/^/  /'; then
    echo "  ✗ plist 语法校验失败：$p" >&2
    exit 1
  fi
done

# 判据：从**已经写好的 plist** 里读回 `open` 的目标再验它存在，而不是信 $APP 变量。
# 让这个脚本"装了个指向不存在 app 的自启项"的正是这两者不一致：bundle 被嵌进子目录时
# plist 仍写扁平路径，装完当场有图标，下次登录后却静默消失，而脚本一路报 OK。
PLIST_APP="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:1' \
             "$LAUNCH/com.modelradar.chart.plist" 2>/dev/null)"
if [ -z "$PLIST_APP" ] || [ ! -x "$PLIST_APP/Contents/MacOS/ModelRadar" ]; then
  echo "  ✗ plist 要 open 的 app 不存在或不可执行：${PLIST_APP:-<读不到>}"
  echo "    期望落点 $APP —— 先跑 widget/build_chart_widget.sh 装好组件再装载"
  exit 1
fi

# 同一个纪律用在每日任务上：从**写好的 plist** 里读回执行体，验它存在且是 /bin/bash 调壳脚本。
# 判据只查"服务装载了没"是不够的 —— 上一版 plist 指 Homebrew python，装载成功、触发也成功，
# 进程却在 exec 那一刻被 AMFI 杀掉，本机看不到任何报错。
PLIST_EXEC="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' \
             "$LAUNCH/com.modelradar.crawler.plist" 2>/dev/null)"
PLIST_SCRIPT="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:1' \
               "$LAUNCH/com.modelradar.crawler.plist" 2>/dev/null)"
PLIST_ARG2="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:2' \
             "$LAUNCH/com.modelradar.crawler.plist" 2>/dev/null)"
if [ "$PLIST_EXEC" != "/bin/bash" ]; then
  echo "  ✗ 每日任务的执行体是 ${PLIST_EXEC}，不是 /bin/bash"
  echo "    指第三方解释器会被 macOS 的 Launch Constraint 在 spawn 时杀掉（brew 升级 python 即失效）"
  exit 1
fi
if [ -z "$PLIST_SCRIPT" ] || [ ! -f "$PLIST_SCRIPT" ]; then
  echo "  ✗ plist 要执行的壳脚本不存在：${PLIST_SCRIPT:-<读不到>}"
  exit 1
fi
if [ "$PLIST_ARG2" != "scheduled" ]; then
  echo "  ✗ 每日任务没带 scheduled（第二个参数是 ${PLIST_ARG2:-<空>}）：少了退避重试，"
  echo "    一次偶发 5xx 就会让当天快照断档"
  exit 1
fi
bash -n "$PLIST_SCRIPT" || { echo "  ✗ 壳脚本语法有错：$PLIST_SCRIPT"; exit 1; }
echo "  ✓ 执行体 /bin/bash + $(basename "$PLIST_SCRIPT") scheduled，脚本语法通过"

# ------------------------------------------------------------------ 装载
# bootstrap 是现代入口（load -w 已废弃但仍可用）；两个都试，成败只看 print 查得到查不到。
load_job() {
  local label="$1" plist="$LAUNCH/$1.plist"
  launchctl bootout "gui/$UID_NUM/$label" >/dev/null 2>&1
  launchctl bootstrap "gui/$UID_NUM" "$plist" >/dev/null 2>&1
  launchctl enable "gui/$UID_NUM/$label" >/dev/null 2>&1
  if launchctl print "gui/$UID_NUM/$label" >/dev/null 2>&1; then
    echo "  ok   $label 已装载"
    return 0
  fi
  # 退回旧接口再试一次
  launchctl load -w "$plist" >/dev/null 2>&1
  if launchctl print "gui/$UID_NUM/$label" >/dev/null 2>&1; then
    echo "  ok   $label 已装载（旧接口）"
    return 0
  fi
  echo "  FAIL $label 未装载"
  return 1
}

echo "== 装载 =="
C_OK=0; H_OK=0
if [ "${MR_SKIP_LAUNCHD:-0}" = 1 ]; then
  echo "  （MR_SKIP_LAUNCHD=1 → 跳过装载；plist 已写入，真机任务未受影响）"
  exit 0
fi

load_job com.modelradar.crawler || C_OK=1
load_job com.modelradar.chart   || H_OK=1

if [ "$C_OK" = 1 ] || [ "$H_OK" = 1 ]; then
  echo
  echo "== 没能全部装载：当前多半不是 GUI 登录会话（SSH / 受限沙箱）=="
  echo "这不是失败：plist 已写好，下次登录 macOS 会自己读。"
  echo "现在先确认组件在不在跑："
  if pgrep -f 'MacOS/ModelRadar$' >/dev/null; then
    echo "  组件在跑（pid $(pgrep -f 'MacOS/ModelRadar$' | head -1)），但**每日自动抓取仍未生效**。"
  else
    echo "  组件没在跑：open '$APP'"
  fi
  echo "在【自己的终端 App】里手工装载："
  echo "  launchctl bootstrap gui/$(id -u) '$LAUNCH/com.modelradar.crawler.plist'"
fi

echo
echo "== 复核（只认 launchctl print，不认 grep list）=="
for label in com.modelradar.crawler com.modelradar.chart; do
  if launchctl print "gui/$UID_NUM/$label" 2>/dev/null | grep -E "state = |program = |run interval|calendarinterval" | head -3 | sed "s/^/  $label: /"; then
    :
  else
    echo "  $label: 未装载"
  fi
done

if [ "${1:-}" = "--now" ]; then
  echo
  echo "== 立刻踢一次 crawler（验证每日路径真能跑通，不是只看命令发出去了）=="
  # 上一版这里 kickstart 完就打一句"等 40s 后自己看日志"，等于把最关键的判据交给人。
  # 装载成功、触发成功、进程被 AMFI 杀掉 —— 这三种情况在前者的输出里长得一模一样。
  LOG="$LOGDIR/crawler.log"
  before_lines="$( [ -f "$LOG" ] && wc -l < "$LOG" | tr -d ' ' || echo 0 )"
  launchctl kickstart -k "gui/$UID_NUM/com.modelradar.crawler" 2>&1 | sed 's/^/  /'
  # 成功判据落在**本次新增行里的完成标记**上，不是"日志变长了"：python 的重定向按块刷，
  # 中途只蹦出一两行属正常，拿字节数当成功会在抓取还没跑完时就报绿（第一版就是这么错的）。
  saw_run=0
  done_ok=0
  i=0
  while [ "$i" -lt 40 ]; do
    i=$((i + 1))
    sleep 5
    st="$(launchctl print "gui/$UID_NUM/com.modelradar.crawler" 2>/dev/null)"
    case "$st" in *"state = running"* | *"state = spawning"* | *"pid = "*) saw_run=1 ;; esac
    if [ "$saw_run" = 1 ] \
       && tail -n +"$((before_lines + 1))" "$LOG" 2>/dev/null | grep -q "已写出 .*latest.json"; then
      done_ok=1
      break
    fi
  done
  echo "  基线行数=${before_lines}｜起过进程=${saw_run}｜跑到完成标记=${done_ok}"
  if [ "$done_ok" = 1 ]; then
    echo "  ✓ 每日路径实跑通。本次新增："
    tail -n +"$((before_lines + 1))" "$LOG" | sed 's/^/    /'
  elif [ "$saw_run" = 0 ]; then
    echo "  ✗ 200s 内没起过进程。launchd 记录："
    launchctl print "gui/$UID_NUM/com.modelradar.crawler" 2>&1 \
      | grep -E "state =|last exit|runs =" | sed 's/^/    /'
    echo "    若含 OS_REASON_CODESIGNING / Launch Constraint Violation：执行体不是 /bin/bash"
    echo "    （见文件头说明），或壳脚本被删了"
    exit 1
  else
    # 到点还没看到完成标记。分两种：进程还在（上游慢，python 的重定向是块缓冲，跑完才刷）
    # 和进程已经没了却没写完成行（真失败）。两者不能都报"失败"，也不能都报"成功"。
    if launchctl print "gui/$UID_NUM/com.modelradar.crawler" 2>/dev/null | grep -q "state = running"; then
      echo "  ⚠ 进程还在跑（200 秒内没写完）—— 上游慢时属正常，不算失败也不算成功："
      echo "     过一会儿自己看： tail -20 $LOG"
      echo "     判据：出现\"已写出 … latest.json\"那行才算这一轮真的跑完"
      exit 2
    fi
    echo "  ✗ 进程已退出但没写出完成标记 —— 按失败处置："
    tail -n +"$((before_lines + 1))" "$LOG" 2>/dev/null | sed 's/^/    /'
    tail -5 "$LOGDIR/crawler.err" 2>/dev/null | sed 's/^/    err| /'
    exit 1
  fi
fi

echo
echo "日志：$LOGDIR/"
echo "手动抓一次：  $MR_PYTHON $MR_REPO/fetch_model_data.py"

# 退出码如实反映装载结果。以前无条件 exit 0，调用方那句 `|| echo "属正常"` 于是永不触发，
# install.command 就一路报"完成"——收件人真没装载上时会被骗（非 GUI 会话才是预期内的失败）。
# 计数即退出码：0=两个任务都在，>0=有几个没装上，由调用方按语义解释。
exit $((C_OK + H_OK))
