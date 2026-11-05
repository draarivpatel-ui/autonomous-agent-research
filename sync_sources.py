"""Vendor the exact small flagship modules required for standalone reproduction."""
import json
import shutil
import subprocess
from pathlib import Path

from research_common import ROOT, sha256

SOURCES = {
    "museweaver": ["formal_verifier.py", "LICENSE"],
    "blast-radius-zero": ["gate.py", "js/policy-engine.js", "js/manifest-gate.js", "LICENSE"],
    "AirCast": ["js/modem-dsp.js", "js/private-packets.js", "LICENSE"],
}


def main():
    manifest = []
    for repo, files in SOURCES.items():
        original = ROOT.parent / "repos" / repo
        commit = subprocess.check_output(["git", "-C", str(original), "rev-parse", "HEAD"], text=True).strip()
        for name in files:
            source = original / name
            dest = ROOT / "verified_sources" / repo / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            manifest.append({"repository": repo, "file": name,
                             "base_commit": commit, "source_sha256": sha256(source)})
    (ROOT / "verified_sources" / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"vendored_files": len(manifest)}))


if __name__ == "__main__":
    main()
