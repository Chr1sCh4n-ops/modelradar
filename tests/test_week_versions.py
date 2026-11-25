"""week_moves：按 (模型, 字段) 取窗口内最早的版本兼容基线。

沿用行与降级快照由抓取侧保证不写入 series；这里只验证 series 里已有观测的取基线与
跨版本跳过语义。
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import summarize as S

T1 = "2026-10-01 07:30:00"


class WeekMovesVersions(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute(
            "CREATE TABLE series(ts TEXT NOT NULL, name TEXT NOT NULL, field TEXT NOT NULL, "
            "value REAL, evaluation_version TEXT, PRIMARY KEY (ts, name, field))")

    def tearDown(self):
        self.con.close()

    def put(self, ts, name, field, value, version="5.0"):
        self.con.execute(
            "INSERT INTO series(ts,name,field,value,evaluation_version) VALUES(?,?,?,?,?)",
            (ts, name, field, value, version))

    def test_窗口跨旧版本时取当前版本最早基线(self):
        self.put("2026-09-24 07:30:00", "A", "index", 40, "4.3")
        self.put("2026-09-30 07:30:00", "A", "index", 50, "5.0")
        self.put(T1, "A", "index", 55, "5.0")
        self.assertEqual([(d["from"], d["to"], d["pct"]) for d in S.week_moves(self.con)],
                         [(50.0, 55.0, 10.0)],
                         "窗口里先有 v4 也必须拿最早一条 v5 当基线")

    def test_同版本没有更早观测时不跨版比较(self):
        self.put("2026-09-24 07:30:00", "A", "index", 40, "4.3")
        self.put(T1, "A", "index", 55, "5.0")
        self.assertEqual(S.week_moves(self.con), [], "没有同版本基线就不报，不拿 v4 凑数")

    def test_版本回退也按最新观测的版本取基线(self):
        self.put("2026-09-24 07:30:00", "A", "index", 60, "5.0")
        self.put("2026-09-29 07:30:00", "A", "index", 40, "4.3")
        self.put(T1, "A", "index", 45, "4.3")
        self.assertEqual([(d["from"], d["to"]) for d in S.week_moves(self.con)],
                         [(40.0, 45.0)], "最新是 v4 就取最早的 v4，不能回头拿 v5")

    def test_速度与价格不受版本门控但取最早(self):
        self.put("2026-09-24 07:30:00", "A", "tok_s", 60, "4.3")
        self.put("2026-09-30 07:30:00", "A", "tok_s", 70, "5.0")
        self.put(T1, "A", "tok_s", 90, "5.0")
        self.put("2026-09-24 07:30:00", "A", "in_per_1m_usd", 1.0, "4.3")
        self.put(T1, "A", "in_per_1m_usd", 2.0, "5.0")
        got = {d["field"]: d for d in S.week_moves(self.con)}
        self.assertEqual((got["tok_s"]["from"], got["tok_s"]["to"], got["tok_s"]["pct"]),
                         (60.0, 90.0, 50.0))
        self.assertEqual((got["in_per_1m_usd"]["from"], got["in_per_1m_usd"]["to"]),
                         (1.0, 2.0))

    def test_每个模型字段各取自己的基线(self):
        self.put("2026-09-25 07:30:00", "A", "index", 50, "5.0")
        self.put(T1, "A", "index", 55, "5.0")
        self.put("2026-09-24 07:30:00", "B", "index", 20, "4.3")
        self.put("2026-09-29 07:30:00", "B", "index", 30, "4.3")
        self.put(T1, "B", "index", 33, "4.3")
        got = {d["name"]: d for d in S.week_moves(self.con)}
        self.assertEqual((got["A"]["from"], got["A"]["to"]), (50.0, 55.0),
                         "A 不能拿全局最早那一轮（那里没有 A）当基线")
        self.assertEqual((got["B"]["from"], got["B"]["to"]), (20.0, 33.0),
                         "B 取自己窗口内最早一条 20")

    def test_最新一轮没有的模型不参与(self):
        self.put("2026-09-29 07:30:00", "B", "tok_s", 30)
        self.put("2026-09-30 07:30:00", "B", "tok_s", 40)
        self.put("2026-09-30 07:30:00", "A", "tok_s", 80)
        self.put(T1, "A", "tok_s", 90)
        self.assertEqual({d["name"] for d in S.week_moves(self.con)}, {"A"})

    def test_旧库没有版本列时安全返回空(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE series(ts TEXT, name TEXT, field TEXT, value REAL)")
        con.execute("INSERT INTO series VALUES('2026-09-24 07:30:00','A','tok_s',60)")
        con.execute("INSERT INTO series VALUES(?,'A','tok_s',90)", (T1,))
        self.assertEqual(S.week_moves(con), [])
        con.close()

    def test_零基线与单快照保持原语义(self):
        self.put("2026-09-24 07:30:00", "A", "apt_usd", 0)
        self.put(T1, "A", "apt_usd", 0.5)
        self.put(T1, "C", "tok_s", 90)
        self.assertEqual(S.week_moves(self.con), [])


if __name__ == "__main__":
    unittest.main()
