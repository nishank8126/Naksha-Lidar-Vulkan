"""Naksha AI Phase 11 GPU runtime helpers.

This package accelerates model inference without changing model weights,
feature definitions, PTC mapping, or semantic/post-processing decisions.
"""

from .runtime import (
    NAKSHA_GPU_PHASE11_VERSION,
    configure_torch_for_accuracy,
    gpu_runtime_report,
    gpu_report_lines,
    premium_device_arg,
    recommended_advanced_batch_size,
    select_torch_device,
    should_use_amp,
)

__all__ = [
    "NAKSHA_GPU_PHASE11_VERSION",
    "configure_torch_for_accuracy",
    "gpu_runtime_report",
    "gpu_report_lines",
    "premium_device_arg",
    "recommended_advanced_batch_size",
    "select_torch_device",
    "should_use_amp",
]
