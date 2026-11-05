"""Closed-schema private agent packets, real AEAD, replay rejection and AirCast DSP.

Only synthetic enumerated measurements are used. No raw microphone recording,
name, address, institution, location or free-form personal text is collected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import struct
import subprocess
import sys
import time
import threading
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.exceptions import InvalidTag

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_common import latency, sha256, source_file, write_results, offline_network_counter

INTENTS = ("measure", "status", "ack")
LEVELS = ("ok", "warning", "idle")
AAD = b"AirCast-v2"


def validate(packet) -> bool:
    return (
        isinstance(packet, dict) and set(packet) == {"version", "sequence", "intent", "level", "value"}
        and type(packet["version"]) is int and packet["version"] == 1
        and type(packet["sequence"]) is int and 0 <= packet["sequence"] <= 65535
        and packet["intent"] in INTENTS and packet["level"] in LEVELS
        and type(packet["value"]) is int and -1000 <= packet["value"] <= 1000
    )


class PacketBoundary:
    def __init__(self, key=None):
        self.aead = AESGCM(key if key is not None else AESGCM.generate_key(bit_length=256))
        self.transmitted_nonces, self.received_nonces = set(), set()
        self.lock = threading.Lock()

    def seal(self, packet, nonce=None) -> bytes:
        with self.lock:
            return self._seal(packet, nonce)

    def _seal(self, packet, nonce=None) -> bytes:
        if not validate(packet):
            raise ValueError("Closed packet schema rejected")
        nonce = nonce if nonce is not None else secrets.token_bytes(12)
        if len(nonce) != 12 or nonce in self.transmitted_nonces or len(self.transmitted_nonces) >= 10000:
            raise ValueError("Invalid, reused or capacity-limited nonce")
        clear = struct.pack(">BHBBh", packet["version"], packet["sequence"],
                            INTENTS.index(packet["intent"]), LEVELS.index(packet["level"]), packet["value"])
        result = nonce + self.aead.encrypt(nonce, clear, AAD)
        self.transmitted_nonces.add(nonce)
        return result

    def open(self, envelope: bytes) -> dict:
        with self.lock:
            return self._open(envelope)

    def _open(self, envelope: bytes) -> dict:
        if not isinstance(envelope, bytes) or len(envelope) != 35:
            raise ValueError("Invalid envelope length")
        nonce = envelope[:12]
        if nonce in self.received_nonces or len(self.received_nonces) >= 10000:
            raise ValueError("Replay or receiver capacity reached")
        clear = self.aead.decrypt(nonce, envelope[12:], AAD)
        version, sequence, intent, level, value = struct.unpack(">BHBBh", clear)
        if intent >= len(INTENTS) or level >= len(LEVELS):
            raise ValueError("Invalid closed enum")
        packet = {"version": version, "sequence": sequence, "intent": INTENTS[intent],
                  "level": LEVELS[level], "value": value}
        if not validate(packet):
            raise ValueError("Closed packet schema rejected")
        self.received_nonces.add(nonce)
        return packet


class PrivateAudit:
    """Hash-chained ciphertext digests, without payloads or personal fields."""
    def __init__(self): self.entries = []

    def append(self, event, envelope):
        if event not in ("accepted", "rejected"):
            raise ValueError("Audit event must be enumerated")
        previous = self.entries[-1]["hash"] if self.entries else "0" * 64
        entry = {"event": event, "ciphertext_sha256": hashlib.sha256(envelope).hexdigest(), "previous": previous}
        entry["hash"] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
        self.entries.append(entry)

    def verify(self):
        previous = "0" * 64
        for entry in self.entries:
            body = {k: v for k, v in entry.items() if k != "hash"}
            if entry["previous"] != previous or entry["hash"] != hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest():
                return False
            previous = entry["hash"]
        return True


def fixture(i):
    return {"version": 1, "sequence": i, "intent": INTENTS[i % 3],
            "level": LEVELS[i % 3], "value": i % 2001 - 1000}


def wilson(successes, count):
    z = 1.959963984540054
    p = successes / count
    center = (p + z * z / (2 * count)) / (1 + z * z / count)
    radius = z * ((p * (1 - p) / count + z * z / (4 * count * count)) ** 0.5) / (1 + z * z / count)
    return max(0, center - radius), min(1, center + radius)


def _run(output: Path, trials, acoustic_trials, seed, network_counter):
    if not 1 <= trials <= 10000 or not 1 <= acoustic_trials <= 100:
        raise ValueError("Require 1-10000 crypto trials and 1-100 acoustic trials")
    # Published synthetic fixture key and monotonic nonces reproduce wire bits.
    # The application default above uses fresh cryptographic keys and nonces.
    key = hashlib.sha256(b"PUBLIC-SYNTHETIC-FIXTURE-KEY-2026").digest()
    sender, receiver = PacketBoundary(key), PacketBoundary(key)
    identity = Ed25519PrivateKey.generate()
    audit = PrivateAudit()
    timings, signature_timings, recovered, signed = [], [], 0, 0
    packets = []
    for i in range(trials):
        packet = fixture(i)
        begin = time.perf_counter_ns()
        envelope = sender.seal(packet, i.to_bytes(12, "big"))
        recovered += receiver.open(envelope) == packet
        timings.append((time.perf_counter_ns() - begin) / 1e6)
        begin = time.perf_counter_ns()
        signature = identity.sign(envelope)
        identity.public_key().verify(signature, envelope)
        signed += 1
        signature_timings.append((time.perf_counter_ns() - begin) / 1e6)
        audit.append("accepted", envelope)
        if i < acoustic_trials: packets.append(envelope)
    if len(packets) < acoustic_trials:
        extra = PacketBoundary(key)
        for i in range(len(packets), acoustic_trials):
            packets.append(extra.seal(fixture(i), i.to_bytes(12, "big")))
    rows = [
        {"experiment": "AEAD_and_replay_state", "trials": trials, "successes": recovered,
         "rejected": 0, "rate": recovered / trials, **latency(timings)},
        {"experiment": "Ed25519_detached_signatures", "trials": trials, "successes": signed,
         "rejected": 0, "rate": signed / trials, **latency(signature_timings)},
    ]
    attack_records = []
    for name in ("tampered_ciphertext", "wrong_key", "replay", "truncated", "unknown_field", "invalid_enum"):
        rejected, durations = 0, []
        for i in range(100):
            packet = fixture(i)
            local = PacketBoundary(key)
            wire = local.seal(packet)
            begin = time.perf_counter_ns()
            try:
                if name == "tampered_ciphertext":
                    changed = bytearray(wire); changed[-1] ^= 1; PacketBoundary(key).open(bytes(changed))
                elif name == "wrong_key": PacketBoundary().open(wire)
                elif name == "replay": local.open(wire); local.open(wire)
                elif name == "truncated": local.open(wire[:-1])
                elif name == "unknown_field": local.seal({**packet, "private_field": "synthetic-rejected-input"})
                else: local.seal({**packet, "intent": "free-form-input"})
            except (ValueError, InvalidTag, struct.error): rejected += 1
            durations.append((time.perf_counter_ns() - begin) / 1e6)
        rows.append({"experiment": name, "trials": 100, "successes": 100 - rejected,
                     "rejected": rejected, "rate": rejected / 100, **latency(durations)})
        attack_records.append({"attack": name, "rejected": rejected, "attempts": 100})
    snrs = (-24, -18, -12, -9, -6, -3, 0, 6, 12)
    cases = [{"id": f"{snr}-{i}", "snr_db": snr, "seed": (seed + i + 100003 * si) & 0xFFFFFFFF,
              "envelope_hex": packets[i].hex()}
             for si, snr in enumerate(snrs) for i in range(acoustic_trials)]
    dsp_path = source_file("AirCast", "js/modem-dsp.js")
    probe = subprocess.run(["node", str(Path(__file__).with_name("acoustic_probe.cjs")), str(dsp_path)],
                           input=json.dumps({"cases": cases}), text=True, capture_output=True,
                           check=True, timeout=240)
    probe_result = json.loads(probe.stdout)
    observations = probe_result["observations"]
    acoustic_rows = []
    for snr in snrs:
        points = [p for p in observations if p["snr_db"] == snr]
        for mode in ("coded", "raw"):
            successes, unsafe_accepts = 0, 0
            for i, point in enumerate(points):
                wire_hex = point[f"{mode}_envelope_hex"]
                if wire_hex:
                    try:
                        decoded = PacketBoundary(key).open(bytes.fromhex(wire_hex))
                        successes += decoded == fixture(i)
                        unsafe_accepts += decoded != fixture(i)
                    except (ValueError, InvalidTag, struct.error):
                        pass
            low, high = wilson(successes, len(points))
            airtime = points[0][f"{mode}_airtime_seconds"]
            acoustic_rows.append({"snr_db": snr, "mode": mode, "trials": len(points),
                                  "authenticated_recoveries": successes, "unsafe_accepts": unsafe_accepts,
                                  "recovery_rate": successes / len(points), "wilson95_low": low, "wilson95_high": high,
                                  "airtime_seconds": airtime, "plaintext_goodput_bps": 56 * successes / len(points) / airtime,
                                  **latency([p[f"{mode}_decode_ms"] for p in points])})
    output.mkdir(parents=True, exist_ok=True)
    (output / "acoustic.json").write_text(json.dumps(acoustic_rows, indent=2) + "\n")
    from research_common import markdown_table
    (output / "ACOUSTIC.md").write_text("# Measured acoustic channel simulations\n\n" + markdown_table(acoustic_rows))
    chain_before = audit.verify()
    audit.entries[0]["event"] = "rejected"
    chain_detected = not audit.verify()
    return write_results(output, "closed-schema local private agent boundary", rows,
                         metadata={"seed": seed, "crypto_trials": trials, "acoustic_trials_per_snr": acoustic_trials,
                                   "acoustic": acoustic_rows, "dsp_source_sha256": sha256(dsp_path),
                                   "benchmark_source_sha256": sha256(Path(__file__)),
                                   "fixture_key_public_and_test_only": True, "plaintext_bytes": 7, "encrypted_envelope_bytes": 35,
                                   "audit_chain_valid_before_mutation": chain_before, "audit_tampering_detected": chain_detected,
                                   "network_attempts": network_counter["attempts"] + probe_result["network_attempts"],
                                   "network_transport": "denied and instrumented in Python and Node",
                                   "physical_acoustic_hardware_tested": False,
                                   "privacy_scope": "closed synthetic schema, not side-channel or compromised-host protection"},
                         details={"attack_counts": attack_records, "channel_observations": observations})


def run(output: Path, trials=1000, acoustic_trials=12, seed=2026):
    with offline_network_counter() as counter:
        return _run(output, trials, acoustic_trials, seed, counter)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "results")
    parser.add_argument("--trials", type=int, default=1000)
    parser.add_argument("--acoustic-trials", type=int, default=12)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    report = run(args.output, args.trials, args.acoustic_trials, args.seed)
    print(json.dumps(report["summary"], indent=2))
    print(json.dumps(report["metadata"]["acoustic"], indent=2))
