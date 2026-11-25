#!/usr/bin/env swift
/*
 verify_reload_menu.swift —— 右键「Reload」修复的回归夹具（与 TokenRadar 同一机制，各自一份）

 症状：popover 里右键 → Reload → 页面变成 "cannot open file" 错误页。
 根因（两层都成立才构成这个 bug）：
   1. 页面是 loadHTMLString(读出的 chart.html + 注入 CSS) 拼出来的，WebKit 手里
      没有可再导航的主资源，历史项的 URL 就是那个 baseURL；
   2. `loadHTMLString` 的 baseURL **只用于相对解析与同源判定，不授予读权限**
      （授权只有 loadFileURL(_:allowingReadAccessTo:) 才做）。
   于是 reload 会向一个未授权、本来也不该当资源加载的 file URL 再发一次请求 → 错误页。

 本夹具测的是**子类拦截**这条路，验四件事：
   A. WKWebView 确实响应这四个 selector（菜单/硬重载/代码调用各有其一）；
   B. 覆写后四个都进我们的回调；
   C. 触发覆写时**没有**发生 WebKit 导航（用真实 WKNavigationDelegate 计数，
      不是只看回调计数 —— 上一版就是拿 hit 数冒充"没导航"）；
   D. 走 NSResponder 链派发（菜单项 target=nil 的真实路径）。非 GUI 会话里
      sendAction 可能没有 key window 可走链，那种情况**明确 skip**，
      不再用第二次 perform 冒充响应链测试。

 用法：swift widget/verify_reload_menu.swift
 */
import Cocoa
import WebKit

final class FakeWebView: WKWebView {
    var onReload: (() -> Void)?
    private func intercept() -> Bool {
        if let onReload { onReload(); return true }
        return false
    }
    override func reload() -> WKNavigation? { intercept() ? nil : super.reload() }
    @objc override func reload(_ sender: Any?) { if !intercept() { super.reload(sender) } }
    override func reloadFromOrigin() -> WKNavigation? { intercept() ? nil : super.reloadFromOrigin() }
    @objc override func reloadFromOrigin(_ sender: Any?) {
        if !intercept() { super.reloadFromOrigin(sender) }
    }
}

/// 真实导航计数：任何一次主资源/导航启动都会被记下来，用来证伪"没导航"这句断言。
final class NavSpy: NSObject, WKNavigationDelegate {
    var started = 0
    var provisional = 0
    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        provisional += 1
    }
    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) { started += 1 }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        started += 1
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) { provisional += 1 }
}

var fails = 0
func check(_ label: String, _ cond: Bool, _ detail: String = "") {
    print("  \(cond ? "ok  " : "FAIL")  \(label)\(detail.isEmpty ? "" : " : \(detail)")")
    fflush(stdout)   // 崩溃会把缓冲区的输出全吞掉，之前定位不到死在哪一步
    if !cond { fails += 1 }
}
func skip(_ label: String, _ why: String) {
    print("  skip  \(label) : \(why)"); fflush(stdout)
}

let sels = ["reload", "reload:", "reloadFromOrigin", "reloadFromOrigin:"]

// A) 这四个 selector 在 WKWebView 上都真实存在（不是我们从文档里猜的）
let base = WKWebView(frame: .zero)
let responding = sels.filter { base.responds(to: Selector(($0))) }
check("基类响应全部四个 reload selector", responding.count == sels.count, responding.joined(separator: " "))

let spy = NavSpy()
let wv = FakeWebView(frame: .zero)
wv.navigationDelegate = spy
var hit = 0
wv.onReload = { hit += 1 }
// 先提交一个带 file baseURL 的字符串页（复刻组件的真实加载方式）
wv.loadHTMLString("<html><body><p>x</p></body></html>",
                  baseURL: URL(fileURLWithPath: "/tmp/__definitely_not_here__/chart.html"))
RunLoop.main.run(until: Date().addingTimeInterval(0.8))   // 让首次加载的回调先落定

// B + C) 四个入口逐个触发：回调必须跑，且 WebKit 不得开始任何导航
let before = (spy.started, spy.provisional)
func fire(_ target: NSObject, _ name: String) {
    // 带冒号的是 action 形式，**必须带 sender**：用无参 perform 调 `reload:`
    // 在 ObjC 里是未定义行为，实测直接 SIGTRAP（第一版夹具就是这么崩的）。
    let sel = Selector((name))
    if name.hasSuffix(":") { target.perform(sel, with: nil) } else { target.perform(sel) }
}
for s in sels {
    hit = 0
    fire(wv, s)
    check("覆写截获 \(s)", hit == 1, "hit=\(hit)")
}

// C) 每个入口单独比对：触发前后导航计数不得变化。
//    基线要等**首次 loadHTMLString 的异步回调落定**之后再取，
//    否则会把自己那次加载算成"触发了导航"（第一版夹具就是这么误报的）。
RunLoop.main.run(until: Date().addingTimeInterval(0.8))
for s in sels {
    let b = (spy.started, spy.provisional)
    fire(wv, s)
    RunLoop.main.run(until: Date().addingTimeInterval(0.4))
    check("触发 \(s) 没引发 WebKit 导航",
          spy.started == b.0 && spy.provisional == b.1,
          "didCommit \(b.0)→\(spy.started)，provisional \(b.1)→\(spy.provisional)")
}

// 兜底分支：没接 onReload 时必须**退回** super（也就是会发起导航），
// 这条同时证明"错误页的来源确实是 WebKit 自己再去够 baseURL"。
let bare = FakeWebView(frame: .zero)
let bareSpy = NavSpy()
bare.navigationDelegate = bareSpy
bare.loadHTMLString("<html><body><p>y</p></body></html>",
                    baseURL: URL(fileURLWithPath: "/tmp/__definitely_not_here__/chart.html"))
let p0 = bareSpy.provisional
fire(bare, "reload:")
RunLoop.main.run(until: Date().addingTimeInterval(0.5))
check("未接 onReload 时会退回 WebKit 导航（所以接线不能丢）",
      bareSpy.provisional > p0, "provisional \(p0)→\(bareSpy.provisional)")

// D) 响应链派发需要真机 GUI（裸可执行文件里 WKWebView + NSWindow 起不来，
//    实测直接不返回）。这里只做**静态等价**断言：菜单项的 action 必须正是我们覆写的那个
//    selector，剩下的"真机右键一次"由人来做——比写一条假的响应链测试诚实。
let menuItem = NSMenuItem(title: "Reload", action: Selector(("reload:")), keyEquivalent: "r")
check("菜单项 action 就是被覆写的 reload:",
      menuItem.action == Selector(("reload:")) && wv.responds(to: menuItem.action!))
skip("响应链实际派发", "裸可执行文件里 WKWebView/NSWindow 起不来；请在真机右键一次确认")

print(fails == 0 ? "\n全部通过（skip 项需在真机右键补一次）" : "\n有 \(fails) 项失败")
exit(fails == 0 ? 0 : 1)
