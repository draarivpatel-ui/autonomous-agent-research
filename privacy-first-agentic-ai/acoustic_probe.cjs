"use strict";
const fs = require("node:fs");
const { performance } = require("node:perf_hooks");
let networkAttempts = 0;
const net = require("node:net");
net.Socket.prototype.connect = () => { networkAttempts++; throw Error("Offline DSP network guard"); };
// Execute the real AirCast module. This process has no browser, mic or network.
require(process.argv[2]);
const dsp = globalThis.AirCastDSP;
const request = JSON.parse(fs.readFileSync(0, "utf8"));
const profile = dsp.PROFILES.audible;
const results = [];
for (const item of request.cases) {
  const bytes = Uint8Array.from(Buffer.from(item.envelope_hex, "hex"));
  const coded = dsp.synthesizePCM(dsp.buildFrame(bytes).frame, profile);
  const raw = dsp.synthesizePCM(dsp.buildUncodedFrame(bytes), profile);
  const begin = performance.now();
  const decoded = dsp.demodulate(dsp.addAWGN(coded.pcm, item.snr_db, dsp.seededRandom(item.seed)), profile);
  const codedMs = performance.now() - begin;
  const rawBegin = performance.now();
  const rawFrame = dsp.bitsToFrame(dsp.symbolsToBits(
    dsp.detectSymbols(dsp.addAWGN(raw.pcm, item.snr_db, dsp.seededRandom(item.seed)), profile).symbols));
  const rawOk = dsp.parseUncodedFrame(rawFrame);
  results.push({
    id: item.id, snr_db: item.snr_db, coded_crc_ok: decoded.parsed.ok,
    coded_envelope_hex: decoded.parsed.ok ? Buffer.from(decoded.parsed.payload).toString("hex") : null,
    raw_crc_ok: rawOk, raw_envelope_hex: rawOk ? Buffer.from(rawFrame.slice(4, -2)).toString("hex") : null,
    corrected_codewords: decoded.parsed.corrections?.length || 0,
    coded_decode_ms: codedMs, raw_decode_ms: performance.now() - rawBegin,
    coded_airtime_seconds: coded.pcm.length / profile.sampleRate,
    raw_airtime_seconds: raw.pcm.length / profile.sampleRate,
  });
}
process.stdout.write(JSON.stringify({observations: results, network_attempts: networkAttempts}));
