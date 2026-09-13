"""Fusion add-in: dispatch filesystem jobs onto Fusion's main thread."""
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import traceback
import types

import adsk.core
import adsk.fusion

EVENT_ID = 'fcoh.prototype.local.job.v1'
handlers = []
stop_event = threading.Event()
worker = None
app = None
root = None
event = None
fusion_version = None


def write_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def annotate(obj, item):
    for field in ('id', 'meaning', 'why'):
        obj.attributes.add('FCOH', field, str(item.get(field) or ''))


def generate(request, directory):
    doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    if not doc:
        raise RuntimeError('Fusion did not create a new document')
    try:
        return generate_document(request, directory, doc)
    except Exception:
        if doc.isValid:
            doc.close(False)
        raise


def generate_document(request, directory, doc):
    plan = request['plan']
    design = adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType'))
    if not design:
        raise RuntimeError('New document has no design product')
    design.designType = adsk.fusion.DesignTypes.ParametricDesignType
    component = design.rootComponent
    # Fusion prohibits renaming the root component of an unsaved design.
    component.attributes.add('FCOH', 'part_name', plan['meta']['name'])
    component.attributes.add('FCOH', 'document', json.dumps(request['document'], ensure_ascii=False))
    for name, param in plan['params'].items():
        design.userParameters.add(name, adsk.core.ValueInput.createByString(str(param['value'])+' mm'), 'mm', param.get('meaning') or '')
    datums, sketches = {}, {}
    features = []
    for datum in plan['datums']:
        plane_input = component.constructionPlanes.createInput()
        plane_input.setByOffset(component.xYConstructionPlane, adsk.core.ValueInput.createByString(str(datum['offset'])+' mm'))
        plane = component.constructionPlanes.add(plane_input)
        plane.name = datum['id']
        annotate(plane, datum)
        datums[datum['id']] = plane
    previous_volume = 0.0
    try:
        for step in plan['steps']:
            if step['kind'] == 'sketch':
                sketch = component.sketches.add(datums[step['datum']])
                sketch.name = step['id']
                annotate(sketch, step)
                for entity in step['entities']:
                    # Fusion API coordinates are centimetres; FCOH uses millimetres.
                    x, y = entity['x']/10, entity['y']/10
                    p = adsk.core.Point3D.create(x, y, 0)
                    if entity['kind'] == 'rectangle':
                        q = adsk.core.Point3D.create(x+entity['width']/10, y+entity['height']/10, 0)
                        curves = sketch.sketchCurves.sketchLines.addTwoPointRectangle(p, q)
                        for i in range(curves.count):
                            annotate(curves.item(i), entity)
                    else:
                        curve = sketch.sketchCurves.sketchCircles.addByCenterRadius(p, entity['radius']/10)
                        annotate(curve, entity)
                sketches[step['id']] = sketch
            else:
                sketch = sketches[step['sketch']]
                profiles = adsk.core.ObjectCollection.create()
                for i in range(sketch.profiles.count):
                    profiles.add(sketch.profiles.item(i))
                if profiles.count == 0:
                    raise RuntimeError('Sketch has no closed profiles: '+step['sketch'])
                operations = adsk.fusion.FeatureOperations
                operation = operations.CutFeatureOperation if step['kind'] == 'pocket' else (operations.NewBodyFeatureOperation if not features else operations.JoinFeatureOperation)
                feature_input = component.features.extrudeFeatures.createInput(profiles, operation)
                original = next(s for s in request['document']['steps'] if s['id'] == step['id'])
                value = original['distance'] if isinstance(original['distance'], str) else str(step['distance'])+' mm'
                extent = adsk.fusion.DistanceExtentDefinition.create(adsk.core.ValueInput.createByString(value))
                direction = adsk.fusion.ExtentDirections.NegativeExtentDirection if step['kind'] == 'pocket' else adsk.fusion.ExtentDirections.PositiveExtentDirection
                feature_input.setOneSideExtent(extent, direction)
                feature = component.features.extrudeFeatures.add(feature_input)
                feature.name = step['id']
                annotate(feature, step)
                sketch.isVisible = False
                if component.bRepBodies.count != 1 or not component.bRepBodies.item(0).isSolid:
                    raise RuntimeError('Expected exactly one solid body')
                volume = component.bRepBodies.item(0).volume * 1000
                if step['kind'] == 'pad' and volume <= previous_volume:
                    raise RuntimeError('Pad did not add material')
                if step['kind'] == 'pocket' and volume >= previous_volume:
                    raise RuntimeError('Pocket did not remove material')
                previous_volume = volume
                features.append({'id': step['id'], 'kind': step['kind'], 'volume_mm3': volume})
        design.computeAll()
        for plane in datums.values():
            plane.isLightBulbOn = False
        body = component.bRepBodies.item(0)
        bounds = body.boundingBox
        native, neutral = directory / 'part.f3d', directory / 'part.step'
        manager = design.exportManager
        for options in [manager.createFusionArchiveExportOptions(str(native)), manager.createSTEPExportOptions(str(neutral))]:
            if not manager.execute(options):
                raise RuntimeError('Fusion export failed')
        reopened = app.importManager.importToNewDocument(app.importManager.createFusionArchiveImportOptions(str(native)))
        if not reopened:
            raise RuntimeError('Could not reopen the exported F3D archive')
        try:
            saved_design = adsk.fusion.Design.cast(reopened.products.itemByProductType('DesignProductType'))
            saved_root = saved_design.rootComponent
            if saved_root.bRepBodies.count != 1 or abs(saved_root.bRepBodies.item(0).volume-body.volume)*1000 > 0.01:
                raise RuntimeError('F3D reopened geometry mismatch')
            saved_attr = saved_root.attributes.itemByName('FCOH', 'document')
            if not saved_attr or json.loads(saved_attr.value) != request['document']:
                raise RuntimeError('F3D did not preserve the FCOH document metadata')
            if saved_design.timeline.count != design.timeline.count:
                raise RuntimeError('F3D did not preserve feature history')
        finally:
            reopened.close(False)
            doc.activate()
        app.activeViewport.fit()
        png = directory / 'preview.png'
        try:
            app.activeViewport.saveAsImageFile(str(png), 1280, 960)
        except Exception:
            pass
        artifacts = [native, neutral] + ([png] if png.exists() else [])
        return {'ok': True, 'cad': 'fusion', 'version': app.version, 'volume_mm3': body.volume*1000,
                'bounds_mm': [(getattr(bounds.maxPoint,a)-getattr(bounds.minPoint,a))*10 for a in ['x','y','z']],
                'solid_count': component.bRepBodies.count, 'valid_shape': body.isSolid,
                'features': features, 'timeline_count': design.timeline.count,
                'reopened_native': True, 'fcoh_metadata_and_order_preserved': True,
                'artifacts': {p.name: {'path': str(p), 'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in artifacts}}
    except Exception:
        # This document was created exclusively for this job; user documents are not touched.
        doc.close(False)
        raise


class JobHandler(adsk.core.CustomEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        # Do not interrupt a user command or another add-in's command.
        active_command = app.userInterface.activeCommand
        write_json(root / '.runtime' / 'fusion-state.json', {
            'timestamp': time.time(), 'active_command': active_command,
            'document_count': app.documents.count,
            'state': 'ready' if active_command == 'SelectCommand' else 'waiting_for_idle'})
        if active_command != 'SelectCommand':
            return
        queue = root / '.runtime' / 'queue'
        for ticket in sorted(queue.glob('*.json')):
            job_id = ticket.stem
            if not re.fullmatch('[a-f0-9]{32}', job_id):
                continue
            claimed = ticket.with_suffix('.processing')
            ticket.replace(claimed)
            directory = (root / 'outputs' / job_id).resolve()
            if not directory.is_relative_to((root / 'outputs').resolve()):
                continue
            try:
                if (directory / 'result.json').exists():
                    continue
                request = json.loads((directory / 'request.json').read_text(encoding='utf-8'))
                if request['job_id'] != job_id or request['cad'] != 'fusion':
                    raise RuntimeError('Invalid job envelope')
                if time.time() > request['expires_at']:
                    raise RuntimeError('Job expired; submit it again')
                # The bridge stays loaded, but CAD generation code is refreshed
                # from this project's fixed, trusted source path for each job.
                # FCOH documents can never specify executable code or this path.
                source = root / 'adapters' / 'fusion' / 'FcohBridge' / 'FcohBridge.py'
                backend = types.ModuleType('fcoh_generation_backend')
                backend.__file__ = str(source)
                exec(compile(source.read_text(encoding='utf-8'), str(source), 'exec'), backend.__dict__)
                backend.app = app
                result = backend.generate(request, directory)
            except Exception:
                result = {'ok': False, 'cad': 'fusion', 'error': traceback.format_exc()}
            write_json(directory / 'result.json', result)
            break


def poll():
    while not stop_event.wait(2):
        try:
            write_json(root / '.runtime' / 'fusion-heartbeat.json', {'timestamp': time.time(), 'version': fusion_version, 'bridge': '0.1.1'})
            if any((root / '.runtime' / 'queue').glob('*.json')):
                app.fireCustomEvent(EVENT_ID)
        except Exception:
            (root / '.runtime' / 'fusion-bridge-error.log').write_text(traceback.format_exc(), encoding='utf-8')


def run(context):
    global app, root, event, worker, fusion_version
    config = json.loads((Path(__file__).parent / 'project-root.json').read_text(encoding='utf-8-sig'))
    root = Path(config['root']).resolve()
    if not (root / 'pyproject.toml').exists():
        raise RuntimeError('FCOH project root not found')
    (root / '.runtime' / 'queue').mkdir(parents=True, exist_ok=True)
    app = adsk.core.Application.get()
    fusion_version = app.version
    event = app.registerCustomEvent(EVENT_ID)
    handler = JobHandler()
    event.add(handler)
    handlers.append(handler)
    stop_event.clear()
    worker = threading.Thread(target=poll, name='FcohBridge', daemon=True)
    worker.start()


def stop(context):
    stop_event.set()
    if worker:
        worker.join(timeout=4)
    if app and event:
        for handler in handlers:
            event.remove(handler)
        app.unregisterCustomEvent(EVENT_ID)
    handlers.clear()
    if root:
        write_json(root / '.runtime' / 'fusion-heartbeat.json', {'timestamp': 0, 'stopped': True})
