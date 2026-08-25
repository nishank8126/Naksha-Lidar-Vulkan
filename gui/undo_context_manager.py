"""
undo_context_manager.py: Centralized undo/redo priority management.

Ensures whichever tool was LAST ACTIVATED gets priority for Ctrl+Z/Ctrl+Y.
Classification tool ALWAYS claims priority when active.
Draw tool only gets undo when classification is NOT active.
"""

try:
    from shiboken6 import isValid as _qt_object_is_valid
except ImportError:
    def _qt_object_is_valid(obj):
        return obj is not None


def _class_picker_visible(app) -> bool:
    picker = getattr(app, 'class_picker', None)
    if picker is None:
        return False
    try:
        if not _qt_object_is_valid(picker):
            app.class_picker = None
            return False
        return bool(picker.isVisible())
    except (RuntimeError, ReferenceError, AttributeError):
        app.class_picker = None
        return False
    except Exception:
        return False

class UndoContextManager:
    """
    Manages which tool currently owns undo/redo (Ctrl+Z/Ctrl+Y).
    
    Priority (highest to lowest):
    1. Classification tool (when active)
    2. Draw tools / Digitizer (when active AND classification is not)
    """
    
    NONE = "none"
    CLASSIFICATION = "classification"
    DRAW = "draw"
    
    def __init__(self, app):
        self.app = app
        self._current_context = self.NONE
        self._context_stack = []
    
    @property
    def current_context(self):
        return self._current_context
    
    def claim_context(self, context_name):
        """Claim undo priority for a tool (called on activation)."""
        if not context_name or context_name == self.NONE:
            return
        if context_name == self._current_context:
            return
        
        if context_name in self._context_stack:
            self._context_stack.remove(context_name)
        
        self._context_stack.append(context_name)
        self._current_context = context_name
        print(f"🔒 Undo context: {context_name}")
    
    def release_context(self, context_name):
        """Release undo priority for a tool (called on deactivation)."""
        if not context_name or context_name not in self._context_stack:
            return
        
        self._context_stack.remove(context_name)
        
        if self._current_context == context_name:
            self._current_context = self._context_stack[-1] if self._context_stack else self.NONE
        
        print(f"🔓 Undo context released: {context_name} (now: {self._current_context})")
    
    def force_context(self, context_name):
        """Force a specific context, clearing all others."""
        self._context_stack = [context_name] if context_name != self.NONE else []
        self._current_context = context_name
        print(f"⚡ Undo context forced: {context_name}")
    
    def is_classification_active(self):
        """
        Check if classification should own undo/redo.
        This is the CRITICAL check - used by InsideFenceDialog.
        """
        app = self.app
        
        # Check if classification interactors exist (tool is actively attached)
        if getattr(app, 'classify_interactor', None) is not None:
            return True
        if getattr(app, 'classify_interactors', None):  # Dict with entries
            return True
        if getattr(app, 'cut_classify_interactor', None) is not None:
            return True
        
        # Check if classification tool is armed
        if getattr(app, 'active_classify_tool', None) is not None:
            return True
        
        # Check if class picker is visible
        if _class_picker_visible(app):
            return True
        
        # Check explicit context ownership
        return self._current_context == self.CLASSIFICATION
    
    def is_draw_undo_allowed(self):
        """Check if draw tool undo should be allowed."""
        # NEVER allow if classification is active
        if self.is_classification_active():
            return False
        
        if self._current_context != self.DRAW:
            return False
        
        digitizer = getattr(self.app, 'digitizer', None)
        if not digitizer or not getattr(digitizer, 'enabled', False):
            return False
        
        undo_stack = getattr(digitizer, 'undo_stack', None)
        return bool(undo_stack and len(undo_stack) > 0)
    
    def is_draw_redo_allowed(self):
        """Check if draw tool redo should be allowed."""
        if self.is_classification_active():
            return False
        if self._current_context != self.DRAW:
            return False
        
        digitizer = getattr(self.app, 'digitizer', None)
        if not digitizer or not getattr(digitizer, 'enabled', False):
            return False
        
        redo_stack = getattr(digitizer, 'redo_stack', None)
        return bool(redo_stack and len(redo_stack) > 0)
    
    def get_undo_handler(self):
        """Return ('classification', None) or ('draw', None) or (None, None)."""
        if self.is_classification_active():
            return ('classification', None)
        if self.is_draw_undo_allowed():
            return ('draw', None)
        return (None, None)
    
    def get_redo_handler(self):
        """Return ('classification', None) or ('draw', None) or (None, None)."""
        if self.is_classification_active():
            return ('classification', None)
        if self.is_draw_redo_allowed():
            return ('draw', None)
        return (None, None)
    
    def reset(self):
        """Reset all contexts."""
        self._context_stack = []
        self._current_context = self.NONE

def get_undo_context_manager(app) -> UndoContextManager:
    """Get or create the UndoContextManager for an app instance."""
    if not hasattr(app, '_undo_context_manager'):
        app._undo_context_manager = UndoContextManager(app)
    return app._undo_context_manager