import argparse
import json
import sys
import time
from .core import describe
from .jobs import build, capabilities, status


def main():
    parser = argparse.ArgumentParser(description='FCOH CAD prototype')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('capabilities')
    validate = sub.add_parser('validate')
    validate.add_argument('file')
    create = sub.add_parser('build')
    create.add_argument('file')
    create.add_argument('--cad', choices=['freecad', 'fusion'], default='freecad')
    create.add_argument('--wait', type=int, default=0, help='Seconds to wait for a Fusion result')
    stat = sub.add_parser('status')
    stat.add_argument('job_id')
    args = parser.parse_args()
    try:
        if args.command == 'capabilities':
            result = capabilities()
        elif args.command == 'validate':
            result = describe(args.file)
        elif args.command == 'status':
            result = status(args.job_id)
        else:
            result = build(args.file, args.cad)
            deadline = time.time() + args.wait
            while args.wait and 'ok' not in result and time.time() < deadline:
                time.sleep(1)
                result = status(result['job_id'])
            if args.wait and 'ok' not in result:
                result['wait_timed_out'] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get('ok') is False or result.get('wait_timed_out') else 0
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
