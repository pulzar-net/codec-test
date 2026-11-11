# Changelog

## 0.1.0 (unreleased)

First release.

- `codec-test` CLI and Python library: run a LoRaWAN codec in a QuickJS
  sandbox the way ChirpStack v4 (`--lns chirpstack`) or The Things Stack
  (`--lns tts`) runs it, and judge it against its test vectors.
- Reads chirpstack-device-profiles `test_decode_*.json` / `test_encode_*.json`
  (passing on `JSON.stringify` equality), lorawan-devices `*-codec.yaml`
  examples (deep equality, `normalizedOutput` included) and `tests.json`
  (only the keys `expect` names, numbers within 1e-6).
- ChirpStack's `Buffer`, strict-mode ES module evaluation, `variables` and
  `recvTime` as a `Date`; The Things Stack's `Decoder()`/`Encoder()`
  fallback in sloppy mode.
- `with_decode_uplink` wraps a legacy `Decoder()`/`Encoder()` codec for
  ChirpStack.
- A composite GitHub Action around the CLI.
- An npm package (`codec-test`) with the same rules, on quickjs-emscripten,
  for the browser and Node.
- `fixtures/conformance.json`, which both runtimes are tested against.
