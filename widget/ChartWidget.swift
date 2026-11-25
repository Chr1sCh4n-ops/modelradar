//  ModelRadar 状态栏组件 —— 只做一件事：把「速度与成本性价比定位图」常驻菜单栏。
//
//  设计取舍：
//  · 纯 AppKit + WKWebView。坐标、配色、Pareto 前沿全在 render_scatter_svg.py 里算，
//    这里只把产出的自包含 HTML 塞进 webview。判据单点定义，不在这里重算一遍——
//    同一套逻辑散在两处，改一处忘另一处就是静默翻车。
//  · 路径全部问 ~/Library/Application Support/ModelRadar/runtime.env 要（见下方 ChartPaths），
//    组件自己不认识仓库在哪，代码目录挪了重跑一次 build 即可。
//  · 心跳只看 chart.html 的 mtime。爬虫改的是 latest.json，mtime 变了就重画，
//    既不必解析 JSON，也不用知道爬虫存在。

import AppKit
import WebKit
import OSLog
import UserNotifications
import Darwin

/// 任务耗时大小模式的唯一真源。与顶栏「▤ 卡片」同一套 UserDefaults 设计：
/// 缺省 false = 速度模式 —— 没有偏好记录时不猜用户想要任务模式。
enum SizeTaskPref {
    static let key = "sizeTaskOn"

    static func isOn(in defaults: UserDefaults = .standard) -> Bool {
        defaults.object(forKey: key) as? Bool ?? false
    }

    static func set(_ on: Bool, in defaults: UserDefaults = .standard) {
        defaults.set(on, forKey: key)
    }

    /// documentStart 注入的初值；页面只认 1/0，字符串形式由 SIZE_JS 兼容。
    static func script(on: Bool) -> String {
        "window.MR_SIZE_TASK=\(on ? 1 : 0);"
    }
}

/// 任务耗时模式下的「全局低分惩罚」开关。与 SizeTaskPref 分开存：
/// 两者独立召回 —— 用户关掉任务模式再打开，低分惩罚仍是他上次的选择。
/// 缺省 false："分数低就缩小"是额外的解释性选择，不替用户默认打开。
enum SizePenaltyPref {
    static let key = "sizePenaltyOn"

    static func isOn(in defaults: UserDefaults = .standard) -> Bool {
        defaults.object(forKey: key) as? Bool ?? false
    }

    static func set(_ on: Bool, in defaults: UserDefaults = .standard) {
        defaults.set(on, forKey: key)
    }

    static func script(on: Bool) -> String {
        "window.MR_SIZE_PENALTY=\(on ? 1 : 0);"
    }
}

// MARK: - 路径
//
// 组件不硬编码任何仓库/解释器位置，全部问 build 期生成的 runtime.env 要：
//
//   ~/Library/Application Support/ModelRadar/runtime.env
//       MR_REPO=<源码仓库绝对路径>
//       MR_DATA=<运行时数据目录绝对路径>
//
// 代码目录挪了、venv 换了、甚至整个仓库重新 clone 到别处，都只要重跑一次
// build_chart_widget.sh，组件不用改一行。
// 早先的做法是在 Application Support 下给 chart.html / latest.json / dashboard.html
// 各建一条软链指回仓库 —— 那等于把「仓库在哪」这个易变事实固化成三条链：仓库一挪就
// 变断链，而断链的表现（读不到文件）和真故障长得一模一样，分不清也不好修。
enum ChartPaths {
    static let supportDir = (FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)
        .first ?? URL(fileURLWithPath: NSHomeDirectory()))
        .appendingPathComponent("ModelRadar").path

    static let runtimeEnvFile = supportDir + "/runtime.env"

    /// KEY=VALUE 逐行解析；跳过空行与 `#` 注释。值两侧的同种引号会被剥掉：
    /// build 期**必须**写带引号的形式（Application Support 路径含空格，bash 侧
    /// `. "$ENV_FILE"` 不加引号就会把路径截断、把后半截当命令执行），
    /// 于是 Swift 侧得负责把引号剥回来。
    private static func parseEnv(_ path: String) -> [String: String] {
        guard let raw = try? String(contentsOfFile: path, encoding: .utf8) else { return [:] }
        var out: [String: String] = [:]
        for line in raw.split(whereSeparator: \.isNewline) {
            let s = line.trimmingCharacters(in: .whitespaces)
            if s.isEmpty || s.hasPrefix("#") { continue }
            guard let eq = s.firstIndex(of: "=") else { continue }
            let key = String(s[s.startIndex..<eq]).trimmingCharacters(in: .whitespaces)
            var val = String(s[s.index(after: eq)...]).trimmingCharacters(in: .whitespaces)
            if val.count >= 2, let f = val.first, (f == "\"" || f == "'"), val.last == f {
                val = String(val.dropFirst().dropLast())
            }
            if !key.isEmpty && !val.isEmpty { out[key] = val }
        }
        return out
    }

    private static let env = parseEnv(runtimeEnvFile)

    /// runtime.env 缺失时退到 supportDir/data —— 那条路径不存在，于是走既有的
    /// 「找不到图表数据」提示，而不是让一堆 Optional 传染到每个调用点。
    static let repo = env["MR_REPO"]  ?? supportDir
    static let data = env["MR_DATA"]  ?? (supportDir + "/data")

    static let chartHTML  = data + "/chart.html"
    static let latestJSON = data + "/latest.json"
    static let dashboard  = repo + "/dashboard.html"

    /// 动作脚本由 build 期生成在中枢目录下，位置固定，不经 runtime.env。
    static let regenerate = supportDir + "/bin/regenerate.sh"
    static let crawl      = supportDir + "/bin/crawl.sh"

    /// 中枢通知出口：**从 runtime.env 读，不写死**。这份仓库要公开，而中枢在开发者个人
    /// 机器的目录里；留空时 legacy 通道直接走系统横幅，功能不坏，只是少了审计落档与点击跳转。
    static let hubNotify = env["MR_HUB_NOTIFY"] ?? ""

    static func exists(_ p: String) -> Bool { FileManager.default.fileExists(atPath: p) }
}

/// 处理中遮罩：自绘一层随系统外观切换的纱。
/// 为什么自绘而不是设 layer.backgroundColor —— 后者存的是**解析后的 CGColor**，
/// 系统切明暗时不会重新解析，得自己挂外观变化钩子；而 draw(_:) 里的动态 NSColor
/// 每次绘制都按当前 effectiveAppearance 取值，天然跟随，少一层状态。
/// 遮罩层。**刻意用自绘视图而不是 NSBox**（理由见 ChartView.overlay 的注释）。
/// 非 private：verify_crawl_notice_ui.swift 要直接验证"不铺纱时命中测试穿透给下层图"。
final class ScrimView: NSView {
    /// notice 提示不铺纱、也不吃鼠标：rc=2 时保留的旧图必须还能悬停/点选。
    /// 只有真正的失败（脚本崩溃/超时/图缺失）才允许用整图遮罩。
    var dimsBackground = true {
        didSet { needsDisplay = true }
    }

    override func draw(_ dirtyRect: NSRect) {
        guard dimsBackground else { return }
        let isDark = effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
        (isDark ? NSColor.black : NSColor.white).withAlphaComponent(0.62).setFill()
        dirtyRect.fill()
    }

    /// 透明态直接把命中测试交还给下层 webview，否则一张"不铺纱"的空视图
    /// 仍然会吃掉点击/悬停，图看着在、实际点不动。
    override func hitTest(_ point: NSPoint) -> NSView? {
        dimsBackground ? super.hitTest(point) : nil
    }
}

/// 角标红点：非模板视图，不会被系统按菜单栏明暗 tint 成灰色；盖在状态按钮右上角。
private final class BadgeDotView: NSView {
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.systemRed.setFill()
        NSBezierPath(ovalIn: bounds.insetBy(dx: 0.5, dy: 0.5)).fill()
        NSColor.white.withAlphaComponent(0.85).setStroke()
        let ring = NSBezierPath(ovalIn: bounds.insetBy(dx: 0.5, dy: 0.5))
        ring.lineWidth = 1
        ring.stroke()
    }
}

// MARK: - 应用委托

/// 注意：**这里不能写 `@main`**。delegate 由 main.swift 用全局常量持有，
/// 详见 main.swift 顶部注释——挂在 `@main` 上是"进程在跑但回调永不触发"的根因。
final class AppDelegate: NSObject, NSApplicationDelegate {

    private var statusItem: NSStatusItem?
    private var popover: NSPopover?
    private let chart = ChartView()
    private var pollTimer: Timer?
    private var knownMtime: Date?
    private var selfCheckWindow: NSWindow?
    // 新数据更新提示：上一次已提示过的内容指纹（content_hash），避免同内容重抓重复弹通知
    private var lastNotifiedKey: String?
    private var badgeActive = false
    private var badgeDot: BadgeDotView?
    private var badgeDeadline: Date?
    /// 本实例启动时磁盘上二进制的 mtime：tick 心跳用来判断"是否已重编译出新版本"，
    /// 是的话在菜单里提示可重启（**只提示，不自动换**——运行时自我替换会让
    /// "我看到的 GUI 是哪一版"变成不可复现的问题，排查时尤其致命）。
    private var launchedBinaryMtime: Date?
    private var updateAvailable = false
    /// 启动时同 bundle 的其它实例 pid（只用于提示，不终止它们）
    private var otherInstancePids: [Int] = []
    private static let baseToolTip = "ModelRadar · 模型性价比定位图"
    private static let unreadToolTip = "ModelRadar · 有未读（点开查看）"
    private static let updateToolTip = "ModelRadar · 有新版本，点 ⋯ → 重启以应用"
    private static let pidFile = ChartPaths.supportDir + "/.widget.pid"
    /// 角标（未读红点）状态持久化文件：自重启 / 启动期换掉旧实例会清掉内存中的角标，
    /// 若不落盘，用户恰在重启前没看到红点就会永久静默（独立复核指出的真问题）。
    private static let badgeStateFile = ChartPaths.supportDir + "/.badge_state.json"

    /// 正在跑的脚本任务。**状态放在这里而不是 ChartView 里**，是因为 popover 是
    /// .transient 的，用户点别处就自己关了；进度若只活在遮罩上，一关就全丢，
    /// 重开看到的是"按钮灰着、没进度、图还是旧的"，等于告诉用户界面坏了。
    private struct TaskState {
        let text: String       // "正在抓取最新数据…"
        let started: Date
        let maxSec: Int
    }
    private var task: TaskState?
    /// 任务在 popover 关着的时候跑完了：结果先攒着，下次打开时补显，不至于彻底丢失。
    private var pendingResult: (text: String, isError: Bool, blocking: Bool)?
    /// 上一次 popover 关闭时刻：用于吃掉"同一次物理点击既触发自动关闭、又触发 toggle"
    /// 造成的立刻重开 —— 那是"点了关不掉、闪一下又开"的真因。
    private var lastPopoverCloseAt: Date?
    /// 上一次量到的页面内容高度（不含顶栏）。下次打开先按它设尺寸，
    /// 免得每次都"先按 600 弹出、量完再缩"看得见地跳一下。
    private var lastContentHeight: CGFloat?


    // MARK: 自检

    /// 离屏渲染一份 popover 内容并存成 PNG。
    /// 窗口必须真的 orderFront 进窗口系统，webview 才肯渲染；所以把它挪到屏幕外
    /// 坐标（-4000,-4000），既不弹到用户脸上，又不是"没上屏所以画不出来"。
    private func runSelfCheck(output: String) {
        Self.scLog("自检模式启动 -> \(output)")
        Self.log("自检模式启动 -> \(output)")
        let size = NSSize(width: 748, height: 600)
        let w = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                         styleMask: [.borderless], backing: .buffered, defer: false)
        w.contentViewController = chart
        w.setFrameOrigin(NSPoint(x: -4000, y: -4000))
        w.orderFront(nil)
        selfCheckWindow = w
        Self.scLog("离屏窗口已 orderFront，frame=\(w.frame)")

        // 兜底：didFinish 万一不来（比如 baseURL 解析卡住），不能就这么挂着
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) {
            Self.scLog("超时兜底触发：didFinish 未回调")
            NSApp.terminate(nil)
        }

        chart.onDidLoad = { [weak self] in
            Self.scLog("didFinish 回调到达")
            guard let self else { return }
            // 再等一拍：didFinish 之后 SVG 的首帧合成未必已经落到图层上
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                Self.scLog("开始 takeSnapshot，web.bounds 已知")
                self.chart.snapshot(to: output) { ok in
                    Self.scLog("takeSnapshot 完成，ok=\(ok)")
                    Self.log("自检导出 \(ok ? "成功" : "失败") -> \(output)")
                    NSApp.terminate(nil)
                }
            }
        }
        Self.scLog("准备 load chart.html")
        chart.load()
        Self.scLog("load 已发起")
    }


    func applicationDidFinishLaunching(_ notification: Notification) {
        // 第一行就落盘：没有这条基准日志就分不清是"没进来"还是"中途抛异常"。
        Self.log("applicationDidFinishLaunching 进入")
        // 把命令行参数也记下来：`--selfcheck` 那类开关"看着传了其实没进来"时，
        // 只有这行日志能一眼看出是参数没到还是分支没走。
        Self.log("启动参数：\(CommandLine.arguments.joined(separator: " "))")
        NSApp.setActivationPolicy(.accessory)
        // 两条启动分支（自检 / 正常）都要量高度，所以挂在分叉之前；
        // 挂晚了就收不到首次 didFinish 的回调。
        chart.onMeasured = { [weak self] h in self?.applyContentHeight(h) }

        // 自检模式（`--selfcheck <out.png>` 或 MR_SELFCHECK=<out.png>）：把 popover 内容
        // 离屏渲染成 PNG。为什么要这么绕：受限会话里合成点击打不开 status item ——
        // .accessory 的 app 激活不了（activate 前后 isActive 都是 false），而 NSPopover
        // 要求宿主 app active 才肯显示。所以让 app 自己渲染自己，走的是和真实 popover
        // 完全同一套 ChartView + WKWebView + 同一个 chart.html，证据比"我去点它一下"更硬。
        // 参数优先走命令行：LSEnvironment 要重新登录才被 LaunchServices 认，实测传不进去。
        if let out = Self.selfCheckOutput() {
            // 这条 scLog 本身就是判据：日志文件若压根没建出来，说明这个回调没进来
            Self.scLog("applicationDidFinishLaunching 进入（自检）")
            runSelfCheck(output: out)
            return
        }

        // 不自动杀旧实例、不自动换版：重编译生效是 build 脚本的显式动作（它负责 quit + pkill + open）。
        // 这里只把"同时跑着好几个实例"这个事实记下来并在 ⋯ 菜单里说清楚 ——
        // 自我替换会让"我看到的界面是哪一版"不可复现，排查时反而更贵。
        warnAboutOtherInstances()
        installStatusItem()
        Self.writePidFile()                       // 让 Python 兜底通知用 pid 判活（避免 pgrep 名字错配双弹）
        restoreBadgeState()                       // 在 installStatusItem 之后：badgeDot 已建好才能点亮（自重启不丢未读）
        Self.requestNotificationAuthorization()   // UN 授权（标准 .app bundle 才走，裸执行会退回 NS）
        chart.load()
        knownMtime = Self.mtimeOfChart()
        startPolling()
        seedNotified()
        notifyIfNewData()
        // 记录启动时二进制 mtime：tick 心跳据此判断是否需要自重启加载新版本。
        launchedBinaryMtime = Self.binaryMtime()

        // 分两种缺法说清楚：没配置（runtime.env 缺）和没数据（chart.html 缺）。
        // 原先只报"找不到图表数据"，而 runtime.env 缺失时路径会退到 supportDir/data，
        // 用户/排查者照着那句话去查数据，其实该跑的是 build。
        if !ChartPaths.exists(ChartPaths.runtimeEnvFile) {
            chart.showMessage("未找到配置 runtime.env\n请先运行 widget/build_chart_widget.sh")
            Self.log("runtime.env 不存在：\(ChartPaths.runtimeEnvFile)")
        } else if !ChartPaths.exists(ChartPaths.chartHTML) {
            chart.showMessage("找不到图表数据\n\(ChartPaths.chartHTML)\n点「⟳ 重新生成」即可画出")
            Self.log("chart.html 不存在：\(ChartPaths.chartHTML)（数据目录=\(ChartPaths.data)）")
        }
        Self.log("启动完成 · chart.html mtime=\(Self.mtimeOfChart()?.timeIntervalSince1970.description ?? "nil")")
    }

    func applicationWillTerminate(_ notification: Notification) {
        pollTimer?.invalidate()
        // 只删"自己写的"pid 文件：自重启时旧实例退出，若无条件删会误删新实例刚写的 pid
        // （独立复核指出：pid 文件是共享固定路径，删除无归属校验）。
        if let cur = try? String(contentsOfFile: Self.pidFile, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines),
           let filePid = Int(cur),
           filePid == ProcessInfo.processInfo.processIdentifier {
            try? FileManager.default.removeItem(atPath: Self.pidFile)
        }
    }

    // MARK: 状态栏

    private func installStatusItem() {
        // 用 squareLength 常量而不是魔数：菜单栏高度变过好几次，
        // 写死 26 会在系统改高度后行为不可控（26 恰好是旧版 squareLength 的取值）。
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        let img = Self.makeIcon()
        item.button?.image = img
        item.button?.toolTip = Self.baseToolTip
        item.button?.action = #selector(toggle(_:))
        item.button?.target = self
        statusItem = item
        // 真实红点角标：底图保持模板图（自动 tint），红点是独立非模板子视图，不受 tint 影响。
        if let btn = item.button {
            let dot = BadgeDotView()
            dot.translatesAutoresizingMaskIntoConstraints = false
            dot.isHidden = true
            btn.addSubview(dot)
            NSLayoutConstraint.activate([
                dot.trailingAnchor.constraint(equalTo: btn.trailingAnchor, constant: -1),
                dot.topAnchor.constraint(equalTo: btn.topAnchor, constant: 3),
                dot.widthAnchor.constraint(equalToConstant: 7),
                dot.heightAnchor.constraint(equalToConstant: 7),
            ])
            badgeDot = dot
        }
        // 把关键状态落盘：图标"装上了但看不见"这种情况，只有把 isVisible /
        // 图像尺寸 / 是否有按钮记下来才分得清，靠肉眼看截图会误判成"没装"。
        Self.log("状态栏项已创建 isVisible=\(item.isVisible) 有按钮=\(item.button != nil) "
                 + "图像=\(img.size.width)x\(img.size.height) 模板=\(img.isTemplate)")
    }

    /// 模板图：系统按菜单栏明暗自动 tint，不用管深色模式。
    /// 优先用系统符号（alpha 由系统保证）；画不出来才自绘兜底。
    private static func makeIcon() -> NSImage {
        // 首选 SF Symbol：模板属性由系统给，不会出现 alpha 不实的问题
        if let sym = NSImage(systemSymbolName: "chart.dots.scatter",
                             accessibilityDescription: "ModelRadar") {
            let cfg = NSImage.SymbolConfiguration(pointSize: 15, weight: .medium)
            let s = sym.withSymbolConfiguration(cfg) ?? sym
            return rasterized(s)
        }

        // 兜底自绘。⚠️ 必须用 .black 而不是 .labelColor：
        // labelColor 是动态色，取 cgColor 出来后 alpha 只有 ~216/255（实测），
        // 图标会整体发虚。模板图只认 alpha 通道，颜色会被系统按菜单栏明暗重刷，
        // 所以填黑不会得到黑图标。
        let img = NSImage(size: NSSize(width: 22, height: 22), flipped: true) { _ in
            let ctx = NSGraphicsContext.current!.cgContext
            ctx.setFillColor(NSColor.black.cgColor)
            let dots: [(CGFloat, CGFloat, CGFloat)] = [
                (3.0, 16.5, 2.3), (8.0, 18.0, 1.8), (13.0, 6.0, 2.6),
                (17.5, 12.0, 2.0), (11.0, 13.5, 3.0), (5.5, 9.0, 1.6),
            ]
            for (x, y, r) in dots {
                ctx.beginPath()
                ctx.addArc(center: NSPoint(x: x, y: y), radius: r,
                           startAngle: 0, endAngle: .pi * 2, clockwise: false)
                ctx.fillPath()
            }
            return true
        }
        img.isTemplate = true
        img.size = NSSize(width: 18, height: 18)
        return img
    }

    /// 按 1x / 2x 各栅格化一份，让系统在 1x 屏上直接取 1x 光栅。
    /// SF Symbol 的 NSImage 内部只有一份 2x 光栅（`cgImage` 实测 42x34，`representations`
    /// 报 0x0），1x 屏必须现场降采样；`chart.dots.scatter` 的横轴只有约 1pt 粗，
    /// 降采样后 alpha 从 255 掉到约 13%（实拍：竖轴 delta 0.507 / 横轴 0.115），
    /// 在非活跃菜单栏里就看着像「那一横没了」。自己给 1x 光栅后横轴保持满 alpha。
    private static func rasterized(_ src: NSImage) -> NSImage {
        let size = src.size
        let img = NSImage(size: size)
        for scale in [1, 2] {
            let w = Int((size.width * CGFloat(scale)).rounded())
            let h = Int((size.height * CGFloat(scale)).rounded())
            guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil,
                                             pixelsWide: w, pixelsHigh: h,
                                             bitsPerSample: 8, samplesPerPixel: 4,
                                             hasAlpha: true, isPlanar: false,
                                             colorSpaceName: .deviceRGB,
                                             bytesPerRow: 0, bitsPerPixel: 0) else { continue }
            rep.size = size
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
            src.draw(in: NSRect(origin: .zero, size: size))
            NSGraphicsContext.restoreGraphicsState()
            img.addRepresentation(rep)
        }
        img.isTemplate = true
        return img
    }

    /// popover 只建一次并长期复用。每次点击新建 NSPopover、却把同一个
    /// NSViewController 塞进去，等于让两个 popover 争同一个 view；
    /// 快速连点时表现为"闪一下又没了"。
    private func ensurePopover() -> NSPopover {
        if let pop = popover { return pop }
        let pop = NSPopover()
        pop.contentViewController = chart
        pop.contentSize = fittedSize()
        pop.behavior = .transient
        pop.animates = false
        pop.delegate = self
        popover = pop
        return pop
    }

    /// 贴合内容的大小：没量过之前退回 600（首屏宁可略高也别裁图）。
    private func fittedSize() -> NSSize {
        NSSize(width: ChartView.popWidth,
               height: (lastContentHeight.map { ChartView.barHeight + $0 }) ?? 600)
    }

    private func applyContentHeight(_ h: CGFloat) {
        lastContentHeight = h
        guard let pop = popover else {
            // 自检模式 / popover 还没建过：把量到的数记下来，这样不点击也能验证贴合算得对不对
            Self.log("量得内容高度 \(Int(h))pt（当前无 popover，下次打开按 \(Int(ChartView.barHeight + h))pt 开）")
            return
        }
        let total = ChartView.barHeight + h
        guard abs(pop.contentSize.height - total) > 1 else { return }
        pop.contentSize = NSSize(width: ChartView.popWidth, height: total)
        Self.log("popover 贴合内容高度：\(Int(total))pt（顶栏 \(Int(ChartView.barHeight)) + 内容 \(Int(h))）")
    }

    /// 顶栏「▤ 卡片」开关：控制悬停时弹不弹卡片（节点卡与聚合卡一起）。
    /// 默认开；关掉状态记在 UserDefaults，重启组件后还是关着的 ——
    /// 每天盯图的人一旦嫌吵，不该第二天又被弹回来。
    static let cardsKey = "cardsOn"
    var cardsOn: Bool {
        get { UserDefaults.standard.object(forKey: Self.cardsKey) as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: Self.cardsKey) }
    }

    @objc func toggleCards(_ sender: NSButton) {
        cardsOn = sender.state == .on
        Self.log("卡片开关 → \(cardsOn)")
        chart.setCards(cardsOn)
    }

    @objc private func toggle(_ sender: AnyObject?) {
        Self.log("toggle 点击 popoverShown=\(popover?.isShown ?? false)")
        guard let btn = statusItem?.button else { return }
        if let pop = popover, pop.isShown {
            pop.close()
            return
        }
        // ⚠️ .transient 的自动关闭与按钮 action 源自同一次物理点击：系统先把它关掉、
        // 再走到这里，若立刻重开就变成"点了关不掉、闪一下又开"。0.35s 内的连点直接忽略。
        if let t = lastPopoverCloseAt, Date().timeIntervalSince(t) < 0.35 {
            Self.log("忽略连点（刚被 transient 关闭，间隔 \(String(format: "%.2f", Date().timeIntervalSince(t)))s）")
            return
        }
        showPopover(source: btn)
        clearBadge()   // 点开即视为已读，消掉角标
    }

    private func showPopover(source: NSStatusBarButton) {
        Self.log("showPopover 进入")
        let pop = ensurePopover()
        // .accessory 的 app 平时不是 active，而 NSPopover 在非活跃宿主上极易被外部事件
        // 顺手关掉（这正是"遮罩一闪就没"的一半原因）。先激活再弹，进度才留得住。
        NSApp.activate(ignoringOtherApps: true)
        pop.show(relativeTo: source.bounds, of: source, preferredEdge: .minY)
        // 打开时才拉数据：爬虫可能在 popover 关着的那阵子跑过了，
        // 而心跳只负责"检测到变了才重画"，不会主动刷新已经打开的内容。
        chart.load()
        chart.focusPage()
        restoreTaskUI()
    }

    /// 把 AppDelegate 持有的任务状态投影到刚打开的视图上：
    /// 跑着就恢复倒计时遮罩并禁按钮；跑完了有攒着的结果就补显；都没有就放行。
    private func restoreTaskUI() {
        if let t = task {
            chart.setControlsEnabled(false)
            chart.resumeProcessing(mainText: t.text, expectedMaxSec: t.maxSec, startedAt: t.started)
            return
        }
        chart.setControlsEnabled(true)
        if let r = pendingResult {
            pendingResult = nil
            chart.showResult(r.text, isError: r.isError, blocking: r.blocking)
        }
    }


    // MARK: 心跳

    private static func mtimeOfChart() -> Date? {
        // 仍然先解软链再取 mtime：chart.html 现在是 MR_DATA 下的真文件，但 MR_DATA 本身
        // 可能被用户指向别处（旧部署就是这么干的）。FileManager.attributesOfItem 走 lstat，
        // 直接读软链自身的 mtime 会永远"没变"——组件既不重画也不弹通知，是最难查的一种哑火。
        let resolved = URL(fileURLWithPath: ChartPaths.chartHTML).resolvingSymlinksInPath().path
        guard let v = try? FileManager.default.attributesOfItem(atPath: resolved)[.modificationDate],
              let d = v as? Date else { return nil }
        return d
    }

    private func startPolling() {
        // 60 秒一轮，只负责"检测到 chart.html 变了就重画"，并不自己抓数据。
        // 真正的数据刷新由每日爬虫（launchd com.modelradar.crawler）完成；
        // 这里检测到 mtime 变化后会顺带跑一次 notifyIfNewData 弹新数据通知。
        // 用非 scheduled 的 Timer 再显式加到 .common 模式——scheduledTimer 已经把自己
        // 加进默认 runloop，再 add 一次是重复（虽无害但误导）。
        let t = Timer(timeInterval: 60, repeats: true) { [weak self] _ in
            self?.tick()
        }
        RunLoop.current.add(t, forMode: .common)
        pollTimer = t
    }

    private static func stampOfMtime(_ d: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f.string(from: d)
    }

    private func tick() {
        if let d = badgeDeadline, Date() >= d { clearBadge() }   // 15 分钟兜底过期，由心跳裁决
        detectBinaryUpdate()
        // ⚠️ 存在性判断只圈住"重画"这一段。原先写成函数级 guard，chart.html 一旦缺失
        // （渲染失败、仓库挪走）就连带跳过了 notifyIfNewData —— 而通知走的是 latest.json，
        // 于是"图没画出来"会顺带把"有新数据"的提示也吞掉，两个故障叠成一个更难查的。
        if ChartPaths.exists(ChartPaths.chartHTML) {
            if let now = Self.mtimeOfChart() {
                if let before = knownMtime, Int(now.timeIntervalSince1970) != Int(before.timeIntervalSince1970) {
                    knownMtime = now
                    Self.log("chart.html 已更新 -> 重画（\(Self.stampOfMtime(now))）")
                    // 关着的时候不重画：下次 showPopover 本来就会 load 最新内容
                    if popover?.isShown == true && task == nil { chart.load() }
                } else {
                    knownMtime = now
                }
            }
        }
        // 内容指纹变化即视为新数据：即便 chart.html 因渲染失败没更新，也能提示（不再漏报）。
        notifyIfNewData()
    }

    /// 发现磁盘二进制比本实例启动时新（差值 >2s，避开时钟粒度误差）→ 只标记可重启，不自动换版。
    private func detectBinaryUpdate() {
        guard !updateAvailable, let launched = launchedBinaryMtime,
              let cur = Self.binaryMtime(), cur.timeIntervalSince(launched) > 2 else { return }
        updateAvailable = true
        Self.log("检测到新二进制（启动 mtime=\(Int(launched.timeIntervalSince1970))，"
                 + "磁盘=\(Int(cur.timeIntervalSince1970))）→ ⋯ 菜单提示可重启；不自动换版")
        refreshToolTip()
        // 任务在跑时别用结果提示盖掉倒计时
        if task == nil, popover?.isShown == true {
            chart.showResult("发现新版本 · 点右上角「⋯」→「重启以应用新版本」", isError: false)
        }
    }


    // MARK: 新数据更新提示

    /// 启动时不把"已存在的数据"当成"新数据"来提示：以当前/上次已提示的 generated_at 为基线。
    private func seedNotified() {
        let path = AppDelegate.notifiedStampFile
        var saved: String?
        if let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
           let raw = String(data: data, encoding: .utf8) {
            let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            saved = trimmed.isEmpty ? nil : trimmed
        }
        if let s = saved {
            lastNotifiedKey = s
        } else if let k = Self.contentKey(of: ChartPaths.latestJSON) {
            lastNotifiedKey = k
        }
    }

    /// 读 latest.json 里的 generated_at（数据生成的北京时间）。
    private static func generatedAt(of path: String) -> String? {
        guard ChartPaths.exists(path),
              let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let at = obj["generated_at"] as? String else { return nil }
        return at
    }

    /// 读 latest.json 的内容指纹（无则退回 generated_at 兼容旧数据）。
    private static func contentKey(of path: String) -> String? {
        guard ChartPaths.exists(path),
              let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let k = (obj["content_hash"] as? String) ?? (obj["generated_at"] as? String),
              !k.isEmpty else { return nil }
        return k
    }

    /// 载荷的"龄"（小时）。`generated_at` 是北京时间字符串，按 +08:00 解析；读不出来返回 nil
    /// （宁可不判，也不要拿"解析失败"当"过期"报）。
    static func ageHours(of obj: [String: Any]) -> Double? {
        guard let s = obj["generated_at"] as? String else { return nil }
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(secondsFromGMT: 8 * 3600)
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        guard let t = f.date(from: s) else { return nil }
        return Date().timeIntervalSince(t) / 3600.0
    }

    static func todayStamp() -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: Date())
    }

    /// 通知策略：**正常更新不出声**（只亮角标），异常才弹。判断在 Python 侧
    /// （`fetch_model_data.alert_policy`）算完，这里只消费同一个结论 —— 两边各写一遍
    /// 迟早漂移。旧载荷没有 alert 字段时按 data_state 兜一个结论，不因字段缺失就沉默。
    private func notifyIfNewData() {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: ChartPaths.latestJSON)),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            Self.log("notifyIfNewData：latest.json 解析失败，跳过"); return
        }
        let state = (obj["data_state"] as? String) ?? "empty"
        let alert = obj["alert"] as? [String: Any]
        var kind = (alert?["kind"] as? String) ?? (state == "ok" ? "fresh" : state)
        var text = (alert?["text"] as? String) ?? ""
        // tone 决定通知标题的语气：info=值得知道的变动，warn=源坏了/整族没了/太久没更新。
        // 判据在 Python 侧（alert_policy）算完，这里只消费；旧载荷没这个字段时按 kind 兜。
        var tone = (alert?["tone"] as? String) ?? ((kind == "fresh" || kind.hasPrefix("moved")) ? "info" : "warn")
        // 去重键：异常带日期（一天一响），正常更新用内容指纹（同内容重抓不重复）。
        var key = (alert?["key"] as? String) ?? (state == "ok"
            ? ((obj["content_hash"] as? String) ?? "")
            : "alert:\(state):\(String(((obj["generated_at"] as? String) ?? "").prefix(10)))")
        // "太久没更新"只能在**读取侧**判：爬虫算 alert 的那一刻载荷刚生成、龄永远≈0，
        // 生产者侧那条分支根本不会触发（2026-10-07 实测：机器关机四天、三轮定时全没跑，
        // 载荷里的 alert 一直停在 fresh）。阈值与文案模板由载荷下发，这里只做减法。
        if kind == "fresh",
           let limit = (alert?["stale_after_hours"] as? NSNumber)?.doubleValue,
           let tpl = alert?["stale_text"] as? String,
           let age = Self.ageHours(of: obj), age > limit {
            kind = "not-running"
            text = tpl.replacingOccurrences(of: "{h}", with: String(Int(age)))
            key = "alert:not-running:" + Self.todayStamp()
            tone = "warn"          // 读取侧改判的这条是故障，不能沿用载荷里可能存在的 info
            Self.log("读取侧算龄 \(Int(age))h > \(Int(limit))h → 改判 not-running")
        }
        guard !key.isEmpty else { Self.log("notifyIfNewData：无去重键，跳过"); return }
        // 每次现读磁盘戳：Python 侧也会写这个文件，只信内存值会重复弹。
        let onDisk = (try? String(contentsOfFile: AppDelegate.notifiedStampFile, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let isNew = key != lastNotifiedKey && onDisk != key
        if kind == "fresh" {
            if isNew {
                lastNotifiedKey = key
                try? key.write(toFile: AppDelegate.notifiedStampFile, atomically: true, encoding: .utf8)
                setBadge(true)   // 角标是唯一留下的可见信号
            }
            Self.log("通知策略：fresh → 只更角标（新内容=\(isNew)）")
            return
        }
        guard isNew else { Self.log("通知策略：\(kind) 这一条已提示过，不重复响"); return }
        lastNotifiedKey = key
        try? key.write(toFile: AppDelegate.notifiedStampFile, atomically: true, encoding: .utf8)
        setBadge(true)
        Self.log("通知策略：\(kind) → 弹提示")
        deliverNotification(kind: kind, tone: tone, text: text.isEmpty ? "数据状态异常（\(kind)）" : text,
                            at: (obj["generated_at"] as? String) ?? "")
    }

    /// 通知标题按 tone 选：以前所有非 fresh 都写死"数据异常"，
    /// 于是一条正常的价格/指数变动会被冠上故障的标题。
    static func alertTitle(tone: String, fallbackKind: String, product: String) -> String {
        let t = tone.isEmpty ? (fallbackKind == "fresh" || fallbackKind.hasPrefix("moved") ? "info" : "warn") : tone
        return t == "info" ? "\(product) 值得注意的变动" : "\(product) 数据异常"
    }

    private func deliverNotification(kind: String, tone: String, text: String, at: String) {
        let subtitle = at.isEmpty ? "状态 \(kind)" : "抓取于 \(at)"
        let title = Self.alertTitle(tone: tone, fallbackKind: kind, product: "ModelRadar")
        switch notifyChannel {
        case .un:        deliverUN(title: title, subtitle: subtitle, body: text)
        case .osascript: deliverLegacy(title: title, subtitle: subtitle,
                                          body: text, why: "无标准 bundle")
        }
    }

    /// UN 在裸可执行文件（无 bundle id）上会直接 crash；标准 .app bundle 才走 UN，否则退回 osascript。
    private var notifyChannel: NotifyChannel {
        guard let bid = Bundle.main.bundleIdentifier, !bid.isEmpty,
              FileManager.default.fileExists(atPath: Bundle.main.bundlePath) else { return .osascript }
        return .un
    }

    private enum NotifyChannel { case un, osascript }

    private func deliverUN(title: String, subtitle: String, body: String) {
        let center = UNUserNotificationCenter.current()
        let post: () -> Void = {
            let c = UNMutableNotificationContent()
            c.title = title
            c.subtitle = subtitle
            c.body = body
            c.sound = UNNotificationSound.default
            let req = UNNotificationRequest(identifier: "modelradar.\(UUID().uuidString)",
                                            content: c, trigger: nil)
            center.add(req) { err in
                if let err {
                    // UN 投递失败不再只亮红点：改走兜底通道，让"有更新"这件事真的到达用户。
                    Self.log("UN 投递失败：\(err.localizedDescription) -> 走 legacy 兜底")
                    self.deliverLegacy(title: title, subtitle: subtitle, body: body,
                                          why: "UN 投递失败")
                } else {
                    Self.log("UN 通知已投递（\(subtitle)）")
                }
            }
        }
        center.getNotificationSettings { [weak self] st in
            guard let self else { return }
            switch st.authorizationStatus {
            case .authorized, .provisional:
                post()
            case .notDetermined:
                center.requestAuthorization(options: [.alert, .sound]) { granted, err in
                    Self.log("UN 授权 granted=\(granted) err=\(err.map(\.localizedDescription) ?? "nil")")
                    if granted { post() }
                    else { self.deliverLegacy(title: title, subtitle: subtitle, body: body,
                                                 why: "UN 未授权") }
                }
            default:  // .denied
                Self.log("UN 授权被拒 -> 走 legacy 兜底")
                self.deliverLegacy(title: title, subtitle: subtitle, body: body, why: "UN 被拒")
            }
        }
    }

    /// legacy 兜底通道（UN 被拒 / 无标准 bundle / UN 投递失败时到这里）。2026-10-03 起
    /// 优先走中枢统一出口（路径见下方 hub 常量）：中枢落审计档，且中枢装有
    /// terminal-notifier 时通知可点击打开看板（display notification 没有点击回调，
    /// 想要点击跳转只有换投递工具这一条路）；中枢不在时才直接 osascript。
    ///
    /// 为什么不用 NSUserNotification：它自 macOS 11 整体废弃，对未进通知中心白名单的
    /// ad-hoc 应用同样静默丢弃 —— 留着只会给人"还有两条路"的错觉。
    private func deliverLegacy(title: String, subtitle: String, body: String, why: String) {
        setBadge(true)   // 角标是无条件信号，通知到不到都先亮
        let hub = ChartPaths.hubNotify
        if !hub.isEmpty && FileManager.default.fileExists(atPath: hub) {
            var args = [title, body, "--source", "modelradar", "--subtitle", subtitle, "--no-sound"]
            let dash = URL(fileURLWithPath: ChartPaths.dashboard).resolvingSymlinksInPath()
            if FileManager.default.fileExists(atPath: dash.path) {
                args += ["--open", dash.absoluteString]
            }
            let p = Process()
            p.executableURL = URL(fileURLWithPath: hub)
            p.arguments = args
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
            p.terminationHandler = { proc in
                Self.log("中枢通知兜底（\(why)）rc=\(proc.terminationStatus)")
            }
            do { try p.run() } catch { Self.log("中枢通知启动失败（\(why)）：\(error.localizedDescription)") }
            return
        }
        let esc: (String) -> String = { $0.replacingOccurrences(of: "\\", with: "\\\\")
                                        .replacingOccurrences(of: "\"", with: "\\\"") }
        let script = "display notification \"\(esc(body))\" with title \"\(esc(title))\""
            + " subtitle \"\(esc(subtitle))\""
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", script]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        p.terminationHandler = { proc in
            Self.log("osascript 兜底（\(why)）rc=\(proc.terminationStatus)")
        }
        do { try p.run() } catch { Self.log("osascript 启动失败（\(why)）：\(error.localizedDescription)") }
    }

    /// 首启请求通知授权（fire-and-forget）；非标准 bundle 直接跳过，交给 legacy 兜底。
    private static func requestNotificationAuthorization() {
        guard let bid = Bundle.main.bundleIdentifier, !bid.isEmpty,
              FileManager.default.fileExists(atPath: Bundle.main.bundlePath) else { return }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { g, e in
            Self.log("通知授权 granted=\(g) err=\(e.map(\.localizedDescription) ?? "nil")")
        }
    }

    private static func writePidFile() {
        try? String(ProcessInfo.processInfo.processIdentifier)
            .write(toFile: Self.pidFile, atomically: true, encoding: .utf8)
    }

    /// 图标红点角标：只改视觉与 tooltip，不单独挂定时器；过期统一由 tick() 判 badgeDeadline。
    private func setBadge(_ on: Bool) {
        let apply: () -> Void = { [weak self] in
            guard let self else { return }
            self.badgeActive = on
            self.badgeDot?.isHidden = !on
            self.refreshToolTip()
            self.badgeDeadline = on ? Date().addingTimeInterval(60 * 15) : nil
            Self.saveBadgeState(active: on, deadline: self.badgeDeadline)
            Self.log("角标 -> \(on ? "亮" : "灭")")
        }
        if Thread.isMainThread { apply() } else { DispatchQueue.main.async(execute: apply) }
    }

    /// 角标状态落盘：亮时记 active + 过期时刻；灭时清文件。配合启动恢复，避免自重启吞掉未读提示。
    private static func saveBadgeState(active: Bool, deadline: Date?) {
        guard active, let d = deadline else {
            try? FileManager.default.removeItem(atPath: Self.badgeStateFile)
            return
        }
        let payload = ["active": true, "deadline": d.timeIntervalSince1970] as [String: Any]
        guard let data = try? JSONSerialization.data(withJSONObject: payload) else { return }
        try? data.write(to: URL(fileURLWithPath: Self.badgeStateFile), options: .atomic)
    }

    /// 启动恢复角标：若上次退出时红点亮且未过期，重新点亮（自重启 / 换版后不丢未读提示）。
    private func restoreBadgeState() {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: Self.badgeStateFile)),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              (obj["active"] as? Bool) == true,
              let ts = obj["deadline"] as? TimeInterval else { return }
        let deadline = Date(timeIntervalSince1970: ts)
        guard deadline > Date() else {
            try? FileManager.default.removeItem(atPath: Self.badgeStateFile)  // 已过期，清掉
            return
        }
        badgeActive = true
        badgeDeadline = deadline
        badgeDot?.isHidden = false
        refreshToolTip()
        Self.log("启动恢复角标（未过期）")
    }

    /// tooltip 优先级：未读红点 > 有新版本 > 常态。
    /// 三处都会改它，集中裁决，免得"检测到新版本"把"有未读"的提示盖掉（或反过来）。
    private func refreshToolTip() {
        statusItem?.button?.toolTip = badgeActive ? Self.unreadToolTip
            : (updateAvailable ? Self.updateToolTip : Self.baseToolTip)
    }

    private func clearBadge() {
        guard badgeActive else { return }
        setBadge(false)
    }

    private static let notifiedStampFile = ChartPaths.supportDir + "/.last_notified"

    // MARK: 动作

    @objc func regenerate(_ sender: AnyObject?) {
        Self.log("regenerate 进入")
        runAsyncTask(label: "regenerate", path: ChartPaths.regenerate,
                     processingText: "重新生成图表中…", timeoutSec: 90) { [weak self] r, _ in
            guard let self else { return .ok("组件已释放") }
            self.knownMtime = Self.mtimeOfChart()
            if self.popover?.isShown == true { self.chart.load() }
            switch r.outcome {
            case .success:
                return .ok("已重新生成图表（\(String(format: "%.1f", r.elapsed))s）")
            case .timeout:
                return .failed("重新生成超时（>90s），已整组终止。详情见 ⋯ → 打开日志目录")
            case .failure:
                return .failed("重新生成失败 rc=\(r.exitCode)："
                    + (r.tail.isEmpty ? "脚本无输出，见日志" : r.tail))
            }
        }
    }

    @objc func crawlNow(_ sender: AnyObject?) {
        Self.log("crawlNow 进入")
        let previousGeneration = Self.generatedAt(of: ChartPaths.latestJSON)
        // 爬虫所有来源共用240秒网络预算，另留60秒给合并、发布与渲染。
        runAsyncTask(label: "crawl", path: ChartPaths.crawl,
                     processingText: "正在抓取最新数据…", timeoutSec: 300,
                      dataLanded: {
                          let generation = Self.generatedAt(of: ChartPaths.latestJSON)
                          return generation != nil && generation != previousGeneration
                              && ["ok", "degraded"].contains(Self.dataState(of: ChartPaths.latestJSON))
                      }) { [weak self] r, landed in
            guard let self else { return .ok("组件已释放") }
            if self.popover?.isShown == true { self.chart.load() }
            self.knownMtime = Self.mtimeOfChart()
            self.notifyIfNewData()
            switch r.outcome {
            case .success, .timeout, .failure:
                return Self.crawlOutcome(
                    outcome: r.outcome, exitCode: r.exitCode, landed: landed,
                    chartExists: ChartPaths.exists(ChartPaths.chartHTML),
                    generatedAt: Self.generatedAt(of: ChartPaths.latestJSON),
                    dataState: Self.dataState(of: ChartPaths.latestJSON),
                    tail: r.tail,
                    sourceFailures: Self.sourceFailureSummary(of: ChartPaths.latestJSON),
                    elapsed: r.elapsed, timeoutSec: 300)
            }
        }
    }

    @objc func openDashboard(_ sender: AnyObject?) {
        Self.log("openDashboard 进入")
        // 先解软链再交给浏览器：看板里 <script src="data/latest.js"> 是按 **URL 路径词法解析**
        // 的。给它软链路径（MR_REPO 可能就是 ~/Applications/ModelRadar/src 这种软链农场），浏览器
        // 去读的是软链同级的 data/；而爬虫 publish_for_browser() 的 REPO_DATA 解过软链、写的是
        // 仓库那份镜像 —— 结果看板停在旧数据上，且 latest.js 读得到，一句错都不报。
        // 顺带让"仓库被挪走/软链断了"从"打开一个空白页"变成下面这句明确提示。
        let target = URL(fileURLWithPath: ChartPaths.dashboard).resolvingSymlinksInPath()
        guard ChartPaths.exists(target.path) else {
            chart.showMessage("找不到 dashboard.html\n（\(target.path)）")
            return
        }
        Self.log("打开看板：\(target.path)")
        NSWorkspace.shared.open(target)
    }

    @objc func quit(_ sender: AnyObject?) {
        Self.log("quit 进入")
        NSApp.terminate(self)
    }

    /// 「⋯」按钮的左键动作：手动弹出下拉菜单。
    /// 不能用 NSView.menu（那是右键上下文菜单，左键不触发）。
    @objc func showMoreMenu(_ sender: NSButton) {
        Self.log("more 按钮被点击 -> 弹出菜单")
        let m = NSMenu()
        func add(_ title: String, _ sel: Selector, _ key: String = "") {
            let it = NSMenuItem(title: title, action: sel, keyEquivalent: key)
            it.target = self
            m.addItem(it)
        }
        add("打开完整看板", #selector(AppDelegate.openDashboard(_:)))
        add("数据状态…", #selector(AppDelegate.showDataStatus(_:)))
        add("打开日志目录", #selector(AppDelegate.openLogDir(_:)))
        if updateAvailable {
            m.addItem(.separator())
            add("重启以应用新版本", #selector(AppDelegate.restartToUpdate(_:)))
        }
        if !otherInstancePids.isEmpty {
            add("⚠️ 另有 \(otherInstancePids.count) 个实例在跑", #selector(AppDelegate.noop(_:)))
        }
        m.addItem(.separator())
        add("退出 ModelRadar", #selector(AppDelegate.quit(_:)), "q")
        m.popUp(positioning: nil, at: NSPoint(x: 0, y: Int(sender.bounds.height)), in: sender)
    }

    @objc private func noop(_ sender: AnyObject?) {}

    @objc private func openLogDir(_ sender: AnyObject?) {
        Self.log("openLogDir 进入")
        let dir = LaunchLog.dir
        guard ChartPaths.exists(dir) else {
            chart.showResult("日志目录还不存在：\(dir)", isError: true)
            return
        }
        NSWorkspace.shared.open(URL(fileURLWithPath: dir))
    }

    @objc private func restartToUpdate(_ sender: AnyObject?) {
        Self.log("restartToUpdate 进入：显式换版（由用户点，不由心跳自动做）")
        guard let exe = Bundle.main.executableURL else { return }
        let p = Process()
        p.executableURL = exe
        do {
            try p.run()
        } catch {
            chart.showResult("拉起新版本失败：\(error.localizedDescription)", isError: true)
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { NSApp.terminate(nil) }
    }

    /// 数据状态面板：把"哪个源挂了、数据多新、路径在哪、跑的是哪一版"一次摊开。
    /// 这些原先在 GUI 里完全看不到，是"数据没更新但界面长得跟正常一样"的主要来源。
    @objc private func showDataStatus(_ sender: AnyObject?) {
        Self.log("showDataStatus 进入")
        let a = NSAlert()
        a.messageText = "ModelRadar 数据状态"
        a.informativeText = dataStatusText()
        a.addButton(withTitle: "好")
        a.runModal()
    }

    private func dataStatusText() -> String {
        var lines: [String] = []
        if !ChartPaths.exists(ChartPaths.runtimeEnvFile) {
            lines.append("⚠️ 缺 runtime.env：\(ChartPaths.runtimeEnvFile)")
            lines.append("   → 请重跑 widget/build_chart_widget.sh")
            return lines.joined(separator: "\n")
        }
        lines.append("数据目录  \(ChartPaths.data)")
        lines.append("代码仓库  \(ChartPaths.repo)")

        guard let data = try? Data(contentsOf: URL(fileURLWithPath: ChartPaths.latestJSON)),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            lines.append("⚠️ latest.json 读不到或解析失败：\(ChartPaths.latestJSON)")
            return lines.joined(separator: "\n")
        }
        // generated_at 是**数据**时间，抓取失败时它停在上一份可用数据上，不代表刚才跑过。
        // 真实抓取时刻只能从各源的 ts 取最大值 —— 那是本轮实际发请求的时间。
        lines.append("数据时间  \(obj["generated_at"] as? String ?? "—")")
        lines.append("最近抓取  \(Self.lastAttemptTime(in: obj) ?? "—")")
        lines.append("数据状态  \(obj["data_state"] as? String ?? "—")"
                     + "  模型 \(obj["models"] as? [Any] != nil ? ((obj["models"] as? [Any])?.count ?? 0) : 0) 个"
                     + "  变动 \((obj["changes"] as? [Any])?.count ?? 0) 条")
        if let note = obj["data_note"] as? String, !note.isEmpty { lines.append("备注      \(note)") }
        if let srcs = obj["sources"] as? [String: Any] {
            for k in srcs.keys.sorted() {
                guard let v = srcs[k] as? [String: Any] else { continue }
                let ok = (v["ok"] as? Bool) ?? false
                let n = (v["n"] as? Int).map(String.init) ?? "—"
                let ts = v["ts"] as? String ?? "—"
                var mark = ok ? "OK" : "失败"
                if (v["stale"] as? Bool) == true { mark = "降级(旧快照)" }
                lines.append("  源 \(k.padding(toLength: 12, withPad: " ", startingAt: 0)) \(mark)  n=\(n)  ts=\(ts)")
                if let e = v["err"] as? String, !e.isEmpty { lines.append("      \(String(e.prefix(160)))") }
            }
        }
        let chartAge = Self.mtimeOfChart().map { String(format: "%.0f 分钟前", Date().timeIntervalSince($0) / 60) } ?? "无"
        lines.append("chart.html \(chartAge)更新")
        if let exe = Bundle.main.executableURL,
           let m = (try? FileManager.default.attributesOfItem(atPath: exe.path))?[.modificationDate] as? Date {
            let f = DateFormatter(); f.dateFormat = "MM-dd HH:mm:ss"
            lines.append("二进制     \(f.string(from: m))" + (updateAvailable ? "  ← 有更新，⋯ 里可重启" : ""))
        }
        return lines.joined(separator: "\n")
    }

    /// 本轮各源实际发请求的时间里最大的那个（"YYYY-MM-DD HH:mm:ss" 可直接按字典序比较）。
    /// 抓取失败时 generated_at 会停在旧数据时间，只有它能回答"到底最近一次是什么时候跑的"。
    private static func lastAttemptTime(in obj: [String: Any]) -> String? {
        guard let srcs = obj["sources"] as? [String: Any] else { return nil }
        let stamps = srcs.values.compactMap { ($0 as? [String: Any])?["ts"] as? String }.filter { !$0.isEmpty }
        return stamps.max()
    }

    /// 只报不杀：同 bundle 还跑着别的实例就在日志和 ⋯ 菜单里说清楚。
    /// （旧版在这里 terminate 掉别的实例以求"重编译零手动生效"，代价是运行时行为
    /// 随时被换掉、且被换掉的那一版没人知道 —— 排查时比多一个图标更麻烦。）
    private func warnAboutOtherInstances() {
        let me = ProcessInfo.processInfo.processIdentifier
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: "com.modelradar.chart")
            .filter { $0.processIdentifier != me }
        otherInstancePids = others.map { Int($0.processIdentifier) }
        guard !others.isEmpty else { return }
        let pids = others.map { String($0.processIdentifier) }.joined(separator: ", ")
        Self.log("⚠️ 同 bundle 还有 \(others.count) 个实例在跑（pid=\(pids)）"
                 + "，菜单栏可能有重复图标。要收掉请在各自菜单里选「退出」，或跑 build 脚本。")
    }

    /// 读当前磁盘上可执行文件的 mtime（用于判断"是否已重编译出新版本"）。
    private static func binaryMtime() -> Date? {
        guard let url = Bundle.main.executableURL else { return nil }
        return try? FileManager.default.attributesOfItem(atPath: url.path)[.modificationDate] as? Date
    }

    /// 脚本执行结果：成功 / 失败 / 超时。
    /// 区分超时的原因：UI 需要给用户不同提示，且超时意味着子进程被强制终止。
    /// 非 private：crawlOutcome 的回归夹具要对同一条裁决传入真实枚举值。
    enum ScriptOutcome {
        case success, failure, timeout
    }

    /// 一次脚本执行的完整回执。⚠️ 光有 outcome 不够：原先 `.failure` 既不记日志也不留输出
    /// （stdout/stderr 直接喂给 nullDevice），于是"点了按钮没反应"这类问题**根本无法归因**——
    /// 2026-09-28 19:44~19:45 三次 regenerate/crawlNow 就是这个状态：数据零变化、日志零行，
    /// 而同样的脚本在终端 35 秒跑完 EXIT=0。带上 exit code + 输出末尾，才有可查的东西。
    private struct ScriptResult {
        let outcome: ScriptOutcome
        let exitCode: Int32
        let elapsed: Double
        let tail: String       // 输出末尾（已压成一行），直接进 UI 提示
    }

    /// 带超时跑一个 build 期生成的 shell。解释器与仓库位置都在 runtime.env / 壳脚本里，
    /// 这里不硬编码，免得挪目录或换 venv 就得重新编译 app。
    /// 超时处理：后台线程用 `waitUntilExit()` 同步等（只阻塞后台线程、不卡主线程），
    /// watch 点在超时点整组 `killpg(SIGKILL)`，据此区分 .timeout / .failure / .success。
    /// ⚠️ 之所以不用 `terminationHandler + DispatchGroup.wait(timeout:)`：后者在后台线程上
    /// `wait` 会阻塞该线程的 runloop pump，而子进程退出的通知需要 runloop 处理，
    /// 形成死锁 → 表现就是"点按钮卡住、要等满 timeout 才动"（此前卡死的真因，已验证）。
    /// 超时判定用 `terminationReason == .uncaughtSignal` 确定性判断，不读跨线程裸 Bool，
    /// 消除 data race（独立复核指出旧 `timedOut` 跨线程读写为竞态）。
    /// 杀整组（killpg）而非单进程：bash 拉起的 python / 爬虫子进程一并清掉，
    /// 不会变孤儿继续回写 latest.json / chart.html（独立复核指出的真问题）。
    private static func runScript(_ label: String, _ path: String,
                                  timeoutSec: Int = 120) -> ScriptResult {
        let started = Date()
        func done(_ outcome: ScriptOutcome, _ code: Int32, _ tail: String) -> ScriptResult {
            ScriptResult(outcome: outcome, exitCode: code,
                         elapsed: Date().timeIntervalSince(started), tail: tail)
        }

        guard ChartPaths.exists(path) else {
            let m = "缺少脚本 \(path)"
            log(m)
            return done(.failure, -1, m)
        }

        // 本次输出先落一个临时文件，结束后整体追加进 script.log —— 这样超时被杀也能留下
        // 已打印的部分（那往往正是卡住的位置）。
        let outPath = ScriptLog.scratch(label)
        FileManager.default.createFile(atPath: outPath, contents: nil)
        guard let fh = FileHandle(forWritingAtPath: outPath) else {
            let m = "打不开输出文件 \(outPath)"
            log(m)
            return done(.failure, -1, m)
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        proc.arguments = [path]
        proc.standardOutput = fh
        proc.standardError = fh          // 同一个句柄：stdout/stderr 合并，顺序即时间顺序
        do {
            try proc.run()
        } catch {
            try? fh.close()
            let m = "启动失败 \(path): \(error.localizedDescription)"
            log(m)
            return done(.failure, -1, m)
        }

        // 让子进程成为自己的进程组组长：killpg(pid) 才能连带清掉它的子孙（python / 爬虫子进程）。
        let childPid = proc.processIdentifier
        _ = setpgid(childPid, childPid)

        let watchdog = DispatchWorkItem { [weak proc] in
            guard let proc else { return }
            // SIGKILL 升级：防止子进程忽略 SIGTERM 残留；整组击杀含后代。
            killpg(proc.processIdentifier, SIGKILL)
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + .seconds(timeoutSec), execute: watchdog)
        proc.waitUntilExit()
        watchdog.cancel()
        try? fh.close()

        let output = (try? Data(contentsOf: URL(fileURLWithPath: outPath))) ?? Data()
        let tail = ScriptLog.oneLine(String(data: output, encoding: .utf8) ?? "")
        let code = proc.terminationStatus
        let elapsed = Date().timeIntervalSince(started)

        // 本进程只会被我们的看门狗以 SIGKILL 整组击杀，故 .uncaughtSignal == 超时；
        // 正常退出 terminationReason 为 .exit，无需跨线程共享标志。
        let outcome: ScriptOutcome
        if proc.terminationReason == .uncaughtSignal {
            outcome = .timeout
        } else {
            outcome = code == 0 ? .success : .failure
        }

        // ⚠️ 三种结果一律记账，包括成功：只记失败的话，"没日志"依然分不清是"没跑"还是"跑好了"。
        let logPath = ScriptLog.append(label: label, path: path,
                                       outcome: String(describing: outcome),
                                       code: code, elapsed: elapsed, output: output)
        log("脚本 \(label) 结束 outcome=\(outcome) rc=\(code) 耗时=\(String(format: "%.1f", elapsed))s"
            + (logPath.map { " 输出->\($0)" } ?? " 输出未落盘")
            + (tail.isEmpty ? "" : " 末行=\(tail.suffix(120))"))
        try? FileManager.default.removeItem(atPath: outPath)
        return ScriptResult(outcome: outcome, exitCode: code, elapsed: elapsed, tail: tail)
    }

    /// 一次任务该给用户看什么：文案 + 是否标红 + 是否允许遮住图。
    /// 三者必须分开——用户看到的"红"与"图被挡住"以前只由退出码决定，
    /// 于是 rc=2（旧数据，图有效）也被画成失败并把整张图盖掉。
    ///
    /// 红字与角标以前只看脚本退出码，于是"抓取成功、只是收尾重画那步非零退出"会被
    /// 渲染成失败；而文案那边还得再判一次数据状态，两处判据迟早漂移。
    /// 现在由 crawlOutcome 算一次，文案 / 颜色 / 角标 / 是否遮图都取这一个结果。
    struct TaskOutcome {
        let text: String
        let isError: Bool
        let blocking: Bool

        static func ok(_ text: String) -> TaskOutcome {
            TaskOutcome(text: text, isError: false, blocking: true)
        }
        /// 数据没变新但已有内容仍有效：如实告知，不涂红、不挡交互、不亮角标。
        static func warn(_ text: String) -> TaskOutcome {
            TaskOutcome(text: text, isError: false, blocking: false)
        }
        static func failed(_ text: String) -> TaskOutcome {
            TaskOutcome(text: text, isError: true, blocking: true)
        }
    }

    /// 「立即抓取」的裁决。纯函数：所有外部事实由调用方读好传进来，
    /// 回归夹具可以对同一份实现跑各种退出码，而不是测一份抄来的副本。
    ///
    /// 判据优先级（每一层都比"退出码非零"更具体）：
    ///   成功码 → 数据是否真的新；超时 → 数据是否已落地；
    ///   失败码 → 已落地 / rc=2 且图仍在（旧数据但有效）/ 其余真失败。
    static func crawlOutcome(outcome: ScriptOutcome, exitCode: Int32, landed: Bool,
                             chartExists: Bool, generatedAt: String?, dataState: String?,
                             tail: String, sourceFailures: String?,
                             elapsed: Double, timeoutSec: Int) -> TaskOutcome {
        switch outcome {
        case .success:
            if let state = dataState, ["ok", "degraded"].contains(state) {
                let at = generatedAt ?? "未知时间"
                let label = state == "degraded" ? "部分更新，失败来源见数据状态" : "抓取完成"
                return .ok("\(label) · 更新于 \(at)（\(String(format: "%.0f", elapsed))s）")
            }
            return .warn("抓取脚本正常退出，但数据源异常（未更新）。详情见 ⋯ → 数据状态")
        case .timeout:
            if landed { return .warn("数据已更新，但收尾超过\(timeoutSec)s，已终止。详情见 ⋯ → 打开日志目录") }
            return .failed("抓取超时（>\(timeoutSec)s），已整组终止。详情见 ⋯ → 打开日志目录")
        case .failure:
            // 数据已落地、只是收尾步骤非零退出：不能说成"抓取失败"，更不能涂红 + 亮角标。
            if landed {
                let at = generatedAt ?? "未知时间"
                return .warn("数据已更新（\(at)），但抓取脚本收尾退出码 \(exitCode)"
                    + (tail.isEmpty ? "" : "：\(tail)") + "。详情见 ⋯ → 打开日志目录")
            }
            // rc=2 是抓取脚本的既定约定（fetch_model_data：本轮 data_state 为 empty/stale），
            // 不是崩溃。crawl.sh 的渲染无条件执行，所以此时磁盘上那张图仍然有效，
            // 只是被如实标注成旧数据。整图涂红会把它画成坏图、还挡住悬停与点选。
            // 因此只做一行不挡交互的提示，但必须说明真实失败来源 —— 不能当成成功。
            if exitCode == 2, chartExists {
                let at = generatedAt ?? "未知时间"
                let why = sourceFailures ?? (tail.isEmpty ? "脚本无输出，见日志" : tail)
                return .warn("未取得新数据：\(why) · 图仍是 \(at) 的旧数据（详情见 ⋯ → 数据状态）")
            }
            return .failed("抓取失败 rc=\(exitCode)：" + (tail.isEmpty ? "脚本无输出，见日志" : tail))
        }
    }

    /// 统一封装"异步跑脚本 + 记账 + 回主线程"。
    ///
    /// ⚠️ 进度与结果都寄存在 `task` / `pendingResult`（AppDelegate 持有），不寄存在遮罩上：
    /// popover 是 .transient 的，用户点别处它就自己关。原先把进度只画在遮罩视图里、
    /// 又在 `popoverWillClose` 无条件 hideOverlay()，于是"点立即抓取 → 顺手点了别处 →
    /// 回来发现按钮灰着、没进度、图还是旧的"，看起来就跟界面坏了没区别。
    /// onDone 只负责给出 `TaskOutcome`，由这里决定是立刻显示还是攒到下次打开。
    ///
    /// `dataLanded` 是"这次到底有没有拿到新数据"的判据（由调用方给，它才知道看哪个字段）。
    private func runAsyncTask(label: String, path: String, processingText: String,
                              timeoutSec: Int,
                              dataLanded: (() -> Bool)? = nil,
                              onDone: @escaping (ScriptResult, Bool) -> TaskOutcome) {
        guard task == nil else {
            Self.log("\(label) 已在运行，忽略重复触发")
            chart.showResult("已有任务在跑（\(processingText)），请等它结束", isError: true)
            return
        }
        let st = TaskState(text: processingText, started: Date(), maxSec: timeoutSec)
        task = st
        // ⚠️ 顺序是刻意的：**先把脚本派出去，再画进度 UI**。
        // 之前是先画遮罩再 dispatch，而画遮罩那步会抛 ObjC 异常（NSBox content view 被拆 →
        // 约束无共同祖先），异常被 AppKit 吞掉后 dispatch 那行永远轮不到执行，
        // 于是 task 被置上却没有任何东西会把它清掉 —— 按钮永久禁用、不出倒计时、不等超时。
        // 表现层出错绝不能拖垮副作用层。
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result = Self.runScript(label, path, timeoutSec: timeoutSec)
            DispatchQueue.main.async {
                guard let self else { return }
                self.task = nil
                let landed = dataLanded?() ?? false
                let outcome = onDone(result, landed)
                // 角标与配色共用同一条裁决：红点代表"真失败"。rc=2（旧数据但图有效）
                // 以前也会亮红点，用户关着 popover 时只看到一个红点、打开却是可用的旧图。
                let failed = outcome.isError
                if self.popover?.isShown == true {
                    self.chart.setControlsEnabled(true)
                    self.chart.showResult(outcome.text, isError: outcome.isError,
                                          blocking: outcome.blocking)
                } else {
                    // 关着：结果攒到下次打开补显，同时亮角标当"有未读"的即时信号。
                    self.pendingResult = (outcome.text, outcome.isError, outcome.blocking)
                    self.chart.hideOverlay()
                    self.chart.setControlsEnabled(true)
                    Self.log("任务结束但 popover 未打开 → 结果暂存并亮角标：\(outcome.text)")
                    if failed { self.setBadge(true) }
                }
            }
        }
        // 副作用已经派出去了，这里才画进度 UI（popover 关着就不画，
        // 重开时由 restoreTaskUI 按 task 的真实起始时刻补上倒计时）。
        if popover?.isShown == true {
            chart.setControlsEnabled(false)
            chart.resumeProcessing(mainText: st.text, expectedMaxSec: st.maxSec, startedAt: st.started)
        }
    }


    /// 读 latest.json 的 data_state（ok / degraded / stale / empty），用于抓取后告诉用户是否真拿到新数据。
    /// degraded = 主源正常但副源缺失，显示部分更新，数据状态保留缺失来源。
    private static func dataState(of path: String) -> String? {
        guard ChartPaths.exists(path),
              let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let s = obj["data_state"] as? String else { return nil }
        return s
    }

    /// 从 latest.json 的 sources 里挑出真正失败/降级的源，压成一句话给提示用。
    /// 这里读的是**已落盘的证据**（爬虫自己写的 ok/stale/err），不是猜；
    /// 一个可疑源都没有时返回 nil，让调用方回退到脚本输出末尾。
    ///
    /// 提示只有一行、贴在图底部，所以不能把 err 原文整条塞进来：真实 err 形如
    /// 「抓取失败 https://artificialanalysis.ai/trends: 网络总时长上限」，
    /// 两条就把提示撑到 200 字。这里剥掉固定前缀和 URL，只留可辨认的那截原因。
    static func sourceFailureSummary(of path: String) -> String? {
        guard ChartPaths.exists(path),
              let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let srcs = obj["sources"] as? [String: Any] else { return nil }
        // 优先报"旧快照"（stale）—— 它才是 data_state=stale、图停在旧数据上的直接原因
        //（fetch_model_data 只在主源失败时这么标）；完全失败的副源排后面。
        var staleItems: [String] = []
        var brokenItems: [String] = []
        for k in srcs.keys.sorted() {
            guard let v = srcs[k] as? [String: Any] else { continue }
            let ok = (v["ok"] as? Bool) ?? false
            let stale = (v["stale"] as? Bool) == true
            guard !ok || stale else { continue }
            let reason = shortReason(v["err"] as? String)
            let item = reason.isEmpty ? k : "\(k) \(reason)"
            // stale 必须自己带标记：它的 err 往往就是"抓取失败…"，只写"失败"会让用户以为
            // 这次抓到了坏数据，实际是图仍停在旧快照上。
            if stale { staleItems.append("\(item)(旧快照)") } else { brokenItems.append(item) }
        }
        let ordered = staleItems + brokenItems
        guard !ordered.isEmpty else { return nil }
        return ordered.prefix(2).joined(separator: " · ")
            + (ordered.count > 2 ? " · 另 \(ordered.count - 2) 个源异常" : "")
    }

    /// 把爬虫的 err 压成"人一眼能认的那截原因"：去掉固定前缀与 URL，超长截断。
    private static func shortReason(_ err: String?) -> String {
        guard var s = err, !s.isEmpty else { return "" }
        for prefix in ["抓取失败", "解析失败", "失败"] where s.hasPrefix(prefix) {
            s.removeFirst(prefix.count)
        }
        // 去掉 URL 词：它占了 80% 的字符宽度，却分不出两个源的区别（每源一个固定榜单地址）。
        s = s.split(separator: " ").filter { !$0.hasPrefix("http") }.joined(separator: " ")
        s = s.trimmingCharacters(in: CharacterSet(charactersIn: " ：:，,；;。"))
        guard !s.isEmpty else { return "" }
        return "失败：" + String(s.prefix(24))
    }

    /// 走统一日志而不是自己写文件。
    /// 原因：自管日志文件会撞上应用的沙箱写权限（Application Support 下尤其常见），
    /// 写失败还是静默的（try? 一吞就没声）。os_log 由系统统一收集，
    /// 用户用「控制台」App 就能看，我这边也能 `log show` 读。
    private static let logger = Logger(subsystem: "com.modelradar.chart", category: "widget")

    /// 日志双写：os_log + 文件。
    /// 为什么必须落文件：本机受限会话里 `log show` 报 "Cannot run while sandboxed"，
    /// 统一日志我这边**读不出来**；只有自己落到家目录的文件才是可验证的证据。
    /// 文件路径：~/Library/Logs/ModelRadar/launch.log（脚本输出另见同目录 script.log）
    static func log(_ msg: String) {
        logger.info("\(msg, privacy: .public)")
        LaunchLog.write(msg)
    }

    /// 自检输出路径：命令行 `--selfcheck <png>` 优先，环境变量兜底。
    private static func selfCheckOutput() -> String? {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--selfcheck"), i + 1 < args.count {
            return args[i + 1]
        }
        if let v = ProcessInfo.processInfo.environment["MR_SELFCHECK"], !v.isEmpty { return v }
        return nil
    }

    static func scLog(_ msg: String) { log(msg) }
}

// MARK: - 落盘日志

/// 状态栏组件自身的落盘日志。
/// 存在的理由：统一日志在本机受限会话里读不出来（`log show` 直接报 sandbox 拒绝），
/// 而"回调到底进没进来""状态栏项建没建成"必须有**可验证证据**，不能靠看截图猜——
/// 上一轮就是靠肉眼看菜单栏，把一个控制中心自带的图标认成了自己的，误判了两轮。
enum LaunchLog {
    static let dir = NSHomeDirectory() + "/Library/Logs/ModelRadar"
    static let file = dir + "/launch.log"
    private static var prepared = false

    private static func prepare() {
        guard !prepared else { return }
        prepared = true
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        // 简单轮转：超过 256KB 就清掉重建，别让它无限长
        if let a = try? FileManager.default.attributesOfItem(atPath: file),
           let size = a[.size] as? Int, size > 256 * 1024 {
            try? FileManager.default.removeItem(atPath: file)
        }
    }

    /// 追加一行。注意用 `FileHandle(forWritingAtPath:)`：写成 `for: appendingTo:`
    /// 的话那个 `for` 会被 Swift 当成 for-in 循环头，报 expected expression。
    static func write(_ msg: String) {
        prepare()
        let line = "\(stamp())  pid=\(ProcessInfo.processInfo.processIdentifier)  \(msg)\n"
        guard let data = line.data(using: .utf8) else { return }
        if let h = FileHandle(forWritingAtPath: file) {
            h.seekToEndOfFile()
            h.write(data)
            try? h.close()
        } else {
            try? data.write(to: URL(fileURLWithPath: file))
        }
    }

    static func stamp(_ d: Date = Date()) -> String {
        let f = DateFormatter()
        f.dateFormat = "MM-dd HH:mm:ss"
        return f.string(from: d)
    }
}

// MARK: - 脚本输出日志

/// 组件拉起的脚本（regenerate / crawl）的输出去处。
///
/// 单独一个文件、不并进 launch.log：爬虫一次能吐几十行抓取日志，混进去会把
/// "回调进没进来""状态栏项建没建成"这类关键行冲掉；而 launch.log 有 256KB 轮转，
/// 一次爬虫就可能把它整个顶掉，等于用可观测性换可观测性。
enum ScriptLog {
    private static let file = LaunchLog.dir + "/script.log"
    private static let maxBytes = 256 * 1024

    /// 本次运行的临时输出文件。按 label 固定名：同一时刻只可能有一个该 label 的任务
    /// （runAsyncTask 已用 task 互斥挡住重复触发），跑完即删。
    static func scratch(_ label: String) -> String {
        try? FileManager.default.createDirectory(atPath: LaunchLog.dir, withIntermediateDirectories: true)
        return LaunchLog.dir + "/.out.\(label)"
    }

    /// 把一次运行的完整输出追加进 script.log，返回文件路径；写失败返回 nil。
    static func append(label: String, path: String, outcome: String, code: Int32,
                       elapsed: Double, output: Data) -> String? {
        let header = "\n===== \(LaunchLog.stamp()) \(label) outcome=\(outcome) rc=\(code) "
            + "elapsed=\(String(format: "%.1f", elapsed))s\n$ \(path)\n"
        guard let hd = header.data(using: .utf8) else { return nil }
        if let a = try? FileManager.default.attributesOfItem(atPath: file),
           let size = a[.size] as? Int, size > maxBytes {
            try? FileManager.default.removeItem(atPath: file)
        }
        guard let h = FileHandle(forWritingAtPath: file) else {
            // 文件还不存在：连头带输出一次写出
            var all = hd
            all.append(output)
            do { try all.write(to: URL(fileURLWithPath: file)); return file } catch { return nil }
        }
        defer { try? h.close() }
        h.seekToEndOfFile()
        h.write(hd)
        h.write(output)
        return file
    }

    /// 输出末尾压成一行，供 UI 直接显示（遮罩只有一行宽度，多行会把图表挤没）。
    static func oneLine(_ s: String) -> String {
        let lines = s.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        let tail = lines.suffix(2).joined(separator: " / ")
        return String(tail.suffix(200))
    }
}


// MARK: - Popover 内容：顶部工具条 + webview

/// WKWebView 自带的右键「Reload」会对当前页面做一次**重新导航**。
/// 我们的页面是 `loadHTMLString(读出来的 chart.html + 注入的配色锁定)`，
/// 根本没有可再导航的主资源，于是 WebKit 只能去够那个 baseURL，报 "cannot open file"。
/// 把 reload 接回我们自己的加载路径就正常了：重读文件 + 重新注入 CSS + 重新量高。
final class ChartWebView: WKWebView {
    /// 脚本桥必须与初始化配置里的 controller 是同一个实例；事后从
    /// `configuration` 取副本再加 handler 不保证能收到消息。
    let messages = WKUserContentController()
    /// 页面是否有非空选区。由 `selectionchange` 上报，只用于判断未处理的
    /// Cmd+C 该不该吞；不参与任何文本选择/鼠标事件的处理。
    var hasPageSelection = false
    var onReload: (() -> Void)?

    /// 文档被换掉时必须主动清掉旧选区状态。新文档是空的，WebKit **不会**为
    /// "旧文档有选区 → 新文档没选区"补发 `selectionchange`，残留的 true 会让
    /// 未处理的 Cmd+C 继续冒泡到 `NSWindow.keyDown:` → `NSBeep`（整屏闪）。
    /// 生产与回归夹具共用这一处，夹具不另抄判定。
    func resetPageSelection(_ reason: String) {
        guard hasPageSelection else { return }
        hasPageSelection = false
        AppDelegate.log("文档重置：清空选区状态（\(reason)）")
    }

    /// 新文档一开始必然没有选区。`documentStart` 无条件报一次 0 —— WebKit 不会为
    /// "旧文档有选区 → 新文档空"补发 `selectionchange`，少了这一报，宿主会带着上一次的
    /// true 继续判。生产注入与回归夹具共用这一份，夹具不另抄判定。
    static func selectionResetScript(handler: String) -> String {
        """
        try {
          window.webkit.messageHandlers.\(handler).postMessage(0);
        } catch (e) {}
        """
    }

    /// 之后按真实选区变化上报，只上报，不参与选择本身。
    static func selectionBridgeScript(handler: String) -> String {
        """
        document.addEventListener('selectionchange', function(){
          try {
            var s = window.getSelection();
            window.webkit.messageHandlers.\(handler).postMessage(
              (s && s.rangeCount > 0 && !s.isCollapsed) ? 1 : 0);
          } catch (e) {}
        });
        """
    }

    init() {
        let cfg = WKWebViewConfiguration()
        cfg.userContentController = messages
        super.init(frame: .zero, configuration: cfg)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    /// true = 页面不要这个键，且它不是焦点遍历/快捷键。纯函数，回归夹具直接调用。
    ///
    /// 判定只给父容器 `KeyFallbackView` 用：**不能在本类覆写 `keyDown`**——那会在
    /// WebKit 把按键交给页面之前吞掉空格/Escape，复选框与页面快捷键全部失效。
    static func shouldSwallowUnhandledKeyDown(_ event: NSEvent) -> Bool {
        if event.keyCode == 48 { return false }   // Tab / Shift-Tab
        let mods = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if mods.contains(.command) || mods.contains(.control) || mods.contains(.option) {
            return false
        }
        return true
    }

    /// true = 已经走我们自己的加载路径，调用方不要再触发 WebKit 导航。
    /// 兜底不静默放行：没接上 onReload 时先记一行日志再走 super，这样"接线被改坏"
    /// 表现为日志里的告警，而不是让用户再撞一次错误页。
    private func intercept(_ which: String) -> Bool {
        if let onReload { onReload(); return true }
        AppDelegate.log("reload[\(which)] 未接上 onReload → 退回 WebKit 导航（可能再现 cannot open file）")
        return false
    }

    /// 四个 reload 相关 selector 全接。WKWebView 一共暴露这四个
    /// （`reload` / `reload:` / `reloadFromOrigin` / `reloadFromOrigin:`，实测均可覆写）：
    /// 菜单与 ⌘R 走 target-action 形式，硬重载走 reloadFromOrigin，代码/窗口恢复/
    /// WebContent 进程重启走无参形式。只接一个，其余三条仍是错误页。
    ///
    /// 修正记录：上一版注释写"实测只有 reload(_:) 一个签名可覆写，其余 selector 家族冲突"
    /// —— **那是错的**。真因是我按 Void 声明 `override func reload()`，而 SDK 里它返回
    /// WKNavigation?；报的 "does not override any method from its superclass" 是签名不匹配。
    override func reload() -> WKNavigation? {
        intercept("reload") ? nil : super.reload()
    }
    @objc override func reload(_ sender: Any?) {
        if !intercept("reload:") { super.reload(sender) }
    }
    override func reloadFromOrigin() -> WKNavigation? {
        intercept("reloadFromOrigin") ? nil : super.reloadFromOrigin()
    }
    @objc override func reloadFromOrigin(_ sender: Any?) {
        if !intercept("reloadFromOrigin:") { super.reloadFromOrigin(sender) }
    }
}

/// 页面处理完仍未消费的按键会沿响应链冒泡到根视图。兜底必须放在这里：
/// WebView 先让 WebKit 把按键交给页面，只有页面不要的键才回到 AppKit；
/// 默认继续落到 `NSWindow.keyDown:` 会对无响应者的按键 `NSBeep` ——
/// 在状态栏 popover 里就是一声系统提示音，开了「闪烁屏幕代替提示音」时整屏闪。
///
/// Tab 与带 Command/Control/Option 的组合仍交回系统：焦点遍历和全局快捷键不能丢。
/// 这里不碰系统告警设置，也不改其它 App 的按键行为。
final class KeyFallbackView: NSView {
    /// Cmd+C 在没有选区时会继续冒泡到 `NSWindow` 并 `NSBeep`（开启「闪烁屏幕
    /// 代替提示音」时整屏闪）。只在**父容器已经收到这个未处理按键**且页面明确
    /// 没有选区时才吞；有选区时仍走 super，复制与文本选择都不动。其它 Command
    /// 组合完全不变。选区状态由页面 `selectionchange` 上报（见 ChartView）。
    override func keyDown(with event: NSEvent) {
        let mods = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if mods == .command, event.keyCode == 8,   // 8 = C（与键盘布局无关）
           chartWebView?.hasPageSelection != true {
            AppDelegate.log("无选区 Cmd+C 已吞掉（避免 NSBeep）")
            return
        }
        guard ChartWebView.shouldSwallowUnhandledKeyDown(event) else {
            super.keyDown(with: event)
            return
        }
        AppDelegate.log("页面未处理按键已吞掉：keyCode=\(event.keyCode)")
    }

    private var chartWebView: ChartWebView? {
        subviews.compactMap { $0 as? ChartWebView }.first
    }
}

/// WKUserContentController 强引用 handler；用弱壳接住 ChartView，避免
/// ChartView → web → controller → ChartView 的环。
private final class WeakScriptMessageHandler: NSObject, WKScriptMessageHandler {
    private weak var target: WKScriptMessageHandler?

    init(_ target: WKScriptMessageHandler) {
        self.target = target
    }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        target?.userContentController(userContentController, didReceive: message)
    }
}

final class ChartView: NSViewController {

    /// 顶栏高度。popover 总高 = 顶栏 + 页面真实内容高（见 onMeasured），
    /// 所以这个值一改，贴合逻辑自动跟着变，不用再手调 popover 高度。
    static let barHeight: CGFloat = 30
    static let popWidth: CGFloat = 748
    static let sizeTaskMessageName = "mrSizeTask"
    static let sizePenaltyMessageName = "mrSizePenalty"
    static let effortMessageName = "mrEffortSmooth"
    static let effortPreferenceKey = "effortSmoothOn"
    static let selectionMessageName = "mrSelection"

    private let web = ChartWebView()
    /// 页面加载完量一次真实内容高度回给宿主，用来把 popover 收到贴合内容的大小。
    var onMeasured: ((CGFloat) -> Void)?
    /// 遮罩容器。**刻意用自绘的 ScrimView 而不是 NSBox**：NSBox 的 `subviews` 会把它自己的
    /// content view（`_NSBoxCustomView`）也返回出来，于是 `subviews.forEach { removeFromSuperview() }`
    /// 会把 content view 一起拆掉，之后 addSubview 进去的 label 与盒子**没有共同祖先**，
    /// NSLayoutConstraint.activate 直接抛 NSGenericException。该异常从按钮 action 里逃出去、
    /// 被 AppKit 吞掉，结果是它后面那行"启动脚本"的代码根本不执行 ——
    /// 表现为"点按钮卡死、不出倒计时、也不超时恢复"，被误诊成死锁两轮。
    /// 复现夹具见 widget/probe_overlay.swift。
    private let overlay = ScrimView()
    /// 遮罩上我们自己加的文本，只清这些，绝不去动容器的 subviews
    private var overlayLabels: [NSTextField] = []
    private var delegate: AppDelegate?
    private var regenButton: NSButton?
    private var crawlButton: NSButton?
    private var cardsButton: NSButton?
    /// 顶栏标题。版本号不是常量：每次 chart 重新加载（首次/打开 popover/心跳/右键 Reload）
    /// 都从 latest.json 的顶层 evaluation_version 重读，避免把某一代评测写死进二进制。
    private var titleLabel: NSTextField?
    /// 遮罩"代际"：每次 showOverlay 自增，自动隐藏只关掉"自己那次"的遮罩，
    /// 避免结果提示的定时器把随后一次处理中遮罩误关掉。
    private var overlayGeneration = 0

    // 处理中遮罩的实时倒计时：最坏预期秒数 + 每秒刷新的剩余秒数。
    private var processingTimer: Timer?
    private var processingStart: Date?
    private var processingMaxSec: Int = 0
    private var processingSubLabel: NSTextField?

    /// 只给自检用：页面加载完通知一声
    var onDidLoad: (() -> Void)?

    /// 把 webview 当前渲染结果存成 PNG（自检用）。
    func snapshot(to path: String, completion: @escaping (Bool) -> Void) {
        let cfg = WKSnapshotConfiguration()
        // bounds 还没布局时给个兜底矩形，否则 snapshot 会返回空图
        cfg.rect = web.bounds.isEmpty ? NSRect(x: 0, y: 0, width: 748, height: 568) : web.bounds
        web.takeSnapshot(with: cfg) { img, err in
            guard let img,
                  let tiff = img.tiffRepresentation,
                  let rep = NSBitmapImageRep(data: tiff),
                  let png = rep.representation(using: .png, properties: [:]) else {
                AppDelegate.scLog("snapshot 失败：\(err?.localizedDescription ?? "拿不到图像")")
                completion(false)
                return
            }
            AppDelegate.scLog("snapshot 拿到图像 \(img.size)，准备写 \(path)")
            do {
                try png.write(to: URL(fileURLWithPath: path))
                AppDelegate.scLog("写盘成功 \(png.count) 字节")
                completion(true)
            } catch {
                AppDelegate.scLog("写文件失败：\(error)")
                completion(false)
            }
        }
    }

    override func loadView() {
        delegate = NSApp.delegate as? AppDelegate
        if delegate == nil {
            AppDelegate.log("loadView 警告：NSApp.delegate 不是 AppDelegate，按钮将无 target（点击静默）")
        }
        let root = KeyFallbackView()

        let bar = NSView()
        bar.translatesAutoresizingMaskIntoConstraints = false
        bar.heightAnchor.constraint(equalToConstant: Self.barHeight).isActive = true

        web.navigationDelegate = self
        // 页面 → 宿主的唯一通道：大小模式偏好与选区状态只走这几条消息。
        web.messages.add(WeakScriptMessageHandler(self), name: Self.sizeTaskMessageName)
        web.messages.add(WeakScriptMessageHandler(self), name: Self.sizePenaltyMessageName)
        web.messages.add(WeakScriptMessageHandler(self), name: Self.effortMessageName)
        web.messages.add(WeakScriptMessageHandler(self), name: Self.selectionMessageName)
        // 右键「Reload」= 走我们自己的加载路径（重读 chart.html + 重注入配色锁定）。
        // load() 之后 navigationDelegate 的 didFinish 会再量一次高度，贴合逻辑自动跟上。
        web.onReload = { [weak self] in self?.load() }
        // 不画背景，让 popover 那层系统材质透出来 —— 顶栏和图表才会同底同色。
        // （chart.html 现在也是 transparent + 随外观切换的取色，两边都不钉死颜色。）
        // ⚠️ 判据修正：WKWebView 上真实存在的选择器是私有的 **_setDrawsBackground:**，
        // 而不是 setDrawsBackground:。早先拿后者做"能不能设"的探测，恒为 false，
        // 于是这行透明底被静默跳过、webview 自己画了不透明底，把 macOS 26 的液态玻璃
        // 整层盖住（症状：popover 变成一块实心板）。KVC 的 "drawsBackground" key
        // 本来就解析到带下划线那条家族，所以查名字要查对它实际会走的那条。
        // Swift 捕获不了 ObjC 异常，所以仍然先探再设 —— 只是探的必须是这个。
        if web.responds(to: Selector(("_setDrawsBackground:"))) {
            web.setValue(false, forKey: "drawsBackground")
            // 设完读回来留证：只判断据有没有被走到，看不出 WebKit 是否真改了，
            // 所以把值打进日志 —— 下次再退化时日志里就有客观依据，不用靠肉眼猜。
            let readBack = (web.value(forKey: "drawsBackground") as? Bool).map(String.init) ?? "读不到"
            AppDelegate.log("透明底已设：drawsBackground=\(readBack)")
        } else {
            AppDelegate.log("WKWebView 不再响应 _setDrawsBackground: → 跳过 KVC 透明底")
        }
        // 公开 API 再兜一层：页面内容之外（回弹区域/未覆盖部分）也不该留实心底。
        if #available(macOS 12.0, *) { web.underPageBackgroundColor = .clear }
        web.translatesAutoresizingMaskIntoConstraints = false

        // 遮罩层：半透明纱盖在图上。底色随系统外观走（浅色下黑纱会把整块压脏、
        // 深色下白纱又刺眼），由 ScrimView 自绘解析，不用 layer.backgroundColor。
        // 容器不要换成 NSBox —— 见 overlay 属性注释里的坑。
        overlay.isHidden = true
        overlay.translatesAutoresizingMaskIntoConstraints = false

        // 顶栏标题按系统 popover 的 caption 规格来：11pt semibold + secondaryLabelColor，
        // 而不是 12pt 主文字色 —— 后者比内容还抢眼，就不像系统自己的东西了。
        let title = NSTextField(labelWithString: Self.titleText(
            evaluationVersion: Self.readEvaluationVersion()))
        title.font = NSFont.systemFont(ofSize: 11, weight: .semibold)
        title.textColor = .secondaryLabelColor
        titleLabel = title

        // ⚠️ target 必须直接指向 AppDelegate 实例：菜单项 / 按钮若只写 action 不写
        // target，会依赖 responder chain 去找，而 .accessory + NSPopover 上下文里
        // 经常找不到 AppDelegate，表现为"点了没反应"。这里显式绑定最稳。
        let regen = NSButton(title: "⟳ 重新生成", target: delegate,
                             action: #selector(AppDelegate.regenerate(_:)))
        regen.bezelStyle = .rounded
        regen.font = NSFont.systemFont(ofSize: 11)
        regen.controlSize = .small
        regenButton = regen

        let crawl = NSButton(title: "↻ 立即抓取", target: delegate,
                             action: #selector(AppDelegate.crawlNow(_:)))
        crawl.bezelStyle = .rounded
        crawl.font = NSFont.systemFont(ofSize: 11)
        crawl.controlSize = .small
        crawlButton = crawl

        // ⚠️ 「⋯」必须显式绑 action：NSView.menu 只是右键/control-click 上下文菜单，
        // 左键单击不会弹。之前只写了 menu、没给按钮 action，结果左键点「⋯」毫无反应
        // （菜单项 target 写得再对也没机会触发）。这里用 action 手动 popUp。
        // 卡片开关：push-on-push-off，状态一眼看得见（不靠"点了有没有反应"猜）。
        // target 同样必须显式绑 delegate，理由见上面那条 ⋯ 的注释。
        let cards = NSButton(title: "▤ 卡片", target: delegate,
                             action: #selector(AppDelegate.toggleCards(_:)))
        cards.setButtonType(.pushOnPushOff)
        cards.bezelStyle = .rounded
        cards.font = NSFont.systemFont(ofSize: 11)
        cards.controlSize = .small
        cards.state = (delegate?.cardsOn ?? true) ? .on : .off
        cardsButton = cards

        let more = NSButton(title: "⋯", target: delegate,
                            action: #selector(AppDelegate.showMoreMenu(_:)))
        more.bezelStyle = .rounded
        more.font = NSFont.systemFont(ofSize: 13)
        more.controlSize = .small

        root.addSubview(bar)
        root.addSubview(web)
        root.addSubview(overlay)
        for v in [title, regen, crawl, cards, more] {
            v.translatesAutoresizingMaskIntoConstraints = false
            bar.addSubview(v)
        }
        // delegate 解析失败时，宁可让按钮灰掉（有可见症状），也不要"看着能点、点了没反应"
        if delegate == nil { [regen, crawl, cards, more].forEach { $0.isEnabled = false } }

        NSLayoutConstraint.activate([
            title.leadingAnchor.constraint(equalTo: bar.leadingAnchor, constant: 12),
            title.centerYAnchor.constraint(equalTo: bar.centerYAnchor),
            more.trailingAnchor.constraint(equalTo: bar.trailingAnchor, constant: -10),
            more.centerYAnchor.constraint(equalTo: bar.centerYAnchor),
            crawl.trailingAnchor.constraint(equalTo: more.leadingAnchor, constant: -6),
            crawl.centerYAnchor.constraint(equalTo: bar.centerYAnchor),
            regen.trailingAnchor.constraint(equalTo: crawl.leadingAnchor, constant: -6),
            regen.centerYAnchor.constraint(equalTo: bar.centerYAnchor),
            cards.trailingAnchor.constraint(equalTo: regen.leadingAnchor, constant: -6),
            cards.centerYAnchor.constraint(equalTo: bar.centerYAnchor),

            bar.topAnchor.constraint(equalTo: root.topAnchor),
            bar.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            bar.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            bar.bottomAnchor.constraint(equalTo: web.topAnchor),

            // webview 撑满 bar 以下到 popover 底，不钉死高度：
            // 图表 SVG 在 748 宽下渲染高约 498，加上 HTML 脚注约 528，
            // popover 总高 600 - bar 32 = 568，留 40px 余量，不再裁切。
            web.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            web.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            web.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            overlay.topAnchor.constraint(equalTo: web.topAnchor),
            overlay.bottomAnchor.constraint(equalTo: web.bottomAnchor),
            overlay.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            overlay.trailingAnchor.constraint(equalTo: root.trailingAnchor),
        ])

        // 初始尺寸只是首屏估计；真实高度由 measureContentHeight() 量完后，
        // 交给 AppDelegate.applyContentHeight 把 popover 收到贴合内容的大小。
        preferredContentSize = NSSize(width: Self.popWidth, height: 600)
        view = root
    }

    // 遮罩底色由 ScrimView 自己在 draw(_:) 里解析动态色 —— NSViewController 没有
    // viewDidChangeEffectiveAppearance（那是 NSView 的），而 layer.backgroundColor
    // 存的是解析后的 CGColor、不会随外观变。自绘这两条都绕开了。

    /// 把开关状态推给页面：`window.MR_CARDS = 0/1`。
    /// 只写这一个全局量，页面里"贴不贴卡"的判断在共享 JS 的 cardsOf() 里读它 ——
    /// 宿主不碰 DOM 结构，两面各自演进也不会打架。
    func setCards(_ on: Bool) {
        cardsWanted = on
        applyCards()
    }

    private var cardsWanted: Bool {
        get { UserDefaults.standard.object(forKey: AppDelegate.cardsKey) as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: AppDelegate.cardsKey) }
    }

    private func applyCards() {
        web.evaluateJavaScript("window.MR_CARDS=\(cardsWanted ? 1 : 0);") { _, err in
            if let err {
                AppDelegate.log("卡片开关下发失败：\(err.localizedDescription)（页面按默认=开继续）")
            }
        }
    }

    /// 量一次页面真实内容高度（开关栏、图表与脚注），回给宿主贴合 popover。
    /// 为什么不再手估：SVG 是 `width:100%; height:auto`，高度随 popover 宽度变，
    /// 固定 600 只能按"约 498 + 脚注"留余量 —— 于是底部常年挂着一条没用的空白，
    /// 宽度一改就变成要么留白要么裁掉脚注。让渲染结果自己报数，两边不会不一致。
    private func measureContentHeight() {
        let js = """
        (function(){
          var w=document.querySelector('.wrap'), f=document.querySelector('.foot');
          var s=document.querySelector('.sizebar');
          if(w&&f){return Math.ceil(w.getBoundingClientRect().height+f.getBoundingClientRect().height+(s?s.getBoundingClientRect().height:0));}
          return Math.ceil(document.body?document.body.scrollHeight:0);
        })()
        """
        web.evaluateJavaScript(js) { [weak self] v, err in
            guard let self else { return }
            if let err {
                AppDelegate.log("内容高度测量失败：\(err.localizedDescription)（popover 维持原尺寸）")
                return
            }
            guard let h = (v as? NSNumber)?.doubleValue, h > 80, h < 4000 else { return }
            DispatchQueue.main.async { self.onMeasured?(CGFloat(h)) }
        }
    }

    /// 宿主当前是深色还是浅色。以 NSApp 的生效外观为准 —— 它就是 popover 材质用的那套，
    /// 页面跟着它走，顶栏和图表才不可能一个深一个浅。
    private static var hostIsDark: Bool {
        NSApp.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
    }

    /// 显式告诉页面该按哪套外观取色，不让 WKWebView 自己猜 prefers-color-scheme。
    /// 实测：系统明明是 Dark，离屏自检窗口里的 webview 却把
    /// `prefers-color-scheme` 报成 light，于是深色文字落在深色材质上 = 看不见。
    /// 只注入 `color-scheme` 这一个开关，具体两套色值仍由 render_scatter_svg.py 单点定义。
    private static func schemeLockCSS() -> String {
        let dark = hostIsDark
        // 每次都记：图上看不清时，"宿主以为是哪套外观"是唯一能一眼定案的证据
        AppDelegate.log("注入配色锁定：color-scheme:\(dark ? "dark" : "light")（NSApp.effectiveAppearance 判定）")
        return "<style>:root{color-scheme:\(dark ? "dark" : "light")}</style>"
    }

    /// 顶栏标题 = 固定文案 + 当前评测代次。5.0 -> v5，4.3 -> v4.3；
    /// 缺失/空/读不到时只显示固定文案，不猜一个版本出来。
    static func titleText(evaluationVersion: String?) -> String {
        let base = "ModelRadar · 性价比定位图"
        guard let v = displayVersion(evaluationVersion) else { return base }
        return "\(base) \(v)"
    }

    /// 只做展示层归一化：去掉可能已存在的 v 前缀、去掉结尾的 .0，再加回 v。
    static func displayVersion(_ raw: String?) -> String? {
        guard var s = raw?.trimmingCharacters(in: .whitespacesAndNewlines),
              !s.isEmpty else { return nil }
        if s.hasPrefix("v") || s.hasPrefix("V") { s.removeFirst() }
        s = s.trimmingCharacters(in: .whitespaces)
        while s.hasSuffix(".0") { s.removeLast(2) }
        guard let first = s.first, first.isNumber else { return nil }
        return "v\(s)"
    }

    /// 只认 latest.json 顶层的 evaluation_version（与 Python 写出口径一致）；
    /// 嵌套字段、旧缓存或无此字段都回退为无后缀标题。
    static func evaluationVersion(fromJSON data: Data) -> String? {
        guard let obj = try? JSONSerialization.jsonObject(with: data),
              let dict = obj as? [String: Any] else { return nil }
        return dict["evaluation_version"] as? String
    }

    static func readEvaluationVersion() -> String? {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: ChartPaths.latestJSON)) else {
            return nil
        }
        return evaluationVersion(fromJSON: data)
    }

    private func updateTitle() {
        titleLabel?.stringValue = Self.titleText(
            evaluationVersion: Self.readEvaluationVersion())
    }

    /// 每次加载前把当前偏好写成 documentStart 用户脚本：页面自己的 SIZE_JS 在首帧
    /// 就看到正确的模式，不会先画速度再跳任务。先清掉上一版脚本，避免重载叠出旧值。
    ///
    /// 两个偏好都必须在**同一帧**注入：只给任务模式不给收缩开关，页面会按
    /// "低分惩罚关着"画首帧，随后用户看到的是没收缩的图 —— 而他上次明确打开了。
    private func injectSizeTask() {
        let on = SizeTaskPref.isOn()
        let pen = SizePenaltyPref.isOn()
        web.messages.removeAllUserScripts()
        web.messages.addUserScript(WKUserScript(
            source: SizeTaskPref.script(on: on),
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true))
        web.messages.addUserScript(WKUserScript(
            source: SizePenaltyPref.script(on: pen),
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true))
        let effort = UserDefaults.standard.bool(forKey: Self.effortPreferenceKey)
        web.messages.addUserScript(WKUserScript(
            source: "window.MR_EFFORT_SMOOTH=\(effort ? 1 : 0);",
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true))
        injectSelectionBridge()
        AppDelegate.log("注入大小模式初值：MR_SIZE_TASK=\(on ? 1 : 0) "
                        + "MR_SIZE_PENALTY=\(pen ? 1 : 0)")
    }

    /// 只上报"有没有选区"，不参与选择本身；父容器据此决定未处理的 Cmd+C 是否吞掉。
    /// 非 private：回归夹具要验证这两段脚本与这条通道本身，复制一份就等于放弃判据。
    ///
    /// 两段脚本缺一不可：
    ///  · documentStart 先无条件报一次 0。新文档一开始必然没有选区，而 WebKit 不会
    ///    为"旧文档有选区 → 新文档空"补发 selectionchange；少了这一报，宿主会带着
    ///    上一次的 true 继续判，无选区的 Cmd+C 就一路冒泡到 NSWindow 触发 NSBeep。
    ///  · documentEnd 再挂 selectionchange，之后按真实选区变化上报。
    func injectSelectionBridge() {
        web.messages.addUserScript(WKUserScript(
            source: ChartWebView.selectionResetScript(handler: Self.selectionMessageName),
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true))
        web.messages.addUserScript(WKUserScript(
            source: ChartWebView.selectionBridgeScript(handler: Self.selectionMessageName),
            injectionTime: .atDocumentEnd,
            forMainFrameOnly: true))
    }

    func load() {
        updateTitle()
        injectSizeTask()
        let url = URL(fileURLWithPath: ChartPaths.chartHTML)
        guard let html = try? String(contentsOf: url, encoding: .utf8) else {
            showMessage("读取 chart.html 失败")
            return
        }
        // loadHTMLString 而不是 load(fileURL:)：避开 popover 宿主窗口下的 file:// 限制。
        // 图是内联 SVG，本来也不需要任何外部资源。脚注"抓取于 xxx"已内置于 HTML。
        // 追加的 style 放在最后，优先级压过页面自带的 @media 判定。
        web.loadHTMLString(html + Self.schemeLockCSS(), baseURL: url)
    }

    /// 把键盘焦点交给页面。NSPopover 打开时 AppKit 会先把焦点放到顶栏第一个按钮上；
    /// 页面没拿到焦点时，按键会停在 AppKit 侧（无人处理就 NSBeep）。打开后显式切到
    /// webview，页面自己的 keydown 与复选框才能接住按键；已装实例的 beep 正是这条路径。
    func focusPage() {
        guard let window = web.window else { return }
        window.makeFirstResponder(web)
    }

    func showMessage(_ text: String) {
        let esc = text
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: "\n", with: "<br>")
        web.loadHTMLString(
            "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
            + ":root{color-scheme:light dark}"
            + "html,body{margin:0;height:100%;background:transparent}"
            + "body{display:flex;align-items:center;justify-content:center;text-align:center;"
            + "font:13px/1.7 -apple-system,'PingFang SC',sans-serif;color:var(--mr-dim)}"
            + "@media (prefers-color-scheme: light){:root{--mr-dim:#6b7280}}"
            + "@media (prefers-color-scheme: dark){:root{--mr-dim:#8b95a5}}"
            + "</style>" + Self.schemeLockCSS() + "</head><body>\(esc)</body></html>",
            baseURL: nil)
    }

    /// 显示处理中/提示遮罩。textColor 默认跟随系统；结果提示可传 .systemRed 标红。
    /// `blocking=false` 用于"数据没变新但图仍有效"这类提示：不铺纱、不吃鼠标，
    /// 旧图保持可交互；文案本身仍需如实说明发生了什么。
    func showOverlay(_ text: String, textColor: NSColor = .labelColor, blocking: Bool = true) {
        overlay.dimsBackground = blocking
        overlayGeneration += 1
        let t = NSTextField(labelWithString: text)
        t.font = NSFont.systemFont(ofSize: 13)
        t.textColor = textColor
        t.alignment = .center
        // 长文案必须换行：原来是 labelWithString + 只钉中心点，"抓取脚本正常退出，但数据源
        // 异常（未更新）…" 这类句子会横向长出 popover 外面，用户只看到半句话。
        // 换行与限宽两件事都要做：只设 wraps 不给宽度约束，Auto Layout 仍按最长行算宽。
        t.lineBreakMode = .byWordWrapping
        t.maximumNumberOfLines = 0
        t.cell?.wraps = true
        t.cell?.isScrollable = false
        // 光设 wraps + width<=容器 不够：label 的单行固有宽度优先级更高，Auto Layout 会直接
        // 违约撑出去（夹具实测：容器 925 时 label 仍占 901 单行，一行都没折）。
        // 必须把水平方向的 hugging/compression 降下来，引擎才肯压窄它、让它纵向长行。
        t.setContentHuggingPriority(.defaultLow, for: .horizontal)
        t.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        t.translatesAutoresizingMaskIntoConstraints = false
        clearOverlayLabels()
        overlay.addSubview(t)
        overlayLabels.append(t)
        if blocking {
            NSLayoutConstraint.activate([
                t.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
                t.centerYAnchor.constraint(equalTo: overlay.centerYAnchor),
                t.widthAnchor.constraint(lessThanOrEqualTo: overlay.widthAnchor, constant: -28),
            ])
        } else {
            // 不挡图：提示贴底、自带底色，图的主体（散点、图例、悬停气泡）不被压住。
            t.drawsBackground = true
            t.backgroundColor = .controlBackgroundColor
            NSLayoutConstraint.activate([
                t.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
                t.bottomAnchor.constraint(equalTo: overlay.bottomAnchor, constant: -8),
                t.widthAnchor.constraint(lessThanOrEqualTo: overlay.widthAnchor, constant: -28),
            ])
        }
        overlay.isHidden = false
    }

    /// 处理中遮罩（带实时倒计时）：主文字 + 副文字「最坏预期 Ns · 剩余 Ms」，
    /// 每秒刷新一次；超过最坏预期后副文字转为「已超预期 Xs，仍在等待…」。
    /// 让用户对"还要等多久"有预期，而不是干瞪静态的"处理中"。
    ///
    /// `startedAt` 由调用方（AppDelegate 的 task）传入而不是取 Date()：popover 中途被
    /// transient 关掉再打开时，倒计时必须接着**任务真实的起始时刻**走，否则每次重开都
    /// 从"剩余 300s"重来，用户永远看不到它到底跑了多久。
    func resumeProcessing(mainText: String, expectedMaxSec: Int, startedAt: Date) {
        overlayGeneration += 1
        overlay.dimsBackground = true
        processingTimer?.invalidate(); processingTimer = nil
        clearOverlayLabels()

        let elapsed0 = Int(Date().timeIntervalSince(startedAt))
        let t = NSTextField(labelWithString: mainText)
        t.font = NSFont.systemFont(ofSize: 13, weight: .medium)
        t.textColor = .labelColor
        t.alignment = .center
        t.translatesAutoresizingMaskIntoConstraints = false

        let sub = NSTextField(labelWithString: Self.formatRemaining(elapsed: elapsed0, max: expectedMaxSec))
        sub.font = NSFont.systemFont(ofSize: 11)
        sub.textColor = .secondaryLabelColor
        sub.alignment = .center
        sub.translatesAutoresizingMaskIntoConstraints = false
        processingSubLabel = sub

        overlay.addSubview(t)
        overlay.addSubview(sub)
        overlayLabels.append(contentsOf: [t, sub])
        NSLayoutConstraint.activate([
            t.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
            t.centerYAnchor.constraint(equalTo: overlay.centerYAnchor, constant: -10),
            sub.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
            sub.topAnchor.constraint(equalTo: t.bottomAnchor, constant: 6),
        ])
        overlay.isHidden = false

        processingStart = startedAt
        processingMaxSec = expectedMaxSec
        let gen = overlayGeneration
        // 用 detached Timer 手动加到 .common mode：popover 打开时主 runloop 可能处于
        // non-default mode，scheduledTimer 默认只进 .default，会导致倒计时不刷新（"没显示"）。
        let timer = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self else { return }
            // 已被新一代遮罩/结果提示取代就停掉自己，避免误刷旧界面
            guard self.overlayGeneration == gen else {
                self.processingTimer?.invalidate(); self.processingTimer = nil
                return
            }
            let elapsed = Int(Date().timeIntervalSince(self.processingStart ?? Date()))
            self.processingSubLabel?.stringValue = Self.formatRemaining(elapsed: elapsed, max: self.processingMaxSec)
        }
        RunLoop.main.add(timer, forMode: .common)
        processingTimer = timer
    }

    /// 倒计时副文字格式化。
    private static func formatRemaining(elapsed: Int, max: Int) -> String {
        if elapsed < max {
            return "最坏预期 \(max)s · 剩余 \(max - elapsed)s"
        }
        return "已超预期 \(elapsed - max)s，仍在等待…"
    }

    /// 结果提示：成功/失败配色，2.5s 后自动消失（代际守卫，不会误删后续遮罩）。
    /// `blocking=false`：不铺纱、不吃鼠标，图保持可交互（rc=2 的降级提示走这条）。
    func showResult(_ text: String, isError: Bool, blocking: Bool = true) {
        showOverlay(text, textColor: isError ? .systemRed : .labelColor, blocking: blocking)
        let gen = overlayGeneration
        // 不挡图的提示贴在底部、字又长（"未取得新数据：… · 图仍是 … 的旧数据"），
        // 2.5s 读不完就消失。它不挡交互，多留一会儿也不碍事，所以给 8s。
        let hold: Double = blocking ? 2.5 : 8
        DispatchQueue.main.asyncAfter(deadline: .now() + hold) { [weak self] in
            guard let self, self.overlayGeneration == gen else { return }
            self.hideOverlay()
        }
    }

    /// 禁用/启用顶部两个「动作」按钮：处理中时禁用，防止重复点击并发跑脚本。
    /// 注意：**只禁 regen / crawl 两个会触发脚本的按钮，最右边的「⋯」更多按钮
    /// 始终不被禁用**——处理中用户仍可打开看板或退出，不被锁死。
    func setControlsEnabled(_ enabled: Bool) {
        regenButton?.isEnabled = enabled
        crawlButton?.isEnabled = enabled
    }

    /// 只移除我们自己登记的文本。**绝不要写 `overlay.subviews.forEach { removeFromSuperview() }`**
    /// —— 那是当初把 NSBox 的 content view 一起拆掉、进而让约束激活抛异常、
    /// 最后表现为"按钮点了没反应"的起点。
    private func clearOverlayLabels() {
        overlayLabels.forEach { $0.removeFromSuperview() }
        overlayLabels.removeAll()
        processingSubLabel = nil
    }

    func hideOverlay() {
        processingTimer?.invalidate()
        processingTimer = nil
        processingStart = nil
        clearOverlayLabels()
        overlay.isHidden = true
    }
}

extension ChartView: WKScriptMessageHandler {
    /// 只接受 0/1。页面已经先应用了新模式，这里只落盘，不回推 —— 回推会形成事件环。
    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        if message.name == Self.selectionMessageName {
            // 只认主框架的回报：子框架里的选区不属于这张图，混进来会让"有选区"卡住不放。
            // 注入本身已经是 forMainFrameOnly，这里再按 frameInfo 挡一次，属于第二道保险。
            guard message.frameInfo.isMainFrame else { return }
            guard let on = (message.body as? NSNumber)?.boolValue else { return }
            web.hasPageSelection = on
            return
        }
        if message.name == Self.sizePenaltyMessageName {
            guard let on = (message.body as? NSNumber)?.boolValue else {
                AppDelegate.log("低分惩罚消息体不是 0/1：\(message.body)（忽略）")
                return
            }
            SizePenaltyPref.set(on)
            AppDelegate.log("低分惩罚开关 → \(on ? "on" : "off")（已写入 UserDefaults）")
            return
        }
        if message.name == Self.effortMessageName {
            guard message.frameInfo.isMainFrame, let value = message.body as? NSNumber,
                  value == 0 || value == 1 else { return }
            UserDefaults.standard.set(value.boolValue, forKey: Self.effortPreferenceKey)
            return
        }
        guard message.name == Self.sizeTaskMessageName else { return }
        guard let on = (message.body as? NSNumber)?.boolValue else {
            AppDelegate.log("大小模式消息体不是 0/1：\(message.body)（忽略）")
            return
        }
        SizeTaskPref.set(on)
        AppDelegate.log("任务耗时大小开关 → \(on ? "task" : "speed")（已写入 UserDefaults）")
    }
}

extension ChartView: WKNavigationDelegate {
    /// 导航一起步就清选区状态：之后无论这条导航是新文档、页面自刷新还是我们自己的
    /// 重载，旧文档的选区都不再存在。documentStart 脚本是第二道保险（它可能来不及
    /// 在按键之前到达），两道都在，才不会留下"已换文档、宿主仍以为有选区"的窗口。
    func webView(_ webView: WKWebView,
                 didStartProvisionalNavigation navigation: WKNavigation!) {
        web.resetPageSelection("navigation start")
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        applyCards()              // 页面是重新加载的，开关状态必须重新推进去
        measureContentHeight()
        onDidLoad?()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        showMessage("加载失败：\(error.localizedDescription)")
    }
}

extension AppDelegate: NSPopoverDelegate {
    func popoverWillClose(_ notification: Notification) {
        guard (notification.object as? NSPopover) === popover else { return }
        lastPopoverCloseAt = Date()
        // popover 只建一次、长期复用（见 ensurePopover），所以这里**不能**把它置 nil；
        // 也不能顺手 hideOverlay —— 那正是"关一次窗就把进行中的倒计时和待显结果全冲掉"的入口。
        // 任务还在跑时保留遮罩内容，重开时由 restoreTaskUI 按真实起始时刻接着刷新倒计时。
        if task == nil { chart.hideOverlay() }
        Self.log("popover 关闭（任务\(task == nil ? "空闲" : "仍在跑")）")
    }
}
