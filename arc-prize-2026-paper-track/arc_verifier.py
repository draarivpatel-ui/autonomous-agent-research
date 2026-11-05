"""Small neuro-symbolic ARC-style synthesizer with exact and invariance checks.

An actual NumPy-trained MLP ranks a fixed DSL. Only example pairs reach the
synthesizer; held-out answers are used solely by the evaluation harness.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import z3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_common import latency, sha256, write_results

OPS = ("identity", "rot90", "rot180", "rot270", "flip_h", "flip_v", "transpose",
       "upscale2", "tile2", "tile_h", "tile_v", "crop_nonzero",
       "gravity_down", "keep_largest", "fill_holes", "shift_right")
GEOMETRIC = set(OPS) - {"crop_nonzero", "gravity_down", "keep_largest", "fill_holes"}


def grid(value) -> tuple[tuple[int, ...], ...]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 30:
        raise ValueError("Require 1-30 grid rows")
    if any(not isinstance(row, (list, tuple)) for row in value):
        raise ValueError("Rows must be sequences")
    width = len(value[0])
    if not 1 <= width <= 30 or any(len(row) != width for row in value):
        raise ValueError("Require a rectangular grid with 1-30 columns")
    if any(type(c) is not int or not 0 <= c <= 9 for row in value for c in row):
        raise ValueError("ARC colors must be integers 0-9")
    return tuple(tuple(row) for row in value)


def apply_op(g, op):
    h, w = len(g), len(g[0])
    if op == "identity": result = g
    elif op == "transpose": result = tuple(zip(*g))
    elif op == "rot90": result = tuple(zip(*g[::-1]))
    elif op == "rot180": result = tuple(tuple(row[::-1]) for row in g[::-1])
    elif op == "rot270": result = tuple(zip(*g))[::-1]
    elif op == "flip_h": result = tuple(tuple(row[::-1]) for row in g)
    elif op == "flip_v": result = g[::-1]
    elif op == "upscale2":
        result = tuple(row for source in g for row in
                       [tuple(c for c in source for _ in range(2))] * 2)
    elif op == "tile2": result = tuple(tuple(row) * 2 for row in g) * 2
    elif op == "tile_h": result = tuple(tuple(row) * 2 for row in g)
    elif op == "tile_v": result = g * 2
    elif op == "shift_right": result = tuple((0,) + tuple(row[:-1]) for row in g)
    elif op == "crop_nonzero":
        positions = [(r, c) for r in range(h) for c in range(w) if g[r][c] != 0]
        if not positions: result = ((0,),)
        else:
            r0, r1 = min(r for r, _ in positions), max(r for r, _ in positions)
            c0, c1 = min(c for _, c in positions), max(c for _, c in positions)
            result = tuple(tuple(row[c0:c1 + 1]) for row in g[r0:r1 + 1])
    elif op == "gravity_down":
        columns = []
        for c in range(w):
            nonzero = [g[r][c] for r in range(h) if g[r][c] != 0]
            columns.append([0] * (h - len(nonzero)) + nonzero)
        result = tuple(tuple(columns[c][r] for c in range(w)) for r in range(h))
    elif op == "keep_largest":
        remaining = {(r, c) for r in range(h) for c in range(w) if g[r][c] != 0}
        components = []
        while remaining:
            start = min(remaining); remaining.remove(start)
            component, frontier = {start}, [start]
            while frontier:
                r, c = frontier.pop()
                for p in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                    if p in remaining:
                        remaining.remove(p); component.add(p); frontier.append(p)
            components.append(component)
        largest = max(components, key=len) if components else set()
        result = tuple(tuple(g[r][c] if (r, c) in largest else 0 for c in range(w)) for r in range(h))
    elif op == "fill_holes":
        zeros = {(r, c) for r in range(h) for c in range(w) if g[r][c] == 0}
        exterior = {p for p in zeros if p[0] in (0, h - 1) or p[1] in (0, w - 1)}
        frontier = list(exterior)
        while frontier:
            r, c = frontier.pop()
            for p in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                if p in zeros and p not in exterior:
                    exterior.add(p); frontier.append(p)
        # Counter ties follow first appearance, preserving color-renaming covariance.
        counts = Counter(c for row in g for c in row if c != 0)
        color = counts.most_common(1)[0][0] if counts else 0
        result = tuple(tuple(color if (r, c) in zeros - exterior else g[r][c]
                             for c in range(w)) for r in range(h))
    else: raise ValueError("Unknown DSL operation")
    if len(result) > 30 or len(result[0]) > 30:
        raise ValueError("Candidate exceeds the ARC grid limit")
    return result


@dataclass(frozen=True)
class Program:
    steps: tuple[str, ...]
    palette: dict[int, int]

    def predict(self, input_grid):
        value = input_grid
        for op in self.steps:
            value = apply_op(value, op)
        return tuple(tuple(self.palette.get(c, c) for c in row) for row in value)


def fit_palette(transformed, targets):
    mapping = {}
    for source, target in zip(transformed, targets):
        if len(source) != len(target) or len(source[0]) != len(target[0]):
            return None
        for xrow, yrow in zip(source, target):
            for x, y in zip(xrow, yrow):
                if x in mapping and mapping[x] != y: return None
                mapping[x] = y
    return mapping


def features(pairs) -> np.ndarray:
    values = []
    for x, y in pairs:
        xvals, yvals = [c for row in x for c in row], [c for row in y for c in row]
        values.append([len(x) / 30, len(x[0]) / 30, len(y) / 30, len(y[0]) / 30,
                       len(y) / len(x), len(y[0]) / len(x[0]),
                       sum(c != 0 for c in xvals) / len(xvals),
                       sum(c != 0 for c in yvals) / len(yvals),
                       len(set(xvals)) / 10, len(set(yvals)) / 10])
    base = np.mean(values, axis=0).tolist()
    for op in OPS:
        matches = []
        for x, y in pairs:
            try:
                candidate = apply_op(x, op)
                shape = (len(candidate), len(candidate[0])) == (len(y), len(y[0]))
                matches.append([float(shape), float(candidate == y)])
            except ValueError: matches.append([0.0, 0.0])
        base.extend(np.mean(matches, axis=0))
    return np.asarray(base, dtype=np.float64)


def random_grid(rng: np.random.Generator):
    h, w = rng.integers(2, 8, size=2)
    values = rng.integers(0, 10, size=(h, w))
    values[rng.random((h, w)) < 0.35] = 0
    return tuple(tuple(int(c) for c in row) for row in values)


class NeuralRanker:
    """One hidden-layer MLP trained by real cross-entropy gradient descent."""
    def fit(self, seed=2026, per_op=60, epochs=180):
        started = time.perf_counter_ns()
        rng = np.random.default_rng(seed)
        samples, labels = [], []
        for index, op in enumerate(OPS):
            for _ in range(per_op):
                pairs = [(x, apply_op(x, op)) for x in [random_grid(rng) for _ in range(3)]]
                samples.append(features(pairs)); labels.append(index)
        x, y = np.asarray(samples), np.asarray(labels)
        self.mean, self.scale = x.mean(0), np.maximum(x.std(0), 0.1)
        x = (x - self.mean) / self.scale
        self.w1 = rng.normal(0, 0.15, (x.shape[1], 32)); self.b1 = np.zeros(32)
        self.w2 = rng.normal(0, 0.15, (32, len(OPS))); self.b2 = np.zeros(len(OPS))
        target = np.eye(len(OPS))[y]
        for _ in range(epochs):
            hidden = np.tanh(x @ self.w1 + self.b1)
            logits = hidden @ self.w2 + self.b2
            logits -= logits.max(1, keepdims=True)
            probabilities = np.exp(logits); probabilities /= probabilities.sum(1, keepdims=True)
            error = (probabilities - target) / len(x)
            dh = (error @ self.w2.T) * (1 - hidden * hidden)
            dw1, db1 = x.T @ dh, dh.sum(0)
            dw2, db2 = hidden.T @ error, error.sum(0)
            self.w1 -= 0.12 * dw1; self.b1 -= 0.12 * db1
            self.w2 -= 0.12 * dw2; self.b2 -= 0.12 * db2
        self.training_ms = (time.perf_counter_ns() - started) / 1e6
        validation = np.random.default_rng(seed + 1)
        correct = total = 0
        for index, op in enumerate(OPS):
            for _ in range(12):
                pairs = [(x, apply_op(x, op)) for x in [random_grid(validation) for _ in range(3)]]
                correct += int(np.argmax(self.probabilities(pairs)) == index); total += 1
        self.validation_accuracy = correct / total
        self.training_samples, self.validation_samples = len(x), total
        return self

    def probabilities(self, pairs):
        x = (features(pairs) - self.mean) / self.scale
        logits = np.tanh(x @ self.w1 + self.b1) @ self.w2 + self.b2
        logits -= logits.max()
        p = np.exp(logits)
        return p / p.sum()

    def save(self, filename):
        np.savez(filename, mean=self.mean, scale=self.scale, w1=self.w1, b1=self.b1, w2=self.w2, b2=self.b2)


def symbolic_predict(g, program):
    value = g
    for op in program.steps:
        if op not in GEOMETRIC: raise ValueError("Structural branches require concrete checks")
        value = apply_op(value, op)
    def recolor(cell):
        expr = z3.IntVal(cell) if type(cell) is int else cell
        original = expr
        for source, target in sorted(program.palette.items()):
            expr = z3.If(original == source, target, expr)
        return expr
    return tuple(tuple(recolor(c) for c in row) for row in value)


def verify_training(program, pairs) -> str:
    if any(program.predict(x) != y for x, y in pairs):
        return "counterexample"
    if any(op not in GEOMETRIC for op in program.steps):
        return "exact_concrete"
    for pair_index, (x, y) in enumerate(pairs):
        symbols = tuple(tuple(z3.Int(f"p{pair_index}_{r}_{c}") for c in range(len(x[0])))
                        for r in range(len(x)))
        predicted = symbolic_predict(symbols, program)
        solver = z3.Solver(); solver.set(timeout=1000)
        solver.add([symbols[r][c] == x[r][c] for r in range(len(x)) for c in range(len(x[0]))])
        solver.add(z3.Or([predicted[r][c] != y[r][c] for r in range(len(y)) for c in range(len(y[0]))]))
        result = solver.check()
        if result != z3.unsat: return str(result)
    return "z3_unsat"


def verify_color_conjugacy(program, seed=1009) -> dict:
    rng = np.random.default_rng(seed)
    permutation = {0: 0, **dict(zip(range(1, 10), map(int, rng.permutation(np.arange(1, 10)))))}
    conjugate = Program(program.steps, {permutation[k]: permutation[v] for k, v in program.palette.items()})
    def rename(g):
        return tuple(tuple(permutation[c] for c in row) for row in g)
    if all(op in GEOMETRIC for op in program.steps):
        symbols = tuple(tuple(z3.Int(f"color_{r}_{c}") for c in range(4)) for r in range(3))
        def permute_expr(cell):
            original = z3.IntVal(cell) if type(cell) is int else cell
            expr = original
            for k, v in permutation.items(): expr = z3.If(original == k, v, expr)
            return expr
        left = tuple(tuple(permute_expr(c) for c in row) for row in symbolic_predict(symbols, program))
        right = symbolic_predict(tuple(tuple(permute_expr(c) for c in row) for row in symbols), conjugate)
        solver = z3.Solver(); solver.set(timeout=1000)
        solver.add([z3.And(c >= 0, c <= 9) for row in symbols for c in row])
        solver.add(z3.Or([a != b for xr, yr in zip(left, right) for a, b in zip(xr, yr)]))
        status = str(solver.check())
        return {"status": status, "scope": "all color assignments on a 3x4 grid", "method": "Z3"}
    passed = 0
    for _ in range(20):
        x = random_grid(rng)
        if rename(program.predict(x)) == conjugate.predict(rename(x)): passed += 1
    return {"status": "passed" if passed == 20 else "counterexample",
            "scope": "20 generated grids, one zero-preserving permutation", "method": "metamorphic", "passed": passed}


def synthesize(pairs, ranker=None, max_depth=2, timeout_s=1.0) -> dict:
    if max_depth not in (1, 2) or not 0 < timeout_s <= 60:
        raise ValueError("Require depth 1 or 2 and timeout in (0,60]")
    started = time.perf_counter_ns()
    probabilities = ranker.probabilities(pairs) if ranker else np.full(len(OPS), 1 / len(OPS))
    order = sorted(OPS, key=lambda op: (-probabilities[OPS.index(op)], OPS.index(op)))
    evaluated, program = 0, None
    status = "not_found"
    for depth in range(1, max_depth + 1):
        steps_list = list(itertools.product(order, repeat=depth))
        if ranker:
            steps_list.sort(key=lambda steps: sum(-math.log(max(probabilities[OPS.index(op)], 1e-9)) for op in steps))
        for steps in steps_list:
            if (time.perf_counter_ns() - started) / 1e9 > timeout_s:
                status = "timeout"; break
            evaluated += 1
            try:
                transformed = []
                for x, _ in pairs:
                    value = x
                    for op in steps: value = apply_op(value, op)
                    transformed.append(value)
                palette = fit_palette(transformed, [y for _, y in pairs])
                if palette is None: continue
                candidate = Program(steps, palette)
                certificate = verify_training(candidate, pairs)
                if certificate in ("z3_unsat", "exact_concrete"):
                    program, status = candidate, certificate; break
            except ValueError:
                continue
        if program or status == "timeout": break
    return {"program": program, "status": status, "candidates": evaluated,
            "search_ms": (time.perf_counter_ns() - started) / 1e6}


def synthetic_tasks(seed=9041, per_op=15):
    rng = np.random.default_rng(seed)
    tasks = {}
    for op in OPS:
        for i in range(per_op):
            inputs = [random_grid(rng) for _ in range(4)]
            tasks[f"synthetic-{op}-{i:03}"] = {
                "train": [{"input": x, "output": apply_op(x, op)} for x in inputs[:3]],
                "test": [{"input": inputs[3], "output": apply_op(inputs[3], op)}],
            }
    return tasks


def run(output: Path, data: Path, seed=2026, timeout_s=1.0) -> dict:
    dataset = json.loads(data.read_text())
    manifest = json.loads((data.parent / "MANIFEST.json").read_text())
    if len(dataset) != 400 or sha256(data) != manifest["dataset_sha256"]:
        raise ValueError("Pinned public corpus integrity check failed")
    ranker = NeuralRanker().fit(seed)
    output.mkdir(parents=True, exist_ok=True)
    ranker.save(output / "neural_ranker.npz")
    rows, details = [], []
    for split, tasks in (("generated_heldout", synthetic_tasks()), ("arc_public_training", dataset)):
        for mode in ("uniform", "neural"):
            durations, solved, pair_correct, total_pairs, candidates = [], 0, 0, 0, 0
            fits, symbolic, invariant_passed = 0, 0, 0
            for task_id, task in sorted(tasks.items()):
                pairs = [(grid(p["input"]), grid(p["output"])) for p in task["train"]]
                # No test output enters synthesize or the ranker.
                result = synthesize(pairs, ranker if mode == "neural" else None, timeout_s=timeout_s)
                program = result["program"]
                predictions, correct = [], []
                for example in task["test"]:
                    try: prediction = program.predict(grid(example["input"])) if program else None
                    except ValueError: prediction = None
                    predictions.append(prediction)
                    correct.append(prediction == grid(example["output"]))
                pair_correct += sum(correct); total_pairs += len(correct)
                solved += bool(correct) and all(correct)
                fits += program is not None
                symbolic += result["status"] == "z3_unsat"
                invariant = verify_color_conjugacy(program) if program else None
                invariant_passed += invariant is not None and invariant["status"] in ("unsat", "passed")
                durations.append(result["search_ms"]); candidates += result["candidates"]
                details.append({"split": split, "mode": mode, "task_id": task_id,
                                "correct": bool(correct) and all(correct), "pair_correct": correct,
                                "program": asdict(program) if program else None,
                                "status": result["status"], "candidates": result["candidates"],
                                "search_ms": result["search_ms"], "color_conjugacy": invariant})
            rows.append({"split": split, "mode": mode, "tasks": len(tasks), "solved": solved,
                         "exact_task_accuracy": solved / len(tasks), "test_pairs": total_pairs,
                         "exact_pair_accuracy": pair_correct / total_pairs, "training_fits": fits,
                         "z3_training_certificates": symbolic, "invariance_checks_passed": invariant_passed,
                         "mean_candidates": candidates / len(tasks), **latency(durations)})
    return write_results(output, "neuro-symbolic ARC-style synthesis", rows,
                         metadata={"seed": seed, "grammar": list(OPS), "max_depth": 2,
                                   "search_timeout_seconds": timeout_s,
                                   "source_commit": manifest["source_commit"], "dataset_sha256": sha256(data),
                                   "dataset_scope": "all 400 public ARC-AGI training tasks, never private competition evaluation",
                                   "neural_training_ms": ranker.training_ms,
                                   "neural_training_samples": ranker.training_samples,
                                   "neural_validation_samples": ranker.validation_samples,
                                   "neural_validation_top1_accuracy": ranker.validation_accuracy,
                                   "model_sha256": sha256(output / "neural_ranker.npz"),
                                   "solver_source_sha256": sha256(Path(__file__))}, details=details)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "results")
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "data" / "arc_public_training.json")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--timeout", type=float, default=1.0)
    args = parser.parse_args()
    report = run(args.output, args.data, args.seed, args.timeout)
    print(json.dumps(report["summary"], indent=2))
