"""Regenerate a changed design and detect a cut that misses the body."""
import copy
import json
import math
import time
import uuid
import yaml
from fcoh.core import ROOT, load
from fcoh.jobs import build

data, _ = load('examples/mounting-bracket.fcoh.yaml')
directory = ROOT / 'outputs' / ('verification-' + uuid.uuid4().hex)
directory.mkdir(parents=True)
variant = copy.deepcopy(data)
variant['params']['length']['value'] = 90
path = directory / 'length90.fcoh.yaml'
path.write_text(yaml.safe_dump(variant, allow_unicode=True, sort_keys=False), encoding='utf-8')
changed = build(str(path), 'freecad')
assert changed['ok'], changed
assert changed['bounds_mm'] == [90.0,40.0,30.0], changed
expected = 90*40*6 + 90*6*24 - 4*math.pi*3**2*6
assert abs(changed['volume_mm3']-expected) < 0.01
assert changed['fcoh_metadata_and_order_preserved']

bad = copy.deepcopy(data)
for entity in bad['steps'][4]['entities']:
    entity['x'] = 1000
bad_path = directory / 'holes-miss-body.fcoh.yaml'
bad_path.write_text(yaml.safe_dump(bad, allow_unicode=True, sort_keys=False), encoding='utf-8')
failed = build(str(bad_path), 'freecad')
assert not failed['ok'], failed
evidence = {'passed': True, 'timestamp': time.time(), 'dimension_change': changed,
            'missed_cut_rejected': {'job_id': failed['job_id'], 'error': failed['error']}}
(ROOT / 'evidence').mkdir(exist_ok=True)
(ROOT / 'evidence' / 'geometry.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(evidence, ensure_ascii=False, indent=2))
