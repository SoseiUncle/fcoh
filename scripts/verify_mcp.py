"""End-to-end test using the registered-style stdio subprocess transport."""
import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import sys
import time

from mcp import Client, StdioServerParameters
from fcoh.core import ROOT


def tool_value(response):
    result = response.structured_content
    if result is None:
        text = '\n'.join(c.text for c in response.content if hasattr(c, 'text'))
        result = json.loads(text)
    if 'result' in result and isinstance(result['result'], dict):
        result = result['result']
    return result


async def main(cad):
    endpoint = StdioServerParameters(command=sys.executable, args=['-m', 'fcoh.server'], cwd=str(ROOT))
    evidence = {'transport':'stdio', 'cad':cad, 'timestamp':time.time()}
    source = ROOT / 'examples' / 'mounting-bracket.fcoh.yaml'
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    async with Client(endpoint, read_timeout_seconds=240) as client:
        listed = await client.list_tools()
        evidence['tools'] = [t.name for t in listed.tools]
        assert set(evidence['tools']) == {'fcoh_capabilities','fcoh_validate','fcoh_build','fcoh_status'}
        valid = await client.call_tool('fcoh_validate', {'file':'examples/mounting-bracket.fcoh.yaml'})
        assert not valid.is_error, valid
        invalid = await client.call_tool('fcoh_validate', {'file':'../outside.yaml'})
        assert invalid.is_error, 'Path escape was not rejected'
        evidence['path_escape_rejected'] = True
        created = await client.call_tool('fcoh_build', {'file':'examples/mounting-bracket.fcoh.yaml','cad':cad})
        assert not created.is_error, created
        result = tool_value(created)
        deadline = time.time()+180
        while 'ok' not in result and time.time()<deadline:
            await asyncio.sleep(1)
            polled = await client.call_tool('fcoh_status', {'job_id': result['job_id']})
            result = tool_value(polled)
        evidence['result'] = result
        assert result.get('ok'), result
        assert result['solid_count'] == 1 and result['valid_shape']
        assert all(abs(a-b)<1e-4 for a,b in zip(result['bounds_mm'],[80,40,30]))
        expected = 80*40*6 + 80*6*24 - 4*math.pi*3**2*6
        assert abs(result['volume_mm3']-expected)<0.01, result['volume_mm3']
        assert [f['id'] for f in result['features']] == ['base_pad','wall_pad','mounting_holes']
        for artifact in result['artifacts'].values():
            path = Path(artifact['path'])
            assert path.is_file() and path.stat().st_size > 100
            assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact['sha256']
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        evidence['source_unchanged'] = True
        evidence['analytic_volume_mm3'] = expected
        evidence['passed'] = True
    directory = ROOT / 'evidence'
    directory.mkdir(exist_ok=True)
    (directory / f'mcp-{cad}.json').write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(evidence, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--cad',choices=['freecad','fusion'],default='freecad')
    asyncio.run(main(parser.parse_args().cad))
