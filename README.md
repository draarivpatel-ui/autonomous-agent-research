# Autonomous Agent Research Artifacts

Four reproducible technical papers, executable notebooks and measured benchmark ledgers. The scope and limits of each evaluation are stated explicitly.

## Included studies

| Directory | Executed experiment | Important scope |
| --- | --- | --- |
| gemma-4-developer-agent-paper | Real MuseWeaver Z3 proofs combined with the BlastRadius Zero action policy | Generated arithmetic mutants, not Gemma inference |
| arc-prize-2026-paper-track | Trained neural operator ranking, program synthesis and scoped invariance checks | All 400 pinned public ARC training tasks, not a private contest score |
| privacy-first-agentic-ai | Real AES-GCM, Ed25519, replay controls and AirCast acoustic DSP | Synthetic packets and simulated channels, not recordings of people |
| flag-os-open-computing-global-challenge-season-2 | Measured NumPy optimization and supplemental local Metal GPU kernel | Triton CUDA/HIP source supplied, but not measured on this host |

Each study contains `PAPER.md`, `paper.ipynb`, its executable benchmark, raw `results/results.json`, a generated CSV table and a measured SVG comparison. Privacy also includes a separate acoustic table. ARC includes its pinned public corpus, license, dataset manifest and trained ranker artifact.

## Reproduce

Python 3.13 was validated on the local host. Use a fresh environment and the platform-qualified, hash-pinned lock.

```bash
uv venv --python 3.13 .venv
uv pip sync --python .venv/bin/python requirements-lock.txt
.venv/bin/python reproduce.py
.venv/bin/python publication_builder.py --execute
.venv/bin/python -m pytest -q --import-mode=importlib
.venv/bin/python public_audit.py
```

Without uv, create a Python 3.13 venv and install with `python -m pip install --require-hashes -r requirements-lock.txt`.

The four studies execute sequentially. Numerical prediction counts are seeded; measured time can change with hardware and system load. The notebook builder reruns every study rather than presenting prefilled outputs as new measurements.

Dependency preparation may use the internet. Benchmark execution does not download data or model weights. The optional local-model adapter is separate and only calls a loopback server.

## Standalone source and licensing

`verified_sources/` contains exact small modules from MuseWeaver, BlastRadius Zero and AirCast. A manifest records hashes and the visible repository base commits. The live campaign workspace and standalone package use the same algorithms.

Program code is MIT licensed. The public ARC data retains the original Apache 2.0 license and attribution. Consolidation changes JSON packaging only, not input or output grids.

## Using the notebooks on Kaggle

Upload the complete bundle as a code/data dataset, attach it to the notebook, and prepare the pinned dependencies. The notebook locates the bundle at its working directory or a standard attached dataset mount. It writes rerun results only to a temporary writable directory.

The included `kernel-metadata.example.json` files are explicitly templates. Replace the Kaggle handle and attach the actual dataset. No user handle, account, competition acceptance or upload has been invented.

Do not claim a private leaderboard score from the public ARC corpus. Do not claim Gemma inference from verification fixtures. Do not claim Triton performance from the Metal supplement. The papers and raw metadata keep those distinctions visible.

## Target venues and publication readiness

The requested campaign targets include Kiroween, AWS MSP AI, Meta Muse Spark Open, Lablab, DoraHacks and the four named paper competitions. Advertised prize targets came from the campaign brief unless otherwise verified; they are not promised awards.

Kiroween's current official rules were read. They require an age-of-majority entrant, sufficient real Kiro usage, a newly created project during the submission window, a licensed public GitHub repository and a demonstration under three minutes. Eligibility is not established by repository code.

The Gemma 4 and ARC Prize Paper Track page titles were publicly verified. Their complete entry requirements still need confirmation. Other venue-specific rules, target hardware, model-use evidence and eligibility must be confirmed before submitting.

See [campaign decisions](CAMPAIGN.md) and [Hack Club audit](HACK_CLUB_AUDIT.md). No contest portal, reward form, paid cloud resource or shipping data was changed in this build.

## Privacy and evidence

The release gate scans text, notebooks and reports for private email, home paths, address patterns, named institutions, vendor key prefixes and non-ASCII dash punctuation. It reports categories rather than matched values.

This is a conservative text gate, not a forensic review of existing binary video or all Git history. Legacy demo videos are unchanged and should not be used as evidence for the newly measured implementation.

All measured results come from actually executing the scripts. The papers do not assert human-subject testing, independently authored student hours, partnerships, winner status or cryptographic guarantees beyond the stated threat models.
