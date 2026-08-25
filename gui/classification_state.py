"""
Classification State Tracker - Dirty Flag System

This is a NEW module that doesn't modify any existing code.
It provides state tracking that the optimizer uses.
"""

import numpy as np
from dataclasses import dataclass, field
from typing import Set, Optional, Dict, List
import time


@dataclass
class ClassificationDirtyState:
    """Tracks what changed during classification."""
    
    dirty_classes: Set[int] = field(default_factory=set)
    dirty_views: Set[int] = field(default_factory=set)
    weights_dirty: bool = False
    borders_dirty: bool = False
    last_modified: float = 0.0
    changed_mask: Optional[np.ndarray] = None
    _changed_indices: Optional[np.ndarray] = None
    _is_refreshing: bool = False
    
    def mark_classes_dirty(self, from_class: Optional[int] = None, 
                           to_class: Optional[int] = None,
                           from_classes: Optional[List[int]] = None):
        """Mark specific classes as needing refresh."""
        if from_class is not None:
            self.dirty_classes.add(int(from_class))
        if to_class is not None:
            self.dirty_classes.add(int(to_class))
        if from_classes:
            for cls in from_classes:
                if cls is not None:
                    self.dirty_classes.add(int(cls))
        self.last_modified = time.time()
    
    def mark_view_dirty(self, view_idx: int):
        """Mark a specific view as needing refresh."""
        self.dirty_views.add(view_idx)
        self.last_modified = time.time()
    
    def set_changed_mask(self, mask: np.ndarray):
        """Store the mask of changed points."""
        self.changed_mask = mask
        self._changed_indices = None
        self.last_modified = time.time()
    
    @property
    def changed_indices(self) -> Optional[np.ndarray]:
        """Get indices of changed points (cached)."""
        if self._changed_indices is None and self.changed_mask is not None:
            self._changed_indices = np.where(self.changed_mask)[0]
        return self._changed_indices
    
    def has_dirty_classes(self) -> bool:
        return len(self.dirty_classes) > 0
    
    def has_dirty_views(self) -> bool:
        return len(self.dirty_views) > 0
    
    def begin_refresh(self) -> bool:
        """Begin refresh operation. Returns False if already refreshing."""
        if self._is_refreshing:
            return False
        self._is_refreshing = True
        return True
    
    def end_refresh(self):
        """End refresh operation."""
        self._is_refreshing = False
    
    def clear(self):
        """Clear all dirty state."""
        self.dirty_classes.clear()
        self.dirty_views.clear()
        self.weights_dirty = False
        self.borders_dirty = False
        self.changed_mask = None
        self._changed_indices = None
        self._is_refreshing = False


class WeightCache:
    """Caches weight values to detect actual changes."""
    
    def __init__(self):
        self._cached_weights: Dict[int, Dict[int, float]] = {}
        self._last_sync_time: float = 0.0
    
    def get_changed_weights(self, view_palettes: Dict) -> Dict[int, Dict[int, float]]:
        """Compare current weights with cache, return only changes."""
        changes = {}
        
        for slot_idx, palette in view_palettes.items():
            if not palette:
                continue
            
            cached_slot = self._cached_weights.get(slot_idx, {})
            slot_changes = {}
            
            for class_code, info in palette.items():
                current_weight = info.get('weight', 1.0)
                cached_weight = cached_slot.get(class_code, -1.0)
                
                if cached_weight < 0 or abs(current_weight - cached_weight) > 0.001:
                    slot_changes[class_code] = current_weight
            
            if slot_changes:
                changes[slot_idx] = slot_changes
        
        return changes
    
    def update_cache(self, view_palettes: Dict):
        """Update cache with current values."""
        for slot_idx, palette in view_palettes.items():
            if not palette:
                continue
            
            if slot_idx not in self._cached_weights:
                self._cached_weights[slot_idx] = {}
            
            for class_code, info in palette.items():
                self._cached_weights[slot_idx][class_code] = info.get('weight', 1.0)
        
        self._last_sync_time = time.time()
    
    def has_changes(self, view_palettes: Dict) -> bool:
        """Quick check if any weights changed."""
        return len(self.get_changed_weights(view_palettes)) > 0
    
    def clear(self):
        """Clear the cache."""
        self._cached_weights.clear()


# Singleton instances
_global_state: Optional[ClassificationDirtyState] = None
_weight_cache: Optional[WeightCache] = None


def get_dirty_state() -> ClassificationDirtyState:
    """Get or create the global dirty state tracker."""
    global _global_state
    if _global_state is None:
        _global_state = ClassificationDirtyState()
    return _global_state


def get_weight_cache() -> WeightCache:
    """Get or create the global weight cache."""
    global _weight_cache
    if _weight_cache is None:
        _weight_cache = WeightCache()
    return _weight_cache


def reset_caches():
    """
    Reset all caches — call when loading new file.
    Also flushes the persistent spatial index and palette-resolve caches
    so stale data from the previous file cannot leak into the new session.
    """
    global _global_state, _weight_cache
    if _global_state:
        _global_state.clear()
    if _weight_cache:
        _weight_cache.clear()
    try:
        from gui.spatial_index import invalidate_cache as _si_invalidate
        _si_invalidate()
    except Exception:
        pass
    try:
        from gui.unified_actor_manager import _palette_resolve_cache
        _palette_resolve_cache.clear()
    except Exception:
        pass