/*
 verify_key_routing.swift —— 按键路由的真 WKWebView 集成夹具

 守的是生产路由本身，不复制判定：
   · ChartWebView 不覆写 keyDown，页面先拿到空格/Escape/复选框按键；
   · 页面不要的普通键冒泡到 KeyFallbackView 后被吞掉，不再落到 NSWindow；
   · Tab 与 Command/Control/Option 组合仍由父容器交回系统；
    · 未消费的普通键不落到 NSWindow；不以无效的 NSBeep 拦截计数作为证据。

 使用真实 ChartWebView + 真实 KeyFallbackView（仅用进程内 swizzle 加计数观察路由），
 放在真实 key window 里，用合成 NSEvent 发送按键，不做 GUI 自动化。

 用法：
   swiftc -O -o /tmp/verify_key_routing \
     widget/ChartWidget.swift widget/verify_key_routing.swift \
     -framework AppKit -framework WebKit -framework OSLog -framework UserNotifications
   /tmp/verify_key_routing
*/
import AppKit
import WebKit

/// 在进程内把生产 KeyFallbackView 的 keyDown 包一层计数；交换后
/// `probeKeyDown` 调回原实现，判定与吞掉行为不变。
var keyRoutingParentHits = 0

extension KeyFallbackView {
    @objc dynamic func probeKeyDown(with event: NSEvent) {
        keyRoutingParentHits += 1
        probeKeyDown(with: event)
    }
}

final class KeyRoutingWindow: NSWindow {
    var keyDownHits = 0

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }

    override func keyDown(with event: NSEvent) {
        keyDownHits += 1
        super.keyDown(with: event)
    }
}

final class KeyRoutingProbe: NSObject, WKNavigationDelegate {
    private let window: KeyRoutingWindow
    private let parent = KeyFallbackView()
    private let web = ChartWebView()
    private(set) var failures = 0
    private var started = false

    init(window: KeyRoutingWindow) {
        self.window = window
        super.init()
    }

    func start() {
        let original = class_getInstanceMethod(
            KeyFallbackView.self, #selector(NSResponder.keyDown(with:)))!
        let replacement = class_getInstanceMethod(
            KeyFallbackView.self, #selector(KeyFallbackView.probeKeyDown(with:)))!
        method_exchangeImplementations(original, replacement)

        window.contentView = parent
        parent.addSubview(web)
        web.frame = parent.bounds
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self
        web.loadHTMLString(Self.html, baseURL: nil)
        window.makeKeyAndOrderFront(nil)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard !started else { return }
        started = true
        window.makeFirstResponder(web)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { self.checkboxSpace() }
    }

    private func checkboxSpace() {
        web.evaluateJavaScript("document.getElementById('task-size').focus()") { _, _ in
            let parentBefore = keyRoutingParentHits
            let windowBefore = self.window.keyDownHits
            self.send(" ", 49)
            self.after {
                self.snapshot { s in
                    self.check("复选框 Space 由页面处理并切换 DOM",
                               s["checked"] as? Bool == true && s["mode"] as? String == "task",
                               "state=\(s)")
                    self.check("复选框 Space 不冒泡到父容器",
                               keyRoutingParentHits == parentBefore,
                               "parent +\(keyRoutingParentHits - parentBefore)")
                    self.check("复选框 Space 不落到 NSWindow",
                               self.window.keyDownHits == windowBefore,
                               "window +\(self.window.keyDownHits - windowBefore)")
                    self.bodySpace()
                }
            }
        }
    }

    private func bodySpace() {
        web.evaluateJavaScript("document.body.focus()") { _, _ in
            let parentBefore = keyRoutingParentHits
            let windowBefore = self.window.keyDownHits
            self.send(" ", 49)
            self.after {
                self.snapshot { s in
                    self.check("BODY Space 页面收到 DOM keydown",
                               (s["bodySpace"] as? Int) == 1,
                               "state=\(s)")
                    self.check("BODY Space 未消费后冒泡到父容器",
                               keyRoutingParentHits == parentBefore + 1,
                               "parent +\(keyRoutingParentHits - parentBefore)")
                    self.check("BODY Space 被父容器吞掉，不落到 NSWindow",
                               self.window.keyDownHits == windowBefore,
                               "window +\(self.window.keyDownHits - windowBefore)")
                    self.escape()
                }
            }
        }
    }

    private func escape() {
        let parentBefore = keyRoutingParentHits
        let windowBefore = self.window.keyDownHits
        send("\u{1b}", 53)
        after {
            self.snapshot { s in
                self.check("Escape 页面收到 DOM keydown",
                           (s["escape"] as? Int) == 1,
                           "state=\(s)")
                self.check("Escape 未消费后冒泡到父容器",
                           keyRoutingParentHits == parentBefore + 1,
                           "parent +\(keyRoutingParentHits - parentBefore)")
                self.check("Escape 被父容器吞掉，不落到 NSWindow",
                           self.window.keyDownHits == windowBefore,
                           "window +\(self.window.keyDownHits - windowBefore)")
                self.allowedModifier()
            }
        }
    }

    private func allowedModifier() {
        let parentBefore = keyRoutingParentHits
        let windowBefore = self.window.keyDownHits
        send("a", 0, mods: .option)
        after {
            self.check("Option 组合未被父容器吞掉",
                       keyRoutingParentHits == parentBefore + 1,
                       "parent +\(keyRoutingParentHits - parentBefore)")
            self.check("Option 组合交回 NSWindow",
                       self.window.keyDownHits == windowBefore + 1,
                       "window +\(self.window.keyDownHits - windowBefore)")
            self.finish()
        }
    }

    private func send(_ chars: String, _ code: UInt16,
                      mods: NSEvent.ModifierFlags = []) {
        let stamp = ProcessInfo.processInfo.systemUptime
        for type in [NSEvent.EventType.keyDown, NSEvent.EventType.keyUp] {
            guard let event = NSEvent.keyEvent(
                with: type, location: .zero, modifierFlags: mods,
                timestamp: stamp, windowNumber: window.windowNumber, context: nil,
                characters: chars, charactersIgnoringModifiers: chars,
                isARepeat: false, keyCode: code) else { continue }
            NSApp.sendEvent(event)
        }
    }

    private func after(_ block: @escaping () -> Void) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.45, execute: block)
    }

    private func snapshot(_ done: @escaping ([String: Any]) -> Void) {
        let js = """
        JSON.stringify({
          checked: document.getElementById('task-size').checked,
          mode: document.querySelector('svg').getAttribute('data-size-mode'),
          bodySpace: window.MR_BODY_SPACE || 0,
          escape: window.MR_ESCAPE || 0
        })
        """
        web.evaluateJavaScript(js) { value, error in
            guard let raw = value as? String,
                  let data = raw.data(using: .utf8),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                self.check("读取页面状态", false,
                           error?.localizedDescription ?? "返回不是 JSON：\(String(describing: value))")
                done([:])
                return
            }
            done(obj)
        }
    }

    private func check(_ label: String, _ condition: Bool, _ detail: String = "") {
        print("  \(condition ? "ok  " : "FAIL")  \(label)\(detail.isEmpty ? "" : " : \(detail)")")
        fflush(stdout)
        if !condition { failures += 1 }
    }

    private func finish() {
        print(failures == 0 ? "VERIFY KEY ROUTING OK" : "VERIFY KEY ROUTING FAILED (\(failures) 项)")
        fflush(stdout)
        NSApp.terminate(nil)
    }

    private static let html = """
    <!doctype html><html><head><meta charset="utf-8"><style>
    html,body{margin:0;height:100%;overflow:hidden}
    body{font:13px -apple-system}
    </style></head><body tabindex="-1">
    <label><input type="checkbox" id="task-size" role="switch"> 任务耗时</label>
    <svg data-size-mode="speed" width="120" height="40"></svg>
    <script>
    window.MR_EVENTS=[];
    document.addEventListener('keydown', function(e){
      window.MR_EVENTS.push({key:e.key, code:e.keyCode, target:e.target && e.target.tagName});
    }, true);
    document.getElementById('task-size').addEventListener('change', function(e){
      document.querySelector('svg').setAttribute('data-size-mode', e.target.checked ? 'task' : 'speed');
    });
    window.addEventListener('keydown', function(e){
      if (e.key === 'Escape') window.MR_ESCAPE = (window.MR_ESCAPE || 0) + 1;
      if (e.key === ' ' && e.target === document.body) window.MR_BODY_SPACE = (window.MR_BODY_SPACE || 0) + 1;
    });
    </script>
    </body></html>
    """
}

@main
struct VerifyKeyRouting {
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)

        let window = KeyRoutingWindow(
            contentRect: NSRect(x: -4000, y: -4000, width: 500, height: 400),
            styleMask: [.borderless], backing: .buffered, defer: false)
        let probe = KeyRoutingProbe(window: window)
        app.activate(ignoringOtherApps: true)
        probe.start()

        DispatchQueue.main.asyncAfter(deadline: .now() + 20) {
            print("VERIFY KEY ROUTING TIMEOUT")
            fflush(stdout)
            NSApp.terminate(nil)
        }
        app.run()
        exit(probe.failures == 0 ? 0 : 1)
    }
}
