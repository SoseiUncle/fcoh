"""Run inside the bundled FreeCAD Python (no pip packages needed)."""
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback

import FreeCAD as App
import Part
import Sketcher


def annotate(obj, item):
    for field, prop in [('id', 'FcohId'), ('meaning', 'Meaning'), ('why', 'Why')]:
        obj.addProperty('App::PropertyString', prop, 'FCOH')
        setattr(obj, prop, str(item.get(field) or ''))


def build(job_dir):
    request = json.loads((job_dir / 'request.json').read_text(encoding='utf-8'))
    plan = request['plan']
    doc = App.newDocument(plan['meta']['id'])
    doc.Label = plan['meta']['name']
    try:
        body = doc.addObject('PartDesign::Body', 'Body')
        body.addProperty('App::PropertyString', 'FcohDocument', 'FCOH')
        body.FcohDocument = json.dumps(request['document'], ensure_ascii=False)
        params = doc.addObject('Spreadsheet::Sheet', 'Params')
        params.set('A1', 'Parameter')
        params.set('B1', 'Value (mm)')
        params.set('C1', 'Meaning')
        for i, (name, param) in enumerate(plan['params'].items(), 2):
            params.set(f'A{i}', name)
            params.set(f'B{i}', str(param['value']) + ' mm')
            params.setAlias(f'B{i}', name)
            params.set(f'C{i}', param.get('meaning') or '')
        sketches, features = {}, []
        last = None
        for step in plan['steps']:
            if step['kind'] == 'sketch':
                sketch = body.newObject('Sketcher::SketchObject', step['id'])
                sketch.Placement.Base = App.Vector(0, 0, step['z'])
                annotate(sketch, step)
                sketch.addProperty('App::PropertyString', 'FcohEntities', 'FCOH')
                sketch.FcohEntities = json.dumps(step['entities'], ensure_ascii=False)
                for entity in step['entities']:
                    x, y = entity['x'], entity['y']
                    if entity['kind'] == 'rectangle':
                        w, h = entity['width'], entity['height']
                        points = [(x,y), (x+w,y), (x+w,y+h), (x,y+h)]
                        for i in range(4):
                            a, b = points[i], points[(i+1)%4]
                            sketch.addGeometry(Part.LineSegment(App.Vector(*a,0), App.Vector(*b,0)), False)
                    else:
                        sketch.addGeometry(Part.Circle(App.Vector(x,y,0), App.Vector(0,0,1), entity['radius']), False)
                sketches[step['id']] = sketch
                doc.recompute()
            else:
                sketch = sketches[step['sketch']]
                obj_type = 'PartDesign::Pad' if step['kind'] == 'pad' else 'PartDesign::Pocket'
                feature = body.newObject(obj_type, step['id'])
                feature.Profile = sketch
                feature.Length = step['distance']
                original = next(s for s in request['document']['steps'] if s['id'] == step['id'])
                if isinstance(original['distance'], str):
                    feature.setExpression('Length', 'Params.' + original['distance'])
                annotate(feature, step)
                doc.recompute()
                if feature.Shape.isNull() or not feature.Shape.isValid() or len(feature.Shape.Solids) != 1:
                    raise RuntimeError(f'{step["id"]}: failed to produce one valid solid; {feature.State}')
                if last is not None:
                    if step['kind'] == 'pad' and feature.Shape.Volume <= last.Shape.Volume:
                        raise RuntimeError('Pad did not add material')
                    if step['kind'] == 'pocket' and feature.Shape.Volume >= last.Shape.Volume:
                        raise RuntimeError('Pocket did not remove material')
                    last.Visibility = False
                sketch.Visibility = False
                features.append({'id': step['id'], 'kind': step['kind'], 'volume_mm3': feature.Shape.Volume})
                last = feature
        body.Tip = last
        doc.recompute()
        native = job_dir / 'part.FCStd'
        step_file = job_dir / 'part.step'
        doc.saveAs(str(native))
        Part.export([last], str(step_file))
        box = last.Shape.BoundBox
        result = {'ok': True, 'cad': 'freecad', 'version': '.'.join(App.Version()[:3]),
                  'volume_mm3': last.Shape.Volume,
                  'bounds_mm': [box.XLength, box.YLength, box.ZLength],
                  'solid_count': len(last.Shape.Solids), 'valid_shape': last.Shape.isValid(),
                  'features': features,
                  'artifacts': {p.name: {'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'bytes': p.stat().st_size} for p in [native, step_file]}}
        # Reopen native and neutral artifacts to check actual persistence.
        App.closeDocument(doc.Name)
        doc = App.openDocument(str(native))
        reopened = doc.getObject('Body').Tip.Shape
        if abs(reopened.Volume-result['volume_mm3']) > 1e-5:
            raise RuntimeError('Native file volume changed after reopen')
        imported = Part.Shape()
        imported.read(str(step_file))
        if not imported.isValid() or abs(imported.Volume-result['volume_mm3']) > 1e-4:
            raise RuntimeError('STEP round-trip geometry mismatch')
        persisted_document = json.loads(doc.getObject('Body').FcohDocument)
        if persisted_document != request['document']:
            raise RuntimeError('FCOH document metadata changed after reopen')
        persisted_ids = [o.FcohId for o in doc.Objects if 'FcohId' in o.PropertiesList]
        if persisted_ids != [s['id'] for s in plan['steps']]:
            raise RuntimeError('Feature/sketch order changed after reopen')
        result['fcoh_metadata_and_order_preserved'] = True
        result['reopened_native_and_step'] = True
        return result
    finally:
        if doc and App.getDocument(doc.Name):
            App.closeDocument(doc.Name)


if __name__ == '__main__':
    directory = Path(os.environ['FCOH_JOB_DIR']).resolve()
    try:
        result = build(directory)
    except Exception:
        result = {'ok': False, 'cad': 'freecad', 'error': traceback.format_exc()}
    (directory / 'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'ok': result['ok'], 'result': str(directory / 'result.json')}))
