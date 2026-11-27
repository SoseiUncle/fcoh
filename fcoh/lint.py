"""Pre-CAD design intent and lightweight geometric lint checks."""
import math

from .expr import extract_refs

MIN_WALL_WARNING_MM = 2.0


def _rect_bbox(entity):
    x0, y0 = entity['x'], entity['y']
    return (x0, y0, x0 + entity['width'], y0 + entity['height'])


def _circle_bbox(entity):
    x, y, r = entity['x'], entity['y'], entity['radius']
    return (x - r, y - r, x + r, y + r)


def _boxes_overlap(a, b, tol=1e-6):
    return not (a[2] <= b[0] + tol or b[2] <= a[0] + tol or a[3] <= b[1] + tol or b[3] <= a[1] + tol)


def _circle_clearance_inside_rect(circle, rect):
    """Signed clearance from circle perimeter to rectangle boundary (>0 means fully inside)."""
    cx, cy, r = circle['x'], circle['y'], circle['radius']
    x0, y0, x1, y1 = _rect_bbox(rect)
    return min((cx - r) - x0, x1 - (cx + r), (cy - r) - y0, y1 - (cy + r))


def _circle_distance_outside_rect(circle, rect):
    """Signed clearance between a circle and an exterior obstacle rectangle (<0 means overlap)."""
    cx, cy, r = circle['x'], circle['y'], circle['radius']
    x0, y0, x1, y1 = _rect_bbox(rect)
    dx = max(x0 - cx, 0.0, cx - x1)
    dy = max(y0 - cy, 0.0, cy - y1)
    if dx == 0.0 and dy == 0.0:
        # Center is inside obstacle rect
        inside_pen = min(cx - x0, x1 - cx, cy - y0, y1 - cy)
        return -(inside_pen + r)
    return math.hypot(dx, dy) - r


def _collect_used_params(document):
    used = set()

    def scan_dim(val):
        if isinstance(val, str):
            try:
                used.update(extract_refs(val))
            except Exception:
                pass

    for param in document.get('params', {}).values():
        scan_dim(param.get('value'))
        scan_dim(param.get('min'))
        scan_dim(param.get('max'))
    for datum in document.get('datums', []):
        scan_dim(datum.get('offset'))
    for step in document.get('steps', []):
        for key in ('distance', 'radius', 'size'):
            scan_dim(step.get(key))
        for ent in step.get('entities', []):
            for key in ('x', 'y', 'width', 'height', 'radius'):
                scan_dim(ent.get(key))
    return used


def lint_plan(document, plan):
    """Run fast, CAD-independent checks on design intent and 2D/3D consistency."""
    issues = []

    def add(severity, code, target, message):
        issues.append({'severity': severity, 'code': code, 'target': target, 'message': message})

    # 1. Parameter bounds (min/max) & missing meanings
    used_params = _collect_used_params(document)
    unused_params = []
    missing_intent = []

    for name, param in plan['params'].items():
        val = param['value']
        if 'min' in param and val < param['min'] - 1e-9:
            add('error', 'param_below_min', name, f'Parameter {name}={val:g} mm is below min ({param["min"]:g} mm)')
        if 'max' in param and val > param['max'] + 1e-9:
            add('error', 'param_above_max', name, f'Parameter {name}={val:g} mm exceeds max ({param["max"]:g} mm)')
        if name not in used_params:
            unused_params.append(name)
            add('warning', 'unused_param', name, f'Parameter {name!r} is defined but never referenced')
        if not (param.get('meaning') or '').strip():
            missing_intent.append(name)
            add('warning', 'missing_intent', name, f'Parameter {name!r} has no meaning text')

    for datum in document.get('datums', []):
        if not (datum.get('meaning') or '').strip():
            missing_intent.append(datum['id'])
            add('warning', 'missing_intent', datum['id'], f'Datum {datum["id"]!r} has no meaning text')

    for step in document.get('steps', []):
        if not (step.get('meaning') or '').strip() or not (step.get('why') or '').strip():
            missing_intent.append(step['id'])
            add('warning', 'missing_intent', step['id'], f'Step {step["id"]!r} is missing meaning or why')

    # 2. Track XY-aligned 2D pad/pocket spans for overlap & clearance checks
    datums = {d['id']: d for d in plan['datums']}
    sketches = {}
    xy_pads = []  # list of (step_id, z0, z1, entities)
    feature_min_dim = {}
    min_hole_edge_clearance = None

    for step in plan['steps']:
        kind = step['kind']
        if kind == 'sketch':
            sketches[step['id']] = step
            # Check circle-to-circle collisions within the same sketch
            circles = [e for e in step['entities'] if e['kind'] == 'circle']
            for i in range(len(circles)):
                for j in range(i + 1, len(circles)):
                    a, b = circles[i], circles[j]
                    gap = math.hypot(a['x'] - b['x'], a['y'] - b['y']) - (a['radius'] + b['radius'])
                    if gap <= 0:
                        add('error', 'entity_overlap', step['id'],
                            f'Circles {a["id"]} and {b["id"]} overlap (gap {gap:.2f} mm)')
                    elif gap < MIN_WALL_WARNING_MM:
                        add('warning', 'thin_web_between_holes', step['id'],
                            f'Thin material between {a["id"]} and {b["id"]} ({gap:.2f} mm < {MIN_WALL_WARNING_MM:g} mm)')
        elif kind == 'pad':
            sk = sketches[step['sketch']]
            datum = datums[sk['datum']]
            plane_kind = datum.get('plane') if datum['kind'] == 'plane' else datum.get('resolved_plane')
            dims = [step['distance']]
            for ent in sk['entities']:
                if ent['kind'] == 'rectangle':
                    dims.extend([ent['width'], ent['height']])
                else:
                    dims.append(ent['radius'] * 2)
            feature_min_dim[step['id']] = min(dims)
            if plane_kind == 'XY' and sk.get('z') is not None:
                z0 = sk['z']
                sign = -1.0 if step.get('direction') == 'reverse' else 1.0
                z1 = z0 + sign * step['distance']
                xy_pads.append((step['id'], min(z0, z1), max(z0, z1), sk['entities']))
        elif kind == 'pocket':
            sk = sketches[step['sketch']]
            datum = datums[sk['datum']]
            plane_kind = datum.get('plane') if datum['kind'] == 'plane' else datum.get('resolved_plane')
            if step.get('distance') is not None:
                feature_min_dim[step['id']] = step['distance']
            if plane_kind == 'XY' and sk.get('z') is not None and xy_pads:
                z0 = sk['z']
                sign = 1.0 if step.get('direction') == 'normal' else -1.0
                if step.get('through_all'):
                    pz_min, pz_max = (-1e6, z0) if sign < 0 else (z0, 1e6)
                else:
                    z1 = z0 + sign * step['distance']
                    pz_min, pz_max = min(z0, z1), max(z0, z1)
                active_pads = [p for p in xy_pads if not (p[2] <= pz_min + 1e-6 or pz_max <= p[1] + 1e-6)]
                above_pads = [p for p in xy_pads if p[1] >= z0 - 1e-6]
                for ent in sk['entities']:
                    eb = _rect_bbox(ent) if ent['kind'] == 'rectangle' else _circle_bbox(ent)
                    hits = []
                    for pad_id, _, _, pents in active_pads:
                        for pe in pents:
                            pb = _rect_bbox(pe) if pe['kind'] == 'rectangle' else _circle_bbox(pe)
                            if _boxes_overlap(eb, pb):
                                hits.append((pad_id, pe))
                    if not hits:
                        add('error', 'pocket_outside_body', step['id'],
                            f'Pocket entity {ent["id"]!r} does not intersect any existing XY pad')
                    elif ent['kind'] == 'circle':
                        # Check clearance inside primary supporting rectangle
                        rect_hits = [pe for _, pe in hits if pe['kind'] == 'rectangle']
                        if rect_hits:
                            best_clr = max(_circle_clearance_inside_rect(ent, pe) for pe in rect_hits)
                            # Also check interference against walls standing above the sketch plane
                            for wall_id, _, _, wents in above_pads:
                                for we in wents:
                                    if we['kind'] == 'rectangle':
                                        wclr = _circle_distance_outside_rect(ent, we)
                                        if wclr < 0:
                                            add('warning', 'hole_hits_adjacent_feature', step['id'],
                                                f'Hole {ent["id"]!r} intersects {wall_id!r} (clearance {wclr:.2f} mm)')
                                        best_clr = min(best_clr, wclr)
                            if min_hole_edge_clearance is None or best_clr < min_hole_edge_clearance:
                                min_hole_edge_clearance = best_clr
                            if best_clr < 0:
                                add('warning', 'hole_breaks_edge', step['id'],
                                    f'Hole {ent["id"]!r} breaks out of pad boundary (clearance {best_clr:.2f} mm)')
                            elif best_clr < MIN_WALL_WARNING_MM:
                                add('warning', 'thin_hole_margin', step['id'],
                                    f'Hole {ent["id"]!r} edge clearance is thin ({best_clr:.2f} mm < {MIN_WALL_WARNING_MM:g} mm)')
        elif kind in ('fillet', 'chamfer'):
            val = step['radius'] if kind == 'fillet' else step['size']
            sel = step['edges']
            refs = list(sel.get('between', []))
            if 'of' in sel:
                refs.append(sel['of'])
            known_dims = [feature_min_dim[r] for r in refs if r in feature_min_dim]
            if known_dims:
                smallest = min(known_dims)
                if val >= smallest:
                    add('error', f'{kind}_exceeds_feature_size', step['id'],
                        f'{kind.title()} {val:g} mm >= referenced feature minimum size ({smallest:g} mm)')
                elif val > smallest * 0.5 + 1e-9:
                    add('warning', f'{kind}_large_relative_to_feature', step['id'],
                        f'{kind.title()} {val:g} mm exceeds half of referenced feature size ({smallest:g} mm)')

    errors = [i for i in issues if i['severity'] == 'error']
    warnings = [i for i in issues if i['severity'] == 'warning']
    checks = {
        'unused_params': unused_params,
        'missing_intent_ids': missing_intent,
    }
    if min_hole_edge_clearance is not None:
        checks['min_hole_edge_clearance_mm'] = round(min_hole_edge_clearance, 4)
    return {
        'ok': len(errors) == 0,
        'error_count': len(errors),
        'warning_count': len(warnings),
        'errors': errors,
        'warnings': warnings,
        'checks': checks,
    }
