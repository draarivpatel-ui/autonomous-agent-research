"""Supplemental local Metal GPU kernel. This is not a Triton/FlagOS measurement."""
import mlx.core as mx

SOURCE = """
    uint row = threadgroup_position_in_grid.x;
    uint tid = thread_position_in_threadgroup.x;
    threadgroup float partial[256];
    float sum = 0.0f;
    for (uint column = tid; column < D; column += 256) {
        float value = x[row * D + column];
        sum += value * value;
    }
    partial[tid] = sum;
    threadgroup_barrier(mem_flags::mem_threadgroup);
    for (uint stride = 128; stride > 0; stride >>= 1) {
        if (tid < stride) partial[tid] += partial[tid + stride];
        threadgroup_barrier(mem_flags::mem_threadgroup);
    }
    float inverse = metal::rsqrt(partial[0] / float(D) + 1e-6f);
    for (uint column = tid; column < D; column += 256) {
        y[row * D + column] = x[row * D + column] * inverse * w[column];
    }
"""
KERNEL = mx.fast.metal_kernel(name="campaign_rmsnorm", input_names=["x", "w"],
                             output_names=["y"], source=SOURCE)


def rmsnorm(x, weight):
    if x.ndim != 2 or x.shape[0] < 1 or not 1 <= x.shape[1] <= 65536 or weight.shape != (x.shape[1],):
        raise ValueError("Require a nonempty matrix and matching weights")
    if x.dtype != mx.float32 or weight.dtype != mx.float32:
        raise ValueError("The evaluated Metal kernel supports float32 only")
    return KERNEL(inputs=[x, weight], template=[("D", x.shape[1])],
                  grid=(x.shape[0] * 256, 1, 1), threadgroup=(256, 1, 1),
                  output_shapes=[x.shape], output_dtypes=[mx.float32])[0]
