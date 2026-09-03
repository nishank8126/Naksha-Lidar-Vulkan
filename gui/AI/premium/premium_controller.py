from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

# NAKSHA_GPU_PHASE11_V3_20_PREMIUM IMPORTS
from gui.AI.common.gpu_engine.runtime import (
    gpu_report_lines as naksha_gpu_report_lines,
    gpu_runtime_report as naksha_gpu_runtime_report,
    premium_device_arg as naksha_premium_device_arg,
)


ProgressCallback = Callable[[int, str], None]


class PremiumAIController:
    """Launch the frozen Premium V4.2 classifier as an isolated Python process.

    The GUI does not reimplement feature extraction or model inference.  It runs
    the same frozen V4.2 program that was validated outside the GUI.
    """

    # Frozen strict-exact production contract.
    EXPECTED_SHA256 = {
        "engine": "29b190f2c5d629f3fa9b6d21d0f34e78842a9345116850b82317d86425884095",
        "v33": "45b107b59f014992449dd815b7baef4dd66fecd654817cf7170ecc2569b54ed9",
        "phase15_worker": "4d456982f856900f4a84f02c247d8f1acc4f9d6c201effa31f0c5e020b868750",
        "scripted_fps": "a25b39f7c8c371bf76edd33b8e366c7681575b1c018bdc6176e48369abe6f0c4",
        "model": "ed36e6eba1802fa7a50c69792e2d212c522bdac3e2cb42ac4e8d3991945fbcbe",
        "stats": "c395874c3dcb7a1f4bb319f882e0d52295a04b40657632803690d6054f317e6f",
        "model_py": "8e76e094c5225cddba588affbc5edccb26db2ca17786bf3892356c0a6e61ff91",
        "hybrid": "ff3d380f6c96d4cdc880f1fb02b31352ef5c342c3fd692e68c568440e51ffd17",
        "dtm": "2d2a461331654768d2a5024bfe24369b6020d3aa6ca87d426ac359ed68061a9e",
    }

    def __init__(self, device=None):
        self.premium_dir = Path(__file__).resolve().parent
        self.engine_dir = self.premium_dir / "engine"
        self.model_dir = self.premium_dir / "models"

        self.engine_path = self.engine_dir / "premium_best_guarded_inference_v4_2.py"
        self.v33_path = self.engine_dir / "premium_best_guarded_inference_v3_3_accuracy_final.py"
        self.phase15_worker_path = self.engine_dir / "v4_phase15_persistent_worker.py"
        self.scripted_fps_path = self.engine_dir / "v4_phase10_scripted_fps.py"
        self.model_py_path = self.engine_dir / "model.py"
        self.hybrid_path = self.engine_dir / "_core_hybrid_v2.py"
        self.dtm_path = self.engine_dir / "_core_dtm_v3.py"

        self.model_path = self.model_dir / "best_guarded.pth"
        self.stats_path = self.model_dir / "feature_stats.json"

        # ai_dialog currently constructs the controller without an explicit device.
        # Keep support for an optional torch.device/string while using the engine's
        # normal auto-selection by default.
        # NAKSHA_GPU_PHASE11_V3_20_PREMIUM DEVICE BEGIN
        self.gpu_phase11_report = naksha_gpu_runtime_report(prefer_cuda=True)
        if device is None:
            self.device_arg = naksha_premium_device_arg()
            for _naksha_line in naksha_gpu_report_lines(
                self.gpu_phase11_report, prefix="Premium GPU Phase11"
            ):
                print(f"[Premium GPU Phase11] {_naksha_line}", flush=True)
        # NAKSHA_GPU_PHASE11_V3_20_PREMIUM DEVICE END
        else:
            value = str(device).lower()
            if "cuda" in value:
                self.device_arg = "cuda"
            elif "cpu" in value:
                self.device_arg = "cpu"
            else:
                self.device_arg = "auto"

        self._process: Optional[subprocess.Popen] = None
        self._process_lock = threading.Lock()
        self._cancel_requested = False

        self.validate_package()

    @staticmethod
    def _sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
        h = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(chunk_size), b""):
                h.update(chunk)
        return h.hexdigest().lower()

    def validate_package(self) -> dict[str, str]:
        paths = {
            "engine": self.engine_path,
            "v33": self.v33_path,
            "phase15_worker": self.phase15_worker_path,
            "scripted_fps": self.scripted_fps_path,
            "model": self.model_path,
            "stats": self.stats_path,
            "model_py": self.model_py_path,
            "hybrid": self.hybrid_path,
            "dtm": self.dtm_path,
        }

        hashes: dict[str, str] = {}
        for key, path in paths.items():
            if not path.is_file():
                raise FileNotFoundError(f"Premium V4.2 required file missing: {path}")
            actual = self._sha256(path)
            expected = self.EXPECTED_SHA256[key]
            hashes[key] = actual
            if actual != expected:
                raise RuntimeError(
                    f"Premium V4.2 frozen-file mismatch for {key}:\n"
                    f"{path}\nexpected: {expected}\nactual:   {actual}\n"
                    "Do not mix Premium model/engine versions."
                )
        return hashes

    @staticmethod
    def _emit(callback: Optional[ProgressCallback], percent: int, message: str) -> None:
        if callback is None:
            return
        try:
            callback(int(max(0, min(100, percent))), str(message))
        except Exception:
            pass

    @staticmethod
    def _progress_from_line(line: str, last_percent: int) -> int:
        text = line.strip()
        if "[1/5]" in text:
            return max(last_percent, 8)
        if "[2/5]" in text:
            return max(last_percent, 18)
        if "[3/5]" in text:
            return max(last_percent, 28)
        if "[4/5]" in text:
            return max(last_percent, 88)
        if "[5/5]" in text:
            return max(last_percent, 95)

        match = re.search(r"Tile\s+(\d+)\s*/\s*(\d+)", text, flags=re.IGNORECASE)
        if match:
            done = int(match.group(1))
            total = max(int(match.group(2)), 1)
            # Stage 3 is the dominant part of the run.
            return max(last_percent, 28 + int(58.0 * min(done / total, 1.0)))

        if text.startswith("Classification result:"):
            return max(last_percent, 98)
        if text.startswith("Elapsed:"):
            return max(last_percent, 99)
        return last_percent

    def _build_command(self, input_path: Path, output_path: Path) -> list[str]:
        # These are the frozen V4.2/V3.3 exact settings used by the validated path.
        return [
            sys.executable,
            str(self.engine_path),
            "--input", str(input_path),
            "--output", str(output_path),
            "--model", str(self.model_path),
            "--stats", str(self.stats_path),
            "--device", self.device_arg,
            "--vote-passes", "2",
            "--batch-size", "1",
            "--workers", "0",
            "--feature-block", "28000",
            "--pca-batch-size", "512",
            "--pca-engine", "reference",
            "--io-chunk", "1000000",
            "--ground-uncat-threshold", "0.70",
            "--ground-uncat-max-abs-hag", "0.25",
            "--ground-uncat-max-above-local-p10", "0.25",
            "--qc-batch-size", "5000",
        ]

    @staticmethod
    def _cleanup_previous_run(output_path: Path) -> None:
        try:
            if output_path.exists():
                output_path.unlink()
        except Exception:
            pass

        report = output_path.with_suffix(
            output_path.suffix + ".premium_v4_2_persistent_exact_report.json"
        )
        try:
            if report.exists():
                report.unlink()
        except Exception:
            pass

        # V3.3/V4.2 mmap work-directory convention.
        work_dir = output_path.parent / (output_path.name + ".v321qc_work")
        shutil.rmtree(work_dir, ignore_errors=True)

    def run(
        self,
        input_path,
        output_path,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Path:
        self.validate_package()

        input_path = Path(input_path).resolve()
        output_path = Path(output_path).resolve()
        if not input_path.is_file():
            raise FileNotFoundError(f"Premium input LAS/LAZ not found: {input_path}")
        if input_path.suffix.lower() not in {".las", ".laz"}:
            raise ValueError(f"Premium input must be LAS/LAZ: {input_path}")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._cleanup_previous_run(output_path)

        self._cancel_requested = False
        self._emit(progress_callback, 5, "Premium V4.2 package verified. Starting exact engine...")

        cmd = self._build_command(input_path, output_path)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        # NAKSHA_GPU_PHASE11_V3_20_PREMIUM SUBPROCESS ENV
        env["NAKSHA_GPU_PHASE11"] = "1"
        if self.device_arg == "cuda":
            env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        process = subprocess.Popen(
            cmd,
            cwd=str(self.engine_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=creationflags,
        )

        with self._process_lock:
            self._process = process

        last_percent = 5
        tail: list[str] = []
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                if line:
                    print(f"[Premium V4.2] {line}", flush=True)
                    tail.append(line)
                    if len(tail) > 80:
                        tail.pop(0)
                    last_percent = self._progress_from_line(line, last_percent)
                    self._emit(progress_callback, last_percent, line)

                if self._cancel_requested:
                    self.cancel()
                    break

            return_code = process.wait()
        finally:
            with self._process_lock:
                self._process = None

        if self._cancel_requested:
            raise RuntimeError("Premium AI classification cancelled by user.")

        if return_code != 0:
            details = "\n".join(tail[-30:])
            raise RuntimeError(
                f"Premium V4.2 engine exited with code {return_code}.\n\n{details}"
            )

        if not output_path.is_file():
            raise RuntimeError(
                f"Premium V4.2 reported success but output was not created: {output_path}"
            )

        self._emit(progress_callback, 99, "Premium V4.2 engine completed. Updating GUI...")
        return output_path

    def cancel(self) -> None:
        self._cancel_requested = True
        with self._process_lock:
            process = self._process

        if process is None or process.poll() is not None:
            return

        try:
            if os.name == "nt":
                # Kill the V4.2 process and its spawned PCA worker processes.
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                process.terminate()
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def load_premium_controller(device=None) -> PremiumAIController:
    """Backward-compatible entry point used by older GUI code."""
    return PremiumAIController(device=device)
