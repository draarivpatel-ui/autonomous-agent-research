/**
 * AirCast - modem-dsp.js
 * ============================================================
 * Pure Web-Audio DSP engine - zero dependencies.
 * Exports on window.AirCastDSP.
 *
 * • CRC-16-CCITT (poly 0x1021, init 0xFFFF)
 * • Hamming(7,4) encoder / decoder (single-bit correction)
 * • MFSK/OFDM tone-map, raised-cosine pulse shaping, PCM synthesis
 * • PCM WAV encoder (16-bit little-endian)
 * • AWGN + multipath room channel simulator
 * • Goertzel / DFT tone-energy detector + preamble detector
 * • Full async BER-sweep runner
 */

(function (global) {
  'use strict';

  // ── CRC-16-CCITT ──────────────────────────────────────────
  const CRC16_TABLE = new Uint16Array(256);
  (function buildCrcTable() {
    for (let i = 0; i < 256; i++) {
      let c = i << 8;
      for (let b = 0; b < 8; b++) c = (c & 0x8000) ? ((c << 1) ^ 0x1021) : (c << 1);
      CRC16_TABLE[i] = c & 0xFFFF;
    }
  })();

  function crc16(data) {
    let crc = 0xFFFF;
    for (let i = 0; i < data.length; i++) {
      crc = ((crc << 8) ^ CRC16_TABLE[((crc >> 8) ^ data[i]) & 0xFF]) & 0xFFFF;
    }
    return crc & 0xFFFF;
  }

  // ── Hamming(7,4) - systematic code ───────────────────────
  // Generator matrix G = [I4 | P] and parity-check H = [P^T | I3]
  // Positions: [d0 d1 d2 d3 | p0 p1 p2]  →  codeword b0..b6
  // Parity equations (even parity):
  //   p0 = d0 ^ d1 ^ d2
  //   p1 = d0 ^ d1 ^ d3
  //   p2 = d0 ^ d2 ^ d3
  // Syndrome → single-bit error position lookup

  // Syndrome → bit index to flip (0-6),  -1 = no error
  // s = H * r^T ;  s value maps to column of H
  const SYNDROME_POS = [-1, 6, 5, 3, 4, 2, 1, 0]; // columns of H, MSB-first bit positions

  function hammingEncode4bits(nibble) {
    const d0 = (nibble >> 3) & 1, d1 = (nibble >> 2) & 1;
    const d2 = (nibble >> 1) & 1, d3 = nibble & 1;
    const p0 = d0 ^ d1 ^ d2;
    const p1 = d0 ^ d1 ^ d3;
    const p2 = d0 ^ d2 ^ d3;
    return (d0 << 6) | (d1 << 5) | (d2 << 4) | (d3 << 3) | (p0 << 2) | (p1 << 1) | p2;
  }

  function hammingDecode7bits(codeword) {
    const d0 = (codeword >> 6) & 1, d1 = (codeword >> 5) & 1;
    const d2 = (codeword >> 4) & 1, d3 = (codeword >> 3) & 1;
    const p0 = (codeword >> 2) & 1, p1 = (codeword >> 1) & 1, p2 = codeword & 1;
    // Recompute expected parities
    const ep0 = d0 ^ d1 ^ d2, ep1 = d0 ^ d1 ^ d3, ep2 = d0 ^ d2 ^ d3;
    const s = ((p0 ^ ep0) << 2) | ((p1 ^ ep1) << 1) | (p2 ^ ep2);
    let corrected = codeword;
    let errorPos = -1;
    if (s !== 0) {
      errorPos = SYNDROME_POS[s];
      if (errorPos >= 0) corrected ^= (1 << (6 - errorPos));
    }
    // Extract data nibble
    const nibble = ((corrected >> 6) & 1) << 3 | ((corrected >> 5) & 1) << 2
                 | ((corrected >> 4) & 1) << 1 | ((corrected >> 3) & 1);
    return { nibble, corrected, errorPos, syndrome: s };
  }

  function hammingEncode(bytes) {
    // Each byte → 2 nibbles → 2×7-bit codewords → pack into byte stream
    // We store each 7-bit codeword in a byte (top bit 0) for simplicity.
    const out = new Uint8Array(bytes.length * 2);
    for (let i = 0; i < bytes.length; i++) {
      out[i * 2]     = hammingEncode4bits((bytes[i] >> 4) & 0xF);
      out[i * 2 + 1] = hammingEncode4bits(bytes[i] & 0xF);
    }
    return out; // each byte holds a 7-bit codeword in bits 6..0
  }

  function hammingDecode(codewords) {
    if (codewords.length % 2 !== 0) throw new RangeError('Codeword count must be even');
    const out = new Uint8Array(Math.floor(codewords.length / 2));
    const corrections = []; // { byteIdx, nibbleIdx, errorPos }
    for (let i = 0; i < codewords.length; i += 2) {
      const r0 = hammingDecode7bits(codewords[i] & 0x7F);
      const r1 = hammingDecode7bits(codewords[i + 1] & 0x7F);
      if (r0.errorPos >= 0) corrections.push({ byteIdx: i >> 1, nibble: 0, errorPos: r0.errorPos });
      if (r1.errorPos >= 0) corrections.push({ byteIdx: i >> 1, nibble: 1, errorPos: r1.errorPos });
      out[i >> 1] = (r0.nibble << 4) | r1.nibble;
    }
    return { bytes: out, corrections };
  }

  // ── Frame builder / parser ────────────────────────────────
  const PREAMBLE = new Uint8Array([0xAA, 0x56]); // v2 integrity-after-correction marker

  /**
   * Build framed packet bytes:
   * [0xAA 0x55] [lenHi lenLo] [hamming(payload)] [crcHi crcLo]
   * CRC is over [lenHi lenLo | hamming(payload)] - preamble excluded.
   */
  function buildFrame(payloadBytes) {
    if (!(payloadBytes instanceof Uint8Array) || payloadBytes.length > 32767) {
      throw new RangeError('Payload must be bytes with at most 32767 bytes');
    }
    const encoded = hammingEncode(payloadBytes);
    const len = encoded.length;
    const header = new Uint8Array([PREAMBLE[0], PREAMBLE[1], (len >> 8) & 0xFF, len & 0xFF]);
    // CRC over length + encoded payload
    const crcData = new Uint8Array(2 + payloadBytes.length);
    crcData[0] = header[2]; crcData[1] = header[3];
    crcData.set(payloadBytes, 2);
    const c = crc16(crcData);
    const tail = new Uint8Array([(c >> 8) & 0xFF, c & 0xFF]);
    const frame = new Uint8Array(header.length + encoded.length + tail.length);
    frame.set(header, 0);
    frame.set(encoded, header.length);
    frame.set(tail, header.length + encoded.length);
    return { frame, encoded, crc: c, len };
  }

  function parseFrame(frame) {
    if (frame.length < 6) return { ok: false, reason: 'Frame too short' };
    if (frame[0] !== PREAMBLE[0] || frame[1] !== PREAMBLE[1]) return { ok: false, reason: 'Bad or legacy preamble' };
    const len = (frame[2] << 8) | frame[3];
    if (len % 2 || frame.length !== 4 + len + 2) return { ok: false, reason: 'Invalid payload length' };
    const encoded = frame.slice(4, 4 + len);
    const rxCrc = (frame[4 + len] << 8) | frame[4 + len + 1];
    if (encoded.some(c => c > 127)) return { ok: false, reason: 'Invalid codeword padding' };
    const { bytes: payload, corrections } = hammingDecode(encoded);
    const crcData = new Uint8Array(2 + payload.length);
    crcData[0] = frame[2]; crcData[1] = frame[3];
    crcData.set(payload, 2);
    const calcCrc = crc16(crcData);
    const crcOk = rxCrc === calcCrc;
    return { ok: crcOk, reason: crcOk ? 'OK' : 'CRC mismatch', payload, corrections, len, rxCrc, calcCrc };
  }

  // ── Helpers ───────────────────────────────────────────────
  function strToBytes(str) { return new TextEncoder().encode(str); }
  function bytesToStr(bytes) { return new TextDecoder().decode(bytes); }
  function bytesToHex(bytes, maxLen) {
    const n = maxLen ? Math.min(bytes.length, maxLen) : bytes.length;
    let s = '';
    for (let i = 0; i < n; i++) s += bytes[i].toString(16).padStart(2, '0') + ' ';
    if (maxLen && bytes.length > maxLen) s += '…';
    return s.trim();
  }
  function frameToHexDump(frame) { return bytesToHex(frame); }
  function bitStreamStr(frame) {
    let s = '';
    for (let i = 0; i < frame.length; i++) s += frame[i].toString(2).padStart(8, '0') + ' ';
    return s.trim();
  }

  // ── Acoustic profiles ─────────────────────────────────────
  const PROFILES = {
    audible: {
      id: 'audible',
      label: 'Audible Classroom Chime',
      freqs: [1850, 2250, 2650, 3100, 3550, 4000], // 6-FSK carriers (Hz)
      baudMs: 12,          // simulated fast 4-FSK symbol duration
      preambleMs: 220,     // chirp duration
      sampleRate: 44100,
    },
    ultrasonic: {
      id: 'ultrasonic',
      label: 'Near-Ultrasonic Whisper',
      freqs: [16500, 17000, 17500, 18000, 18600, 19200],
      baudMs: 14,
      preambleMs: 180,
      sampleRate: 48000,
    },
    stress: {
      id: 'stress',
      label: 'Noisy Multipath Stress-Test',
      freqs: [1850, 2250, 2650, 3100, 3550, 4000],
      baudMs: 12,
      preambleMs: 220,
      sampleRate: 44100,
    },
  };

  // ── Symbol mapping ────────────────────────────────────────
  // 4-FSK: 2 bits per symbol → pick among 4 of the profile freqs.
  // We use the 4 middle freqs for data; outer freqs reserved for chirp.
  // Actually use all freqs: preamble chirp sweeps, data uses 4-FSK.
  // Simpler: 2-FSK per bit is too slow. Use 4-FSK with 2 bits/symbol.

  function getDataFreqs(profile) {
    // Use freqs[1..4] as 4-FSK constellation (00,01,10,11)
    return profile.freqs.slice(1, 5);
  }

  function bitsToSymbols(bitArray) {
    // Pack bits into 2-bit symbols
    const symbols = [];
    for (let i = 0; i < bitArray.length; i += 2) {
      const b0 = bitArray[i], b1 = (i + 1 < bitArray.length) ? bitArray[i + 1] : 0;
      symbols.push((b0 << 1) | b1);
    }
    return symbols;
  }

  function frameToBits(frame) {
    const bits = [];
    for (let i = 0; i < frame.length; i++)
      for (let b = 7; b >= 0; b--) bits.push((frame[i] >> b) & 1);
    return bits;
  }

  // ── PCM Synthesis (raised-cosine windowing) ──────────────
  function synthesizePCM(frame, profile) {
    if (!profile || !Number.isSafeInteger(profile.sampleRate) || profile.sampleRate < 8000 || profile.sampleRate > 96000 ||
        !Number.isFinite(profile.baudMs) || profile.baudMs < 1 || profile.baudMs > 1000 ||
        !Number.isFinite(profile.preambleMs) || profile.preambleMs < 1 || profile.preambleMs > 1000 ||
        !Array.isArray(profile.freqs) || profile.freqs.length !== 6 ||
        !profile.freqs.every(f => Number.isFinite(f) && f > 0 && f < profile.sampleRate / 2))
      throw new RangeError('Invalid or aliased acoustic profile');
    const sr = profile.sampleRate;
    const symSamples = Math.round((profile.baudMs / 1000) * sr);
    const preambleSamples = Math.round((profile.preambleMs / 1000) * sr);
    const dataFreqs = getDataFreqs(profile);
    const bits = frameToBits(frame);
    const symbols = bitsToSymbols(bits);

    const totalSamples = preambleSamples + symbols.length * symSamples;
    if (!Number.isSafeInteger(totalSamples) || totalSamples > 20000000)
      throw new RangeError('Waveform exceeds the bounded 20-million-sample synthesizer');
    const pcm = new Float32Array(totalSamples);

    // Preamble: linear chirp from freqs[0] to freqs[5]
    const f0 = profile.freqs[0], f1 = profile.freqs[profile.freqs.length - 1];
    for (let n = 0; n < preambleSamples; n++) {
      const t = n / sr;
      const frac = n / preambleSamples;
      const f = f0 + (f1 - f0) * frac;
      const env = 0.5 * (1 - Math.cos(Math.PI * Math.min(1, n / (symSamples * 0.3))));
      // fade in for first 30% of preamble
      const fadeIn = n < symSamples * 0.3 ? 0.5 * (1 - Math.cos(Math.PI * n / (symSamples * 0.3))) : 1;
      // fade out last 10% of preamble
      const fadeOut = n > preambleSamples - symSamples * 0.2
        ? 0.5 * (1 + Math.cos(Math.PI * (n - (preambleSamples - symSamples * 0.2)) / (symSamples * 0.2))) : 1;
      const duration = preambleSamples / sr;
      pcm[n] = Math.sin(2 * Math.PI * (f0 * t + 0.5 * (f1 - f0) * t * t / duration)) * 0.88 * fadeIn * fadeOut;
    }

    // Data symbols: MFSK tones with raised-cosine windowing
    const rollOff = Math.round(symSamples * 0.12); // raised-cosine roll-off samples
    for (let s = 0; s < symbols.length; s++) {
      const freq = dataFreqs[symbols[s]];
      const offset = preambleSamples + s * symSamples;
      for (let n = 0; n < symSamples; n++) {
        const t = (offset + n) / sr;
        // Raised-cosine window
        let w = 1;
        if (n < rollOff) w = 0.5 * (1 - Math.cos(Math.PI * n / rollOff));
        else if (n >= symSamples - rollOff) w = 0.5 * (1 - Math.cos(Math.PI * (symSamples - n) / rollOff));
        pcm[offset + n] = Math.sin(2 * Math.PI * freq * t) * 0.82 * w;
      }
    }

    return { pcm, sampleRate: sr, symSamples, preambleSamples, numSymbols: symbols.length, dataFreqs, bits };
  }

  // ── WAV encoder (16-bit PCM) ──────────────────────────────
  function encodeWAV(pcm, sampleRate) {
    const numSamples = pcm.length;
    const blockAlign = 2, byteRate = sampleRate * blockAlign;
    const dataSize = numSamples * 2;
    const buf = new ArrayBuffer(44 + dataSize);
    const view = new DataView(buf);
    let o = 0;
    function str4(s) { for (let i = 0; i < 4; i++) view.setUint8(o++, s.charCodeAt(i)); }
    function u32(v) { view.setUint32(o, v, true); o += 4; }
    function u16(v) { view.setUint16(o, v, true); o += 2; }
    str4('RIFF'); u32(36 + dataSize); str4('WAVE');
    str4('fmt '); u32(16); u16(1); u16(1); u32(sampleRate); u32(byteRate); u16(blockAlign); u16(16);
    str4('data'); u32(dataSize);
    for (let i = 0; i < numSamples; i++) {
      const s = Math.max(-1, Math.min(1, pcm[i]));
      view.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7FFF, true); o += 2;
    }
    return new Blob([buf], { type: 'audio/wav' });
  }

  // ── Channel simulator: AWGN + multipath echo ─────────────
  // Box-Muller Gaussian
  function seededRandom(seed = 42) {
    let state = seed >>> 0;
    return () => { state = (Math.imul(1664525, state) + 1013904223) >>> 0; return state / 4294967296; };
  }
  function gaussian(rng) {
    let u, v, s;
    do { u = rng() * 2 - 1; v = rng() * 2 - 1; s = u * u + v * v; } while (s >= 1 || s === 0);
    const m = Math.sqrt(-2 * Math.log(s) / s);
    return u * m;
  }

  function addAWGN(pcm, snrDb, rng = seededRandom(42)) {
    if (!Number.isFinite(snrDb) || snrDb < -100 || snrDb > 100 || !pcm.length) {
      throw new RangeError('Require nonempty PCM and SNR between -100 and 100 dB');
    }
    // SNR = 10 log10(Psig / Pnoise)  →  noise power = signal power / 10^(SNR/10)
    let sigPow = 0;
    for (let i = 0; i < pcm.length; i++) sigPow += pcm[i] * pcm[i];
    sigPow /= pcm.length;
    const noisePow = sigPow / Math.pow(10, snrDb / 10);
    const noiseStd = Math.sqrt(noisePow);
    const out = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) out[i] = pcm[i] + gaussian(rng) * noiseStd;
    return out;
  }

  function addMultipath(pcm, sampleRate, delayMs, attenuation) {
    const delaySamp = Math.round((delayMs / 1000) * sampleRate);
    const out = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) {
      out[i] = pcm[i] + (i >= delaySamp ? pcm[i - delaySamp] * attenuation : 0);
    }
    return out;
  }

  function simulateChannel(pcm, sampleRate, opts) {
    // opts: { snrDb, multipathDelayMs, multipathAtten }
    let out = pcm;
    if (opts.snrDb != null) out = addAWGN(out, opts.snrDb, seededRandom(opts.seed ?? 42));
    if (opts.multipathDelayMs && opts.multipathAtten) {
      out = addMultipath(out, sampleRate, opts.multipathDelayMs, opts.multipathAtten);
    }
    return out;
  }

  // ── Goertzel detector ────────────────────────────────────
  function goertzelMag(samples, start, N, targetFreq, sampleRate) {
    const k = (N * targetFreq) / sampleRate;
    const omega = (2 * Math.PI * k) / N;
    const coeff = 2 * Math.cos(omega);
    let s0 = 0, s1 = 0, s2 = 0;
    for (let n = 0; n < N; n++) {
      s0 = samples[start + n] + coeff * s1 - s2;
      s2 = s1; s1 = s0;
    }
    const real = s1 - s2 * Math.cos(omega);
    const imag = s2 * Math.sin(omega);
    return real * real + imag * imag;
  }

  function detectSymbols(pcm, profile) {
    const sr = profile.sampleRate;
    const symSamples = Math.round((profile.baudMs / 1000) * sr);
    const preambleSamples = Math.round((profile.preambleMs / 1000) * sr);
    const dataFreqs = getDataFreqs(profile);

    // Skip preamble, demodulate each symbol window
    const numSymbols = Math.floor((pcm.length - preambleSamples) / symSamples);
    const symbols = [];
    const energies = []; // per-symbol energy vector for visualization

    for (let s = 0; s < numSymbols; s++) {
      const offset = preambleSamples + s * symSamples;
      const mags = dataFreqs.map(f => goertzelMag(pcm, offset, symSamples, f, sr));
      energies.push(mags.slice());
      let best = 0, bestMag = mags[0];
      for (let k = 1; k < mags.length; k++) if (mags[k] > bestMag) { bestMag = mags[k]; best = k; }
      symbols.push(best);
    }
    return { symbols, energies, numSymbols, symSamples, preambleSamples, dataFreqs };
  }

  function symbolsToBits(symbols) {
    const bits = [];
    for (const sym of symbols) { bits.push((sym >> 1) & 1); bits.push(sym & 1); }
    return bits;
  }

  function bitsToFrame(bits) {
    const numBytes = Math.floor(bits.length / 8);
    const frame = new Uint8Array(numBytes);
    for (let i = 0; i < numBytes; i++) {
      let b = 0;
      for (let k = 0; k < 8; k++) b = (b << 1) | bits[i * 8 + k];
      frame[i] = b;
    }
    return frame;
  }

  function demodulate(pcm, profile) {
    const { symbols, energies, numSymbols } = detectSymbols(pcm, profile);
    const bits = symbolsToBits(symbols);
    const frame = bitsToFrame(bits);
    const parsed = parseFrame(frame);
    let payloadStr = '';
    try { if (parsed.payload) payloadStr = bytesToStr(parsed.payload); } catch (_) {}
    return { symbols, energies, bits, frame, parsed, payloadStr, numSymbols };
  }

  // ── SNR estimation helper ────────────────────────────────
  function estimateSNR(energies) {
    if (!energies.length) return 0;
    let sigSum = 0, noiseSum = 0;
    for (const mags of energies) {
      const sorted = mags.slice().sort((a, b) => b - a);
      sigSum += sorted[0];
      // noise = mean of non-max bins
      let n = 0; for (let i = 1; i < sorted.length; i++) n += sorted[i];
      noiseSum += n / (sorted.length - 1);
    }
    const sigAvg = sigSum / energies.length;
    const noiseAvg = Math.max(noiseSum / energies.length, 1e-12);
    return 10 * Math.log10(sigAvg / noiseAvg);
  }

  // ── BER sweep (50 trials × SNR points) ────────────────────
  function buildUncodedFrame(payload) {
    const frame = new Uint8Array(payload.length + 6);
    frame.set([0xAA, 0x57, payload.length >> 8, payload.length & 255]);
    frame.set(payload, 4);
    const crc = crc16(frame.slice(2, -2));
    frame[frame.length - 2] = crc >> 8; frame[frame.length - 1] = crc & 255;
    return frame;
  }
  function parseUncodedFrame(frame) {
    if (frame.length < 6 || frame[0] !== 0xAA || frame[1] !== 0x57 ||
        frame.length !== ((frame[2] << 8) | frame[3]) + 6) return false;
    return crc16(frame.slice(2, -2)) === ((frame[frame.length - 2] << 8) | frame[frame.length - 1]);
  }
  async function runBERSweep(payloadStr, profile, snrPoints, trialsPerPoint, onProgress, seed = 42) {
    if (!Number.isInteger(trialsPerPoint) || trialsPerPoint < 1) throw new RangeError('Trials must be positive');
    const payloadBytes = strToBytes(payloadStr);
    const { frame } = buildFrame(payloadBytes);
    const synth = synthesizePCM(frame, profile);
    const uncoded = synthesizePCM(buildUncodedFrame(payloadBytes), profile);
    const results = [];

    for (let pi = 0; pi < snrPoints.length; pi++) {
      const snrDb = snrPoints[pi];
      let uncodedOk = 0, codedOk = 0;

      for (let t = 0; t < trialsPerPoint; t++) {
        const trialSeed = (seed + pi * 100003 + t) >>> 0;
        const noisy = addAWGN(synth.pcm, snrDb, seededRandom(trialSeed));
        const res = demodulate(noisy, profile);
        if (res.parsed.ok) codedOk++;
        const raw = addAWGN(uncoded.pcm, snrDb, seededRandom(trialSeed));
        const rawFrame = bitsToFrame(symbolsToBits(detectSymbols(raw, profile).symbols));
        if (parseUncodedFrame(rawFrame)) uncodedOk++;
      }

      results.push({
        snrDb,
        codedRate: codedOk / trialsPerPoint,
        uncodedRate: uncodedOk / trialsPerPoint,
        trials: trialsPerPoint, seed,
        codedDurationMs: synth.pcm.length / profile.sampleRate * 1000,
        uncodedDurationMs: uncoded.pcm.length / profile.sampleRate * 1000,
      });

      if (onProgress) {
        onProgress({ done: pi + 1, total: snrPoints.length, point: results[results.length - 1] });
        // yield to UI thread
        await new Promise(r => setTimeout(r, 0));
      }
    }
    return results;
  }

  // ── Duration / bitrate helpers ───────────────────────────
  function estimateDurationMs(frameLen, profile) {
    const bits = frameLen * 8;
    const symbols = Math.ceil(bits / 2); // 2 bits/symbol
    return profile.preambleMs + symbols * profile.baudMs;
  }
  function estimateBitrate(frameLen, profile) {
    const bits = frameLen * 8;
    const durSec = estimateDurationMs(frameLen, profile) / 1000;
    return Math.round(bits / durSec);
  }

  global.AirCastDSP = {
    crc16, CRC16_TABLE,
    hammingEncode4bits, hammingDecode7bits, hammingEncode, hammingDecode,
    PREAMBLE, buildFrame, parseFrame,
    strToBytes, bytesToStr, bytesToHex, frameToHexDump, bitStreamStr,
    PROFILES, getDataFreqs, frameToBits, bitsToSymbols,
    synthesizePCM, encodeWAV,
    addAWGN, addMultipath, simulateChannel, seededRandom, buildUncodedFrame, parseUncodedFrame,
    goertzelMag, detectSymbols, symbolsToBits, bitsToFrame, demodulate,
    estimateSNR, runBERSweep,
    estimateDurationMs, estimateBitrate,
  };

})(typeof window !== 'undefined' ? window : globalThis);
