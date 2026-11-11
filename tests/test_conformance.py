"""The shared fixtures, and what only Python does (YAML, the CLI, Codec).
Runs under pytest or as `python tests/test_conformance.py`."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from codec_test import (Codec, CodecError, encode_downlink, from_pulzar, judge, js_stringify, load,
                                run_vector, run_vectors, with_decode_uplink)
from codec_test.vectors import _YamlLoader

FIXTURES = Path(__file__).resolve().parent.parent / 'fixtures'
CONFORMANCE = json.loads((FIXTURES / 'conformance.json').read_text())


def test_judge():
    for case in CONFORMANCE['judge']:
        vector = case['vector'] if 'routine' in case['vector'] else from_pulzar(case['vector'])
        out = js_stringify(case['output']) if 'output' in case else None
        result = judge(vector, out, case.get('threw'))
        assert result['passed'] == case['passed'], (case['name'], result)
        assert [d['field'] for d in result.get('diffs', [])] == case['diffs'], (case['name'], result)


def test_run():
    for case in CONFORMANCE['run']:
        result = run_vector(case['codec'], case['vector'], lns=case['lns'])
        assert result['passed'] == case['passed'], (case['name'], result)
        assert ('crash' in result) == case['crash'], (case['name'], result)


def test_wrap():
    for case in CONFORMANCE['wrap']:
        assert with_decode_uplink(case['source']) == case['wrapped'], case['name']


def test_formats():
    files = ['tests.json', 'test_decode_decoder.json', 'test_encode_decoder.json', 'decoder-codec.yaml']
    total = 0
    for name in files:
        for codec, vectors, lns in load(str(FIXTURES / 'formats' / name)):
            results = run_vectors(Path(codec).read_text(), vectors, lns=lns)
            assert all(r['passed'] for r in results), (name, results)
            total += len(results)
    assert total == 12, total


def test_cli():
    import tempfile
    formats = FIXTURES / 'formats'
    env = {**os.environ, 'PYTHONPATH': str(FIXTURES.parent / 'src')}
    run = lambda *a: subprocess.run([sys.executable, '-m', 'codec_test.cli', *map(str, a)],  # noqa: E731
                                    capture_output=True, text=True, env=env)
    ok = run(formats / 'tests.json', formats / 'test_decode_decoder.json', formats / 'decoder-codec.yaml')
    assert ok.returncode == 0 and '11 of 11 passed' in ok.stdout, ok.stdout + ok.stderr
    report = json.loads(run('--json', formats / 'test_encode_decoder.json').stdout)
    assert report[0]['lns'] == 'chirpstack' and report[0]['results'][0]['passed'], report
    with tempfile.TemporaryDirectory() as tmp:
        wrong = Path(tmp) / 'wrong.js'
        wrong.write_text('function decodeUplink(i) { return { data: { open: false } }; }')
        failed = run(wrong, formats / 'tests.json')
        assert failed.returncode == 1 and '✗ open — open: expected true, got false' in failed.stdout, failed.stdout
    assert run(formats / 'nope.json').returncode == 2


def test_yaml_reads_as_js_yaml():
    doc = yaml.load('a: on\nb: no\nc: true\nd: 0x2A\ne: 2026-10-11T08:30:00Z\nf: 2026-10-11\n', Loader=_YamlLoader)
    assert doc == {'a': 'on', 'b': 'no', 'c': True, 'd': 42, 'e': '2026-10-11T08:30:00.000Z',
                   'f': '2026-10-11T00:00:00.000Z'}, doc


def test_codec():
    now = datetime(2026, 10, 11, 8, 30, tzinfo=timezone.utc)
    echo = Codec('function decodeUplink(i) { return { data: { n: i.bytes.length, port: i.fPort, '
                 'at: i.recvTime.toISOString(), parsed: i.parsed, via: i.transport } }; }')
    assert echo.decode(b'\x01\x02', 1, now) == {'n': 2, 'port': 1, 'at': '2026-10-11T08:30:00.000Z'}
    assert echo.decode(None, 0, now, parsed={'alarm': 17}, transport='ble')['parsed'] == {'alarm': 17}
    for source, message in (('function decodeUplink(i) { while (true) {} }', 'interrupted'),
                            ('function decodeUplink(i) { return { errors: ["not mine"] }; }', 'not mine'),
                            ('function decodeUplink(i) { return {}; }', "did not return 'data'")):
        try:
            Codec(source).decode(b'', 1, now)
            raise AssertionError(source)
        except CodecError as e:
            assert message in str(e), (source, e)
    try:
        Codec('function decodeUplink(i) {')
        raise AssertionError('a broken codec compiled')
    except CodecError as e:
        assert 'does not compile' in str(e)

    encoder = 'function encodeDownlink(i) { return { bytes: [i.data.on ? 1 : 0, i.fPort === undefined ? 0 : i.fPort] }; }'
    assert encode_downlink(encoder, {'on': True}, 5) == (b'\x01\x00', 5)  # ChirpStack gives no fPort
    assert encode_downlink(encoder, {'on': True}, 5, lns='tts') == (b'\x01\x05', 5)
    assert encode_downlink('function encodeDownlink(i) { return { fPort: 9, bytes: [] }; }', {}, 5) == (b'', 9)
    for bad in ('[256]', '[1.5]', '[true]', '"01"'):
        try:
            encode_downlink(f'function encodeDownlink(i) {{ return {{ bytes: {bad} }}; }}', {}, 1)
            raise AssertionError(bad)
        except CodecError:
            pass

    legacy = 'function Decoder(bytes, port) { return { n: bytes.length }; }\nfunction Encoder(o, port) { return [o.n]; }'
    wrapped = with_decode_uplink(legacy)
    assert Codec(wrapped).decode(b'\x01', 1, now) == {'n': 1}
    assert encode_downlink(wrapped, {'n': 7}, 1) == (b'\x07', 1)
    assert with_decode_uplink(wrapped) == wrapped


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok', name)
