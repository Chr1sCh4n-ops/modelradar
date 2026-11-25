//  ModelRadar 状态栏组件入口。
//
//  ⚠️ 为什么不用 `@main` 挂在 AppDelegate 上（这是"组件没装上"的真正根因）：
//  `NSApplication.delegate` 是 **weak/unretained** 的。手写 `@main` 样板时，
//  `let d = AppDelegate(); NSApp.delegate = d` 里的 `d` 只活在那个作用域，
//  ARC 会在赋值后立刻回收它，于是 `applicationDidFinishLaunching` **永远不触发** ——
//  症状极具迷惑性：
//  实测对照（同一台机、同一个受限会话）：
//    · `@main` 版本      → 无回调、无日志、菜单栏无图标
//    · 本文件这种写法    → 回调到达、statusItem.isVisible=true、菜单栏可见
//  所以入口写成顶层代码，delegate 用全局常量持有：进程活着，它就一定活着。

import AppKit

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
