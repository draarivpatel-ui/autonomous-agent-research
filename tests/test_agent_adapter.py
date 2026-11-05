import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "gemma-4-developer-agent-paper"
sys.path.insert(0, str(STUDY))
import agent_boundary
import local_agent_adapter

SAFE = {"proposal": {"kind": "token_bucket", "clamp": True, "debit": 1000000, "inclusive": True},
        "manifest": {"version": 1, "kind": "test", "parameters": {"argv": ["python", "-m", "pytest", "-q"], "cwd": "/workspace"}}}


def test_composed_runtime_uses_both_actual_layers():
    assert agent_boundary.authorize(SAFE)["allowed"]
    bad = {**SAFE, "proposal": {**SAFE["proposal"], "clamp": False}}
    assert not agent_boundary.authorize(bad)["allowed"]
    bad = {**SAFE, "manifest": {"version": 1, "kind": "unknown", "parameters": {}}}
    assert not agent_boundary.authorize(bad)["allowed"]


@pytest.mark.parametrize("url", ["https://example.invalid/v1/chat/completions",
    "http://evil-localhost.example/v1/chat/completions", "http://user:pass@localhost/v1/chat/completions",
    "http://127.0.0.1/v1/chat/completions?token=example"])
def test_remote_hosts_and_credential_urls_are_rejected(url):
    with pytest.raises(ValueError):
        local_agent_adapter.ask_local_model(url, "local-test-model")


def test_local_model_response_contract_without_network():
    class Opener:
        def open(self, request, timeout):
            assert request.full_url.startswith("http://127.0.0.1:")
            assert "Authorization" not in request.headers
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(SAFE)}}]}).encode())
    result = local_agent_adapter.ask_local_model("http://127.0.0.1:8080/v1/chat/completions",
                                                "local-test-model", Opener())
    assert result["allowed"]
    assert result["execution"] == "not_performed"


def test_duplicate_model_fields_are_rejected():
    with pytest.raises(ValueError):
        json.loads('{"proposal":{},"proposal":{}}', object_pairs_hook=local_agent_adapter.unique_object)
