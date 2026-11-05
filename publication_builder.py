"""Generate empirical paper appendices, valid notebooks and measured SVG figures."""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from pathlib import Path

import nbformat
from jupyter_client import KernelManager
from nbclient import NotebookClient

from research_common import ROOT, markdown_table
from reproduce import STUDIES

START, END = "<!-- MEASURED_APPENDIX_START -->", "<!-- MEASURED_APPENDIX_END -->"


def comparison_svg(name, report):
    if name.startswith("gemma"):
        entries = [(r["mode"], 100 * r["unsafe_rejection_rate"]) for r in report["summary"]]
        unit, caption = "%", "Unsafe fixture rejection, 40 distinct combinations repeated 12 times"
    elif name.startswith("arc"):
        entries = [(r["split"] + " / " + r["mode"], 100 * r["exact_task_accuracy"]) for r in report["summary"]]
        unit, caption = "%", "Exact task accuracy, generated and public corpora are different distributions"
    elif name.startswith("privacy"):
        entries = [(str(r["snr_db"]) + " dB / " + r["mode"], 100 * r["recovery_rate"])
                   for r in report["metadata"]["acoustic"]]
        unit, caption = "%", "Authenticated acoustic recovery, 12 trials per simulated point"
    else:
        entries = [(r["backend"] + " / " + r["implementation"], r["median_ms"])
                   for r in report["summary"] if r["rows"] == 1024 and r["features"] == 4096]
        unit, caption = " ms", "Median synchronized latency for a 1024 by 4096 float32 matrix"
    maximum = max([v for _, v in entries] + [1.0]) if unit != "%" else 100
    height = 64 + len(entries) * 32
    elements = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 760 {height}" role="img" aria-label="{html.escape(caption)}">',
                '<style>text{font:12px sans-serif;fill:currentColor}.bar{fill:var(--chart-1,#58a6ff)}.track{fill:var(--secondary,#d3dce8)}</style>',
                f'<text x="4" y="18">{html.escape(caption)}</text>']
    for i, (label, value) in enumerate(entries):
        y = 40 + i * 32
        elements += [f'<text x="4" y="{y + 13}">{html.escape(label)}</text>',
                     f'<rect class="track" x="390" y="{y}" width="270" height="18" rx="3"/>',
                     f'<rect class="bar" x="390" y="{y}" width="{270 * value / maximum:.4f}" height="18" rx="3"/>',
                     f'<text x="670" y="{y + 13}">{value:.3f}{unit}</text>']
    return "".join(elements) + "</svg>"


def notebook_code(name, script):
    return f'''import json, os, subprocess, sys, tempfile
from pathlib import Path
from IPython.display import display, Markdown, SVG

# Run from the bundle root or the paper directory. No personal path is embedded.
candidates = [Path.cwd(), *Path.cwd().parents]
ROOT = next((p for p in candidates if (p / "research_common.py").is_file()), None)
if ROOT is None and Path("/kaggle/input").is_dir():
    mounted = list(Path("/kaggle/input").glob("*/research_common.py"))
    mounted += list(Path("/kaggle/input").glob("*/kaggle_submissions/research_common.py"))
    if len(mounted) == 1:
        ROOT = mounted[0].parent
if ROOT is None:
    raise RuntimeError("Open this notebook within the complete research bundle")
sys.path.insert(0, str(ROOT))
from research_common import markdown_table
from publication_builder import comparison_svg

env = {{**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1"}}
with tempfile.TemporaryDirectory(prefix="research-reproduction-") as temporary:
    output = Path(temporary)
    child = subprocess.run(
        [sys.executable, str(ROOT / {name!r} / {script!r}), "--output", str(output)],
        text=True, capture_output=True, env=env, timeout=600,
    )
    if child.returncode:
        raise RuntimeError("Benchmark failed; inspect the local command output")
    report = json.loads((output / "results.json").read_text())

assert report["schema_version"] == 1
assert report["summary"]
display(Markdown("## Recomputed empirical results\\n\\n" + markdown_table(report["summary"])))
if "acoustic" in report["metadata"]:
    display(Markdown("## Recomputed acoustic results\\n\\n" + markdown_table(report["metadata"]["acoustic"])))
display(SVG(comparison_svg({name!r}, report)))
print(json.dumps({{"environment": report["environment"], "metadata": report["metadata"]}}, indent=2))
'''


def build(name, script, execute=False):
    folder = ROOT / name
    report = json.loads((folder / "results" / "results.json").read_text())
    article = (folder / "PAPER.md").read_text().split(START)[0].rstrip()
    appendix = "\n\n" + START + "\n\n## Appendix of executed measurements\n\n" + markdown_table(report["summary"])
    if "acoustic" in report["metadata"]:
        appendix += "\n### Acoustic measurements\n\n" + markdown_table(report["metadata"]["acoustic"])
    appendix += "\n### Measurement provenance\n\n" + "```json\n" + json.dumps(
        {"generated_utc": report["generated_utc"], "environment": report["environment"], "metadata": report["metadata"]},
        indent=2) + "\n```\n\n" + END + "\n"
    (folder / "PAPER.md").write_text(article + appendix)
    svg = comparison_svg(name, report)
    (folder / "results" / "comparison.svg").write_text(svg + "\n")
    (folder / "results" / "comparison.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Measured benchmark comparison</title>'
        '<style>html,body{background:transparent;margin:0;color:var(--foreground);font-family:var(--font-sans)}'
        'svg{width:100%;height:auto}</style>' + svg + "</html>\n")
    notebook = nbformat.v4.new_notebook(cells=[
        nbformat.v4.new_markdown_cell(article),
        nbformat.v4.new_markdown_cell(
            "## Executable reproduction\n\nThe next cell reruns the actual benchmark. "
            "Only the bundled public data and synthetic fixtures are used. "
            "Install the pinned requirements first. Hardware-dependent timings may change. "
            "Unavailable GPU backends are not relabelled as CPU results."),
        nbformat.v4.new_code_cell(notebook_code(name, script)),
    ], metadata={"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
                 "language_info": {"name": "python"}, "artifact": {"study": name, "ai_assistance": "disclosed"}})
    if execute:
        manager = KernelManager(kernel_name="python3")
        manager.kernel_spec.argv = [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"]
        client = NotebookClient(notebook, km=manager, timeout=600, resources={"metadata": {"path": str(folder)}})
        try:
            client.execute(env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1"})
        finally:
            if manager.has_kernel:
                manager.shutdown_kernel(now=True)
            manager.cleanup_resources()
    nbformat.validate(notebook)
    nbformat.write(notebook, folder / "paper.ipynb")
    print(json.dumps({"study": name, "notebook_executed": execute, "cells": len(notebook.cells)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    for name, script in STUDIES.items():
        build(name, script, args.execute)
