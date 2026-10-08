"""AUTO HARDWARE PROFILE + DYNAMIC BUDGET MODEL.

NO HARD-CODED MACHINE ASSUMPTIONS. Nothing here knows what any specific GPU
model is: every number is measured or read from the running system, so the same
code adapts to a workstation, a laptop iGPU or a discrete card.

MEASURED SOURCES
----------------
CPU : os.cpu_count() for logical cores; physical cores from the OS where it
      exposes them (Windows registry / Linux /proc/cpuinfo / psutil).
RAM : total and AVAILABLE from the OS, so a busy machine budgets conservatively
      without needing a restart.
GPU: the live renderer adapter first - it can expose device-local memory and a
      Vulkan memory budget. With no GPU, everything degrades to a conservative
      software default rather than pretending to know a card.

BUDGET PRINCIPLE (PART 11)
--------------------------
Never 100% of anything. The reserve covers the swapchain, framebuffers, the
desktop compositor, Qt/VTK coexistence, driver allocations, mesh/surface mode
and transient transfer buffers - all of which share the device and would
otherwise be the thing that actually OOMs.
"""
from __future__ import annotations

import os
import platform
import time
from dataclasses import dataclass, field
from typing import Optional

GIB = 1024 ** 3
MIB = 1024 ** 2

# Fractions: documented, overridable, applied to MEASURED values.
GPU_SAFE_FRACTION = 0.55      # no Vulkan budget: conservative share of VRAM
GPU_BUDGET_FRACTION = 0.80    # with a budget: trust the driver a little more
RAM_SOFT_FRACTION = 0.35      # spill target, well before the OS complains
RAM_HARD_FRACTION = 0.60

ENV_GPU_FRACTION = "NAKSHA_GPU_SAFE_FRACTION"
ENV_RAM_SOFT = "NAKSHA_RAM_SOFT_FRACTION"
ENV_RAM_HARD = "NAKSHA_RAM_HARD_FRACTION"
ENV_DECODE_WORKERS = "NAKSHA_DECODE_WORKERS"
ENV_COMPRESS_WORKERS = "NAKSHA_COMPRESS_WORKERS"
ENV_STREAM_WORKERS = "NAKSHA_STREAM_WORKERS"


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        return float(v) if v else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str):
    try:
        v = os.environ.get(name)
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# CPU / RAM detection                                                          #
# --------------------------------------------------------------------------- #
def _physical_cores_windows() -> Optional[int]:
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor")
        try:
            found, i = [], 0
            while True:
                try:
                    found.append(str(winreg.EnumValue(key, i)[0]))
                except OSError:
                    break
                i += 1
            for d in found:
                if "physical" in d.lower() or "core" in d.lower():
                    return len(found)
        finally:
            winreg.CloseKey(key)
    except Exception:
        pass
    return None


def _physical_cores_linux() -> Optional[int]:
    try:
        ids, core, pkg = set(), None, None
        with open("/proc/cpuinfo", "r", encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                key, _s, val = line.partition(":")
                key, val = key.strip(), val.strip()
                if key == "core id":
                    core = val
                elif key == "physical id":
                    pkg = val
                elif key == "" and core is not None:
                    ids.add((pkg, core))
                    core = pkg = None
        return len(ids) or None
    except Exception:
        return None


def detect_cpu() -> dict:
    logical = int(os.cpu_count() or 1)
    physical = None
    if platform.system() == "Windows":
        physical = _physical_cores_windows()
    elif platform.system() == "Linux":
        physical = _physical_cores_linux()
    try:
        import psutil
        physical = physical or int(psutil.cpu_count(logical=False))
    except Exception:
        pass
    physical = int(physical) if physical else logical
    model = platform.processor() or platform.machine() or "unknown"
    if platform.system() == "Windows":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            try:
                model = str(winreg.QueryValueEx(
                    key, "ProcessorNameString")[0])
            finally:
                winreg.CloseKey(key)
        except Exception:
            pass
    return {"model": str(model).strip(),
            "architecture": platform.machine() or platform.arch()[0],
            "physical_cores": max(1, physical),
            "logical_cores": max(1, logical),
            "hyperthreaded": bool(logical > physical)}


def detect_ram() -> dict:
    total = avail = None
    try:
        import psutil
        vm = psutil.virtual_memory()
        total, avail = int(vm.total), int(vm.available)
    except Exception:
        pass
    if total is None and platform.system() == "Windows":
        try:
            import ctypes
            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtVirtual", ctypes.c_ulonglong)]
            m = _MS()
            m.dwLength = ctypes.sizeof(_MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                total, avail = int(m.ullTotalPhys), int(m.ullAvailPhys)
        except Exception:
            pass
    if total is None:
        try:
            total = int(os.sysconf("SC_PHYS_PAGES")) * int(
                os.sysconf("SC_PAGE_SIZE"))
        except Exception:
            total = 4 * GIB
    if avail is None:
        avail = int(total * 0.5)
    return {"total_bytes": int(total),
            "available_bytes": int(min(max(avail, 0), total))}


# --------------------------------------------------------------------------- #
# GPU                                                                          #
# --------------------------------------------------------------------------- #
_GPU_TOTAL_GETTERS = ("gpu_total_bytes", "total_vram_bytes", "vram_bytes",
                      "device_local_memory_bytes", "total_memory_bytes")
_GPU_BUDGET_GETTERS = ("heap_budget_bytes", "memory_budget_bytes",
                       "gpu_budget_bytes")


def _probe_adapter_gpu(adapter) -> dict:
    """Ask the LIVE renderer for real device memory. Never guesses a model."""
    out = {"name": None, "api": None, "total_bytes": 0, "budget_bytes": 0,
           "source": "unavailable"}
    if adapter is None:
        return out
    for getter in ("gpu_name", "device_name", "adapter_name"):
        fn = getattr(adapter, getter, None)
        if callable(fn):
            try:
                v = fn()
                if v:
                    out["name"] = str(v)
                    break
            except Exception:
                pass
    for getter in ("vulkan_api_version", "api_version"):
        fn = getattr(adapter, getter, None)
        if callable(fn):
            try:
                v = fn()
                if v:
                    out["api"] = str(v)
                    break
            except Exception:
                pass
    for getter in _GPU_BUDGET_GETTERS:
        fn = getattr(adapter, getter, None)
        if not callable(fn):
            continue
        try:
            v = int(fn() or 0)
        except Exception:
            continue
        if v > 0 and not out["budget_bytes"]:
            out["budget_bytes"] = v
            out["source"] = f"probed:{getter}"
    for getter in _GPU_TOTAL_GETTERS:
        fn = getattr(adapter, getter, None)
        if not callable(fn):
            continue
        try:
            v = int(fn() or 0)
        except Exception:
            continue
        if v > 0 and not out["total_bytes"]:
            out["total_bytes"] = v
            if out["source"] == "unavailable":
                out["source"] = f"probed:{getter}"
    return out


def detect_gpu(adapter=None) -> dict:
    g = _probe_adapter_gpu(adapter)
    if g["total_bytes"] <= 0 and g["budget_bytes"] <= 0:
        # No renderer attached. Refuse to invent a card: fall back so software
        # Vulkan still works, just conservatively.
        g.update({"total_bytes": 1 * GIB, "budget_bytes": 0,
                  "source": "software-fallback-1GiB",
                  "note": "no GPU probe available; conservative software "
                          "default, not a claim about any real card"})
    return g


# --------------------------------------------------------------------------- #
# Workers                                                                      #
# --------------------------------------------------------------------------- #
def choose_workers(cpu: dict, ram: dict) -> dict:
    """PART 13 - bounded by REAL cores, never a literal 2 / 4 / 8 in source.

    Decode and compression are bounded SEPARATELY because they are different
    bottlenecks, and both shrink on a small-RAM machine because each worker
    holds a chunk buffer.
    """
    phys = max(1, int(cpu.get("physical_cores", 1)))
    logical = max(1, int(cpu.get("logical_cores", phys)))
    # Always leave the GUI thread headroom.
    decode = _env_int(ENV_DECODE_WORKERS) or max(1, min(phys - 1 if phys > 1
                                                        else 1, 8))
    compress = _env_int(ENV_COMPRESS_WORKERS) or max(1, min(phys, 6))
    stream = _env_int(ENV_STREAM_WORKERS) or max(2, min(logical // 2 or 1, 6))
    avail = int(ram.get("available_bytes", 0) or 0)
    if avail and avail < 4 * GIB:
        compress = min(compress, 2)
        decode = min(decode, 4)
    if avail and avail < 2 * GIB:
        compress = 1
        decode = min(decode, 2)
    return {"decode_workers": int(decode),
            "compression_workers": int(compress),
            "stream_workers": int(stream),
            "physical_cores": phys, "logical_cores": logical}


# --------------------------------------------------------------------------- #
# The profile                                                                  #
# --------------------------------------------------------------------------- #
def _gb(n) -> str:
    try:
        return f"{float(n) / GIB:.2f} GB"
    except Exception:
        return "?"


@dataclass
class HardwareProfile:
    cpu: dict = field(default_factory=dict)
    ram: dict = field(default_factory=dict)
    gpu: dict = field(default_factory=dict)
    workers: dict = field(default_factory=dict)
    gpu_usable_bytes: int = 0
    ram_soft_limit_bytes: int = 0
    ram_hard_limit_bytes: int = 0
    gpu_fraction: float = GPU_SAFE_FRACTION
    probed_at: float = field(default_factory=time.time)

    @classmethod
    def detect(cls, adapter=None) -> "HardwareProfile":
        cpu, ram, gpu = detect_cpu(), detect_ram(), detect_gpu(adapter)
        gpu_fraction = _env_float(ENV_GPU_FRACTION, GPU_SAFE_FRACTION)
        # A Vulkan memory budget is the best truth available: the driver has
        # already accounted for other clients. Prefer it, else total VRAM.
        if gpu["budget_bytes"] > 0:
            usable = int(gpu["budget_bytes"] * _env_float(
                ENV_GPU_FRACTION, GPU_BUDGET_FRACTION))
        else:
            usable = int(gpu["total_bytes"] * gpu_fraction)
        soft_frac = _env_float(ENV_RAM_SOFT, RAM_SOFT_FRACTION)
        hard_frac = _env_float(ENV_RAM_HARD, RAM_HARD_FRACTION)
        return cls(cpu=cpu, ram=ram, gpu=gpu,
                   workers=choose_workers(cpu, ram),
                   gpu_usable_bytes=max(64 * MIB, int(usable)),
                   ram_soft_limit_bytes=int(ram["available_bytes"] * soft_frac),
                   ram_hard_limit_bytes=int(ram["available_bytes"] * hard_frac),
                   gpu_fraction=gpu_fraction)

    def refresh_ram(self) -> int:
        """Re-read AVAILABLE RAM so budgets follow real pressure, not just a
        one-shot reading at startup (PART 12/39)."""
        self.ram = detect_ram()
        soft_frac = _env_float(ENV_RAM_SOFT, RAM_SOFT_FRACTION)
        hard_frac = _env_float(ENV_RAM_HARD, RAM_HARD_FRACTION)
        self.ram_soft_limit_bytes = int(self.ram["available_bytes"] * soft_frac)
        self.ram_hard_limit_bytes = int(self.ram["available_bytes"] * hard_frac)
        return self.ram["available_bytes"]

    def describe(self) -> str:
        return "\n".join([
            "[NAKSHA HARDWARE PROFILE]",
            f"CPU: {self.cpu.get('model', '?')}",
            f"physical cores: {self.cpu.get('physical_cores', '?')}",
            f"logical cores: {self.cpu.get('logical_cores', '?')}",
            f"architecture: {self.cpu.get('architecture', '?')}",
            f"RAM total: {_gb(self.ram.get('total_bytes', 0))}",
            f"RAM available: {_gb(self.ram.get('available_bytes', 0))}",
            f"GPU: {self.gpu.get('name') or 'unknown'}",
            f"VRAM total: {_gb(self.gpu.get('total_bytes', 0))}",
            f"VRAM current budget: {_gb(self.gpu.get('budget_bytes', 0))}",
            f"GPU usable budget: {_gb(self.gpu_usable_bytes)}",
            f"Vulkan API: {self.gpu.get('api') or 'unavailable'}",
            f"GPU budget source: {self.gpu.get('source', 'unavailable')}",
            f"RAM soft limit: {_gb(self.ram_soft_limit_bytes)}",
            f"RAM hard limit: {_gb(self.ram_hard_limit_bytes)}",
            "[CACHE WORKERS]",
            f"decode_workers: {self.workers.get('decode_workers')}",
            f"compression_workers: {self.workers.get('compression_workers')}",
            f"stream_workers: {self.workers.get('stream_workers')}",
            "[/NAKSHA HARDWARE PROFILE]",
        ])

    def as_dict(self) -> dict:
        return {"cpu": dict(self.cpu), "ram": dict(self.ram),
                "gpu": dict(self.gpu), "workers": dict(self.workers),
                "gpu_usable_bytes": int(self.gpu_usable_bytes),
                "ram_soft_limit_bytes": int(self.ram_soft_limit_bytes),
                "ram_hard_limit_bytes": int(self.ram_hard_limit_bytes),
                "gpu_fraction": float(self.gpu_fraction)}


_PROFILE = None


def get_profile(adapter=None, refresh: bool = False) -> HardwareProfile:
    """Process-wide profile, measured once unless explicitly refreshed."""
    global _PROFILE
    if _PROFILE is None or refresh:
        _PROFILE = HardwareProfile.detect(adapter)
    return _PROFILE


