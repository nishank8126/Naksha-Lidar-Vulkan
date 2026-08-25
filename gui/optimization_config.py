"""
Optimization Configuration - Feature Flags

Controls which optimizations are active. 
Set ENABLE_OPTIMIZED_REFRESH = False to use original code.
"""

# ═══════════════════════════════════════════════════════════════════════
# MASTER SWITCH - Set to False to disable ALL optimizations
# ═══════════════════════════════════════════════════════════════════════
ENABLE_OPTIMIZED_REFRESH = True

# ═══════════════════════════════════════════════════════════════════════
# INDIVIDUAL FEATURE FLAGS
# ═══════════════════════════════════════════════════════════════════════

# Skip weight sync if weights haven't changed
ENABLE_DELTA_WEIGHT_SYNC = True

# Only refresh views that contain changed points
ENABLE_DIRTY_VIEW_TRACKING = True

# Update actors in-place instead of recreating
ENABLE_INPLACE_ACTOR_UPDATE = True

# Single render pass instead of multiple
ENABLE_BATCHED_RENDERING = True

# Use stable actor names (no weight suffix)
ENABLE_STABLE_ACTOR_NAMES = True

# Render every loaded point in the settled main view. The existing temporary
# interaction LOD may still be used while actively panning or zooming.
MAIN_VIEW_RENDER_ALL_POINTS = True

# Single-class shading local-patch safety limits.  These are deliberately
# conservative: exceeding any limit falls back to the existing full rebuild.
SINGLE_CLASS_PATCH_MAX_CHANGED_POINTS = 100_000
SINGLE_CLASS_PATCH_MAX_CHANGED_RATIO = 0.03
SINGLE_CLASS_PATCH_MAX_EXISTING_VERTICES = 300_000
SINGLE_CLASS_PATCH_MAX_FACES = 600_000
SINGLE_CLASS_PATCH_MAX_AREA_RATIO = 0.08
SINGLE_CLASS_PATCH_MARGIN_FACTOR = 8.0
SINGLE_CLASS_COMPACTION_THRESHOLD = 0.20

# ═══════════════════════════════════════════════════════════════════════
# DEBUGGING
# ═══════════════════════════════════════════════════════════════════════

# Print timing information
ENABLE_PERFORMANCE_LOGGING = True

# Print detailed state changes
ENABLE_DEBUG_LOGGING = False


def is_optimization_enabled() -> bool:
    """Check if optimization is enabled."""
    return ENABLE_OPTIMIZED_REFRESH


def get_active_optimizations() -> dict:
    """Get dictionary of active optimizations."""
    return {
        'delta_weight_sync': ENABLE_DELTA_WEIGHT_SYNC,
        'dirty_view_tracking': ENABLE_DIRTY_VIEW_TRACKING,
        'inplace_actor_update': ENABLE_INPLACE_ACTOR_UPDATE,
        'batched_rendering': ENABLE_BATCHED_RENDERING,
        'stable_actor_names': ENABLE_STABLE_ACTOR_NAMES,
    }
