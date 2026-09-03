from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, Optional, Tuple

NAKSHA_GPU_PHASE11_VERSION = "NAKSHA_GPU_ACCELERATION_PHASE11_V3_20"


@dataclass(frozen=True)
class GPURuntimeInfo:
    version: str
    torch_version: str
    torch_cuda_build: Optional[str]
    cuda_available: bool
    selected_device: str
    gpu_name: Optional[str]
    vram_gb: float
    compute_capability: Optional[str]
    nvidia_driver_visible: bool
    cuda_visible_devices: Optional[str]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _env_true(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _nvidia_driver_visible() -> bool:
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=4,
            check=False,
        )
        return completed.returncode == 0 and bool(completed.stdout.strip())
    except Exception:
        return False


def gpu_runtime_report(prefer_cuda: bool = True) -> Dict[str, Any]:
    """Inspect the *current Python environment* and report GPU readiness.

    The function never raises for a missing/CPU-only PyTorch build. This is
    intentional: Naksha AI must always retain a safe CPU fallback.
    """
    try:
        import torch
    except Exception as exc:
        return GPURuntimeInfo(
            version=NAKSHA_GPU_PHASE11_VERSION,
            torch_version="unavailable",
            torch_cuda_build=None,
            cuda_available=False,
            selected_device="cpu",
            gpu_name=None,
            vram_gb=0.0,
            compute_capability=None,
            nvidia_driver_visible=_nvidia_driver_visible(),
            cuda_visible_devices=os.environ.get("CUDA_VISIBLE_DEVICES"),
            reason=f"torch_import_failed:{type(exc).__name__}",
        ).to_dict()

    force_cpu = _env_true("NAKSHA_GPU_FORCE_CPU", False)
    cuda_available = bool(torch.cuda.is_available()) and not force_cpu
    selected = "cuda:0" if prefer_cuda and cuda_available else "cpu"
    gpu_name = None
    vram_gb = 0.0
    capability = None

    if cuda_available:
        try:
            props = torch.cuda.get_device_properties(0)
            gpu_name = str(props.name)
            vram_gb = float(props.total_memory) / (1024.0 ** 3)
            cc = torch.cuda.get_device_capability(0)
            capability = f"{int(cc[0])}.{int(cc[1])}"
        except Exception:
            pass

    driver_visible = _nvidia_driver_visible()
    torch_cuda_build = getattr(torch.version, "cuda", None)

    if force_cpu:
        reason = "forced_cpu_by_NAKSHA_GPU_FORCE_CPU"
    elif cuda_available:
        reason = "cuda_ready"
    elif driver_visible and not torch_cuda_build:
        reason = "nvidia_gpu_visible_but_pytorch_is_cpu_only"
    elif driver_visible:
        reason = "nvidia_gpu_visible_but_torch_cuda_unavailable"
    else:
        reason = "no_cuda_runtime_visible_to_current_python"

    return GPURuntimeInfo(
        version=NAKSHA_GPU_PHASE11_VERSION,
        torch_version=str(getattr(torch, "__version__", "unknown")),
        torch_cuda_build=None if torch_cuda_build is None else str(torch_cuda_build),
        cuda_available=bool(cuda_available),
        selected_device=selected,
        gpu_name=gpu_name,
        vram_gb=float(vram_gb),
        compute_capability=capability,
        nvidia_driver_visible=bool(driver_visible),
        cuda_visible_devices=os.environ.get("CUDA_VISIBLE_DEVICES"),
        reason=reason,
    ).to_dict()


def select_torch_device(prefer_cuda: bool = True):
    """Return (torch.device, report) with a guaranteed CPU fallback."""
    import torch

    report = gpu_runtime_report(prefer_cuda=prefer_cuda)
    return torch.device(report["selected_device"]), report


def configure_torch_for_accuracy(device) -> None:
    """Configure CUDA for parity-first inference.

    Phase 11 intentionally does *not* change model weights, features, vote
    rules, or class thresholds. TF32 and automatic mixed precision are off by
    default so the first GPU-vs-CPU acceptance test is as close as practical.
    """
    try:
        import torch
    except Exception:
        return

    try:
        if hasattr(torch, "set_float32_matmul_precision"):
            torch.set_float32_matmul_precision("highest")
    except Exception:
        pass

    if getattr(device, "type", str(device).split(":", 1)[0]) != "cuda":
        return

    try:
        torch.backends.cuda.matmul.allow_tf32 = False
    except Exception:
        pass
    try:
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cudnn.benchmark = False
    except Exception:
        pass


def should_use_amp(device) -> bool:
    """AMP is opt-in in Phase 11 to protect classification parity."""
    device_type = getattr(device, "type", str(device).split(":", 1)[0])
    return bool(device_type == "cuda" and _env_true("NAKSHA_GPU_USE_AMP", False))


def recommended_advanced_batch_size(device, default: int = 8) -> int:
    """VRAM-aware tile batch size for the 8192-point Advanced model.

    The NVIDIA T400 4 GB is deliberately conservative at batch 2. Larger GPUs
    scale automatically. CPU retains the existing batch setting.
    """
    device_type = getattr(device, "type", str(device).split(":", 1)[0])
    if device_type != "cuda":
        return int(max(1, default))

    try:
        import torch
        vram_gb = float(torch.cuda.get_device_properties(device).total_memory) / (1024.0 ** 3)
    except Exception:
        return min(int(max(1, default)), 2)

    override = os.environ.get("NAKSHA_ADVANCED_GPU_BATCH")
    if override:
        try:
            return max(1, min(16, int(override)))
        except Exception:
            pass

    if vram_gb <= 4.75:
        return min(max(1, int(default)), 2)
    if vram_gb <= 8.5:
        return min(max(1, int(default)), 4)
    if vram_gb <= 16.5:
        return min(max(1, int(default)), 8)
    return min(max(1, int(default)), 12)


def premium_device_arg() -> str:
    """Device argument for the frozen Premium engine without modifying it."""
    report = gpu_runtime_report(prefer_cuda=True)
    return "cuda" if report.get("cuda_available") else "auto"


def gpu_report_lines(report: Dict[str, Any], prefix: str = "GPU Phase11") -> Iterable[str]:
    """Stable log lines shared by Advanced/Premium diagnostics."""
    yield f"{prefix} version          : {report.get('version')}"
    yield f"{prefix} selected device  : {report.get('selected_device')}"
    yield f"{prefix} torch             : {report.get('torch_version')}"
    yield f"{prefix} torch CUDA build  : {report.get('torch_cuda_build')}"
    yield f"{prefix} CUDA available    : {report.get('cuda_available')}"
    if report.get("gpu_name"):
        yield f"{prefix} GPU               : {report.get('gpu_name')}"
        yield f"{prefix} VRAM              : {float(report.get('vram_gb', 0.0)):.2f} GB"
        yield f"{prefix} capability        : {report.get('compute_capability')}"
    yield f"{prefix} NVIDIA driver     : {report.get('nvidia_driver_visible')}"
    yield f"{prefix} reason            : {report.get('reason')}"
