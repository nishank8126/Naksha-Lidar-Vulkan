"""
VTK Pipeline Update Utilities - ENHANCED FOR BORDERS

This module provides utilities for forcing VTK rendering pipeline updates,
with special handling for border rendering (where each class has 2 actors).

When borders are enabled (border_percent > 0), each classification class
gets TWO actors:
- A black border actor (rendered first, at larger size)
- A colored point actor (rendered on top, at normal size)

This creates additional complexity in the rendering pipeline that requires
more aggressive update strategies to ensure visual consistency.

Author: Enhanced fix for border rendering glitch
Date: 2026-01-05
"""

import numpy as np


def force_vtk_pipeline_update_with_borders(app):
    """
    Force complete VTK rendering pipeline update with border actor support.
    
    When borders are enabled (border_percent > 0), each class has TWO actors:
    - A black border actor (rendered first, larger)
    - A colored point actor (rendered on top, normal size)
    
    This function ensures BOTH types of actors are properly updated and
    uses more aggressive rendering strategies when borders are detected.
    
    The function automatically detects if borders are enabled and adjusts
    its update strategy accordingly:
    - No borders: 3 render passes
    - With borders: 5 render passes + input data updates
    
    Args:
        app: Main application instance (NakshaApp)
        
    Returns:
        bool: True if update succeeded, False otherwise
        
    Example:
        >>> # After classification
        >>> force_vtk_pipeline_update_with_borders(app)
        True
    """
    try:
        vtk_widget = getattr(app, 'vtk_widget', None)
        if not vtk_widget:
            print("⚠️ force_vtk_pipeline_update: No VTK widget found")
            return False
        
        # Get renderer
        renderer = getattr(vtk_widget, 'renderer', None)
        if renderer is None and hasattr(vtk_widget, 'GetRenderWindow'):
            rw = vtk_widget.GetRenderWindow()
            if rw:
                renderer = rw.GetRenderers().GetFirstRenderer()
        
        if not renderer:
            print("⚠️ force_vtk_pipeline_update: No renderer found")
            return False
        
        # Check if borders are enabled
        border_percent = getattr(app, "point_border_percent", 0)
        border_enabled = border_percent > 0
        
        if border_enabled:
            print(f"   🔲 Border mode active ({border_percent}%) - Enhanced update")
        
        # Step 1: Force update ALL actor mappers (including borders)
        actors = renderer.GetActors()
        actors.InitTraversal()
        
        border_actors = 0
        point_actors = 0
        other_actors = 0
        
        for i in range(actors.GetNumberOfItems()):
            actor = actors.GetNextActor()
            if actor:
                # Get actor name to identify type (PyVista style)
                actor_name = ""
                if hasattr(vtk_widget, 'actors'):
                    for name, act in vtk_widget.actors.items():
                        if act == actor:
                            actor_name = name
                            break
                
                # Update mapper
                mapper = actor.GetMapper()
                if mapper:
                    # Force mapper to rebuild
                    mapper.Modified()
                    mapper.Update()
                    
                    # ✅ CRITICAL FOR BORDERS: Force input data update
                    # This ensures the mapper's input data is also marked as modified
                    if hasattr(mapper, 'GetInput'):
                        input_data = mapper.GetInput()
                        if input_data:
                            input_data.Modified()
                    
                    # Count actor types for diagnostics
                    if "border_" in actor_name:
                        border_actors += 1
                    elif "class_" in actor_name:
                        point_actors += 1
                    else:
                        other_actors += 1
        
        # Report what was updated
        if border_enabled:
            print(f"      ✅ Updated {border_actors} border actors")
            print(f"      ✅ Updated {point_actors} point actors")
        else:
            total = point_actors + other_actors
            if total > 0:
                print(f"      ✅ Updated {total} actors")
        
        # Step 2: Force renderer to recognize changes
        renderer.Modified()
        
        # Step 3: Reset camera clipping range
        renderer.ResetCameraClippingRange()
        
        # Step 4: AGGRESSIVE render passes
        # More passes needed for borders because we have double the actors
        render_passes = 5 if border_enabled else 3
        
        for i in range(render_passes):
            vtk_widget.render()
        
        if border_enabled:
            print(f"      ✅ {render_passes} render passes (border mode)")
        
        # Step 5: Force render window update
        if hasattr(vtk_widget, 'GetRenderWindow'):
            render_window = vtk_widget.GetRenderWindow()
            if render_window:
                render_window.Render()
        
        print("   ✅ VTK pipeline fully updated")
        return True
        
    except Exception as e:
        print(f"❌ force_vtk_pipeline_update failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def force_vtk_pipeline_update(app):
    """
    Force complete VTK rendering pipeline update.
    
    This is the main function that should be called after classification
    or any other operation that modifies point cloud data. It automatically
    detects whether borders are enabled and uses the appropriate update
    strategy.
    
    This function is a wrapper around force_vtk_pipeline_update_with_borders
    and provides backward compatibility with the non-border version.
    
    Args:
        app: Main application instance (NakshaApp)
        
    Returns:
        bool: True if update succeeded, False otherwise
        
    Example:
        >>> from gui.vtk_utils import force_vtk_pipeline_update
        >>> force_vtk_pipeline_update(self.app)
        True
    """
    return force_vtk_pipeline_update_with_borders(app)


def force_renderer_update(renderer, widget):
    """
    Force update for a specific renderer and widget.
    
    Similar to force_vtk_pipeline_update but works with a specific
    renderer instead of the main app instance. Useful for cross-section
    views or other secondary renderers.
    
    Note: This version does not auto-detect borders since it doesn't have
    access to the app instance. Use the standard version for main view.
    
    Args:
        renderer: VTK renderer instance
        widget: VTK widget instance
        
    Returns:
        bool: True if update succeeded, False otherwise
        
    Example:
        >>> force_renderer_update(section_renderer, section_widget)
        True
    """
    try:
        if not renderer or not widget:
            print("⚠️ force_renderer_update: Invalid renderer or widget")
            return False
        
        # Update all actors in this renderer
        actors = renderer.GetActors()
        actors.InitTraversal()
        updated_count = 0
        
        for i in range(actors.GetNumberOfItems()):
            actor = actors.GetNextActor()
            if actor:
                mapper = actor.GetMapper()
                if mapper:
                    mapper.Modified()
                    mapper.Update()
                    
                    # Also update input data
                    if hasattr(mapper, 'GetInput'):
                        input_data = mapper.GetInput()
                        if input_data:
                            input_data.Modified()
                    
                    updated_count += 1
        
        # Mark renderer as modified
        renderer.Modified()
        renderer.ResetCameraClippingRange()
        
        # Multiple renders
        for i in range(3):
            widget.render()
        
        return True
        
    except Exception as e:
        print(f"❌ force_renderer_update failed: {e}")
        return False


def clear_vtk_cache(app):
    """
    Clear VTK-related caches in the application.
    
    This function clears various caches that might prevent proper
    visual updates after data modifications. Call this before
    force_vtk_pipeline_update for maximum effectiveness.
    
    Args:
        app: Main application instance
        
    Returns:
        int: Number of caches cleared
        
    Example:
        >>> cleared = clear_vtk_cache(app)
        >>> print(f"Cleared {cleared} caches")
        Cleared 3 caches
    """
    cleared_count = 0
    
    cache_attrs = [
        '_class_actor_map',
        '_class_point_indices', 
        '_class_vtk_data',
        '_point_cloud_actor',
        '_cached_polydata'
    ]
    
    for attr in cache_attrs:
        if hasattr(app, attr):
            cache = getattr(app, attr)
            try:
                if hasattr(cache, 'clear'):
                    cache.clear()
                    cleared_count += 1
                elif isinstance(cache, dict):
                    cache.clear()
                    cleared_count += 1
            except Exception as e:
                print(f"⚠️ Failed to clear {attr}: {e}")
    
    if cleared_count > 0:
        print(f"   🗑️ Cleared {cleared_count} VTK caches")
    
    return cleared_count


def clear_border_actors(app, renderer=None):
    """
    Clear all border actors from renderer.
    
    Useful when border percentage changes or when you need to
    rebuild all actors from scratch. Border actors are named with
    the pattern "border_{class_code}_{weight}".
    
    Args:
        app: Main application instance
        renderer: Optional specific renderer (uses main if None)
        
    Returns:
        int: Number of border actors removed
        
    Example:
        >>> removed = clear_border_actors(app)
        >>> print(f"Removed {removed} border actors")
        Removed 8 border actors
    """
    try:
        if renderer is None:
            vtk_widget = getattr(app, 'vtk_widget', None)
            if not vtk_widget:
                return 0
            
            renderer = getattr(vtk_widget, 'renderer', None)
            if renderer is None and hasattr(vtk_widget, 'GetRenderWindow'):
                rw = vtk_widget.GetRenderWindow()
                if rw:
                    renderer = rw.GetRenderers().GetFirstRenderer()
        
        if not renderer:
            return 0
        
        removed_count = 0
        
        # PyVista style - has actors dict
        if hasattr(app, 'vtk_widget') and hasattr(app.vtk_widget, 'actors'):
            actors_to_remove = []
            for name in list(app.vtk_widget.actors.keys()):
                if name.startswith("border_"):
                    actors_to_remove.append(name)
            
            for name in actors_to_remove:
                try:
                    app.vtk_widget.remove_actor(name)
                    removed_count += 1
                except Exception:
                    pass
        
        if removed_count > 0:
            print(f"   🗑️ Removed {removed_count} border actors")
        
        return removed_count
        
    except Exception as e:
        print(f"⚠️ Failed to clear border actors: {e}")
        return 0


def rebuild_actors_with_borders(app):
    """
    Complete rebuild of all actors when border settings change.
    
    This is more aggressive than force_vtk_pipeline_update and should
    be used when:
    - Border percentage changes in Display Mode
    - Weight values change significantly
    - You need to ensure completely clean state
    - Visual glitches persist after normal update
    
    This function:
    1. Clears all VTK caches
    2. Removes all border actors
    3. Triggers full display mode update
    4. Forces pipeline refresh
    
    Args:
        app: Main application instance
        
    Returns:
        bool: True if rebuild succeeded, False otherwise
        
    Example:
        >>> # In Display Mode apply_settings when border changes:
        >>> rebuild_actors_with_borders(app)
        True
    """
    try:
        print("🔄 Rebuilding actors with border support...")
        
        # Step 1: Clear all caches
        cleared = clear_vtk_cache(app)
        
        # Step 2: Clear border actors explicitly
        removed = clear_border_actors(app)
        
        # Step 3: Update display mode
        if app.display_mode == "class":
            from gui.class_display import update_class_mode
            app._preserve_view = True
            update_class_mode(app)
        
        # Step 4: Force pipeline update with border detection
        force_vtk_pipeline_update(app)
        
        print(f"   ✅ Complete rebuild: {cleared} caches cleared, {removed} borders removed")
        return True
        
    except Exception as e:
        print(f"❌ Rebuild failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def force_complete_refresh(app):
    """
    Perform a complete refresh including cache clearing and pipeline update.
    
    This is the most aggressive refresh function and should be used when
    you want to ensure absolutely everything is updated. It combines
    cache clearing with forced pipeline updates.
    
    Automatically handles border rendering if enabled.
    
    Args:
        app: Main application instance
        
    Returns:
        bool: True if refresh succeeded, False otherwise
        
    Example:
        >>> force_complete_refresh(app)
        True
    """
    try:
        print("🔄 Performing complete VTK refresh...")
        
        # Step 1: Clear caches
        cleared = clear_vtk_cache(app)
        
        # Step 2: Force pipeline update (border-aware)
        success = force_vtk_pipeline_update(app)
        
        if success:
            print(f"✅ Complete refresh done ({cleared} caches cleared)")
            return True
        else:
            print("⚠️ Complete refresh partially failed")
            return False
            
    except Exception as e:
        print(f"❌ Complete refresh failed: {e}")
        return False