import numpy as np
import vtk
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox
from gui.naksha_plugin_api import NakshaPlugin

class TracePlugin(NakshaPlugin):
    def on_load(self, app_window):
        self.app_window = app_window
        self.active = False
        self.trace_points = []
        self.current_preview_actor = None
        self.current_trace_actor = None
        self.active_drawing = None
        self.last_snapped_index = -1
        self.observer_ids = []

    def get_ribbon_button(self):
        return {
            "label": "Trace",
            "emoji": "",
            "section": "Custom Plugins",
            "callback": self.toggle_trace,
            "toggleable": True
        }

    def on_unload(self):
        self.deactivate()

    def toggle_trace(self):
        if self.active:
            self.deactivate()
        else:
            self.activate()

    def activate(self):
        if self.active:
            return
        self.active = True
        self.trace_points = []
        self.active_drawing = None
        self.last_snapped_index = -1
        
        # Deactivate other draw/digitize tools
        if hasattr(self.app_window, "digitizer") and self.app_window.digitizer:
            self.app_window.digitizer.set_tool(None)
            # Disable digitizer observers to avoid conflicts
            self.app_window.digitizer.enabled = False

        # Add observers to VTK interactor
        interactor = self.app_window.vtk_widget.interactor
        self.observer_ids.append(interactor.AddObserver("LeftButtonPressEvent", self.on_left_press, 10.0))
        self.observer_ids.append(interactor.AddObserver("MouseMoveEvent", self.on_mouse_move, 10.0))
        self.observer_ids.append(interactor.AddObserver("RightButtonPressEvent", self.on_right_press, 10.0))

        # Show status message
        if hasattr(self.app_window, "statusBar"):
            self.app_window.statusBar().showMessage("Trace Tool Active: Click on an existing line vertex to start tracing. Right-click to finalize.", 5000)

    def deactivate(self):
        if not self.active:
            return
        self.active = False
        
        # Remove observers
        interactor = self.app_window.vtk_widget.interactor
        for oid in self.observer_ids:
            interactor.RemoveObserver(oid)
        self.observer_ids = []

        # Remove preview actors
        self.clear_previews()

        # Re-enable digitizer if needed
        if hasattr(self.app_window, "digitizer") and self.app_window.digitizer:
            self.app_window.digitizer.enabled = True

        # Uncheck ribbon button if externally deactivated
        try:
            if hasattr(self.app_window, "ribbon_manager"):
                self.app_window.ribbon_manager.clear_all_tool_buttons(exclude_btn=None)
        except Exception:
            pass

        if hasattr(self.app_window, "statusBar"):
            self.app_window.statusBar().showMessage("Trace Tool Deactivated.", 2000)

    def clear_previews(self):
        digitizer = self.app_window.digitizer
        if digitizer:
            if self.current_preview_actor:
                digitizer._remove_actor_from_overlay(self.current_preview_actor)
                self.current_preview_actor = None
            if self.current_trace_actor:
                digitizer._remove_actor_from_overlay(self.current_trace_actor)
                self.current_trace_actor = None
        self.app_window.vtk_widget.render()

    def get_mouse_world(self):
        interactor = self.app_window.vtk_widget.interactor
        renderer = self.app_window.digitizer.renderer
        picker = self.app_window.digitizer.picker
        
        x, y = interactor.GetEventPosition()
        picker.Pick(x, y, 0, renderer)
        pos = np.array(picker.GetPickPosition())

        # 2D projection Z consistency
        if not getattr(self.app_window, 'is_3d_mode', False) and self.trace_points:
            pos[2] = float(self.trace_points[0][2])
            
        return pos

    def find_snap_target(self, pos, screen_tol_px=25):
        digitizer = self.app_window.digitizer
        if not digitizer:
            return None, -1, pos
            
        world_tol = digitizer._screen_to_world_tolerance(screen_tol_px)
        best_dist = world_tol
        snap_drawing = None
        snap_idx = -1
        snap_pos = pos

        for d in digitizer.drawings:
            coords = d.get("coords") or []
            for idx, c in enumerate(coords):
                dx = float(c[0]) - float(pos[0])
                dy = float(c[1]) - float(pos[1])
                dist = np.sqrt(dx*dx + dy*dy)
                if dist < best_dist:
                    best_dist = dist
                    snap_drawing = d
                    snap_idx = idx
                    snap_pos = c
                    
        return snap_drawing, snap_idx, snap_pos

    def on_mouse_move(self, obj, event):
        if not self.active or not self.trace_points:
            return
            
        pos = self.get_mouse_world()
        snap_drawing, snap_idx, snap_pos = self.find_snap_target(pos)

        # Path trace preview
        preview_points = []
        if self.active_drawing and snap_drawing == self.active_drawing and snap_idx != -1:
            # Trace along the active drawing vertices
            coords = self.active_drawing["coords"]
            if self.last_snapped_index <= snap_idx:
                preview_points = coords[self.last_snapped_index : snap_idx + 1]
            else:
                preview_points = coords[snap_idx : self.last_snapped_index + 1][::-1]
        else:
            # Just draw straight line to cursor
            preview_points = [self.trace_points[-1], snap_pos]

        # Update trace preview actor
        digitizer = self.app_window.digitizer
        if digitizer and len(preview_points) >= 2:
            if self.current_trace_actor:
                digitizer._remove_actor_from_overlay(self.current_trace_actor)
            
            # Draw preview in orange/yellow
            self.current_trace_actor = digitizer._make_polyline_actor(
                preview_points, color=(0.95, 0.6, 0.1), width=2, line_style="dashed"
            )
            if self.current_trace_actor:
                digitizer._add_actor_to_overlay(self.current_trace_actor)
                
        self.app_window.vtk_widget.render()

    def on_left_press(self, obj, event):
        pos = self.get_mouse_world()
        snap_drawing, snap_idx, snap_pos = self.find_snap_target(pos)

        if not self.trace_points:
            # Start of trace line
            self.trace_points.append(snap_pos)
            self.active_drawing = snap_drawing
            self.last_snapped_index = snap_idx
        else:
            # Add trace segment
            if self.active_drawing and snap_drawing == self.active_drawing and snap_idx != -1:
                coords = self.active_drawing["coords"]
                # Trace path vertices
                if self.last_snapped_index <= snap_idx:
                    path = coords[self.last_snapped_index : snap_idx + 1]
                else:
                    path = coords[snap_idx : self.last_snapped_index + 1][::-1]
                
                # Append path vertices excluding the first
                for pt in path[1:]:
                    self.trace_points.append(pt)
                self.last_snapped_index = snap_idx
            else:
                # Add straight line point
                self.trace_points.append(snap_pos)
                self.active_drawing = snap_drawing
                self.last_snapped_index = snap_idx

            # Update permanent preview actor
            digitizer = self.app_window.digitizer
            if digitizer and len(self.trace_points) >= 2:
                if self.current_preview_actor:
                    digitizer._remove_actor_from_overlay(self.current_preview_actor)
                
                # Permanent preview in cyan/teal
                self.current_preview_actor = digitizer._make_polyline_actor(
                    self.trace_points, color=(0, 0.8, 0.8), width=2, line_style="solid"
                )
                if self.current_preview_actor:
                    digitizer._add_actor_to_overlay(self.current_preview_actor)

        self.app_window.vtk_widget.render()
        # Prevent default VTK click handling from panning/rotating during drawing
        if hasattr(obj, "AbortFlagOn"):
            obj.AbortFlagOn()
        elif hasattr(obj, "SetAbortFlag"):
            try: obj.SetAbortFlag(1)
            except Exception: pass

    def on_right_press(self, obj, event):
        if len(self.trace_points) >= 2:
            digitizer = self.app_window.digitizer
            if digitizer:
                # Save undo state
                digitizer._save_state()
                
                # Create final polyline actor in overlay
                final_actor = digitizer._make_polyline_actor(
                    self.trace_points, color=(0.1, 0.8, 0.3), width=2, line_style="solid"
                )
                digitizer._add_actor_to_overlay(final_actor)

                drawing_entry = {
                    "type": "polyline",
                    "coords": self.trace_points,
                    "actor": final_actor,
                    "bounds": final_actor.GetBounds(),
                    "original_color": (0.1, 0.8, 0.3),
                    "original_width": 2,
                    "original_style": "solid",
                }
                digitizer._tag_sbm(drawing_entry)
                digitizer.drawings.append(drawing_entry)
                digitizer._emit_drawing_finalized(drawing_entry)

        # Clean up trace state
        self.trace_points = []
        self.active_drawing = None
        self.last_snapped_index = -1
        self.clear_previews()
        
        # Abort VTK default handling
        if hasattr(obj, "AbortFlagOn"):
            obj.AbortFlagOn()
        elif hasattr(obj, "SetAbortFlag"):
            try: obj.SetAbortFlag(1)
            except Exception: pass
