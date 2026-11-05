"""Run the four local studies sequentially, with bounded CPU threading."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STUDIES = {
    "gemma-4-developer-agent-paper": "guardrail_benchmark.py",
    "arc-prize-2026-paper-track": "arc_verifier.py",
    "privacy-first-agentic-ai": "privacy_agent_benchmark.py",
    "flag-os-open-computing-global-challenge-season-2": "kernel_benchmark.py",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", choices=list(STUDIES))
    parser.add_argument("--output", type=Path, help="Optional separate run directory")
    args = parser.parse_args()
    selected = {args.study: STUDIES[args.study]} if args.study else STUDIES
    env = {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1"}
    for name, script in selected.items():
        output = args.output / name if args.output else ROOT / name / "results"
        result = subprocess.run([sys.executable, str(ROOT / name / script), "--output", str(output)],
                                capture_output=True, text=True, env=env, timeout=600)
        if result.returncode:
            # Report the child error, not captured environment or secret values.
            print(result.stderr[-4000:], file=sys.stderr)
            raise SystemExit(result.returncode)
        report = json.loads((output / "results.json").read_text())
        print(json.dumps({"study": name, "rows": len(report["summary"]), "status": "executed"}), flush=True)


if __name__ == "__main__":
    main()
