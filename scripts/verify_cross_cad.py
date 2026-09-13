"""Run STEP comparison in FreeCAD's own Python runtime."""
import os
import subprocess
from fcoh.core import ROOT
from fcoh.jobs import freecad_python

python = freecad_python()
env = os.environ.copy()
env['PYTHONUTF8'] = '1'
env['PYTHONPATH'] = str(python.parent)
env.pop('PYTHONHOME', None)
result = subprocess.run([str(python), str(ROOT/'adapters/compare_step.py')], cwd=python.parent,
                        env=env, timeout=90, capture_output=True,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
print(result.stdout.decode('utf-8',errors='replace'))
if result.stderr:
    print(result.stderr.decode('utf-8',errors='replace'))
raise SystemExit(result.returncode)
