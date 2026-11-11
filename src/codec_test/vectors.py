"""Test vectors in the three shapes codecs ship them in, and the rule each
shape's own repository judges by.

  pulzar      tests.json next to decoder.js: [{name, fPort?, bytes: hex,
              expect?}]. `expect` null: the LNS must refuse the frame; absent:
              it must accept it; an object: it must accept it and those keys
              of `data` must match, numbers within 1e-6. Matched "subset".
  chirpstack  chirpstack-device-profiles' test_decode_<codec>.json and
              test_encode_<codec>.json: a JavaScript array (hex literals and
              comments allowed) of {name, input, expected}. Passes when
              JSON.stringify of the output equals JSON.stringify(expected),
              key order included. Matched "stringify".
  ttn         TheThingsNetwork/lorawan-devices' <codec>-codec.yaml: examples
              of {description, input, output, normalizedOutput?} under
              uplinkDecoder, downlinkEncoder and downlinkDecoder. Passes when
              the output deep-equals `output` (lodash isEqual). Matched "equal".

A normalized vector is {name, routine, input, match, expect?, lnsInput?}.
Only pulzar vectors are built the way the LNS builds input (`lnsInput`):
recvTime becomes a Date, `variables` defaults to {}. The other two pass
`input` exactly as written, as their own runners do.
"""

import json
import os
import re
from datetime import datetime, timezone

import quickjs
import yaml

from .runtime import (CodecError, MEMORY_LIMIT, VECTOR_TIME_LIMIT_S, _iso, call,
                      js_stringify, uplink_rejection)

_MISSING = object()


def hex_to_bytes(text):
    """Anything not hex is dropped (spaces, colons); an odd length is refused."""
    clean = ''.join(c for c in (text or '') if c in '0123456789abcdefABCDEF')
    if len(clean) % 2:
        raise ValueError(f'hex payload has an odd length: {text}')
    return [int(clean[i:i + 2], 16) for i in range(0, len(clean), 2)]


def from_pulzar(vector):
    """One tests.json vector, normalized. BLE inputs (`parsed`, `transport`)
    pass through when present."""
    payload = vector.get('bytes')
    input = {'bytes': payload if isinstance(payload, list) else hex_to_bytes(payload),
             'fPort': 1 if vector.get('fPort') is None else vector['fPort']}
    for key in ('recvTime', 'variables', 'parsed', 'transport'):
        if key in vector:
            input[key] = vector[key]
    out = {'name': vector.get('name'), 'routine': 'decodeUplink', 'input': input,
           'match': 'subset', 'lnsInput': True}
    if 'expect' in vector:
        if vector['expect'] is not None and not isinstance(vector['expect'], dict):
            raise ValueError('expect must be an object or null')
        out['expect'] = vector['expect']
    return out


def from_chirpstack(tests, routine='decodeUplink'):
    return [{'name': t.get('name'), 'routine': routine, 'input': t.get('input') or {},
             'match': 'stringify', 'expect': t.get('expected')} for t in tests]


def from_ttn(codec):
    """[(fileName, vectors)] for each section of a parsed -codec.yaml."""
    sections = []
    for key, routine in (('uplinkDecoder', 'decodeUplink'), ('downlinkEncoder', 'encodeDownlink'),
                         ('downlinkDecoder', 'decodeDownlink')):
        section = codec.get(key)
        if not section:
            continue
        vectors = []
        for e in section.get('examples') or []:
            vectors.append({'name': e.get('description'), 'routine': routine,
                            'input': e.get('input') or {}, 'match': 'equal', 'expect': e.get('output')})
            if routine == 'decodeUplink' and e.get('normalizedOutput'):
                vectors.append({'name': f"{e.get('description')} (normalized)", 'routine': 'normalizeUplink',
                                'input': e.get('output'), 'match': 'equal', 'expect': e['normalizedOutput']})
        sections.append((section.get('fileName'), vectors))
    return sections


def parse_js_literal(text):
    """A JSON-ish file that is really JavaScript (chirpstack-device-profiles'
    tests have hex numbers and comments), evaluated in the sandbox."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    context = quickjs.Context()
    context.set_time_limit(VECTOR_TIME_LIMIT_S)
    context.set_memory_limit(MEMORY_LIMIT)
    try:
        return json.loads(context.eval(f'JSON.stringify((\n{text}\n))'))
    except quickjs.JSException as e:
        raise ValueError(f'not JSON or a JavaScript literal: {str(e).strip()}') from None


class _YamlLoader(yaml.SafeLoader):
    """YAML as js-yaml reads it, which lorawan-devices uses: only true/false
    are booleans, so an unquoted `on` or `no` stays a string."""


_YamlLoader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != 'tag:yaml.org,2002:bool']
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()}
_YamlLoader.add_implicit_resolver(
    'tag:yaml.org,2002:bool', re.compile(r'^(?:true|True|TRUE|false|False|FALSE)$'), list('tTfF'))


def _timestamp(loader, node):
    """js-yaml makes a timestamp a Date, and lorawan-devices hands the input
    to its runner through JSON.stringify: an ISO string in UTC, milliseconds."""
    value = yaml.SafeLoader.construct_yaml_timestamp(loader, node)
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime('%Y-%m-%dT%H:%M:%S.') + f'{value.microsecond // 1000:03d}Z'


_YamlLoader.add_constructor('tag:yaml.org,2002:timestamp', _timestamp)


def load(path, codec=None):
    """[(codec_path, vectors, lns)] for one test file. `codec` overrides the
    codec path the file implies."""
    directory = os.path.dirname(path)
    with open(path, encoding='utf-8') as f:
        text = f.read()
    if path.endswith(('.yaml', '.yml')):
        doc = yaml.load(text, Loader=_YamlLoader) or {}
        return [(codec or os.path.join(directory, name or ''), vectors, 'tts')
                for name, vectors in from_ttn(doc)]
    tests = parse_js_literal(text)
    if not isinstance(tests, list):
        raise ValueError(f'{path}: expected an array of tests')
    base = os.path.basename(path)
    if any(isinstance(t, dict) and 'expected' in t for t in tests):
        routine = 'encodeDownlink' if base.startswith('test_encode_') else 'decodeUplink'
        implied = base.split('_', 2)[-1][:-len('.json')] + '.js' if base.startswith('test_') else 'codec.js'
        return [(codec or os.path.join(directory, implied), from_chirpstack(tests, routine), 'chirpstack')]
    return [(codec or os.path.join(directory, 'decoder.js'), tests, 'chirpstack')]


# ── Judging ───────────────────────────────────────────────────────────────────

def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _js_truthy(value):
    if value is _MISSING or value is None or value is False:
        return False
    if _number(value):
        return value != 0
    if isinstance(value, str):
        return value != ''
    return True  # objects and arrays, empty or not


def deep_equal(a, b):
    """lodash isEqual over JSON values: 1 equals 1.0, true does not equal 1."""
    if _number(a) and _number(b):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(deep_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(deep_equal(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def _subset_diffs(expected, data):
    """Mismatches on the keys the expectation names, and only those."""
    problems = []
    for key, want in expected.items():
        got = data.get(key, _MISSING) if isinstance(data, dict) else _MISSING
        if _number(want) and _number(got):
            equal = abs(want - got) < 1e-6
        else:
            equal = got is not _MISSING and js_stringify(want) == js_stringify(got)
        if not equal:
            problems.append({'field': key, 'expected': want, 'actual': None if got is _MISSING else got})
    return problems


def judge(vector, out, threw):
    """A result for one normalized vector, given what `call` returned."""
    result = {'name': vector.get('name')}
    decoded = None if out is None else json.loads(out)
    if threw is None:
        result['output'] = decoded
        if isinstance(decoded, dict) and isinstance(decoded.get('errors'), list):
            result['errors'] = decoded['errors']
    else:
        result['crash'] = threw

    if vector['match'] == 'subset':
        expect = vector.get('expect', _MISSING)
        reason = uplink_rejection(decoded, threw, vector['routine'])
        if reason is not None and 'crash' not in result and not result.get('errors'):
            result['crash'] = reason
        if expect is None:
            result['passed'] = reason is not None
            return result
        result['passed'] = reason is None
        if isinstance(expect, dict) and reason is None:
            diffs = _subset_diffs(expect, decoded['data'])
            if diffs:
                result['passed'] = False
                result['diffs'] = diffs
        return result

    if threw is not None:
        result['passed'] = False
        return result
    actual = decoded
    if vector['routine'] == 'normalizeUplink' and isinstance(actual, dict) \
            and _js_truthy(actual.get('data', _MISSING)) and not isinstance(actual['data'], list):
        actual = {**actual, 'data': [actual['data']]}  # what lorawan-devices does before comparing
    expected = vector.get('expect')
    if vector['match'] == 'stringify':
        result['passed'] = out is not None and js_stringify(actual) == js_stringify(expected)
        same = lambda x, y: js_stringify(x) == js_stringify(y)  # noqa: E731
    else:
        result['passed'] = deep_equal(expected, actual)
        same = deep_equal
    if not result['passed']:
        if isinstance(expected, dict) and isinstance(actual, dict):
            keys = list(expected) + [k for k in actual if k not in expected]
            result['diffs'] = [{'field': k, 'expected': expected.get(k), 'actual': actual.get(k)}
                               for k in keys if k not in expected or k not in actual
                               or not same(expected[k], actual[k])]
        else:
            result['diffs'] = [{'field': '', 'expected': expected, 'actual': actual}]
        if not result['diffs']:
            del result['diffs']
            result['note'] = 'same values in a different key order; ChirpStack compares JSON.stringify'
    return result


def run_vector(source, vector, now=None, lns='chirpstack'):
    """One vector (normalized, or a pulzar tests.json entry) in a fresh sandbox."""
    try:
        if 'routine' not in vector:
            vector = from_pulzar(vector)
        input = vector['input']
        if vector.get('lnsInput'):
            input = {**input}
            input.setdefault('recvTime', _iso(now))
            if lns == 'chirpstack':
                input.setdefault('variables', {})
        try:
            out, threw = call(source, vector['routine'], input, lns, bool(vector.get('lnsInput')))
        except CodecError as e:
            # The codec did not run at all. Never a pass, even for a vector
            # expecting an error: a codec that does not compile refuses everything.
            return {'name': vector.get('name'), 'passed': False, 'crash': str(e)}
        return judge(vector, out, threw)
    except Exception as e:  # noqa: BLE001 -- a malformed vector is a failed one
        return {'name': vector.get('name'), 'passed': False, 'crash': str(e)}


def run_vectors(source, vectors, now=None, lns='chirpstack'):
    return [run_vector(source, v, now, lns) for v in vectors or []]


def failure_reason(result):
    """One line saying why a vector failed."""
    if result.get('crash'):
        return result['crash']
    if result.get('diffs'):
        return ' · '.join(f"{d['field'] or 'output'}: expected {js_stringify(d['expected'])}, "
                          f"got {js_stringify(d['actual'])}" for d in result['diffs'])
    if result.get('note'):
        return result['note']
    if result.get('errors'):
        return '; '.join(str(e) for e in result['errors'])
    if 'output' in result:
        return 'decoded; expected an error'
    return 'failed'

