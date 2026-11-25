/*
 verify_key_beep.swift —— 未处理按键兜底的回归夹具

 与 ChartWidget.swift 同模块编译，直接调用 ChartWebView.shouldSwallowUnhandledKeyDown，
 不复制一份判定。实际路由是：WebView 先让页面处理，页面不要的键才冒泡到父容器
 KeyFallbackView；那里吞掉普通键，避免继续落到 `NSWindow.keyDown:` → `NSBeep`。
 这里守的是"判断本身正确：普通键吞掉，Tab 与 Command/Control/Option 组合仍交回系统"。

 用法：
   swiftc -O -o /tmp/verify_key_beep \
     widget/ChartWidget.swift widget/verify_key_beep.swift \
     -framework AppKit -framework WebKit -framework OSLog -framework UserNotifications
   /tmp/verify_key_beep
*/
import AppKit

@main
struct VerifyKeyBeep {
    static var fails = 0

    static func check(_ label: String, _ cond: Bool, _ detail: String = "") {
        print("  \(cond ? "ok  " : "FAIL")  \(label)\(detail.isEmpty ? "" : " : \(detail)")")
        fflush(stdout)
        if !cond { fails += 1 }
    }

    static func event(_ chars: String, _ code: UInt16,
                      _ mods: NSEvent.ModifierFlags = []) -> NSEvent {
        NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: mods,
                         timestamp: 0, windowNumber: 0, context: nil,
                         characters: chars, charactersIgnoringModifiers: chars,
                         isARepeat: false, keyCode: code)!
    }

    static func main() {
        check("普通字母：吞掉（页面返回 unhandled 时不再 NSBeep）",
              ChartWebView.shouldSwallowUnhandledKeyDown(event("a", 0)))
        check("空格落在 BODY：吞掉（点击复选框后按空格不再响）",
              ChartWebView.shouldSwallowUnhandledKeyDown(event(" ", 49)))
        check("Escape：吞掉（无响应者时也不响）",
              ChartWebView.shouldSwallowUnhandledKeyDown(event("\u{1b}", 53)))
        check("方向键：吞掉（避免滚动同时触发系统提示音）",
              ChartWebView.shouldSwallowUnhandledKeyDown(event("\u{f702}", 123)))
        check("Shift + 字母：仍吞掉（只是普通输入）",
              ChartWebView.shouldSwallowUnhandledKeyDown(event("A", 0, .shift)))

        check("Tab：交回系统（焦点遍历不能丢）",
              !ChartWebView.shouldSwallowUnhandledKeyDown(event("\t", 48)))
        check("Shift-Tab：交回系统",
              !ChartWebView.shouldSwallowUnhandledKeyDown(event("\t", 48, .shift)))
        check("Command 组合：交回系统（菜单快捷键不能丢）",
              !ChartWebView.shouldSwallowUnhandledKeyDown(event("r", 15, .command)))
        check("Control 组合：交回系统",
              !ChartWebView.shouldSwallowUnhandledKeyDown(event("c", 8, .control)))
        check("Option 组合：交回系统",
              !ChartWebView.shouldSwallowUnhandledKeyDown(event("a", 0, .option)))

        print(fails == 0 ? "VERIFY OK" : "VERIFY FAILED (\(fails) 项)")
        exit(fails == 0 ? 0 : 1)
    }
}
