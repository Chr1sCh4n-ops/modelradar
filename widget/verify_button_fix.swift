#!/usr/bin/env swift
// 自动验证：状态栏组件跑脚本那套机制是否真的生效（可随时复跑：swift widget/verify_button_fix.swift）。
//
// 覆盖四件事，均复现 AppDelegate.runScript 的真实机制：
//   1) 不死锁：后台线程 waitUntilExit()（而非 terminationHandler + DispatchGroup.wait 的死锁写法）；
//   2) 超时判定确定性：terminationReason == .uncaughtSignal，不读跨线程裸 Bool（消 data race）；
//   3) 进程组整组击杀：超时 killpg(SIGKILL) 连带清掉 bash 的子孙（python/爬虫子进程），不留孤儿；
//   4) 非零退出必须判 .failure **且留下输出**：以前 stdout/stderr 喂给 nullDevice、`.failure`
//      一行日志都不写，于是"点了按钮没反应"根本无法归因 —— 这条是防它回归的哨兵。
//
// 退出码 0 = 全部通过；非 0 = 有失败项。
import Foundation
import Darwin

enum Outcome { case success, failure, timeout, launchFail }

struct Result {
    let outcome: Outcome
    let elapsed: Double
    let tail: String
}

/// 与 AppDelegate.runScript 等价的机制（仅用于验证，不依赖 AppKit）。
func runScript(_ path: String, timeoutSec: Int, label: String, logDir: String) -> Result {
    let start = Date()
    let outPath = logDir + "/.out.\(label)"
    try? FileManager.default.removeItem(atPath: outPath)
    FileManager.default.createFile(atPath: outPath, contents: nil)
    guard let fh = FileHandle(forWritingAtPath: outPath) else {
        return Result(outcome: .launchFail, elapsed: 0, tail: "打不开输出文件")
    }
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/bin/bash")
    proc.arguments = [path]
    proc.standardOutput = fh
    proc.standardError = fh            // 与 app 一致：两路合并进同一文件，顺序即时间顺序
    do { try proc.run() } catch {
        try? fh.close()
        return Result(outcome: .launchFail, elapsed: Date().timeIntervalSince(start),
                      tail: error.localizedDescription)
    }

    // 让子进程成为自己的进程组组长：killpg(pid) 才能连带清掉它的子孙（独立复核指出的孤儿问题）。
    let pid = proc.processIdentifier
    _ = setpgid(pid, pid)

    let wd = DispatchWorkItem { killpg(pid, SIGKILL) }
    DispatchQueue.global().asyncAfter(deadline: .now() + .seconds(timeoutSec), execute: wd)
    proc.waitUntilExit()
    wd.cancel()
    try? fh.close()

    let elapsed = Date().timeIntervalSince(start)
    // 本进程只会被看门狗以 SIGKILL 整组击杀，故 .uncaughtSignal == 超时（确定性，无跨线程标志）。
    let outcome: Outcome
    if proc.terminationReason == .uncaughtSignal {
        outcome = .timeout
    } else {
        outcome = proc.terminationStatus == 0 ? .success : .failure
    }
    let raw = (try? Data(contentsOf: URL(fileURLWithPath: outPath))) ?? Data()
    let lines = (String(data: raw, encoding: .utf8) ?? "")
        .split(whereSeparator: \.isNewline).map(String.init).filter { !$0.isEmpty }
    return Result(outcome: outcome, elapsed: elapsed,
                  tail: lines.suffix(2).joined(separator: " / "))
}

let support = (NSHomeDirectory() as NSString)
    .appendingPathComponent("Library/Application Support/ModelRadar")
let logDir = (NSHomeDirectory() as NSString)
    .appendingPathComponent("Library/Logs/ModelRadar")
try? FileManager.default.createDirectory(atPath: logDir, withIntermediateDirectories: true)
let regenerate = (support as NSString).appendingPathComponent("bin/regenerate.sh")

var failures = 0
func check(_ name: String, _ cond: Bool, _ detail: String) {
    print("  \(cond ? "✅" : "❌") \(name): \(detail)")
    if !cond { failures += 1 }
}
func lineCount(_ f: String) -> Int {
    guard let s = try? String(contentsOfFile: f, encoding: .utf8) else { return 0 }
    return s.split(separator: "\n").count
}
/// 写一个临时脚本夹具。先删后写：不删的话上一轮残留会让"心跳行数"判据失真。
func writeFixture(_ body: String, _ name: String) -> String {
    let p = "/tmp/\(name)"
    try? FileManager.default.removeItem(atPath: p)
    try! body.write(toFile: p, atomically: true, encoding: .utf8)
    try! Process.run(URL(fileURLWithPath: "/bin/chmod"), arguments: ["+x", p])
    return p
}

print("== 验证 1：秒回脚本（真实 regenerate.sh）不应死锁到 timeout ==")
if FileManager.default.fileExists(atPath: regenerate) {
    let r = runScript(regenerate, timeoutSec: 90, label: "verify_regen", logDir: logDir)
    check("regenerate.sh 结果", r.outcome == .success, "outcome=\(r.outcome), 耗时 \(String(format: "%.1f", r.elapsed))s")
    check("regenerate.sh 未死锁", r.elapsed < 10, "耗时 \(String(format: "%.1f", r.elapsed))s（旧死锁写法会卡到 90s）")
    check("输出被留下", !r.tail.isEmpty, "末两行=\(r.tail.prefix(80))")
} else {
    check("regenerate.sh 存在", false, "未找到 \(regenerate)（先跑 widget/build_chart_widget.sh）")
}

print("== 验证 2：卡死脚本应在 watchdog 超时点被终止，而非死锁到远超 timeout ==")
let hbFile = "/tmp/mr_verify_hb.txt"
try? FileManager.default.removeItem(atPath: hbFile)
// 前台 sleep 100，并在同进程组内后台起一个每 0.5s 写心跳的子孙：
// 若超时只杀父 bash（旧 terminate() 写法），子孙会继续写心跳 → 验证 3 抓出孤儿。
let hangScript = writeFixture("""
#!/bin/bash
( while true; do echo "$(date +%s)" >> "\(hbFile)"; sleep 0.5; done ) &
sleep 100
""", "mr_verify_hang.sh")
let r2 = runScript(hangScript, timeoutSec: 3, label: "verify_hang", logDir: logDir)
check("卡死脚本被 watchdog 终止", r2.outcome == .timeout, "outcome=\(r2.outcome), 耗时 \(String(format: "%.1f", r2.elapsed))s")
check("卡死脚本未死锁", r2.elapsed < 6, "耗时 \(String(format: "%.1f", r2.elapsed))s（旧写法会卡到 100s）")

print("== 验证 3：超时整组击杀，不留孤儿（子孙心跳应停止）==")
let c1 = lineCount(hbFile)
Thread.sleep(forTimeInterval: 1.5)
let c2 = lineCount(hbFile)
let added = c2 - c1
check("无孤儿进程", added < 2,
      "超时后 1.5s 内心跳新增 \(added) 行（<2 表示子孙已随整组击杀退出；旧 terminate() 写法会持续 +3 行）")

print("== 验证 4：非零退出要判 .failure，且 stderr 必须留下可读线索 ==")
let failScript = writeFixture("""
#!/bin/bash
echo "traceback: 假装的失败原因在 stderr" >&2
exit 7
""", "mr_verify_fail.sh")
let r4 = runScript(failScript, timeoutSec: 10, label: "verify_fail", logDir: logDir)
check("非零退出判为 .failure", r4.outcome == .failure, "outcome=\(r4.outcome)")
check("失败原因被捕获", r4.tail.contains("假装的失败原因"), "tail=\(r4.tail.prefix(80))")

for p in [hangScript, failScript, hbFile,
          logDir + "/.out.verify_regen", logDir + "/.out.verify_hang", logDir + "/.out.verify_fail"] {
    try? FileManager.default.removeItem(atPath: p)
}

print()
if failures == 0 {
    print("✅ 全部通过：不死锁 + 确定性超时 + 整组击杀（无孤儿）+ 失败留证 均生效。")
    exit(0)
} else {
    print("❌ \(failures) 项失败：修复未生效或回归。")
    exit(1)
}
