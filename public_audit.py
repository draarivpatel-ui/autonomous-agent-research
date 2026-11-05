"""Conservative text release gate. Findings disclose categories, never values."""
import argparse
import json
import re
from pathlib import Path

TEXT = {".md", ".py", ".js", ".cjs", ".mjs", ".json", ".ipynb", ".html", ".txt", ".yml", ".yaml", ".csv", ".svg"}
SKIP = {".git", ".venv", ".pytest_cache", "__pycache__", ".ipynb_checkpoints", "review-packs"}
PATTERNS = {
    "personal_email": re.compile(r"@(?:gmail|hotmail|outlook)\.com", re.I),
    "personal_home_path": re.compile(r"/(?:Users|home)/[a-zA-Z0-9_.-]+/"),
    "street_address": re.compile(r"\b\d{1,6}\s+[A-Z][A-Za-z ]{1,50}\s+(?:Street|Avenue|Drive|Road|Lane|Court|Boulevard)\b"),
    "named_school": re.compile(r"\b(?:[A-Z][a-z]{2,}\s+){1,4}(?:High School|Middle School|Elementary School|Academy)\b"),
    "vendor_key_prefix": re.compile(r"\b(?:ghp_|github_pat_|AKIA|AIza|tskey-|sk-|xox|gsk_|nvapi-|tvly-)(?:[A-Za-z0-9_-]+|\.\.\.)"),
}


def scan_text(text):
    result = [name for name, pattern in PATTERNS.items() if pattern.search(text)]
    if chr(0x2013) in text or chr(0x2014) in text:
        result.append("non_ascii_dash")
    return result


def scan(root):
    findings, count = [], 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT or any(p in SKIP for p in path.relative_to(root).parts):
            continue
        count += 1
        categories = scan_text(path.read_text(errors="replace"))
        if categories:
            findings.append({"file": str(path.relative_to(root)), "categories": categories})
    return {"target": root.resolve().name, "text_files_scanned": count, "findings": findings,
            "media_scope": "binary media and existing history not covered by text gate"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    roots = args.paths or [Path(__file__).parent]
    reports = [scan(root) for root in roots]
    text = json.dumps(reports, indent=2) + "\n"
    if args.output: args.output.write_text(text)
    print(text)
    raise SystemExit(1 if any(r["findings"] for r in reports) else 0)
