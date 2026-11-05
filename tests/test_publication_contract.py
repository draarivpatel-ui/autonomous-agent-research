import csv
import json
import sys
from pathlib import Path

import nbformat
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_common import load_module, sha256
from reproduce import STUDIES
from public_audit import scan_text


@pytest.mark.parametrize("study", STUDIES)
def test_notebook_is_valid_and_actually_executed(study):
    notebook = nbformat.read(ROOT / study / "paper.ipynb", as_version=4)
    nbformat.validate(notebook)
    code = [cell for cell in notebook.cells if cell.cell_type == "code"]
    assert code and all(cell.execution_count is not None for cell in code)
    assert all(output.output_type != "error" for cell in code for output in cell.outputs)
    assert all(not scan_text(cell.source) for cell in notebook.cells)


@pytest.mark.parametrize("study", STUDIES)
def test_actual_results_match_csv_and_measured_source(study):
    report = json.loads((ROOT / study / "results/results.json").read_text())
    with (ROOT / study / "results/summary.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == len(report["summary"])
    assert report["metadata"]["common_source_sha256"] == sha256(ROOT / "research_common.py")
    for csv_row, row in zip(rows, report["summary"]):
        for key, value in row.items():
            if isinstance(value, (float, int)):
                assert float(csv_row[key]) == pytest.approx(value)
            else:
                assert csv_row[key] == str(value)
        assert row["median_ms"] >= 0 and row["p95_ms"] >= row["median_ms"]


def test_public_corpus_integrity_and_no_private_score_claim():
    folder = ROOT / "arc-prize-2026-paper-track"
    manifest = json.loads((folder / "data/MANIFEST.json").read_text())
    assert manifest["tasks"] == 400
    assert manifest["dataset_sha256"] == sha256(folder / "data/arc_public_training.json")
    report = json.loads((folder / "results/results.json").read_text())
    assert sum(r["tasks"] for r in report["summary"] if r["split"] == "arc_public_training") == 800


def test_research_sources_are_exact_snapshots():
    manifest = json.loads((ROOT / "verified_sources/MANIFEST.json").read_text())
    for record in manifest:
        copied = ROOT / "verified_sources" / record["repository"] / record["file"]
        assert sha256(copied) == record["source_sha256"]


def test_optional_metal_kernel_is_actually_checked_when_available():
    kernel = load_module("optional_metal_test", ROOT / "flag-os-open-computing-global-challenge-season-2/kernel_benchmark.py")
    if not kernel.metal_capability()["available"]:
        assert "reason" in kernel.metal_capability()
        return
    import mlx.core as mx
    metal = load_module("optional_metal_impl", ROOT / "flag-os-open-computing-global-challenge-season-2/mlx_rmsnorm.py")
    rng = np.random.default_rng(91)
    for shape in [(1, 1), (3, 33), (5, 127), (2, 513), (2, 4097)]:
        for scale in [0.0, 1e-9, 1.0, 1000.0]:
            x = (rng.normal(size=shape) * scale).astype(np.float32)
            weights = rng.normal(size=shape[1]).astype(np.float32)
            out = metal.rmsnorm(mx.array(x), mx.array(weights))
            mx.eval(out)
            np.testing.assert_allclose(np.asarray(out), kernel.reference(x, weights), rtol=2e-5, atol=2e-5)


def test_release_gate_detects_synthetic_categories_without_embedding_real_private_data():
    assert "personal_home_path" in scan_text("/User" + "s/test-user/document.py")
    assert "personal_email" in scan_text("synthetic-test@" + "gmail.com")
    assert "non_ascii_dash" in scan_text("title" + chr(0x2014) + "subtitle")
    assert not scan_text("Anonymous local measurements with public source hashes")
