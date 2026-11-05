"""Model-independent approval of a restricted proposal and structured action."""
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_common import load_module, source_file

formal = load_module("runtime_muse_formal", source_file("museweaver", "formal_verifier.py"))
policy = load_module("runtime_blast_policy", source_file("blast-radius-zero", "gate.py"))


def authorize(envelope):
    if not isinstance(envelope, dict) or set(envelope) != {"proposal", "manifest"}:
        return {"allowed": False, "reason": "ENVELOPE_SCHEMA"}
    decision = policy.decide(envelope["manifest"])
    try:
        proof = formal.verify_proposal(envelope["proposal"])
    except (ValueError, TypeError):
        return {"allowed": False, "reason": "PROPOSAL_SCHEMA", "policy": asdict(decision)}
    return {"allowed": proof.verified and decision.allowed,
            "proof": asdict(proof), "policy": asdict(decision), "execution": "not_performed"}


if __name__ == "__main__":
    try:
        result = authorize(json.load(sys.stdin))
    except (ValueError, TypeError):
        result = {"allowed": False, "reason": "INVALID_JSON"}
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["allowed"] else 1)
