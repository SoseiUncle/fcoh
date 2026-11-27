"""Fusion add-in: dispatch filesystem jobs onto Fusion's main thread."""
import hashlib
import json
import math
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

SIDE_VECTORS = {
    '+X': (1.0, 0.0, 0.0),
    '-X': (-1.0, 0.0, 0.0),
    '+Y': (0.0, 1.0, 0.0),
    '-Y': (0.0, -1.0, 0.0),
    '+Z': (0.0, 0.0, 1.0),
    '-Z': (0.0, 0.0, -1.0),
}

AXIS_VECTORS = {
    'X': (1.0, 0.0, 0.0),
    'Y': (0.0, 1.0, 0.0),
    'Z': (0.0, 0.0, 1.0),
}


def write_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def annotate(obj, item):
    for field in ('id', 'meaning', 'why'):
        obj.attributes.add('FCOH', field, str(item.get(field) or ''))


def _fusion_expr(item_plan, key, item_raw=None):
    exprs = item_plan.get('exprs') or {}
    if key in exprs:
        return exprs[key]['fusion_expr']
    if item_raw is not None and isinstance(item_raw.get(key), str):
        return item_raw[key]
    return None


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(v):
    length = math.sqrt(_dot(v, v))
    if length <= 1e-12:
        return (0.0, 0.0, 0.0)
    return (v[0] / length, v[1] / length, v[2] / length)


def _face_point_and_normal(face):
    pt = face.pointOnFace
    ok, normal = face.evaluator.getNormalAtPoint(pt)
    if not ok:
        return (pt.x, pt.y, pt.z), (0.0, 0.0, 0.0)
    n = _norm((normal.x, normal.y, normal.z))
    if face.isParamReversed:
        # Fusion's getNormalAtPoint already reflects outward BRepFace orientation;
        # keep evaluator normal as reported.
        pass
    return (pt.x, pt.y, pt.z), n


def _face_samples(face):
    """Collect sample (point_cm, normal) pairs on a Fusion BRepFace."""
    samples = [_face_point_and_normal(face)]
    try:
        mesh = face.meshManager.displayMeshes.bestMesh
        if mesh:
            coords = mesh.nodeCoordinates
            indices = mesh.nodeIndices
            tris = []
            for i in range(0, len(indices), 3):
                p0, p1, p2 = coords[indices[i]], coords[indices[i + 1]], coords[indices[i + 2]]
                ax, ay, az = p1.x - p0.x, p1.y - p0.y, p1.z - p0.z
                bx, by, bz = p2.x - p0.x, p2.y - p0.y, p2.z - p0.z
                cx, cy, cz = ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
                area2 = math.sqrt(cx * cx + cy * cy + cz * cz)
                if area2 > 1e-10:
                    mid = adsk.core.Point3D.create((p0.x + p1.x + p2.x) / 3.0,
                                                   (p0.y + p1.y + p2.y) / 3.0,
                                                   (p0.z + p1.z + p2.z) / 3.0)
                    tris.append((area2, mid))
            tris.sort(key=lambda t: t[0], reverse=True)
            for _, pt in tris[:8]:
                ok, normal = face.evaluator.getNormalAtPoint(pt)
                if ok:
                    samples.append(((pt.x, pt.y, pt.z), _norm((normal.x, normal.y, normal.z))))
    except Exception:
        pass
    return samples


def _tag_and_update_provenance(body, feature, step_id, prev_snapshots):
    """Track face provenance via geometric matching plus Fusion feature.faces."""
    new_face_tokens = set()
    try:
        for i in range(feature.faces.count):
            new_face_tokens.add(feature.faces.item(i).tempId)
    except Exception:
        pass

    prov_by_temp_id = {}
    snapshots = []
    for i in range(body.faces.count):
        face = body.faces.item(i)
        samples = _face_samples(face)
        owners = set()
        if not prev_snapshots:
            owners.add(step_id)
        else:
            has_new = False
            for pt, n in samples:
                matched_old = False
                for old_faces_info in prev_snapshots:
                    old_owners, old_samples, old_bbox = old_faces_info
                    # Bounding-box quick reject (cm)
                    if (pt[0] < old_bbox[0] - 1e-3 or pt[0] > old_bbox[3] + 1e-3 or
                        pt[1] < old_bbox[1] - 1e-3 or pt[1] > old_bbox[4] + 1e-3 or
                        pt[2] < old_bbox[2] - 1e-3 or pt[2] > old_bbox[5] + 1e-3):
                        continue
                    for opt, on in old_samples:
                        if _dot(n, on) > 0.99:
                            # Plane coplanarity or proximity check
                            dx, dy, dz = pt[0] - opt[0], pt[1] - opt[1], pt[2] - opt[2]
                            plane_dist = abs(dx * on[0] + dy * on[1] + dz * on[2])
                            if plane_dist < 1e-3:
                                owners.update(old_owners)
                                matched_old = True
                                break
                    if matched_old:
                        break
                if not matched_old:
                    has_new = True
            if has_new or face.tempId in new_face_tokens or not owners:
                owners.add(step_id)
        prov_by_temp_id[face.tempId] = owners
        bb = face.boundingBox
        bbox = (bb.minPoint.x, bb.minPoint.y, bb.minPoint.z,
                bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z)
        snapshots.append((owners, samples, bbox))
    return prov_by_temp_id, snapshots


def _edge_convexity_fusion(edge, f1, f2):
    ok, start_p, end_p = edge.evaluator.getParameterExtents()
    if not ok:
        return 'smooth'
    ok, mid_pt = edge.evaluator.getPointAtParameter(0.5 * (start_p + end_p))
    if not ok:
        return 'smooth'
    ok1, n1_v = f1.evaluator.getNormalAtPoint(mid_pt)
    ok2, n2_v = f2.evaluator.getNormalAtPoint(mid_pt)
    if not (ok1 and ok2):
        return 'smooth'
    n1 = _norm((n1_v.x, n1_v.y, n1_v.z))
    n2 = _norm((n2_v.x, n2_v.y, n2_v.z))
    mid = (mid_pt.x, mid_pt.y, mid_pt.z)

    def inward_tangent(face, normal):
        samples = _face_samples(face)
        best, best_d = None, float('inf')
        for pt, _ in samples:
            diff = (pt[0] - mid[0], pt[1] - mid[1], pt[2] - mid[2])
            dn = _dot(diff, normal)
            tang = (diff[0] - normal[0] * dn, diff[1] - normal[1] * dn, diff[2] - normal[2] * dn)
            tlen = math.sqrt(_dot(tang, tang))
            dlen = math.sqrt(_dot(diff, diff))
            if tlen > 1e-6 and dlen < best_d:
                best = (tang[0] / tlen, tang[1] / tlen, tang[2] / tlen)
                best_d = dlen
        return best

    t1 = inward_tangent(f1, n1)
    t2 = inward_tangent(f2, n2)
    scores = []
    if t2 is not None:
        scores.append(_dot(n1, t2))
    if t1 is not None:
        scores.append(_dot(n2, t1))
    if not scores:
        return 'smooth'
    score = sum(scores) / len(scores)
    if score > 0.05:
        return 'concave'
    if score < -0.05:
        return 'convex'
    return 'smooth'


def _select_face_fusion(body, prov_by_temp_id, selector):
    feat_id = selector['of']
    sv = SIDE_VECTORS[selector['side']]
    candidates = []
    for i in range(body.faces.count):
        face = body.faces.item(i)
        owners = prov_by_temp_id.get(face.tempId, set())
        if feat_id not in owners:
            continue
        pt, n = _face_point_and_normal(face)
        if _dot(n, sv) > 0.98:
            candidates.append((_dot(pt, sv), face))
    if not candidates:
        raise RuntimeError(f'Face selector matched 0 faces: {selector}')
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def _select_edges_fusion(body, prov_by_temp_id, selector):
    matched = adsk.core.ObjectCollection.create()
    for i in range(body.edges.count):
        edge = body.edges.item(i)
        if edge.faces.count != 2:
            continue
        f1, f2 = edge.faces.item(0), edge.faces.item(1)
        p1 = prov_by_temp_id.get(f1.tempId, set())
        p2 = prov_by_temp_id.get(f2.tempId, set())

        if 'between' in selector:
            a, b = selector['between']
            hit = ((a in (p1 - p2) and b in (p2 - p1)) or (b in (p1 - p2) and a in (p2 - p1)))
            if not hit:
                continue
        if 'of' in selector:
            fid = selector['of']
            if fid not in p1 and fid not in p2:
                continue
        geom = edge.geometry
        is_line = isinstance(geom, adsk.core.Line3D)
        is_circle = isinstance(geom, (adsk.core.Circle3D, adsk.core.Arc3D))
        if 'kind' in selector:
            if selector['kind'] == 'line' and not is_line:
                continue
            if selector['kind'] == 'circle' and not is_circle:
                continue
        if 'orientation' in selector:
            if not is_line:
                continue
            axis = AXIS_VECTORS[selector['orientation']]
            d = _norm((geom.endPoint.x - geom.startPoint.x,
                       geom.endPoint.y - geom.startPoint.y,
                       geom.endPoint.z - geom.startPoint.z))
            if abs(_dot(d, axis)) < 0.99:
                continue
        if 'side' in selector:
            sv = SIDE_VECTORS[selector['side']]
            _, n1 = _face_point_and_normal(f1)
            _, n2 = _face_point_and_normal(f2)
            if _dot(n1, sv) <= 0.98 and _dot(n2, sv) <= 0.98:
                continue
        if 'convexity' in selector and selector['convexity'] != 'any':
            if _edge_convexity_fusion(edge, f1, f2) != selector['convexity']:
                continue
        matched.add(edge)
    if matched.count == 0:
        raise RuntimeError(f'Edge selector matched 0 edges: {selector}')
    return matched


def _apply_dim_expr(dim, expr_str):
    if expr_str:
        dim.parameter.expression = expr_str


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
    raw_doc = request['document']
    design = adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType'))
    if not design:
        raise RuntimeError('New document has no design product')
    design.designType = adsk.fusion.DesignTypes.ParametricDesignType
    component = design.rootComponent
    # Fusion prohibits renaming the root component of an unsaved design.
    component.attributes.add('FCOH', 'part_name', plan['meta']['name'])
    component.attributes.add('FCOH', 'document', json.dumps(raw_doc, ensure_ascii=False))

    param_order = plan.get('param_order', list(plan['params'].keys()))
    for name in param_order:
        param = plan['params'][name]
        expr_meta = param.get('expr')
        val_str = expr_meta['fusion_expr'] if expr_meta else (str(param['value']) + ' mm')
        design.userParameters.add(name, adsk.core.ValueInput.createByString(val_str), 'mm', param.get('meaning') or '')

    base_planes = {
        'XY': component.xYConstructionPlane,
        'XZ': component.xZConstructionPlane,
        'YZ': component.yZConstructionPlane,
    }
    raw_datums = {d['id']: d for d in raw_doc['datums']}
    plan_datums = {d['id']: d for d in plan['datums']}
    datums, sketches = {}, {}
    features = []

    for datum in plan['datums']:
        if datum['kind'] == 'plane':
            plane_input = component.constructionPlanes.createInput()
            off_expr = _fusion_expr(datum, 'offset', raw_datums[datum['id']]) or (str(datum['offset']) + ' mm')
            plane_input.setByOffset(base_planes[datum.get('plane', 'XY')], adsk.core.ValueInput.createByString(off_expr))
            plane = component.constructionPlanes.add(plane_input)
            plane.name = datum['id']
            annotate(plane, datum)
            datums[datum['id']] = plane

    raw_steps = {s['id']: s for s in raw_doc['steps']}
    previous_volume = 0.0
    prov_by_temp_id = {}
    prev_snapshots = []

    def ensure_datum_fusion(datum_id):
        if datum_id in datums:
            return datums[datum_id]
        datum = plan_datums[datum_id]
        body = component.bRepBodies.item(0)
        target_face = _select_face_fusion(body, prov_by_temp_id, datum['face'])
        off_expr = _fusion_expr(datum, 'offset', raw_datums[datum_id])
        if off_expr or abs(datum.get('offset', 0.0)) > 1e-9:
            plane_input = component.constructionPlanes.createInput()
            val_str = off_expr or (str(datum['offset']) + ' mm')
            plane_input.setByOffset(target_face, adsk.core.ValueInput.createByString(val_str))
            plane = component.constructionPlanes.add(plane_input)
            plane.name = datum['id']
            annotate(plane, datum)
            datums[datum_id] = plane
            return plane
        datums[datum_id] = target_face
        return target_face

    try:
        for step in plan['steps']:
            kind = step['kind']
            raw_step = raw_steps[step['id']]
            if kind == 'sketch':
                plane_or_face = ensure_datum_fusion(step['datum'])
                sketch = component.sketches.add(plane_or_face)
                sketch.name = step['id']
                annotate(sketch, step)
                raw_entities = {e['id']: e for e in raw_step['entities']}
                horiz = adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation
                vert = adsk.fusion.DimensionOrientations.VerticalDimensionOrientation
                for entity in step['entities']:
                    raw_ent = raw_entities[entity['id']]
                    # Fusion API coordinates are centimetres; FCOH uses millimetres.
                    x, y = entity['x'] / 10.0, entity['y'] / 10.0
                    p = adsk.core.Point3D.create(x, y, 0)
                    if entity['kind'] == 'rectangle':
                        w, h = entity['width'] / 10.0, entity['height'] / 10.0
                        q = adsk.core.Point3D.create(x + w, y + h, 0)
                        curves = sketch.sketchCurves.sketchLines.addTwoPointRectangle(p, q)
                        for i in range(curves.count):
                            annotate(curves.item(i), entity)
                        l0, l1 = curves.item(0), curves.item(1)
                        ex = _fusion_expr(entity, 'x', raw_ent)
                        ey = _fusion_expr(entity, 'y', raw_ent)
                        ew = _fusion_expr(entity, 'width', raw_ent)
                        eh = _fusion_expr(entity, 'height', raw_ent)
                        try:
                            if ex or abs(x) > 1e-9:
                                d = sketch.sketchDimensions.addDistanceDimension(
                                    sketch.originPoint, l0.startSketchPoint, horiz,
                                    adsk.core.Point3D.create(x * 0.5, y - 0.5, 0))
                                _apply_dim_expr(d, ex)
                            else:
                                sketch.geometricConstraints.addVerticalPoints(sketch.originPoint, l0.startSketchPoint)
                            if ey or abs(y) > 1e-9:
                                d = sketch.sketchDimensions.addDistanceDimension(
                                    sketch.originPoint, l0.startSketchPoint, vert,
                                    adsk.core.Point3D.create(x - 0.5, y * 0.5, 0))
                                _apply_dim_expr(d, ey)
                            else:
                                sketch.geometricConstraints.addHorizontalPoints(sketch.originPoint, l0.startSketchPoint)
                            dw = sketch.sketchDimensions.addDistanceDimension(
                                l0.startSketchPoint, l0.endSketchPoint, horiz,
                                adsk.core.Point3D.create(x + w * 0.5, y - 0.3, 0))
                            _apply_dim_expr(dw, ew)
                            dh = sketch.sketchDimensions.addDistanceDimension(
                                l1.startSketchPoint, l1.endSketchPoint, vert,
                                adsk.core.Point3D.create(x + w + 0.3, y + h * 0.5, 0))
                            _apply_dim_expr(dh, eh)
                        except Exception:
                            # Keep numeric geometry if a redundant constraint occurs
                            pass
                    else:
                        r = entity['radius'] / 10.0
                        curve = sketch.sketchCurves.sketchCircles.addByCenterRadius(p, r)
                        annotate(curve, entity)
                        ex = _fusion_expr(entity, 'x', raw_ent)
                        ey = _fusion_expr(entity, 'y', raw_ent)
                        er = _fusion_expr(entity, 'radius', raw_ent)
                        try:
                            if ex or abs(x) > 1e-9:
                                d = sketch.sketchDimensions.addDistanceDimension(
                                    sketch.originPoint, curve.centerSketchPoint, horiz,
                                    adsk.core.Point3D.create(x * 0.5, y - 0.3, 0))
                                _apply_dim_expr(d, ex)
                            if ey or abs(y) > 1e-9:
                                d = sketch.sketchDimensions.addDistanceDimension(
                                    sketch.originPoint, curve.centerSketchPoint, vert,
                                    adsk.core.Point3D.create(x - 0.3, y * 0.5, 0))
                                _apply_dim_expr(d, ey)
                            dr = sketch.sketchDimensions.addRadialDimension(
                                curve, adsk.core.Point3D.create(x + r + 0.2, y + r + 0.2, 0))
                            _apply_dim_expr(dr, er)
                        except Exception:
                            pass
                sketches[step['id']] = sketch
            elif kind in ('pad', 'pocket'):
                sketch = sketches[step['sketch']]
                profiles = adsk.core.ObjectCollection.create()
                for i in range(sketch.profiles.count):
                    profiles.add(sketch.profiles.item(i))
                if profiles.count == 0:
                    raise RuntimeError('Sketch has no closed profiles: ' + step['sketch'])
                operations = adsk.fusion.FeatureOperations
                operation = (operations.CutFeatureOperation if kind == 'pocket'
                             else (operations.NewBodyFeatureOperation if not features else operations.JoinFeatureOperation))
                feature_input = component.features.extrudeFeatures.createInput(profiles, operation)
                if kind == 'pocket' and step.get('through_all'):
                    extent = adsk.fusion.ThroughAllExtentDefinition.create()
                else:
                    dist_expr = _fusion_expr(step, 'distance', raw_step) or (str(step['distance']) + ' mm')
                    extent = adsk.fusion.DistanceExtentDefinition.create(adsk.core.ValueInput.createByString(dist_expr))
                if kind == 'pad':
                    direction = (adsk.fusion.ExtentDirections.NegativeExtentDirection
                                 if step.get('direction') == 'reverse'
                                 else adsk.fusion.ExtentDirections.PositiveExtentDirection)
                else:
                    direction = (adsk.fusion.ExtentDirections.PositiveExtentDirection
                                 if step.get('direction') == 'normal'
                                 else adsk.fusion.ExtentDirections.NegativeExtentDirection)
                feature_input.setOneSideExtent(extent, direction)
                feature = component.features.extrudeFeatures.add(feature_input)
                feature.name = step['id']
                annotate(feature, step)
                sketch.isVisible = False
                if component.bRepBodies.count != 1 or not component.bRepBodies.item(0).isSolid:
                    raise RuntimeError('Expected exactly one solid body')
                body = component.bRepBodies.item(0)
                volume = body.volume * 1000.0
                if kind == 'pad' and volume <= previous_volume:
                    raise RuntimeError('Pad did not add material')
                if kind == 'pocket' and volume >= previous_volume:
                    raise RuntimeError('Pocket did not remove material')
                previous_volume = volume
                prov_by_temp_id, prev_snapshots = _tag_and_update_provenance(body, feature, step['id'], prev_snapshots)
                features.append({'id': step['id'], 'kind': kind, 'volume_mm3': volume})
            elif kind in ('fillet', 'chamfer'):
                body = component.bRepBodies.item(0)
                edges = _select_edges_fusion(body, prov_by_temp_id, step['edges'])
                if kind == 'fillet':
                    r_expr = _fusion_expr(step, 'radius', raw_step) or (str(step['radius']) + ' mm')
                    f_input = component.features.filletFeatures.createInput()
                    f_input.edgeSetInputs.addConstantRadiusEdgeSet(
                        edges, adsk.core.ValueInput.createByString(r_expr), True)
                    feature = component.features.filletFeatures.add(f_input)
                else:
                    s_expr = _fusion_expr(step, 'size', raw_step) or (str(step['size']) + ' mm')
                    c_feats = component.features.chamferFeatures
                    if hasattr(c_feats, 'createInput2'):
                        c_input = c_feats.createInput2()
                        c_input.chamferEdgeSets.addEqualDistanceChamferEdgeSet(
                            edges, adsk.core.ValueInput.createByString(s_expr), True)
                    else:
                        c_input = c_feats.createInput(edges, True)
                        c_input.setToEqualDistance(adsk.core.ValueInput.createByString(s_expr))
                    feature = c_feats.add(c_input)
                feature.name = step['id']
                annotate(feature, step)
                body = component.bRepBodies.item(0)
                if not body.isSolid:
                    raise RuntimeError(f'{step["id"]}: {kind} produced invalid body')
                volume = body.volume * 1000.0
                if abs(volume - previous_volume) < 1e-6:
                    raise RuntimeError(f'{step["id"]}: {kind} did not alter solid geometry')
                previous_volume = volume
                prov_by_temp_id, prev_snapshots = _tag_and_update_provenance(body, feature, step['id'], prev_snapshots)
                features.append({'id': step['id'], 'kind': kind, 'matched_edges': edges.count, 'volume_mm3': volume})
            else:
                raise RuntimeError(f'Unsupported step kind: {kind}')

        design.computeAll()
        for plane in datums.values():
            if hasattr(plane, 'isLightBulbOn'):
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
            if saved_root.bRepBodies.count != 1 or abs(saved_root.bRepBodies.item(0).volume - body.volume) * 1000 > 0.01:
                raise RuntimeError('F3D reopened geometry mismatch')
            saved_attr = saved_root.attributes.itemByName('FCOH', 'document')
            if not saved_attr or json.loads(saved_attr.value) != raw_doc:
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
        return {
            'ok': True, 'cad': 'fusion', 'fcoh': plan['fcoh'], 'version': app.version,
            'volume_mm3': body.volume * 1000.0,
            'bounds_mm': [(getattr(bounds.maxPoint, a) - getattr(bounds.minPoint, a)) * 10.0 for a in ['x', 'y', 'z']],
            'solid_count': component.bRepBodies.count, 'valid_shape': body.isSolid,
            'features': features, 'timeline_count': design.timeline.count,
            'reopened_native': True, 'fcoh_metadata_and_order_preserved': True,
            'artifacts': {
                p.name: {'path': str(p), 'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in artifacts
            },
        }
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
            write_json(root / '.runtime' / 'fusion-heartbeat.json', {'timestamp': time.time(), 'version': fusion_version, 'bridge': '0.1.2'})
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
