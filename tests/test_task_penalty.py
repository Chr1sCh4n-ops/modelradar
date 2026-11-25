"""Global score penalty calibration and explicit non-reasoning exclusion."""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_size as T
import fetch_model_data as F
import render_scatter_svg as R


def row(name, score, seconds, tier='high', family='a', speed=100):
    return dict(name=name, family=family, index=score, task_s=seconds,
                task_est_s=None, tier=tier, tok_s=speed)


class GlobalPenalty(unittest.TestCase):
    def test_nonreason_is_excluded_before_anchor_selection(self):
        rows = [row('low', 25, 10, 'low'), row('high', 50, 100, 'high')]
        baseline = T.fit_task_size_penalty(rows)
        # The outlier stays inside the existing time domain, but has the highest score.
        result = T.fit_task_size_penalty(rows + [row('off', 99, 50, 'non-reasoning')])
        self.assertAlmostEqual(result['lambda'], baseline['lambda'])
        self.assertEqual(result['excluded_non_reasoning'], 1)
        self.assertEqual(result['points'], 2)
        self.assertEqual(result['global_max_index'], 99)
        self.assertLess(T.size_factor(25, result), 1)
        # A non-reasoning point still receives the global factor when below the maximum.
        self.assertLess(T.size_factor(50, result), 1)

    def test_none_excluded_but_minimal_and_low_retained(self):
        for tier in ('none', 'non-reasoning', 'non reasoning with fallback'):
            self.assertTrue(T.is_no_reasoning_tier(tier))
        for tier in ('minimal', 'min', 'low', '', 'high'):
            self.assertFalse(T.is_no_reasoning_tier(tier))
        result = T.fit_task_size_penalty([
            row('minimal', 25, 10, 'minimal'), row('high', 50, 100),
            row('none', 80, 50, 'none')])
        self.assertGreater(result['lambda'], 0)
        self.assertEqual(result['points'], 2)

    def test_single_global_factor_and_order_independence(self):
        rows = [row('low', 25, 10, 'low'), row('high', 50, 100),
                row('singleton', 25, 30, family='b')]
        result = T.fit_task_size_penalty(rows)
        reverse = T.fit_task_size_penalty(list(reversed(rows)))
        self.assertEqual(result, reverse)
        factor = T.size_factor(25, result)
        self.assertGreater(factor, 0)
        self.assertLess(factor, 1)
        self.assertEqual(T.size_factor(50, result), 1)
        self.assertEqual(T.size_factor(100, result), 1)
        expected = (25 / 50) ** result['lambda']
        self.assertAlmostEqual(factor, expected)

    def test_no_fit_and_invalid_inputs_do_not_shrink(self):
        for rows in ([], [row('one', 50, 100)],
                     [row('off', 25, 10, 'non-reasoning'), row('high', 50, 100)]):
            result = T.fit_task_size_penalty(rows)
            self.assertEqual(result['lambda'], 0)
            self.assertEqual(T.size_factor(25, result), 1)
        for value in (None, True, '25', float('nan'), 0, -1):
            self.assertEqual(T.size_factor(value, {'lambda': 2, 'global_max_index': 50}), 1)

    def test_penalized_radius_preserves_missing_and_never_enlarges(self):
        for seconds in (10, 30, 100):
            for factor in (0, .1, .5, 1):
                self.assertLessEqual(T.task_radius_penalized(seconds, 10, 100, factor),
                                     T.task_radius(seconds, 10, 100))
        self.assertEqual(T.task_radius_penalized(None, 10, 100, .1), T.R_MISSING)
        points = [dict(y=25, tp=10), dict(y=50, tp=100), dict(y=25, tp=None)]
        R.apply_task_radii(points, {'lambda': 2, 'global_max_index': 50})
        self.assertLess(points[0]['r_task_pen'], points[0]['r_task'])
        self.assertEqual(points[2]['r_task_pen'], T.R_MISSING)

    def test_fetch_only_fits_plotted_models(self):
        models = [dict(name=r['name'], index=r['index'], tok_s=r['tok_s'],
                       time_per_task_s=r['task_s'], tier=r['tier'], series_key=r['family'],
                       apt_usd=1) for r in [row('low', 25, 10, 'low'), row('high', 50, 100)]]
        expected = F.task_size_penalty(models)
        hidden = dict(models[0], name='hidden', apt_usd=None, index=99)
        self.assertEqual(expected, F.task_size_penalty(models + [hidden]))


if __name__ == '__main__':
    unittest.main()
