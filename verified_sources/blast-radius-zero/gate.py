"""Deterministic, offline, default-deny boundary for structured agent actions.

This module authorizes descriptions only. It never executes shell commands,
deletes files, allocates resources or sends network traffic.
"""
from __future__ import annotations

import hashlib
import json
import math
import posixpath
from dataclasses import asdict, dataclass

POLICY = {
    "version": 1,
    "workspace": "/workspace",
    "delete_preview_root": "/workspace/build",
    "hourly_budget_cents": 5000,
    "total_budget_cents": 10000,
    "maximum_duration_minutes": 60,
    "iam_actions": ["s3:GetObject", "s3:ListBucket"],
    "iam_resources": ["arn:aws:s3:::contest-demo", "arn:aws:s3:::contest-demo/*"],
    "network": "offline",
}


def digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rules: tuple[str, ...]
    manifest_sha256: str | None
    policy_sha256: str


def _integer(value: object, low: int, high: int) -> bool:
    # JSON numbers with an integral value have the same semantics in JS/Python.
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value) and value == math.floor(value)


def _path(value: object, root: str, *, include_root: bool = True) -> bool:
    if not isinstance(value, str) or len(value) > 1024:
        return False
    if not value.startswith("/") or any(c in value for c in ("\\", "\x00", "$", "*", "?", "~")):
        return False
    if any(part in ("", ".", "..") for part in value.split("/")[1:]):
        return False
    normalized = posixpath.normpath(value)
    return (include_root and normalized == root) or normalized.startswith(root + "/")


def decide(manifest: object) -> Decision:
    reasons: list[str] = []
    try:
        manifest_hash = digest(manifest)
    except (TypeError, ValueError, OverflowError, RecursionError):
        manifest_hash = None
        reasons.append("SCHEMA")
    policy_hash = digest(POLICY)
    if not isinstance(manifest, dict) or set(manifest) != {"version", "kind", "parameters"}:
        return Decision(False, ("SCHEMA",), manifest_hash, policy_hash)
    if not _integer(manifest["version"], 1, 1):
        reasons.append("SCHEMA")
    kind, params = manifest["kind"], manifest["parameters"]
    if not isinstance(kind, str) or not isinstance(params, dict):
        return Decision(False, ("SCHEMA",), manifest_hash, policy_hash)
    schemas = {
        "read": {"path"}, "test": {"argv", "cwd"}, "delete": {"path", "dry_run"},
        "iam": {"actions", "resources", "effect"},
        "compute": {"replicas", "hourly_cost_cents", "duration_minutes"},
        "network": {"host", "method", "data_class"},
    }
    if kind not in schemas or set(params) != schemas[kind]:
        return Decision(False, ("SCHEMA",), manifest_hash, policy_hash)
    if kind == "read" and not _path(params["path"], POLICY["workspace"]):
        reasons.append("PATH_SCOPE")
    elif kind == "test":
        allowed_argv = [["python", "-m", "pytest"], ["python", "-m", "pytest", "-q"],
                        ["pytest", "-q"], ["npm", "test"]]
        if params["argv"] not in allowed_argv or not _path(params["cwd"], POLICY["workspace"]):
            reasons.append("TEST_ALLOWLIST")
    elif kind == "delete":
        if params["dry_run"] is not True:
            reasons.append("DELETION_APPROVAL")
        if not _path(params["path"], POLICY["delete_preview_root"], include_root=False):
            reasons.append("PATH_SCOPE")
    elif kind == "iam":
        actions, resources = params["actions"], params["resources"]
        if (params["effect"] != "Allow"
                or not isinstance(actions, list) or not actions
                or any(not isinstance(a, str) or a not in POLICY["iam_actions"] for a in actions)
                or not isinstance(resources, list) or not resources
                or any(not isinstance(r, str) or r not in POLICY["iam_resources"] for r in resources)):
            reasons.append("LEAST_PRIVILEGE")
    elif kind == "compute":
        replicas, cents, minutes = (params[k] for k in
                                    ("replicas", "hourly_cost_cents", "duration_minutes"))
        if not (_integer(replicas, 1, 1000) and _integer(cents, 0, 1_000_000)
                and _integer(minutes, 1, POLICY["maximum_duration_minutes"])):
            reasons.append("COST_SCHEMA")
        elif (replicas * cents > POLICY["hourly_budget_cents"]
              or replicas * cents * minutes > POLICY["total_budget_cents"] * 60):
            reasons.append("COST_CAP")
    elif kind == "network":
        reasons.append("OFFLINE_EGRESS")
    return Decision(not reasons, tuple(sorted(set(reasons))), manifest_hash, policy_hash)


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    result = decide(json.loads(args.manifest.read_text()))
    print(json.dumps(asdict(result), indent=2))
    raise SystemExit(0 if result.allowed else 1)
