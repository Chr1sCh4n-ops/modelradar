"""2026-11-04 用户要求按字段语义匹配，列变化不得静默污染历史。"""
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import sqlite3
from contextlib import closing

import fetch_model_data as F


class AAColumns(unittest.TestCase):
    def page(self, columns):
        headers = ''.join(f'<th>{name}</th>' for name, _ in columns)
        cells = ''.join(f'<td>{value}</td>' for _, value in columns)
        return f'<table><thead><tr>{headers}</tr></thead><tbody><tr>{cells}</tr></tbody></table>'

    def columns(self):
        return [('Model', '<a href="/models/model">Model</a>'), ('Context Window', '1M'),
                ('Creator', 'Acme'), ('Artificial Analysis Intelligence Index', '50'),
                ('Cost per Task USD', '$1'), ('Output Speed (t/s)', '100'),
                ('Latency First Chunk (s)', '0.5'), ('Total Response (s)', '5.5')]

    def test_reorder_and_unrelated_insert_do_not_shift_metrics(self):
        columns = list(reversed(self.columns()))
        columns.insert(2, ('Unrelated Metric', '999'))
        row = F.parse_aa(self.page(columns))[0]
        self.assertEqual((row['name'], row['index'], row['tok_s'], row['ttft_s'], row['total_s']),
                         ('Model', 50, 100, 0.5, 5.5))

    def test_deleted_optional_column_stays_missing(self):
        row = F.parse_aa(self.page(self.columns()[:-1]))[0]
        self.assertIsNone(row['total_s'])
        self.assertEqual(row['tok_s'], 100)

    def test_unknown_unit_duplicate_or_missing_header_rejects(self):
        columns = self.columns()
        columns[5] = ('Output Speed (minutes)', '100')
        for candidate in (columns, self.columns() + [('Output Speed (t/s)', '1')], self.columns()[1:]):
            with self.assertRaises(ValueError):
                F.parse_aa(self.page(candidate))

    def test_row_width_mismatch_rejects_instead_of_shifting(self):
        page = self.page(self.columns()).replace('</tbody>', '<tr><td>broken</td></tr></tbody>')
        with self.assertRaises(ValueError):
            F.parse_aa(page)

    def test_multiple_tables_choose_unique_semantic_table(self):
        page = self.page([('Other', '999')]) + self.page(self.columns())
        self.assertEqual(F.parse_aa(page)[0]['tok_s'], 100)
        with self.assertRaises(ValueError):
            F.parse_aa(page + self.page(self.columns()))

    # 2026-11-15：榜单改成"一个 release 一行 + 档位在 initialModels 里"。下面的
    # 夹具复刻真实结构：分组表头、7 列、Model 单元格里带 logo 与灰色档位 span。
    def new_page(self, models, rows):
        head = (
            '<thead><tr><th></th><th>Intelligence</th><th>Price</th><th>Speed</th>'
            '<th>Latency</th><th>Features</th><th></th></tr>'
            '<tr><th>Model</th><th>Artificial Analysis Intelligence Index</th>'
            '<th>Cost per Task USD</th><th>Time per Task Minutes</th>'
            '<th>First Answer Token (s)</th><th>Context Window</th>'
            '<th>Further Analysis</th></tr></thead>'
        )
        payload = json.dumps([1, json.dumps({"initialModels": models})])
        return (f'<table>{head}<tbody>{"".join(rows)}</tbody></table>'
                f'<script>self.__next_f.push({payload})</script>')

    def new_row(self, name, slug, index, cost, ctx="1M", creator="Acme", suffix=None):
        tier = (f' <span class="font-normal text-neutral-500">{suffix}</span>'
                if suffix else '')
        return (f'<tr><td><img alt="{creator}" src="/img/logos/acme.svg"/>'
                f'<a href="/models/{slug}">{name}</a>{tier}</td>'
                f'<td>{index}</td><td>{cost}</td><td>1</td><td>2</td><td>{ctx}</td><td></td></tr>')

    def test_new_layout_reads_all_tiers_from_initial_models(self):
        models = [
            {"release": {"slug": "alpha", "name": "Alpha"}, "name": "Alpha (High)",
             "shortName": "Alpha (high)", "suffix": "High", "modelCreatorName": "Acme",
             "contextWindowTokens": 1000000, "intelligenceIndex": 50.4,
             "intelligenceIndexCostPerTask": 0.04381, "outputSpeed": 10.0,
             "timeToFirstToken": 1.0, "timeToFirstAnswerToken": 2.0},
            {"release": {"slug": "alpha", "name": "Alpha"}, "name": "Alpha (Low)",
             "shortName": "Alpha (low)", "suffix": "Low", "modelCreatorName": "Acme",
             "contextWindowTokens": 1000000, "intelligenceIndex": 40.6,
             "intelligenceIndexCostPerTask": 2.215, "outputSpeed": 20.0,
             "timeToFirstToken": 3.0, "timeToFirstAnswerToken": 4.0},
            {"release": {"slug": "beta", "name": "Beta"}, "name": "Beta (max)",
             "shortName": "Beta (max)", "suffix": "Max", "modelCreatorName": "Beta Inc",
             "contextWindowTokens": 256000, "intelligenceIndex": None},
        ]
        page = self.new_page(models, [self.new_row("Alpha High", "alpha", "50", "$0.04", suffix="High")])
        rows = F.parse_aa(page)
        self.assertEqual([r["name"] for r in rows], ["Alpha (high)", "Alpha (low)"])
        high, low = rows
        self.assertEqual((high["creator"], high["ctx"], high["logo"]),
                         ("Acme", "1M", "/img/logos/acme.svg"))
        self.assertEqual((high["index"], high["apt_usd"], high["tok_s"],
                          high["ttft_s"], high["first_answer_s"]),
                         (50, 0.0438, 10.0, 1.0, 2.0))
        self.assertEqual((low["index"], low["apt_usd"]), (41, 2.22))

    def test_new_layout_rejects_model_without_release_slug(self):
        models = [{"name": "Alpha (High)", "shortName": "Alpha (high)",
                   "intelligenceIndex": 50}]
        page = self.new_page(models, [self.new_row("Alpha High", "alpha", "50", "$1")])
        with self.assertRaises(ValueError):
            F.parse_aa(page)

    def test_isolated_tps_keeps_trusted_display_without_history_write(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / 'history.db'
            with closing(sqlite3.connect(db)) as con:
                con.execute('CREATE TABLE snapshots(ts TEXT,source TEXT,ok INT,payload TEXT)')
                con.execute('INSERT INTO snapshots VALUES(?,?,1,?)', ('1', F.SRC_DIFF, json.dumps([
                    {'name': 'Model', 'tok_s': 100, 'evaluation_version': '5.0.1'}])))
                con.commit()
            model = {'name': 'Model', 'creator': 'Acme', 'tok_s': None, 'evaluation_version': '5.0.1'}
            payload = {'models': [model], 'diff_ts': '2'}
            rules = [{'name': 'Model', 'field': 'tok_s', 'from_ts': '2', 'to_ts': '2'}]
            with patch.object(F, 'DB_PATH', db), patch.object(F, 'load_quarantine', return_value=rules), \
                 patch.object(F, 'load_config', return_value={}), patch.object(F, '_atomic_write'), \
                 patch.object(F.mr_paths, 'ensure_dirs'), patch.object(F.mr_paths, 'publish_for_browser', return_value=[]), \
                 patch.object(F, 'estimate_missing_tok_s'), patch.object(F, 'tier_ladder', return_value=[]), \
                 patch.object(F, 'annotate_efficiency'), patch.object(F, 'build_legend', return_value=[]):
                F.write_outputs(payload, {})
            self.assertEqual((model['tok_s'], model['tok_s_asof']), (100, '1'))
            with closing(sqlite3.connect(db)) as con:
                self.assertEqual(con.execute('SELECT count(*) FROM snapshots').fetchone()[0], 1)
