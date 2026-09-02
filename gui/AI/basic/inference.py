##### part 333333 #######

#### OM DUM DURGAYE NAMAHA #####
"""
Naksha Basic AI Inference - ISOLATED PRODUCTION VERSION
Power line / pole detection is now optional via `enable_power_lines` flag.
When disabled (default), the pipeline behaves exactly like the original
5-class building/vegetation/ground classifier — preserving building accuracy.
"""

import numpy as np
import torch
import json
import gc
import os
import time
import threading
import importlib.util
import concurrent.futures
from pathlib import Path
from scipy.spatial import cKDTree
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from PySide6.QtCore import QThread, Signal

try:
    import jakteristics
    HAS_JAKTERISTICS = True
except ImportError:
    HAS_JAKTERISTICS = False

try:
    import CSF
    HAS_CSF = True
except ImportError:
    HAS_CSF = False

try:
    import onnxruntime as ort  # type: ignore
    HAS_ONNX = True
except ImportError:
    HAS_ONNX = False
    ort = None


# ═══════════════════════════════════════════════════════════════
# CONFIGURATION  — defaults (overridden per-project via advanced_config)
# ═══════════════════════════════════════════════════════════════

class InferenceConfig:
    TILE_SIZE             = 50.0
    TILE_OVERLAP          = 10.0
    NUM_POINTS            = 8192
    VOTE_PASSES           = 3
    INFERENCE_BATCH_SIZE  = 8

    FEATURE_SCALES        = [0.5, 1.0, 2.0, 5.0, 10.0]
    GEOM_VOXEL_SIZE       = 0.30
    EXPECTED_NUM_FEATURES = 66

    # ── CSF ground defaults ──
    CSF_CLOTH_RESOLUTION  = 0.5
    CSF_RIGIDNESS         = 3
    CSF_CLASS_THRESHOLD   = 0.5
    CSF_TIME_STEP         = 0.65
    CSF_ITERATIONS        = 500

    # ── HAG vegetation boundary defaults ──
    LOWVEG_HAG_MIN        = 0.15
    LOWVEG_HAG_MAX        = 0.50
    MIDVEG_HAG_MAX        = 3.00
    HIGHVEG_HAG_MIN       = 3.00

    # ── Wire detection defaults ──
    WIRE_VERTICALITY_MAX  = 0.15
    WIRE_INTERNAL_CODE    = 5
    POLE_INTERNAL_CODE    = 6
    WIRE_LINEARITY_MIN    = 0.72
    WIRE_PLANARITY_MAX    = 0.25
    WIRE_HAG_MIN          = 3.0
    WIRE_HAG_MAX          = 80.0
    WIRE_DENSITY_MAX      = 12
    WIRE_CHAIN_RADIUS     = 2.5
    WIRE_CHAIN_MIN        = 2
    WIRE_MIN_SEGMENT_PTS  = 50

    POLE_VERTICALITY_MIN  = 0.65
    POLE_HAG_MIN          = 1.5
    POLE_HAG_MAX          = 80.0
    POLE_2D_RADIUS        = 1.5
    POLE_CLUSTER_MIN_PTS  = 8
    POLE_WIRE_PROXIMITY   = 25.0

    RANDOM_SEED           = 42

    GPU_PCA_MAX_VOXELS    = 30_000


# ── Feature column registry ──
_GEOM_FEATURE_NAMES = [
    'eigenvalue1', 'eigenvalue2', 'eigenvalue3',
    'linearity', 'planarity', 'sphericity',
    'omnivariance', 'anisotropy', 'eigenentropy',
    'surface_variation', 'verticality',
]
_N_GEOM_PER_SCALE = len(_GEOM_FEATURE_NAMES)
_GEOM_OFFSET      = 11


def _feat_col(scale_idx: int, feature_name: str) -> int:
    feat_idx = _GEOM_FEATURE_NAMES.index(feature_name)
    return _GEOM_OFFSET + scale_idx * _N_GEOM_PER_SCALE + feat_idx


def _validate_col_registry():
    n_geom = _N_GEOM_PER_SCALE * len(InferenceConfig.FEATURE_SCALES)
    expected_total = _GEOM_OFFSET + n_geom
    assert expected_total == InferenceConfig.EXPECTED_NUM_FEATURES, (
        f"Column registry mismatch: computed {expected_total}, "
        f"config says {InferenceConfig.EXPECTED_NUM_FEATURES}"
    )
    assert _feat_col(0, 'linearity')   == 14
    assert _feat_col(0, 'planarity')   == 15
    assert _feat_col(0, 'verticality') == 21
    assert _feat_col(1, 'linearity')   == 25
    assert _feat_col(1, 'verticality') == 32


_validate_col_registry()

DEFAULT_POWER_MAPPING = {
    InferenceConfig.WIRE_INTERNAL_CODE: 14,
    InferenceConfig.POLE_INTERNAL_CODE: 15,
}

_POST_PROC_COLUMNS = {
    'linearity_s0':   _feat_col(0, 'linearity'),
    'planarity_s0':   _feat_col(0, 'planarity'),
    'verticality_s0': _feat_col(0, 'verticality'),
    'linearity_s1':   _feat_col(1, 'linearity'),
    'verticality_s1': _feat_col(1, 'verticality'),
}
_POST_COL_INDICES = list(_POST_PROC_COLUMNS.values())
_POST_COL_NAMES   = list(_POST_PROC_COLUMNS.keys())

_GPU_K_PER_RADIUS = {0.5: 30, 1.0: 80, 2.0: 250, 5.0: 600, 10.0: 1500}


# ═══════════════════════════════════════════════════════════════
# HELPER
# ═══════════════════════════════════════════════════════════════

def voxel_downsample(xyz, voxel_size):
    shifted = xyz - xyz.min(axis=0)
    vc      = np.floor(shifted / voxel_size).astype(np.int64)
    dims    = vc.max(axis=0) + 1
    max_key = dims[0] * dims[1] * dims[2]
    if max_key > 2**62:
        raise ValueError(f"Voxel grid too large: {dims} -> {max_key}.")
    keys = (vc[:, 0] * dims[1] * dims[2] + vc[:, 1] * dims[2] + vc[:, 2])
    unique_keys, inverse, counts = np.unique(keys, return_inverse=True, return_counts=True)
    n_vox     = len(unique_keys)
    centroids = np.zeros((n_vox, 3), dtype=np.float64)
    np.add.at(centroids, inverse, xyz)
    centroids /= counts[:, None]
    return centroids, inverse, n_vox


# ═══════════════════════════════════════════════════════════════
# ONNX CONVERSION UTILITY
# ═══════════════════════════════════════════════════════════════

def convert_pth_to_onnx(pth_path: str, output_path: str = None) -> str:
    pth_path = Path(pth_path)
    if output_path is None:
        output_path = pth_path.with_suffix('.onnx')
    output_path = Path(output_path)

    device   = torch.device('cpu')
    ckpt     = torch.load(str(pth_path), map_location=device, weights_only=False)
    model_py = pth_path.parent / "model.py"
    if not model_py.exists():
        raise FileNotFoundError(f"model.py not found: {model_py}")

    spec = importlib.util.spec_from_file_location("pointnet2_model", str(model_py))
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    num_features = ckpt.get('num_features', InferenceConfig.EXPECTED_NUM_FEATURES)
    num_classes  = ckpt.get('num_classes', 5)
    model = mod.PointNet2SSG(num_features=num_features, num_classes=num_classes)
    model.load_state_dict(ckpt['model_state_dict'], strict=True)
    model.eval()

    B = InferenceConfig.INFERENCE_BATCH_SIZE
    N = InferenceConfig.NUM_POINTS
    F = num_features
    dummy_coords = torch.zeros(B, N, 3, dtype=torch.float32)
    dummy_feats  = torch.zeros(B, N, F, dtype=torch.float32)

    print(f"Exporting ONNX  batch={B}  points={N}  features={F}")
    with torch.no_grad():
        torch.onnx.export(
            model, (dummy_coords, dummy_feats), str(output_path),
            export_params=True, opset_version=17, do_constant_folding=True,
            input_names=['coords', 'features'], output_names=['logits'],
            dynamic_axes={'coords': {0: 'batch'}, 'features': {0: 'batch'},
                          'logits': {0: 'batch'}},
        )
    print(f"ONNX saved → {output_path}  ({output_path.stat().st_size/1e6:.1f} MB)")
    return str(output_path)


# ═══════════════════════════════════════════════════════════════
# INFERENCE WORKER
# ═══════════════════════════════════════════════════════════════

class InferenceWorker(QThread):
    progress = Signal(int, str)
    finished = Signal()
    error    = Signal(str)

    GROUND   = 0
    LOWVEG   = 1
    MIDVEG   = 2
    HIGHVEG  = 3
    BUILDING = 4

    def __init__(self, data_dict, class_mapping, power_mapping,
             advanced_config=None, enable_power_lines=False,
             target_indices=None):
        """
        Args:
            data_dict           : App data dict with xyz, intensity, etc.
            class_mapping       : Dict {0..4: output_code}
            power_mapping       : Dict {5: wire_code, 6: pole_code}
                                  Used ONLY when enable_power_lines=True.
            advanced_config     : Optional override dict for tunable params.
            enable_power_lines  : If False (default), the power-line / pole
                                  post-pass is skipped entirely. Output will
                                  contain only the 5 base classes — exactly
                                  matching the original reliable pipeline.
                                  Building accuracy is preserved.
        """
        super().__init__()

        full_xyz = np.array(data_dict["xyz"], dtype=np.float64)
        self._source_point_count = len(full_xyz)
        self._target_indices = None
        if target_indices is not None:
            idx = np.asarray(target_indices, dtype=np.int64).ravel()
            if idx.size > 0:
                idx = idx[(idx >= 0) & (idx < self._source_point_count)]
                if idx.size > 0:
                    self._target_indices = np.unique(idx)
        self._is_fence_mode = self._target_indices is not None
        if self._is_fence_mode:
            self._xyz = full_xyz[self._target_indices]
        else:
            self._xyz = full_xyz

        self._intensity = None
        if data_dict.get("intensity") is not None:
            intensity = np.array(data_dict["intensity"], dtype=np.float32)
            if self._is_fence_mode:
                if len(intensity) == self._source_point_count:
                    intensity = intensity[self._target_indices]
                elif len(intensity) != len(self._xyz):
                    intensity = None
            elif len(intensity) != len(self._xyz):
                intensity = None
            self._intensity = intensity

        self._return_number = self._number_of_returns = None
        if data_dict.get("return_number") is not None:
            rn = np.array(data_dict["return_number"], dtype=np.float32)
            if self._is_fence_mode:
                if len(rn) == self._source_point_count:
                    rn = rn[self._target_indices]
                elif len(rn) != len(self._xyz):
                    rn = None
            elif len(rn) != len(self._xyz):
                rn = None
            self._return_number = rn
        if data_dict.get("number_of_returns") is not None:
            nr = np.array(data_dict["number_of_returns"], dtype=np.float32)
            if self._is_fence_mode:
                if len(nr) == self._source_point_count:
                    nr = nr[self._target_indices]
                elif len(nr) != len(self._xyz):
                    nr = None
            elif len(nr) != len(self._xyz):
                nr = None
            self._number_of_returns = nr

        self._data_dict_ref     = data_dict
        self.class_mapping      = dict(class_mapping)
        self.power_mapping      = dict(power_mapping) if power_mapping else {}
        self.enable_power_lines = bool(enable_power_lines)
        self._cancel_requested  = threading.Event()
        self.model     = None
        self.ort_sess  = None
        self.device    = None
        self.feat_mean = self.feat_std = None
        self._use_onnx = False

        # ── Apply advanced config (override InferenceConfig defaults) ──
        cfg = advanced_config or {}

        # CSF
        self._csf_cloth_resolution = float(cfg.get('csf_cloth_resolution',
                                                     InferenceConfig.CSF_CLOTH_RESOLUTION))
        self._csf_rigidness        = int(cfg.get('csf_rigidness',
                                                  InferenceConfig.CSF_RIGIDNESS))
        self._csf_class_threshold  = float(cfg.get('csf_class_threshold',
                                                    InferenceConfig.CSF_CLASS_THRESHOLD))

        # HAG vegetation correction thresholds
        self._lowveg_min  = float(cfg.get('lowveg_min',  InferenceConfig.LOWVEG_HAG_MIN))
        self._lowveg_max  = float(cfg.get('lowveg_max',  InferenceConfig.LOWVEG_HAG_MAX))
        self._midveg_max  = float(cfg.get('midveg_max',  InferenceConfig.MIDVEG_HAG_MAX))
        self._highveg_min = float(cfg.get('highveg_min', InferenceConfig.HIGHVEG_HAG_MIN))

        # Wire geometry
        self._wire_hag_min        = float(cfg.get('wire_hag_min',
                                                   InferenceConfig.WIRE_HAG_MIN))
        self._wire_hag_max        = float(cfg.get('wire_hag_max',
                                                   InferenceConfig.WIRE_HAG_MAX))
        self._wire_chain_radius   = float(cfg.get('wire_chain_radius',
                                                   InferenceConfig.WIRE_CHAIN_RADIUS))
        self._wire_density_max    = int(cfg.get('wire_density_max',
                                                 InferenceConfig.WIRE_DENSITY_MAX))
        self._wire_min_segment_pts = int(cfg.get('wire_min_segment_pts',
                                                  InferenceConfig.WIRE_MIN_SEGMENT_PTS))
        self._wire_linearity_min  = float(cfg.get('wire_linearity_min',
                                                   InferenceConfig.WIRE_LINEARITY_MIN))

        # CL corridor guidance for power-line detection.
        # IMPORTANT: this does NOT limit normal 5-class classification.
        # It is used only inside _detect_power_lines() to focus wire/pole candidates.
        self._use_cl_powerline_prior = bool(
            cfg.get('use_cl_powerline_prior', cfg.get('use_cl_corridor', False))
        )
        self._cl_corridor_width = float(
            cfg.get('cl_corridor_width', cfg.get('power_corridor_width', 0.0)) or 0.0
        )
        self._cl_corridor_coords = None

        cl_coords = cfg.get('cl_corridor_coords', None)
        if self._use_cl_powerline_prior and cl_coords is not None:
            try:
                cl_arr = np.asarray(cl_coords, dtype=np.float64)
                if cl_arr.ndim == 2 and cl_arr.shape[0] >= 2 and cl_arr.shape[1] >= 2:
                    self._cl_corridor_coords = np.ascontiguousarray(cl_arr[:, :2])
                else:
                    self._use_cl_powerline_prior = False
            except Exception as e:
                print(f"  WARNING: invalid CL corridor coordinates ignored: {e}")
                self._use_cl_powerline_prior = False

        if self._cl_corridor_width <= 0.0:
            self._use_cl_powerline_prior = False

        self._build_mapping_array()
        seed      = InferenceConfig.RANDOM_SEED
        self._rng = np.random.RandomState(seed) if seed is not None else np.random.RandomState()

        self._log_active_config()

    def _log_active_config(self):
        print("\n  ── Active inference config ──")
        print(f"  Power-line detection: {'ENABLED' if self.enable_power_lines else 'DISABLED (5-class mode)'}")
        if self._is_fence_mode:
            print(f"  Scope: fence region ({len(self._xyz):,} / {self._source_point_count:,} pts)")
        else:
            print(f"  Scope: full cloud ({len(self._xyz):,} pts)")
        print(f"  CSF: cloth_res={self._csf_cloth_resolution}  rigidness={self._csf_rigidness}"
              f"  threshold={self._csf_class_threshold}")
        print(f"  HAG veg: Ground<{self._lowveg_min}m  LowVeg:{self._lowveg_min}-{self._lowveg_max}m"
              f"  MidVeg:{self._lowveg_max}-{self._midveg_max}m  HighVeg>={self._highveg_min}m")
        if self.enable_power_lines:
            print(f"  Wire: HAG={self._wire_hag_min}-{self._wire_hag_max}m"
                  f"  chain_r={self._wire_chain_radius}m  density_max={self._wire_density_max}"
                  f"  min_seg={self._wire_min_segment_pts}pts  lin_min={self._wire_linearity_min}")

            if self._use_cl_powerline_prior and self._cl_corridor_coords is not None:
                print(
                    f"  CL power-line prior: ENABLED  "
                    f"radius=±{self._cl_corridor_width:.1f}m  "
                    f"vertices={len(self._cl_corridor_coords)}"
                )
            else:
                print("  CL power-line prior: DISABLED")

    def _build_mapping_array(self):
        # Always include base 5 classes; include power codes only if we have them.
        all_keys = list(self.class_mapping.keys())
        all_vals = list(self.class_mapping.values())
        if self.enable_power_lines and self.power_mapping:
            all_keys += list(self.power_mapping.keys())
            all_vals += list(self.power_mapping.values())

        for k in all_keys:
            if not isinstance(k, int) or k < 0:
                raise ValueError(f"Mapping key must be non-negative int, got {k}")
        for v in all_vals:
            if not isinstance(v, int) or v < 0 or v > 255:
                raise ValueError(f"Mapping value must be 0-255, got {v}")

        max_idx        = max(all_keys)
        self.map_array = np.zeros(max_idx + 1, dtype=np.uint8)
        for k, v in self.class_mapping.items():
            self.map_array[k] = v
        if self.enable_power_lines and self.power_mapping:
            for k, v in self.power_mapping.items():
                self.map_array[k] = v
        print(f"  Class mapping array: {self.map_array}")

    def cancel(self):
        self._cancel_requested.set()

    def _check_cancel(self):
        if self._cancel_requested.is_set():
            self._cleanup_gpu()
            raise InterruptedError("Cancelled by user")

    def _cleanup_gpu(self):
        if self.model is not None:
            del self.model; self.model = None
        if self.ort_sess is not None:
            del self.ort_sess; self.ort_sess = None
        if self.device is not None and self.device.type == 'cuda':
            torch.cuda.empty_cache()
        gc.collect()

    def run(self):
        completed = False
        try:
            self.progress.emit(1, "Loading AI model...")
            self._load_model_internal()
            self._check_cancel()
            self._classify_memory()
            if not self._cancel_requested.is_set():
                completed = True
        except InterruptedError:
            self.error.emit("Classification cancelled by user")
        except Exception as e:
            import traceback
            self.error.emit(
                f"Classification failed: {str(e)}\n\n{traceback.format_exc()}"
            )
        finally:
            self._cleanup_gpu()
            if completed and not self._cancel_requested.is_set():
                self.finished.emit()

    # ── MODEL LOADING ─────────────────────────────────────────

    def _load_model_internal(self):
        model_dir = Path(__file__).resolve().parent / "models"
        model_path = model_dir / "best_building.pth"
        stats_path = model_dir / "feature_stats.json"

        if not model_path.exists():
            raise FileNotFoundError(f"Basic AI model not found: {model_path}")
        if not stats_path.exists():
            raise FileNotFoundError(f"Basic AI feature_stats.json not found: {stats_path}")

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        onnx_path = model_path.with_suffix('.onnx')
        if HAS_ONNX and onnx_path.exists():
            print(f"  ONNX model found: {onnx_path}")
            providers  = (['CUDAExecutionProvider', 'CPUExecutionProvider']
                          if self.device.type == 'cuda' else ['CPUExecutionProvider'])
            sess_opts  = ort.SessionOptions()
            sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess_opts.intra_op_num_threads      = os.cpu_count()
            self.ort_sess      = ort.InferenceSession(
                str(onnx_path), sess_options=sess_opts, providers=providers
            )
            self._use_onnx      = True
            self._onnx_in_names = [i.name for i in self.ort_sess.get_inputs()]
            print(f"  ONNX providers: {self.ort_sess.get_providers()}")
        else:
            if HAS_ONNX and not onnx_path.exists():
                print(f"  No ONNX model — using PyTorch.")
            self._use_onnx = False

        model_py = model_path.parent / "model.py"
        if not model_py.exists():
            raise FileNotFoundError(f"model.py not found: {model_py}")
        spec = importlib.util.spec_from_file_location("pointnet2_model", str(model_py))
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        PointNet2SSG = mod.PointNet2SSG

        ckpt         = torch.load(str(model_path), map_location=self.device,
                                   weights_only=False)
        num_features = ckpt.get('num_features', InferenceConfig.EXPECTED_NUM_FEATURES)
        num_classes  = ckpt.get('num_classes', 5)

        if num_features != InferenceConfig.EXPECTED_NUM_FEATURES:
            raise ValueError(
                f"Model expects {num_features} features, "
                f"pipeline generates {InferenceConfig.EXPECTED_NUM_FEATURES}"
            )

        if not self._use_onnx:
            self.model = PointNet2SSG(
                num_features=num_features, num_classes=num_classes
            ).to(self.device)
            self.model.load_state_dict(ckpt['model_state_dict'], strict=True)
            self.model.eval()

        del ckpt

        with open(stats_path) as f:
            stats = json.load(f)
        self.feat_mean = np.array(stats['mean'], dtype=np.float32)
        self.feat_std  = np.array(stats['std'],  dtype=np.float32)

        if len(self.feat_mean) != InferenceConfig.EXPECTED_NUM_FEATURES:
            raise ValueError(
                f"Stats {len(self.feat_mean)} dims != "
                f"{InferenceConfig.EXPECTED_NUM_FEATURES}"
            )

        print(f"  Model: {model_path}")
        print(f"  Device: {self.device}")
        print(f"  Features: {num_features}, Classes: {num_classes}")
        print(f"  Backend: {'ONNX Runtime' if self._use_onnx else 'PyTorch'}")

    # ── MAIN PIPELINE ─────────────────────────────────────────

    def _classify_memory(self):
        t_start = time.time()
        self._check_cancel()

        xyz     = self._xyz
        n_total = len(xyz)
        print(f"  Points: {n_total:,}")
        if n_total == 0:
            raise ValueError("No points available for AI inference.")

        has_intensity = self._intensity is not None
        has_returns   = (self._return_number is not None
                         and self._number_of_returns is not None)

        if has_returns:
            rn_arr = self._return_number
            nr_arr = self._number_of_returns
            all_single = (
                rn_arr.max() == 1.0 and nr_arr.max() == 1.0 and
                rn_arr.min() == 1.0 and nr_arr.min() == 1.0
            )
            if all_single:
                if bool(self._data_dict_ref.get("_return_fields_from_source", False)):
                    print("  Return data verified from source: genuine all-single-return cloud")
                else:
                    print(
                        "  WARNING: all return fields are 1/1 but were not source-verified; "
                        "treating returns as unavailable"
                    )
                    has_returns = False

        if not has_intensity: print("  WARNING: No intensity — zero-filled")
        if not has_returns:   print("  WARNING: No returns — zero-filled")

        cols = [xyz]
        if has_intensity: cols.append(self._intensity.reshape(-1, 1))
        if has_returns:
            cols.append(rn_arr.reshape(-1, 1))
            cols.append(nr_arr.reshape(-1, 1))
        points = np.hstack(cols)

        self._check_cancel()
        self.progress.emit(10, "Computing Height Above Ground (CSF)...")
        t0  = time.time()
        hag = self._compute_hag_csf(xyz)
        print(f"    HAG done: {time.time()-t0:.1f}s  "
              f"range=[{hag.min():.2f}, {hag.max():.2f}]")

        self._check_cancel()
        self.progress.emit(30, "Extracting geometric features...")
        t0       = time.time()
        features = self._build_features(points, hag, has_intensity, has_returns)
        print(f"    Features done: {time.time()-t0:.1f}s  shape={features.shape}")

        assert features.shape == (n_total, InferenceConfig.EXPECTED_NUM_FEATURES)

        post_features = features[:, _POST_COL_INDICES].copy()

        self._check_cancel()
        self.progress.emit(45, "Normalizing features...")
        features -= self.feat_mean
        features /= self.feat_std
        np.clip(features, -10.0, 10.0, out=features)
        np.nan_to_num(features, copy=False, nan=0.0)

        self._check_cancel()
        self.progress.emit(50, "Running AI classification...")
        t0 = time.time()
        predictions, vote_counts = self._run_inference(xyz, features, n_total)
        print(f"    Inference done: {time.time()-t0:.1f}s")

        self._check_cancel()
        total_votes = vote_counts.sum(axis=1, keepdims=True).astype(np.float32)
        total_votes[total_votes == 0] = 1
        confidence  = vote_counts.max(axis=1) / total_votes.squeeze()

        # ── HAG correction pass ──
        self._check_cancel()
        self.progress.emit(86, "Applying HAG vegetation correction...")
        t0 = time.time()
        predictions, hag_fixes = self._hag_correction_pass(predictions, hag)
        print(f"    HAG correction: {hag_fixes:,} fixes ({time.time()-t0:.1f}s)")

        pre_post_predictions = predictions.copy()

        # ── Standard post-processing (always runs) ──
        self._check_cancel()
        self.progress.emit(88, "Post-processing...")
        t0 = time.time()
        predictions, fix_report = self._post_process_all(
            xyz, predictions, confidence, hag, post_features
        )
        print(f"    Post-processing: {sum(fix_report.values()):,} fixes "
              f"({time.time()-t0:.1f}s)")
        for k, v in fix_report.items():
            print(f"      {k}: {v:,}")

        try:
            pre_u, pre_c = np.unique(pre_post_predictions, return_counts=True)
            post_u, post_c = np.unique(predictions, return_counts=True)
            pre_dist = dict(zip(pre_u.tolist(), pre_c.tolist()))
            post_dist = dict(zip(post_u.tolist(), post_c.tolist()))
            pre_high = float(pre_dist.get(self.HIGHVEG, 0)) / float(n_total)
            post_high = float(post_dist.get(self.HIGHVEG, 0)) / float(n_total)

            suspicious_collapse = (
                len(pre_u) >= 3 and
                post_high >= 0.97 and
                pre_high <= 0.85
            )
            if suspicious_collapse:
                print("    WARNING: Post-processing collapse detected.")
                print(
                    f"      pre_highveg={pre_high*100:.1f}% "
                    f"post_highveg={post_high*100:.1f}%"
                )
                print("      Reverting to pre-postprocess predictions.")
                predictions = pre_post_predictions
        except Exception as _collapse_e:
            print(f"    WARNING: Collapse guard skipped: {_collapse_e}")

        # ══════════════════════════════════════════════════════
        # Power-line / pole post-pass — ONLY IF ENABLED
        # ══════════════════════════════════════════════════════
        if self.enable_power_lines:
            self._check_cancel()
            self.progress.emit(92, "Detecting power lines and poles...")
            t0 = time.time()
            predictions, power_report = self._detect_power_lines(
                xyz, predictions, hag, post_features
            )
            print(f"    Power lines: {time.time()-t0:.1f}s")
            for k, v in power_report.items():
                print(f"      {k}: {v:,}")
        else:
            print(f"    Power-line detection SKIPPED (disabled by user)")

        self._check_cancel()
        self.progress.emit(97, "Applying class mapping...")
        classified = self.map_array[predictions]

        if len(np.unique(classified)) == 1 and len(np.unique(predictions)) > 1:
            raise RuntimeError(
                "Class mapping collapsed multiple predicted classes into one output code. "
                "Check AI Class Mapping values."
            )

        names = {0:'Ground',1:'LowVeg',2:'MidVeg',3:'HighVeg',4:'Building',
                 5:'Wire',6:'Pole'}
        max_idx = 7 if self.enable_power_lines else 5
        print(f"\n  Mapping applied:")
        for model_idx in range(max_idx):
            mask = predictions == model_idx
            n    = mask.sum()
            if n > 0:
                code = self.map_array[model_idx] if model_idx < len(self.map_array) else '?'
                print(f"    {names[model_idx]} (idx {model_idx}) "
                      f"-> code {code}: {n:,} pts")

        if self._is_fence_mode:
            cls_full = self._data_dict_ref.get("classification")
            if cls_full is None or len(cls_full) != self._source_point_count:
                cls_full = np.zeros(self._source_point_count, dtype=np.uint8)
            else:
                cls_full = np.array(cls_full, copy=True)
            cls_full[self._target_indices] = classified
            self._data_dict_ref["classification"] = cls_full
            self._data_dict_ref["_ai_last_target_indices"] = self._target_indices.copy()
        else:
            self._data_dict_ref["classification"] = classified
            self._data_dict_ref["_ai_last_target_indices"] = None

        unique, counts = np.unique(classified, return_counts=True)
        print(f"\n  Final classification codes in memory:")
        for cls, cnt in zip(unique, counts):
            print(f"    Code {cls}: {cnt:>12,} ({100*cnt/n_total:.1f}%)")

        has_votes = vote_counts.sum(axis=1) > 0
        if has_votes.any():
            print(f"\n  Confidence: mean={confidence[has_votes].mean():.3f}")
        else:
            print("\n  Confidence: no vote coverage (fallback path)")
        print(f"  Total time: {time.time()-t_start:.1f}s")
        self.progress.emit(100, "Classification complete!")

    # ── HAG ───────────────────────────────────────────────────

    def _compute_hag_csf(self, xyz):
        """
        Compute HAG = Z - local ground Z.

        CSF is preferred. In small fence / building-heavy / vegetation-heavy
        areas, CSF may return too few ground points. Do not abort Basic AI in
        that case. Fall back to a robust local grid ground estimator so Basic AI
        can still classify Ground / Vegetation / Building and run the optional
        Wire / Pole pass.
        """
        if xyz is None or len(xyz) == 0:
            return np.zeros(0, dtype=np.float32)

        xyz = np.asarray(xyz, dtype=np.float64)

        if not HAS_CSF:
            print("    WARNING: CSF not available - using robust HAG fallback")
            return self._compute_hag_robust_fallback(
                xyz,
                reason="CSF module not available"
            )

        try:
            csf = CSF.CSF()
            csf.params.bSloopSmooth = False
            csf.params.cloth_resolution = self._csf_cloth_resolution
            csf.params.rigidness = self._csf_rigidness
            csf.params.time_step = InferenceConfig.CSF_TIME_STEP
            csf.params.class_threshold = self._csf_class_threshold

            # Some CSF Python builds use the misspelled attribute name.
            if hasattr(csf.params, "interations"):
                csf.params.interations = InferenceConfig.CSF_ITERATIONS
            elif hasattr(csf.params, "iterations"):
                csf.params.iterations = InferenceConfig.CSF_ITERATIONS

            csf.setPointCloud(xyz)

            ground_idx = CSF.VecInt()
            non_ground_idx = CSF.VecInt()
            csf.do_filtering(ground_idx, non_ground_idx)
            ground_idx = np.asarray(ground_idx, dtype=np.int64)

        except Exception as e:
            print(f"    WARNING: CSF failed - using robust HAG fallback: {e}")
            return self._compute_hag_robust_fallback(
                xyz,
                reason=f"CSF exception: {e}"
            )

        # Need enough ground samples for stable HAG interpolation.
        min_ground_pts = max(50, int(len(xyz) * 0.001))

        if len(ground_idx) < min_ground_pts:
            print(
                f"    WARNING: CSF returned too few ground points "
                f"({len(ground_idx):,}/{len(xyz):,}) - using robust HAG fallback"
            )
            return self._compute_hag_robust_fallback(
                xyz,
                reason=(
                    f"CSF returned too few ground points "
                    f"({len(ground_idx):,}/{len(xyz):,})"
                )
            )

        ground_pts = xyz[ground_idx]

        if len(ground_pts) > 500000:
            sel = self._rng.choice(len(ground_pts), 500000, replace=False)
            ground_pts = ground_pts[sel]

        tree = cKDTree(ground_pts[:, :2])
        k = min(5, len(ground_pts))

        try:
            dists, idxs = tree.query(xyz[:, :2], k=k, workers=-1)
        except TypeError:
            dists, idxs = tree.query(xyz[:, :2], k=k)

        del tree

        if k == 1:
            dists = dists.reshape(-1, 1)
            idxs = idxs.reshape(-1, 1)

        weights = 1.0 / (dists + 1e-6)
        weights /= weights.sum(axis=1, keepdims=True)

        local_ground_z = np.sum(ground_pts[idxs, 2] * weights, axis=1)
        hag = xyz[:, 2] - local_ground_z
        hag = np.clip(hag, -2.0, 120.0)

        return np.nan_to_num(
            hag,
            nan=0.0,
            posinf=120.0,
            neginf=-2.0
        ).astype(np.float32)

    def _compute_hag_robust_fallback(self, xyz, reason=""):
        """
        Robust HAG fallback for Basic AI when CSF cannot produce enough ground.

        This keeps Basic AI alive in small fence selections and improves the
        height feature used by Ground / Veg / Building plus the optional Wire /
        Pole post-pass.
        """
        print(f"    Robust HAG fallback active: {reason}")

        xyz = np.asarray(xyz, dtype=np.float64)
        n = len(xyz)

        if n == 0:
            return np.zeros(0, dtype=np.float32)

        xy = xyz[:, :2]
        z = xyz[:, 2]

        finite = (
            np.isfinite(xy[:, 0]) &
            np.isfinite(xy[:, 1]) &
            np.isfinite(z)
        )

        if finite.sum() < 10:
            return np.zeros(n, dtype=np.float32)

        z_valid = z[finite]
        z_p02, z_p98 = np.percentile(z_valid, [2, 98])
        z_med = np.median(z_valid)

        trim_mask = finite & (z >= z_p02) & (z <= z_p98)
        if trim_mask.sum() < max(50, int(n * 0.001)):
            trim_mask = finite

        trim_idx = np.where(trim_mask)[0]
        if len(trim_idx) == 0:
            ground_z = np.nanpercentile(z_valid, 5)
            hag = z - ground_z
            return np.clip(hag, -2.0, 120.0).astype(np.float32)

        xy_trim = xy[trim_idx]

        # 2 m grid is stable for roads, yards, roofs, and vegetation fences.
        cell_size = 2.0
        x0 = float(np.min(xy_trim[:, 0]))
        y0 = float(np.min(xy_trim[:, 1]))

        cx = np.floor((xy_trim[:, 0] - x0) / cell_size).astype(np.int64)
        cy = np.floor((xy_trim[:, 1] - y0) / cell_size).astype(np.int64)

        cy_span = int(cy.max()) + 1
        keys = cx * max(cy_span, 1) + cy

        order = np.argsort(keys)
        sorted_keys = keys[order]
        _, starts, counts = np.unique(
            sorted_keys,
            return_index=True,
            return_counts=True
        )

        ground_parts = []

        for start, count in zip(starts, counts):
            if count < 5:
                continue

            local_order = order[start:start + count]
            ids = trim_idx[local_order]
            local_z = z[ids]

            # Lowest local points are the safest ground candidates.
            low_z = np.percentile(local_z, 10)
            local_ground = ids[local_z <= low_z + 0.45]

            if len(local_ground) > 0:
                ground_parts.append(local_ground)

        if ground_parts:
            ground_idx = np.unique(np.concatenate(ground_parts))
        else:
            ground_idx = np.array([], dtype=np.int64)

        # Last fallback: bottom 2% of trimmed points.
        if len(ground_idx) < 50:
            take = max(20, int(len(trim_idx) * 0.02))
            take = min(take, len(trim_idx))
            ground_idx = trim_idx[np.argsort(z[trim_idx])[:take]]

        if len(ground_idx) == 0:
            ground_z = np.percentile(z_valid, 5)
            hag = z - ground_z
            return np.clip(hag, -2.0, 120.0).astype(np.float32)

        if len(ground_idx) > 300000:
            ground_idx = self._rng.choice(ground_idx, 300000, replace=False)

        ground_xy = xy[ground_idx]
        ground_z = z[ground_idx]

        tree = cKDTree(ground_xy)
        k = min(8, len(ground_idx))

        try:
            dists, idxs = tree.query(xy, k=k, workers=-1)
        except TypeError:
            dists, idxs = tree.query(xy, k=k)

        del tree

        if k == 1:
            dists = dists.reshape(-1, 1)
            idxs = idxs.reshape(-1, 1)

        # Stable inverse-distance interpolation.
        weights = 1.0 / (dists + 0.25)
        weights /= weights.sum(axis=1, keepdims=True)

        local_ground_z = np.sum(ground_z[idxs] * weights, axis=1)
        hag = z - local_ground_z
        hag = np.clip(hag, -2.0, 120.0)
        hag = np.nan_to_num(
            hag,
            nan=0.0,
            posinf=120.0,
            neginf=-2.0
        ).astype(np.float32)

        print(
            f"    Robust HAG fallback ground candidates: {len(ground_idx):,} | "
            f"z_med={z_med:.2f} z_p02={z_p02:.2f} z_p98={z_p98:.2f}"
        )

        return hag

    # ── HAG CORRECTION PASS ───────────────────────────────────

    def _hag_correction_pass(self, predictions, hag):
        fixes = 0

        veg_mask = np.isin(predictions, [self.LOWVEG, self.MIDVEG, self.HIGHVEG])
        below_ground = veg_mask & (hag < self._lowveg_min)
        n1 = int(below_ground.sum())
        if n1 > 0:
            predictions[below_ground] = self.GROUND
            fixes += n1
            print(f"      HAG corr-1 (veg→ground, HAG<{self._lowveg_min:.2f}m): {n1:,}")

        ground_mask   = predictions == self.GROUND
        above_lowveg  = ground_mask & (hag > self._lowveg_max)
        n_candidates  = int(above_lowveg.sum())

        if n_candidates > 0:
            building_mask = predictions == self.BUILDING
            n_corrected   = 0
            cand_idx      = np.where(above_lowveg)[0]

            if building_mask.sum() > 0:
                tree_bldg = cKDTree(self._xyz[building_mask][:, :2])
                dists, _  = tree_bldg.query(
                    self._xyz[cand_idx][:, :2], k=1, workers=-1
                )
                safe_to_correct = dists >= 3.0
                to_fix = cand_idx[safe_to_correct]
            else:
                to_fix = cand_idx

            if len(to_fix) > 0:
                predictions[to_fix] = self.LOWVEG
                n_corrected = len(to_fix)
                fixes += n_corrected

            print(f"      HAG corr-2 (ground→lowveg, HAG>{self._lowveg_max:.2f}m): "
                  f"{n_corrected:,} of {n_candidates:,} candidates")

        return predictions, fixes

    # ── FEATURES ──────────────────────────────────────────────

    def _build_features(self, points, hag, has_intensity, has_returns):
        n = len(points); xyz = points[:, :3]
        features = np.zeros(
            (n, InferenceConfig.EXPECTED_NUM_FEATURES), dtype=np.float32
        )
        features[:, 0:3] = xyz.astype(np.float32)
        features[:, 3]   = hag
        if has_intensity:
            intensity = points[:, 3].astype(np.float32)
            mx = intensity.max()
            if mx > 0: intensity /= mx
            features[:, 4] = intensity
        if has_returns:
            col_rn = 4 if has_intensity else 3; col_nr = col_rn + 1
            rn = points[:, col_rn].astype(np.float32)
            nr = points[:, col_nr].astype(np.float32)
            features[:, 5]  = rn; features[:, 6]  = nr
            features[:, 7]  = np.where(nr > 0, rn / nr, 0)
            features[:, 8]  = (nr == 1).astype(np.float32)
            features[:, 9]  = (rn == 1).astype(np.float32)
            features[:, 10] = (rn == nr).astype(np.float32)

        print("    Computing geometric features...")
        geom = self._compute_geometric_features(xyz.astype(np.float64))
        expected_geom = _N_GEOM_PER_SCALE * len(InferenceConfig.FEATURE_SCALES)
        if geom.shape[1] != expected_geom:
            raise ValueError(
                f"Geom features: expected {expected_geom}, got {geom.shape[1]}"
            )
        features[:, 11:11 + geom.shape[1]] = geom
        total_used = 11 + geom.shape[1]
        if total_used != InferenceConfig.EXPECTED_NUM_FEATURES:
            raise ValueError(
                f"Feature count mismatch: expected "
                f"{InferenceConfig.EXPECTED_NUM_FEATURES}, built {total_used}"
            )
        np.nan_to_num(features, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
        return features

    # ── GEOMETRIC FEATURE ROUTER ─────────────────────────────

    def _compute_geometric_features(self, xyz: np.ndarray) -> np.ndarray:
        ds_pts, vox_map, n_vox = voxel_downsample(
            xyz, InferenceConfig.GEOM_VOXEL_SIZE
        )
        print(f"    Voxel: {len(xyz):,} -> {n_vox:,}")

        use_gpu_pca = (
            self.device.type == 'cuda'
            and n_vox <= InferenceConfig.GPU_PCA_MAX_VOXELS
        )

        if use_gpu_pca:
            print(f"    Feature path: GPU PCA (N_vox={n_vox:,})")
            ds_all = self._geom_features_gpu(ds_pts, n_vox)
        elif HAS_JAKTERISTICS:
            print(f"    Feature path: Parallel jakteristics (N_vox={n_vox:,})")
            ds_all = self._geom_features_cpu_parallel(ds_pts)
        else:
            raise RuntimeError(
                "Neither GPU PCA (cloud too large) nor jakteristics available."
            )

        return ds_all[vox_map]

    # ── GPU PCA ───────────────────────────────────────────────

    def _geom_features_gpu(self, ds_pts: np.ndarray, n_vox: int) -> np.ndarray:
        pts_gpu = torch.from_numpy(ds_pts.astype(np.float32)).to(self.device)
        N = n_vox
        try:
            free_vram, _ = torch.cuda.mem_get_info(self.device)
            safe_bytes   = min(free_vram * 0.40, 512e6)
        except Exception:
            safe_bytes = 256e6
        chunk = max(64, min(2048, int(safe_bytes / max(N * 4, 1))))
        print(f"    GPU PCA chunk={chunk}")

        feat_blocks = []
        for radius in InferenceConfig.FEATURE_SCALES:
            self._check_cancel()
            t0 = time.time()
            k  = min(_GPU_K_PER_RADIUS.get(radius, 300), N - 1)
            try:
                feats = self._gpu_pca_block(pts_gpu, radius, k, chunk, N)
            except RuntimeError as e:
                if 'memory' in str(e).lower() and HAS_JAKTERISTICS:
                    print(f"      Scale {radius}m: GPU OOM → jakteristics fallback")
                    torch.cuda.empty_cache()
                    feats = jakteristics.compute_features(
                        ds_pts.astype(np.float64),
                        search_radius=radius,
                        feature_names=_GEOM_FEATURE_NAMES,
                        num_threads=-1,
                    )
                    feats = np.nan_to_num(feats, nan=0.0).astype(np.float32)
                else:
                    raise
            print(f"      Scale {radius}m: {time.time()-t0:.2f}s")
            feat_blocks.append(feats)
        return np.hstack(feat_blocks)

    def _gpu_pca_block(self, pts, radius, k, chunk, N):
        k1  = min(k + 1, N)
        out = torch.zeros((N, 11), dtype=torch.float32, device=self.device)
        with torch.inference_mode():
            for start in range(0, N, chunk):
                end = min(start + chunk, N); B_cur = end - start
                query = pts[start:end]
                dist  = torch.cdist(query, pts)
                kd, ki = dist.topk(k1, dim=1, largest=False)
                valid  = (kd <= radius) & (kd > 1e-8)
                n_valid = valid.float().sum(dim=1)
                has_min = n_valid >= 3
                if not has_min.any(): continue
                nb = pts[ki]; v_f = valid.unsqueeze(-1).float()
                nv_safe  = n_valid.clamp(min=1).view(B_cur, 1)
                centroid = (nb * v_f).sum(1) / nv_safe
                centered = (nb - centroid.unsqueeze(1)) * v_f
                cov = (
                    torch.bmm(centered.transpose(1, 2), centered)
                    / n_valid.clamp(min=1).view(B_cur, 1, 1)
                )
                try:
                    ev, evec = torch.linalg.eigh(cov)
                except RuntimeError:
                    continue
                ev = ev.flip(-1).clamp(min=1e-10); evec = evec.flip(-1)
                l1, l2, l3 = ev[:,0], ev[:,1], ev[:,2]
                l1c = l1.clamp(min=1e-10); lsum = (l1+l2+l3).clamp(min=1e-10)
                f = out[start:end]
                f[:,0]=l1; f[:,1]=l2; f[:,2]=l3
                f[:,3]=(l1-l2)/l1c; f[:,4]=(l2-l3)/l1c; f[:,5]=l3/l1c
                f[:,6]=(l1*l2*l3).clamp(min=1e-30).pow(1/3)
                f[:,7]=(l1-l3)/l1c
                l_norm=(ev/lsum.unsqueeze(1)).clamp(min=1e-10)
                f[:,8]=-(l_norm*l_norm.log()).sum(1)
                f[:,9]=l3/lsum
                f[:,10]=1.0-evec[:,2,2].abs()
                f[~has_min]=0.0
        return out.cpu().numpy()

    # ── PARALLEL JAKTERISTICS ─────────────────────────────────

    def _geom_features_cpu_parallel(self, ds_pts: np.ndarray) -> np.ndarray:
        scales      = InferenceConfig.FEATURE_SCALES
        n_scales    = len(scales)
        n_cpu       = os.cpu_count() or 4
        t_per_scale = max(1, n_cpu // n_scales)
        pts_f64     = ds_pts.astype(np.float64)

        print(f"    Parallel jakteristics: {n_scales} scales × {t_per_scale} threads")

        def _one_scale(radius: float) -> tuple:
            t0 = time.time()
            f  = jakteristics.compute_features(
                pts_f64,
                search_radius=radius,
                feature_names=_GEOM_FEATURE_NAMES,
                num_threads=t_per_scale,
            )
            f = np.nan_to_num(f, nan=0.0).astype(np.float32)
            print(f"      Scale {radius}m: {time.time()-t0:.1f}s")
            return (radius, f)

        ordered = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_scales) as ex:
            futures = {ex.submit(_one_scale, r): r for r in scales}
            for fut in concurrent.futures.as_completed(futures):
                radius, feats = fut.result()
                ordered[radius] = feats

        return np.hstack([ordered[r] for r in scales])

    # ── INFERENCE ─────────────────────────────────────────────

    def _run_inference(self, xyz, features, n_total):
        vote_counts = np.zeros((n_total, 5), dtype=np.int32)
        tile_size    = InferenceConfig.TILE_SIZE
        stride       = tile_size - InferenceConfig.TILE_OVERLAP
        x_min, y_min = xyz[:,0].min(), xyz[:,1].min()
        x_max, y_max = xyz[:,0].max(), xyz[:,1].max()

        x_order  = np.argsort(xyz[:, 0])
        x_sorted = xyz[x_order, 0]

        tile_specs = []
        xs = x_min
        while xs <= x_max:
            ys = y_min
            while ys <= y_max:
                tile_specs.append((xs, ys)); ys += stride
            xs += stride

        tile_members = []
        for tx, ty in tile_specs:
            lo = np.searchsorted(x_sorted, tx, side='left')
            hi = np.searchsorted(x_sorted, tx + tile_size, side='left')
            if hi <= lo:
                tile_members.append(None); continue
            cands  = x_order[lo:hi]
            y_vals = xyz[cands, 1]
            t_idx  = cands[(y_vals >= ty) & (y_vals < ty + tile_size)]
            t_idx.sort()
            tile_members.append(t_idx if len(t_idx) >= 100 else None)

        n_active = sum(1 for m in tile_members if m is not None)
        if n_active == 0:
            # Small/sparse fence selections may have no 100+ point tiles.
            # Fallback to a single pseudo-tile over all selected points.
            tile_members = [np.arange(n_total, dtype=np.int64)]
            tile_specs = [(x_min, y_min)]
            n_active = 1
        print(f"    Tiles: {len(tile_specs)} ({n_active} active)")
        total_batches = 0
        total_work    = max(1, n_active * InferenceConfig.VOTE_PASSES)

        for vote in range(InferenceConfig.VOTE_PASSES):
            self._check_cancel()
            gpu_batch = []; batch_meta = []

            for ti, members in enumerate(tile_members):
                if ti % 50 == 0: self._check_cancel()
                if members is None: continue
                n_tile = len(members)
                if n_tile >= InferenceConfig.NUM_POINTS:
                    sel = self._rng.choice(n_tile, InferenceConfig.NUM_POINTS,
                                           replace=False)
                else:
                    sel = self._rng.choice(n_tile, InferenceConfig.NUM_POINTS,
                                           replace=True)
                batch_coords  = xyz[members[sel]].copy().astype(np.float32)
                batch_feat    = features[members[sel]].copy()
                batch_coords -= batch_coords.mean(axis=0)
                gpu_batch.append((batch_coords, batch_feat))
                batch_meta.append((members, sel))

                if len(gpu_batch) >= InferenceConfig.INFERENCE_BATCH_SIZE:
                    self._check_cancel()
                    preds_list = self._run_batched_inference(gpu_batch)
                    for pred, (t_idx, s) in zip(preds_list, batch_meta):
                        actual = t_idx[s]; valid = pred < 5
                        np.add.at(vote_counts, (actual[valid], pred[valid]), 1)
                    total_batches += len(gpu_batch)
                    gpu_batch = []; batch_meta = []
                    pct = int(50 + 36 * total_batches / total_work)
                    self.progress.emit(
                        min(pct, 85),
                        f"Vote {vote+1}/{InferenceConfig.VOTE_PASSES}"
                    )
                    if total_batches % 50 == 0 and self.device.type == 'cuda':
                        torch.cuda.empty_cache()

            if gpu_batch:
                preds_list = self._run_batched_inference(gpu_batch)
                for pred, (t_idx, s) in zip(preds_list, batch_meta):
                    actual = t_idx[s]; valid = pred < 5
                    np.add.at(vote_counts, (actual[valid], pred[valid]), 1)

        has_votes   = vote_counts.sum(axis=1) > 0
        n_no_votes  = (~has_votes).sum()
        predictions = np.zeros(n_total, dtype=np.uint8)
        if not np.any(has_votes):
            return predictions, vote_counts
        predictions[has_votes] = vote_counts[has_votes].argmax(axis=1)

        if n_no_votes > 0:
            print(f"    {n_no_votes:,} without votes — NN fill")
            tree    = cKDTree(xyz[has_votes])
            _, nn_i = tree.query(xyz[~has_votes], k=1)
            del tree
            predictions[~has_votes] = predictions[has_votes][nn_i]

        return predictions, vote_counts

    def _run_batched_inference(self, tile_batch):
        B = len(tile_batch)
        if B == 0: return []
        coords_batch = np.stack([t[0] for t in tile_batch])
        feats_batch  = np.stack([t[1] for t in tile_batch])

        if self._use_onnx and self.ort_sess is not None:
            ort_inputs = {
                self._onnx_in_names[0]: coords_batch.astype(np.float32),
                self._onnx_in_names[1]: feats_batch.astype(np.float32),
            }
            logits_np = self.ort_sess.run(None, ort_inputs)[0]
            preds     = logits_np.argmax(axis=-1)
            del ort_inputs, logits_np
            return [preds[i] for i in range(B)]

        c_t = torch.from_numpy(coords_batch).float().to(self.device)
        f_t = torch.from_numpy(feats_batch).float().to(self.device)
        with torch.inference_mode():
            if self.device.type == 'cuda':
                with torch.amp.autocast('cuda'):
                    logits = self.model(c_t, f_t)
            else:
                logits = self.model(c_t, f_t)
        preds = logits.argmax(dim=-1).cpu().numpy()
        del c_t, f_t, logits
        return [preds[i] for i in range(B)]

    # ── POST-PROCESSING ───────────────────────────────────────

    _PF_LIN_S0  = 0
    _PF_PLAN_S0 = 1
    _PF_VERT_S0 = 2
    _PF_LIN_S1  = 3
    _PF_VERT_S1 = 4

    def _post_process_all(self, xyz, predictions, confidence, hag, post_features):
        fix_report = {}

        self.progress.emit(89, "Post-processing: ground-on-roof...")
        t0 = time.time()
        predictions, n = self._fix_ground_on_roofs(xyz, predictions, hag)
        fix_report['ground_on_roof'] = n
        print(f"      ground_on_roof: {n:,} ({time.time()-t0:.1f}s)")

        self._check_cancel()
        self.progress.emit(90, "Post-processing: wall recovery...")
        t0 = time.time()
        predictions, n = self._fix_building_walls(
            xyz, predictions, hag, post_features
        )
        fix_report['wall_recovery'] = n
        print(f"      wall_recovery: {n:,} ({time.time()-t0:.1f}s)")

        self._check_cancel()
        self.progress.emit(91, "Post-processing: boundary cleanup...")
        t0 = time.time()
        predictions, n = self._fix_salt_pepper(xyz, predictions, confidence)
        fix_report['salt_pepper'] = n
        print(f"      salt_pepper: {n:,} ({time.time()-t0:.1f}s)")

        return predictions, fix_report

    def _fix_ground_on_roofs(self, xyz, predictions, hag):
        ground_mask   = predictions == self.GROUND
        building_mask = predictions == self.BUILDING
        if ground_mask.sum() == 0 or building_mask.sum() == 0:
            return predictions, 0
        ground_indices = np.where(ground_mask)[0]
        tree_bldg_2d   = cKDTree(xyz[building_mask][:, :2])
        dists, _       = tree_bldg_2d.query(
            xyz[ground_indices][:, :2], k=1, workers=-1
        )
        del tree_bldg_2d
        candidates = ground_indices[dists < 3.0]
        if len(candidates) == 0: return predictions, 0
        tree_all       = cKDTree(xyz)
        neighbor_lists = tree_all.query_ball_point(
            xyz[candidates], r=2.0, workers=-1
        )
        all_ground_xyz = xyz[ground_mask]
        if len(all_ground_xyz) == 0: return predictions, 0

        ratio_pass = []
        for i, neighbors in enumerate(neighbor_lists):
            if len(neighbors) >= 5:
                if (predictions[neighbors] == self.BUILDING).sum() / len(neighbors) >= 0.50:
                    ratio_pass.append(i)
        if not ratio_pass:
            return predictions, 0

        pass_candidates = candidates[np.array(ratio_pass)]

        tree_ground_2d = cKDTree(all_ground_xyz[:, :2])
        d_g, i_g = tree_ground_2d.query(
            xyz[pass_candidates, :2], k=10, workers=-1
        )
        del tree_ground_2d

        near_mask    = d_g < 20.0
        ground_z     = all_ground_xyz[i_g, 2]
        ground_z_m   = np.where(near_mask, ground_z, np.nan)
        median_gz    = np.nanmedian(ground_z_m, axis=1)
        has_near     = near_mask.any(axis=1)
        to_fix       = has_near & ((xyz[pass_candidates, 2] - median_gz) > 2.5)

        predictions[pass_candidates[to_fix]] = self.BUILDING
        return predictions, int(to_fix.sum())

    def _fix_building_walls(self, xyz, predictions, hag, post_features):
        building_mask = predictions == self.BUILDING
        veg_mask      = (predictions == self.HIGHVEG) | (predictions == self.MIDVEG)
        if building_mask.sum() == 0 or veg_mask.sum() == 0:
            return predictions, 0
        building_xyz = xyz[building_mask]; building_hag = hag[building_mask]
        tree_bldg_2d = cKDTree(building_xyz[:, :2])
        veg_idx = np.where(veg_mask)[0]; veg_hag = hag[veg_idx]
        vert_05 = post_features[veg_idx, self._PF_VERT_S0]
        d_b, nn_b   = tree_bldg_2d.query(
            xyz[veg_idx][:, :2], k=1, workers=-1
        )
        nn_bldg_hag = building_hag[nn_b]
        wall = (
            (d_b < 3.0) & (vert_05 > 0.4) & (veg_hag > 1.0) &
            (veg_hag < nn_bldg_hag + 2.0) & (veg_hag > 0.15 * nn_bldg_hag)
        )
        predictions[veg_idx[wall]] = self.BUILDING; fixes = wall.sum()
        rem = np.where(
            (predictions == self.HIGHVEG) | (predictions == self.MIDVEG)
        )[0]
        if len(rem) > 0:
            d_r, nn_r = tree_bldg_2d.query(xyz[rem][:, :2], k=1, workers=-1)
            buf = (
                (d_r < 0.5) & (hag[rem] > 1.0) &
                (hag[rem] < building_hag[nn_r] + 1.0)
            )
            predictions[rem[buf]] = self.BUILDING; fixes += buf.sum()
        del tree_bldg_2d
        return predictions, fixes

    def _fix_salt_pepper(self, xyz, predictions, confidence):
        fixes = 0; tree_all = cKDTree(xyz)
        b_mask = predictions == self.BUILDING
        if b_mask.sum() > 100:
            nb_idx = np.where(~b_mask)[0]
            tree_b = cKDTree(xyz[b_mask])
            d, _   = tree_b.query(xyz[nb_idx], k=1, workers=-1)
            del tree_b
            cand   = nb_idx[d < 2.0]
            if len(cand) > 0:
                nls = tree_all.query_ball_point(xyz[cand], r=1.5, workers=-1)
                for pi, nbrs in zip(cand, nls):
                    if (len(nbrs) >= 5 and
                            (predictions[nbrs] == self.BUILDING).sum() / len(nbrs) > 0.70):
                        predictions[pi] = self.BUILDING; fixes += 1

        b_mask = predictions == self.BUILDING; b_idx = np.where(b_mask)[0]
        if len(b_idx) > 100:
            check = (
                self._rng.choice(b_idx, min(200_000, len(b_idx)), replace=False)
                if len(b_idx) > 200_000 else b_idx
            )
            nls      = tree_all.query_ball_point(xyz[check], r=2.0, workers=-1)
            isolated = []
            for pi, nbrs in zip(check, nls):
                if len(nbrs) < 5: isolated.append(pi)
                elif (predictions[nbrs] == self.BUILDING).sum() / len(nbrs) < 0.15:
                    isolated.append(pi)
            if isolated:
                isolated_arr = np.array(isolated)
                _, nn_batch  = tree_all.query(xyz[isolated_arr], k=20, workers=-1)
                nn_preds     = predictions[nn_batch[:, 1:]]
                for i, idx in enumerate(isolated_arr):
                    nb = nn_preds[i][nn_preds[i] != self.BUILDING]
                    if len(nb) > 0:
                        v, c = np.unique(nb, return_counts=True)
                        predictions[idx] = v[c.argmax()]; fixes += 1

        lc = np.where(
            (confidence < 0.6) &
            np.isin(predictions, [self.MIDVEG, self.HIGHVEG, self.BUILDING])
        )[0]
        if 0 < len(lc) < 500_000:
            _, nn_batch = tree_all.query(xyz[lc], k=15, workers=-1)
            nn_conf = confidence[nn_batch[:, 1:]]
            nn_pred = predictions[nn_batch[:, 1:]]
            high_conf = nn_conf > 0.8
            n_high    = high_conf.sum(axis=1)
            for i in np.where(n_high >= 5)[0]:
                hp = nn_pred[i][high_conf[i]]
                v, c = np.unique(hp, return_counts=True)
                new = v[c.argmax()]
                if new != predictions[lc[i]]:
                    predictions[lc[i]] = new; fixes += 1
        del tree_all
        return predictions, fixes

    def _points_near_polyline_xy(self, xyz, coords_xy, radius):
        """
        Return mask for points whose XY distance is within radius from a CL polyline.
        Used only as a power-line/pole candidate prior, never as the full AI scope.
        """
        try:
            pts = np.asarray(coords_xy, dtype=np.float64)
            if pts.ndim != 2 or pts.shape[0] < 2 or pts.shape[1] < 2:
                return np.zeros(len(xyz), dtype=bool)

            xy = np.asarray(xyz[:, :2], dtype=np.float64)
            line = pts[:, :2]
            radius = float(radius)
            if radius <= 0.0:
                return np.zeros(len(xyz), dtype=bool)

            inside = np.zeros(len(xy), dtype=bool)
            r2 = radius * radius

            for i in range(len(line) - 1):
                a = line[i]
                b = line[i + 1]
                ab = b - a
                ab2 = float(np.dot(ab, ab))
                if ab2 <= 1e-12:
                    continue

                min_x = min(a[0], b[0]) - radius
                max_x = max(a[0], b[0]) + radius
                min_y = min(a[1], b[1]) - radius
                max_y = max(a[1], b[1]) + radius

                cand = np.where(
                    (~inside) &
                    (xy[:, 0] >= min_x) & (xy[:, 0] <= max_x) &
                    (xy[:, 1] >= min_y) & (xy[:, 1] <= max_y)
                )[0]

                if len(cand) == 0:
                    continue

                p = xy[cand]
                t = ((p[:, 0] - a[0]) * ab[0] + (p[:, 1] - a[1]) * ab[1]) / ab2
                t = np.clip(t, 0.0, 1.0)

                proj = a + t[:, None] * ab[None, :]
                d = p - proj
                keep = (d[:, 0] * d[:, 0] + d[:, 1] * d[:, 1]) <= r2

                if np.any(keep):
                    inside[cand[keep]] = True

            return inside

        except Exception as e:
            print(f"    CL corridor mask failed: {e}")
            return np.zeros(len(xyz), dtype=bool)


    # ── POWER LINE DETECTION (only invoked when enable_power_lines=True) ──

    def _polyline_distance_station_xy(self, xyz, coords_xy):
        """
        For each xyz point, return:
        - nearest XY distance to the CL polyline
        - station/chainage along the CL polyline at the nearest projected point
        """
        xy = np.asarray(xyz[:, :2], dtype=np.float64)
        line = np.asarray(coords_xy, dtype=np.float64)[:, :2]
        n = len(xy)

        best_d2 = np.full(n, np.inf, dtype=np.float64)
        best_s = np.zeros(n, dtype=np.float64)

        seg_vec = line[1:] - line[:-1]
        seg_len = np.sqrt(np.sum(seg_vec * seg_vec, axis=1))
        cum = np.concatenate([[0.0], np.cumsum(seg_len)])

        for i in range(len(seg_vec)):
            a = line[i]
            ab = seg_vec[i]
            ab2 = float(np.dot(ab, ab))
            if ab2 <= 1e-12:
                continue

            p = xy - a[None, :]
            t = (p[:, 0] * ab[0] + p[:, 1] * ab[1]) / ab2
            t = np.clip(t, 0.0, 1.0)
            proj = a[None, :] + t[:, None] * ab[None, :]
            d = xy - proj
            d2 = d[:, 0] * d[:, 0] + d[:, 1] * d[:, 1]

            better = d2 < best_d2
            if np.any(better):
                best_d2[better] = d2[better]
                best_s[better] = cum[i] + t[better] * seg_len[i]

        return np.sqrt(best_d2), best_s

    def _grid_cluster_labels_power(self, xy, cell_size=1.5):
        """
        Memory-safe approximate 2D clustering using grid cells.
        Avoids cKDTree.query_pairs() on large candidate sets.
        """
        xy = np.asarray(xy, dtype=np.float64)
        n = len(xy)
        if n == 0:
            return np.array([], dtype=np.int32), 0

        cell_size = max(float(cell_size), 0.25)
        cells = np.floor(xy / cell_size).astype(np.int64)
        unique_cells, inverse = np.unique(cells, axis=0, return_inverse=True)

        n_cells = len(unique_cells)
        parent = np.arange(n_cells, dtype=np.int32)

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            ra = find(a)
            rb = find(b)
            if ra != rb:
                parent[rb] = ra

        lookup = {(int(c[0]), int(c[1])): i for i, c in enumerate(unique_cells)}

        for i, c in enumerate(unique_cells):
            cx = int(c[0])
            cy = int(c[1])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    j = lookup.get((cx + dx, cy + dy))
                    if j is not None:
                        union(i, j)

        roots = np.array([find(i) for i in range(n_cells)], dtype=np.int32)
        _, compact = np.unique(roots, return_inverse=True)
        labels = compact[inverse].astype(np.int32)
        return labels, int(compact.max() + 1) if len(compact) else 0

    def _long_station_runs(self, stations, bin_size=2.0, min_run_m=12.0, max_gap_bins=2):
        """
        Return boolean mask of points that belong to long continuous station runs.
        This is much more reliable than local PCA when wires are near vegetation.
        """
        stations = np.asarray(stations, dtype=np.float64)
        if len(stations) == 0:
            return np.zeros(0, dtype=bool)

        s0 = float(np.nanmin(stations))
        bins = np.floor((stations - s0) / float(bin_size)).astype(np.int64)
        unique_bins = np.unique(bins)
        if len(unique_bins) == 0:
            return np.zeros(len(stations), dtype=bool)

        keep_bins = []
        run = [int(unique_bins[0])]

        for b in unique_bins[1:]:
            b = int(b)
            if b - run[-1] <= max_gap_bins + 1:
                run.append(b)
            else:
                span_m = (run[-1] - run[0] + 1) * bin_size
                if span_m >= min_run_m:
                    keep_bins.extend(run)
                run = [b]

        span_m = (run[-1] - run[0] + 1) * bin_size
        if span_m >= min_run_m:
            keep_bins.extend(run)

        if not keep_bins:
            return np.zeros(len(stations), dtype=bool)

        return np.isin(bins, np.asarray(keep_bins, dtype=np.int64))

    def _cl_hard_wire_rescue(self, xyz, predictions, hag, post_features,
                             cl_mask, existing_wire_idx=None):
        """
        CL-guided conductor rescue for production use.

        Key differences from the older SAFE rescue:
        - The user-selected CL corridor is not collapsed to +/-2.4 m.
        - Multiple parallel conductors are allowed across a wider ROW corridor.
        - Points initially predicted as Building may be recovered as Wire only
          when their geometry is strongly cable-like.
        - Vegetation bleed is still controlled by HAG, density, multi-scale
          linearity/planarity, station continuity and per-station caps.
        """
        existing_wire_idx = (
            np.asarray(existing_wire_idx, dtype=np.int64)
            if existing_wire_idx is not None
            else np.array([], dtype=np.int64)
        )

        if cl_mask is None or not np.any(cl_mask):
            print("    CL conductor rescue skipped: no CL mask")
            return np.array([], dtype=np.int64)

        if self._cl_corridor_coords is None or len(self._cl_corridor_coords) < 2:
            print("    CL conductor rescue skipped: no CL geometry")
            return np.array([], dtype=np.int64)

        wire_hag_min = max(2.0, float(self._wire_hag_min) - 1.0)
        wire_hag_max = float(self._wire_hag_max)

        # A 5-class model can call a real conductor HighVeg/MidVeg/LowVeg or
        # even Building.  Ground is intentionally excluded from the rescue.
        base_mask = (
            cl_mask &
            np.isin(predictions, [self.LOWVEG, self.MIDVEG, self.HIGHVEG, self.BUILDING]) &
            (hag >= wire_hag_min) &
            (hag <= wire_hag_max)
        )

        base_idx = np.where(base_mask)[0]
        print(f"    CL conductor rescue pool: {len(base_idx):,} pts")

        if len(base_idx) < 6:
            return np.array([], dtype=np.int64)

        d_cl, station = self._polyline_distance_station_xy(
            xyz[base_idx],
            self._cl_corridor_coords,
        )

        # Preserve parallel phase conductors.  For a +/-9 m corridor this is
        # about +/-5.4 m, instead of the old +/-2.4 m hard clamp.
        core_width = max(
            3.0,
            min(6.0, float(self._cl_corridor_width) * 0.60),
        )
        core_width = min(core_width, float(self._cl_corridor_width))
        core = d_cl <= core_width

        cand_idx = base_idx[core]
        cand_station = station[core]
        cand_d = d_cl[core]

        print(
            f"    CL conductor rescue core: {len(cand_idx):,} pts "
            f"within +/-{core_width:.1f}m"
        )

        if len(cand_idx) < 6:
            return np.array([], dtype=np.int64)

        try:
            t = cKDTree(xyz[cand_idx])
            local_density = np.asarray(
                t.query_ball_point(
                    xyz[cand_idx],
                    r=0.75,
                    workers=-1,
                    return_length=True,
                ),
                dtype=np.int32,
            ) - 1
            del t
        except Exception as e:
            print(f"    CL conductor density check skipped: {e}")
            local_density = np.zeros(len(cand_idx), dtype=np.int32)

        density_limit = max(12, min(32, int(self._wire_density_max) * 2 + 4))

        lin0 = post_features[cand_idx, self._PF_LIN_S0].astype(np.float32)
        lin1 = post_features[cand_idx, self._PF_LIN_S1].astype(np.float32)
        plan0 = post_features[cand_idx, self._PF_PLAN_S0].astype(np.float32)
        was_building = predictions[cand_idx] == self.BUILDING

        # Normal vegetation-like points get a relaxed cable geometry gate.
        normal_geom = (
            (lin0 >= 0.30) |
            (lin1 >= 0.35) |
            ((plan0 <= 0.35) & (local_density <= density_limit))
        )

        # Building -> Wire is allowed only with much stronger evidence.
        building_density_limit = min(density_limit, 20)
        building_geom = (
            ((lin0 >= 0.62) | (lin1 >= 0.68)) &
            (plan0 <= 0.28) &
            (local_density <= building_density_limit)
        )

        geom_ok = np.where(was_building, building_geom, normal_geom)
        keep = geom_ok & (local_density <= density_limit)

        cand_idx = cand_idx[keep]
        cand_station = cand_station[keep]
        cand_d = cand_d[keep]
        lin0 = lin0[keep]
        lin1 = lin1[keep]
        plan0 = plan0[keep]
        local_density = local_density[keep]

        print(f"    CL conductor after geometry/density: {len(cand_idx):,} pts")

        if len(cand_idx) < 6:
            return np.array([], dtype=np.int64)

        # Cable points must continue along the corridor for a meaningful span.
        run_mask = self._long_station_runs(
            cand_station,
            bin_size=2.0,
            min_run_m=10.0,
            max_gap_bins=3,
        )

        cand_idx = cand_idx[run_mask]
        cand_station = cand_station[run_mask]
        cand_d = cand_d[run_mask]
        lin0 = lin0[run_mask]
        lin1 = lin1[run_mask]
        plan0 = plan0[run_mask]
        local_density = local_density[run_mask]

        print(f"    CL conductor long-run candidates: {len(cand_idx):,} pts")

        if len(cand_idx) < 6:
            return np.array([], dtype=np.int64)

        if len(existing_wire_idx) > 0:
            try:
                t_wire = cKDTree(xyz[existing_wire_idx, :2])
                d_old, _ = t_wire.query(xyz[cand_idx, :2], k=1, workers=-1)
                del t_wire
                keep_new = d_old > 0.50
                cand_idx = cand_idx[keep_new]
                cand_station = cand_station[keep_new]
                cand_d = cand_d[keep_new]
                lin0 = lin0[keep_new]
                lin1 = lin1[keep_new]
                plan0 = plan0[keep_new]
                local_density = local_density[keep_new]
            except Exception:
                pass

        if len(cand_idx) < 6:
            return np.array([], dtype=np.int64)

        hag_score = np.clip(
            (hag[cand_idx].astype(np.float32) - wire_hag_min) / 20.0,
            0.0,
            1.0,
        )
        # CL distance helps rank candidates, but no longer forces every cable
        # to sit almost exactly on the center line.
        dist_score = 1.0 / (cand_d.astype(np.float32) + 1.00)
        geom_score = (
            np.clip(lin0, 0.0, 1.0) +
            np.clip(lin1, 0.0, 1.0) +
            np.clip(1.0 - plan0, 0.0, 1.0)
        )
        density_score = 1.0 / (local_density.astype(np.float32) + 1.0)
        score = 1.5 * dist_score + 0.75 * hag_score + 0.75 * geom_score + 0.75 * density_score

        # Keep enough points for multiple parallel conductors at the same
        # station.  The old cap (28) could thin a multi-phase corridor heavily.
        bin_size = 2.0
        s0 = float(np.nanmin(cand_station))
        bins = np.floor((cand_station - s0) / bin_size).astype(np.int64)

        kept_parts = []
        max_per_bin = 80
        for b in np.unique(bins):
            loc = np.flatnonzero(bins == b)
            if len(loc) <= max_per_bin:
                kept_parts.append(loc)
            else:
                best = loc[np.argpartition(score[loc], -max_per_bin)[-max_per_bin:]]
                kept_parts.append(best)

        if not kept_parts:
            return np.array([], dtype=np.int64)

        keep_local = np.concatenate(kept_parts)
        cand_idx = cand_idx[keep_local]
        cand_station = cand_station[keep_local]

        final_run_mask = self._long_station_runs(
            cand_station,
            bin_size=2.0,
            min_run_m=10.0,
            max_gap_bins=3,
        )
        wire_idx = np.unique(cand_idx[final_run_mask]).astype(np.int64)

        print(f"    CL conductor rescue FINAL: {len(wire_idx):,} pts")
        return wire_idx


    def _cl_hard_pole_rescue(self, xyz, predictions, hag, post_features,
                             cl_mask, wire_idx):
        """
        CL-guided Pole/Pylon rescue.

        The old implementation assumed every class-16 object was a narrow
        utility pole (XY span <= ~2.6 m).  ENEL class 16 is "Pylons or Poles",
        so this version has two explicit geometry branches:

        1) narrow pole: compact XY footprint + tall multi-height structure
        2) lattice pylon/tower: wider footprint + substantial height +
           multi-height occupancy + structural/vertical support

        Confirmed Wire points are never overwritten.
        """
        if cl_mask is None or not np.any(cl_mask):
            return np.array([], dtype=np.int64)

        wire_idx = np.asarray(wire_idx, dtype=np.int64)
        wire_confirmed = len(wire_idx) > 0

        vert0_all = post_features[:, self._PF_VERT_S0].astype(np.float32)
        vert1_all = post_features[:, self._PF_VERT_S1].astype(np.float32)

        # Building points are always eligible because a 5-class model commonly
        # interprets lattice towers as Building. Vegetation-like points need
        # some vertical structural evidence before entering the pole pool.
        structural_support = (
            (predictions == self.BUILDING) |
            (
                np.isin(predictions, [self.LOWVEG, self.MIDVEG, self.HIGHVEG]) &
                ((vert0_all >= 0.16) | (vert1_all >= 0.18))
            )
        )

        pole_mask = (
            cl_mask &
            structural_support &
            (predictions != InferenceConfig.WIRE_INTERNAL_CODE) &
            (hag >= max(1.0, InferenceConfig.POLE_HAG_MIN - 0.5)) &
            (hag <= InferenceConfig.POLE_HAG_MAX)
        )

        pole_idx = np.where(pole_mask)[0]
        if len(pole_idx) < 5:
            print("    CL Pole/Pylon rescue skipped: no elevated structural pool")
            return np.array([], dtype=np.int64)

        # Do not cut a wide pylon down to a 4 m tube around the conductor.
        # Keep a broad near-wire support zone, then decide at cluster level.
        if wire_confirmed:
            try:
                wire_tree = cKDTree(xyz[wire_idx, :2])
                d_wire, _ = wire_tree.query(xyz[pole_idx, :2], k=1, workers=-1)
                del wire_tree
                near_limit = max(
                    6.0,
                    min(10.0, float(self._cl_corridor_width) * 0.95),
                )
                old = len(pole_idx)
                pole_idx = pole_idx[d_wire <= near_limit]
                print(
                    f"    CL Pole/Pylon near-wire pool: {old:,} -> {len(pole_idx):,} "
                    f"within {near_limit:.1f}m"
                )
            except Exception as e:
                print(f"    CL Pole/Pylon near-wire filter skipped: {e}")

        if len(pole_idx) < 5:
            return np.array([], dtype=np.int64)

        # Wider grid joins the legs/cross-members of a lattice pylon rather
        # than treating each leg as an unrelated tiny pole cluster.
        cluster_cell = max(
            1.5,
            min(3.0, float(self._cl_corridor_width) * 0.28),
        )
        labels, n_components = self._grid_cluster_labels_power(
            xyz[pole_idx, :2],
            cell_size=cluster_cell,
        )

        kept = []
        diag_limit = 30
        diag_count = 0

        for lab in np.unique(labels):
            local = np.flatnonzero(labels == lab)
            if len(local) < max(5, InferenceConfig.POLE_CLUSTER_MIN_PTS // 2):
                continue

            global_idx = pole_idx[local]
            pts = xyz[global_idx]
            hvals = hag[global_idx]

            z_span = float(pts[:, 2].max() - pts[:, 2].min())
            hag_span = float(hvals.max() - hvals.min())
            height_span = max(z_span, hag_span)
            xy_span = float(max(
                pts[:, 0].max() - pts[:, 0].min(),
                pts[:, 1].max() - pts[:, 1].min(),
            ))

            if height_span <= 0.0:
                continue

            # Number of occupied vertical slices.  This rejects a single
            # elevated clump while keeping a structure that exists through
            # several heights.
            z0 = float(pts[:, 2].min())
            z_bins = np.floor((pts[:, 2] - z0) / 1.5).astype(np.int32)
            occupied_height_bins = int(len(np.unique(z_bins)))

            vert_support = float(np.mean(
                (vert0_all[global_idx] >= 0.22) |
                (vert1_all[global_idx] >= 0.24)
            ))
            building_fraction = float(np.mean(predictions[global_idx] == self.BUILDING))
            aspect = height_span / max(xy_span, 0.25)

            # A real pole/pylon should normally meet a conductor near its upper
            # structure.  Use a loose 3D attachment test so a nearby tree trunk
            # is not accepted merely because it is vertically aligned in XY.
            wire_link = not wire_confirmed
            if wire_confirmed:
                try:
                    link_tree = cKDTree(xyz[wire_idx, :2])
                    d_link, i_link = link_tree.query(pts[:, :2], k=1, workers=-1)
                    del link_tree
                    wire_z = xyz[wire_idx[np.asarray(i_link, dtype=np.int64)], 2]
                    dz_link = np.abs(pts[:, 2] - wire_z)
                    wire_link = bool(np.any((d_link <= 4.5) & (dz_link <= 5.0)))
                except Exception:
                    wire_link = True

            narrow_pole = (
                height_span >= 3.0 and
                xy_span <= 3.2 and
                occupied_height_bins >= 3 and
                (vert_support >= 0.12 or building_fraction >= 0.10) and
                wire_link
            )

            lattice_pylon = (
                height_span >= 5.0 and
                2.0 < xy_span <= 12.0 and
                occupied_height_bins >= 4 and
                aspect >= 0.55 and
                (vert_support >= 0.18 or building_fraction >= 0.05) and
                wire_link and
                len(global_idx) <= 20000
            )

            accepted_as = "POLE" if narrow_pole else "PYLON" if lattice_pylon else "REJECT"
            if diag_count < diag_limit:
                print(
                    f"    Pole/Pylon cluster {int(lab):03d}: "
                    f"pts={len(global_idx):,} height={height_span:.2f}m "
                    f"xy={xy_span:.2f}m bins={occupied_height_bins} "
                    f"vert={vert_support:.2f} build={building_fraction:.2f} "
                    f"wire_link={wire_link} aspect={aspect:.2f} -> {accepted_as}"
                )
                diag_count += 1

            if narrow_pole or lattice_pylon:
                kept.append(global_idx)

        if not kept:
            print(
                f"    CL Pole/Pylon rescue FINAL: 0 kept from "
                f"{n_components:,} clusters"
            )
            return np.array([], dtype=np.int64)

        pole_final = np.unique(np.concatenate(kept)).astype(np.int64)
        if len(wire_idx) > 0:
            pole_final = np.setdiff1d(pole_final, wire_idx, assume_unique=False)

        print(f"    CL Pole/Pylon rescue FINAL: {len(pole_final):,} pts")
        return pole_final


    def _prune_wire_candidates_safely(self, xyz, predictions, hag, post_features,
                                      wire_idx, cl_prior_active=False, cl_mask=None):
        """
        Final conductor cleanup before assigning the Wire class.

        Keeps the anti-vegetation safeguards while preserving parallel
        conductors across the actual user-selected CL corridor.
        """
        wire_idx = np.asarray(wire_idx, dtype=np.int64)
        if len(wire_idx) == 0:
            return wire_idx

        before = len(wire_idx)

        keep = (
            (wire_idx >= 0) &
            (wire_idx < len(xyz)) &
            (hag[wire_idx] >= max(2.0, float(self._wire_hag_min) - 1.0)) &
            (hag[wire_idx] <= float(self._wire_hag_max))
        )
        wire_idx = wire_idx[keep]

        if len(wire_idx) == 0:
            print(f"    Wire safe prune: {before:,} -> 0")
            return wire_idx

        cand_station = None
        cand_d = None
        if (
            cl_prior_active and
            self._cl_corridor_coords is not None and
            len(self._cl_corridor_coords) >= 2
        ):
            cand_d, cand_station = self._polyline_distance_station_xy(
                xyz[wire_idx],
                self._cl_corridor_coords,
            )
            # For +/-9 m, retain up to about +/-5.9 m.  The old hard cap of
            # +/-2.8 m was deleting parallel conductors.
            max_wire_width = max(
                3.5,
                min(6.0, float(self._cl_corridor_width) * 0.65),
            )
            max_wire_width = min(max_wire_width, float(self._cl_corridor_width))
            keep = cand_d <= max_wire_width
            wire_idx = wire_idx[keep]
            cand_station = cand_station[keep]
            cand_d = cand_d[keep]
            print(f"    Wire lateral corridor kept within +/-{max_wire_width:.1f}m")

        if len(wire_idx) == 0:
            print(f"    Wire safe prune: {before:,} -> 0")
            return wire_idx

        try:
            t = cKDTree(xyz[wire_idx])
            density = np.asarray(
                t.query_ball_point(
                    xyz[wire_idx],
                    r=0.80,
                    workers=-1,
                    return_length=True,
                ),
                dtype=np.int32,
            ) - 1
            del t
        except Exception:
            density = np.zeros(len(wire_idx), dtype=np.int32)

        lin0 = post_features[wire_idx, self._PF_LIN_S0].astype(np.float32)
        lin1 = post_features[wire_idx, self._PF_LIN_S1].astype(np.float32)
        plan0 = post_features[wire_idx, self._PF_PLAN_S0].astype(np.float32)
        was_building = predictions[wire_idx] == self.BUILDING

        density_limit = max(14, min(36, int(self._wire_density_max) * 2 + 8))
        normal_geom = (
            (lin0 >= 0.25) |
            (lin1 >= 0.30) |
            ((plan0 <= 0.40) & (density <= density_limit))
        )
        building_geom = (
            ((lin0 >= 0.62) | (lin1 >= 0.68)) &
            (plan0 <= 0.28) &
            (density <= min(20, density_limit))
        )
        geom_ok = np.where(was_building, building_geom, normal_geom)

        keep = (density <= density_limit) & geom_ok
        wire_idx = wire_idx[keep]
        density = density[keep]
        lin0 = lin0[keep]
        lin1 = lin1[keep]
        plan0 = plan0[keep]
        if cand_station is not None:
            cand_station = cand_station[keep]
            cand_d = cand_d[keep]

        if len(wire_idx) == 0:
            print(f"    Wire safe prune: {before:,} -> 0")
            return wire_idx

        if cand_station is not None and len(cand_station) == len(wire_idx):
            score = (
                np.clip(lin0, 0.0, 1.0) +
                np.clip(lin1, 0.0, 1.0) +
                np.clip(1.0 - plan0, 0.0, 1.0) +
                1.0 / (cand_d.astype(np.float32) + 1.0) +
                1.0 / (density.astype(np.float32) + 1.0)
            )
            bins = np.floor(
                (cand_station - float(np.nanmin(cand_station))) / 2.0
            ).astype(np.int64)
            kept_parts = []
            # Enough capacity for several parallel phase/earth conductors.
            max_per_bin = 100
            for b in np.unique(bins):
                loc = np.flatnonzero(bins == b)
                if len(loc) <= max_per_bin:
                    kept_parts.append(loc)
                else:
                    kept_parts.append(
                        loc[np.argpartition(score[loc], -max_per_bin)[-max_per_bin:]]
                    )
            if kept_parts:
                keep_local = np.concatenate(kept_parts)
                wire_idx = wire_idx[keep_local]
                cand_station = cand_station[keep_local]
                run_mask = self._long_station_runs(
                    cand_station,
                    bin_size=2.0,
                    min_run_m=8.0,
                    max_gap_bins=3,
                )
                wire_idx = wire_idx[run_mask]

        wire_idx = np.unique(wire_idx).astype(np.int64)
        print(f"    Wire safe prune: {before:,} -> {len(wire_idx):,}")
        return wire_idx


    def _detect_power_lines(self, xyz, predictions, hag, post_features):
        report = {
            'wire_candidates': 0,
            'wires_final': 0,
            'wire_hard_rescue': 0,
            'pole_candidates': 0,
            'poles_final': 0,
            'pole_hard_rescue': 0,
            'cl_prior_points': 0,
        }

        cfg = InferenceConfig
        wire_hag_min = float(self._wire_hag_min)
        wire_hag_max = float(self._wire_hag_max)
        wire_chain_radius = float(self._wire_chain_radius)
        wire_density_max = int(self._wire_density_max)
        wire_min_seg = int(self._wire_min_segment_pts)
        wire_linearity_min = float(self._wire_linearity_min)
        wire_planarity_max = float(cfg.WIRE_PLANARITY_MAX)

        print(
            f"    Wire params: HAG={wire_hag_min}-{wire_hag_max}m  "
            f"chain_r={wire_chain_radius}m  density_max={wire_density_max}  "
            f"min_seg={wire_min_seg}  lin_min={wire_linearity_min}"
        )

        cl_prior_active = False
        cl_mask = None

        if (
            self._use_cl_powerline_prior and
            self._cl_corridor_coords is not None and
            self._cl_corridor_width > 0.0
        ):
            cl_mask = self._points_near_polyline_xy(
                xyz,
                self._cl_corridor_coords,
                self._cl_corridor_width,
            )
            n_cl = int(cl_mask.sum())
            report['cl_prior_points'] = n_cl
            if n_cl > 0:
                cl_prior_active = True
                print(
                    f"    CL corridor prior ACTIVE: "
                    f"{n_cl:,}/{len(xyz):,} pts inside +/-{self._cl_corridor_width:.1f}m"
                )
            else:
                print("    CL corridor prior found 0 points - using normal power-line search")

        # STEP 1: normal/relaxed wire pass.
        if cl_prior_active:
            # CL mode must be relaxed enough to catch wires, but not so relaxed
            # that nearby vegetation/ground becomes red.
            effective_lin_min = max(0.50, wire_linearity_min - 0.22)
            effective_plan_max = max(0.42, wire_planarity_max + 0.18)
            effective_density_max = max(wire_density_max, 24)
            effective_chain_radius = max(wire_chain_radius, 3.0)
            effective_min_seg = max(10, min(wire_min_seg, 24))
            effective_hag_min = max(2.0, wire_hag_min - 1.0)
            hag_consistency_max = 2.0
        else:
            effective_lin_min = wire_linearity_min
            effective_plan_max = wire_planarity_max
            effective_density_max = wire_density_max
            effective_chain_radius = wire_chain_radius
            effective_min_seg = wire_min_seg
            effective_hag_min = wire_hag_min
            hag_consistency_max = 1.0

        wire_pool_mask = (
            np.isin(predictions, [self.LOWVEG, self.MIDVEG, self.HIGHVEG]) &
            (predictions != self.BUILDING) &
            (hag >= effective_hag_min) &
            (hag <= wire_hag_max)
        )

        if cl_prior_active and cl_mask is not None:
            wire_pool_mask &= cl_mask

        wire_pool_idx = np.where(wire_pool_mask)[0]
        wire_idx = np.array([], dtype=np.int64)

        if len(wire_pool_idx) >= 6:
            lin_s0 = post_features[wire_pool_idx, self._PF_LIN_S0]
            plan_s0 = post_features[wire_pool_idx, self._PF_PLAN_S0]
            lin_s1 = post_features[wire_pool_idx, self._PF_LIN_S1]

            geom_pass = (
                ((lin_s0 >= effective_lin_min) & (plan_s0 <= effective_plan_max)) |
                (lin_s1 >= effective_lin_min)
            )

            geom_idx = wire_pool_idx[geom_pass]
            report['wire_candidates'] = int(len(geom_idx))
            print(f"    Wire candidates after relaxed geom filter: {len(geom_idx):,}")

            if len(geom_idx) >= 3:
                try:
                    tree_cand = cKDTree(xyz[geom_idx])
                    counts = np.asarray(
                        tree_cand.query_ball_point(
                            xyz[geom_idx],
                            r=0.5,
                            workers=-1,
                            return_length=True,
                        ),
                        dtype=np.int32,
                    ) - 1
                    del tree_cand

                    sparse_idx = geom_idx[counts <= effective_density_max]
                    print(f"    Wire after SAFE density filter: {len(sparse_idx):,}")
                except Exception as e:
                    print(f"    Wire density filter skipped: {e}")
                    sparse_idx = geom_idx

                if len(sparse_idx) >= 3:
                    try:
                        tree_sparse = cKDTree(xyz[sparse_idx])
                        chain_counts = np.asarray(
                            tree_sparse.query_ball_point(
                                xyz[sparse_idx],
                                r=effective_chain_radius,
                                workers=-1,
                                return_length=True,
                            ),
                            dtype=np.int32,
                        ) - 1
                        del tree_sparse

                        chain_idx = sparse_idx[chain_counts >= 1]
                        print(f"    Wire after chain filter: {len(chain_idx):,}")
                    except Exception as e:
                        print(f"    Wire chain filter skipped: {e}")
                        chain_idx = sparse_idx

                    if len(chain_idx) >= effective_min_seg:
                        try:
                            k = min(10, len(chain_idx))
                            tree_chain = cKDTree(xyz[chain_idx])
                            _, nn_idx = tree_chain.query(xyz[chain_idx], k=k, workers=-1)
                            del tree_chain
                            if k == 1:
                                nn_idx = nn_idx.reshape(-1, 1)
                            nb_hag = hag[chain_idx][nn_idx]
                            hag_ok = (nb_hag.max(axis=1) - nb_hag.min(axis=1)) <= hag_consistency_max
                            wire_idx = chain_idx[hag_ok]
                        except Exception as e:
                            print(f"    Wire HAG consistency skipped: {e}")
                            wire_idx = chain_idx

                        print(f"    Wire FINAL normal/relaxed: {len(wire_idx):,} pts")
        else:
            print(
                f"    Wire: no pool candidates in HAG "
                f"{effective_hag_min:.2f}-{wire_hag_max:.2f}m"
            )

        # STEP 1B: hard CL rescue. This is the important fix for your log.
        if cl_prior_active and cl_mask is not None:
            hard_wire_idx = self._cl_hard_wire_rescue(
                xyz=xyz,
                predictions=predictions,
                hag=hag,
                post_features=post_features,
                cl_mask=cl_mask,
                existing_wire_idx=wire_idx,
            )

            report['wire_hard_rescue'] = int(len(hard_wire_idx))

            if len(hard_wire_idx) > 0:
                if len(wire_idx) > 0:
                    wire_idx = np.unique(np.concatenate([wire_idx, hard_wire_idx])).astype(np.int64)
                else:
                    wire_idx = np.unique(hard_wire_idx).astype(np.int64)

        if len(wire_idx) > 0:
            wire_idx = self._prune_wire_candidates_safely(
                xyz=xyz,
                predictions=predictions,
                hag=hag,
                post_features=post_features,
                wire_idx=wire_idx,
                cl_prior_active=cl_prior_active,
                cl_mask=cl_mask,
            )

        if len(wire_idx) > 0:
            predictions[wire_idx] = cfg.WIRE_INTERNAL_CODE

        report['wires_final'] = int(len(wire_idx))
        print(f"    Wire FINAL after all passes: {len(wire_idx):,} pts")

        # STEP 2: hard CL pole rescue first. It does not depend on verticality columns.
        pole_idx = np.array([], dtype=np.int64)
        if cl_prior_active and cl_mask is not None:
            hard_pole_idx = self._cl_hard_pole_rescue(
                xyz=xyz,
                predictions=predictions,
                hag=hag,
                post_features=post_features,
                cl_mask=cl_mask,
                wire_idx=wire_idx,
            )
            report['pole_hard_rescue'] = int(len(hard_pole_idx))
            if len(hard_pole_idx) > 0:
                pole_idx = np.unique(hard_pole_idx).astype(np.int64)

        # STEP 3: normal pole pass as backup.
        pole_pool_mask = (
            np.isin(predictions, [self.BUILDING, self.MIDVEG, self.HIGHVEG]) &
            (hag >= max(0.8, cfg.POLE_HAG_MIN - 0.7)) &
            (hag <= cfg.POLE_HAG_MAX)
        )

        if cl_prior_active and cl_mask is not None:
            pole_pool_mask &= cl_mask

        pole_pool_idx = np.where(pole_pool_mask)[0]
        print(f"    Pole pool size: {len(pole_pool_idx):,}")

        if len(pole_pool_idx) >= max(4, cfg.POLE_CLUSTER_MIN_PTS // 2):
            if len(wire_idx) > 0:
                try:
                    tree_wire = cKDTree(xyz[wire_idx, :2])
                    d_wire, _ = tree_wire.query(xyz[pole_pool_idx, :2], k=1, workers=-1)
                    del tree_wire
                    pole_near_limit = max(3.0, min(8.0, float(self._cl_corridor_width) * 0.75)) if cl_prior_active else cfg.POLE_WIRE_PROXIMITY
                    pole_pool_idx = pole_pool_idx[d_wire <= pole_near_limit]
                except Exception as e:
                    print(f"    Pole wire prefilter skipped: {e}")

            if len(pole_pool_idx) >= max(4, cfg.POLE_CLUSTER_MIN_PTS // 2):
                vert_s0 = post_features[pole_pool_idx, self._PF_VERT_S0]
                vert_s1 = post_features[pole_pool_idx, self._PF_VERT_S1]
                vert_min = 0.42 if cl_prior_active else cfg.POLE_VERTICALITY_MIN

                vert_pass = (vert_s0 >= vert_min) | (vert_s1 >= max(0.40, vert_min - 0.08))
                pole_cand_idx = pole_pool_idx[vert_pass]
                report['pole_candidates'] = int(len(pole_cand_idx))
                print(f"    Pole candidates after relaxed vert filter: {len(pole_cand_idx):,}")

                if len(pole_cand_idx) >= max(4, cfg.POLE_CLUSTER_MIN_PTS // 2):
                    labels, _ = self._grid_cluster_labels_power(
                        xyz[pole_cand_idx, :2],
                        cell_size=max(0.8, min(1.5, cfg.POLE_2D_RADIUS)),
                    )

                    normal_poles = []
                    for lab in np.unique(labels):
                        local = np.flatnonzero(labels == lab)
                        if len(local) < max(4, cfg.POLE_CLUSTER_MIN_PTS // 2):
                            continue
                        global_idx = pole_cand_idx[local]
                        pts = xyz[global_idx]
                        z_span = float(pts[:, 2].max() - pts[:, 2].min())
                        h_span = float(hag[global_idx].max() - hag[global_idx].min())
                        xy_span = float(max(
                            pts[:, 0].max() - pts[:, 0].min(),
                            pts[:, 1].max() - pts[:, 1].min(),
                        ))
                        height_span = max(z_span, h_span)
                        z0 = float(pts[:, 2].min())
                        z_bins = np.floor((pts[:, 2] - z0) / 1.5).astype(np.int32)
                        occupied_height_bins = int(len(np.unique(z_bins)))
                        aspect = height_span / max(xy_span, 0.25)
                        building_fraction = float(np.mean(predictions[global_idx] == self.BUILDING))

                        narrow_pole = (
                            height_span >= (3.0 if cl_prior_active else 2.5) and
                            xy_span <= (3.2 if cl_prior_active else 4.5) and
                            occupied_height_bins >= 3
                        )
                        lattice_pylon = (
                            cl_prior_active and
                            height_span >= 5.0 and
                            2.0 < xy_span <= 12.0 and
                            occupied_height_bins >= 4 and
                            aspect >= 0.55 and
                            building_fraction >= 0.03
                        )

                        if narrow_pole or lattice_pylon:
                            normal_poles.append(global_idx)

                    if normal_poles:
                        normal_poles = np.unique(np.concatenate(normal_poles)).astype(np.int64)
                        if len(pole_idx) > 0:
                            pole_idx = np.unique(np.concatenate([pole_idx, normal_poles])).astype(np.int64)
                        else:
                            pole_idx = normal_poles

        if len(pole_idx) > 0:
            # Do not allow pole overwrite of rescued wire points.
            pole_idx = np.setdiff1d(pole_idx, wire_idx, assume_unique=False)
            if len(pole_idx) > 0:
                predictions[pole_idx] = cfg.POLE_INTERNAL_CODE

        report['poles_final'] = int(len(pole_idx))
        print(f"    Pole FINAL after all passes: {len(pole_idx):,} pts")

        return predictions, report
