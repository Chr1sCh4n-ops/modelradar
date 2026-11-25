#!/bin/bash
# 编译并安装 ModelRadar 状态栏组件，产出 ~/Applications/ModelRadar.app
#
#   ./build_chart_widget.sh              # 编译 + 装数据 + 迁移旧状态 + 启动 + 装 launchd
#   ./build_chart_widget.sh /tmp/x.app   # 只编到别处（调试用，不动已装实例）
#
# 职责边界（重要）：
#   · 「让新二进制生效」是**本脚本**的显式动作（quit + pkill + open）。app 自己运行时
#     只会在 ⋯ 菜单里提示"有新版本"，不会自动换版 —— 运行时自我替换会让"我看到的界面
#     是哪一版"不可复现，排查时代价比多一个图标更高。
#   · 可变状态一律落在 ~/Library/Application Support/ModelRadar/，仓库只放代码。
#     仓库因此可以随时挪走 / 重新 clone，SQLite 历史不会跟着丢。
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SRC="$HERE/ChartWidget.swift"
ENTRY="$HERE/main.swift"
APP_DIR="${1:-$HOME/Applications}"
APP="$APP_DIR/ModelRadar.app"
SUPPORT="$HOME/Library/Application Support/ModelRadar"
OLD_SUPPORT="$HOME/Library/Application Support/ModelRadarChart"   # 旧布局，只参与迁移
DATA="$SUPPORT/data"
DB="$DATA/ModelRadar.db"          # 台账文件名（改名前叫 modelradar.db，迁移检查两种名称）
BIN="$SUPPORT/bin"
LOG="$HOME/Library/Logs/ModelRadar/launch.log"
BUNDLE_ID="com.modelradar.chart"

# ---------------------------------------------------------------- 解释器探测
# 候选顺序：显式 env > 仓库自持 venv > Homebrew（Apple Silicon 与 Intel 都覆盖）> 系统。
# 判据是"能起解释器且版本 ≥3.9"——运行期零第三方依赖，所以不再拿 import lxml 当判据
# （那样会把没装 lxml 的收件人挡在门外，而那本来就不是必要条件）。
# 以前这里是写死的一行字面量，venv 一换路径就整条静默失效，且失败长得像"脚本坏了"。
pick_python() {
  printf '%s\n' \
    "${MODELRADAR_PYTHON:-}" \
    "$ROOT/.venv/bin/python" \
    "/opt/homebrew/bin/python3" \
    "/usr/local/bin/python3" \
    "/usr/bin/python3" |
  while IFS= read -r p; do
    [ -n "$p" ] && [ -x "$p" ] || continue
    # 零依赖版只要解释器能跑 + 版本够（代码用了 3.9+ 的内置泛型标注）
    if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      echo "$p"; break
    fi
  done
}

echo "== 探测解释器（≥3.9，无需任何第三方包）=="
PY="$(pick_python)"
[ -n "$PY" ] || {
  echo "找不到可用的 python3（要求 ≥3.9，本项目已零第三方依赖）。任选一种："
  echo "  装 Xcode 命令行工具：xcode-select --install   （自带 /usr/bin/python3）"
  echo "  或 Homebrew：brew install python"
  echo "  或 export MODELRADAR_PYTHON=/path/to/python"
  exit 1
}
echo "  用 ${PY}（$("$PY" -V 2>&1)）"

# ---------------------------------------------------------------- 状态目录 + 迁移
echo "== 准备运行时状态目录 $SUPPORT =="
mkdir -p "$DATA" "$BIN" "$HOME/Library/Logs/ModelRadar"

# 第六批 Radar 修复：SQLite backup 包含未 checkpoint 的 WAL，源库与 sidecar 一律保留。
# 先整体检查候选，双库并存时拒绝覆盖，不按主文件大小猜测数据新旧。
DB_SOURCES=("$DATA/modelradar.db")
for legacy in ModelRadar.db modelradar.db; do
  DB_SOURCES+=("$ROOT/data/$legacy" "$OLD_SUPPORT/data/$legacy" "$OLD_SUPPORT/$legacy")
done
"$PY" "$HERE/migrate_db.py" "$DB" "${DB_SOURCES[@]}" || exit 1

for f in latest.json latest.js logo_palette.json; do
  if [ -f "$ROOT/data/$f" ] && [ ! -f "$DATA/$f" ]; then
    mv "$ROOT/data/$f" "$DATA/$f" && echo "  已迁移 $f"
  fi
done
for d in logos logos_raw; do
  if [ -d "$ROOT/data/$d" ] && [ ! -d "$DATA/$d" ]; then
    mv "$ROOT/data/$d" "$DATA/$d" && echo "  已迁移 $d/"
  fi
done

# 旧布局的三条软链（chart.html / latest.json / dashboard.html 指回仓库）新布局不再需要。
# 只删软链，真文件一律不动。
if [ -d "$OLD_SUPPORT" ]; then
  find "$OLD_SUPPORT" -maxdepth 1 -type l -exec rm -f {} + 2>/dev/null
  rmdir "$OLD_SUPPORT" 2>/dev/null && echo "  已清掉旧布局目录 $OLD_SUPPORT" \
    || { echo "  ⚠️ $OLD_SUPPORT 里还有非软链文件，未删除。内容如下（都是旧版生成的壳脚本/戳文件，"
         echo "     确认后可自行丢废纸篓）："; ls -1A "$OLD_SUPPORT" | sed 's/^/       /'; }
fi

# 仓库内 data 必须是**真目录**，不能是指向 $DATA 的软链。
# 之前这里是软链，理由是"真实字节只有一份"——听起来干净，实际把看板弄坏了：
# dashboard.html 用 <script src="data/latest.js"> 相对取数，而 ~/Library 是 0700，
# 沙箱化的浏览器（Safari）不跟软链进去，于是页面能开、数据读不到。
# 现在改成：仓库里放一份真件（latest.js + logos/），由爬虫每次镜像；
# 权威状态（SQLite / latest.json / chart.html）仍只在 $DATA 一份。
if [ -L "$ROOT/data" ]; then
  rm "$ROOT/data" && echo "  已移除仓库内 data 软链（沙箱浏览器跟不进去）"
fi
if [ -d "$ROOT/data" ]; then
  # 老布局遗留的真文件先收进 ${DATA}，别让它们留在仓库里变成第二份权威
  find "$ROOT/data" -maxdepth 1 -type f \
    ! -iname '*.db' ! -iname '*.db-wal' ! -iname '*.db-shm' ! -iname '*.db-journal' \
    -exec mv -n {} "$DATA/" \; 2>/dev/null
  find "$ROOT/data" -maxdepth 1 -type f -name '.DS_Store' -exec rm -f {} + 2>/dev/null
  rmdir "$ROOT/data" 2>/dev/null || mkdir -p "$ROOT/data/logos"
fi
mkdir -p "$ROOT/data/logos"
# 立刻镜像一次，免得要等到下次抓取看板才有数据可读
# （必须 cd 到仓库：`python -` 的 sys.path[0] 是 cwd，不 cd 就 import 不到 mr_paths）
(cd "$ROOT" && "$PY" - <<'PYMIRROR'
import mr_paths
wrote = mr_paths.publish_for_browser()
print(f"  已镜像给看板：{len(wrote)} 个文件" if wrote else "  看板镜像已是最新（无变更）")
if not (mr_paths.REPO_DATA / 'latest.js').is_file():
    print("  ⚠️ 仓库内没有 data/latest.js —— 看板会显示读不到数据；先跑一次 crawl.sh")
PYMIRROR
)

# runtime.env 是组件与壳脚本的唯一路径来源：挪目录 / 换 venv 只需重跑本脚本。
# ⚠️ 值必须加双引号：macOS 的 Application Support 路径**含空格**，不引号的话
#    壳脚本里的 `. "$ENV_FILE"` 会把 $HOME/Library/Application 当值、
#    再把 Support/ModelRadar/data 当命令执行 → "No such file or directory"。
#    （Swift 侧按行解析不受影响，所以这个坑只在 bash 侧炸，更难对上了。）
# 中枢通知出口（可选集成）。**路径不进仓库**：这份代码要公开，而中枢在开发者个人机器的
# 目录里，写死在源码或注释里就等于在公开树里留一个指向它的箭头。来源优先级：
#   环境变量 MR_HUB_NOTIFY  >  只放在本机运行目录的配置文件 $SUPPORT/hub.conf  >  空
# 留空时组件与爬虫直接走系统横幅，功能不坏，只是少了审计落档与"点击打开看板"。
HUB="${MR_HUB_NOTIFY:-}"
if [ -z "$HUB" ] && [ -f "$SUPPORT/hub.conf" ]; then
  HUB="$(grep -m1 '^MR_HUB_NOTIFY=' "$SUPPORT/hub.conf" | cut -d= -f2- | tr -d '"')"
fi
cat > "$SUPPORT/runtime.env" <<ENV
# 由 widget/build_chart_widget.sh 生成，勿手改（改了下次 build 会被覆盖）
MR_REPO="$ROOT"
MR_DATA="$DATA"
MR_PYTHON="$PY"
MR_HUB_NOTIFY="$HUB"
ENV
echo "  已写 $SUPPORT/runtime.env（中枢出口：${HUB:+已配置}${HUB:-未配置，走系统横幅}）"

cat > "$BIN/regenerate.sh" <<'SH'
#!/bin/bash
# 只重画图表（不联网）。路径全部来自 runtime.env，不写死。
set -uo pipefail
ENV_FILE="$HOME/Library/Application Support/ModelRadar/runtime.env"
[ -f "$ENV_FILE" ] || { echo "缺 ${ENV_FILE}，请先跑 widget/build_chart_widget.sh" >&2; exit 1; }
. "$ENV_FILE"
"$MR_PYTHON" "$MR_REPO/render_scatter_svg.py" -o "$MR_DATA/chart.html" || exit 1
SH
cat > "$BIN/crawl.sh" <<'SH'
#!/bin/bash
# 抓三源 + 重画图表。渲染无条件执行：抓取失败时也要让图刷新成"如实标注旧数据"的样子，
# 但最终退出码沿用抓取结果 —— 别把失败伪装成成功。
#
# 第一个参数是 scheduled 时带上 --scheduled（launchd 每日任务用：抓到才收工，
# 否则按 1m→…→1h 退避重试到当日窗口耗尽）。组件上的「↻ 立即抓取」不带它 ——
# 人对着弹窗等的时候应该马上看到结果。
set -uo pipefail
ENV_FILE="$HOME/Library/Application Support/ModelRadar/runtime.env"
[ -f "$ENV_FILE" ] || { echo "缺 ${ENV_FILE}，请先跑 widget/build_chart_widget.sh" >&2; exit 1; }
. "$ENV_FILE"
cd "$MR_REPO" || exit 1
SCHED=""
[ "${1:-}" = "scheduled" ] && SCHED="--scheduled"
"$MR_PYTHON" fetch_model_data.py --sources aa,aa_progress,aa_times,openrouter,openrouter_usage,datalearner $SCHED
rc=$?
"$MR_PYTHON" "$MR_REPO/render_scatter_svg.py" -o "$MR_DATA/chart.html"
exit $rc
SH
chmod +x "$BIN/regenerate.sh" "$BIN/crawl.sh"
echo "  已生成 $BIN/{regenerate,crawl}.sh"

# 生成物自检。两份脚本都是用**带引号**的 heredoc 写出来的（<<'SH'），所以里面的 $VAR
# 不会被生成期展开 —— 每个大写变量只有两种活路：脚本自己赋过值，或 runtime.env 给了它。
# 三条都没有的话，`set -u` 下脚本一跑到那行就退出，而"退出"往往被外层写成 rc=1 的失败，
# 真实故障却被记成"抓取失败"（姊妹仓的 crawl.sh 就是这样用了一个从未定义的 $BIN，
# 每天定点失败一次，抓取其实全成功）。所以这条判据必须挂在生成之后、交付之前。
gate_generated() {
  local f="$1" v miss=""
  # 只看代码行：整行注释里的 $BIN / $TR_* 是**说明历史**，不是引用（不排除就会自己误报）。
  for v in $(grep -vE '^[[:space:]]*#' "$f" | grep -oE '\$\{?[A-Z][A-Z0-9_]*' | sed 's/[${]//g' | sort -u); do
    case "$v" in HOME | PATH | PWD | IFS | UID | GID | SHELL) continue ;; esac
    grep -qE "(^|[^A-Za-z0-9_])${v}=" "$f" && continue
    grep -qE "^${v}=" "$SUPPORT/runtime.env" && continue
    miss="$miss \$$v"
  done
  if [ -n "$miss" ]; then
    echo "  ✗ $(basename "$f") 用了既不在脚本内赋值、也不在 runtime.env 里的变量：$miss" >&2
    echo "    （带引号的 heredoc 不会在生成期展开，运行时 set -u 会当场退出）" >&2
    return 1
  fi
  echo "  ✓ $(basename "$f") 的变量引用全部有出处（脚本内赋值或 runtime.env）"
}
gate_generated "$BIN/crawl.sh" || exit 1
gate_generated "$BIN/regenerate.sh" || exit 1

# 先出一份图，免得组件起来读到空文件闪一下"找不到数据"
if [ ! -f "$DATA/chart.html" ]; then
  echo "== 首屏图表 =="
  bash "$BIN/regenerate.sh" || echo "  警告：没能画出 chart.html，组件会显示缺数据提示"
fi

# ---------------------------------------------------------------- 装 app bundle
# 两条路：
#   · 源码构建（默认）：需要 Xcode 命令行工具里的 swiftc；
#   · 发行安装（设了 MR_PREBUILT_APP）：直接用打包时预编译好的 bundle，
#     收件人机器上不用装 CLT —— 但抓取仍然要 python3（爬虫在那儿），这点不绕。
if [ -n "${MR_PREBUILT_APP:-}" ]; then
  [ -d "$MR_PREBUILT_APP" ] || { echo "MR_PREBUILT_APP 指的不是一个 app bundle：$MR_PREBUILT_APP" >&2; exit 1; }
  echo "== 安装预编译 bundle（不编译）：$MR_PREBUILT_APP =="
  mkdir -p "$APP_DIR"
  # 同路径判定用 -ef（比 inode），别用字符串相等：APFS 默认大小写不敏感，
  # 大小写变体 / 指向目标的软链 / 经软链的父目录都能骗过字符串比较，而判错一次的后果
  # 是下面那句 rm -rf 把正要读的源删掉（第一次端到端安装测试就死在这类问题上）。
  if [ "$MR_PREBUILT_APP" -ef "$APP" ]; then
    echo "  来源与目标同一个 bundle，跳过拷贝，只做重签"
  else
    rm -rf "$APP"; cp -R "$MR_PREBUILT_APP" "$APP"
  fi
  # 拷过来的 bundle 里 Info.plist 已被封存，但**签名标识必须与本机一致**：
  # UNUserNotificationCenter 按 bundle 标识认宿主，对不上就是静默不弹通知。
  # 重签失败不能 `|| true` 吞掉：吞了之后症状是"通知从此不弹"，排查代价远大于当场报错。
  if ! codesign --force --sign - --identifier "$BUNDLE_ID" "$APP" 2>/dev/null; then
    echo "  ✗ 重签失败：签名标识需等于 ${BUNDLE_ID}，否则通知宿主认不出" >&2
    exit 1
  fi
  codesign -dv "$APP" 2>&1 | grep -E "^Identifier" | sed 's/^/  /'
  xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true   # 从磁盘映像拷来的会带隔离属性
else
# ---------------------------------------------------------------- 编译 + 签名
echo "== 编译 =="
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
# 注意：**不加 -parse-as-library**，入口是 main.swift 里的顶层代码。
# 之前用 `@main` 挂 AppDelegate，delegate 只被 weak 属性持有 → 被 ARC 提前回收 →
# applicationDidFinishLaunching 永不触发（进程在跑但菜单栏什么都没有）。详见 main.swift。
# target 按宿主架构选：README/INSTALL.md 承诺 Intel 走源码编译 —— 写死 arm64 的话，
# Intel 机器上 swiftc 会"编译成功"地产出一个双击即 exec format 失败的二进制。
case "$(uname -m)" in
  arm64) TARGET="arm64-apple-macosx14.0" ;;
  *)     TARGET="x86_64-apple-macosx14.0" ;;
esac
swiftc -O -target "$TARGET" \
       -o "$APP/Contents/MacOS/ModelRadar" "$ENTRY" "$SRC" \
       -framework AppKit -framework WebKit 2>&1 | sed 's/^/  /'
[ -x "$APP/Contents/MacOS/ModelRadar" ] || { echo "编译失败"; exit 1; }

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>ModelRadar</string>
  <key>CFBundleDisplayName</key><string>ModelRadar</string>
  <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
  <key>CFBundleExecutable</key><string>ModelRadar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

# ⚠️ 必须在写完 Info.plist 之后显式重签。
# swiftc 产出的是 linker-signed adhoc 签名：标识取可执行文件名（ModelRadar），
# 且 Info.plist 不参与封存（codesign -dv 显示 "Info.plist=not bound"）。
# UNUserNotificationCenter 按 bundle 标识认宿主，标识对不上直接 error 1（not authorized），
# 于是"新数据弹通知"整条链路静默失效 —— 这就是此前每次启动都 granted=false 的根因。
echo "== 签名（标识必须等于 ${BUNDLE_ID}）=="
codesign --force --sign - --identifier "$BUNDLE_ID" "$APP" 2>&1 | sed 's/^/  /'
codesign --verify "$APP" >/dev/null 2>&1 && echo "  verify ok" || echo "  ⚠️ codesign --verify 未通过"
codesign -dv "$APP" 2>&1 | grep -E "^Identifier|^flags|Info.plist" | sed 's/^/  /'

echo "== 注册 LaunchServices =="
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f -R "$APP" >/dev/null 2>&1 || true

fi

# ---------------------------------------------------------------- 换版生效
echo "== 退出旧实例（让新二进制生效是本脚本的活，不是 app 运行时的活）=="
# osascript 在受限会话里会报 -10004，那不算失败，继续用 pkill 兜底
osascript -e 'tell application "ModelRadar" to quit' >/dev/null 2>&1 || true
sleep 1
# 锚定到本次 bundle 的完整可执行文件路径，避免误杀同名旧副本
pgrep -f "$APP/Contents/MacOS/ModelRadar" >/dev/null 2>&1 && { pkill -f "$APP/Contents/MacOS/ModelRadar" 2>/dev/null; sleep 1; }
STRAY="$(pgrep -f 'MacOS/ModelRadar' | tr '\n' ' ')"
if [ -n "$STRAY" ]; then
  echo "  ⚠️ 仍有 ModelRadar 进程存活（pid: ${STRAY}）。受限会话里 pkill 可能够不到 GUI 进程；"
  echo "     新实例的 ⋯ 菜单会显示「另有 N 个实例在跑」，请在旧图标菜单里手动退出。"
fi

# 不清空旧日志：那等于把上一次故障的证据一起抹掉。只插一条分隔行。
mkdir -p "$(dirname "$LOG")"
MARK="----- build $(date '+%m-%d %H:%M:%S') 重编译并启动新实例 ----_"
printf -- '%s\n' "$MARK" >> "$LOG"
# 只看这条分隔行之后的日志：grep 整个历史日志会被**上一次成功启动**留下的行骗过，
# 表现为"新二进制其实没起来，脚本照样报 OK"。
after_mark() { awk -v m="$MARK" 'index($0,m){f=1;next} f' "$LOG"; }

open "$APP" || echo "警告：open 启动失败，见上方日志"
sleep 4

# 判定"装上了没有"必须看回调自己落的日志，不能只看进程在不在——
# 上一轮的教训：进程在跑 + isFinishedLaunching + policy=1 全为真，图标却是零个。
# 那种"全部指标正常但其实没装"的状态，只有回调日志能拆穿。
if after_mark | grep -q "启动完成" && after_mark | grep -q "isVisible=true"; then
  echo "OK：启动回调已执行、状态栏项 isVisible=true（证据 ${LOG}）"
  after_mark | grep -E "状态栏项已创建|启动完成|同 bundle 还有" | tail -3 | sed 's/^/    /'
else
  echo "警告：回调日志不完整，看 ${LOG}"
  [ -f "$LOG" ] && tail -8 "$LOG" | sed 's/^/    /' || echo "    （日志文件没建出来 → applicationDidFinishLaunching 没进）"
fi

# 一并装好 launchd（状态栏自启 + 每日爬虫），plist 创建/装载统一由 install_launchd.sh 负责。
# 非 GUI 会话里装载会失败属正常，plist 已就位，登录后自动生效。
# MR_APP 必须传：install_launchd.sh 会把这个路径写进 `open <app>`，不传它就用默认的
# $HOME/Applications/ModelRadar.app —— 一旦本脚本被传了自定义目录，plist 会指向不存在的 app。
LAUNCHD_RC=0
if [ -f "$HERE/../install_launchd.sh" ]; then
  echo "== 安装 launchd 自启项 =="
  MR_APP="$APP" bash "$HERE/../install_launchd.sh"
  LAUNCHD_RC=$?
  # 不把非零码用 `|| echo "属正常"` 吞掉：那样 GUI 会话里真没装载上也一律报成正常。
  if [ "$LAUNCHD_RC" != 0 ]; then
    echo "  ↑ $LAUNCHD_RC 个任务当前没装载上。非 GUI 会话（SSH/受限沙箱）里属预期："
    echo "    plist 已写好，下次登录会自己读到；要立刻生效就照上面打印的命令手工 bootstrap 一次。"
  fi
fi

echo
echo "用法："
echo "  点菜单栏图标          # 弹出性价比定位图"
echo "  ⟳ 重新生成            # 数据没变时重画（秒回）"
echo "  ↻ 立即抓取            # 跑爬虫，约 1 分钟"
echo "  ⋯ → 数据状态…         # 哪个源挂了 / 数据多新 / 路径在哪 / 跑的哪一版"
echo "  ⋯ → 打开日志目录      # launch.log（组件自身）+ script.log（脚本输出）"
echo "  卸载：widget/uninstall_chart_widget.sh --dry 先看，再去掉 --dry 执行"
