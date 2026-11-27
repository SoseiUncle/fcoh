import copy
import unittest

from fcoh.core import FcohError, describe, load, normalize
from fcoh.expr import ExprError, evaluate_dimension, resolve_params
from fcoh.lint import lint_plan


class V04ExpressionAndSchemaTests(unittest.TestCase):
    def setUp(self):
        self.data, self.plan = load('examples/mounting-bracket-v04.fcoh.yaml')

    def test_v04_expression_resolution_and_transpilation(self):
        self.assertEqual(self.plan['params']['wall_y']['value'], 34.0)
        self.assertEqual(self.plan['params']['right_x']['value'], 68.0)
        self.assertEqual(self.plan['params']['rear_y']['value'], 26.0)
        self.assertEqual(self.plan['params']['root_fillet']['value'], 3.0)
        self.assertEqual(self.plan['params']['wall_y']['expr']['freecad_sheet'], '=width - thickness')
        self.assertEqual(self.plan['params']['wall_y']['expr']['fusion_expr'], 'width - thickness')
        self.assertEqual(self.plan['params']['root_fillet']['expr']['fusion_expr'], 'min(thickness / 2; 3 mm)')
        self.assertEqual(self.plan['params']['root_fillet']['expr']['freecad_expr'], 'min(Params.thickness / 2; 3 mm)')

    def test_parameter_change_propagates_through_expressions(self):
        modified = copy.deepcopy(self.data)
        modified['params']['length']['value'] = 110
        modified['params']['width']['value'] = 55
        modified['params']['thickness']['value'] = 4
        plan = normalize(modified)
        self.assertEqual(plan['params']['wall_y']['value'], 51.0)
        self.assertEqual(plan['params']['right_x']['value'], 98.0)
        self.assertEqual(plan['params']['rear_y']['value'], 43.0)
        self.assertEqual(plan['params']['root_fillet']['value'], 2.0)

    def test_unsafe_and_invalid_expressions_rejected(self):
        bad_exprs = [
            '__import__("os").system("echo hi")',
            'eval("1+1")',
            'pow(2, 3)',
            'length ** 2',
            'length * width',   # dimensional error: area
            '10 / length',      # dimensional error: inverse mm
            'thickness / 0',    # division by zero
            'unknown_var + 1',
            '(lambda x: x)(1)',
            'length.real',
            '"abc"',
        ]
        env = {'length': 80.0, 'width': 40.0, 'thickness': 6.0}
        for expr in bad_exprs:
            with self.subTest(expr=expr), self.assertRaises(ExprError):
                evaluate_dimension(expr, env)

    def test_circular_parameter_reference_rejected(self):
        params = {
            'a': {'value': 'b + 1', 'unit': 'mm', 'meaning': 'a'},
            'b': {'value': 'a + 1', 'unit': 'mm', 'meaning': 'b'},
        }
        with self.assertRaises(ExprError):
            resolve_params(params)

    def test_v04_structural_errors_rejected(self):
        # 1. face_plane referencing future feature
        d1 = copy.deepcopy(self.data)
        d1['steps'][0]['datum'] = 'top_plane'
        with self.assertRaises(FcohError):
            normalize(d1)

        # 2. fillet referencing unknown/future feature
        d2 = copy.deepcopy(self.data)
        d2['steps'][6]['edges'] = {'between': ['base_pad', 'wall_top_chamfer']}
        with self.assertRaises(FcohError):
            normalize(d2)

        # 3. edges.between with duplicate ids
        d3 = copy.deepcopy(self.data)
        d3['steps'][6]['edges'] = {'between': ['base_pad', 'base_pad']}
        with self.assertRaises(FcohError):
            normalize(d3)

        # 4. pocket specifying both distance and through_all
        d4 = copy.deepcopy(self.data)
        d4['steps'][5]['distance'] = 6
        with self.assertRaises(FcohError):
            normalize(d4)

    def test_lint_passes_on_valid_v04_example(self):
        info = describe('examples/mounting-bracket-v04.fcoh.yaml', strict=True)
        self.assertTrue(info['valid'])
        self.assertEqual(info['fcoh'], '0.4')
        self.assertTrue(info['lint']['ok'])
        self.assertEqual(info['lint']['error_count'], 0)
        self.assertEqual(info['lint']['warning_count'], 0)
        self.assertGreaterEqual(info['lint']['checks']['min_hole_edge_clearance_mm'], 5.0)

    def test_lint_catches_bounds_outside_pocket_thin_wall_and_overlap(self):
        # Parameter below min
        d1 = copy.deepcopy(self.data)
        d1['params']['length']['value'] = 30
        lint1 = lint_plan(d1, normalize(d1))
        self.assertFalse(lint1['ok'])
        self.assertIn('param_below_min', [e['code'] for e in lint1['errors']])

        # Pocket outside body
        d2 = copy.deepcopy(self.data)
        d2['steps'][4]['entities'][0]['x'] = 500
        lint2 = lint_plan(d2, normalize(d2))
        self.assertFalse(lint2['ok'])
        self.assertIn('pocket_outside_body', [e['code'] for e in lint2['errors']])

        # Overlapping holes
        d3 = copy.deepcopy(self.data)
        d3['steps'][4]['entities'][1]['x'] = 'edge_margin + 2'
        lint3 = lint_plan(d3, normalize(d3))
        self.assertFalse(lint3['ok'])
        self.assertIn('entity_overlap', [e['code'] for e in lint3['errors']])

        # Hole colliding with rear wall when width shrinks
        d4 = copy.deepcopy(self.data)
        d4['params']['width']['value'] = 30
        d4['params']['rear_y']['value'] = 'wall_y - 1'
        lint4 = lint_plan(d4, normalize(d4))
        self.assertIn('hole_hits_adjacent_feature', [w['code'] for w in lint4['warnings']])

        # Unused param & missing meaning warning
        d5 = copy.deepcopy(self.data)
        d5['params']['orphan'] = {'value': 5, 'unit': 'mm', 'meaning': None}
        lint5 = lint_plan(d5, normalize(d5))
        warn_codes = [w['code'] for w in lint5['warnings']]
        self.assertIn('unused_param', warn_codes)
        self.assertIn('missing_intent', warn_codes)

        # Oversized fillet caught before CAD run
        d6 = copy.deepcopy(self.data)
        d6['params']['root_fillet']['value'] = 6
        lint6 = lint_plan(d6, normalize(d6))
        self.assertIn('fillet_exceeds_feature_size', [e['code'] for e in lint6['errors']])


if __name__ == '__main__':
    unittest.main()
