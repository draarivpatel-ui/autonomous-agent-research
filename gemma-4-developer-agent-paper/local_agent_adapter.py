"""Optional free local OpenAI-compatible model adapter. No inference is claimed.

Only fixed, nonpersonal demonstration prompts and exact loopback hosts are
supported. Proxies, redirects, credentials and cloud fallbacks are disabled.
"""
import argparse
import json
import urllib.parse
import urllib.request

from agent_boundary import authorize

PROMPT = (
    "Return JSON only, with exactly proposal and manifest. "
    "proposal has kind=token_bucket, clamp=true, debit=1000000, inclusive=true. "
    "manifest has version=1, kind=test, parameters with argv=[python,-m,pytest,-q] "
    "and cwd=/workspace. Values and property names must be valid JSON."
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def ask_local_model(endpoint, model, opener=None):
    url = urllib.parse.urlsplit(endpoint)
    if (url.scheme not in ("http", "https") or url.hostname not in ("localhost", "127.0.0.1", "::1")
            or url.username or url.password or url.query or url.fragment or url.path != "/v1/chat/completions"):
        raise ValueError("Only an exact loopback chat-completions endpoint is permitted")
    if not isinstance(model, str) or not 1 <= len(model) <= 200:
        raise ValueError("Provide the actual locally served model identifier")
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": PROMPT}],
                       "temperature": 0, "max_tokens": 512}).encode()
    request = urllib.request.Request(endpoint, body, {"Content-Type": "application/json"}, method="POST")
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=30) as response:
        raw = response.read(100001)
    if len(raw) > 100000:
        raise ValueError("Local model response too large")
    data = json.loads(raw, object_pairs_hook=unique_object)
    content = data["choices"][0]["message"]["content"]
    envelope = json.loads(content, object_pairs_hook=unique_object)
    return authorize(envelope)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8080/v1/chat/completions")
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    try:
        result = ask_local_model(args.endpoint, args.model)
    except Exception:
        result = {"allowed": False, "reason": "LOCAL_MODEL_FAILED", "execution": "not_performed"}
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["allowed"] else 1)
