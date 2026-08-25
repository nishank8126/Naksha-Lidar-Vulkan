"""
Memory utilities for periodic cleanup and memory pressure handling.
"""

from __future__ import annotations

import gc
import os
import time

from PySide6.QtCore import QObject, QTimer

try:
    import psutil
except Exception:  # pragma: no cover - optional at runtime
    psutil = None


MAX_UNDO_STEPS = 20
GC_INTERVAL_MS = 30_000
RAM_CHECK_MS = 30_000
RAM_WARN_PERCENT = 80
MAX_PENDING_MAIN_VIEW_CHUNKS = 128


class ObserverRegistry:
    """Central store for VTK interactor observer IDs.

    Keyed by weakref so that a recycled memory address (same id() on a NEW
    object after the old one is GC'd) never silently maps to a stale entry.
    Dead entries are pruned lazily on every mutating call.
    """

    # key: weakref.ref(interactor) → {"ids": [...], "tags": [...]}
    _registry: dict = {}

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #
    @classmethod
    def _prune_dead(cls) -> None:
        """Remove entries whose interactor has been GC'd."""
        dead = [ref for ref in list(cls._registry) if ref() is None]
        for ref in dead:
            del cls._registry[ref]

    @classmethod
    def _find_entry(cls, interactor):
        """Return (ref, entry) for a live interactor, or (None, None)."""
        for ref, entry in cls._registry.items():
            if ref() is interactor:
                return ref, entry
        return None, None

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #
    @classmethod
    def track(cls, interactor, observer_id: int, tag: str = "") -> None:
        if interactor is None or observer_id is None:
            return
        cls._prune_dead()
        _, entry = cls._find_entry(interactor)
        if entry is None:
            import weakref
            ref = weakref.ref(interactor)
            entry = {"ids": [], "tags": []}
            cls._registry[ref] = entry
        entry["ids"].append(observer_id)
        entry["tags"].append(tag)

    @classmethod
    def release(cls, interactor) -> None:
        if interactor is None:
            return
        cls._prune_dead()
        ref, entry = cls._find_entry(interactor)
        if entry is None:
            return
        del cls._registry[ref]
        removed = 0
        for observer_id in entry["ids"]:
            try:
                interactor.RemoveObserver(observer_id)
                removed += 1
            except Exception:
                pass
        if removed:
            print(f"[MEM] ObserverRegistry removed {removed} observer(s)")

    @classmethod
    def release_all(cls) -> None:
        for ref, entry in list(cls._registry.items()):
            interactor = ref()
            if interactor is None:
                continue
            for observer_id in entry.get("ids", []):
                try:
                    interactor.RemoveObserver(observer_id)
                except Exception:
                    pass
        cls._registry.clear()

    @classmethod
    def count(cls) -> int:
        cls._prune_dead()
        return sum(len(entry["ids"]) for entry in cls._registry.values())


def _free_undo_entry(entry: dict) -> None:
    if not isinstance(entry, dict):
        return
    for key in ("mask", "indices", "old_classes", "new_classes", "old", "new", "classification"):
        arr = entry.pop(key, None)
        if arr is not None:
            del arr


def trim_undo_stack(app, max_steps: int = MAX_UNDO_STEPS) -> None:
    """Trim undo/redo stacks and free arrays from dropped entries."""
    undo = getattr(app, "undo_stack", None)
    if not isinstance(undo, list):
        return

    redo = getattr(app, "redo_stack", None)
    if isinstance(redo, list):
        while len(redo) > max_steps:
            _free_undo_entry(redo.pop(0))

    while len(undo) > max_steps:
        _free_undo_entry(undo.pop(0))


def _cloud_point_count(app) -> int:
    """Best-effort point count for adaptive memory policies."""
    try:
        data = getattr(app, "data", None)
        if isinstance(data, dict):
            xyz = data.get("xyz")
            if xyz is not None:
                return int(len(xyz))
    except Exception:
        pass
    return 0


def _recommended_undo_steps(app, ram_pct: float) -> int:
    """
    Adaptive undo depth:
    - large clouds keep fewer snapshots to prevent long-session RAM growth
    - tighten further under RAM pressure
    """
    n_points = _cloud_point_count(app)

    if n_points >= 12_000_000:
        steps = 12
    elif n_points >= 8_000_000:
        steps = 15
    elif n_points >= 4_000_000:
        steps = 20
    else:
        steps = MAX_UNDO_STEPS

    if ram_pct >= 80:
        steps = min(steps, 6)
    elif ram_pct >= 70:
        steps = min(steps, 10)

    return max(4, int(steps))


def current_recommended_undo_steps(app) -> int:
    """
    Runtime-safe undo depth recommendation using current RAM pressure when
    available, with point-count fallback.
    """
    ram_pct = 0.0
    try:
        ram_pct = float(getattr(app, "_last_ram_pct", 0.0) or 0.0)
    except Exception:
        ram_pct = 0.0

    if ram_pct <= 0.0 and psutil is not None:
        try:
            ram_pct = float(psutil.virtual_memory().percent)
        except Exception:
            ram_pct = 0.0

    return _recommended_undo_steps(app, ram_pct)


def trim_pending_main_view_chunks(app, max_chunks: int = MAX_PENDING_MAIN_VIEW_CHUNKS) -> None:
    """
    Prevent unbounded growth of deferred main-view color update chunks.
    Older chunks are stale once newer edits supersede them.
    """
    chunks = getattr(app, "_pending_main_view_index_chunks", None)
    if not isinstance(chunks, list):
        return
    if len(chunks) <= max_chunks:
        return
    del chunks[:-max_chunks]


def trim_interactor_temp_buffers(app) -> None:
    """
    Release temporary per-interaction buffers when no drag is active.
    These buffers are meant to be short-lived but can remain allocated after
    interrupted workflows; clearing them prevents long-session memory creep.
    """
    interactors = []

    inter_map = getattr(app, "classify_interactors", None)
    if isinstance(inter_map, dict):
        interactors.extend([i for i in inter_map.values() if i is not None])

    inter_single = getattr(app, "classify_interactor", None)
    if inter_single is not None:
        interactors.append(inter_single)

    if not interactors:
        return

    # De-duplicate object identities
    seen = set()
    unique = []
    for inter in interactors:
        key = id(inter)
        if key in seen:
            continue
        seen.add(key)
        unique.append(inter)

    for inter in unique:
        try:
            if getattr(inter, "is_dragging", False):
                continue
        except Exception:
            continue

        for attr in (
            "_brush_indices_arrays",
            "_brush_old_classes_arrays",
            "_brush_frame_chunks",
            "_brush_stroke_positions",
        ):
            try:
                value = getattr(inter, attr, None)
                if isinstance(value, list) and value:
                    value.clear()
            except Exception:
                pass

        for attr in (
            "_brush_accumulated_mask",
            "_brush_section_local_mask",
            "_brush_section_pts2d",
            "_brush_section_indices",
            "_last_brush_center",
            "_last_brush_center_uv",
            "_last_brush_preview_P2",
            "_last_rectangle_preview_P2",
            "_last_circle_preview_P2",
            "_last_line_preview_P2",
        ):
            try:
                if hasattr(inter, attr):
                    setattr(inter, attr, None)
            except Exception:
                pass

def prune_stale_classification_interactors(app) -> int:
    """
    If classification is not active, aggressively release lingering interactor
    wrappers and their worker/timer resources.
    """
    if getattr(app, "active_classify_tool", None):
        return 0

    pruned = 0

    inter_map = getattr(app, "classify_interactors", None)
    if isinstance(inter_map, dict) and inter_map:
        for key, inter in list(inter_map.items()):
            try:
                if inter is not None and hasattr(inter, "cleanup"):
                    inter.cleanup()
            except Exception:
                pass
            try:
                del inter_map[key]
            except Exception:
                pass
            pruned += 1

    for attr in ("classify_interactor", "cut_classify_interactor"):
        inter = getattr(app, attr, None)
        if inter is None:
            continue
        try:
            if hasattr(inter, "cleanup"):
                inter.cleanup()
        except Exception:
            pass
        try:
            setattr(app, attr, None)
        except Exception:
            pass
        pruned += 1

    return pruned


def _release_vtk_actor_resources(actor) -> None:
    """Drop heavy VTK refs (texture/mapper/input data) from an actor."""
    if actor is None:
        return
    try:
        texture = actor.GetTexture()
        if texture is not None:
            try:
                texture.SetInputData(None)
            except Exception:
                pass
            try:
                actor.SetTexture(None)
            except Exception:
                pass
    except Exception:
        pass

    try:
        mapper = actor.GetMapper()
        if mapper is not None:
            try:
                mapper.SetInputData(None)
            except Exception:
                pass
            try:
                actor.SetMapper(None)
            except Exception:
                pass
    except Exception:
        pass


def release_data_arrays(app) -> None:
    """Release project arrays and memory-heavy structures."""
    data = getattr(app, "data", None)
    if isinstance(data, dict):
        for key in list(data.keys()):
            value = data.pop(key, None)
            if value is not None:
                del value
        del data
    app.data = None

    if hasattr(app, "spatial_index") and app.spatial_index is not None:
        try:
            del app.spatial_index.tree
            del app.spatial_index.xyz
        except Exception:
            pass
        _si = app.spatial_index
        app.spatial_index = None
        del _si          # drops last Python ref → triggers C destructor now
        gc.collect()     # flush C-heap immediately, don't wait for next GC cycle

    for attr in ("current_gpu_indices", "current_visibility_mask"):
        if hasattr(app, attr):
            setattr(app, attr, None)

    for stack_name in ("undo_stack", "redo_stack"):
        stack = getattr(app, stack_name, None)
        if isinstance(stack, list):
            for entry in stack:
                _free_undo_entry(entry)
            stack.clear()

    geotiff_actors = getattr(app, "geotiff_actors", None)
    if isinstance(geotiff_actors, list) and geotiff_actors:
        renderer = None
        vtk_widget = getattr(app, "vtk_widget", None)
        if vtk_widget is not None:
            renderer = getattr(vtk_widget, "renderer", None)
            if renderer is None:
                try:
                    renderer = vtk_widget.GetRenderWindow().GetRenderers().GetFirstRenderer()
                except Exception:
                    renderer = None

        released = 0
        for actor in list(geotiff_actors):
            try:
                if renderer is not None:
                    renderer.RemoveActor(actor)
            except Exception:
                pass
            _release_vtk_actor_resources(actor)
            released += 1

        geotiff_actors.clear()
        app.geotiff_actors = []
        if released:
            print(f"[MEM] Released {released} GeoTIFF texture actor(s)")

    print("[MEM] Data arrays explicitly released")


def cleanup_stale_actors(app) -> int:
    """Remove orphaned class actors from section views."""
    cleaned = 0
    section_vtks = getattr(app, "section_vtks", None)
    if not isinstance(section_vtks, dict):
        return cleaned

    for view_idx, vtk_widget in section_vtks.items():
        if vtk_widget is None or not hasattr(vtk_widget, "actors"):
            continue

        core_mask = getattr(app, f"section_{view_idx}_core_mask", None)
        if core_mask is not None:
            continue

        try:
            actor_names = list(vtk_widget.actors.keys())
        except Exception:
            continue

        for name in actor_names:
            if not str(name).startswith("class_"):
                continue
            try:
                # Grab actor before removal so we can explicitly release
                # its VTK mapper + polydata after the renderer drops it.
                actor = vtk_widget.actors.get(name)
                vtk_widget.remove_actor(name, render=False)
                if actor is not None:
                    try:
                        mapper = actor.GetMapper()
                        if mapper is not None:
                            mapper.SetInputData(None)  # drop polydata ref
                            actor.SetMapper(None)      # drop mapper ref
                            del mapper                 # release Python ref → VTK C++ ref count → 0
                    except Exception:
                        pass
                    del actor
                cleaned += 1
            except Exception:
                pass
    if cleaned:
        print(f"[MEM] Cleaned {cleaned} stale actor(s) from section views")
        gc.collect()   # flush VTK C++ ref counts immediately
    return cleaned


def count_all_actors(app) -> int:
    total = 0

    main = getattr(app, "vtk_widget", None)
    if main is not None and hasattr(main, "actors"):
        total += len(main.actors)

    section_vtks = getattr(app, "section_vtks", None)
    if isinstance(section_vtks, dict):
        for view in section_vtks.values():
            if view is not None and hasattr(view, "actors"):
                total += len(view.actors)

    cut_ctrl = getattr(app, "cut_section_controller", None)
    if cut_ctrl is not None:
        cut_vtk = getattr(cut_ctrl, "cut_vtk", None)
        if cut_vtk is not None and hasattr(cut_vtk, "actors"):
            total += len(cut_vtk.actors)

    return total


class MemoryLeakGuard(QObject):
    """
    Periodic memory maintenance:
    - gc.collect() every 15s
    - RAM check every 15s (warn if above threshold)
    - Undo stack trim
    - Stale actor cleanup

    ``app`` is stored as a weakref so the guard never prevents the main
    window from being freed during teardown.  Qt parent is still ``app``
    so the guard's lifetime is correctly bound to the window.
    """

    def __init__(self, app):
        import weakref
        super().__init__(app)
        self._app_ref = weakref.ref(app)   # weak — does not keep app alive
        self._gc_timer = QTimer(self)
        self._ram_timer = QTimer(self)
        self._process = psutil.Process(os.getpid()) if psutil is not None else None
        self._gc_timer.timeout.connect(self._run_gc)
        self._ram_timer.timeout.connect(self._check_ram)
        self._last_gc_freed = 0

    # ------------------------------------------------------------------
    @property
    def app(self):
        """Return the live app, or None if it has been destroyed."""
        return self._app_ref()

    def start(self) -> None:
        self._gc_timer.start(GC_INTERVAL_MS)
        if psutil is not None:
            self._ram_timer.start(RAM_CHECK_MS)
        print(
            f"[MEM] MemoryLeakGuard started (GC every {GC_INTERVAL_MS // 1000}s, "
            f"adaptive undo <= {MAX_UNDO_STEPS})"
        )

    def stop(self) -> None:
        self._gc_timer.stop()
        self._ram_timer.stop()

    def force_gc(self) -> None:
        self._run_gc(verbose=True)

    def _run_gc(self, verbose: bool = False) -> None:
        try:
            app = self.app
            if app is None:       # window already destroyed — stop silently
                self.stop()
                return

            # Defer GC while the user is actively classifying — a gen-2
            # collection on a 5+ GB process can stall 100–300 ms and produce
            # the random "every ~30s one click feels slow" spike. We skip the
            # tick if a classification happened in the last 2 s. Skipping is
            # bounded: the timer fires again 30 s later, and emergency RAM
            # checks run on their own _ram_timer independently. Manual
            # force_gc() calls (verbose=True) always proceed.
            if not verbose:
                last_classify = getattr(app, "_last_classify_ts", 0.0)
                if last_classify and (time.time() - last_classify) < 2.0:
                    return

            ram_pct = self._get_ram_pct()
            app._last_ram_pct = ram_pct
            app._max_undo_steps = current_recommended_undo_steps(app)
            trim_undo_stack(app, app._max_undo_steps)
            trim_pending_main_view_chunks(app)
            trim_interactor_temp_buffers(app)
            pruned = prune_stale_classification_interactors(app)

            if ram_pct >= 85:
                # Emergency posture for very long sessions under pressure.
                trim_undo_stack(app, 4)
                redo = getattr(app, "redo_stack", None)
                if isinstance(redo, list):
                    while redo:
                        _free_undo_entry(redo.pop(0))

            cleanup_stale_actors(app)


            freed = gc.collect(generation=2)
            self._last_gc_freed = freed

            if verbose or freed > 0:
                ram_mb = 0
                if self._process is not None:
                    ram_mb = self._process.memory_info().rss // 1024**2
                actors = count_all_actors(app)
                observers = ObserverRegistry.count()
                undo_depth = len(getattr(app, "undo_stack", []))
                print(
                    f"[MEM] GC: freed {freed} objects | RAM: {ram_mb} MB | "
                    f"Actors: {actors} | Undo: {undo_depth} | Observers: {observers} | "
                    f"Pruned classify: {pruned}"
                )
        except Exception as exc:
            print(f"[MEM] MemoryLeakGuard GC error: {exc}")

    def _get_ram_pct(self) -> float:
        if psutil is None or self._process is None:
            return 0.0
        try:
            proc_mb = self._process.memory_info().rss // 1024**2
            total_mb = psutil.virtual_memory().total // 1024**2
            return (proc_mb / total_mb) * 100
        except Exception:
            return 0.0

    def _check_ram(self) -> None:
        if psutil is None or self._process is None:
            return
        try:
            app = self.app
            if app is None:
                self.stop()
                return
            pct = self._get_ram_pct()
            if pct < RAM_WARN_PERCENT:
                return
            proc_mb = self._process.memory_info().rss // 1024**2
            total_mb = psutil.virtual_memory().total // 1024**2
            print(f"[MEM] HIGH RAM: {proc_mb} MB / {total_mb} MB ({pct:.1f}%)")
            if hasattr(app, "statusBar"):
                app.statusBar().showMessage(
                    f"High memory: {proc_mb} MB - consider clearing project", 8000
                )

        except Exception:
            pass

    def get_stats(self) -> dict:
        try:
            app = self.app
            if self._process is not None and psutil is not None:
                proc_mb = self._process.memory_info().rss // 1024**2
                total_mb = psutil.virtual_memory().total // 1024**2
                ram_pct = round((proc_mb / total_mb) * 100, 1)
            else:
                proc_mb = 0
                total_mb = 0
                ram_pct = 0.0

            return {
                "ram_mb": proc_mb,
                "ram_total_mb": total_mb,
                "ram_pct": ram_pct,
                "undo_depth": len(getattr(app, "undo_stack", [])) if app else 0,
                "redo_depth": len(getattr(app, "redo_stack", [])) if app else 0,
                "live_observers": ObserverRegistry.count(),
                "total_actors": count_all_actors(self.app),
                "last_gc_freed": self._last_gc_freed,
            }
        except Exception:
            return {}
