/* Local cryptographic primitives. No personal fields, storage or network calls. */
(function (root) {
  "use strict";
  const encoder = new TextEncoder();
  const b64 = bytes => btoa(String.fromCharCode(...new Uint8Array(bytes)));
  const unb64 = text => Uint8Array.from(atob(text), c => c.charCodeAt(0));
  const canonicalCredential = c => JSON.stringify({
    version: c.version, issuer: c.issuer, subject: c.subject, badge: c.badge,
  });
  async function createCredential() {
    const key = await crypto.subtle.generateKey({ name: "Ed25519" }, false, ["sign", "verify"]);
    const c = { version: 1, issuer: "ephemeral-local-key",
      subject: crypto.randomUUID(), badge: "acoustic-modem-v2" };
    const signature = await crypto.subtle.sign("Ed25519", key.privateKey, encoder.encode(canonicalCredential(c)));
    return { ...c, public_key: b64(await crypto.subtle.exportKey("raw", key.publicKey)), signature: b64(signature) };
  }
  async function verifyCredential(c) {
    try {
      if (!c || JSON.stringify(Object.keys(c).sort()) !==
          JSON.stringify(["badge", "issuer", "public_key", "signature", "subject", "version"]) ||
          c.version !== 1 || c.issuer !== "ephemeral-local-key" || c.badge !== "acoustic-modem-v2" ||
          typeof c.subject !== "string" || !/^[0-9a-f-]{36}$/.test(c.subject) ||
          typeof c.public_key !== "string" || typeof c.signature !== "string") return false;
      const key = await crypto.subtle.importKey("raw", unb64(c.public_key), "Ed25519", false, ["verify"]);
      return await crypto.subtle.verify("Ed25519", key, unb64(c.signature), encoder.encode(canonicalCredential(c)));
    } catch (_) { return false; }
  }
  function validatePacket(packet) {
    return packet !== null && typeof packet === "object" && !Array.isArray(packet) &&
      JSON.stringify(Object.keys(packet).sort()) === JSON.stringify(["intent", "level", "sequence", "value", "version"]) &&
      packet.version === 1 && Number.isInteger(packet.sequence) && packet.sequence >= 0 && packet.sequence <= 65535 &&
      ["measure", "status", "ack"].includes(packet.intent) && ["ok", "warning", "idle"].includes(packet.level) &&
      Number.isInteger(packet.value) && packet.value >= -1000 && packet.value <= 1000;
  }
  async function createBoundary() {
    const key = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
    const seen = new Set(), pending = new Set();
    return {
      async seal(packet) {
        if (!validatePacket(packet)) throw new Error("Closed packet schema rejected");
        const nonce = crypto.getRandomValues(new Uint8Array(12));
        const encrypted = await crypto.subtle.encrypt(
          { name: "AES-GCM", iv: nonce, additionalData: encoder.encode("AirCast-v2") },
          key, encoder.encode(JSON.stringify(packet)));
        return { nonce: b64(nonce), ciphertext: b64(encrypted) };
      },
      async open(envelope) {
        if (!envelope || JSON.stringify(Object.keys(envelope).sort()) !== JSON.stringify(["ciphertext", "nonce"]) ||
            typeof envelope.nonce !== "string" || typeof envelope.ciphertext !== "string" ||
            envelope.ciphertext.length > 4096 || envelope.nonce.length > 24)
          throw new Error("Malformed, replayed or capacity-limited packet");
        const nonce = unb64(envelope.nonce);
        if (nonce.length !== 12) throw new Error("Invalid nonce");
        const nonceId = b64(nonce);
        if (envelope.nonce !== nonceId || seen.has(nonceId) || pending.has(nonceId) || seen.size + pending.size >= 10000)
          throw new Error("Noncanonical, replayed or capacity-limited packet");
        pending.add(nonceId);
        try {
          const clear = await crypto.subtle.decrypt(
            { name: "AES-GCM", iv: nonce, additionalData: encoder.encode("AirCast-v2") }, key, unb64(envelope.ciphertext));
          const packet = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(clear));
          if (!validatePacket(packet)) throw new Error("Closed packet schema rejected");
          seen.add(nonceId);
          return packet;
        } finally { pending.delete(nonceId); }
      },
    };
  }
  root.AirCastPrivacy = { createCredential, verifyCredential, createBoundary, validatePacket };
  if (typeof module !== "undefined") module.exports = root.AirCastPrivacy;
})(typeof window === "undefined" ? globalThis : window);
