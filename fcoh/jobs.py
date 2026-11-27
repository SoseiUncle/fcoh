"""Local jobs, isolated output directories, no listening sockets."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from .core import ROOT, FcohError, load


def freecad_python():
    candidates = sorted((ROOT / 'vendor').glob('FreeCAD*/bin/python.exe'))
    if not candidates:
        raise FcohError('FreeCAD portable Python is missing: run scripts/setup.ps1')
    return candidates[-1]


def status(job_id):
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise FcohError('Invalid job id')
    directory = ROOT / 'outputs' / job_id
    result = directory / 'result.json'
    if result.exists():
        return {'job_id': job_id, **json.loads(result.read_text(encoding='utf-8'))}
    if not (directory / 'request.json').exists():
        raise FcohError('Unknown job id')
    request = json.loads((directory / 'request.json').read_text(encoding='utf-8'))
    state = 'expired' if request['expires_at'] < time.time() else 'queued_or_running'
    return {'job_id': job_id, 'state': state, 'cad': request['cad'], 'output_dir': str(directory)}


def capabilities():
    heartbeat = ROOT / '.runtime' / 'fusion-heartbeat.json'
    fusion = None
    if heartbeat.exists():
        try:
            fusion = json.loads(heartbeat.read_text(encoding='utf-8'))
            fusion['recent'] = time.time() - fusion.get('timestamp', 0) < 15
        except (ValueError, OSError):
            fusion = {'recent': False}
    if fusion:
        state_file = ROOT / '.runtime' / 'fusion-state.json'
        if state_file.exists():
            try:
                fusion['last_main_thread_state'] = json.loads(state_file.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                pass
    return {'format': 'FCOH 0.3 / 0.4 experimental subset', 'versions': ['0.3', '0.4'], 'units': ['mm'],
            'datums': ['plane:XY', 'plane:XZ', 'plane:YZ', 'face_plane'],
            'steps': ['sketch:rectangle', 'sketch:circle', 'pad', 'pocket', 'pocket:through_all',
                      'fillet:semantic_edges', 'chamfer:semantic_edges'],
            'expressions': ['+', '-', '*', '/', 'min', 'max', 'abs'],
            'freecad_installed': bool(list((ROOT / 'vendor').glob('FreeCAD*/bin/python.exe'))),
            'fusion': fusion, 'transport': 'stdio', 'root': str(ROOT),
            'limits': ['new single solid parts only', 'no STEP/STL import yet',
                       'no freeform surfaces/lofts or assemblies yet', 'geometry regenerated from YAML']}


def build(path, cad='freecad'):
    if cad not in ('freecad', 'fusion'):
        raise FcohError('cad must be freecad or fusion')
    document, plan = load(path)
    python = freecad_python() if cad == 'freecad' else None
    if cad == 'fusion' and not (capabilities()['fusion'] or {}).get('recent'):
        raise FcohError('Fusion bridge is not running. Start Fusion and run the FcohBridge add-in.')
    job_id = uuid.uuid4().hex
    directory = ROOT / 'outputs' / job_id
    directory.mkdir(parents=True)
    request = {'job_id': job_id, 'cad': cad, 'created_at': time.time(), 'expires_at': time.time()+600,
               'document': document, 'plan': plan}
    (directory / 'request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding='utf-8')
    (directory / 'source.fcoh.yaml').write_bytes((ROOT / path).resolve().read_bytes())
    if cad == 'fusion':
        queue = ROOT / '.runtime' / 'queue'
        queue.mkdir(parents=True, exist_ok=True)
        pending = queue / (job_id + '.tmp')
        pending.write_text(json.dumps({'job_id': job_id}), encoding='utf-8')
        pending.replace(queue / (job_id + '.json'))
        return status(job_id)
    env = os.environ.copy()
    env['FCOH_JOB_DIR'] = str(directory)
    env['PYTHONUTF8'] = '1'
    env['PYTHONPATH'] = str(python.parent) + os.pathsep + str(python.parent.parent / 'Mod')
    env.pop('PYTHONHOME', None)
    try:
        completed = subprocess.run([str(python), str(ROOT / 'adapters' / 'freecad_runner.py')],
                                   cwd=python.parent, env=env, capture_output=True, timeout=180,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        (directory / 'freecad.log').write_bytes(completed.stdout + b'\n' + completed.stderr)
        if not (directory / 'result.json').exists():
            (directory / 'result.json').write_text(json.dumps({'ok': False, 'cad': cad,
                'error': f'FreeCAD exited {completed.returncode}; see freecad.log'}), encoding='utf-8')
    except subprocess.TimeoutExpired:
        (directory / 'result.json').write_text(json.dumps({'ok': False, 'cad': cad, 'error': 'FreeCAD timed out after 180 seconds'}), encoding='utf-8')
    return status(job_id)
