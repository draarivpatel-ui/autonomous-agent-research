"""Measured CPU RMSNorm optimization and optional real Triton GPU measurements.

CPU timings are never labelled as Triton or FlagOS accelerator performance.
Use --backend gpu to fail explicitly when a CUDA/HIP device is unavailable.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
import platform
import importlib.metadata
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_common import latency, sha256, write_results, load_module


def validate(x, weight, eps):
    if x.ndim != 2 or x.shape[0] < 1 or x.shape[1] < 1 or weight.shape != (x.shape[1],):
        raise ValueError("Require a nonempty matrix and matching feature weights")
    if not np.isfinite(eps) or eps <= 0 or not np.isfinite(x).all() or not np.isfinite(weight).all():
        raise ValueError("Require finite inputs and positive epsilon")


def reference(x, weight, eps=1e-6):
    variance = np.mean(x * x, axis=1, keepdims=True)
    return (x / np.sqrt(variance + eps)) * weight


def fused_cpu(x, weight, eps=1e-6):
    # A C-level dot reduction avoids materializing the full squared matrix.
    variance = np.einsum("ij,ij->i", x, x, optimize=False) / x.shape[1]
    inverse = 1.0 / np.sqrt(variance + eps)
    result = np.empty_like(x)
    np.multiply(x, inverse[:, None], out=result)
    np.multiply(result, weight, out=result)
    return result


def measure(fn, warmup=10, repeats=50):
    for _ in range(warmup): fn()
    values = []
    for _ in range(repeats):
        begin = time.perf_counter_ns(); fn()
        values.append((time.perf_counter_ns() - begin) / 1e6)
    return latency(values)


def gpu_capability():
    if importlib.util.find_spec("torch") is None or importlib.util.find_spec("triton") is None:
        return {"available": False, "reason": "Optional torch and/or Triton are not installed"}
    import torch
    if not torch.cuda.is_available():
        return {"available": False, "reason": "No CUDA or HIP device exposed by PyTorch"}
    return {"available": True, "backend": "HIP" if torch.version.hip else "CUDA",
            "device": torch.cuda.get_device_name(0), "torch": torch.__version__}


def metal_capability():
    if platform.system() != "Darwin" or importlib.util.find_spec("mlx") is None:
        return {"available": False, "reason": "Supplemental MLX/Metal backend not available"}
    import mlx.core as mx
    if not mx.metal.is_available():
        return {"available": False, "reason": "MLX does not expose a Metal device"}
    return {"available": True, "backend": "Metal", "mlx": importlib.metadata.version("mlx")}


def metal_rows(seed, repeats):
    import mlx.core as mx
    rmsnorm = load_module("campaign_mlx_kernel", Path(__file__).with_name("mlx_rmsnorm.py")).rmsnorm
    rng = np.random.default_rng(seed)
    rows = []
    for n, d in ((32, 128), (512, 1024), (1024, 4096), (33, 513)):
        source = rng.normal(size=(n, d)).astype(np.float32)
        weights = rng.normal(size=d).astype(np.float32)
        x, weight = mx.array(source), mx.array(weights)
        mx.eval(x, weight)
        expected = reference(source, weights)
        def baseline(): return x * mx.rsqrt(mx.mean(x * x, axis=-1, keepdims=True) + 1e-6) * weight
        actual = rmsnorm(x, weight)
        mx.eval(actual)
        np.testing.assert_allclose(np.asarray(actual), expected, rtol=2e-5, atol=2e-5)
        error = float(np.max(np.abs(np.asarray(actual) - expected)))
        for name, fn in (("mlx_expression", baseline), ("metal_fused", lambda: rmsnorm(x, weight))):
            def synchronous():
                out = fn()
                mx.eval(out)
                mx.synchronize()
            timing = measure(synchronous, repeats=repeats)
            rows.append({"backend": "metal", "implementation": name, "rows": n, "features": d,
                         "dtype": "float32", "max_absolute_error": error, "num_warps": 0, **timing})
    return rows


def gpu_rows(seed, repeats):
    import torch
    import triton.testing
    rmsnorm = load_module("campaign_triton_kernel", Path(__file__).with_name("triton_rmsnorm.py")).rmsnorm
    torch.manual_seed(seed)
    rows = []
    for n, d in ((32, 128), (512, 1024), (1024, 4096), (33, 513)):
        x = torch.randn((n, d), device="cuda", dtype=torch.float32)
        weight = torch.randn((d,), device="cuda", dtype=torch.float32)
        def baseline():
            xf = x.float()
            return xf * torch.rsqrt((xf * xf).mean(-1, keepdim=True) + 1e-6) * weight.float()
        expected = baseline()
        # Calibrate warp count on a separate allocation, then evaluate.
        calibration = torch.randn_like(x)
        options = {warps: triton.testing.do_bench(lambda: rmsnorm(calibration, weight, num_warps=warps))
                   for warps in (1, 2, 4, 8)}
        best = min(options, key=options.get)
        actual = rmsnorm(x, weight, num_warps=best)
        torch.testing.assert_close(actual, expected, rtol=2e-5, atol=2e-5)
        for name, fn in (("torch_reference", baseline), ("triton_fused", lambda: rmsnorm(x, weight, num_warps=best))):
            samples = [float(triton.testing.do_bench(fn, warmup=25, rep=100)) for _ in range(5)]
            rows.append({"backend": "gpu", "implementation": name, "rows": n, "features": d,
                         "dtype": "float32", "max_absolute_error": float((actual - expected).abs().max().item()),
                         "num_warps": best if name == "triton_fused" else 0, **latency(samples)})
    return rows


def run(output: Path, seed=2026, repeats=50, backend="auto"):
    if repeats < 5 or backend not in ("cpu", "gpu", "metal", "auto"):
        raise ValueError("Require at least five repeats and a supported backend")
    capability = gpu_capability()
    metal = metal_capability()
    if backend == "gpu" and not capability["available"]:
        raise RuntimeError(capability["reason"])
    if backend == "metal" and not metal["available"]:
        raise RuntimeError(metal["reason"])
    rng = np.random.default_rng(seed)
    rows = []
    if backend in ("cpu", "auto"):
        for n, d in ((32, 128), (512, 1024), (1024, 4096), (33, 513)):
            x = rng.normal(size=(n, d)).astype(np.float32)
            weight = rng.normal(size=d).astype(np.float32)
            validate(x, weight, 1e-6)
            expected, actual = reference(x, weight), fused_cpu(x, weight)
            np.testing.assert_allclose(actual, expected, rtol=2e-5, atol=2e-5)
            error = float(np.max(np.abs(actual - expected)))
            for name, fn in (("numpy_materialized", reference), ("numpy_fused_reduction", fused_cpu)):
                timing = measure(lambda: fn(x, weight), repeats=repeats)
                rows.append({"backend": "cpu", "implementation": name, "rows": n, "features": d,
                             "dtype": "float32", "max_absolute_error": error, "num_warps": 0, **timing})
    if backend in ("gpu", "auto") and capability["available"]:
        rows.extend(gpu_rows(seed, repeats))
    if backend in ("metal", "auto") and metal["available"]:
        rows.extend(metal_rows(seed, repeats))
    return write_results(output, "RMSNorm kernel optimization", rows,
                         metadata={"seed": seed, "warmup": 10, "cpu_repeats": repeats,
                                   "gpu": capability, "gpu_measurements_collected": capability["available"] and backend in ("gpu", "auto"),
                                   "metal": metal, "metal_measurements_collected": metal["available"] and backend in ("metal", "auto"),
                                   "flagos_target_hardware_validated": False, "tolerance_rtol": 2e-5, "tolerance_atol": 2e-5,
                                   "benchmark_source_sha256": sha256(Path(__file__)),
                                   "triton_kernel_sha256": sha256(Path(__file__).with_name("triton_rmsnorm.py")),
                                   "metal_kernel_sha256": sha256(Path(__file__).with_name("mlx_rmsnorm.py"))})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "results")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--repeats", type=int, default=50)
    parser.add_argument("--backend", choices=("cpu", "gpu", "metal", "auto"), default="auto")
    args = parser.parse_args()
    try: report = run(args.output, args.seed, args.repeats, args.backend)
    except RuntimeError as exc:
        print(json.dumps({"status": "unavailable", "reason": str(exc)})); raise SystemExit(2)
    print(json.dumps(report["summary"], indent=2))
    print(json.dumps(report["metadata"]["gpu"], indent=2))
