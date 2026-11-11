# codec-test

Run a LoRaWAN payload codec the way the network server runs it, and test it
against its examples the way the codec's own repository does. One CLI takes
all three common test-vector shapes:

| File | From | Passes when |
|---|---|---|
| `test_decode_<codec>.json`, `test_encode_<codec>.json` | [chirpstack-device-profiles](https://github.com/chirpstack/chirpstack-device-profiles) | `JSON.stringify(output) === JSON.stringify(expected)`, key order included |
| `<codec>-codec.yaml` (`examples`, `normalizedOutput`) | [TTN lorawan-devices](https://github.com/TheThingsNetwork/lorawan-devices) | the output deep-equals `output` (lodash `isEqual`) |
| `tests.json` beside `decoder.js` | this project ([below](#test-files)) | the keys `expect` names match `data` (numbers within 1e-6); `expect: null` means the server must refuse the frame |

Codecs run in [QuickJS](https://bellard.org/quickjs/): no filesystem, no
network, a time limit a loop cannot catch, a 16 MB memory limit, and a fresh
sandbox per call. There is a Python package (CLI and library), a GitHub
Action, and an npm package that runs the same rules in the browser. Both
runtimes are held to the same fixtures (`fixtures/conformance.json`).

## Install

```
pip install codec-test          # CLI and Python library, Python 3.9+
npm install codec-test          # browser and Node runner
```

Neither is published yet. Until then, install from a checkout:
`pip install .` at the root, or `npm install ./js`.

## Quick start

A codec and its tests, side by side:

```js
// decoder.js
function decodeUplink(input) {
  if (input.bytes.length < 2) return { errors: ['too short'] };
  return { data: { temperature: ((input.bytes[0] << 8) | input.bytes[1]) / 10 } };
}
```

```json
[
  { "name": "21.5 °C", "fPort": 1, "bytes": "00d7", "expect": { "temperature": 21.5 } },
  { "name": "too short", "bytes": "00", "expect": null }
]
```

```
$ codec-test decoder.js tests.json
tests.json (decoder.js, chirpstack)
  ✓ 21.5 °C
  ✓ too short
2 of 2 passed
```

## CLI

```
codec-test vendors/acme/codecs/test_decode_widget.json
codec-test vendor/acme/widget-codec.yaml
codec-test decoder.js tests.json
codec-test --lns tts --json my-codec.js test_decode_my-codec.json
```

Without a `.js` argument each file names its codec: `<codec>.js` beside
`test_decode_<codec>.json`, the YAML's `fileName`, `decoder.js` beside
`tests.json`. It exits 0 when every vector passes, 1 when one fails, 2 when a
file cannot be read. `--json` prints a machine-readable report instead.

`--lns` picks the network server to imitate. By default it is `tts` for YAML
and `chirpstack` for everything else. The CLI runs in UTC, as both servers
and both repositories' CI do.

## Test files

**`tests.json`** is this project's own shape, for codecs that have no
repository of their own. It is a JSON array of uplinks:

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | shown in the report |
| `bytes` | yes | the payload as hex (spaces and colons ignored) or an array of numbers |
| `fPort` | no | defaults to 1 |
| `expect` | no | absent: the server must accept the frame. `null`: it must refuse it. An object: it must accept it, and each key given must match `data` (numbers within 1e-6); keys not given are not compared |
| `recvTime` | no | an ISO time, passed to the codec as a `Date` |
| `variables` | no | ChirpStack device variables |
| `parsed`, `transport` | no | passed through unchanged, for non-LoRaWAN transports such as BLE |

A frame is refused when the codec throws, returns a non-empty `errors` array,
or returns no `data` object, as both servers refuse it.

**`test_decode_<codec>.json`** and **`test_encode_<codec>.json`** are
chirpstack-device-profiles' files: a JavaScript array literal (hex numbers
and comments allowed) of `{name, input, expected}`.

**`<codec>-codec.yaml`** is lorawan-devices' codec file: `examples` of
`{description, input, output, normalizedOutput}` under `uplinkDecoder`,
`downlinkEncoder` and `downlinkDecoder`.

[`fixtures/formats/`](fixtures/formats) has one codec tested in all three
shapes.

## What the codec is given

|  | `--lns chirpstack` | `--lns tts` |
|---|---|---|
| Evaluated as | an ES module (strict mode) | a script (sloppy mode) |
| `decodeUplink` input | `{bytes, fPort, recvTime: Date, variables}` | `{bytes, fPort, recvTime: Date}` |
| `encodeDownlink` input | `{data, variables}` (no `fPort`) | `{data, fPort}` |
| `Decoder(bytes, port)` / `Encoder(obj, port)` | not called (the CLI adds wrappers and says so) | called when the new names are missing |
| `decodeDownlink`, `normalizeUplink` | — | called by name |
| `Buffer` | ChirpStack's (feross/buffer) | — |
| Refused output | a throw, a non-empty `errors` array, no `data` object | the same |

That is the input for `tests.json` vectors, which describe an uplink. The
other two formats spell the input out, and it is passed exactly as written,
as their own runners do. So a `recvTime` in a chirpstack-device-profiles test
arrives as a string, and there are no `variables` unless the test gives them.

## Library

```python
from codec_test import Codec, CodecError, encode_downlink, load, run_vectors, with_decode_uplink

codec = Codec(source)                          # lns='chirpstack', 100 ms per call
data = codec.decode(payload, f_port, received)  # raises CodecError when the server would refuse it
frame, port = encode_downlink(source, {'interval': 3600})

results = run_vectors(source, json.load(open('tests.json')))  # [{name, passed, output, errors, diffs, crash}]
for codec_path, vectors, lns in load('widget-codec.yaml'):
    run_vectors(open(codec_path).read(), vectors, lns=lns)
```

`Codec.decode` takes extra keyword arguments and passes them into the input
unchanged, for transports that carry more than LoRaWAN does (e.g. BLE's
`parsed` and `transport`). `tests.json` vectors may carry `recvTime`,
`variables`, `parsed` and `transport` too.

## Browser and Node

```ts
import { runAll, failureReason, fromChirpstack, fromTtn, parseJsLiteral } from 'codec-test';

const results = await runAll(source, vectors, { lns: 'chirpstack' });
results.filter((r) => !r.passed).map(failureReason);
```

The WASM (~1 MB) loads on the first run. `fromTtn` takes the YAML already
parsed, so the package does not pull in a YAML parser.

## GitHub Action

```yaml
- uses: actions/checkout@v7
- uses: pulzar-net/codec-test@v0
  with:
    files: vendors/**/codecs/test_*.json
    # lns: chirpstack          # or tts; by default tts for YAML, chirpstack otherwise
    # python-version: '3.12'
```

`files` is space-separated and globs are expanded, `**` included. The step
fails when any vector fails. No release is tagged yet; until there is one,
pin a commit SHA.

## How close it is

All the vectors in both repositories, run on 2026-11-11:

- **chirpstack-device-profiles**: 443 of 445 tests pass. The two failures are
  `digital-matter-g62`, whose codec reads `new Date()`, so its expected output
  is only right on the day it was written. Another 20 decentlab test files name
  codecs that do not exist, and ChirpStack's runner skips them too.
- **lorawan-devices**: 4024 of 4129 examples pass. The 105 failures sampled
  were all defects in the repository itself: 56 come from Dragino files that
  are a single line of literal `\n` escapes, and most of the rest are examples
  that disagree with their codec (different key names, stale values) or codecs
  that call `console` or an undefined `decimal`. They have not been checked
  against TTN's own runner, which needs Go and the whole lorawan-stack module.

## What still differs

- **The engine.** The Things Stack runs codecs in goja, not QuickJS. Syntax
  goja lacks passes here and fails there.
- **Output conversion.** Both servers convert the returned object themselves
  rather than reading its JSON. Here a `Date` in `data` comes out as its ISO
  string. ChirpStack converts the object directly, so a `Date` or `undefined`
  does not come out as `JSON.stringify` writes it, and The Things Stack stores
  a `Date` as Unix nanoseconds.
- **Limits.** Live decoding (`Codec`) gets 100 ms, both servers' default. A
  test vector gets 1 s, as TTN's runner gives one. ChirpStack sets no memory
  limit; this sets 16 MB.
- **Bytes out.** `encodeDownlink` returning a value outside 0–255 is an error
  here. ChirpStack saturates it (256 → 255, -1 → 0).
- **Runners.** lorawan-devices' runner calls `decodeUplink` directly, so a
  codec with only `Decoder()` fails there but passes here under `tts`, as it
  works on The Things Stack. chirpstack-device-profiles evaluates the test
  file in the same module as the codec; here the test file is evaluated alone,
  so a test file cannot refer to the codec's definitions.
- **YAML.** PyYAML is made to read booleans and timestamps the way js-yaml
  does. Other YAML 1.1 corners (`1e3` without a dot, `1:30`) still read
  differently. A timestamp in an expected output is a Date to js-yaml, which
  never equals JSON output there, but it can match here.
- **Time zone.** The CLI runs in UTC. The library uses the process's time
  zone and the browser uses the viewer's, so a codec that formats local time
  can disagree with the server.
- **Buffer.** It is loaded only when the codec's source mentions `Buffer`. A
  codec that declares its own `let Buffer` is a SyntaxError on ChirpStack
  (where `Buffer` is an import) and fine here.
- **The 1e-6 tolerance** in `tests.json` is ours. Neither repository has one.

## Conformance fixtures

[`fixtures/conformance.json`](fixtures/conformance.json) is what keeps the
Python and TypeScript runtimes the same. Both test suites read it:

- `judge`: a vector, the codec's `output` (or what it `threw`), and whether it
  `passed`, with the `diffs` expected.
- `run`: a `codec` source, an `lns`, a vector, and whether it `passed` or
  `crash`ed. These exercise the sandbox: input shape, strict mode, `Buffer`,
  time limits, the legacy `Decoder()` fallback.
- `wrap`: a legacy `Decoder()`/`Encoder()` codec and the source
  `with_decode_uplink` turns it into.

## Development

```
python -m venv .venv && .venv/bin/pip install -e . pytest && .venv/bin/pytest tests
cd js && npm install && npm test && npm run build
```

`src/codec_test/chirpstack_buffer.js` is the one copy of ChirpStack's
Buffer. `js/scripts/embed-buffer.mjs` turns it into `js/src/buffer.ts` before
the browser build and tests. Rules change in both runtimes together, with a
case in `fixtures/conformance.json`.

## License

See [LICENSE](LICENSE). `chirpstack_buffer.js` is feross/buffer, base64-js and ieee754 (MIT, MIT, BSD-3-Clause) by way of ChirpStack (MIT); their licenses are in [THIRD_PARTY_LICENSES](THIRD_PARTY_LICENSES).
