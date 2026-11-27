"""Safe YAML loading, expression resolution, validation, and CAD-independent planning."""
import copy
import json
import math
from pathlib import Path

import jsonschema
import yaml

from .expr import ExprError, evaluate_dimension, resolve_params
from .lint import lint_plan

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = {
    '0.3': ROOT / 'schemas' / 'fcoh-0.3.schema.json',
    '0.4': ROOT / 'schemas' / 'fcoh-0.4.schema.json',
}
SCHEMA_PATH = SCHEMAS['0.3']


class FcohError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise FcohError('YAML aliases are not supported')
        return super().compose_node(parent, index)


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise FcohError('Mapping keys must be strings')
        if key in result:
            raise FcohError(f'Duplicate YAML key: {key}')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def project_path(path):
    resolved = (ROOT / path).resolve()
    if not resolved.is_relative_to(ROOT):
        raise FcohError('Path must remain inside the FCOH project folder')
    return resolved


def _normalize_03(data):
    schema = json.loads(SCHEMAS['0.3'].read_text(encoding='utf-8'))
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(data), key=lambda e: str(e.path))
    if errors:
        raise FcohError('; '.join(f'{"/".join(map(str, e.path)) or "/"}: {e.message}' for e in errors[:5]))
    plan = copy.deepcopy(data)
    params = data['params']

    def dimension(value, positive=False):
        if isinstance(value, str):
            if value not in params:
                raise FcohError(f'Unknown parameter: {value}')
            value = params[value]['value']
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise FcohError('Dimensions must be finite numbers')
        if positive and value <= 0:
            raise FcohError('Lengths/radii must be greater than zero')
        return float(value)

    for param in params.values():
        dimension(param['value'])
    plan['param_order'] = list(params.keys())
    ids = set()

    def claim(identifier):
        if identifier in ids:
            raise FcohError(f'Duplicate entity/step/datum id: {identifier}')
        ids.add(identifier)

    datums = {}
    for datum in plan['datums']:
        claim(datum['id'])
        datum['offset'] = dimension(datum['offset'])
        datums[datum['id']] = datum
    sketches = set()
    used = set()
    solid = False
    for step in plan['steps']:
        claim(step['id'])
        if step['kind'] == 'sketch':
            if step['datum'] not in datums:
                raise FcohError(f'Unknown datum: {step["datum"]}')
            step['z'] = datums[step['datum']]['offset']
            for entity in step['entities']:
                claim(entity['id'])
                for key in ('x', 'y', 'width', 'height', 'radius'):
                    if key in entity:
                        entity[key] = dimension(entity[key], key in ('width', 'height', 'radius'))
            sketches.add(step['id'])
        else:
            if step['sketch'] not in sketches or step['sketch'] in used:
                raise FcohError('Feature needs an earlier, unused sketch')
            if step['kind'] == 'pocket' and not solid:
                raise FcohError('Pocket requires an existing solid')
            step['distance'] = dimension(step['distance'], True)
            used.add(step['sketch'])
            solid = True
    if not solid or used != sketches:
        raise FcohError('All sketches must be consumed and at least one pad is required')
    return plan


def _side_to_plane(side):
    if side in ('+Z', '-Z'):
        return 'XY'
    if side in ('+Y', '-Y'):
        return 'XZ'
    return 'YZ'


def _normalize_04(data):
    schema = json.loads(SCHEMAS['0.4'].read_text(encoding='utf-8'))
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(data), key=lambda e: str(e.path))
    if errors:
        raise FcohError('; '.join(f'{"/".join(map(str, e.path)) or "/"}: {e.message}' for e in errors[:5]))
    plan = copy.deepcopy(data)
    try:
        order, env, param_metas = resolve_params(data['params'])
    except ExprError as error:
        raise FcohError(str(error)) from error

    def resolve_dim(raw, positive=False):
        try:
            return evaluate_dimension(raw, env, positive=positive)
        except ExprError as error:
            raise FcohError(str(error)) from error

    plan['param_order'] = order
    for name in order:
        spec = plan['params'][name]
        spec['value'] = env[name]
        if param_metas[name] is not None:
            spec['expr'] = param_metas[name]
        for bound in ('min', 'max'):
            if bound in spec:
                bval, bmeta = resolve_dim(spec[bound], positive=False)
                spec[bound] = bval
                if bmeta is not None:
                    spec.setdefault('bound_exprs', {})[bound] = bmeta
        if 'min' in spec and 'max' in spec and spec['min'] > spec['max']:
            raise FcohError(f'Parameter {name!r} has min > max')

    ids = set()

    def claim(identifier):
        if identifier in ids:
            raise FcohError(f'Duplicate entity/step/datum id: {identifier}')
        ids.add(identifier)

    datums = {}
    for datum in plan['datums']:
        claim(datum['id'])
        raw_offset = datum.get('offset', 0)
        oval, ometa = resolve_dim(raw_offset, positive=False)
        datum['offset'] = oval
        if ometa is not None:
            datum['exprs'] = {'offset': ometa}
        if datum['kind'] == 'face_plane':
            datum['resolved_plane'] = _side_to_plane(datum['face']['side'])
        datums[datum['id']] = datum

    sketches = {}
    used_sketches = set()
    solid_features = set()
    pad_z_extents = {}  # step_id -> (z_min, z_max) for XY pads
    solid = False

    for step in plan['steps']:
        claim(step['id'])
        kind = step['kind']
        if kind == 'sketch':
            if step['datum'] not in datums:
                raise FcohError(f'Unknown datum: {step["datum"]}')
            datum = datums[step['datum']]
            if datum['kind'] == 'plane':
                step['plane'] = datum['plane']
                step['z'] = datum['offset'] if datum['plane'] == 'XY' else None
            else:
                ref_feat = datum['face']['of']
                if ref_feat not in solid_features:
                    raise FcohError(
                        f'Sketch {step["id"]!r} uses face_plane datum {datum["id"]!r} '
                        f'referencing {ref_feat!r}, which has not been created yet'
                    )
                step['plane'] = datum['resolved_plane']
                side = datum['face']['side']
                if ref_feat in pad_z_extents and side in ('+Z', '-Z'):
                    base_z = pad_z_extents[ref_feat][1] if side == '+Z' else pad_z_extents[ref_feat][0]
                    step['z'] = base_z + (datum['offset'] if side == '+Z' else -datum['offset'])
                else:
                    step['z'] = None
            for entity in step['entities']:
                claim(entity['id'])
                exprs = {}
                for key in ('x', 'y', 'width', 'height', 'radius'):
                    if key in entity:
                        val, meta = resolve_dim(entity[key], positive=key in ('width', 'height', 'radius'))
                        entity[key] = val
                        if meta is not None:
                            exprs[key] = meta
                if exprs:
                    entity['exprs'] = exprs
            sketches[step['id']] = step
        elif kind in ('pad', 'pocket'):
            if step['sketch'] not in sketches or step['sketch'] in used_sketches:
                raise FcohError('Feature needs an earlier, unused sketch')
            if kind == 'pocket' and not solid:
                raise FcohError('Pocket requires an existing solid')
            if kind == 'pad':
                step.setdefault('direction', 'normal')
                val, meta = resolve_dim(step['distance'], positive=True)
                step['distance'] = val
                if meta is not None:
                    step['exprs'] = {'distance': meta}
                sk = sketches[step['sketch']]
                if sk.get('plane') == 'XY' and sk.get('z') is not None:
                    z0 = sk['z']
                    z1 = z0 + (-val if step['direction'] == 'reverse' else val)
                    pad_z_extents[step['id']] = (min(z0, z1), max(z0, z1))
            else:
                step.setdefault('direction', 'reverse')
                if step.get('through_all'):
                    step['through_all'] = True
                    step['distance'] = None
                else:
                    step['through_all'] = False
                    val, meta = resolve_dim(step['distance'], positive=True)
                    step['distance'] = val
                    if meta is not None:
                        step['exprs'] = {'distance': meta}
            used_sketches.add(step['sketch'])
            solid_features.add(step['id'])
            solid = True
        elif kind in ('fillet', 'chamfer'):
            if not solid:
                raise FcohError(f'{kind.title()} requires an existing solid')
            sel = step['edges']
            if 'between' in sel:
                a, b = sel['between']
                if a == b:
                    raise FcohError(f'{step["id"]}: edges.between requires two distinct feature ids')
                for fid in (a, b):
                    if fid not in solid_features:
                        raise FcohError(f'{step["id"]}: edges.between references unknown/future feature {fid!r}')
            if 'of' in sel and sel['of'] not in solid_features:
                raise FcohError(f'{step["id"]}: edges.of references unknown/future feature {sel["of"]!r}')
            key = 'radius' if kind == 'fillet' else 'size'
            val, meta = resolve_dim(step[key], positive=True)
            step[key] = val
            if meta is not None:
                step['exprs'] = {key: meta}
            solid_features.add(step['id'])
        else:
            raise FcohError(f'Unsupported step kind: {kind}')

    # Validate any unreferenced face_plane datums too
    for datum in plan['datums']:
        if datum['kind'] == 'face_plane' and datum['face']['of'] not in solid_features:
            raise FcohError(f'Datum {datum["id"]!r} references unknown feature {datum["face"]["of"]!r}')

    if not solid or used_sketches != set(sketches.keys()):
        raise FcohError('All sketches must be consumed and at least one pad is required')
    return plan


def normalize(data):
    if not isinstance(data, dict):
        raise FcohError('FCOH document root must be a YAML mapping')
    version = data.get('fcoh')
    if version == '0.3':
        return _normalize_03(data)
    if version == '0.4':
        return _normalize_04(data)
    raise FcohError(f'Unsupported FCOH format version: {version!r} (supported: 0.3, 0.4)')


def load(path):
    path = project_path(path)
    if path.stat().st_size > 1_000_000:
        raise FcohError('FCOH file exceeds 1 MB')
    try:
        data = yaml.load(path.read_text(encoding='utf-8-sig'), Loader=UniqueLoader)
        plan = normalize(data)
    except (yaml.YAMLError, RecursionError) as error:
        raise FcohError(f'Invalid YAML: {error}') from error
    return data, plan


def describe(path, strict=False):
    document, plan = load(path)
    lint = lint_plan(document, plan)
    if strict and not lint['ok']:
        summary = '; '.join(f'{e["target"]}: {e["message"]}' for e in lint['errors'][:5])
        raise FcohError(f'Strict lint failed: {summary}')
    return {
        'valid': True,
        'fcoh': plan['fcoh'],
        'id': plan['meta']['id'],
        'units': 'mm',
        'steps': [{'id': s['id'], 'kind': s['kind']} for s in plan['steps']],
        'parameters': len(plan['params']),
        'resolved_params': {k: plan['params'][k]['value'] for k in plan.get('param_order', plan['params'])},
        'lint': lint,
        'supported_cads': ['freecad', 'fusion'],
    }
