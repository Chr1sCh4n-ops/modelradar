/*
 verify_crawl_notice_ui.swift —— 结果提示"不挡图"的界面级夹具

 上一版把 rc=2 的提示**当成失败**处理，直接走整图遮罩：0.62 不透明纱把散点压暗、
 遮罩层还吃掉了命中测试，悬停气泡与点选全部失灵。文案换对了还不够 ——
 用户真正抱怨的是"图看起来坏了、还点不动"。

 本夹具用真实 ChartView 搭出 popover 内容，只验证两件事：
   1) 不挡模式（blocking=false）：遮罩不吃鼠标（hitTest 穿透到 webview），
      提示标签贴底、不压在图的中心区域；
   2) 挡模式（blocking=true）：遮罩仍然吃鼠标、标签居中 —— 真失败时
      "整图不可用"这件事必须看得见、也必须点不动。

 判据取用户可见的结果（能不能命中图的视图、标签落在哪），不比对内部标志位。

 用法：
   swiftc -O -o /tmp/verify_crawl_notice_ui \
     widget/main.swift widget/ChartWidget.swift widget/verify_crawl_notice_ui.swift \
     -framework AppKit -framework WebKit -framework OSLog -framework UserNotifications
   /tmp/verify_crawl_notice_ui
*/
import AppKit

final class FakeDelegate: NSObject, NSApplicationDelegate {}
private let fakeDelegate = FakeDelegate()

@main
struct VerifyCrawlNoticeUI {
    static var fails = 0

    static func check(_ label: String, _ cond: Bool, _ detail: String = "") {
        print("  \(cond ? "ok  " : "FAIL")  \(label)" + (detail.isEmpty ? "" : "  [\(detail)]"))
        fflush(stdout)
        if !cond { fails += 1 }
    }

    /// 弹出到的真实 popover 内容视图（含顶栏、webview、遮罩）。
    static func makeHost() -> (NSWindow, ChartView, NSView) {
        let vc = ChartView()
        let host = vc.view          // 触发 loadView
        let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: ChartView.popWidth, height: 620),
                           styleMask: [.borderless], backing: .buffered, defer: false)
        win.contentView = host
        win.orderFront(nil)
        host.layoutSubtreeIfNeeded()
        return (win, vc, host)
    }

    /// 遮罩层本体：ChartView 里唯一铺满 webview 的 ScrimView。
    static func scrim(in host: NSView) -> ScrimView? {
        func walk(_ v: NSView) -> ScrimView? {
            if let s = v as? ScrimView { return s }
            for sub in v.subviews { if let f = walk(sub) { return f } }
            return nil
        }
        return walk(host)
    }

    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        app.delegate = fakeDelegate

        print("== 不挡模式：rc=2 的提示必须让旧图保持可交互 ==")
        do {
            let (win, vc, host) = makeHost()
            vc.showResult("未取得新数据：aa 失败：HTTP 503 · 图仍是 08:10 的旧数据",
                          isError: false, blocking: false)
            host.layoutSubtreeIfNeeded()

            guard let s = scrim(in: host) else {
                print("PROBE FAIL：找不到遮罩层"); exit(1)
            }
            check("遮罩可见（提示要看得见）", !s.isHidden)
            // 命中测试必须穿透：遮罩中央落点若返回遮罩自己，图就被挡住了。
            let center = NSPoint(x: s.bounds.midX, y: s.bounds.midY)
            let hit = s.hitTest(center)
            check("遮罩不吞鼠标（hitTest 落空，交还下层 webview）", hit == nil,
                  hit.map { String(describing: type(of: $0)) } ?? "nil")

            // 提示标签必须贴底，而不是压在散点云的中心。
            let labels = s.subviews.compactMap { $0 as? NSTextField }
            check("不挡模式只有一条提示", labels.count == 1, "count=\(labels.count)")
            if let t = labels.first {
                let lowerHalf = t.frame.midY < s.bounds.midY
                check("提示落在下半区（不压图主体）", lowerHalf,
                      "label.y=\(String(format: "%.0f", t.frame.midY)) 容器高=\(String(format: "%.0f", s.bounds.height))")
                check("提示未溢出容器", t.frame.minX >= s.bounds.minX && t.frame.maxX <= s.bounds.maxX,
                      "x∈[\(String(format: "%.0f", t.frame.minX)), \(String(format: "%.0f", t.frame.maxX))]")
                check("提示自带底色（压在图上可读）", t.drawsBackground, "drawsBackground=\(t.drawsBackground)")
            }
            win.orderOut(nil)
        }

        print("== 挡模式：真失败仍要整图遮挡、并且点不动 ==")
        do {
            let (win, vc, host) = makeHost()
            vc.showResult("抓取超时（>240s），已整组终止", isError: true, blocking: true)
            host.layoutSubtreeIfNeeded()

            guard let s = scrim(in: host) else {
                print("PROBE FAIL：找不到遮罩层"); exit(1)
            }
            let center = NSPoint(x: s.bounds.midX, y: s.bounds.midY)
            check("挡模式遮罩吃掉鼠标", s.hitTest(center) != nil,
                  s.hitTest(center).map { String(describing: type(of: $0)) } ?? "nil")
            let labels = s.subviews.compactMap { $0 as? NSTextField }
            if let t = labels.first {
                check("挡模式提示居中", abs(t.frame.midY - s.bounds.midY) < s.bounds.height * 0.25,
                      "label.y=\(String(format: "%.0f", t.frame.midY))")
            }
            win.orderOut(nil)
        }

        print("== 挡过之后再来一条不挡提示：纱必须真的撤掉 ==")
        do {
            let (win, vc, host) = makeHost()
            vc.showResult("抓取失败 rc=1", isError: true, blocking: true)
            host.layoutSubtreeIfNeeded()
            vc.showResult("未取得新数据：图仍是旧数据", isError: false, blocking: false)
            host.layoutSubtreeIfNeeded()
            guard let s = scrim(in: host) else { print("PROBE FAIL：找不到遮罩层"); exit(1) }
            let center = NSPoint(x: s.bounds.midX, y: s.bounds.midY)
            check("纱已撤（hitTest 落空）", s.hitTest(center) == nil)
            check("旧提示已清（不叠影）", s.subviews.compactMap { $0 as? NSTextField }.count == 1,
                  "count=\(s.subviews.compactMap { $0 as? NSTextField }.count)")
            win.orderOut(nil)
        }

        print("== 真实最长文案：贴底提示不能长到把图盖掉 ==")
        do {
            let (win, vc, host) = makeHost()
            let real = "未取得新数据：aa(旧快照) · aa_progress 失败：网络总时长上限 · "
                + "另 1 个源异常 · 图仍是 2026-11-04 17:23:46 的旧数据（详情见 ⋯ → 数据状态）"
            vc.showResult(real, isError: false, blocking: false)
            host.layoutSubtreeIfNeeded()
            guard let s = scrim(in: host), let t = s.subviews.compactMap({ $0 as? NSTextField }).first else {
                print("PROBE FAIL：找不到遮罩/提示"); exit(1)
            }
            let ratio = t.frame.height / s.bounds.height
            print("  提示 frame=\(t.frame) 占容器高 \(String(format: "%.1f%%", ratio * 100))")
            check("贴底提示高度不超过容器 20%（图主体仍露出）", ratio < 0.20,
                  String(format: "%.1f%%", ratio * 100))
            check("提示未溢出容器", t.frame.minX >= s.bounds.minX && t.frame.maxX <= s.bounds.maxX,
                  "x∈[\(String(format: "%.0f", t.frame.minX)), \(String(format: "%.0f", t.frame.maxX))]")
            win.orderOut(nil)
        }

        print()
        print(fails == 0 ? "VERIFY OK" : "VERIFY FAILED (\(fails) 项)")
        exit(fails == 0 ? 0 : 1)
    }
}
