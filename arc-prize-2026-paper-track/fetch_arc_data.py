"""Fetch ONLY the pinned, public ARC-AGI training split, with its license.

The tarball is read in memory. No archive member is extracted to the filesystem.
This is the only network step; benchmark and notebook execution are offline.
"""
import hashlib
import io
import json
import re
import tarfile
import urllib.request
from pathlib import Path

COMMIT = "399030444e0ab0cc8b4e199870fb20b863846f34"
URL = f"https://codeload.github.com/fchollet/ARC-AGI/tar.gz/{COMMIT}"
DEST = Path(__file__).parent / "data"


def main():
    with urllib.request.urlopen(URL, timeout=45) as response:
        blob = response.read(30_000_001)
    if len(blob) > 30_000_000:
        raise ValueError("Archive exceeds the documented download limit")
    tasks, license_text = {}, None
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        for entry in archive.getmembers():
            if not entry.isfile():
                continue
            if re.fullmatch(r"[^/]+/data/training/[0-9a-f]{8}\.json", entry.name):
                task = json.load(archive.extractfile(entry))
                tasks[Path(entry.name).stem] = task
            elif entry.name.count("/") == 1 and entry.name.endswith("/LICENSE"):
                license_text = archive.extractfile(entry).read().decode()
    if len(tasks) != 400 or not license_text or "Apache License" not in license_text:
        raise ValueError("Unexpected pinned data layout, task count or license")
    DEST.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(dict(sorted(tasks.items())), separators=(",", ":")) + "\n").encode()
    (DEST / "arc_public_training.json").write_bytes(encoded)
    (DEST / "LICENSE-ARC.txt").write_text(license_text)
    manifest = {"source_url": URL, "source_commit": COMMIT, "split": "public training",
                "tasks": len(tasks), "dataset_sha256": hashlib.sha256(encoded).hexdigest(),
                "license": "Apache-2.0", "transformation": "JSON task files consolidated without changing grids"}
    (DEST / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
