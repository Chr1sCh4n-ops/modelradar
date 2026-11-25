/*
 verify_crawl_rc2.swift —— 「立即抓取」退出码语义的回归夹具

 背景（2026-11-04 实机故障）：爬虫三源全失败时 crawl.sh 沿用抓取退出码 2
 （fetch_model_data.py：本轮 data_state 为 empty/stale），而 crawl.sh 的渲染是无条件
 执行的，所以磁盘上的 chart.html 仍然是**一张有效图**，只是如实标注成旧数据。
 组件却把 rc=2 一律当"抓取失败"：整张图被红纱盖住、文字标红、还亮未读角标 ——
 图看着坏了、悬停与点选也全被遮罩吃掉；而提示正文只是爬虫日志末尾（那几行往往是
 指纹/绘图收尾的噪音），看不出到底是哪个源挂了。

 本夹具直接调用 AppDelegate.crawlOutcome 这份生产裁决（不复制一份算法），覆盖：
   · rc=2 且图仍在 → 不标红、不挡交互、文案带真实失败源与旧数据时刻
   · rc=2 但图已缺失 → 退回真失败（没有可用内容可保）
   · 其他非零码（1/3/7）→ 真失败，不因"图还在"被误判成 warn
   · 非零码但数据已落地 → warn（收尾步骤失败，数据是新的）
   · 成功码 → ok 文案带更新时刻与耗时；数据源异常时 warn
   · 超时 → 数据已落地 warn，否则 failed

 用法：
   swiftc -O -o /tmp/verify_crawl_rc2 \
     widget/ChartWidget.swift widget/verify_crawl_rc2.swift \
     -framework AppKit -framework WebKit -framework OSLog -framework UserNotifications
   /tmp/verify_crawl_rc2
*/
import Foundation

@main
struct VerifyCrawlRc2 {
    static var fails = 0

    static func check(_ label: String, _ cond: Bool, _ detail: String = "") {
        print("  \(cond ? "ok  " : "FAIL")  \(label)" + (detail.isEmpty ? "" : "  [\(detail)]"))
        fflush(stdout)
        if !cond { fails += 1 }
    }

    /// 现场数据：老图 08:10 生成，AA 失败、openrouter 降级。
    static func call(_ outcome: AppDelegate.ScriptOutcome, rc: Int32, landed: Bool,
                     chart: Bool = true, at: String? = "2026-11-04 08:10:00",
                     state: String? = "stale", tail: String = "",
                     sources: String? = "aa 失败：HTTP 503；openrouter 降级(旧快照)",
                     elapsed: Double = 41) -> AppDelegate.TaskOutcome {
        AppDelegate.crawlOutcome(outcome: outcome, exitCode: rc, landed: landed,
                                 chartExists: chart, generatedAt: at, dataState: state,
                                 tail: tail, sourceFailures: sources,
                                 elapsed: elapsed, timeoutSec: 300)
    }

    static func main() {
        print("== sourceFailureSummary：从真实 latest.json 形状里挑出可疑源 ==")
        let tmp = NSTemporaryDirectory() + "mr_verify_sources_\(getpid()).json"
        // 形状照抄 fetch_model_data.py 的 status 台账（aa 失败后被标记 stale + from）。
        let payload = """
        {"data_state":"stale","generated_at":"2026-11-04 17:23:46","sources":{
          "aa":{"ok":true,"stale":true,"err":"抓取失败 https://artificialanalysis.ai/leaderboards","from":"2026-11-04 17:23:46"},
          "aa_progress":{"ok":false,"err":"抓取失败 https://artificialanalysis.ai/trends: 网络总时长上限"},
          "aa_times":{"ok":false,"err":"抓取失败 https://artificialanalysis.ai/models: curl rc 28"},
          "openrouter":{"ok":true},
          "datalearner":{"ok":true}
        }}
        """
        try? payload.write(toFile: tmp, atomically: true, encoding: .utf8)
        let summary = AppDelegate.sourceFailureSummary(of: tmp)
        check("能读出可疑源", summary != nil, summary ?? "nil")
        check("指出 aa 降级/旧快照", summary?.contains("aa") == true, summary ?? "")
        check("旧快照（图停住的直接原因）排在失败副源之前",
              (summary?.range(of: "旧快照")?.lowerBound).map { a in
                  (summary?.range(of: "网络总时长")?.lowerBound).map { b in a < b } ?? true
              } ?? false, summary ?? "")
        check("不把健康的 openrouter 算进来", summary?.contains("openrouter") == false, summary ?? "")
        check("不把健康的 datalearner 算进来", summary?.contains("datalearner") == false, summary ?? "")
        check("超出 2 个时明确「另有 N 个」", summary?.contains("另") == true, summary ?? "")

        try? "{\"sources\":{\"openrouter\":{\"ok\":true}}}".write(toFile: tmp, atomically: true, encoding: .utf8)
        check("全部健康时返回 nil（调用方回退到脚本输出）",
              AppDelegate.sourceFailureSummary(of: tmp) == nil)
        try? "not json".write(toFile: tmp, atomically: true, encoding: .utf8)
        check("坏文件返回 nil，不编造原因", AppDelegate.sourceFailureSummary(of: tmp) == nil)
        check("文件不存在返回 nil", AppDelegate.sourceFailureSummary(of: tmp + ".nope") == nil)
        try? FileManager.default.removeItem(atPath: tmp)

        print("== rc=2：旧数据但图有效，绝不能画成失败、不能盖住图 ==")
        let r2 = call(.failure, rc: 2, landed: false,
                      tail: "fingerprint abc123 / chart 已写出")
        check("rc=2 不标红", !r2.isError, "isError=\(r2.isError)")
        check("rc=2 不遮挡（图保持可交互）", !r2.blocking, "blocking=\(r2.blocking)")
        check("rc=2 文案说明未取得新数据", r2.text.contains("未取得新数据"), r2.text)
        check("rc=2 文案给出真实失败源", r2.text.contains("aa 失败"),
              r2.text)
        check("rc=2 文案点明仍是旧数据", r2.text.contains("08:10"), r2.text)
        check("rc=2 不把爬虫收尾噪音当原因", !r2.text.contains("fingerprint"), r2.text)
        check("rc=2 不冒充成功", !r2.text.contains("抓取完成"), r2.text)

        print("== rc=2 没有可疑源时回退到脚本输出，仍如实说明 ==")
        let r2n = call(.failure, rc: 2, landed: false,
                       tail: "所有源均无数据", sources: nil)
        check("回退文案含脚本末行", r2n.text.contains("所有源均无数据"), r2n.text)
        check("回退仍不标红/不遮挡", !r2n.isError && !r2n.blocking)

        print("== rc=2 但图缺失：没有可保留的内容，必须退回真失败 ==")
        let r2m = call(.failure, rc: 2, landed: false, chart: false)
        check("图缺失时标红", r2m.isError)
        check("图缺失时遮挡（整图不可用）", r2m.blocking)
        check("图缺失时文案说抓取失败", r2m.text.contains("抓取失败 rc=2"), r2m.text)

        print("== 其他非零码：即便图还在也是真失败 ==")
        for rc: Int32 in [1, 3, 7] {
            let r = call(.failure, rc: rc, landed: false)
            check("rc=\(rc) 标红", r.isError, r.text)
            check("rc=\(rc) 文案带退出码", r.text.contains("rc=\(rc)"), r.text)
        }

        print("== 非零码但数据已落地：收尾失败，不能说成抓取失败 ==")
        let landedFail = call(.failure, rc: 1, landed: true,
                              at: "2026-11-04 13:20:00", state: "ok")
        check("落地后不标红", !landedFail.isError, landedFail.text)
        check("落地后说明数据已更新", landedFail.text.contains("数据已更新"), landedFail.text)
        check("落地后文案带新时刻", landedFail.text.contains("13:20"), landedFail.text)

        print("== 成功码 ==")
        let okOut = call(.success, rc: 0, landed: true,
                         at: "2026-11-04 13:20:00", state: "ok", elapsed: 38)
        check("成功不标红", !okOut.isError)
        check("成功说明抓取完成", okOut.text.contains("抓取完成"), okOut.text)
        check("成功带更新时刻与耗时", okOut.text.contains("13:20") && okOut.text.contains("38s"),
              okOut.text)
        let deg = call(.success, rc: 0, landed: true, state: "degraded")
        check("degraded 说明部分更新", deg.text.contains("部分更新"), deg.text)
        let weird = call(.success, rc: 0, landed: false, state: "stale")
        check("成功码但数据源异常只 warn", !weird.isError && !weird.blocking, weird.text)

        print("== 超时 ==")
        let tOut = call(.timeout, rc: -1, landed: false)
        check("超时未落地 → failed", tOut.isError && tOut.blocking, tOut.text)
        check("超时文案带上限秒数", tOut.text.contains("300"), tOut.text)
        let tLanded = call(.timeout, rc: -1, landed: true)
        check("超时但数据已落地 → warn", !tLanded.isError && !tLanded.blocking, tLanded.text)

        print()
        print(fails == 0 ? "VERIFY OK" : "VERIFY FAILED (\(fails) 项)")
        exit(fails == 0 ? 0 : 1)
    }
}
