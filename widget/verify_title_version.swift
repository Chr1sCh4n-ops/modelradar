/*
 verify_title_version.swift —— 顶栏标题版本后缀的回归夹具

 与 ChartWidget.swift 同模块编译，直接调用 ChartView 的实现，避免测试复制一份算法
 （复制出来的那份永远通过，真实代码改坏了也发现不了）。

 用法：
   swiftc -o /tmp/verify_title_version \
     widget/ChartWidget.swift widget/verify_title_version.swift \
     -framework AppKit -framework WebKit
   /tmp/verify_title_version

 覆盖：
   · 5.0 -> v5，4.3 -> v4.3，4.3.2 -> v4.3.2（大版本去掉结尾 .0）
   · 已带 v 前缀 / 前后空白 / 空字符串 / 缺失 / 非数字 -> 同一口径
   · latest.json 只认顶层 evaluation_version，坏 JSON 与嵌套字段都回退 nil
   · 若本机有真实 latest.json，再确认读取链路能拿到非空版本
*/
import Foundation

@main
struct VerifyTitleVersion {
    static var fails = 0
    static let base = "ModelRadar · 性价比定位图"

    static func check(_ label: String, _ got: String, _ want: String) {
        let ok = got == want
        print("  \(ok ? "ok  " : "FAIL")  \(label) : got=\(got) want=\(want)")
        fflush(stdout)
        if !ok { fails += 1 }
    }

    static func check(_ label: String, _ cond: Bool) {
        print("  \(cond ? "ok  " : "FAIL")  \(label)")
        fflush(stdout)
        if !cond { fails += 1 }
    }

    static func skip(_ label: String) {
        print("  skip  \(label)")
        fflush(stdout)
    }

    static func main() {
        // 展示归一化
        check("5.0", ChartView.titleText(evaluationVersion: "5.0"), base + " v5")
        check("4.3", ChartView.titleText(evaluationVersion: "4.3"), base + " v4.3")
        check("4.3.2", ChartView.titleText(evaluationVersion: "4.3.2"), base + " v4.3.2")
        check("5.0.0", ChartView.titleText(evaluationVersion: "5.0.0"), base + " v5")
        check("已带 v 前缀", ChartView.titleText(evaluationVersion: "v4.3"), base + " v4.3")
        check("前后空白", ChartView.titleText(evaluationVersion: " 4.3 "), base + " v4.3")
        check("空字符串", ChartView.titleText(evaluationVersion: ""), base)
        check("缺失", ChartView.titleText(evaluationVersion: nil), base)
        check("非数字", ChartView.titleText(evaluationVersion: "unknown"), base)

        // latest.json 解析：只认顶层字段
        let top = #"{"evaluation_version":"5.0","models":[]}"#.data(using: .utf8)!
        check("顶层 evaluation_version", ChartView.evaluationVersion(fromJSON: top) ?? "nil", "5.0")
        let nested = #"{"models":[{"evaluation_version":"4.3"}]}"#.data(using: .utf8)!
        check("嵌套字段不冒充顶层", ChartView.evaluationVersion(fromJSON: nested) ?? "nil", "nil")
        let malformed = Data("{".utf8)
        check("坏 JSON 回退", ChartView.evaluationVersion(fromJSON: malformed) ?? "nil", "nil")

        // 真实数据链路（没有数据目录时明确 skip，不伪装通过）
        if FileManager.default.fileExists(atPath: ChartPaths.latestJSON) {
            let v = ChartView.readEvaluationVersion()
            check("真实 latest.json 可读到版本", v != nil)
            if let v {
                let title = ChartView.titleText(evaluationVersion: v)
                check("真实标题带 v 后缀", title.hasPrefix(base + " v"))
                print("        实际版本：\(v)（\(ChartPaths.latestJSON)）")
                print("        实际标题：\(title)")
            }
        } else {
            skip("真实 latest.json 不存在：\(ChartPaths.latestJSON)")
        }

        print(fails == 0 ? "VERIFY OK" : "VERIFY FAILED (\(fails) 项)")
        exit(fails == 0 ? 0 : 1)
    }
}
