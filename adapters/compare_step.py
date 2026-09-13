"""Compare actual exported CAD solids using OpenCASCADE booleans."""
import json
from pathlib import Path
import sys
import FreeCAD
import Part

root = Path(__file__).resolve().parents[1]
freecad_result = json.loads((root/'evidence/mcp-freecad.json').read_text(encoding='utf-8'))['result']
fusion_result = json.loads((root/'evidence/mcp-fusion.json').read_text(encoding='utf-8'))['result']
shapes = []
for result in [freecad_result, fusion_result]:
    shape = Part.Shape()
    shape.read(result['artifacts']['part.step']['path'])
    assert shape.isValid() and len(shape.Solids) == 1
    shapes.append(shape)
a, b = shapes
missing = a.cut(b).Volume
extra = b.cut(a).Volume
result = {'freecad_job':freecad_result['job_id'], 'fusion_job':fusion_result['job_id'],
          'freecad_volume_mm3':a.Volume, 'fusion_volume_mm3':b.Volume,
          'freecad_minus_fusion_mm3':missing, 'fusion_minus_freecad_mm3':extra,
          'tolerance_mm3':0.01, 'passed':abs(missing)<0.01 and abs(extra)<0.01}
(root/'evidence/cross-cad.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
print(json.dumps(result, indent=2))
raise SystemExit(0 if result['passed'] else 1)
