"""Running a LoRaWAN payload codec the way the network server does, and
judging it against test vectors the way each codec repository does.

The sandbox is QuickJS: no filesystem, no network, no imports, a time limit
that a loop cannot catch, and a memory limit. Every call gets a fresh one,
as ChirpStack gives every uplink a fresh runtime, so a codec cannot carry
state from one frame to the next here when it cannot there either.

`lns` names the network server being imitated:

  chirpstack  The codec is evaluated as an ES module (so in strict mode),
              decodeUplink gets {bytes, fPort, recvTime, variables} and
              encodeDownlink gets {data, variables}. A codec with only the
              older Decoder(bytes, port) is not called: ChirpStack does not.
  tts         The codec is a plain script (sloppy mode), decodeUplink gets
              {bytes, fPort, recvTime} and encodeDownlink {data, fPort}; a
              codec with only Decoder()/Encoder() is called through them, as
              The Things Stack does.

In both, recvTime is a Date. ChirpStack also gives every codec `Buffer`
(feross/buffer), and so does this under `chirpstack`. See README.md for what still differs.
"""

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import quickjs

LNS = ('chirpstack', 'tts')
ROUTINES = ('decodeUplink', 'encodeDownlink', 'decodeDownlink', 'normalizeUplink')

# ChirpStack's default codec.js.max_execution_time, for live traffic.
UPLINK_TIME_LIMIT_S = 0.1
# What TTN's runscript and the browser runner give one test vector.
VECTOR_TIME_LIMIT_S = 1.0
MEMORY_LIMIT = 16 * 1024 * 1024


class CodecError(Exception):
    """The codec could not run, or the network server would refuse what it returned."""


_BUFFER = Path(__file__).with_name('chirpstack_buffer.js')


def _sandbox(source, lns, time_limit):
    """A fresh QuickJS context with the limits, and ChirpStack's Buffer when
    the codec could be using it (parsing it costs a few milliseconds)."""
    context = quickjs.Context()
    context.set_time_limit(time_limit)
    context.set_memory_limit(MEMORY_LIMIT)
    if lns == 'chirpstack' and 'Buffer' in source:
        context.eval(_BUFFER.read_text(encoding='utf-8'))
    return context


def _call(routine, lns):
    if routine not in ROUTINES:
        raise ValueError(f'unknown routine {routine!r}')
    if lns not in LNS:
        raise ValueError(f'unknown lns {lns!r}, expected one of {LNS}')
    # The Things Stack's own fallback for The Things Network V2 functions.
    if lns == 'tts' and routine == 'decodeUplink':
        return ("typeof decodeUplink === 'function' ? decodeUplink(input)"
                " : { data: Decoder(input.bytes, input.fPort) }")
    if lns == 'tts' and routine == 'encodeDownlink':
        return ("typeof encodeDownlink === 'function' ? encodeDownlink(input)"
                " : { bytes: Encoder(input.data, input.fPort), fPort: input.fPort }")
    return f'{routine}(input)'


def call(source, routine, input, lns='chirpstack', lns_input=False, time_limit=VECTOR_TIME_LIMIT_S):
    """Run one routine over `input` in a fresh sandbox.

    Returns (output, threw): `output` is JSON.stringify of what the routine
    returned (None for undefined), `threw` the message of what it threw.
    `lns_input` turns `input.recvTime` (an ISO string) into a Date, as the
    network server hands it. Raises CodecError when the program cannot run
    at all: a syntax error, a top-level throw, the time limit.
    """
    prepare = ('if (input.recvTime !== undefined) input.recvTime = new Date(input.recvTime);'
               if lns_input else '')
    program = (
        f'{source}\n;globalThis.__lct = (function () {{\n'
        f'  var input = {json.dumps(input, ensure_ascii=False)}; {prepare}\n'
        f'  try {{ return {{ out: JSON.stringify({_call(routine, lns)}) }}; }}\n'
        '  catch (e) { return { threw: String((e && e.message) || e) }; }\n'
        '})();\n'
    )
    context = _sandbox(source, lns, time_limit)
    try:
        (context.module if lns == 'chirpstack' else context.eval)(program)
        result = json.loads(context.eval('JSON.stringify(globalThis.__lct)'))
    except quickjs.JSException as e:
        raise CodecError(str(e).strip()) from None
    return result.get('out'), result.get('threw')


def uplink_rejection(decoded, threw, routine='decodeUplink'):
    """Why the network server would refuse a decoder's output, or None.

    ChirpStack refuses a throw, a non-empty `errors` array and an output
    without a `data` object; The Things Stack refuses the same.
    """
    if threw is not None:
        return threw
    if not isinstance(decoded, dict):
        return f'{routine} returned {js_stringify(decoded)}, not an object'
    errors = decoded.get('errors')
    if isinstance(errors, list) and errors:
        return '; '.join(e if isinstance(e, str) else js_stringify(e) for e in errors)
    if not isinstance(decoded.get('data'), dict):
        return f"{routine} did not return 'data'"
    return None


def _iso(when):
    """Date.prototype.toISOString: UTC, milliseconds."""
    when = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return when.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


class Codec:
    """A codec as the network server runs it on live traffic."""

    def __init__(self, source, lns='chirpstack', time_limit=UPLINK_TIME_LIMIT_S):
        self.source, self.lns, self.time_limit = source, lns, time_limit
        _call('decodeUplink', lns)  # validates lns
        context = _sandbox(source, lns, time_limit)
        try:
            (context.module if lns == 'chirpstack' else context.eval)(source)
        except quickjs.JSException as e:
            raise CodecError(f'the codec does not compile: {str(e).strip()}') from None

    def decode(self, payload, f_port, recv_time=None, variables=None, **extra):
        """The decoded `data`. Raises CodecError when the LNS would have nothing.

        `extra` goes into the input as is: a BLE transport's `parsed` and
        `transport`, which no LoRaWAN server sends.
        """
        input = {'bytes': list(payload or b''), 'fPort': f_port, 'recvTime': _iso(recv_time), **extra}
        if self.lns == 'chirpstack':
            input['variables'] = variables or {}
        out, threw = call(self.source, 'decodeUplink', input, self.lns, True, self.time_limit)
        decoded = None if out is None else json.loads(out)
        reason = uplink_rejection(decoded, threw)
        if reason:
            raise CodecError(reason)
        return decoded['data']

    def encode(self, data, f_port=None, variables=None):
        """(bytes, fPort) for a downlink. ChirpStack does not tell
        encodeDownlink the port; the codec may name one, else `f_port` stands."""
        if self.lns == 'chirpstack':
            input = {'data': data, 'variables': variables or {}}
        else:
            input = {'data': data} if f_port is None else {'data': data, 'fPort': f_port}
        out, threw = call(self.source, 'encodeDownlink', input, self.lns, False, self.time_limit)
        if threw is not None:
            raise CodecError(threw)
        result = None if out is None else json.loads(out)
        if not isinstance(result, dict):
            raise CodecError('encodeDownlink returned no object')
        if isinstance(result.get('errors'), list) and result['errors']:
            raise CodecError('; '.join(str(e) for e in result['errors']))
        frame = result.get('bytes')
        # Stricter than ChirpStack, which saturates 256 to 255 and -1 to 0.
        if not isinstance(frame, list) or not all(
                isinstance(b, int) and not isinstance(b, bool) and 0 <= b <= 255 for b in frame):
            raise CodecError(f'encodeDownlink returned no bytes: {js_stringify(frame) if frame is not None else "none"}')
        port = result.get('fPort', f_port)
        return bytes(frame), port


def encode_downlink(source, data, f_port=None, lns='chirpstack', variables=None):
    return Codec(source, lns).encode(data, f_port, variables)


# ── The older The Things Network form ─────────────────────────────────────────

_DECODE_UPLINK = re.compile(r'\bdecodeUplink\s*(=|\()')
_ENCODE_DOWNLINK = re.compile(r'\bencodeDownlink\s*(=|\()')
_DECODER = re.compile(r'\bfunction\s+Decoder\s*\(|\bDecoder\s*=\s*function\b')
_ENCODER = re.compile(r'\bfunction\s+Encoder\s*\(|\bEncoder\s*=\s*function\b')


def with_decode_uplink(source):
    """The codec with decodeUplink/encodeDownlink calling its Decoder/Encoder
    when it has only those, so ChirpStack (which calls only the new names)
    can run it. Unchanged otherwise.

    One step past The Things Stack's own fallback: a Decoder that returns
    {errors} is taken to be refusing the frame, where TTS would store the
    errors as data."""
    added = []
    if source and _DECODER.search(source) and not _DECODE_UPLINK.search(source):
        added.append('function decodeUplink(input) {\n'
                     '  var decoded = Decoder(input.bytes, input.fPort);\n'
                     '  return decoded && decoded.errors ? { errors: decoded.errors } : { data: decoded };\n}')
    if source and _ENCODER.search(source) and not _ENCODE_DOWNLINK.search(source):
        added.append('function encodeDownlink(input) {\n'
                     '  return { bytes: Encoder(input.data, input.fPort), fPort: input.fPort };\n}')
    if not added:
        return source
    return (source.rstrip() + '\n\n// Added by codec-test: this codec is in The Things Network\'s older\n'
            '// Decoder(bytes, port) / Encoder(object, port) form.\n' + '\n'.join(added) + '\n')


# ── JavaScript's view of a JSON value ─────────────────────────────────────────

def _js_number(value):
    """Number.prototype.toString for a finite double (ECMA-262 Number::toString).

    Python's repr gives the same shortest round-tripping digits; only where
    the decimal point and the exponent go differs.
    """
    if value == 0:
        return '0'
    sign = '-' if value < 0 else ''
    digits_tuple, exponent = Decimal(repr(abs(float(value)))).as_tuple()[1:]
    digits = ''.join(str(d) for d in digits_tuple).lstrip('0') or '0'
    trailing = len(digits) - len(digits.rstrip('0'))
    digits = digits.rstrip('0') or '0'
    exponent += trailing
    k = len(digits)
    n = k + exponent
    if k <= n <= 21:
        return sign + digits + '0' * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + '.' + digits[n:]
    if -6 < n <= 0:
        return sign + '0.' + '0' * (-n) + digits
    e = n - 1
    mantissa = digits if k == 1 else digits[0] + '.' + digits[1:]
    return f"{sign}{mantissa}e{'+' if e > 0 else '-'}{abs(e)}"


def js_stringify(value):
    """JSON.stringify(value), byte for byte, for anything JSON.parse can return.

    Not json.dumps: that writes 1.0 where JavaScript writes 1, and escapes
    non-ASCII where JavaScript does not.
    """
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, int):
        return str(value) if abs(value) < 10 ** 21 else _js_number(float(value))
    if isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            return 'null'
        return _js_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return '[' + ','.join(js_stringify(v) for v in value) + ']'
    if isinstance(value, dict):
        return '{' + ','.join(
            f'{json.dumps(str(k), ensure_ascii=False)}:{js_stringify(v)}' for k, v in value.items()
        ) + '}'
    raise TypeError(f'not a JSON value: {type(value).__name__}')
