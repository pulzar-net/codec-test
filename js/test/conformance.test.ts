// The shared fixtures, run against the browser runner.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse } from 'yaml';
import {
  fromChirpstack, fromPulzar, fromTtn, judge, parseJsLiteral, runAll, runVector, withDecodeUplink,
  type Lns, type Vector,
} from '../src/index.ts';

const fixtures = new URL('../../fixtures/', import.meta.url);
const read = (path: string) => readFileSync(new URL(path, fixtures), 'utf8');
const conformance = JSON.parse(read('conformance.json'));

test('judge', () => {
  for (const c of conformance.judge) {
    const vector: Vector = 'routine' in c.vector ? c.vector : fromPulzar(c.vector);
    const out = 'output' in c ? JSON.stringify(c.output) : undefined;
    const result = judge(vector, out, c.threw);
    assert.equal(result.passed, c.passed, `${c.name}: ${JSON.stringify(result)}`);
    assert.deepEqual((result.diffs ?? []).map((d) => d.field), c.diffs, c.name);
  }
});

test('run', async () => {
  for (const c of conformance.run) {
    const result = await runVector(c.codec, c.vector, { lns: c.lns as Lns });
    assert.equal(result.passed, c.passed, `${c.name}: ${JSON.stringify(result)}`);
    assert.equal('crash' in result, c.crash, `${c.name}: ${JSON.stringify(result)}`);
  }
});

test('formats', async () => {
  const codec = read('formats/decoder.js');
  const results = [
    ...(await runAll(codec, JSON.parse(read('formats/tests.json')))),
    ...(await runAll(codec, fromChirpstack((await parseJsLiteral(read('formats/test_decode_decoder.json'))) as never))),
    ...(await runAll(codec, fromChirpstack((await parseJsLiteral(read('formats/test_encode_decoder.json'))) as never, 'encodeDownlink'))),
  ];
  for (const section of fromTtn(parse(read('formats/decoder-codec.yaml')))) {
    results.push(...(await runAll(codec, section.vectors, { lns: 'tts' })));
  }
  assert.equal(results.length, 12);
  for (const r of results) assert.ok(r.passed, JSON.stringify(r));
});

test('wrap', () => {
  for (const c of conformance.wrap) {
    assert.equal(withDecodeUplink(c.source), c.wrapped, c.name);
    assert.equal(withDecodeUplink(c.wrapped), c.wrapped, `${c.name}, twice`);
  }
});
