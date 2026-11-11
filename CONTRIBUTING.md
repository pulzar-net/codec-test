# Contributing

Issues and pull requests are welcome. A codec that runs differently here
than on ChirpStack or The Things Stack is a bug: please include the codec,
the payload, and what the server returned.

## Setup

```
python -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/pytest tests

cd js && npm install && npm test && npm run build
```

Both suites run on every pull request.

## Changing a rule

There are two runtimes, `src/codec_test/` (Python) and `js/src/index.ts`
(TypeScript), and they must agree. A change to how vectors are read, how
codecs are called, or how output is judged:

1. adds or changes a case in `fixtures/conformance.json`,
2. changes both runtimes, and
3. passes both test suites.

If the change is about matching a server, say in the pull request which
server version and which part of its source you matched, and update
"What still differs" in the README when a difference is closed or found.

`src/codec_test/chirpstack_buffer.js` is generated from ChirpStack's vendored
Buffer; don't edit it by hand. `js/src/buffer.ts` is generated from it by
`npm run embed`.

## License

Contributions are licensed under Apache-2.0, as the project is.
