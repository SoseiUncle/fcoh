"""Safe YAML loading and a CAD-independent numeric execution plan."""
import copy
import json
import math
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / 'schemas' / 'fcoh-0.3.schema.json'


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


def normalize(data):
    schema = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(data), key=lambda e: str(e.path))
    if errors:
        raise FcohError('; '.join(f'{"/".join(map(str,e.path)) or "/"}: {e.message}' for e in errors[:5]))
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


def describe(path):
    _, plan = load(path)
    return {'valid': True, 'id': plan['meta']['id'], 'units': 'mm',
            'steps': [{'id': s['id'], 'kind': s['kind']} for s in plan['steps']],
            'parameters': len(plan['params']), 'supported_cads': ['freecad', 'fusion']}
