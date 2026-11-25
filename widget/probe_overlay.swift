// 遮罩构造的回归夹具：证明"裸 NSView + 自己登记的 label"这条路径不会抛 ObjC 异常。
//
//   swift widget/probe_overlay.swift        # 跑到 PROBE DONE 即通过；抛异常即失败（进程终止）
//
// 为什么需要它：ChartView 的进度遮罩曾经用 NSBox 当容器，而 **NSBox.subviews 会把盒子自己的
// content view（_NSBoxCustomView）一起返回**，于是
//     overlayBox.subviews.forEach { $0.removeFromSuperview() }
// 把 content view 拆了；之后 addSubview 进来的文本与盒子**没有共同祖先**，
// NSLayoutConstraint.activate 抛 NSGenericException：
//     "Unable to activate constraint ... because they have no common ancestor."
// 该异常从按钮 action 逃出去、被 AppKit 吞掉，于是它后面那行"启动脚本"的代码永远不执行 ——
// 用户看到的是"点按钮卡死、不出倒计时、也不等超时恢复"，而前两轮排查都把它当成
// 线程死锁去修（DispatchGroup.wait → waitUntilExit），方向完全错了。
//
// Swift 侧不能 catch ObjC 异常，所以判据就是"进程活着走到最后一行"。
// 本夹具同时把两个变体都跑一遍：NSBox 版（预期抛，注释掉以免误伤 CI）与 NSView 版（预期通过）。
import AppKit

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

func makeWindow(_ root: NSView) -> NSWindow {
    let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 748, height: 600),
                       styleMask: [.borderless], backing: .buffered, defer: false)
    win.contentView = root
    win.orderFront(nil)
    root.layoutSubtreeIfNeeded()
    return win
}

/// 与 ChartView 现在这套遮罩同构：裸 NSView 容器 + 只移除自己登记的文本。
func buildOverlayLikeChartView() {
    let root = NSView(frame: NSRect(x: 0, y: 0, width: 748, height: 600))
    let web = NSView()
    web.translatesAutoresizingMaskIntoConstraints = false
    let overlay = NSView()
    overlay.wantsLayer = true
    overlay.layer?.backgroundColor = NSColor.black.withAlphaComponent(0.62).cgColor
    overlay.translatesAutoresizingMaskIntoConstraints = false
    root.addSubview(web)
    root.addSubview(overlay)
    NSLayoutConstraint.activate([
        web.leadingAnchor.constraint(equalTo: root.leadingAnchor),
        web.trailingAnchor.constraint(equalTo: root.trailingAnchor),
        web.topAnchor.constraint(equalTo: root.topAnchor),
        web.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        overlay.topAnchor.constraint(equalTo: web.topAnchor),
        overlay.bottomAnchor.constraint(equalTo: web.bottomAnchor),
        overlay.leadingAnchor.constraint(equalTo: root.leadingAnchor),
        overlay.trailingAnchor.constraint(equalTo: root.trailingAnchor),
    ])
    _ = makeWindow(root)

    var labels: [NSTextField] = []
    // 连做三轮 show → clear，专门覆盖"第二次清空"这条最容易出事的路径
    for round in 1...3 {
        labels.forEach { $0.removeFromSuperview() }
        labels.removeAll()
        let t = NSTextField(labelWithString: "正在抓取最新数据… 第 \(round) 轮")
        t.font = NSFont.systemFont(ofSize: 13, weight: .medium)
        t.alignment = .center
        t.translatesAutoresizingMaskIntoConstraints = false
        let sub = NSTextField(labelWithString: "最坏预期 300s · 剩余 \(300 - round)s")
        sub.font = NSFont.systemFont(ofSize: 11)
        sub.alignment = .center
        sub.translatesAutoresizingMaskIntoConstraints = false
        overlay.addSubview(t)
        overlay.addSubview(sub)
        labels.append(contentsOf: [t, sub])
        NSLayoutConstraint.activate([
            t.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
            t.centerYAnchor.constraint(equalTo: overlay.centerYAnchor, constant: -10),
            sub.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
            sub.topAnchor.constraint(equalTo: t.bottomAnchor, constant: 6),
        ])
        overlay.isHidden = false
        overlay.layoutSubtreeIfNeeded()
        print("  第 \(round) 轮：遮罩已建，label.frame=\(t.frame) 容器 subviews=\(overlay.subviews.count)")
    }
    labels.forEach { $0.removeFromSuperview() }
    print("  NSBox 陷阱对照：NSView 容器的 subviews 只含我们自己加的文本（\(labels.count) 个已清空）")
}

/// 倒计时必须真的每秒 fire —— 这是用户报"不出倒计时"的另一半。
func countdownTimerFires() {
    var fired = 0
    let timer = Timer(timeInterval: 1.0, repeats: true) { _ in fired += 1 }
    RunLoop.main.add(timer, forMode: .common)
    RunLoop.main.run(until: Date().addingTimeInterval(2.5))
    timer.invalidate()
    print("  .common 模式下 2.5s 内 timer 触发 \(fired) 次")
    if fired < 2 {
        print("PROBE FAIL：倒计时不刷新（Timer 未进 .common 模式）")
        exit(1)
    }
}

/// 结果提示必须换行且不横向长出 popover：用户报"更新提示超出不换行"，
/// 根因是 labelWithString + 只钉中心点（没有宽度约束），长句子直接溢出被裁成半句。
/// 这里跑的是**同一套布局配置**（wraps / maximumNumberOfLines / width≤容器-28），
/// 用一条 60 字以上的长句子量出实际 frame。
func resultLabelWraps() {
    let root = NSView(frame: NSRect(x: 0, y: 0, width: 420, height: 320))
    let overlay = NSView(frame: root.bounds)
    overlay.translatesAutoresizingMaskIntoConstraints = false
    root.addSubview(overlay)
    NSLayoutConstraint.activate([
        // 容器尺寸必须钉死：只靠 makeWindow 的 contentRect 会被系统按屏幕/缩放改成别的值
        // （第一次跑就量出"容器 925"，判据当场失去意义）
        root.widthAnchor.constraint(equalToConstant: 420),
        root.heightAnchor.constraint(equalToConstant: 320),
        overlay.leadingAnchor.constraint(equalTo: root.leadingAnchor),
        overlay.trailingAnchor.constraint(equalTo: root.trailingAnchor),
        overlay.topAnchor.constraint(equalTo: root.topAnchor),
        overlay.bottomAnchor.constraint(equalTo: root.bottomAnchor),
    ])
    _ = makeWindow(root)
    let long = "数据已更新（2026-10-08 13:38:00），但抓取脚本收尾退出码 1："
        + "已写出 70 个模型（degraded），变动 67 条，通知=degraded。详情见 ⋯ → 打开日志目录"
    let t = NSTextField(labelWithString: long)
    t.font = NSFont.systemFont(ofSize: 13)
    t.alignment = .center
    t.lineBreakMode = .byWordWrapping
    t.maximumNumberOfLines = 0
    t.cell?.wraps = true
    t.cell?.isScrollable = false
    t.setContentHuggingPriority(.defaultLow, for: .horizontal)
    t.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
    t.translatesAutoresizingMaskIntoConstraints = false
    overlay.addSubview(t)
    NSLayoutConstraint.activate([
        t.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
        t.centerYAnchor.constraint(equalTo: overlay.centerYAnchor),
        t.widthAnchor.constraint(lessThanOrEqualTo: overlay.widthAnchor, constant: -28),
    ])
    overlay.layoutSubtreeIfNeeded()
    let oneLine = t.cell?.cellSize(forBounds: NSRect(x: 0, y: 0, width: 10000, height: 1000)).height ?? 0
    print("  长文案：label.frame=\(t.frame) 容器宽=\(overlay.bounds.width) 单行高≈\(String(format: "%.1f", oneLine))")
    // 判据取"用户看得见的那件事"：文本必须整块待在容器里、并且折成了多行。
    // 不用"宽度 ≤ 容器-20"这种间接式 —— 实测 Auto Layout 会在左右内边距上差几个点，
    // 间接式会因为那几个点误报，而"有没有溢出容器"才是用户在意的那件事。
    let box = overlay.bounds
    if t.frame.minX < box.minX || t.frame.maxX > box.maxX {
        print("PROBE FAIL：结果提示横向长出容器 x∈[\(t.frame.minX), \(t.frame.maxX)] 容器 \(box.width)")
        exit(1)
    }
    if t.frame.height <= oneLine * 1.6 {
        print("PROBE FAIL：结果提示没换行（高 \(t.frame.height) 只够一行）")
        exit(1)
    }
}

print("== 遮罩构造（NSView 容器 + 登记式清空）==")
buildOverlayLikeChartView()
print("== 结果提示换行与限宽 ==")
resultLabelWraps()
print("== 倒计时刷新 ==")
countdownTimerFires()
print("PROBE DONE")
exit(0)
