/*
 verify_mouse_routing.swift —— 鼠标路由的真原生集成夹具

 背景：用户报告「点 popover 空白处也闪系统告警」。键盘那条路已经由
 KeyFallbackView 兜底；这里单独验证**鼠标**，避免再把键盘路径当成鼠标证据。

 守的是：
   · 点 WebKit 空白区时，事件由 WebKit 消费，DOM click 正常且不进入按键兜底；
   · 点原生顶栏空白时，事件可以落到 NSWindow 的 mouseDown 路由，但绝不落到 keyDown；
   · 点复选框时，控件照常切换，不被父容器吞掉；
   · 空白点击不改变页面焦点；
   · 未处理的普通按键仍由 KeyFallbackView 吞掉，不落到 NSWindow.keyDown。

 为什么用 NSWindow.noResponderFor: 计数而不是自称拦截了 NSBeep：
  AppKit 只在 eventSelector == keyDown: 时由默认 noResponder 发出 NSBeep；
  mouseDown/mouseUp 走到同一个路由也不会响。夹具窗口覆写 noResponder 后
  不调 super，避免测试本身触发系统告警，同时保留「哪条 selector 真的走到了
  NSWindow」这条可核对的证据。正向对照是一条普通 NSView 的空白点击与按键：
  两者都必须被这里看见，证明计数不是摆设。

 使用真实 ChartView + ChartWebView + KeyFallbackView；页面换成进程内夹具页，
 不依赖已装 chart.html，也不写数据库与偏好。鼠标/按键均为合成 NSEvent。

 用法：
   swiftc -O -o /tmp/verify_mouse_routing \
     widget/ChartWidget.swift widget/verify_mouse_routing.swift \
     -framework AppKit -framework WebKit -framework OSLog -framework UserNotifications
   /tmp/verify_mouse_routing
*/
import AppKit
import WebKit

/// 在进程内把生产 KeyFallbackView 的 keyDown 包一层计数；交换后
/// `probeMouseRoutingKeyDown` 调回原实现，判定与吞掉行为不变。
var mouseRoutingParentKeyHits = 0

extension KeyFallbackView {
    @objc dynamic func probeMouseRoutingKeyDown(with event: NSEvent) {
        mouseRoutingParentKeyHits += 1
        probeMouseRoutingKeyDown(with: event)
    }
}

final class MouseRoutingWindow: NSWindow {
    var keyDownHits = 0
    var noResponderSelectors: [String] = []

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }

    override func keyDown(with event: NSEvent) {
        keyDownHits += 1
        super.keyDown(with: event)
    }

    /// 只记录、不调 super：默认实现在 keyDown 上会 NSBeep，测试不能自己闪屏。
    override func noResponder(for eventSelector: Selector) {
        noResponderSelectors.append(NSStringFromSelector(eventSelector))
    }

    func keyDownNoResponderCount() -> Int {
        noResponderSelectors.filter { $0 == "keyDown:" }.count
    }
}

final class MouseRoutingProbe: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private let window: MouseRoutingWindow
    private let rawWindow: MouseRoutingWindow
    private let chart = ChartView()
    private var web: ChartWebView!
    private(set) var failures = 0
    private var started = false

    init(window: MouseRoutingWindow, rawWindow: MouseRoutingWindow) {
        self.window = window
        self.rawWindow = rawWindow
        super.init()
    }

    func start() {
        let original = class_getInstanceMethod(
            KeyFallbackView.self, #selector(NSResponder.keyDown(with:)))!
        let replacement = class_getInstanceMethod(
            KeyFallbackView.self, #selector(KeyFallbackView.probeMouseRoutingKeyDown(with:)))!
        method_exchangeImplementations(original, replacement)

        rawWindow.contentView = NSView(frame: NSRect(x: 0, y: 0, width: 200, height: 120))
        rawWindow.setFrameOrigin(NSPoint(x: -4000, y: -4000))
        rawWindow.makeKeyAndOrderFront(nil)
        rawWindow.makeFirstResponder(rawWindow.contentView)

        window.contentViewController = chart
        window.setFrameOrigin(NSPoint(x: -4000, y: -4000))
        window.makeKeyAndOrderFront(nil)
        web = findWeb(chart.view)
        guard let web else {
            check("找到生产 ChartWebView", false, "ChartView 视图树里没有 ChartWebView")
            return finish()
        }
        web.navigationDelegate = self
        web.loadHTMLString(Self.html, baseURL: nil)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {}

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard !started else { return }
        started = true
        chart.focusPage()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
            self.rawPositiveControls()
        }
    }

    private func findWeb(_ v: NSView) -> ChartWebView? {
        if let w = v as? ChartWebView { return w }
        for s in v.subviews {
            if let w = findWeb(s) { return w }
        }
        return nil
    }

    // MARK: 正向对照

    private func rawPositiveControls() {
        rawWindow.makeKeyAndOrderFront(nil)
        rawWindow.makeFirstResponder(rawWindow.contentView)
        let p = NSPoint(x: 100, y: 60)
        rawWindow.noResponderSelectors.removeAll()
        click(p, in: rawWindow, type: .leftMouseDown)
        click(p, in: rawWindow, type: .leftMouseUp)
        check("正向对照：空白 NSView 的 mouseDown 能走到 NSWindow.noResponderFor(mouseDown:)",
              rawWindow.noResponderSelectors.contains("mouseDown:"),
              "selectors=\(rawWindow.noResponderSelectors)")
        check("正向对照：鼠标路由没有 keyDown 混入",
              !rawWindow.noResponderSelectors.contains("keyDown:"),
              "selectors=\(rawWindow.noResponderSelectors)")

        rawWindow.noResponderSelectors.removeAll()
        let before = rawWindow.keyDownHits
        sendKey("a", 0, in: rawWindow)
        after {
            self.check("正向对照：未处理按键能走到 NSWindow.keyDown",
                       self.rawWindow.keyDownHits > before,
                       "window +\(self.rawWindow.keyDownHits - before)")
            self.check("正向对照：未处理按键同时走到 noResponderFor(keyDown:)",
                       self.rawWindow.noResponderSelectors.contains("keyDown:"),
                       "selectors=\(self.rawWindow.noResponderSelectors)")
            self.rawWindow.orderOut(nil)
            self.window.makeKeyAndOrderFront(nil)
            self.chart.focusPage()
            self.productionMouse()
        }
    }

    // MARK: 生产视图

    private func productionMouse() {
        window.noResponderSelectors.removeAll()
        let keyBefore = window.keyDownHits

        geometry { geo in
            guard let geo else { return self.finish() }
            let wf = self.web.convert(self.web.bounds, to: nil)
            let blank = NSPoint(x: wf.minX + geo.blankX, y: wf.minY + self.weby(geo.blankY, wf.height))
            let checkbox = NSPoint(x: wf.minX + geo.checkboxX, y: wf.minY + self.weby(geo.checkboxY, wf.height))
            let toolbar = NSPoint(x: wf.midX, y: wf.maxY + 15)

            self.click(blank, in: self.window, type: .leftMouseDown)
            self.click(blank, in: self.window, type: .leftMouseUp)
            self.after {
                self.domState { state in
                    self.check("WebKit 空白点击由页面消费（DOM click +1）",
                               (state?["clicks"] as? Int) == 1,
                               "state=\(String(describing: state))")
                    self.check("WebKit 空白点击不落到 NSWindow.keyDown",
                               self.window.keyDownHits == keyBefore,
                               "window +\(self.window.keyDownHits - keyBefore)")
                    self.check("WebKit 空白点击不产生 noResponderFor(keyDown:)",
                               self.window.keyDownNoResponderCount() == 0,
                               "selectors=\(self.window.noResponderSelectors)")
                    self.check("WebKit 空白点击不改变页面焦点",
                               (self.window.firstResponder as AnyObject?) === self.web,
                               "firstResponder=\(String(describing: self.window.firstResponder))")

                    self.click(toolbar, in: self.window, type: .leftMouseDown)
                    self.click(toolbar, in: self.window, type: .leftMouseUp)
                    self.after {
                        self.check("原生顶栏空白点击不落到 NSWindow.keyDown",
                                   self.window.keyDownHits == keyBefore,
                                   "window +\(self.window.keyDownHits - keyBefore)")
                        self.check("原生顶栏空白点击不产生 noResponderFor(keyDown:)",
                                   self.window.keyDownNoResponderCount() == 0,
                                   "selectors=\(self.window.noResponderSelectors)")
                        self.check("原生顶栏空白点击不改变页面焦点",
                                   (self.window.firstResponder as AnyObject?) === self.web,
                                   "firstResponder=\(String(describing: self.window.firstResponder))")

                        self.click(checkbox, in: self.window, type: .leftMouseDown)
                        self.click(checkbox, in: self.window, type: .leftMouseUp)
                        self.after {
                            self.domState { state in
                                self.check("复选框点击切换 DOM 状态",
                                           (state?["checked"] as? Bool) == true
                                           && (state?["mode"] as? String) == "task",
                                           "state=\(String(describing: state))")
                                self.check("复选框点击不被父容器吞掉（DOM click 继续累加）",
                                           (state?["clicks"] as? Int) == 2,
                                           "state=\(String(describing: state))")
                                self.check("复选框点击不落到 NSWindow.keyDown",
                                           self.window.keyDownHits == keyBefore,
                                           "window +\(self.window.keyDownHits - keyBefore)")
                                self.keyboardStillRouted()
                            }
                        }
                    }
                }
            }
        }
    }

    private func keyboardStillRouted() {
        chart.focusPage()
        let keyBefore = window.keyDownHits
        let parentBefore = mouseRoutingParentKeyHits
        sendKey("a", 0, in: window)
        after {
            self.check("未处理普通键仍冒泡到 KeyFallbackView",
                       mouseRoutingParentKeyHits == parentBefore + 1,
                       "parent +\(mouseRoutingParentKeyHits - parentBefore)")
            self.check("KeyFallbackView 吞掉普通键，不落到 NSWindow.keyDown",
                       self.window.keyDownHits == keyBefore,
                       "window +\(self.window.keyDownHits - keyBefore)")
            self.check("键盘回归后焦点仍在页面",
                       (self.window.firstResponder as AnyObject?) === self.web,
                       "firstResponder=\(String(describing: self.window.firstResponder))")
            self.copyKeyFallback()
        }
    }

    /// 夹具页面没有选区，模拟真实 popover 里「拖了半天没选中内容，再按 Cmd+C」
    /// 之后 AppKit 继续落到 NSWindow 的那一段。父容器只在页面明确无选区时
    /// 收掉这个未处理 keyDown；页面上报有选区时必须继续走 super。Cmd+X 作为
    /// 「没有吞掉所有 Command 组合」的对照。
    private func copyKeyFallback() {
        let keyBefore = window.keyDownHits
        sendCommandKey("c", 8)
        after {
            self.check("无选区 Cmd+C 由父容器收掉，不落到 NSWindow.keyDown",
                       self.window.keyDownHits == keyBefore,
                       "window +\(self.window.keyDownHits - keyBefore)")
            // 页面明确上报有选区：父容器不能再截胡，必须交回 super/NSWindow。
            self.web.hasPageSelection = true
            let beforeSelected = self.window.keyDownHits
            self.sendCommandKey("c", 8)
            self.after {
                self.web.hasPageSelection = false
                self.check("有选区上报时 Cmd+C 不被父容器吞掉，仍交回系统",
                           self.window.keyDownHits > beforeSelected,
                           "window +\(self.window.keyDownHits - beforeSelected)")
                let beforeX = self.window.keyDownHits
                self.sendCommandKey("x", 7)
                self.after {
                    self.check("Cmd+X 对照：没有被一起吞掉，仍落到 NSWindow.keyDown",
                               self.window.keyDownHits > beforeX,
                               "window +\(self.window.keyDownHits - beforeX)")
                    self.finish()
                }
            }
        }
    }

    private func sendCommandKey(_ chars: String, _ code: UInt16) {
        let stamp = ProcessInfo.processInfo.systemUptime
        for type in [NSEvent.EventType.keyDown, NSEvent.EventType.keyUp] {
            guard let event = NSEvent.keyEvent(
                with: type, location: .zero, modifierFlags: .command,
                timestamp: stamp, windowNumber: window.windowNumber, context: nil,
                characters: chars, charactersIgnoringModifiers: chars,
                isARepeat: false, keyCode: code) else { continue }
            NSApp.sendEvent(event)
        }
    }

    // MARK: 工具

    private struct Geometry {
        let blankX: CGFloat
        let blankY: CGFloat
        let checkboxX: CGFloat
        let checkboxY: CGFloat
    }

    private func geometry(_ done: @escaping (Geometry?) -> Void) {
        let js = """
        (function(){
          var cb=document.getElementById('task-size');
          if(!cb) return null;
          var r=cb.getBoundingClientRect();
          return JSON.stringify({
            blankX: Math.round(innerWidth*0.55),
            blankY: Math.round(innerHeight*0.70),
            checkboxX: r.x + r.width/2,
            checkboxY: r.y + r.height/2
          });
        })()
        """
        web.evaluateJavaScript(js) { value, error in
            guard let raw = value as? String,
                  let data = raw.data(using: .utf8),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Double],
                  let bx = obj["blankX"], let by = obj["blankY"],
                  let cx = obj["checkboxX"], let cy = obj["checkboxY"] else {
                self.check("读取夹具页面几何", false,
                           error?.localizedDescription ?? "返回=\(String(describing: value))")
                done(nil)
                return
            }
            done(Geometry(blankX: bx, blankY: by, checkboxX: cx, checkboxY: cy))
        }
    }

    private func click(_ p: NSPoint, in window: NSWindow, type: NSEvent.EventType) {
        guard let event = NSEvent.mouseEvent(
            with: type, location: p, modifierFlags: [],
            timestamp: ProcessInfo.processInfo.systemUptime,
            windowNumber: window.windowNumber, context: nil,
            eventNumber: 1, clickCount: 1, pressure: 1) else { return }
        NSApp.sendEvent(event)
    }

    private func sendKey(_ chars: String, _ code: UInt16, in window: NSWindow) {
        let stamp = ProcessInfo.processInfo.systemUptime
        for type in [NSEvent.EventType.keyDown, NSEvent.EventType.keyUp] {
            guard let event = NSEvent.keyEvent(
                with: type, location: .zero, modifierFlags: [],
                timestamp: stamp, windowNumber: window.windowNumber, context: nil,
                characters: chars, charactersIgnoringModifiers: chars,
                isARepeat: false, keyCode: code) else { continue }
            NSApp.sendEvent(event)
        }
    }

    private func after(_ block: @escaping () -> Void) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.45, execute: block)
    }

    private func domState(_ done: @escaping ([String: Any]?) -> Void) {
        let js = """
        JSON.stringify({
          clicks: window.MR_CLICKS || 0,
          checked: document.getElementById('task-size').checked,
          mode: document.querySelector('svg').getAttribute('data-size-mode')
        })
        """
        web.evaluateJavaScript(js) { value, error in
            guard let raw = value as? String,
                  let data = raw.data(using: .utf8),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                self.check("读取页面状态", false,
                           error?.localizedDescription ?? "返回=\(String(describing: value))")
                done(nil)
                return
            }
            done(obj)
        }
    }

    /// CSS 坐标（原点在左上）转 NSView 坐标（原点在左下）。
    private func weby(_ cssY: CGFloat, _ height: CGFloat) -> CGFloat {
        height - cssY
    }

    private func check(_ label: String, _ condition: Bool, _ detail: String = "") {
        print("  \(condition ? "ok  " : "FAIL")  \(label)\(detail.isEmpty ? "" : " : \(detail)")")
        fflush(stdout)
        if !condition { failures += 1 }
    }

    private func finish() {
        print(failures == 0 ? "VERIFY MOUSE ROUTING OK" : "VERIFY MOUSE ROUTING FAILED (\(failures) 项)")
        fflush(stdout)
        NSApp.terminate(nil)
    }

    private static let html = """
    <!doctype html><html><head><meta charset="utf-8"><style>
    html,body{margin:0;height:100%;overflow:hidden;font:13px -apple-system}
    .sizebar{height:26px;padding:3px 12px;box-sizing:border-box}
    .wrap{height:calc(100% - 26px);background:#fff}
    svg{display:block;width:100%;height:100%}
    </style></head><body tabindex="-1">
    <div class="sizebar"><label><input type="checkbox" id="task-size" role="switch"> 任务耗时</label>
    <span id="size-caption">speed</span></div>
    <div class="wrap"><svg data-size-mode="speed"><rect width="100%" height="100%" fill="transparent"/></svg></div>
    <script>
    window.MR_CLICKS = 0;
    window.MR_LAST = '';
    document.addEventListener('click', function(e){
      window.MR_CLICKS += 1;
      window.MR_LAST = e.target && e.target.tagName;
    }, true);
    document.getElementById('task-size').addEventListener('change', function(e){
      var task = !!e.target.checked;
      document.querySelector('svg').setAttribute('data-size-mode', task ? 'task' : 'speed');
      document.getElementById('size-caption').textContent = task ? 'task' : 'speed';
      try { e.target.focus(); } catch (err) {}
    });
    </script>
    </body></html>
    """
}

@main
struct VerifyMouseRouting {
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)

        let window = MouseRoutingWindow(
            contentRect: NSRect(x: 0, y: 0, width: 748, height: 600),
            styleMask: [.borderless], backing: .buffered, defer: false)
        let rawWindow = MouseRoutingWindow(
            contentRect: NSRect(x: 0, y: 0, width: 200, height: 120),
            styleMask: [.borderless], backing: .buffered, defer: false)
        let probe = MouseRoutingProbe(window: window, rawWindow: rawWindow)
        app.delegate = probe
        app.activate(ignoringOtherApps: true)
        probe.start()

        DispatchQueue.main.asyncAfter(deadline: .now() + 20) {
            print("VERIFY MOUSE ROUTING TIMEOUT")
            fflush(stdout)
            NSApp.terminate(nil)
        }
        app.run()
        exit(probe.failures == 0 ? 0 : 1)
    }
}
