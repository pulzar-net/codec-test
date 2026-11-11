import { getQuickJS } from 'quickjs-emscripten';
import type { QuickJSContext, QuickJSWASMModule } from 'quickjs-emscripten';
import { BUFFER } from './buffer.ts';

/**
 * Runs a LoRaWAN payload codec in the browser the way the network server
 * does, and judges it against test vectors the way each codec repository
 * does. The Python package (codec_test) is the same, rule for rule;
 * fixtures/conformance.json holds both to it.
 *
 * QuickJS compiled to WebAssembly: a codec gets no DOM and no network, a
 * loop hits an interrupt instead of freezing the tab, and each call gets a
 * fresh runtime, as ChirpStack gives each uplink.
 */

export type Lns = 'chirpstack' | 'tts';
export type Routine = 'decodeUplink' | 'encodeDownlink' | 'decodeDownlink' | 'normalizeUplink';
export type Match = 'subset' | 'stringify' | 'equal';

/** A vector in any repository's shape, normalized. */
export interface Vector {
  name?: string;
  routine: Routine;
  /** Exactly what the routine gets, before recvTime becomes a Date. */
  input: Record<string, unknown>;
  match: Match;
  expect?: unknown;
  /** Build input as the network server does: recvTime a Date, variables {}. */
  lnsInput?: boolean;
}

/** A Pulzar tests.json entry. */
export interface PulzarVector {
  name: string;
  fPort?: number;
  /** Hex, e.g. "0124092c2d07", or the bytes. */
  bytes: string | number[];
  /** Expected subset of `data`. Null: the network server must refuse the frame. */
  expect?: Record<string, unknown> | null;
  recvTime?: string;
  variables?: Record<string, string>;
  /** BLE only. */
  parsed?: unknown;
  transport?: string;
}

/** @deprecated The console's old name. */
export type TestVector = PulzarVector;

export interface TestResult {
  name?: string;
  passed: boolean;
  output?: unknown;
  errors?: unknown[];
  diffs?: { field: string; expected: unknown; actual: unknown }[];
  crash?: string;
  note?: string;
}

export interface RunOptions {
  lns?: Lns;
  now?: Date;
}

export class CodecError extends Error {}

/** What TTN's runscript and this runner have always given one vector. */
export const VECTOR_TIME_LIMIT_MS = 1000;
export const MEMORY_LIMIT = 16 * 1024 * 1024;

let modulePromise: Promise<QuickJSWASMModule> | null = null;
function quickjs() {
  // ~1MB of WASM, loaded on the first run.
  modulePromise ??= getQuickJS();
  return modulePromise;
}

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);
const has = (o: object, k: string) => Object.prototype.hasOwnProperty.call(o, k);

function callExpression(routine: Routine, lns: Lns) {
  // The Things Stack's own fallback for The Things Network V2 functions.
  if (lns === 'tts' && routine === 'decodeUplink') {
    return "typeof decodeUplink === 'function' ? decodeUplink(input) : { data: Decoder(input.bytes, input.fPort) }";
  }
  if (lns === 'tts' && routine === 'encodeDownlink') {
    return "typeof encodeDownlink === 'function' ? encodeDownlink(input) : { bytes: Encoder(input.data, input.fPort), fPort: input.fPort }";
  }
  return `${routine}(input)`;
}

function evaluate(context: QuickJSContext, code: string, type: 'module' | 'global') {
  const result = context.evalCode(code, 'codec.js', { type });
  if (result.error) {
    const e = context.dump(result.error) as { name?: string; message?: string } | string;
    result.error.dispose();
    throw new CodecError(typeof e === 'object' && e ? `${e.name}: ${e.message}` : String(e));
  }
  // A module evaluates to a promise; one that rejected threw at the top level.
  const state = context.getPromiseState(result.value);
  if (state.type === 'rejected') {
    const e = context.dump(state.error) as { name?: string; message?: string };
    state.error.dispose();
    result.value.dispose();
    throw new CodecError(`${e?.name}: ${e?.message}`);
  }
  if (state.type === 'fulfilled' && !state.notAPromise) state.value.dispose();
  result.value.dispose();
}

async function sandbox<T>(source: string, lns: Lns, timeLimitMs: number, use: (c: QuickJSContext) => T) {
  const QuickJS = await quickjs();
  const runtime = QuickJS.newRuntime();
  const deadline = Date.now() + timeLimitMs;
  runtime.setInterruptHandler(() => Date.now() > deadline);
  runtime.setMemoryLimit(MEMORY_LIMIT);
  const context = runtime.newContext();
  try {
    // ChirpStack's Buffer, when the codec could be using it.
    if (lns === 'chirpstack' && source.includes('Buffer')) evaluate(context, BUFFER, 'global');
    return use(context);
  } finally {
    context.dispose();
    runtime.dispose();
  }
}

/**
 * Run one routine over `input` in a fresh sandbox. `out` is JSON.stringify
 * of what it returned (undefined for undefined), `threw` what it threw.
 * Throws CodecError when the program cannot run at all.
 */
export async function call(
  source: string,
  routine: Routine,
  input: unknown,
  { lns = 'chirpstack', lnsInput = false, timeLimitMs = VECTOR_TIME_LIMIT_MS }: { lns?: Lns; lnsInput?: boolean; timeLimitMs?: number } = {},
): Promise<{ out?: string; threw?: string }> {
  const prepare = lnsInput ? 'if (input.recvTime !== undefined) input.recvTime = new Date(input.recvTime);' : '';
  const program = `${source}
;globalThis.__lct = (function () {
  var input = ${JSON.stringify(input)}; ${prepare}
  try { return { out: JSON.stringify(${callExpression(routine, lns)}) }; }
  catch (e) { return { threw: String((e && e.message) || e) }; }
})();
`;
  return sandbox(source, lns, timeLimitMs, (context) => {
    evaluate(context, program, lns === 'chirpstack' ? 'module' : 'global');
    const result = context.evalCode('JSON.stringify(globalThis.__lct)');
    const raw = context.dump(context.unwrapResult(result) as never) as string;
    return JSON.parse(raw) as { out?: string; threw?: string };
  });
}

/** Why the network server would refuse a decoder's output, or undefined. */
export function uplinkRejection(decoded: unknown, threw: string | undefined, routine: string = 'decodeUplink') {
  if (threw !== undefined) return threw;
  if (!isObject(decoded)) return `${routine} returned ${JSON.stringify(decoded) ?? 'undefined'}, not an object`;
  const errors = decoded.errors;
  if (Array.isArray(errors) && errors.length) {
    return errors.map((e) => (typeof e === 'string' ? e : JSON.stringify(e))).join('; ');
  }
  if (!isObject(decoded.data)) return `${routine} did not return 'data'`;
  return undefined;
}

// ── Vector shapes ────────────────────────────────────────────────────────────

/** Anything not hex is dropped (spaces, colons); an odd length is refused. */
export function hexToBytes(hex: string): number[] {
  const clean = (hex ?? '').replace(/[^0-9a-fA-F]/g, '');
  if (clean.length % 2 !== 0) throw new Error(`hex payload has an odd length: ${hex}`);
  const bytes: number[] = [];
  for (let i = 0; i < clean.length; i += 2) bytes.push(parseInt(clean.slice(i, i + 2), 16));
  return bytes;
}

export function fromPulzar(v: PulzarVector): Vector {
  const input: Record<string, unknown> = {
    bytes: Array.isArray(v.bytes) ? v.bytes : hexToBytes(v.bytes),
    fPort: v.fPort ?? 1,
  };
  for (const key of ['recvTime', 'variables', 'parsed', 'transport'] as const) {
    if (has(v, key)) input[key] = v[key];
  }
  const out: Vector = { name: v.name, routine: 'decodeUplink', input, match: 'subset', lnsInput: true };
  if (has(v, 'expect') && v.expect !== undefined) {
    if (v.expect !== null && !isObject(v.expect)) throw new Error('expect must be an object or null');
    out.expect = v.expect;
  }
  return out;
}

/** chirpstack-device-profiles' test_decode_/test_encode_ entries. */
export function fromChirpstack(
  tests: { name?: string; input?: Record<string, unknown>; expected?: unknown }[],
  routine: Routine = 'decodeUplink',
): Vector[] {
  return tests.map((t) => ({ name: t.name, routine, input: t.input ?? {}, match: 'stringify', expect: t.expected ?? null }));
}

/** A parsed lorawan-devices -codec.yaml, one entry per section. */
export function fromTtn(codec: Record<string, any>): { fileName?: string; vectors: Vector[] }[] {
  const sections = [];
  for (const [key, routine] of [
    ['uplinkDecoder', 'decodeUplink'],
    ['downlinkEncoder', 'encodeDownlink'],
    ['downlinkDecoder', 'decodeDownlink'],
  ] as const) {
    const section = codec?.[key];
    if (!section) continue;
    const vectors: Vector[] = [];
    for (const e of section.examples ?? []) {
      vectors.push({ name: e.description, routine, input: e.input ?? {}, match: 'equal', expect: e.output });
      if (routine === 'decodeUplink' && e.normalizedOutput) {
        vectors.push({
          name: `${e.description} (normalized)`,
          routine: 'normalizeUplink',
          input: e.output,
          match: 'equal',
          expect: e.normalizedOutput,
        });
      }
    }
    sections.push({ fileName: section.fileName, vectors });
  }
  return sections;
}

/** A JSON-ish file that is really JavaScript (hex numbers, comments), evaluated in the sandbox. */
export async function parseJsLiteral(text: string): Promise<unknown> {
  try {
    return JSON.parse(text);
  } catch {
    return sandbox('', 'tts', VECTOR_TIME_LIMIT_MS, (context) => {
      const result = context.evalCode(`JSON.stringify((\n${text}\n))`);
      if (result.error) {
        const e = context.dump(result.error) as { message?: string };
        result.error.dispose();
        throw new Error(`not JSON or a JavaScript literal: ${e?.message}`);
      }
      return JSON.parse(context.dump(result.value)) as unknown;
    });
  }
}

// ── Judging ──────────────────────────────────────────────────────────────────

/** lodash isEqual over JSON values. */
export function deepEqual(a: unknown, b: unknown): boolean {
  if (isObject(a) && isObject(b)) {
    const keys = Object.keys(a);
    return keys.length === Object.keys(b).length && keys.every((k) => has(b, k) && deepEqual(a[k], b[k]));
  }
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => deepEqual(x, b[i]));
  return a === b;
}

const MISSING = Symbol('missing');

function subsetDiffs(expected: Record<string, unknown>, data: unknown) {
  const problems: { field: string; expected: unknown; actual: unknown }[] = [];
  for (const [key, want] of Object.entries(expected)) {
    const got = isObject(data) && has(data, key) ? data[key] : MISSING;
    const equal =
      typeof want === 'number' && typeof got === 'number'
        ? Math.abs(want - got) < 1e-6
        : got !== MISSING && JSON.stringify(want) === JSON.stringify(got);
    if (!equal) problems.push({ field: key, expected: want, actual: got === MISSING ? null : got });
  }
  return problems;
}

/** A result for one normalized vector, given what `call` returned. */
export function judge(vector: Vector, out: string | undefined, threw: string | undefined): TestResult {
  const result: TestResult = { name: vector.name, passed: false };
  const decoded: unknown = out === undefined ? null : JSON.parse(out);
  if (threw === undefined) {
    result.output = decoded;
    if (isObject(decoded) && Array.isArray(decoded.errors)) result.errors = decoded.errors;
  } else {
    result.crash = threw;
  }

  if (vector.match === 'subset') {
    const reason = uplinkRejection(decoded, threw, vector.routine);
    if (reason !== undefined && result.crash === undefined && !result.errors?.length) result.crash = reason;
    if (vector.expect === null) {
      result.passed = reason !== undefined;
      return result;
    }
    result.passed = reason === undefined;
    if (isObject(vector.expect) && reason === undefined) {
      const diffs = subsetDiffs(vector.expect, (decoded as Record<string, unknown>).data);
      if (diffs.length) {
        result.passed = false;
        result.diffs = diffs;
      }
    }
    return result;
  }

  if (threw !== undefined) return result;
  let actual = decoded;
  if (vector.routine === 'normalizeUplink' && isObject(actual) && actual.data && !Array.isArray(actual.data)) {
    actual = { ...actual, data: [actual.data] }; // what lorawan-devices does before comparing
  }
  const expected = vector.expect;
  const same =
    vector.match === 'stringify' ? (x: unknown, y: unknown) => JSON.stringify(x) === JSON.stringify(y) : deepEqual;
  result.passed =
    vector.match === 'stringify'
      ? out !== undefined && JSON.stringify(actual) === JSON.stringify(expected)
      : deepEqual(expected, actual);
  if (!result.passed) {
    if (isObject(expected) && isObject(actual)) {
      const keys = [...Object.keys(expected), ...Object.keys(actual).filter((k) => !has(expected, k))];
      result.diffs = keys
        .filter((k) => !has(expected, k) || !has(actual, k) || !same(expected[k], (actual as Record<string, unknown>)[k]))
        .map((k) => ({
          field: k,
          expected: has(expected, k) ? expected[k] : null,
          actual: has(actual as object, k) ? (actual as Record<string, unknown>)[k] : null,
        }));
    } else {
      result.diffs = [{ field: '', expected, actual }];
    }
    if (!result.diffs.length) {
      delete result.diffs;
      result.note = 'same values in a different key order; ChirpStack compares JSON.stringify';
    }
  }
  return result;
}

/** One vector (normalized, or a Pulzar tests.json entry) in a fresh sandbox. */
export async function runVector(source: string, vector: Vector | PulzarVector, options: RunOptions = {}): Promise<TestResult> {
  const lns = options.lns ?? 'chirpstack';
  try {
    const v = 'routine' in vector ? vector : fromPulzar(vector);
    let input = v.input;
    if (v.lnsInput) {
      input = { ...input };
      if (!has(input, 'recvTime')) input.recvTime = (options.now ?? new Date()).toISOString();
      if (lns === 'chirpstack' && !has(input, 'variables')) input.variables = {};
    }
    let res;
    try {
      res = await call(source, v.routine, input, { lns, lnsInput: Boolean(v.lnsInput) });
    } catch (e) {
      // The codec did not run at all: never a pass, even for a vector expecting an error.
      if (e instanceof CodecError) return { name: v.name, passed: false, crash: e.message };
      throw e;
    }
    return judge(v, res.out, res.threw);
  } catch (e) {
    return { name: vector.name, passed: false, crash: (e as Error).message };
  }
}

/** @deprecated The console's old name. */
export const runCodec = runVector;

export async function runAll(source: string, vectors: (Vector | PulzarVector)[], options: RunOptions = {}) {
  const results: TestResult[] = [];
  // Sequential: a dozen WASM runtimes in flight is worse than waiting.
  for (const vector of vectors ?? []) results.push(await runVector(source, vector, options));
  return results;
}

/** One line saying why a vector failed. */
export function failureReason(result: TestResult) {
  if (result.crash) return result.crash;
  if (result.diffs?.length) {
    return result.diffs
      .map((d) => `${d.field || 'output'}: expected ${JSON.stringify(d.expected)}, got ${JSON.stringify(d.actual)}`)
      .join(' · ');
  }
  if (result.note) return result.note;
  if (result.errors?.length) return result.errors.join('; ');
  if (result.output !== undefined) return 'decoded; expected an error';
  return 'failed';
}

// ── The older The Things Network form ────────────────────────────────────────

const DECODE_UPLINK = /\bdecodeUplink\s*(=|\()/;
const ENCODE_DOWNLINK = /\bencodeDownlink\s*(=|\()/;
const DECODER = /\bfunction\s+Decoder\s*\(|\bDecoder\s*=\s*function\b/;
const ENCODER = /\bfunction\s+Encoder\s*\(|\bEncoder\s*=\s*function\b/;

/**
 * The codec with decodeUplink/encodeDownlink calling its Decoder/Encoder when
 * it has only those, so ChirpStack can run it. A Decoder returning {errors}
 * is refusing the frame (TTS's own fallback would store them as data).
 */
export function withDecodeUplink(source: string) {
  const added: string[] = [];
  if (source && DECODER.test(source) && !DECODE_UPLINK.test(source)) {
    added.push(
      'function decodeUplink(input) {\n  var decoded = Decoder(input.bytes, input.fPort);\n' +
        '  return decoded && decoded.errors ? { errors: decoded.errors } : { data: decoded };\n}',
    );
  }
  if (source && ENCODER.test(source) && !ENCODE_DOWNLINK.test(source)) {
    added.push(
      'function encodeDownlink(input) {\n  return { bytes: Encoder(input.data, input.fPort), fPort: input.fPort };\n}',
    );
  }
  if (!added.length) return source;
  return (
    source.trimEnd() +
    "\n\n// Added by codec-test: this codec is in The Things Network's older\n" +
    '// Decoder(bytes, port) / Encoder(object, port) form.\n' +
    added.join('\n') +
    '\n'
  );
}
