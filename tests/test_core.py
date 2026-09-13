import copy
import unittest
from fcoh.core import FcohError, ROOT, UniqueLoader, load, normalize, project_path
import yaml


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.data, self.plan = load('examples/mounting-bracket.fcoh.yaml')

    def test_parameters_are_resolved_without_mutation(self):
        self.assertEqual(self.plan['steps'][1]['distance'], 6)
        self.assertEqual(self.data['steps'][1]['distance'], 'thickness')
        self.assertEqual(self.plan['steps'][2]['z'], 6)

    def test_bad_inputs_rejected(self):
        cases = []
        data = copy.deepcopy(self.data); data['meta']['units'] = 'cm'; cases.append(data)
        data = copy.deepcopy(self.data); data['steps'][1]['distance'] = 'missing'; cases.append(data)
        data = copy.deepcopy(self.data); data['steps'][1]['sketch'] = 'holes_sketch'; cases.append(data)
        data = copy.deepcopy(self.data); data['params']['length']['value'] = float('nan'); cases.append(data)
        data = copy.deepcopy(self.data); data['steps'][1]['distance'] = -1; cases.append(data)
        data = copy.deepcopy(self.data); data['steps'][1]['kind'] = 'fillet'; cases.append(data)
        data = copy.deepcopy(self.data); data['datums'][1]['id'] = 'base_plane'; cases.append(data)
        data = copy.deepcopy(self.data); data['source'] = {'path':'secret.step'}; cases.append(data)
        data = copy.deepcopy(self.data); data['recipe'] = {'file':'danger.py'}; cases.append(data)
        data = copy.deepcopy(self.data); data['steps'][1]['kind'] = 'pocket'; cases.append(data)
        for i, data in enumerate(cases):
            with self.subTest(case=i), self.assertRaises(FcohError):
                normalize(data)

    def test_duplicate_keys_and_aliases_rejected(self):
        for text in ['a: 1\na: 2', 'a: &a [1]\nb: *a']:
            with self.assertRaises(FcohError):
                yaml.load(text, Loader=UniqueLoader)

    def test_path_cannot_escape_project(self):
        with self.assertRaises(FcohError):
            project_path('../outside.yaml')

    def test_parameter_change_reaches_geometry(self):
        self.data['params']['length']['value'] = 90
        changed = normalize(self.data)
        self.assertEqual(changed['steps'][0]['entities'][0]['width'], 90)
        self.assertEqual(changed['steps'][2]['entities'][0]['width'], 90)


if __name__ == '__main__':
    unittest.main()
