// A door sensor, written once and tested in every repository's shape.
function decodeUplink(input) {
  var b = input.bytes;
  if (input.fPort !== 1) return { errors: ['unknown FPort'] };
  if (b.length < 3) return { errors: ['too short'] };
  return { data: { open: b[0] === 1, count: (b[1] << 8) | b[2], state: b[0] === 1 ? 'on' : 'off' } };
}

function encodeDownlink(input) {
  return { fPort: 2, bytes: [input.data.interval >> 8, input.data.interval & 0xff] };
}

function decodeDownlink(input) {
  return { data: { interval: (input.bytes[0] << 8) | input.bytes[1] } };
}

function normalizeUplink(input) {
  return { data: { action: { contactState: input.data.open ? 'open' : 'closed' } } };
}
