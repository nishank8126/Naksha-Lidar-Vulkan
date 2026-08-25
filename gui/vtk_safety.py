# ════════════════════════════════════════════════════════════════════════════════
# VTK SAFETY SYSTEM - Prevents crashes from concurrent VTK operations
# ════════════════════════════════════════════════════════════════════════════════

import threading
import time
from functools import wraps

class VTKSafetyManager:
    """
    Thread-safe manager for VTK operations.
    Prevents crashes caused by concurrent rendering, camera changes, and classification.
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialize()
        return cls._instance
    
    def _initialize(self):
        """Initialize the safety manager."""
        self._render_lock = threading.RLock()  # Reentrant lock for nested calls
        self._is_rendering = False
        self._is_syncing = False
        self._last_render_time = {}  # Per-widget throttling
        self._min_render_interval = 16  # ~60 FPS max (milliseconds)
        self._operation_count = 0
        self._max_operations_per_frame = 5  # Prevent operation storms
    
    def acquire_render_lock(self, widget_id=None, timeout=0.5):
        """
        Acquire lock for VTK rendering operations.
        Returns True if lock acquired, False if timeout.
        """
        acquired = self._render_lock.acquire(timeout=timeout)
        if acquired:
            self._is_rendering = True
        return acquired
    
    def release_render_lock(self):
        """Release the render lock."""
        self._is_rendering = False
        try:
            self._render_lock.release()
        except RuntimeError:
            pass  # Lock not held
    
    def is_safe_to_render(self, widget_id=None):
        """Check if it's safe to render (not already rendering, enough time passed)."""
        if self._is_rendering:
            return False
        
        if widget_id is not None:
            now = time.time() * 1000
            last_render = self._last_render_time.get(widget_id, 0)
            if now - last_render < self._min_render_interval:
                return False
        
        return True
    
    def mark_rendered(self, widget_id):
        """Mark that a widget was just rendered."""
        self._last_render_time[widget_id] = time.time() * 1000
    
    @property
    def is_syncing(self):
        return self._is_syncing
    
    @is_syncing.setter
    def is_syncing(self, value):
        self._is_syncing = value


# Global instance
_vtk_safety = VTKSafetyManager()


def safe_vtk_operation(func):
    """
    Decorator that ensures VTK operations are thread-safe.
    Prevents crashes from concurrent rendering.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        safety = VTKSafetyManager()
        
        if not safety.acquire_render_lock(timeout=0.1):
            # Couldn't acquire lock - operation in progress
            # Schedule for later instead of blocking
            try:
                from PySide6.QtCore import QTimer
                QTimer.singleShot(20, lambda: func(*args, **kwargs))
            except Exception:
                pass
            return None
        
        try:
            return func(*args, **kwargs)
        except Exception as e:
            print(f"⚠️ VTK operation error in {func.__name__}: {e}")
            import traceback
            traceback.print_exc()
            return None
        finally:
            safety.release_render_lock()
    
    return wrapper


def safe_render(vtk_widget, widget_id=None):
    """
    Safely render a VTK widget with all protections.
    """
    safety = VTKSafetyManager()
    
    if vtk_widget is None:
        return False
    
    # Generate widget ID if not provided
    if widget_id is None:
        widget_id = id(vtk_widget)
    
    # Check if safe to render
    if not safety.is_safe_to_render(widget_id):
        return False
    
    # Try to acquire lock
    if not safety.acquire_render_lock(timeout=0.05):
        return False
    
    try:
        # Validate widget before rendering
        if not _validate_vtk_widget(vtk_widget):
            return False
        
        # Perform render
        render_window = vtk_widget.GetRenderWindow()
        if render_window:
            render_window.Render()
            safety.mark_rendered(widget_id)
            return True
        
    except Exception as e:
        print(f"⚠️ Safe render failed: {e}")
        return False
    
    finally:
        safety.release_render_lock()
    
    return False

def _validate_vtk_widget(vtk_widget):
    """Validate that a VTK widget is safe to use."""
    try:
        if vtk_widget is None:
            return False
        
        # Check render window
        rw = vtk_widget.GetRenderWindow()
        if rw is None:
            return False
        
        # Check if window is mapped
        if not rw.GetMapped():
            return False
        
        # Check renderer
        renderers = rw.GetRenderers()
        if renderers is None or renderers.GetNumberOfItems() == 0:
            return False
        
        renderer = renderers.GetFirstRenderer()
        if renderer is None:
            return False
        
        # Check camera
        camera = renderer.GetActiveCamera()
        if camera is None:
            return False
        
        # Validate camera values
        import numpy as np
        pos = camera.GetPosition()
        if pos is None or any(np.isnan(pos)) or any(np.isinf(pos)):
            return False
        
        return True
        
    except Exception:
        return False