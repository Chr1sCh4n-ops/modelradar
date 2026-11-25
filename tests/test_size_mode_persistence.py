"""大小模式偏好的接线契约：状态栏走 UserDefaults，看板走带版本键的 localStorage。

行为回归在 verify_dashboard.mjs（真 SVG / 真 onchange / 真存储桩）。这里守的是
**接线**：任何一半改了名字、注入时机或默认值，页面看起来仍能切换，只是重启后
悄悄忘掉 —— 那种失败没有报错，必须靠源码契约当场拦住。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWIFT = ROOT / "widget" / "ChartWidget.swift"
RENDERER = ROOT / "render_scatter_svg.py"
DASHBOARD = ROOT / "dashboard.html"


class SwiftUserDefaultsWiring(unittest.TestCase):
    def setUp(self):
        self.src = SWIFT.read_text(encoding="utf-8")

    def test_缺省是速度模式且键名固定(self):
        self.assertIn('static let key = "sizeTaskOn"', self.src)
        # object(forKey:) 区分"没写过"与"写过 false"；?? false 保证从未设置时是速度模式。
        self.assertRegex(
            self.src,
            r"defaults\.object\(forKey: key\) as\? Bool \?\? false",
        )

    def test_注入在文档开始时且在加载之前(self):
        inject = re.search(
            r"private func injectSizeTask\(\)\s*\{(.*?)\n    \}", self.src, re.S)
        self.assertIsNotNone(inject, "找不到 injectSizeTask()")
        body = inject.group(1)
        self.assertIn("SizeTaskPref.isOn()", body)
        self.assertIn(".atDocumentStart", body)
        self.assertIn("SizeTaskPref.script(on: on)", body)

        load = re.search(r"func load\(\)\s*\{(.*?)web\.loadHTMLString", self.src, re.S)
        self.assertIsNotNone(load, "找不到 load()")
        self.assertIn("injectSizeTask()", load.group(1),
                      "load() 必须在 loadHTMLString 之前注入初值")

    def test_变更消息只落盘不回推(self):
        handler = re.search(
            r"extension ChartView: WKScriptMessageHandler \{(.*?)\n\}", self.src, re.S)
        self.assertIsNotNone(handler, "找不到 WKScriptMessageHandler 扩展")
        body = handler.group(1)
        self.assertIn('sizeTaskMessageName = "mrSizeTask"', self.src)
        self.assertIn("SizeTaskPref.set(on)", body)
        # 页面已经先应用了模式；handler 再 evaluateJavaScript 会形成事件环。
        self.assertNotIn("evaluateJavaScript", body)


class SizeJSContract(unittest.TestCase):
    def setUp(self):
        src = RENDERER.read_text(encoding="utf-8")
        m = re.search(r'SIZE_JS = """(.*?)"""', src, re.S)
        self.assertIsNotNone(m, "找不到 SIZE_JS")
        self.js = m.group(1)

    def test_初始化读宿主初值并恢复勾选_不写回(self):
        self.assertIn("window.MR_SIZE_TASK", self.js)
        self.assertIn("box.checked = wantTask;", self.js)
        self.assertIn("apply(wantTask);", self.js)
        # 初始化路径必须在 change 监听之前，且 change 之外没有回传。
        self.assertLess(self.js.index("apply(wantTask)"), self.js.index("addEventListener"))
        self.assertEqual(self.js.count("postMessage"), 1)
        init = self.js[:self.js.index("function save(")]
        self.assertNotIn("postMessage", init)
        self.assertNotIn("save('", init)

    def test_变更经原生桥回传且无桥时静默(self):
        self.assertIn("messageHandlers[name]", self.js)
        self.assertIn("postMessage(value ? 1 : 0)", self.js)
        self.assertIn("save('mrSizeTask', task)", self.js)
        self.assertIn("try {", self.js)
        self.assertIn("} catch (e) {}", self.js)

    def test_切换后把键盘焦点留在开关上(self):
        # macOS WebKit 点击复选框不改键盘焦点；不补 focus()，随后按空格会落到 BODY，
        # 被 WebKit 退回 AppKit 后触发 NSBeep。
        self.assertIn("box.focus();", self.js)
        self.assertLess(self.js.index("apply(task);"), self.js.index("box.focus();"))
        self.assertLess(self.js.index("box.focus();"), self.js.index("save('mrSizeTask', task)"))

    def test_惩罚偏好独立且只在任务模式显示(self):
        self.assertIn("window.MR_SIZE_PENALTY", self.js)
        self.assertIn("penBox.checked = wantPen", self.js)
        self.assertIn("save('mrSizePenalty', !!penBox.checked)", self.js)
        self.assertIn("if (task) penWrap.removeAttribute('hidden')", self.js)
        self.assertIn("else penWrap.setAttribute('hidden', '')", self.js)
        self.assertLess(self.js.index("penBox.focus();"),
                        self.js.index("save('mrSizePenalty', !!penBox.checked)"))

    def test_不在页面里另存一份偏好(self):
        # 状态栏的唯一真源是 UserDefaults；页面再写 localStorage 会造成两份状态打架。
        self.assertNotIn("localStorage", self.js)


class ChartKeyBeepWiring(unittest.TestCase):
    """未处理按键必须由父容器兜底，且不能吞掉 Tab / 全局快捷键。

    已装实例的 beep 路径（实测）：页面不处理 keyDown → WebKit 调
    `_web_superKeyDown:` → `NSWindow.keyDown:` → `NSBeep`。这里守的是
    ChartWebView 不覆写 keyDown（页面先拿到空格/Escape），父容器
    KeyFallbackView 只在按键冒泡回来后吞掉未消费键，并保留焦点遍历与快捷组合。
    """

    def setUp(self):
        self.src = SWIFT.read_text(encoding="utf-8")

    def _body(self, pattern, label):
        m = re.search(pattern, self.src, re.S)
        self.assertIsNotNone(m, f"找不到 {label}")
        return m.group(1)

    def test_WKWebView不覆写keyDown_页面先拿到按键(self):
        body = self._body(
            r"final class ChartWebView: WKWebView \{(.*?)\n\}",
            "ChartWebView")
        self.assertNotIn("override func keyDown(with event: NSEvent)", body,
                         "WebView 覆写 keyDown 会在页面拿到按键前吞掉空格/Escape")
        # 根视图必须是父容器兜底，否则冒泡回来的未消费键没有接应。
        self.assertIn("let root = KeyFallbackView()", self.src)

    def test_未消费按键冒泡到父容器再吞掉(self):
        body = self._body(
            r"final class KeyFallbackView: NSView \{(.*?)\n\}",
            "KeyFallbackView")
        self.assertIn("override func keyDown(with event: NSEvent)", body)
        guard = re.search(
            r"guard ChartWebView\.shouldSwallowUnhandledKeyDown\(event\) else \{(.*?)\n        \}",
            body, re.S)
        self.assertIsNotNone(guard, "父容器必须先用纯函数判定再分流")
        self.assertIn("super.keyDown(with: event)", guard.group(1))
        # 吞掉分支不能调用 super，否则仍会走到 NSWindow.keyDown → NSBeep。
        self.assertEqual(body.count("super.keyDown(with: event)"), 1)

    def test_Tab与修饰键仍交回系统(self):
        body = self._body(
            r"static func shouldSwallowUnhandledKeyDown\(_ event: NSEvent\) -> Bool\s*\{(.*?)\n    \}",
            "shouldSwallowUnhandledKeyDown")
        self.assertIn("event.keyCode == 48", body)
        self.assertIn(".command", body)
        self.assertIn(".control", body)
        self.assertIn(".option", body)

    def test_popover打开后把键盘焦点交给页面(self):
        show = self._body(
            r"private func showPopover\(source: NSStatusBarButton\)\s*\{(.*?)\n    \}",
            "showPopover")
        self.assertIn("chart.focusPage()", show)
        self.assertLess(show.index("chart.load()"), show.index("chart.focusPage()"))
        focus = self._body(r"func focusPage\(\)\s*\{(.*?)\n    \}", "focusPage")
        self.assertIn("makeFirstResponder(web)", focus)

    def test_鼠标事件不交给按键兜底(self):
        # 空白点击的告警证据在键盘侧（系统声音请求总跟在 keyDown 后）；鼠标空白
        # 点击由 WebKit/NSWindow 的 mouseDown 路由处理，默认 noResponder 只在
        # keyDown 上 NSBeep。父容器若覆写 mouseDown 吞事件，会先吃掉复选框、
        # 卡片和菜单的点击 —— 这是回归，不是修复。
        body = self._body(
            r"final class KeyFallbackView: NSView \{(.*?)\n\}",
            "KeyFallbackView")
        self.assertNotIn("override func mouseDown", body)
        self.assertNotIn("override func rightMouseDown", body)
        self.assertNotIn("override func otherMouseDown", body)

    def test_只在页面明确没有选区时吞CmdC(self):
        # 实测：拖完没有选区再按 Cmd+C，WebKit 不复制，AppKit 继续落到
        # NSWindow 并 NSBeep（开启「闪烁屏幕代替提示音」就是整屏闪）。
        # 有选区时必须继续交回 WebKit/系统；父容器只处理已冒泡回来的未处理
        # keyDown，不能提前在 performKeyEquivalent 里截胡复制。
        body = self._body(
            r"final class KeyFallbackView: NSView \{(.*?)\n\}",
            "KeyFallbackView")
        self.assertIn("hasPageSelection != true", body)
        self.assertIn("event.keyCode == 8", body)
        self.assertIn("mods == .command", body)
        self.assertNotIn("performKeyEquivalent", body)

    def test_页面通过消息上报选区(self):
        self.assertIn('selectionMessageName = "mrSelection"', self.src)
        # 脚本是参数化的：通道名由调用方给，夹具才能挂到别的名字上观察。
        # 因此断言"通道名被插进了脚本"，而不是断言写死的字面量。
        self.assertRegex(
            self.src,
            r"window\.webkit\.messageHandlers\.\\\(handler\)\.postMessage",
        )
        self.assertIn("handler: Self.selectionMessageName", self.src)
        self.assertIn("web.hasPageSelection = on", self.src)

    def test_换文档时清选区状态的三层都在(self):
        # 少任何一层都会留下"已换文档、宿主仍以为有选区"的窗口，
        # 未处理的 Cmd+C 就冒泡到 NSWindow 触发 NSBeep（整屏闪）。
        self.assertIn("func resetPageSelection(", self.src)
        self.assertIn("injectionTime: .atDocumentStart", self.src)
        self.assertIn("didStartProvisionalNavigation", self.src)
        self.assertIn("message.frameInfo.isMainFrame", self.src)


class DashboardStorageWiring(unittest.TestCase):
    def setUp(self):
        self.src = DASHBOARD.read_text(encoding="utf-8")

    def test_键名带版本且与状态栏隔离(self):
        self.assertIn("modelradar.dashboard.sizeMode.v1", self.src)
        self.assertNotIn("sizeTaskOn", self.src)

    def test_读写都有catch_禁用存储退回速度(self):
        self.assertRegex(self.src, r"try \{ return localStorage\.getItem\(SIZE_MODE_KEY\)")
        self.assertRegex(self.src, r"catch \(e\) \{ return false; \}")
        self.assertRegex(self.src, r"try \{ localStorage\.setItem\(SIZE_MODE_KEY")
        self.assertIn("catch (e) {}", self.src)

    def test_初始化恢复勾选_只在用户改变时写入(self):
        self.assertIn("sizeToggle.checked = sizeByTask;", self.src)
        self.assertNotIn("sizeToggle.checked = false;", self.src)
        self.assertIn("saveSizeMode(on);", self.src)
        self.assertEqual(self.src.count("saveSizeMode(on);"), 1)


if __name__ == "__main__":
    unittest.main()
