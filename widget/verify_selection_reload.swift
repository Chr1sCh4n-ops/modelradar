/*
 verify_selection_reload.swift —— 换文档后选区状态必须归零的真原生夹具

 守的是 HIGH 缺陷本身：页面有选区时换掉文档（真实 reload / 重新 loadHTMLString），
 旧文档的选区已经不存在，但 WebKit **不会**补发 selectionchange。若宿主继续拿着
 上一次的 true，未处理的 Cmd+C 会被放行到 NSWindow.keyDown: → NSBeep（整屏闪）。

 判据三条，全部走生产代码：
   1) 导航一起步 ChartView.webView(_:didStartProvisionalNavigation:) 就把
      hasPageSelection 清成 false（生产 delegate，夹具不覆写、不改写判定）；
   2) documentStart 的生产脚本无条件报一次 0（生产静态脚本 + 生产 handler）；
   3) 子框架里的上报被 frameInfo.isMainFrame 挡掉，不能把"有选区"卡成 true。

 用真实 ChartView + 真实 ChartWebView + 真实 KeyFallbackView + 真实导航。
 页面换成进程内夹具页，不读已装 chart.html，不写数据库、偏好或系统设置。
 **不以 NSBeep 拦截计数作为证据**：证据是 `NSWindow.keyDown` 命中次数与
 `hasPageSelection` 这个生产状态位本身。

 用法：
   swiftc -O -o /tmp/verify_selection_reload \
     widget/ChartWidget.swift widget/verify_selection_reload.swift \
     -framework AppKit -framework WebKit -framework OSLog -framework UserNotifications
   /tmp/verify_selection_reload
*/
import AppKit
import WebKit

final class SelectionReloadWindow: NSWindow {
    var keyDownHits = 0

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }

    override func keyDown(with event: NSEvent) {
        keyDownHits += 1
        super.keyDown(with: event)
    }
}

/// 只观察页面真正上报了什么（含来自哪个 frame），不改状态、不替生产 handler 做判断。
/// 生产 handler 名已被 ChartView 占用，观察者必须挂在另一个名字上；脚本本体仍由
/// 生产静态方法生成，两路只差一个 handler 字符串，判定不分叉。
final class SelectionObserver: NSObject, WKScriptMessageHandler {
    static let name = "mrSelectionProbe"

    struct Report {
        let value: Bool
        let isMainFrame: Bool
    }
    private(set) var reports: [Report] = []

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.name == Self.name else { return }
        guard let value = (message.body as? NSNumber)?.boolValue else { return }
        reports.append(Report(value: value, isMainFrame: message.frameInfo.isMainFrame))
    }

    func reset() { reports.removeAll() }
}

final class SelectionReloadProbe: NSObject {
    private let window: SelectionReloadWindow
    private let chart = ChartView()
    private let observer = SelectionObserver()
    private var web: ChartWebView!
    private(set) var failures = 0
    private var reloadCount = 0

    init(window: SelectionReloadWindow) {
        self.window = window
        super.init()
    }

    func start() {
        window.contentViewController = chart
        window.setFrameOrigin(NSPoint(x: -4000, y: -4000))
        window.makeKeyAndOrderFront(nil)
        guard let found = Self.findWeb(chart.view) else {
            check("找到生产 ChartWebView", false, "ChartView 视图树里没有 ChartWebView")
            return finish()
        }
        web = found
        // 只观察上报，不接管状态；生产 ChartView 的 handler 仍在同一条通道上。
        web.messages.add(observer, name: SelectionObserver.name)
        // 右键 Reload 走的是这条接线；夹具换成自己的页面，避免依赖已装 chart.html。
        web.onReload = { [weak self] in
            guard let self else { return }
            self.reloadCount += 1
            self.web.loadHTMLString(Self.html, baseURL: nil)
        }
        // 走生产注入路径本身（两段脚本 + 生产 handler 名，主框架专用）。
        // 若生产漏掉 documentStart 那一段，下面的断言会立刻变红。
        chart.injectSelectionBridge()
        // 观察者：完整两段，放开 forMainFrameOnly，用来核对"上报来自 main 还是 sub"。
        web.messages.addUserScript(WKUserScript(
            source: ChartWebView.selectionResetScript(handler: SelectionObserver.name),
            injectionTime: .atDocumentStart,
            forMainFrameOnly: false))
        web.messages.addUserScript(WKUserScript(
            source: ChartWebView.selectionBridgeScript(handler: SelectionObserver.name),
            injectionTime: .atDocumentEnd,
            forMainFrameOnly: false))
        // 只为验生产 handler 的 frameInfo.isMainFrame 那道门：单独把**桥**放进子框架
        // （不放开 documentStart 的归零脚本，免得把上面第 2 条判据一起遮住）。
        web.messages.addUserScript(WKUserScript(
            source: ChartWebView.selectionBridgeScript(handler: ChartView.selectionMessageName),
            injectionTime: .atDocumentEnd,
            forMainFrameOnly: false))
        web.loadHTMLString(Self.html, baseURL: nil)
        waitReady { self.initialBridge() }
    }

    private static func findWeb(_ v: NSView) -> ChartWebView? {
        if let w = v as? ChartWebView { return w }
        for s in v.subviews {
            if let w = findWeb(s) { return w }
        }
        return nil
    }

    // MARK: 步骤

    private func initialBridge() {
        selectText { selected in
            self.check("夹具页面上真的产生了 DOM 选区", selected)
            self.after {
                self.check("选区桥：主框架上报 1，生产状态位变 true",
                           self.web.hasPageSelection && self.observer.reports.contains { $0.value && $0.isMainFrame },
                           "hasPageSelection=\(self.web.hasPageSelection) 上报=\(self.describe())")
                self.check("初始加载时 documentStart 报过 0（新文档从无选区起步）",
                           self.observer.reports.contains { !$0.value && $0.isMainFrame },
                           "上报=\(self.describe())")
                self.nativeReload()
            }
        }
    }

    /// 真实原生 reload：web.reload() → 生产 intercept → onReload → 换文档。
    private func nativeReload() {
        let before = reloadCount
        _ = web.reload()
        waitReady { [self] in
            check("原生 reload 确实走了生产的 onReload 接线", reloadCount == before + 1,
                  "onReload 次数 \(reloadCount - before)")
            check("原生 reload 换文档后 hasPageSelection 归零",
                  web.hasPageSelection == false,
                  "hasPageSelection=\(web.hasPageSelection) 上报=\(describe())")
            copyMustBeSwallowed(after: "原生 reload") { self.regeneratePath() }
        }
    }

    /// 重新 load()（「⟳ 重新生成」与程序内刷新走的同一条）：同样是换文档。
    private func regeneratePath() {
        selectText { selected in
            self.check("第二次加载后仍能选中文字（桥没有被 reload 弄坏）", selected)
            self.after {
                self.check("第二次加载后生产状态位再次变 true",
                           self.web.hasPageSelection,
                           "hasPageSelection=\(self.web.hasPageSelection)")
                self.web.loadHTMLString(Self.html, baseURL: nil)
                self.waitReady { [self] in
                    check("再次 loadHTMLString 换文档后 hasPageSelection 归零",
                          web.hasPageSelection == false,
                          "hasPageSelection=\(web.hasPageSelection) 上报=\(describe())")
                    copyMustBeSwallowed(after: "再次 load") { self.subframeReport() }
                }
            }
        }
    }

    /// 子框架上报 1 不能把状态卡成 true（生产 handler 按 frameInfo.isMainFrame 挡）。
    private func subframeReport() {
        observer.reset()
        web.evaluateJavaScript("""
            (function(){
              var f = document.getElementById('sub');
              if (!f) return 0;
              try { f.contentWindow.postMessage({mr:1}, '*'); } catch (e) { return -1; }
              return 1;
            })();
            """) { value, error in
            let posted = (value as? NSNumber)?.intValue == 1
            self.check("夹具页面上确实向子框架投递了上报", posted,
                       error?.localizedDescription ?? "返回=\(String(describing: value))")
            self.after {
                self.check("子框架上报不改变生产状态位",
                           self.web.hasPageSelection == false,
                           "hasPageSelection=\(self.web.hasPageSelection) 上报=\(self.describe())")
                self.check("子框架上报被识别为非主框架（frameInfo.isMainFrame == false）",
                           self.observer.reports.contains { !$0.isMainFrame },
                           "上报=\(self.describe())")
                self.finish()
            }
        }
    }

    // MARK: 断言公用

    /// 换文档后按 Cmd+C：生产状态位是 false，父容器必须吞掉，不能落到 NSWindow。
    private func copyMustBeSwallowed(after label: String, then next: @escaping () -> Void) {
        chart.focusPage()
        let before = window.keyDownHits
        sendCommandC()
        after {
            self.check("\(label)后无选区 Cmd+C 仍被父容器吞掉，不落到 NSWindow.keyDown",
                       self.window.keyDownHits == before,
                       "window +\(self.window.keyDownHits - before)")
            next()
        }
    }

    // MARK: 工具

    private func selectText(_ done: @escaping (Bool) -> Void) {
        let js = """
        (function(){
          var el = document.getElementById('pick-me');
          if (!el) return 0;
          var r = document.createRange(); r.selectNodeContents(el);
          var s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
          return s.isCollapsed ? 0 : 1;
        })()
        """
        web.evaluateJavaScript(js) { value, _ in
            done((value as? NSNumber)?.intValue == 1)
        }
    }

    /// 轮询到页面可交互：不覆写 navigationDelegate，生产 delegate 才是被验的那条。
    private func waitReady(_ done: @escaping () -> Void, attempt: Int = 0) {
        let js = "(document.readyState === 'complete' && !!document.getElementById('pick-me')) ? 1 : 0"
        web.evaluateJavaScript(js) { value, _ in
            if (value as? NSNumber)?.intValue == 1 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25, execute: done)
            } else if attempt < 60 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                    self.waitReady(done, attempt: attempt + 1)
                }
            } else {
                self.check("等待页面就绪", false, "6 秒内没有就绪")
                self.finish()
            }
        }
    }

    private func sendCommandC() {
        let stamp = ProcessInfo.processInfo.systemUptime
        for type in [NSEvent.EventType.keyDown, NSEvent.EventType.keyUp] {
            guard let event = NSEvent.keyEvent(
                with: type, location: .zero, modifierFlags: .command,
                timestamp: stamp, windowNumber: window.windowNumber, context: nil,
                characters: "c", charactersIgnoringModifiers: "c",
                isARepeat: false, keyCode: 8) else { continue }
            NSApp.sendEvent(event)
        }
    }

    private func after(_ block: @escaping () -> Void) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.45, execute: block)
    }

    private func describe() -> String {
        observer.reports.map { "\($0.value ? 1 : 0)@\($0.isMainFrame ? "main" : "sub")" }
            .joined(separator: ",")
    }

    private func check(_ label: String, _ condition: Bool, _ detail: String = "") {
        print("  \(condition ? "ok  " : "FAIL")  \(label)\(detail.isEmpty ? "" : " : \(detail)")")
        fflush(stdout)
        if !condition { failures += 1 }
    }

    private func finish() {
        print(failures == 0 ? "VERIFY SELECTION RELOAD OK" : "VERIFY SELECTION RELOAD FAILED (\(failures) 项)")
        fflush(stdout)
        NSApp.terminate(nil)
    }

    private static let html = """
    <!doctype html><html><head><meta charset="utf-8"><style>
    html,body{margin:0;height:100%;overflow:hidden}
    body{font:13px -apple-system}
    </style></head><body tabindex="-1">
    <p id="pick-me">可选中的一段文字</p>
    <iframe id="sub" style="width:1px;height:1px;border:0" srcdoc="<body><script>
      window.addEventListener('message', function(){
        // 必须用子框架自己的 window.webkit：走 parent.webkit 的话调用发生在主框架
        // 的 JS 世界里，frameInfo 会被记成 main，等于没验到那道门。
        try { window.webkit.messageHandlers.mrSelection.postMessage(1); } catch (e) {}
        try { window.webkit.messageHandlers.mrSelectionProbe.postMessage(1); } catch (e) {}
      });
    </script></body>"></iframe>
    </body></html>
    """
}

@main
struct VerifySelectionReload {
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)

        let window = SelectionReloadWindow(
            contentRect: NSRect(x: -4000, y: -4000, width: 500, height: 400),
            styleMask: [.borderless], backing: .buffered, defer: false)
        let probe = SelectionReloadProbe(window: window)
        app.activate(ignoringOtherApps: true)
        probe.start()

        DispatchQueue.main.asyncAfter(deadline: .now() + 45) {
            print("VERIFY SELECTION RELOAD TIMEOUT")
            fflush(stdout)
            NSApp.terminate(nil)
        }
        app.run()
        exit(probe.failures == 0 ? 0 : 1)
    }
}
