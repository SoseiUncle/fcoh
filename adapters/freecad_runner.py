"""Run inside the bundled FreeCAD Python (no pip packages needed)."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import traceback

import FreeCAD as App
import Part
import Sketcher


SIDE_VECTORS = {
    '+X': App.Vector(1, 0, 0),
    '-X': App.Vector(-1, 0, 0),
    '+Y': App.Vector(0, 1, 0),
    '-Y': App.Vector(0, -1, 0),
    '+Z': App.Vector(0, 0, 1),
    '-Z': App.Vector(0, 0, -1),
}

AXIS_VECTORS = {
    'X': App.Vector(1, 0, 0),
    'Y': App.Vector(0, 1, 0),
    'Z': App.Vector(0, 0, 1),
}


def annotate_step(obj, item):
    for field, prop in [('id', 'FcohId'), ('meaning', 'Meaning'), ('why', 'Why')]:
        obj.addProperty('App::PropertyString', prop, 'FCOH')
        setattr(obj, prop, str(item.get(field) or ''))


def annotate_datum(obj, item):
    for field, prop in [('id', 'FcohDatumId'), ('meaning', 'Meaning')]:
        obj.addProperty('App::PropertyString', prop, 'FCOH')
        setattr(obj, prop, str(item.get(field) or ''))


def _freecad_expr(item_plan, key, item_raw=None):
    exprs = item_plan.get('exprs') or {}
    if key in exprs:
        return exprs[key]['freecad_expr']
    if item_raw is not None and isinstance(item_raw.get(key), str):
        return 'Params.' + item_raw[key]
    return None


def _sample_face_points(face, max_samples=12):
    """Return list of (point, outward_normal) reliably inside the face (even with holes)."""
    samples = []
    try:
        verts, tris = face.tessellate(0.5)
    except Exception:
        verts, tris = [], []
    if verts and tris:
        ranked = []
        for a, b, c in tris:
            va, vb, vc = verts[a], verts[b], verts[c]
            area2 = (vb - va).cross(vc - va).Length
            if area2 > 1e-8:
                ranked.append((area2, (va + vb + vc) * (1.0 / 3.0)))
        ranked.sort(key=lambda t: t[0], reverse=True)
        for _, pt in ranked[:max_samples]:
            try:
                u, v = face.Surface.parameter(pt)
                p = face.valueAt(u, v)
                n = face.normalAt(u, v)
                if n.Length > 1e-9:
                    n.normalize()
                    samples.append((p, n))
            except Exception:
                continue
    if not samples:
        u0, u1, v0, v1 = face.ParameterRange
        u, v = 0.5 * (u0 + u1), 0.5 * (v0 + v1)
        p = face.valueAt(u, v)
        n = face.normalAt(u, v)
        if n.Length > 1e-9:
            n.normalize()
            samples.append((p, n))
    return samples


def _point_matches_face(pt, normal, face, tol=1e-3):
    try:
        dist = face.distToShape(Part.Vertex(pt))[0]
        if dist > tol:
            return False
        u, v = face.Surface.parameter(pt)
        fn = face.normalAt(u, v)
        if fn.Length <= 1e-9:
            return False
        fn.normalize()
        return fn.dot(normal) > 0.99
    except Exception:
        return False


def update_face_provenance(prev_shape, prev_prov, curr_shape, step_id):
    """Track which FCOH feature(s) produced each face in curr_shape."""
    if prev_shape is None:
        return [{step_id} for _ in curr_shape.Faces]
    old_faces = list(prev_shape.Faces)
    new_prov = []
    for face in curr_shape.Faces:
        samples = _sample_face_points(face)
        owners = set()
        has_new_region = False
        for pt, normal in samples:
            matched_any = False
            for idx, old_face in enumerate(old_faces):
                if _point_matches_face(pt, normal, old_face):
                    owners.update(prev_prov[idx])
                    matched_any = True
            if not matched_any:
                has_new_region = True
        if has_new_region or not owners:
            owners.add(step_id)
        new_prov.append(owners)
    return new_prov


def _face_avg_normal(face):
    samples = _sample_face_points(face, max_samples=4)
    if not samples:
        return App.Vector(0, 0, 0)
    acc = App.Vector(0, 0, 0)
    for _, n in samples:
        acc = acc + n
    if acc.Length > 1e-9:
        acc.normalize()
    return acc


def _edge_convexity(edge, f1, f2):
    """Classify dihedral edge as 'convex', 'concave', or 'smooth' using adjacent face normals."""
    mid_param = 0.5 * (edge.FirstParameter + edge.LastParameter)
    mid = edge.valueAt(mid_param)
    try:
        u1, v1 = f1.Surface.parameter(mid)
        n1 = f1.normalAt(u1, v1)
        n1.normalize()
        u2, v2 = f2.Surface.parameter(mid)
        n2 = f2.normalAt(u2, v2)
        n2.normalize()
    except Exception:
        return 'smooth'

    def tangent_inward(face, normal):
        samples = _sample_face_points(face, max_samples=12)
        best = None
        best_dist = float('inf')
        for pt, _ in samples:
            diff = pt - mid
            tang = diff - normal * diff.dot(normal)
            if tang.Length > 1e-5 and diff.Length < best_dist:
                best = tang
                best_dist = diff.Length
        if best is None:
            return None
        best.normalize()
        return best

    t1 = tangent_inward(f1, n1)
    t2 = tangent_inward(f2, n2)
    scores = []
    if t2 is not None:
        scores.append(n1.dot(t2))
    if t1 is not None:
        scores.append(n2.dot(t1))
    if not scores:
        return 'smooth'
    score = sum(scores) / len(scores)
    if score > 0.05:
        return 'concave'
    if score < -0.05:
        return 'convex'
    return 'smooth'


def select_face(shape, face_prov, selector):
    """Resolve semantic face selector {of, side} -> (face_index_1based, Part.Face)."""
    feat_id = selector['of']
    side_vec = SIDE_VECTORS[selector['side']]
    candidates = []
    for idx, face in enumerate(shape.Faces):
        if feat_id not in face_prov[idx]:
            continue
        normal = _face_avg_normal(face)
        if normal.dot(side_vec) > 0.98:
            samples = _sample_face_points(face, max_samples=1)
            coord = samples[0][0].dot(side_vec) if samples else 0.0
            candidates.append((coord, idx + 1, face))
    if not candidates:
        raise RuntimeError(f'Face selector matched 0 faces: {selector}')
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1], candidates[0][2]


def select_edges(shape, face_prov, selector):
    """Resolve semantic edge selector -> list of FreeCAD edge names (['Edge1', ...])."""
    face_map = {}
    for idx, face in enumerate(shape.Faces):
        face_map[face.hashCode()] = (idx, face)

    matched = []
    for e_idx, edge in enumerate(shape.Edges, 1):
        adj_faces = shape.ancestorsOfType(edge, Part.Face)
        if len(adj_faces) != 2:
            continue
        info = []
        for af in adj_faces:
            entry = face_map.get(af.hashCode())
            if entry is None:
                for idx, f in enumerate(shape.Faces):
                    if f.isSame(af):
                        entry = (idx, f)
                        break
            if entry is not None:
                info.append(entry)
        if len(info) != 2:
            continue
        (i1, f1), (i2, f2) = info
        p1, p2 = face_prov[i1], face_prov[i2]

        if 'between' in selector:
            a, b = selector['between']
            hit = ((a in (p1 - p2) and b in (p2 - p1)) or (b in (p1 - p2) and a in (p2 - p1)))
            if not hit:
                continue
        if 'of' in selector:
            fid = selector['of']
            if fid not in p1 and fid not in p2:
                continue
        if 'kind' in selector:
            is_line = isinstance(edge.Curve, Part.Line)
            is_circle = isinstance(edge.Curve, Part.Circle)
            if selector['kind'] == 'line' and not is_line:
                continue
            if selector['kind'] == 'circle' and not is_circle:
                continue
        if 'orientation' in selector:
            if not isinstance(edge.Curve, Part.Line):
                continue
            axis = AXIS_VECTORS[selector['orientation']]
            d = edge.Curve.Direction
            if d.Length <= 1e-9:
                continue
            d = App.Vector(d)
            d.normalize()
            if abs(d.dot(axis)) < 0.99:
                continue
        if 'side' in selector:
            sv = SIDE_VECTORS[selector['side']]
            n1 = _face_avg_normal(f1)
            n2 = _face_avg_normal(f2)
            if n1.dot(sv) <= 0.98 and n2.dot(sv) <= 0.98:
                continue
        if 'convexity' in selector and selector['convexity'] != 'any':
            conv = _edge_convexity(edge, f1, f2)
            if conv != selector['convexity']:
                continue
        matched.append(f'Edge{e_idx}')

    if not matched:
        raise RuntimeError(f'Edge selector matched 0 edges: {selector}')
    return matched


def _bind_sketch_expr(sketch, c_idx, expr_text):
    if expr_text:
        sketch.setExpression(f'Constraints[{c_idx}]', expr_text)


def _export_preview_svg(shape, svg_path, meta, bounds_mm, volume_mm3):
    """Generate a headless isometric SVG preview using bundled TechDraw."""
    try:
        import TechDraw
        frag = TechDraw.projectToSVG(shape, App.Vector(1, -1, 0.9))
    except Exception:
        return False
    xs, ys = [], []
    num_re = r'[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?'
    for d_attr in re.findall(r'd="([^"]+)"', frag):
        for m in re.finditer(rf'([ML])\s*({num_re})\s*,?\s*({num_re})', d_attr):
            xs.append(float(m.group(2)))
            ys.append(-float(m.group(3)))
        for m in re.finditer(rf'A\s*{num_re}\s*,?\s*{num_re}\s+{num_re}\s+[01]\s+[01]\s+({num_re})\s*,?\s*({num_re})', d_attr):
            xs.append(float(m.group(1)))
            ys.append(-float(m.group(2)))
    if len(xs) >= 2 and len(ys) >= 2:
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        w = max(max_x - min_x, 1.0)
        h = max(max_y - min_y, 1.0)
        pad = max(w, h) * 0.14
        vx, vy, vw, vh = min_x - pad, min_y - pad, w + 2 * pad, h + 2 * pad
    else:
        vx, vy, vw, vh = -100, -100, 200, 200
    frag = re.sub(r'stroke-width="[^"]+"', 'stroke-width="0.45"', frag)
    bx, by, bz = bounds_mm
    title = f'{meta["id"]} ({bx:.1f} x {by:.1f} x {bz:.1f} mm, {volume_mm3:.1f} mm^3)'
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="960" height="720" '
        f'viewBox="{vx:.3f} {vy:.3f} {vw:.3f} {vh:.3f}">\n'
        f'  <title>{title}</title>\n'
        f'  <rect x="{vx:.3f}" y="{vy:.3f}" width="{vw:.3f}" height="{vh:.3f}" fill="#faf8f5"/>\n'
        f'  {frag}\n'
        f'</svg>\n'
    )
    svg_path.write_text(svg, encoding='utf-8')
    return True


def build(job_dir):
    request = json.loads((job_dir / 'request.json').read_text(encoding='utf-8'))
    plan = request['plan']
    raw_doc = request['document']
    doc = App.newDocument(plan['meta']['id'])
    doc.Label = plan['meta']['name']
    try:
        body = doc.addObject('PartDesign::Body', 'Body')
        body.addProperty('App::PropertyString', 'FcohDocument', 'FCOH')
        body.FcohDocument = json.dumps(raw_doc, ensure_ascii=False)

        params = doc.addObject('Spreadsheet::Sheet', 'Params')
        params.set('A1', 'Parameter')
        params.set('B1', 'Value (mm)')
        params.set('C1', 'Meaning')
        params.set('D1', 'Expression')
        param_order = plan.get('param_order', list(plan['params'].keys()))
        for i, name in enumerate(param_order, 2):
            param = plan['params'][name]
            params.set(f'A{i}', name)
            expr_meta = param.get('expr')
            if expr_meta:
                params.set(f'B{i}', expr_meta['freecad_sheet'])
                params.set(f'D{i}', expr_meta['raw'])
            else:
                params.set(f'B{i}', str(param['value']) + ' mm')
            params.setAlias(f'B{i}', name)
            params.set(f'C{i}', param.get('meaning') or '')
        doc.recompute()

        origin_planes = {
            o.Role: o for o in body.Origin.OriginFeatures
            if hasattr(o, 'Role') and o.Role in ('XY_Plane', 'XZ_Plane', 'YZ_Plane')
        }
        raw_datums = {d['id']: d for d in raw_doc['datums']}
        plan_datums = {d['id']: d for d in plan['datums']}
        datum_objs = {}
        datum_z_exprs = {}  # datum_id -> freecad expression for XY plane z

        def create_plane_datum(datum):
            plane_name = datum.get('plane', 'XY')
            role = f'{plane_name}_Plane'
            dp = body.newObject('PartDesign::Plane', datum['id'])
            dp.AttachmentSupport = [(origin_planes[role], '')]
            dp.MapMode = 'FlatFace'
            dp.AttachmentOffset = App.Placement(App.Vector(0, 0, datum['offset']), App.Rotation())
            expr = _freecad_expr(datum, 'offset', raw_datums[datum['id']])
            if expr:
                dp.setExpression('AttachmentOffset.Base.z', expr)
                if plane_name == 'XY':
                    datum_z_exprs[datum['id']] = expr
            elif plane_name == 'XY':
                datum_z_exprs[datum['id']] = f'{datum["offset"]:g} mm'
            annotate_datum(dp, datum)
            dp.Visibility = False
            datum_objs[datum['id']] = dp
            return dp

        # Create origin-based plane datums up front
        for datum in plan['datums']:
            if datum['kind'] == 'plane':
                create_plane_datum(datum)
        doc.recompute()

        raw_steps = {s['id']: s for s in raw_doc['steps']}
        sketches, features = {}, []
        last = None
        face_prov = None
        feature_top_z_expr = {}  # feat_id -> ('+Z': expr, '-Z': expr) for XY pads

        def ensure_datum(datum_id):
            if datum_id in datum_objs:
                return datum_objs[datum_id]
            datum = plan_datums[datum_id]
            if datum['kind'] != 'face_plane':
                return create_plane_datum(datum)
            if last is None:
                raise RuntimeError(f'Datum {datum_id!r} needs an existing solid')
            _, target_face = select_face(last.Shape, face_prov, datum['face'])
            side = datum['face']['side']
            plane_name = datum['resolved_plane']
            role = f'{plane_name}_Plane'
            axis_sign = 1.0 if side.startswith('+') else -1.0
            samples = _sample_face_points(target_face, max_samples=1)
            pt = samples[0][0]
            if plane_name == 'XY':
                base_coord = pt.z
            elif plane_name == 'XZ':
                base_coord = pt.y
            else:
                base_coord = pt.x
            total_offset = base_coord + axis_sign * datum['offset']
            dp = body.newObject('PartDesign::Plane', datum['id'])
            dp.AttachmentSupport = [(origin_planes[role], '')]
            dp.MapMode = 'FlatFace'
            # Note: XZ_Plane normal in FreeCAD is -Y, YZ_Plane normal is +X, XY_Plane normal is +Z
            local_z = -total_offset if plane_name == 'XZ' else total_offset
            dp.AttachmentOffset = App.Placement(App.Vector(0, 0, local_z), App.Rotation())
            # Bind symbolic expression when referencing a tracked XY pad face
            ref_feat = datum['face']['of']
            if plane_name == 'XY' and ref_feat in feature_top_z_expr and side in feature_top_z_expr[ref_feat]:
                base_expr = feature_top_z_expr[ref_feat][side]
                off_expr = _freecad_expr(datum, 'offset', raw_datums[datum['id']])
                if off_expr:
                    sign_str = '+' if axis_sign > 0 else '-'
                    full_expr = f'({base_expr}) {sign_str} ({off_expr})'
                elif abs(datum['offset']) > 1e-9:
                    sign_str = '+' if axis_sign > 0 else '-'
                    full_expr = f'({base_expr}) {sign_str} {datum["offset"]:g} mm'
                else:
                    full_expr = base_expr
                dp.setExpression('AttachmentOffset.Base.z', full_expr)
                datum_z_exprs[datum['id']] = full_expr
            annotate_datum(dp, datum)
            dp.Visibility = False
            datum_objs[datum['id']] = dp
            doc.recompute()
            return dp

        for step in plan['steps']:
            kind = step['kind']
            raw_step = raw_steps[step['id']]
            if kind == 'sketch':
                dp = ensure_datum(step['datum'])
                sketch = body.newObject('Sketcher::SketchObject', step['id'])
                sketch.AttachmentSupport = [(dp, '')]
                sketch.MapMode = 'FlatFace'
                annotate_step(sketch, step)
                sketch.addProperty('App::PropertyString', 'FcohEntities', 'FCOH')
                sketch.FcohEntities = json.dumps(step['entities'], ensure_ascii=False)
                raw_entities = {e['id']: e for e in raw_step['entities']}
                for entity in step['entities']:
                    raw_ent = raw_entities[entity['id']]
                    x, y = entity['x'], entity['y']
                    if entity['kind'] == 'rectangle':
                        w, h = entity['width'], entity['height']
                        g0 = sketch.addGeometry(Part.LineSegment(App.Vector(x, y, 0), App.Vector(x + w, y, 0)), False)
                        g1 = sketch.addGeometry(Part.LineSegment(App.Vector(x + w, y, 0), App.Vector(x + w, y + h, 0)), False)
                        g2 = sketch.addGeometry(Part.LineSegment(App.Vector(x + w, y + h, 0), App.Vector(x, y + h, 0)), False)
                        g3 = sketch.addGeometry(Part.LineSegment(App.Vector(x, y + h, 0), App.Vector(x, y, 0)), False)
                        sketch.addConstraint([
                            Sketcher.Constraint('Coincident', g0, 2, g1, 1),
                            Sketcher.Constraint('Coincident', g1, 2, g2, 1),
                            Sketcher.Constraint('Coincident', g2, 2, g3, 1),
                            Sketcher.Constraint('Coincident', g3, 2, g0, 1),
                            Sketcher.Constraint('Horizontal', g0),
                            Sketcher.Constraint('Horizontal', g2),
                            Sketcher.Constraint('Vertical', g1),
                            Sketcher.Constraint('Vertical', g3),
                        ])
                        cx = sketch.addConstraint(Sketcher.Constraint('DistanceX', -1, 1, g0, 1, x))
                        cy = sketch.addConstraint(Sketcher.Constraint('DistanceY', -1, 1, g0, 1, y))
                        cw = sketch.addConstraint(Sketcher.Constraint('DistanceX', g0, 1, g0, 2, w))
                        ch = sketch.addConstraint(Sketcher.Constraint('DistanceY', g1, 1, g1, 2, h))
                        _bind_sketch_expr(sketch, cx, _freecad_expr(entity, 'x', raw_ent))
                        _bind_sketch_expr(sketch, cy, _freecad_expr(entity, 'y', raw_ent))
                        _bind_sketch_expr(sketch, cw, _freecad_expr(entity, 'width', raw_ent))
                        _bind_sketch_expr(sketch, ch, _freecad_expr(entity, 'height', raw_ent))
                    else:
                        r = entity['radius']
                        gc = sketch.addGeometry(Part.Circle(App.Vector(x, y, 0), App.Vector(0, 0, 1), r), False)
                        cx = sketch.addConstraint(Sketcher.Constraint('DistanceX', -1, 1, gc, 3, x))
                        cy = sketch.addConstraint(Sketcher.Constraint('DistanceY', -1, 1, gc, 3, y))
                        cr = sketch.addConstraint(Sketcher.Constraint('Radius', gc, r))
                        _bind_sketch_expr(sketch, cx, _freecad_expr(entity, 'x', raw_ent))
                        _bind_sketch_expr(sketch, cy, _freecad_expr(entity, 'y', raw_ent))
                        _bind_sketch_expr(sketch, cr, _freecad_expr(entity, 'radius', raw_ent))
                sketches[step['id']] = sketch
                doc.recompute()
            elif kind in ('pad', 'pocket'):
                sketch = sketches[step['sketch']]
                obj_type = 'PartDesign::Pad' if kind == 'pad' else 'PartDesign::Pocket'
                feature = body.newObject(obj_type, step['id'])
                feature.Profile = sketch
                if kind == 'pad':
                    feature.Reversed = (step.get('direction') == 'reverse')
                    feature.Length = step['distance']
                    dist_expr = _freecad_expr(step, 'distance', raw_step)
                    if dist_expr:
                        feature.setExpression('Length', dist_expr)
                    # Record symbolic +Z / -Z expressions for downstream face_plane datums
                    sk_plan = next(s for s in plan['steps'] if s['id'] == step['sketch'])
                    if sk_plan.get('plane', 'XY') == 'XY' and sk_plan['datum'] in datum_z_exprs:
                        base_e = datum_z_exprs[sk_plan['datum']]
                        d_e = dist_expr or f'{step["distance"]:g} mm'
                        if step.get('direction') == 'reverse':
                            feature_top_z_expr[step['id']] = {'+Z': base_e, '-Z': f'({base_e}) - ({d_e})'}
                        else:
                            feature_top_z_expr[step['id']] = {'-Z': base_e, '+Z': f'({base_e}) + ({d_e})'}
                else:
                    feature.Reversed = (step.get('direction') == 'normal')
                    if step.get('through_all'):
                        feature.Type = 'ThroughAll'
                    else:
                        feature.Length = step['distance']
                        dist_expr = _freecad_expr(step, 'distance', raw_step)
                        if dist_expr:
                            feature.setExpression('Length', dist_expr)
                annotate_step(feature, step)
                prev_shape = last.Shape.copy() if last is not None else None
                doc.recompute()
                if feature.Shape.isNull() or not feature.Shape.isValid() or len(feature.Shape.Solids) != 1:
                    raise RuntimeError(f'{step["id"]}: failed to produce one valid solid; {feature.State}')
                if last is not None:
                    if kind == 'pad' and feature.Shape.Volume <= last.Shape.Volume:
                        raise RuntimeError('Pad did not add material')
                    if kind == 'pocket' and feature.Shape.Volume >= last.Shape.Volume:
                        raise RuntimeError('Pocket did not remove material')
                    last.Visibility = False
                sketch.Visibility = False
                face_prov = update_face_provenance(prev_shape, face_prov, feature.Shape, step['id'])
                features.append({'id': step['id'], 'kind': kind, 'volume_mm3': feature.Shape.Volume})
                last = feature
            elif kind in ('fillet', 'chamfer'):
                if last is None:
                    raise RuntimeError(f'{step["id"]}: {kind} requires an existing solid')
                edge_names = select_edges(last.Shape, face_prov, step['edges'])
                prev_shape = last.Shape.copy()
                if kind == 'fillet':
                    feature = body.newObject('PartDesign::Fillet', step['id'])
                    feature.Base = (last, edge_names)
                    feature.Radius = step['radius']
                    rad_expr = _freecad_expr(step, 'radius', raw_step)
                    if rad_expr:
                        feature.setExpression('Radius', rad_expr)
                else:
                    feature = body.newObject('PartDesign::Chamfer', step['id'])
                    feature.Base = (last, edge_names)
                    feature.Size = step['size']
                    size_expr = _freecad_expr(step, 'size', raw_step)
                    if size_expr:
                        feature.setExpression('Size', size_expr)
                annotate_step(feature, step)
                doc.recompute()
                if feature.Shape.isNull() or not feature.Shape.isValid() or len(feature.Shape.Solids) != 1:
                    raise RuntimeError(f'{step["id"]}: {kind} failed to produce one valid solid; {feature.State}')
                if abs(feature.Shape.Volume - prev_shape.Volume) < 1e-6:
                    raise RuntimeError(f'{step["id"]}: {kind} did not alter solid geometry')
                last.Visibility = False
                face_prov = update_face_provenance(prev_shape, face_prov, feature.Shape, step['id'])
                features.append({
                    'id': step['id'], 'kind': kind,
                    'matched_edges': len(edge_names),
                    'volume_mm3': feature.Shape.Volume,
                })
                last = feature
            else:
                raise RuntimeError(f'Unsupported step kind: {kind}')

        body.Tip = last
        doc.recompute()
        native = job_dir / 'part.FCStd'
        step_file = job_dir / 'part.step'
        svg_file = job_dir / 'preview.svg'
        doc.saveAs(str(native))
        Part.export([last], str(step_file))
        box = last.Shape.BoundBox
        bounds_mm = [box.XLength, box.YLength, box.ZLength]
        svg_ok = _export_preview_svg(last.Shape, svg_file, plan['meta'], bounds_mm, last.Shape.Volume)
        out_files = [native, step_file] + ([svg_file] if svg_ok and svg_file.exists() else [])
        result = {
            'ok': True,
            'cad': 'freecad',
            'fcoh': plan['fcoh'],
            'version': '.'.join(App.Version()[:3]),
            'volume_mm3': last.Shape.Volume,
            'bounds_mm': bounds_mm,
            'solid_count': len(last.Shape.Solids),
            'valid_shape': last.Shape.isValid(),
            'features': features,
            'artifacts': {
                p.name: {'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'bytes': p.stat().st_size}
                for p in out_files
            },
        }
        # Reopen native and neutral artifacts to check actual persistence.
        App.closeDocument(doc.Name)
        doc = App.openDocument(str(native))
        reopened = doc.getObject('Body').Tip.Shape
        if abs(reopened.Volume - result['volume_mm3']) > 1e-5:
            raise RuntimeError('Native file volume changed after reopen')
        imported = Part.Shape()
        imported.read(str(step_file))
        if not imported.isValid() or abs(imported.Volume - result['volume_mm3']) > 1e-4:
            raise RuntimeError('STEP round-trip geometry mismatch')
        persisted_document = json.loads(doc.getObject('Body').FcohDocument)
        if persisted_document != raw_doc:
            raise RuntimeError('FCOH document metadata changed after reopen')
        persisted_ids = [o.FcohId for o in doc.Objects if 'FcohId' in o.PropertiesList]
        if persisted_ids != [s['id'] for s in plan['steps']]:
            raise RuntimeError(f'Feature/sketch order changed after reopen: {persisted_ids}')
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
