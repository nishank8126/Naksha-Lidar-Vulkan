import numpy as np
import vtk


class CrossSectionInteractor(vtk.vtkInteractorStyleTrackballCamera):
    """Persistent interactor for directional cross-section selection."""

    def __init__(self, app, iren):
        super().__init__()
        self.app = app
        self.iren = iren

        # ✅ NEW: Store original interactor style for restoration
        self._original_style = iren.GetInteractorStyle()
        
        # ✅ NEW: Store observer IDs for cleanup
        self._observer_ids = []
        self._cleaned_up = False

        # state machine — pure 3-click "data point" placement (MicroStation-
        # style: click 1 = P1, click 2 = P2, click 3 = width + finalize).
        # No drag gesture: distinguishing a deliberate tap from a drag by
        # release timing/pixel-distance proved unreliable (a slightly slower
        # or slightly jittery 2nd click could be misread as a drag and
        # finalize the section early, before the 3rd click). Every press is
        # simply the next data point.
        self.slice_state = 0
        self.P1 = None
        self.P2 = None
        self._last_move_pos = None

        # ✅ FIXED: Store observer IDs
        self._observer_ids.append(
            self.AddObserver("LeftButtonPressEvent", self.on_left_press)
        )
        self._observer_ids.append(
            self.AddObserver("MouseMoveEvent", self.on_mouse_move)
        )
        self._observer_ids.append(
            self.AddObserver("LeftButtonReleaseEvent", self.on_left_release)
        )

        # keep camera navigation
        self._observer_ids.append(
            self.AddObserver("MiddleButtonPressEvent", lambda o, e: self.OnMiddleButtonDown())
        )
        self._observer_ids.append(
            self.AddObserver("MiddleButtonReleaseEvent", lambda o, e: self.OnMiddleButtonUp())
        )
        self._observer_ids.append(
            self.AddObserver("RightButtonPressEvent", lambda o, e: self.OnRightButtonDown())
        )
        self._observer_ids.append(
            self.AddObserver("RightButtonReleaseEvent", lambda o, e: self.OnRightButtonUp())
        )

    # ✅ NEW: Add cleanup method
    def cleanup(self):
        """Remove all observers and restore original interactor style."""
        self._cleaned_up = True
        try:
            # Remove all observers
            for obs_id in self._observer_ids:
                try:
                    self.RemoveObserver(obs_id)
                except Exception:
                    pass
            self._observer_ids.clear()
            
            # Restore original interactor style
            if self._original_style is not None and self.iren is not None:
                try:
                    self.iren.SetInteractorStyle(self._original_style)
                    print("✅ CrossSectionInteractor: Original style restored")
                except Exception as e:
                    print(f"⚠️ Failed to restore original style: {e}")
            
            # Clear state
            self.P1 = None
            self.P2 = None
            self.slice_state = 0
            self._last_move_pos = None

        except Exception as e:
            print(f"⚠️ CrossSectionInteractor cleanup error: {e}")

    def _is_runtime_alive(self):
        """Return False once the app/VTK widget has entered shutdown cleanup."""
        if self._cleaned_up:
            return False
        if self.app is None or self.iren is None:
            return False
        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is None:
            return False
        if getattr(vtk_widget, "renderer", None) is None:
            return False
        return True

    def cancel(self):
        """Cancel current cross-section gesture and remove unfinished preview."""
        if self._cleaned_up:
            return
        try:
            section_controller = getattr(self.app, "section_controller", None)
            if section_controller is not None and hasattr(section_controller, "clear_preview"):
                try:
                    section_controller.clear_preview(force_overlay_destroy=True)
                except TypeError:
                    section_controller.clear_preview()
        except Exception as e:
            print(f"⚠️ CrossSectionInteractor cancel preview cleanup failed: {e}")

        # Reset in-progress gesture state only (do not touch finalized sections)
        self.P1 = None
        self.P2 = None
        self.slice_state = 0
        self._last_move_pos = None

    # ---------------- PICKER ----------------

    def on_left_press(self, obj, ev):
        if not self._is_runtime_alive():
            return
        x, y = self.iren.GetEventPosition()
        pt = self._display_to_world(x, y)
        if pt is None:
            return
        self._last_move_pos = (x, y)

        if self.slice_state == 0:
            section_controller = getattr(self.app, "section_controller", None)
            if section_controller is None:
                return
            section_controller.clear()
            self.P1 = pt
            self.slice_state = 1
            # Clear section-locate rubber-band (user drew P1 manually in main view)
            self.app._section_locate_display = None
            self.app._section_locate_view = None
            try:
                sc = getattr(self.app, 'section_controller', None)
                if sc is not None and hasattr(sc, 'clear_locate_state'):
                    sc.clear_locate_state()
            except Exception:
                pass

        elif self.slice_state == 1:
            self.P2 = pt
            self.slice_state = 2

        elif self.slice_state == 2:
            # 3rd click: finalize using half_width from last mouse-move preview
            section_controller = getattr(self.app, "section_controller", None)
            if section_controller is None:
                return
            section_controller.finalize_section(self.P1, self.P2)
            try:
                view_idx = getattr(section_controller, "active_view", None)
                if view_idx is None:
                    view_idx = 0
                if hasattr(self.app, "_on_section_updated"):
                    self.app._on_section_updated(view_idx)
            except Exception as e:
                print(f"⚠️ Sync hook failed after 3rd-click finalize: {e}")
            self.P1 = None
            self.P2 = None
            self.slice_state = 0
            self._last_move_pos = None



    def _display_to_world(self, x, y):
        """
        Cursor-accurate DISPLAY → WORLD conversion.
        No snapping. No picking. Zoom safe.
        """
        if not self._is_runtime_alive():
            return None

        ren = self.app.vtk_widget.renderer

        coord = vtk.vtkCoordinate()
        coord.SetCoordinateSystemToDisplay()
        coord.SetValue(float(x), float(y), 0.0)

        world = coord.GetComputedWorldValue(ren)
        return np.array(world, dtype=np.float64)



    def on_mouse_move(self, obj, ev):
        if not self._is_runtime_alive():
            return
        x, y = self.iren.GetEventPosition()
        if self._last_move_pos == (x, y):
            return
        self._last_move_pos = (x, y)
        curr = self._display_to_world(x, y)
        if curr is None:
            return

        # 🔒 LOCK Z — THIS REMOVES ALL MAGNETIC EFFECTS
        if self.P1 is not None:
            curr[2] = self.P1[2]

        if self.slice_state == 1 and self.P1 is not None:
            section_controller = getattr(self.app, "section_controller", None)
            if section_controller is not None:
                section_controller.draw_centerline(self.P1, curr)

        elif self.slice_state == 2:
            if self.P1 is None or self.P2 is None:
                return

            v = self.P2[:2] - self.P1[:2]
            if np.linalg.norm(v) < 1e-9:
                return

            dir = v / np.linalg.norm(v)
            perp = np.array([-dir[1], dir[0]])
            half_width = abs(np.dot(curr[:2] - self.P2[:2], perp))

            section_controller = getattr(self.app, "section_controller", None)
            if section_controller is not None:
                section_controller.draw_rubber_rectangle(
                    self.P1, self.P2, half_width
                )

        # Keep camera navigation
        # ALWAYS allow camera movement (pan/zoom/rotate) even if a gesture is in progress.
        # vtkInteractorStyleTrackballCamera handles buttons/states internally.
        # If GetState() is not 0 (VTKIS_NONE), it means a button is being held for camera interaction.
        try:
            if self.slice_state == 0 or self.GetState() != 0:
                self.OnMouseMove()
        except Exception:
            pass




    def on_left_release(self, obj, evt):
        # Point placement is entirely press-driven now (on_left_press): a
        # pure 3-click "data point" workflow — click 1 = P1, click 2 = P2,
        # click 3 = width + finalize. There is deliberately no drag gesture
        # here, so releases don't do anything to the section state; this
        # keeps the tool's behavior independent of click speed or hand
        # tremor between press and release.
        return