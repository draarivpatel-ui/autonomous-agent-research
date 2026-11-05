"""Optional CUDA/HIP Triton RMSNorm. This module requires a supported GPU host."""
import torch
import triton
import triton.language as tl
import math


@triton.jit
def _rmsnorm(X, W, Y, N: tl.constexpr, EPS: tl.constexpr, BLOCK: tl.constexpr):
    row = tl.program_id(0)
    columns = tl.arange(0, BLOCK)
    values = tl.load(X + row * N + columns, columns < N, other=0).to(tl.float32)
    weight = tl.load(W + columns, columns < N, other=0).to(tl.float32)
    inverse = tl.rsqrt(tl.sum(values * values, axis=0) / N + EPS)
    tl.store(Y + row * N + columns, values * inverse * weight, columns < N)


def rmsnorm(x, weight, eps=1e-6, num_warps=4):
    if not x.is_cuda or x.ndim != 2 or not x.is_contiguous() or x.shape[0] < 1 or not 1 <= x.shape[1] <= 65536:
        raise ValueError("Require a contiguous nonempty 2-D CUDA/HIP tensor")
    if weight.shape != (x.shape[1],) or not weight.is_contiguous() or weight.device != x.device:
        raise ValueError("Weight shape and device must match the feature axis")
    if x.dtype != torch.float32 or weight.dtype != torch.float32:
        raise ValueError("The supplied evaluated contract is float32 only")
    if not math.isfinite(eps) or eps <= 0 or num_warps not in (1, 2, 4, 8):
        raise ValueError("Require positive epsilon and a supported warp count")
    out = torch.empty_like(x)
    _rmsnorm[(x.shape[0],)](x, weight, out, x.shape[1], eps,
                          BLOCK=triton.next_power_of_2(x.shape[1]), num_warps=num_warps)
    return out
