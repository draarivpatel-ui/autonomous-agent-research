import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_common import load_module, offline_network_counter

arc = load_module("research_arc_test", ROOT / "arc-prize-2026-paper-track/arc_verifier.py")
privacy = load_module("research_privacy_test", ROOT / "privacy-first-agentic-ai/privacy_agent_benchmark.py")
kernel = load_module("research_kernel_test", ROOT / "flag-os-open-computing-global-challenge-season-2/kernel_benchmark.py")
guards = load_module("research_guard_test", ROOT / "gemma-4-developer-agent-paper/guardrail_benchmark.py")


@pytest.mark.parametrize("bad_grid", [[], [[1], [1, 2]], [[-1]], [[10]], [[True]], [[1.0]]])
def test_arc_grid_contract(bad_grid):
    with pytest.raises(ValueError):
        arc.grid(bad_grid)


@pytest.mark.parametrize("op", arc.OPS)
def test_arc_program_fits_and_preserves_heldout_transform(op):
    rng = np.random.default_rng(104)
    inputs = [arc.random_grid(rng) for _ in range(4)]
    pairs = [(x, arc.apply_op(x, op)) for x in inputs[:3]]
    program = arc.Program((op,), {i: i for i in range(10)})
    assert arc.verify_training(program, pairs) in ("z3_unsat", "exact_concrete")
    assert program.predict(inputs[3]) == arc.apply_op(inputs[3], op)
    assert arc.verify_color_conjugacy(program)["status"] in ("unsat", "passed")


def test_z3_rejects_a_real_grid_counterexample():
    x = arc.grid([[1, 2], [3, 4]])
    program = arc.Program(("rot90",), {})
    assert arc.verify_training(program, [(x, x)]) == "counterexample"


def test_neural_ranker_actually_learns_and_is_seeded():
    first = arc.NeuralRanker().fit(seed=11, per_op=3, epochs=8)
    second = arc.NeuralRanker().fit(seed=11, per_op=3, epochs=8)
    np.testing.assert_allclose(first.w1, second.w1, atol=1e-14)
    probabilities = first.probabilities([(arc.grid([[0, 1], [2, 3]]), arc.grid([[2, 0], [3, 1]]))])
    assert len(probabilities) == len(arc.OPS)
    assert np.isclose(probabilities.sum(), 1)


def test_synthesis_only_receives_example_pairs():
    x = arc.grid([[0, 1], [2, 3]])
    result = arc.synthesize([(x, arc.apply_op(x, "rot90"))], timeout_s=1)
    assert result["program"] is not None
    assert result["program"].predict(x) == arc.apply_op(x, "rot90")


def test_guardrail_labels_are_not_derived_from_the_gate():
    cases = guards.make_cases(repetitions=1)
    assert len(cases) == 40
    assert sum(c["safe"] for c in cases) == 5
    assert all(c["safe"] == (c["program_safe"] and c["action_safe"]) for c in cases)


@pytest.mark.parametrize("field,value", [("private_field", "synthetic"), ("value", True),
                                       ("intent", "free-form"), ("sequence", -1)])
def test_private_schema_rejects_unconstrained_content(field, value):
    assert not privacy.validate({**privacy.fixture(1), field: value})


def test_real_aead_replay_and_nonce_reuse_controls():
    boundary = privacy.PacketBoundary()
    nonce = bytes(12)
    wire = boundary.seal(privacy.fixture(0), nonce)
    assert len(wire) == 35
    assert boundary.open(wire) == privacy.fixture(0)
    with pytest.raises(ValueError): boundary.open(wire)
    with pytest.raises(ValueError): boundary.seal(privacy.fixture(1), nonce)


def test_ciphertext_only_audit_detects_tampering():
    audit = privacy.PrivateAudit()
    audit.append("accepted", b"synthetic-ciphertext")
    assert audit.verify()
    assert set(audit.entries[0]) == {"event", "ciphertext_sha256", "previous", "hash"}
    audit.entries[0]["event"] = "rejected"
    assert not audit.verify()


def test_offline_transport_is_instrumented_and_denied():
    import socket
    with offline_network_counter() as counter:
        with pytest.raises(RuntimeError): socket.getaddrinfo("example.invalid", 443)
        assert counter["attempts"] == 1


@pytest.mark.parametrize("shape", [(1, 1), (33, 513), (5, 127), (2, 4097)])
@pytest.mark.parametrize("scale", [0.0, 1e-9, 1.0, 1000.0])
def test_fused_kernel_reference_on_edge_cases(shape, scale):
    rng = np.random.default_rng(13)
    x = (rng.normal(size=shape) * scale).astype(np.float32)
    weight = rng.normal(size=shape[1]).astype(np.float32)
    kernel.validate(x, weight, 1e-6)
    np.testing.assert_allclose(kernel.fused_cpu(x, weight), kernel.reference(x, weight), rtol=2e-5, atol=2e-5)


def test_unavailable_gpu_does_not_emit_fake_measurements(tmp_path):
    report = kernel.run(tmp_path, repeats=5, backend="cpu")
    assert not report["metadata"]["gpu_measurements_collected"]
    assert all(row["backend"] == "cpu" for row in report["summary"])
