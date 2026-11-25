#!/bin/bash
# 打分发用的最小包：预编译 app + 运行必需的源码 + 一键安装脚本 → .dmg（附带 .zip）
#
#   bash pack_release.sh              # 产出 dist/ModelRadar-<版本>.dmg 与 .zip
#   bash pack_release.sh --no-dmg     # 只准备 staging 目录与 zip（调试用）
#
# 刻意不进包的东西（每一项都有理由，别顺手加回来）：
#   .venv            虚拟环境**不可搬迁**（里面写死了绝对路径），对方装了反而出错
#   data/            只是给沙箱浏览器用的镜像（latest.js + 厂商 logo），重抓即生成
#   .git             历史与本机路径噪声；要源码请走仓库
#   runtime.env/DB   运行时的私有状态与台账，含本机的绝对路径
#   widget/.build     编译中间产物
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1

MAKE_DMG=1
# 分发前必过 3.9 兼容闸（收件人机器上通常只有系统 python3）。
# 分成两步写：`[ -x ] && 闸门 || die` 会把"这台机器没有系统 python3"也报成"闸门未通过"。
if [ -x /usr/bin/python3 ]; then
  bash "$HERE/tools/py39_gate.sh" /usr/bin/python3 || { echo "3.9 兼容闸未通过" >&2; exit 1; }
else
  echo "✗ 本机没有 /usr/bin/python3，无法做 3.9 兼容验证 —— 这一份未经低版本 Python 检验" >&2
  exit 1
fi

MISSING=0
[ "${1:-}" = "--no-dmg" ] && MAKE_DMG=0
trap '[ "$MISSING" = 1 ] && echo "有必需文件没进包 —— 不要分发" >&2' EXIT

HASH="$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo dev)"
VER="1.0+$HASH-$(date +%Y%m%d)"
NAME="ModelRadar-$VER"
STAGE="$HERE/dist/$NAME"
SRC_BUNDLE="$HOME/Applications/ModelRadar.app"       # 以本机已装的产物为预编译来源

echo "打包 $NAME"
[ -d "$SRC_BUNDLE" ] || { echo "本机没有已装好的 $SRC_BUNDLE —— 先跑 widget/build_chart_widget.sh" >&2; exit 1; }
ARCH="$(lipo -archs "$SRC_BUNDLE/Contents/MacOS/ModelRadar" 2>/dev/null || echo unknown)"
echo "  预编译 app 架构：${ARCH}（Intel 机器跑不了这个分支，得自己编译）"

rm -rf "$STAGE"; mkdir -p "$STAGE/src" "$STAGE/ModelRadar.app"

# ---------------------------------------------------------------- 源码（运行必需）
# 以 `git ls-files` 为准，而不是"把几个目录整棵拷进来"。
# 后者会把工作树里只供本机使用的辅助文件一起装走 —— 实际踩过：一份扫描名单跟着
# `cp -R tools` 进了 dmg，等于把开发环境的细节发给收件人。跟踪清单就是边界。
SRC_LIST="$(git -C "$HERE" ls-files)"
[ -n "$SRC_LIST" ] || { echo "✗ git ls-files 为空，没有可打包的源码" >&2; exit 1; }
while IFS= read -r f; do
  [ -f "$f" ] || continue
  mkdir -p "$STAGE/src/$(dirname "$f")"
  cp -p "$f" "$STAGE/src/$f"
done <<< "$SRC_LIST"
rm -rf "$STAGE/src/widget/.build"                    # 中间产物不带（保险起见再清一次）
for f in src/fetch_model_data.py src/summarize.py src/tools/test_rules.py src/mr_paths.py src/task_size.py \
         src/widget/build_chart_widget.sh \
         src/widget/ChartWidget.swift src/install_launchd.sh src/widget/uninstall_chart_widget.sh \
         src/config/watchlist.json src/verify_dashboard.mjs src/tools/py39_gate.sh; do
  [ -f "$STAGE/$f" ] || { echo "  ✗ 包里缺 $f" >&2; MISSING=1; }
done

# 出门前的隐私闸在"产出"之前 —— 那时 app 与 install.command 都已就位，扫的才是最终进包的东西。

# ---------------------------------------------------------------- app 与安装脚本
cp -R "$SRC_BUNDLE/." "$STAGE/ModelRadar.app/"
xattr -dr com.apple.quarantine "$STAGE/ModelRadar.app" 2>/dev/null || true

cat > "$STAGE/install.command" <<'CMD'
#!/bin/bash
# 双击运行：源码放 ~/Applications/ModelRadar/src，组件装成 ~/Applications/ModelRadar.app，
# 写 runtime.env 与 bin 脚本，注册 LaunchAgent（登录后每天 07:30 抓一次），然后启动组件。
# 不需要管理员密码，不碰 /Applications，不装任何系统组件。
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Applications/ModelRadar"
SUPPORT="$HOME/Library/Application Support/ModelRadar"

echo "== ModelRadar 安装 =="
# 爬虫是 Python 写的：没有 python3（≥3.9）就没法取数，这里先说清楚再动手
PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "✗ 没找到可用的 python3（需 ≥3.9）。装 Xcode 命令行工具即可：xcode-select --install"
  echo "  装完重新双击本脚本。"
  read -r -p "按回车关闭…" _; exit 1
fi
echo "  用 ${PY}（$("$PY" -V 2>&1)）"

mkdir -p "$HOME/Applications"
if [ -e "$DEST" ]; then
  # 以前 mv 失败只 `|| true`，然后无条件 echo "已移入废纸篓"，接着 cp 就把源码嵌成
  # dest/src/src —— 一个既装不上也退不掉的目录。
  if mv "$DEST" "$HOME/.Trash/ModelRadar-old.$(date +%s)" 2>/dev/null; then
    echo "  旧的 src 目录已移入废纸篓"
  else
    echo "  ✗ 旧目录 $DEST 没能移进废纸篓，已中止（不覆盖、不静默继续）"
    read -r -p "按回车关闭…" _; exit 1
  fi
fi
mkdir -p "$DEST"
cp -R "$HERE/src" "$DEST/src"

# 复用仓库里那套安装逻辑（状态目录、迁移、runtime.env、bin 脚本、launchd、启动、自检），
# 只把"编译"换成"用包里的预编译 bundle" —— 避免在收件人机器上再造一遍流程。
# ⚠️ 这里**故意不传位置参数**：默认落点是 ~/Applications/ModelRadar.app（扁平 bundle），
# 而 uninstall 脚本、launchd 的 `open <app>` 认的都是这个路径。以前传了 "$DEST"，
# bundle 被嵌成 ~/Applications/ModelRadar/ModelRadar.app，plist 仍写扁平路径 →
# 装完当场能看到图标，**下次登录后 LaunchAgent 打不开 app，菜单栏静默消失**。
export MR_PREBUILT_APP="$HERE/ModelRadar.app"
bash "$DEST/src/widget/build_chart_widget.sh"
rc=$?
echo
if [ "$rc" = 0 ]; then
  echo "菜单栏应出现一个柱状图小图标；点它看定位图，⋯ → 数据状态… 看三个源的情况。"
  # 装完当场抓一次：build 只会用"已有数据"画图，收件人机器上没有历史，
  # 不踢这一脚就要等到次日 07:30 才有内容，而图标当天一直显示"找不到图表数据"。
  echo "== 首次取数（约 1 分钟，走网络）=="
  "$PY" "$DEST/src/fetch_model_data.py" 2>&1 | tail -5
  if [ -f "$SUPPORT/data/latest.json" ]; then
    echo "  ✓ 拿到数据，重画状态栏图"
    [ -x "$SUPPORT/bin/regenerate.sh" ] && bash "$SUPPORT/bin/regenerate.sh" >/dev/null 2>&1
  else
    echo "  ✗ 这次没拿到数据（网络不通或源挂了）。菜单栏会如实显示暂无数据，"
    echo "    稍后点图标 ⋯ → ↻ 立即抓取 再试一次即可。"
  fi
  echo "此后每天 07:30 自动抓（launchd，不需要终端）。"
else
  echo "安装脚本返回 $rc —— 把上面的输出发回来即可，日志在 ~/Library/Logs/ModelRadar/"
fi
# 自证一步：不看安装脚本说了什么，直接问 launchd 现在到底装没装上。
if launchctl print "gui/$(id -u)/com.modelradar.crawler" >/dev/null 2>&1; then
  echo "  ✓ 每日自动抓取已装载（launchctl print 实测）"
else
  echo "  ⚠️ 每日自动抓取此刻未装载：下次登录会自动生效；要现在生效就在自己的终端里跑"
  echo "     launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.modelradar.crawler.plist"
fi
U="$DEST/src/widget/uninstall_chart_widget.sh"
[ -f "$U" ] || U="$DEST/src/uninstall_chart_widget.sh"
echo "卸载：bash $U --dry  先看清单，再去掉 --dry 执行"
[ -t 0 ] && read -r -p "按回车关闭…" _
exit "$rc"
CMD
chmod +x "$STAGE/install.command"

cat > "$STAGE/INSTALL.md" <<MD
# ModelRadar $VER

**装**：双击 \`install.command\`。若 macOS 拦一道 Gatekeeper（本项目是 ad-hoc 签名，没有
开发者证书也没有公证），右键点文件 →「打开」→ 在弹窗里再点一次「打开」；命令行等价：
挂载 dmg 后 \`xattr -dr com.apple.quarantine /Volumes/ModelRadar\`（zip 则指向你解压出来的那个目录）。
注意别写成本打包时的目录名 —— 那个名字在 dmg 里并不存在。

落点：\`~/Applications/ModelRadar.app\`（组件本体）与 \`~/Applications/ModelRadar/src\`（源码），
状态与台账在 \`~/Library/Application Support/ModelRadar/\`，日志在 \`~/Library/Logs/ModelRadar/\`。
不要管理员密码，不写 /Applications，不装任何系统组件。

**要求**：macOS 14+；python3 ≥ 3.9（\`xcode-select --install\` 就有）。运行时
**零第三方 Python 依赖** —— HTML 解析走标准库（\`html_tables.py\`），
所以不需要 pip 装 lxml。
**架构**：本包内预编译二进制是 \`$ARCH\`，只有 Apple 芯片能直接跑；Intel 机器请拿源码后
\`bash src/widget/build_chart_widget.sh\`（会调用 swiftc 本机编译）。

**它做什么**：每天 07:30 抓 Artificial Analysis 榜单 + OpenRouter 实时价 + DataLearner 交叉验证，
算成本×智能指数的 Pareto 前沿，画成状态栏里的一张定位图；数据更新时弹通知。
三个源里两个是解析前端页面（AA 与 DataLearner 没有公开 API），抓取失败时组件会
在「数据状态…」里如实标黄/标红，并沿用上一次快照标注为旧数据 —— 不会把缺失当成 0。

**卸载**：\`bash ~/Applications/ModelRadar/src/widget/uninstall_chart_widget.sh --dry\` 先看清单，
再去掉 \`--dry\` 执行（一律进废纸篓；默认保留 SQLite 台账，那是唯一不可再生的东西）。

**自证装对了**：\`cd ~/Applications/ModelRadar/src && python3 fetch_model_data.py\`，
然后点菜单栏图标 ⋯ → 数据状态… 看三个源是否都到数。
（仓库里还有一个 \`verify_dashboard.mjs\` 做渲染回归，**需要 node**；没有装 node 就跳过这条，
它不是安装的必要条件。）
MD

# ---------------------------------------------------------------- 出门前的隐私闸
# 扫的是**真正进包的东西**：staging 里的每个文件，文本按行扫、其余按 strings 扫
# （早先只 grep 文本 + 只 strings 了主二进制，结果 .pyc / Resources / 出包时才生成的
#   AppleDouble 全在视野外）。
# 名单从 tools/privacy_traces.txt 读（或 MR_TRACES_FILE 指定）。名单本身不进仓库：
# 它的内容就是"不许出现在包里的那些字样"，入库等于让扫描器把自己扫成命中。
TRACES_FILE="${MR_TRACES_FILE:-$HERE/tools/privacy_traces.txt}"
if [ -f "$TRACES_FILE" ]; then
  TRACES="$(grep -vE '^[[:space:]]*(#|$)' "$TRACES_FILE" | paste -sd'|' -)"
  WHO="$(whoami)"
  # cd 进去扫：find/strings 打出的 "文件名:" 表头会带 $STAGE 的绝对路径，
  # 在仓库里直接扫等于自己造出"家目录绝对路径"形状的假阳性。
  scan_stage() {  # $1 = 命中模式；文本按行、其余按 strings 扫 staging
    ( cd "$STAGE" && { grep -rnI -i -E "$1" . 2>/dev/null
                       find . -type f -print0 | xargs -0 strings -a 2>/dev/null | grep -i -E "$1"
                     } )
  }
  # 两路**分开**扫：$HOME/用户名没有"合法出现"的场景，不过任何白名单。工具链名那一路
  # 要豁免"模型名里带 claude 的"，但豁免必须逐行拆词：原先把过滤链挂在合并管道的一行
  # 上，同一行既有合法模型名又有真泄漏时整行被静默吞掉、闸报绿。
  drop_model_names() {   # stdin = 命中行；把合法模型名样式挖掉后仍含禁词的才留下
    local line stripped
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      stripped="$(printf '%s' "$line" | tr '[:upper:]' '[:lower:]' \
                  | sed -E 's/claude [a-z]+ [0-9]+|claude-[a-z]+-[0-9]+//g')"
      printf '%s' "$stripped" | grep -qi -E "$TRACES" && printf '%s\n' "$line"
    done
    return 0
  }
  LEAKS="$( { scan_stage "$HOME|$WHO"
              scan_stage "$TRACES" | drop_model_names
            } | head -12 )"
  if [ -n "$LEAKS" ]; then
    echo "  ✗ 进包内容里有隐私痕迹（本机路径/用户名/工具链名）："
    printf '%s\n' "$LEAKS" | sed 's/^/      /'
    echo "    改掉来源文件或把它加进 .gitignore 后重打包 —— 不要分发这一份"
    exit 1
  fi
  echo "  ✓ 隐私闸：进包内容（含二进制 strings）不含本机路径/用户名/工具链名"
elif [ "${MR_ALLOW_UNSCANNED:-}" = "1" ]; then
  echo "  ⚠️ 缺 ${TRACES_FILE}，按 MR_ALLOW_UNSCANNED=1 放行：这一份**未经隐私扫描**"
else
  # 原先这里只打一行 ⚠️ 就继续出包 —— 于是"零隐私扫描"的包可以一路静默出门。
  # 缺判据就当失败：宁可不出包，也不发一份没扫过的东西。
  echo "  ✗ 找不到隐私扫描名单 ${TRACES_FILE}，无法扫描。"
  echo "    用 MR_TRACES_FILE=<名单路径> 指定；确认不需要扫描再显式加 MR_ALLOW_UNSCANNED=1。"
  exit 1
fi

# ---------------------------------------------------------------- 产出
# 缺必需文件就拒绝出包。以前只在 EXIT trap 里打一行"不要分发"，
# 结果是 dmg/zip 照样生成、名字看起来正常 —— 靠人记得读末行才不上当。
if [ "$MISSING" = 1 ]; then
  echo "  ✗ 有必需文件没进包，已拒绝出包（先 git add 缺的东西再重跑）" >&2
  exit 1
fi
if [ "$MAKE_DMG" = 1 ]; then
  rm -f "$HERE/dist/$NAME.dmg"
  hdiutil create -volname "ModelRadar" -srcfolder "$STAGE" -ov -format UDZO \
    "$HERE/dist/$NAME.dmg" >/dev/null && echo "  ✓ dmg：dist/$NAME.dmg"
fi
rm -f "$HERE/dist/$NAME.zip"
# --norsrc：不加就会把 com.apple.provenance 之类扩展属性写成 40 个 AppleDouble(`._*`) 条目，
# 收件人 unzip 之后 `codesign --verify --strict` 直接报 "a sealed resource is missing or
# invalid"、spctl 判 rejected，双击就是"已损坏"。dmg 走 HFS 存 xattr，不受影响。
(cd "$HERE/dist" && ditto -c -k --keepParent --norsrc "$NAME" "$NAME.zip") && echo "  ✓ zip：dist/$NAME.zip"
du -sh "$HERE/dist/$NAME.dmg" "$HERE/dist/$NAME.zip" 2>/dev/null
# 出包判据：验的是**解压出来的那份**，不是 staging。只验 staging 会对 zip 破坏签名失明。
ZCHK="$(mktemp -d)"
if (cd "$ZCHK" && unzip -q "$HERE/dist/$NAME.zip" \
      && [ "$(find . -name '._*' | wc -l | tr -d ' ')" = "0" ] \
      && codesign --verify --strict "$NAME/ModelRadar.app" ); then
  echo "  ✓ zip 复核：无 AppleDouble 条目，解压后签名完整"
else
  echo "  ✗ zip 复核失败：解压后签名不完整（多半是扩展属性/AppleDouble），不要分发"
  rm -rf "$ZCHK"; exit 1
fi
rm -rf "$ZCHK"
codesign -dv "$STAGE/ModelRadar.app" 2>/dev/null | grep -E "^Identifier|flags" | sed 's/^/  签名: /'
echo "注意：ad-hoc 签名 + 未公证 → 收件人首次必须右键「打开」一次。"
