"""End-to-end verification for FCOH v0.4 features on FreeCAD (including native Spreadsheet recompute)."""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from mcp import Client, StdioServerParameters
from fcoh.core import ROOT
from fcoh.jobs import freecad_python


def analytic_bracket_v04_volume(length, width, thickness, wall_height, hole_r, root_r, top_c, hole_c):
    base = length * width * thickness
    wall = length * thickness * wall_height
    holes = 4 * math.pi * (hole_r ** 2) * thickness
    fillet_add = (1.0 - math.pi / 4.0) * (root_r ** 2) * length
    top_chamfer_cut = 2.0 * (0.5 * (top_c ** 2)) * length
    # Frustum cone minus inner cylinder for 4 circular top chamfers
    r_outer = hole_r + hole_c
    one_hole_chamfer = math.pi * hole_c * ((r_outer ** 2 + r_outer * hole_r + hole_r ** 2) / 3.0 - hole_r ** 2)
    return base + wall - holes + fillet_add - top_chamfer_cut - 4.0 * one_hole_chamfer


def tool_value(response):
    result = response.structured_content
    if result is None:
        text = '\n'.join(c.text for c in response.content if hasattr(c, 'text'))
        result = json.loads(text)
    if 'result' in result and isinstance(result['result'], dict):
        result = result['result']
    return result


async def run_mcp_v04():
    endpoint = StdioServerParameters(command=sys.executable, args=['-m', 'fcoh.server'], cwd=str(ROOT))
    source = ROOT / 'examples' / 'mounting-bracket-v04.fcoh.yaml'
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    async with Client(endpoint, read_timeout_seconds=240) as client:
        caps = tool_value(await client.call_tool('fcoh_capabilities', {}))
        assert '0.4' in caps.get('versions', []), caps
        val = tool_value(await client.call_tool('fcoh_validate', {
            'file': 'examples/mounting-bracket-v04.fcoh.yaml', 'strict': True}))
        assert val['valid'] and val['lint']['ok'] and val['lint']['warning_count'] == 0, val
        built = tool_value(await client.call_tool('fcoh_build', {
            'file': 'examples/mounting-bracket-v04.fcoh.yaml', 'cad': 'freecad'}))
        assert built.get('ok'), built
        expected = analytic_bracket_v04_volume(80, 40, 6, 24, 3, 3, 1.0, 0.6)
        assert abs(built['volume_mm3'] - expected) < 0.05, (built['volume_mm3'], expected)
        assert all(abs(a - b) < 1e-4 for a, b in zip(built['bounds_mm'], [80, 40, 30])), built['bounds_mm']
        assert built['solid_count'] == 1 and built['valid_shape']
        assert built['fcoh_metadata_and_order_preserved'] and built['reopened_native_and_step']
        assert 'preview.svg' in built['artifacts'], built['artifacts']
        feat_map = {f['id']: f for f in built['features']}
        assert feat_map['root_fillet_feat']['matched_edges'] == 1, feat_map
        assert feat_map['wall_top_chamfer']['matched_edges'] == 2, feat_map
        assert feat_map['hole_lead_chamfer']['matched_edges'] == 4, feat_map
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        return built, expected


def verify_native_parametric_recompute(fcstd_path):
    """Open the generated FCStd directly in FreeCAD, edit Spreadsheet params, and check full geometry update."""
    fc_py = freecad_python()
    env = os.environ.copy()
    env['PYTHONUTF8'] = '1'
    env['PYTHONPATH'] = str(fc_py.parent) + os.pathsep + str(fc_py.parent.parent / 'Mod')
    env.pop('PYTHONHOME', None)
    probe = r"""
import json, sys, FreeCAD as App
doc = App.openDocument(sys.argv[1])
sheet = doc.getObject('Params')
# Change length 80 -> 110 and width 40 -> 55 inside native FreeCAD without re-running FCOH!
for row in range(2, 30):
    try:
        name = sheet.get(f'A{row}')
    except Exception:
        break
    if name == 'length':
        sheet.set(f'B{row}', '110 mm')
    elif name == 'width':
        sheet.set(f'B{row}', '55 mm')
doc.recompute()
tip = doc.getObject('Body').Tip
box = tip.Shape.BoundBox
print(json.dumps({
    'wall_y': str(sheet.get('wall_y')),
    'right_x': str(sheet.get('right_x')),
    'rear_y': str(sheet.get('rear_y')),
    'bounds_mm': [box.XLength, box.YLength, box.ZLength],
    'volume_mm3': tip.Shape.Volume,
    'valid': tip.Shape.isValid(),
    'solids': len(tip.Shape.Solids),
}))
App.closeDocument(doc.Name)
"""
    proc = subprocess.run([str(fc_py), '-c', probe, str(fcstd_path)],
                          cwd=fc_py.parent, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + '\n' + proc.stderr
    lines = [l for l in proc.stdout.splitlines() if l.strip().startswith('{')]
    assert lines, proc.stdout + '\n' + proc.stderr
    data = json.loads(lines[-1])
    expected = analytic_bracket_v04_volume(110, 55, 6, 24, 3, 3, 1.0, 0.6)
    assert data['valid'] and data['solids'] == 1, data
    assert all(abs(a - b) < 1e-4 for a, b in zip(data['bounds_mm'], [110, 55, 30])), data
    assert abs(data['volume_mm3'] - expected) < 0.05, (data['volume_mm3'], expected)
    data['expected_volume_mm3'] = expected
    return data


def main():
    built, expected = asyncio.run(run_mcp_v04())
    recompute = verify_native_parametric_recompute(built['artifacts']['part.FCStd']['path'])
    evidence = {
        'passed': True,
        'timestamp': time.time(),
        'v04_build': built,
        'analytic_volume_mm3': expected,
        'native_spreadsheet_recompute': recompute,
    }
    ev_dir = ROOT / 'evidence'
    ev_dir.mkdir(exist_ok=True)
    (ev_dir / 'v04-freecad.json').write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(evidence, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
