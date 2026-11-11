// SHA-256 and HMAC by hand: crypto.subtle does not exist on http://, and a hub on the LAN has nothing
// else to serve (ADR 017). The wall panel page and the Guest TV page load this one file (/panel/lan-sign.js;
// the hub puts it inline into /tv, so a TV keeps one file). ES2017, as the panel page: an old iPad runs it,
// and a 2018 Samsung TV polyfills padStart first. Partner's panels.test.ts compares it with node:crypto.
// lan-sign:start
const SHA_K = [];
const SHA_H = [];
for (let n = 2, found = 0; found < 64; n++) {
let prime = true;
for (let d = 2; d * d <= n; d++) if (n % d === 0) prime = false;
if (!prime) continue;
if (found < 8) SHA_H.push((Math.sqrt(n) % 1) * 4294967296 | 0);
SHA_K.push((Math.cbrt(n) % 1) * 4294967296 | 0);
found++;
}
function sha256(bytes) {
const rotr = (x, n) => (x >>> n) | (x << (32 - n));
const size = ((bytes.length + 72) >> 6) << 6;
const padded = new Uint8Array(size);
padded.set(bytes);
padded[bytes.length] = 0x80;
const view = new DataView(padded.buffer);
view.setUint32(size - 8, Math.floor(bytes.length / 536870912));
view.setUint32(size - 4, bytes.length << 3);
const h = SHA_H.slice();
const w = new Int32Array(64);
for (let at = 0; at < size; at += 64) {
for (let t = 0; t < 16; t++) w[t] = view.getInt32(at + t * 4);
for (let t = 16; t < 64; t++) {
const x = w[t - 15];
const y = w[t - 2];
w[t] = (w[t - 16] + (rotr(x, 7) ^ rotr(x, 18) ^ (x >>> 3)) + w[t - 7] + (rotr(y, 17) ^ rotr(y, 19) ^ (y >>> 10))) | 0;
}
let [a, b, c, d, e, f, g, i] = h;
for (let t = 0; t < 64; t++) {
const one = (i + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + SHA_K[t] + w[t]) | 0;
const two = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) | 0;
i = g;
g = f;
f = e;
e = (d + one) | 0;
d = c;
c = b;
b = a;
a = (one + two) | 0;
}
[a, b, c, d, e, f, g, i].forEach((v, n) => { h[n] = (h[n] + v) | 0; });
}
const out = new Uint8Array(32);
const outView = new DataView(out.buffer);
h.forEach((v, n) => outView.setInt32(n * 4, v));
return out;
}
function hmacSha256(key, message) {
const block = new Uint8Array(64);
block.set(key.length > 64 ? sha256(key) : key);
const inner = new Uint8Array(64 + message.length);
const outer = new Uint8Array(96);
for (let n = 0; n < 64; n++) {
inner[n] = block[n] ^ 0x36;
outer[n] = block[n] ^ 0x5c;
}
inner.set(message, 64);
outer.set(sha256(inner), 64);
return sha256(outer);
}
const toHex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
/** The hub stores the hash of the secret, so the hash is what both sides sign with. */
function lanSignature(secret, nonce, method, target, body) {
const text = new TextEncoder();
const key = text.encode(toHex(sha256(text.encode(secret))));
const message = [nonce, method, target, toHex(sha256(text.encode(body)))].join("\n");
return toHex(hmacSha256(key, text.encode(message)));
}
// lan-sign:end
