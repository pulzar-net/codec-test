"""codec-test [--lns chirpstack|tts] [--json] [CODEC.js] TESTS...

TESTS are tests.json (Pulzar), chirpstack-device-profiles test_decode_*.json
/ test_encode_*.json, or lorawan-devices *-codec.yaml, in any mix. Without
CODEC.js each file names its own: decoder.js beside tests.json, <codec>.js
beside test_decode_<codec>.json, the yaml's fileName.
"""

import argparse
import json
import os
import sys
import time

from .runtime import LNS, with_decode_uplink
from .vectors import failure_reason, load, run_vectors


def main(argv=None):
    parser = argparse.ArgumentParser(prog='codec-test', description=__doc__.split('\n\n', 1)[1],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('files', nargs='+', metavar='FILE')
    parser.add_argument('--lns', choices=LNS,
                        help='network server to imitate (default: tts for .yaml, chirpstack otherwise)')
    parser.add_argument('--json', action='store_true', help='print results as JSON')
    args = parser.parse_args(argv)
    # Dates as the network servers' and the repositories' CI see them.
    os.environ['TZ'] = 'UTC'
    time.tzset()

    codecs = [f for f in args.files if f.endswith(('.js', '.mjs'))]
    tests = [f for f in args.files if f not in codecs]
    if len(codecs) > 1 or not tests:
        parser.error('give at most one codec .js and at least one test file')

    report, failed = [], 0
    for path in tests:
        try:
            runs = load(path, codecs[0] if codecs else None)
        except (OSError, ValueError) as e:
            print(f'{path}: {e}', file=sys.stderr)
            return 2
        for codec_path, vectors, lns in runs:
            if not vectors:
                continue
            lns = args.lns or lns
            try:
                with open(codec_path, encoding='utf-8') as f:
                    source = f.read()
            except OSError as e:
                print(f'{path}: {e}', file=sys.stderr)
                return 2
            if lns == 'chirpstack':
                wrapped = with_decode_uplink(source)
                if wrapped != source and not args.json:
                    print(f'note: {codec_path} has only Decoder()/Encoder(); ChirpStack calls neither, '
                          'so it is run with decodeUplink/encodeDownlink wrappers added')
                source = wrapped
            results = run_vectors(source, vectors, lns=lns)
            failed += sum(not r['passed'] for r in results)
            report.append({'tests': path, 'codec': codec_path, 'lns': lns, 'results': results})
            if not args.json:
                print(f'{path} ({codec_path}, {lns})')
                for r in results:
                    mark = '✓' if r['passed'] else '✗'
                    print(f"  {mark} {r['name']}" + ('' if r['passed'] else f' — {failure_reason(r)}'))

    total = sum(len(r['results']) for r in report)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f'{total - failed} of {total} passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
