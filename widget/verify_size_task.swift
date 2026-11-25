/*
 verify_size_task.swift —— 任务耗时大小模式原生持久化的回归夹具

 与 ChartWidget.swift 同模块编译，直接调用 SizeTaskPref，避免测试复制一套默认值。
 用独立 UserDefaults suite，不碰真实的 com.modelradar.chart 偏好。

 用法：
   swiftc -O -o /tmp/verify_size_task \
     widget/ChartWidget.swift widget/verify_size_task.swift \
     -framework AppKit -framework WebKit
   /tmp/verify_size_task
*/
import Foundation

@main
struct VerifySizeTask {
    static var fails = 0

    static func check(_ label: String, _ cond: Bool, _ detail: String = "") {
        print("  \(cond ? "ok  " : "FAIL")  \(label)\(detail.isEmpty ? "" : " : \(detail)")")
        fflush(stdout)
        if !cond { fails += 1 }
    }

    static func main() {
        let suite = "com.modelradar.sizeTask.verify.\(UUID().uuidString)"
        guard let defaults = UserDefaults(suiteName: suite) else {
            print("FAIL 无法创建临时 UserDefaults suite")
            exit(1)
        }
        defaults.removePersistentDomain(forName: suite)

        check("从未写入时默认速度（false），不能猜成任务模式",
              SizeTaskPref.isOn(in: defaults) == false)
        check("documentStart 初值 false → 0",
              SizeTaskPref.script(on: false) == "window.MR_SIZE_TASK=0;",
              SizeTaskPref.script(on: false))
        check("documentStart 初值 true → 1",
              SizeTaskPref.script(on: true) == "window.MR_SIZE_TASK=1;",
              SizeTaskPref.script(on: true))

        SizeTaskPref.set(true, in: defaults)
        check("写入任务模式后新读取仍是 true（重载/重启语义）",
              SizeTaskPref.isOn(in: defaults) == true)
        SizeTaskPref.set(false, in: defaults)
        check("显式写回速度后读取 false（与'从未设置'同样落到速度）",
              SizeTaskPref.isOn(in: defaults) == false)

        defaults.removePersistentDomain(forName: suite)
        print(fails == 0 ? "VERIFY OK" : "VERIFY FAILED (\(fails) 项)")
        exit(fails == 0 ? 0 : 1)
    }
}
