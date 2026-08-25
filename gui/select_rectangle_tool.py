import numpy as np
import vtk
from PySide6.QtWidgets import QMessageBox, QProgressDialog
from PySide6.QtCore import Qt, QTimer


class SelectRectangleTool:
    """Tool for selecting points AND DXF entities within a drawn rectangle"""
    
    def __init__(self, app):
        self.app = app
        self.active = False
        self.start_pos = None
        self.rubber_band_actor = None
        self.selected_mask = None
        self.selected_count = 0
        self.selected_dxf_actors = []  # ✅ NEW: Store selected DXF actors
        self.is_drawing = False
        self._processing_click = False 
        self.pick_method = None  # ✅ Default to None (user must pick)
        self.polygon_points = []   # ✅ NEW: For "Shape" mode
        self.world_polygon_points = [] # ✅ NEW: Track world coords for panning
        self.world_start_pos = None    # ✅ NEW: Track world start for panning
        self.preview_line_actor = None # ✅ NEW: For "Shape" mode
        
        # VTK event tags for cleanup
        self.observer_ids = []
        
    def activate(self):
        """Activate the selection tool"""
        if self.active:
            return
        
        self.active = True
        self.start_pos = None
        self.selected_mask = None
        self.selected_count = 0
        self.selected_dxf_actors = []
        self.is_drawing = False
        self.polygon_points = []
        self._clear_shape_preview()
        
        # Get main VTK interactor
        if not hasattr(self.app, 'vtk_widget') or not self.app.vtk_widget:
            print("⚠️ No VTK widget found")
            return
        
        interactor = self.app.vtk_widget.interactor
        
        # Add observers with HIGH PRIORITY
        press_id = interactor.AddObserver("LeftButtonPressEvent", self.on_left_press, 1.0)
        move_id = interactor.AddObserver("MouseMoveEvent", self.on_mouse_move, 1.0)
        right_id = interactor.AddObserver("RightButtonPressEvent", self.on_right_click, 1.0)
        
        self.observer_ids = [press_id, move_id, right_id]
        
        # ✅ NEW: Add camera observer to handle panning/zooming
        try:
            renderer = self.app.vtk_widget.renderer
            camera = renderer.GetActiveCamera()
            self.cam_obs_id = camera.AddObserver("ModifiedEvent", self._on_camera_modified)
            print(f"   🎥 Camera observer attached (ID: {self.cam_obs_id})")
        except Exception as e:
            print(f"   ⚠️ Failed to attach camera observer: {e}")
        
        print("✅ Select Rectangle tool activated")
        print("   📍 Left-click & drag to draw rectangle")
        print("   🖱️ Right-click to finish and show delete dialog")
        print("   🎯 Selects BOTH point cloud AND DXF entities")
        
        self.app.statusBar().showMessage("🟧 Draw rectangle: LEFT-CLICK & drag, RIGHT-CLICK to finish", 5000)
        
    def deactivate(self):
        """Deactivate the selection tool"""
        if not self.active:
            return
        
        self.active = False
        self.is_drawing = False
        self.polygon_points = []
        self._clear_shape_preview()
        
        # Clear rubber band
        self._clear_rubber_band()
        
        # Clear selection highlight
        self._clear_selection_highlight()
        
        # Clear selection
        self.selected_mask = None
        self.selected_count = 0
        self.selected_dxf_actors = []
        self.start_pos = None
        self.world_start_pos = None
        self.world_polygon_points = []
        
        # Remove ALL observers
        if hasattr(self.app, 'vtk_widget') and self.app.vtk_widget:
            interactor = self.app.vtk_widget.interactor
            
            # ✅ NEW: Remove camera observer
            try:
                renderer = self.app.vtk_widget.renderer
                camera = renderer.GetActiveCamera()
                camera.RemoveObserver(self.cam_obs_id)
            except Exception:
                pass
            
            for obs_id in self.observer_ids:
                try:
                    interactor.RemoveObserver(obs_id)
                except Exception:
                    pass
            
            self.observer_ids = []
        
        print("✅ Select Rectangle tool deactivated")
        
    def set_pick_method(self, method):
        """Set the pick method (Block or Shape)"""
        if method and method not in ["Block", "Shape"]:
            return
        
        self.pick_method = method
        self.is_drawing = False
        self.polygon_points = []
        self._clear_rubber_band()
        self._clear_shape_preview()
        self._clear_selection_highlight()
        
        if method == "Block":
            self.app.statusBar().showMessage("🟧 Block Select: LEFT-CLICK & drag, RIGHT-CLICK to finish", 5000)
        else:
            self.app.statusBar().showMessage("🟧 Shape Select: LEFT-CLICK to add vertices, RIGHT-CLICK to close", 5000)
        
        print(f"🔄 Pick method changed to: {self.pick_method}")

    def on_left_press(self, obj, event):
        """Handle left click based on pick method"""
        if not self.active:
            return
        
        if self.pick_method == "Block":
            self._on_left_press_block(obj, event)
        elif self.pick_method == "Shape":
            self._on_left_press_shape(obj, event)

    def _on_camera_modified(self, obj, event):
        """Update screen positions of selection when camera moves (pan/zoom)"""
        if not self.active or not self.is_drawing or getattr(self, '_updating_cam', False):
            return
            
        self._updating_cam = True
        try:
            # Update Block start position from its world anchor
            if self.pick_method == "Block" and self.world_start_pos is not None:
                self.start_pos = self._world_to_screen(self.world_start_pos)
                
                # Redraw rubber band live while panning
                interactor = self.app.vtk_widget.interactor
                curr_pos = interactor.GetEventPosition()
                self._draw_rubber_band(self.start_pos, curr_pos)
                
            # ✅ FIX: Update Shape preview to move with camera
            elif self.pick_method == "Shape" and self.world_polygon_points:
                # Re-project all world anchors to current screen positions
                self.polygon_points = [self._world_to_screen(wp) for wp in self.world_polygon_points]
                self._draw_shape_preview()
        finally:
            self._updating_cam = False

    def _screen_to_world(self, screen_pos):
        """Convert screen position to world coordinates using point cloud depth"""
        try:
            renderer = self.app.vtk_widget.renderer
            
            # Use PointPicker for best precision on LiDAR data
            picker = vtk.vtkPointPicker()
            picker.SetTolerance(0.005) # Tighter tolerance
            
            if picker.Pick(screen_pos[0], screen_pos[1], 0, renderer):
                return picker.GetPickPosition()
            
            # Fallback 1: CellPicker (for DXF or surface-like clouds)
            cpicker = vtk.vtkCellPicker()
            cpicker.SetTolerance(0.01)
            if cpicker.Pick(screen_pos[0], screen_pos[1], 0, renderer):
                return cpicker.GetPickPosition()
                
            # Fallback 2: Focal plane projection
            coord = vtk.vtkCoordinate()
            coord.SetCoordinateSystemToDisplay()
            coord.SetValue(screen_pos[0], screen_pos[1], 0)
            return coord.GetComputedWorldValue(renderer)
        except:
            return None

    def _world_to_screen(self, world_pos):
        """Convert 3D world position to display coordinates"""
        try:
            renderer = self.app.vtk_widget.renderer
            coord = vtk.vtkCoordinate()
            coord.SetCoordinateSystemToWorld()
            coord.SetValue(world_pos)
            return coord.GetComputedDisplayValue(renderer)
        except:
            return (0, 0)

    def _on_left_press_block(self, obj, event):
        """Start drawing rectangle"""
        if not self.active:
            return
        
        print(f"\n{'='*60}")
        print(f"🟧 SELECT TOOL: Left-click pressed")
        
        interactor = obj
        self.start_pos = interactor.GetEventPosition()
        self.world_start_pos = self._screen_to_world(self.start_pos)
        self.is_drawing = True
        
        # Clear previous rubber band and selection
        self._clear_rubber_band()
        self._clear_selection_highlight()
        
        print(f"   📍 Start position: {self.start_pos} | World: {self.world_start_pos}")
        print(f"{'='*60}\n")
        
        # ✅ FIXED: Abort event properly
        try:
            interactor.GetInteractorStyle().OnLeftButtonDown()
        except Exception:
            pass

    def _on_left_press_shape(self, obj, event):
        """Add vertex to polygon"""
        interactor = obj
        pos = interactor.GetEventPosition()
        world_pos = self._screen_to_world(pos)
        
        if world_pos:
            self.polygon_points.append(pos)
            self.world_polygon_points.append(world_pos)
            self.is_drawing = True
            print(f"   📍 Added vertex: {pos} | World: {world_pos} (Total: {len(self.polygon_points)})")
        
        # Clear previous selection if this is the first point
        if len(self.polygon_points) == 1:
            self._clear_selection_highlight()
            self.selected_mask = None
            self.selected_count = 0
            self.selected_dxf_actors = []

        self._draw_shape_preview()
        
        try:
            interactor.GetInteractorStyle().OnLeftButtonDown()
        except Exception:
            pass
        
    def on_mouse_move(self, obj, event):
        """Update preview during drawing"""
        if not self.active or not self.is_drawing:
            return
        
        if self.pick_method == "Block":
            if not self.start_pos: return
            interactor = obj
            current_pos = interactor.GetEventPosition()
            self._draw_rubber_band(self.start_pos, current_pos)
        else:
            if not self.polygon_points: return
            self._draw_shape_preview()
        
    def on_right_click(self, obj, event):
        """Handle right click based on pick method"""
        if not self.active:
            return
        
        if self.pick_method == "Block":
            self._on_right_click_block(obj, event)
        elif self.pick_method == "Shape":
            self._on_right_click_shape(obj, event)

    def _on_right_click_block(self, obj, event):
        """Right-click to finish selection and show delete dialog"""
        
        # ✅ FIX: Prevent multiple rapid clicks
        if hasattr(self, '_processing_click') and self._processing_click:
            print("⚠️ Already processing a click, ignoring...")
            return
        
        self._processing_click = True
        
        print(f"\n{'='*60}")
        print(f"🟧 SELECT TOOL: Right-click pressed")
        
        try:
            interactor = obj
            
            # ✅ NEW: Sync start position with current camera right before calculation
            if self.world_start_pos is not None:
                self.start_pos = self._world_to_screen(self.world_start_pos)
            
            if self.is_drawing and self.start_pos:
                end_pos = interactor.GetEventPosition()
                
                # Check if this is a valid rectangle
                x_diff = abs(end_pos[0] - self.start_pos[0])
                y_diff = abs(end_pos[1] - self.start_pos[1])
                
                print(f"   📍 End position: {end_pos}")
                print(f"   📏 Rectangle size: {x_diff}x{y_diff}")
                
                if x_diff > 5 and y_diff > 5:
                    # Select points AND DXF within rectangle
                    self._select_in_rectangle_fast(self.start_pos, end_pos)
                    
                    # Stop drawing
                    self.is_drawing = False
                    
                    # Show delete confirmation if anything was selected
                    total_selected = self.selected_count + len(self.selected_dxf_actors)
                    
                    if total_selected > 0:
                        print(f"   ✅ Total selected: {self.selected_count:,} points + {len(self.selected_dxf_actors)} DXF entities")
                        print(f"   💬 Showing delete confirmation dialog")
                        print(f"{'='*60}\n")
                        
                        # Show confirmation dialog
                        self._show_delete_confirmation()
                    else:
                        print(f"   ⚠️ Nothing selected in rectangle")
                        print(f"{'='*60}\n")
                        
                        self._clear_rubber_band()
                        self.start_pos = None
                        self.app.statusBar().showMessage("⚠️ Nothing selected in rectangle", 2000)
                else:
                    print(f"   ⚠️ Rectangle too small - ignored")
                    print(f"{'='*60}\n")
                    
                    self._clear_rubber_band()
                    self._clear_shape_preview()
                    self.start_pos = None
                    self.world_start_pos = None
                    self.polygon_points = []
                    self.world_polygon_points = []
                    self.is_drawing = False
                    self.app.statusBar().showMessage("⚠️ Draw a larger rectangle", 2000)
            
            elif (self.selected_mask is not None and np.any(self.selected_mask)) or self.selected_dxf_actors:
                print(f"   💬 Re-showing delete confirmation")
                print(f"{'='*60}\n")
                self._show_delete_confirmation()
            
            else:
                print(f"   ⚠️ No active selection")
                print(f"{'='*60}\n")
            
            # Abort event properly
            try:
                interactor.GetInteractorStyle().OnRightButtonDown()
            except Exception:
                pass
        
        finally:
            # ✅ FIX: Always reset the processing flag
            self._processing_click = False

    def _on_right_click_shape(self, obj, event):
        """Close polygon and process selection"""
        if not self.is_drawing or len(self.polygon_points) < 3:
            print("⚠️ Shape selection requires at least 3 points")
            self.app.statusBar().showMessage("⚠️ Shape selection requires at least 3 points", 2000)
            return

        # ✅ FIX: Prevent multiple rapid clicks
        if hasattr(self, '_processing_click') and self._processing_click:
            return
        
        self._processing_click = True
        
        print(f"\n{'='*60}")
        print(f"🟧 SELECT TOOL (SHAPE): Finishing polygon")
        
        try:
            # ✅ NEW: Update screen points from world points right before selection
            # This ensures the selection matches the current panned/zoomed view.
            if self.world_polygon_points:
                updated_screen_pts = []
                for wp in self.world_polygon_points:
                    updated_screen_pts.append(self._world_to_screen(wp))
                self.polygon_points = updated_screen_pts
                
            self._select_in_shape_fast(self.polygon_points)
            
            # Reset drawing state
            self.is_drawing = False
            self._clear_shape_preview()
            
            # Show delete confirmation if anything was selected
            total_selected = self.selected_count + len(self.selected_dxf_actors)
            
            if total_selected > 0:
                self._show_delete_confirmation()
            else:
                print("⚠️ Nothing selected in shape")
                self.app.statusBar().showMessage("⚠️ Nothing selected in shape", 2000)
                self.polygon_points = []
                self.world_polygon_points = []
                self._clear_shape_preview()
                
            # Abort event
            try:
                obj.GetInteractorStyle().OnRightButtonDown()
            except Exception:
                pass
        finally:
            self._processing_click = False

    def _select_in_shape_fast(self, points):
        """Select elements within a polygon"""
        import time
        start_time = time.time()
        
        # Create progress dialog
        progress = QProgressDialog("Detecting elements in shape...", "Cancel", 0, 100, self.app)
        progress.setWindowTitle("Selection Progress")
        progress.setWindowModality(Qt.WindowModal)
        progress.show()
        
        try:
            # 1. Point Cloud
            if hasattr(self.app, 'data') and self.app.data is not None:
                self._select_points_shape_fast(points, progress, 0, 50)
            
            # 2. DXF
            # For simplicity in this first version, I'll use a bounding box check then polygon check for DXF
            pts = np.array(points)
            xmin, xmax = np.min(pts[:, 0]), np.max(pts[:, 0])
            ymin, ymax = np.min(pts[:, 1]), np.max(pts[:, 1])
            self._select_dxf_fast(xmin, xmax, ymin, ymax, progress, 50, 90)
            
            # 3. Highlight
            self._clear_selection_highlight()
            if self.selected_count > 0: self._highlight_selected_points()
            if self.selected_dxf_actors: self._highlight_selected_dxf()
            
            total = self.selected_count + len(self.selected_dxf_actors)
            print(f"   📊 Shape select finished: {total:,} elements in {time.time()-start_time:.3f}s")
            
        finally:
            progress.close()

    def _select_points_shape_fast(self, screen_poly_pts, progress, start_pct, end_pct):
        """Select points within a screen-space polygon"""
        xyz = self.app.data['xyz']
        if xyz is None or len(xyz) == 0: return

        # Transform all points to screen space (reusing logic from _select_points_fast)
        renderer = self.app.vtk_widget.renderer
        camera = renderer.GetActiveCamera()
        composite = camera.GetCompositeProjectionTransformMatrix(renderer.GetTiledAspectRatio(), 0, 1)
        
        matrix = np.zeros((4, 4))
        for i in range(4):
            for j in range(4): matrix[i, j] = composite.GetElement(i, j)
            
        ones = np.ones((len(xyz), 1))
        homogeneous = np.hstack([xyz, ones])
        transformed = homogeneous @ matrix.T
        
        with np.errstate(divide='ignore', invalid='ignore'):
            transformed[:, :3] /= transformed[:, 3:4]
            
        size = renderer.GetSize()
        origin = renderer.GetOrigin()
        display_x = (transformed[:, 0] + 1.0) * size[0] * 0.5 + origin[0]
        display_y = (transformed[:, 1] + 1.0) * size[1] * 0.5 + origin[1]
        
        # Optimization: Pre-filter with bounding box
        pts_2d = np.array(screen_poly_pts)
        xmin, xmax = pts_2d[:, 0].min(), pts_2d[:, 0].max()
        ymin, ymax = pts_2d[:, 1].min(), pts_2d[:, 1].max()
        
        in_bbox = (display_x >= xmin) & (display_x <= xmax) & (display_y >= ymin) & (display_y <= ymax)
        
        if not np.any(in_bbox):
            self.selected_mask = np.zeros(len(xyz), dtype=bool)
            self.selected_count = 0
            return

        # Point-in-polygon test (only on points inside bounding box)
        try:
            from matplotlib.path import Path
            poly_path = Path(screen_poly_pts)
            
            points_in_bbox_2d = np.vstack((display_x[in_bbox], display_y[in_bbox])).T
            mask_in_bbox = poly_path.contains_points(points_in_bbox_2d)
            
            self.selected_mask = np.zeros(len(xyz), dtype=bool)
            self.selected_mask[in_bbox] = mask_in_bbox
            
            # Apply visibility filter same as rectangle select
            if self.app.display_mode == "class" and hasattr(self.app, 'class_palette'):
                visible_classes = [c for c, v in self.app.class_palette.items() if v.get('show', True)]
                if visible_classes and 'classification' in self.app.data:
                    classes = self.app.data['classification']
                    self.selected_mask &= np.isin(classes, visible_classes)

            self.selected_count = np.sum(self.selected_mask)
        except Exception as e:
            print(f"❌ Error in point-in-polygon: {e}")
            self.selected_mask = np.zeros(len(xyz), dtype=bool)
            self.selected_count = 0

    def _draw_shape_preview(self):
        """Draw the polygon preview using 2D screen coordinates for pixel-perfect alignment"""
        self._clear_shape_preview()
        
        if not self.polygon_points:
            return
            
        # Use captured screen points
        pts_2d = list(self.polygon_points)
        
        # Add current mouse position live if currently drawing
        interactor = self.app.vtk_widget.interactor
        curr_screen = interactor.GetEventPosition()
        
        if self.is_drawing:
            pts_2d.append(curr_screen)
            
        if len(pts_2d) < 2:
            return
            
        # Create 2D points
        points = vtk.vtkPoints()
        for p in pts_2d:
            points.InsertNextPoint(p[0], p[1], 0)
            
        num_pts = points.GetNumberOfPoints()
        line = vtk.vtkPolyLine()
        line.GetPointIds().SetNumberOfIds(num_pts)
        for i in range(num_pts):
            line.GetPointIds().SetId(i, i)
            
        cells = vtk.vtkCellArray()
        cells.InsertNextCell(line)
        
        pd = vtk.vtkPolyData()
        pd.SetPoints(points)
        pd.SetLines(cells)
        
        # Use 2D mapper for pixel-perfect alignment
        mapper = vtk.vtkPolyDataMapper2D()
        mapper.SetInputData(pd)
        
        self.preview_line_actor = vtk.vtkActor2D()
        self.preview_line_actor.SetMapper(mapper)
        self.preview_line_actor.GetProperty().SetColor(1.0, 0.5, 0.0) # Orange
        self.preview_line_actor.GetProperty().SetLineWidth(3)
        
        self.app.vtk_widget.renderer.AddActor2D(self.preview_line_actor)
        self.app.vtk_widget.render()

    def _clear_shape_preview(self):
        """Remove shape preview actors from renderer"""
        if hasattr(self, 'preview_line_actor') and self.preview_line_actor:
            try:
                # Handle both Actor2D and Actor cleanup for safety during transition
                self.app.vtk_widget.renderer.RemoveActor(self.preview_line_actor)
                self.app.vtk_widget.renderer.RemoveActor2D(self.preview_line_actor)
                self.app.vtk_widget.render()
            except:
                pass
            self.preview_line_actor = None
    def _select_in_rectangle_fast(self, start_pos, end_pos):
        """
        ✅ OPTIMIZED: Select both point cloud AND DXF entities in rectangle
        """
        import time
        start_time = time.time()
        
        # Create progress dialog
        progress = QProgressDialog("Detecting elements...", "Cancel", 0, 100, self.app)
        progress.setWindowTitle("Selection Progress")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.setAutoClose(True)
        progress.setAutoReset(True)
        
        progress.setStyleSheet("""
            QProgressDialog {
                background-color: #2c2c2c;
                color: #f0f0f0;
            }
            QProgressBar {
                border: 2px solid #555;
                border-radius: 5px;
                text-align: center;
                background-color: #1c1c1c;
                color: #f0f0f0;
            }
            QProgressBar::chunk {
                background-color: #ff6f00;
                border-radius: 3px;
            }
            QPushButton {
                background-color: #3c3c3c;
                color: #f0f0f0;
                border: 1px solid #555;
                padding: 5px 15px;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: #4c4c4c;
            }
        """)
        
        try:
            # Get screen bounds
            x1, y1 = start_pos
            x2, y2 = end_pos
            
            xmin, xmax = min(x1, x2), max(x1, x2)
            ymin, ymax = min(y1, y2), max(y1, y2)
            
            # ========================================
            # PART 1: Select Point Cloud (50% of progress)
            # ========================================
            progress.setLabelText("Analyzing point cloud...")
            progress.setValue(5)
            
            if hasattr(self.app, 'data') and self.app.data is not None:
                self._select_points_fast(xmin, xmax, ymin, ymax, progress, 5, 50)
            else:
                self.selected_mask = None
                self.selected_count = 0
            
            if progress.wasCanceled():
                print("⚠️ Selection cancelled")
                self._clear_rubber_band()
                self.start_pos = None
                return
            
            # ========================================
            # PART 2: Select DXF Entities (50% of progress)
            # ========================================
            progress.setLabelText("Analyzing DXF entities...")
            progress.setValue(50)
            
            self._select_dxf_fast(xmin, xmax, ymin, ymax, progress, 50, 90)
            
            if progress.wasCanceled():
                print("⚠️ Selection cancelled")
                self._clear_rubber_band()
                self.start_pos = None
                return
            
            # ========================================
            # PART 3: Highlight selections
            # ========================================
            progress.setLabelText("Highlighting selections...")
            progress.setValue(95)
            
            self._clear_selection_highlight()
            
            # Highlight selected points
            if self.selected_count > 0:
                self._highlight_selected_points()
            
            # Highlight selected DXF
            if self.selected_dxf_actors:
                self._highlight_selected_dxf()
            
            progress.setValue(100)
            
            elapsed = time.time() - start_time
            total_selected = self.selected_count + len(self.selected_dxf_actors)
            
            print(f"   📊 Total selected: {total_selected:,} ({self.selected_count:,} points + {len(self.selected_dxf_actors)} DXF)")
            print(f"   ⚡ Time: {elapsed:.3f}s")
            
            # Update UI
            if hasattr(self.app, 'ribbon_manager'):
                identify_ribbon = self.app.ribbon_manager.ribbons.get('identify')
                if identify_ribbon:
                    identify_ribbon.update_selected_count(total_selected)
            
        except Exception as e:
            print(f"❌ Selection error: {e}")
            import traceback
            traceback.print_exc()
        
        finally:
            progress.close()
    
    def _select_points_fast(self, xmin, xmax, ymin, ymax, progress, start_pct, end_pct):
        """Select point cloud points in rectangle"""
        xyz = self.app.data.get('xyz')
        if xyz is None or len(xyz) == 0:
            self.selected_mask = None
            self.selected_count = 0
            return
        
        total_points = len(xyz)
        progress.setLabelText(f"Checking {total_points:,} points...")
        progress.setValue(start_pct + 5)
        
        # Get visible points mask
        visible_mask = np.ones(len(xyz), dtype=bool)
        
        if self.app.display_mode == "class" and hasattr(self.app, 'class_palette'):
            visible_classes = [c for c, v in self.app.class_palette.items() if v.get('show', True)]
            if visible_classes and 'classification' in self.app.data:
                classes = self.app.data['classification']
                visible_mask = np.isin(classes, visible_classes)
        
        visible_xyz = xyz[visible_mask]
        visible_indices = np.where(visible_mask)[0]
        
        progress.setValue(start_pct + 10)
        
        # Vectorized coordinate transformation
        renderer = self.app.vtk_widget.renderer
        camera = renderer.GetActiveCamera()
        
        composite = camera.GetCompositeProjectionTransformMatrix(
            renderer.GetTiledAspectRatio(), 0, 1
        )
        
        progress.setLabelText("Converting coordinates...")
        progress.setValue(start_pct + 20)
        
        # Batch transform
        ones = np.ones((len(visible_xyz), 1))
        homogeneous = np.hstack([visible_xyz, ones])
        
        matrix = np.zeros((4, 4))
        for i in range(4):
            for j in range(4):
                matrix[i, j] = composite.GetElement(i, j)
        
        transformed = homogeneous @ matrix.T
        
        progress.setValue(start_pct + 30)
        
        # Normalize by w
        with np.errstate(divide='ignore', invalid='ignore'):
            transformed[:, :3] /= transformed[:, 3:4]
        
        # Convert to display coordinates
        size = renderer.GetSize()
        origin = renderer.GetOrigin()
        width, height = size
        
        display_x = (transformed[:, 0] + 1.0) * width * 0.5 + origin[0]
        display_y = (transformed[:, 1] + 1.0) * height * 0.5 + origin[1]
        
        progress.setLabelText("Selecting points...")
        progress.setValue(start_pct + 40)
        
        # Vectorized bounds check
        in_bounds = (
            (display_x >= xmin) & (display_x <= xmax) &
            (display_y >= ymin) & (display_y <= ymax)
        )
        
        selected_visible_indices = visible_indices[in_bounds]
        
        # Create selection mask
        self.selected_mask = np.zeros(len(xyz), dtype=bool)
        self.selected_mask[selected_visible_indices] = True
        self.selected_count = len(selected_visible_indices)
        
        progress.setValue(end_pct)
        print(f"   ✅ Points selected: {self.selected_count:,}")
    
    def _select_dxf_fast(self, xmin, xmax, ymin, ymax, progress, start_pct, end_pct):
        """
        ✅ NEW: Select DXF actors whose geometry falls within rectangle
        """
        self.selected_dxf_actors = []
        
        if not hasattr(self.app, 'dxf_actors') or not self.app.dxf_actors:
            progress.setValue(end_pct)
            return
        
        total_dxf = len(self.app.dxf_actors)
        progress.setLabelText(f"Checking {total_dxf} DXF entities...")
        progress.setValue(start_pct + 5)
        
        renderer = self.app.vtk_widget.renderer
        
        for idx, dxf_data in enumerate(self.app.dxf_actors):
            # Update progress
            pct = start_pct + 5 + int((idx / total_dxf) * (end_pct - start_pct - 5))
            progress.setValue(pct)
            
            if progress.wasCanceled():
                return
            
            actors = dxf_data.get('actors', [])
            
            for actor in actors:
                # Skip if not visible
                if not actor.GetVisibility():
                    continue
                
                # Check if any part of this actor is in the rectangle
                if self._is_actor_in_rectangle(actor, xmin, xmax, ymin, ymax, renderer):
                    self.selected_dxf_actors.append({
                        'dxf_data': dxf_data,
                        'actor': actor
                    })
                    break  # One actor per DXF entity is enough
        
        progress.setValue(end_pct)
        print(f"   ✅ DXF entities selected: {len(self.selected_dxf_actors)}")
    
    def _is_actor_in_rectangle(self, actor, xmin, xmax, ymin, ymax, renderer):
        """
        ✅ FIXED: Check if any part of a VTK actor intersects with the screen rectangle
        Uses the same reliable coordinate transformation as point cloud selection
        """
        try:
            # Get actor's mapper
            mapper = actor.GetMapper()
            if not mapper:
                print(f"            ⚠️ No mapper")
                return False
            
            # Get polydata
            polydata = mapper.GetInput()
            if not polydata:
                print(f"            ⚠️ No polydata")
                return False
            
            # Get points
            points = polydata.GetPoints()
            if not points or points.GetNumberOfPoints() == 0:
                print(f"            ⚠️ No points in polydata")
                return False
            
            num_points = points.GetNumberOfPoints()
            print(f"            📍 Actor has {num_points} points")
            
            # ✅ Use the same transformation as point cloud selection
            camera = renderer.GetActiveCamera()
            
            # Get composite projection matrix
            composite = camera.GetCompositeProjectionTransformMatrix(
                renderer.GetTiledAspectRatio(), 0, 1
            )
            
            # Convert VTK matrix to numpy
            matrix = np.zeros((4, 4))
            for i in range(4):
                for j in range(4):
                    matrix[i, j] = composite.GetElement(i, j)
            
            # Sample points for performance (check max 100 points for large geometries)
            sample_step = max(1, num_points // 100)
            print(f"            📊 Sample step: {sample_step} (will check ~{num_points // sample_step} points)")
            
            points_checked = 0
            points_in_rect = 0
            
            for i in range(0, num_points, sample_step):
                world_pt = points.GetPoint(i)
                
                # Create homogeneous coordinate
                homogeneous = np.array([world_pt[0], world_pt[1], world_pt[2], 1.0])
                
                # Transform to clip space
                transformed = matrix @ homogeneous
                
                # Normalize by w
                if abs(transformed[3]) < 1e-10:
                    continue
                
                normalized = transformed[:3] / transformed[3]
                
                # Convert to display coordinates
                size = renderer.GetSize()
                width, height = size
                
                display_x = (normalized[0] + 1.0) * width * 0.5
                display_y = (normalized[1] + 1.0) * height * 0.5
                
                points_checked += 1
                
                # Debug first and last few points
                if points_checked <= 2 or i >= num_points - 2:
                    print(f"            Point {i}: world=({world_pt[0]:.3f}, {world_pt[1]:.3f}, {world_pt[2]:.3f})")
                    print(f"                     → display=({display_x:.1f}, {display_y:.1f})")
                    print(f"                     → in rect? x:{xmin:.1f}≤{display_x:.1f}≤{xmax:.1f}, y:{ymin:.1f}≤{display_y:.1f}≤{ymax:.1f}")
                
                # Check if in rectangle
                if xmin <= display_x <= xmax and ymin <= display_y <= ymax:
                    points_in_rect += 1
                    if points_in_rect == 1:  # First match
                        print(f"            ✅ FIRST MATCH at point {i}: ({display_x:.1f}, {display_y:.1f})")
                    return True
            
            print(f"            ❌ No match: checked {points_checked} points, {points_in_rect} were in rectangle bounds")
            return False
            
        except Exception as e:
            print(f"            ❌ Exception in check: {e}")
            import traceback
            traceback.print_exc()
            return False
                    
                
    def _draw_rubber_band(self, start_pos, end_pos):
        """Draw a rubber band rectangle on screen"""
        self._clear_rubber_band()
        
        x1, y1 = start_pos
        x2, y2 = end_pos
        
        points = vtk.vtkPoints()
        points.InsertNextPoint(x1, y1, 0)
        points.InsertNextPoint(x2, y1, 0)
        points.InsertNextPoint(x2, y2, 0)
        points.InsertNextPoint(x1, y2, 0)
        points.InsertNextPoint(x1, y1, 0)
        
        lines = vtk.vtkCellArray()
        lines.InsertNextCell(5)
        for i in range(5):
            lines.InsertCellPoint(i)
        
        polydata = vtk.vtkPolyData()
        polydata.SetPoints(points)
        polydata.SetLines(lines)
        
        mapper = vtk.vtkPolyDataMapper2D()
        mapper.SetInputData(polydata)
        
        self.rubber_band_actor = vtk.vtkActor2D()
        self.rubber_band_actor.SetMapper(mapper)
        self.rubber_band_actor.GetProperty().SetColor(1.0, 0.5, 0.0)
        self.rubber_band_actor.GetProperty().SetLineWidth(4)
        self.rubber_band_actor.GetProperty().SetOpacity(0.8)
        
        self.app.vtk_widget.renderer.AddActor2D(self.rubber_band_actor)
        self.app.vtk_widget.render()
        
    def _clear_rubber_band(self):
        """Remove rubber band from display"""
        if self.rubber_band_actor:
            try:
                self.app.vtk_widget.renderer.RemoveActor2D(self.rubber_band_actor)
                self.app.vtk_widget.render()
            except Exception:
                pass
            self.rubber_band_actor = None
    
    def _clear_selection_highlight(self):
        """Clear all selection highlights"""
        # Clear point highlight
        if hasattr(self.app.vtk_widget, 'actors') and 'selection_highlight' in self.app.vtk_widget.actors:
            try:
                self.app.vtk_widget.remove_actor('selection_highlight', render=False)
            except Exception:
                pass
        
        # ✅ Clear DXF highlight overlays
        if hasattr(self.app.vtk_widget, 'actors') and 'dxf_selection_highlight' in self.app.vtk_widget.actors:
            try:
                self.app.vtk_widget.remove_actor('dxf_selection_highlight', render=False)
            except Exception:
                pass
            
    def _highlight_selected_points(self):
        """Visually highlight the selected points"""
        if self.selected_mask is None or not np.any(self.selected_mask):
            return
        
        xyz = self.app.data['xyz']
        selected_xyz = xyz[self.selected_mask]
        
        print(f"   🎨 Highlighting {len(selected_xyz):,} points in orange")
        
        import pyvista as pv
        cloud = pv.PolyData(selected_xyz)
        
        self.app.vtk_widget.add_points(
            cloud,
            color='orange',
            point_size=12,
            render_points_as_spheres=True,
            name='selection_highlight',
            reset_camera=False,
            render=False
        )
    
    def _highlight_selected_dxf(self):
        """
        ✅ NEW: Highlight selected DXF entities by changing their color to orange
        """
        print(f"   🎨 Highlighting {len(self.selected_dxf_actors)} DXF entities in orange")
        
        for item in self.selected_dxf_actors:
            actor = item['actor']
            try:
                # Change color to orange
                actor.GetProperty().SetColor(1.0, 0.5, 0.0)  # Orange
                actor.GetProperty().SetLineWidth(5)  # Thicker
                actor.GetProperty().SetOpacity(1.0)
            except Exception:
                pass
        
        self.app.vtk_widget.render()
    
    def _show_delete_confirmation(self):
        """Show delete confirmation, then optional save-deleted-part-as-LAZ dialog."""
        total_selected = self.selected_count + len(self.selected_dxf_actors)

        if total_selected == 0:
            print("⚠️ Nothing selected")
            return

        message = f"Delete selected items?\n\n"
        message += f"• {self.selected_count:,} point cloud points\n"
        message += f"• {len(self.selected_dxf_actors)} DXF entities\n\n"
        message += "This action cannot be undone."

        from PySide6.QtCore import QCoreApplication
        QCoreApplication.processEvents()

        # Preserve selection through dialogs
        stored_mask = self.selected_mask.copy() if self.selected_mask is not None else None
        stored_dxf = list(self.selected_dxf_actors)
        stored_count = self.selected_count

        reply = QMessageBox.question(
            self.app,
            "Delete Selection",
            message,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        # Restore selection state after dialog
        self.selected_mask = stored_mask
        self.selected_dxf_actors = stored_dxf
        self.selected_count = stored_count

        if reply != QMessageBox.Yes:
            print("🚫 Deletion cancelled")
            self._cancel_selection()
            return

        # SECOND DIALOG
        save_reply = QMessageBox.question(
            self.app,
            "Save Deleted Part as LAZ",
            "Do you want to save the deleted part as a new .laz file?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes
        )

        save_deleted_part = (save_reply == QMessageBox.Yes)

        self._delete_selection(save_deleted_part=save_deleted_part)
    
    def _cancel_selection(self):
        """Cancel selection and clear highlights"""
        print("   🔄 Cancelling selection and restoring colors...")
        
        # Clear rubber band and shape preview
        self._clear_rubber_band()
        self._clear_shape_preview()
        
        # Restore DXF colors BEFORE clearing selection list
        # ... (rest of the DXF restore logic)
        for item in self.selected_dxf_actors:
            actor = item['actor']
            dxf_data = item['dxf_data']
            try:
                # Restore original color
                original_color = dxf_data.get('color', (0, 1, 0))
                actor.GetProperty().SetColor(*original_color)
                actor.GetProperty().SetLineWidth(2)
                actor.GetProperty().SetOpacity(0.8)
            except Exception as e:
                print(f"⚠️ Failed to restore actor color: {e}")
        
        # Clear highlights
        self._clear_selection_highlight()
        
        # Force render to show restored colors
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass
        
        # Clear selection state
        self.selected_mask = None
        self.selected_count = 0
        self.selected_dxf_actors = []
        self.start_pos = None
        self.world_start_pos = None
        self.polygon_points = []
        self.world_polygon_points = []
        self.is_drawing = False
        
        # Update UI
        if hasattr(self.app, 'ribbon_manager'):
            identify_ribbon = self.app.ribbon_manager.ribbons.get('identify')
            if identify_ribbon:
                identify_ribbon.update_selected_count(0)
        
        self.app.statusBar().showMessage("🚫 Selection cancelled", 2000)
    
    def _delete_selection(self, save_deleted_part=False):
        """Delete selected points/DXF and optionally save the deleted point subset as .laz."""
        progress = QProgressDialog("Deleting selection...", None, 0, 100, self.app)
        progress.setWindowTitle("Deletion Progress")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.setCancelButton(None)
        
        progress.setStyleSheet("""
            QProgressDialog {
                background-color: #2c2c2c;
                color: #f0f0f0;
            }
            QProgressBar {
                border: 2px solid #555;
                border-radius: 5px;
                text-align: center;
                background-color: #1c1c1c;
                color: #f0f0f0;
            }
            QProgressBar::chunk {
                background-color: #d32f2f;
                border-radius: 3px;
            }
        """)
        
        try:
            deleted_points = 0
            deleted_dxf = 0
            
            # ✅ NEW: Save deleted data BEFORE removing
            deleted_xyz = None
            deleted_classification = None
            deleted_rgb = None
            deleted_intensity = None
            
            # ========================================
            # PART 1: Save & Delete Point Cloud Points (50%)
            # ========================================
            if self.selected_count > 0:
                progress.setLabelText(f"Saving deleted points...")
                progress.setValue(5)
                
                # Extract deleted points data
                xyz = self.app.data['xyz']
                deleted_xyz = xyz[self.selected_mask].copy()
                
                if 'classification' in self.app.data:
                    deleted_classification = self.app.data['classification'][self.selected_mask].copy()
                
                if 'rgb' in self.app.data and self.app.data['rgb'] is not None:
                    deleted_rgb = self.app.data['rgb'][self.selected_mask].copy()
                
                if 'intensity' in self.app.data and self.app.data['intensity'] is not None:
                    deleted_intensity = self.app.data['intensity'][self.selected_mask].copy()
                
                progress.setLabelText(f"Deleting {self.selected_count:,} points...")
                progress.setValue(10)
                
                # Create keep mask
                keep_mask = ~self.selected_mask
                
                # Update all data arrays
                self.app.data['xyz'] = self.app.data['xyz'][keep_mask]
                progress.setValue(20)
                
                if 'classification' in self.app.data:
                    self.app.data['classification'] = self.app.data['classification'][keep_mask]
                progress.setValue(30)
                
                if 'rgb' in self.app.data and self.app.data['rgb'] is not None:
                    self.app.data['rgb'] = self.app.data['rgb'][keep_mask]
                progress.setValue(40)
                
                if 'intensity' in self.app.data and self.app.data['intensity'] is not None:
                    self.app.data['intensity'] = self.app.data['intensity'][keep_mask]
                progress.setValue(50)
                
                deleted_points = self.selected_count
                print(f"   ✅ Deleted {deleted_points:,} points")
            
            # ========================================
            # PART 2: Delete DXF Entities (25%)
            # ========================================
            if self.selected_dxf_actors:
                progress.setLabelText(f"Deleting {len(self.selected_dxf_actors)} DXF entities...")
                progress.setValue(55)
                
                renderer = self.app.vtk_widget.renderer
                
                # Group by DXF data to remove entire entities
                dxf_to_remove = set()
                
                for item in self.selected_dxf_actors:
                    dxf_data = item['dxf_data']
                    actor = item['actor']
                    
                    # Remove actor from renderer
                    try:
                        renderer.RemoveActor(actor)
                    except Exception:
                        pass
                    
                    dxf_to_remove.add(id(dxf_data))
                
                progress.setValue(65)
                
                # Remove from app's dxf_actors list
                self.app.dxf_actors = [
                    dxf for dxf in self.app.dxf_actors
                    if id(dxf) not in dxf_to_remove
                ]
                
                deleted_dxf = len(self.selected_dxf_actors)
                print(f"   ✅ Deleted {deleted_dxf} DXF entities")
                
                progress.setValue(70)
            
            # ========================================
            # PART 2.5: Optionally save DELETED PART as LAZ
            # ========================================
            saved_deleted_path = None
            progress.setLabelText("Preparing deleted data...")
            progress.setValue(72)

            if save_deleted_part and deleted_xyz is not None and len(deleted_xyz) > 0:
                progress.setLabelText("Saving deleted part as .laz...")
                saved_deleted_path = self._save_deleted_data_as_laz(
                    deleted_xyz,
                    deleted_points_classification=deleted_classification,
                    deleted_points_rgb=deleted_rgb,
                    deleted_points_intensity=deleted_intensity,
                    deleted_dxf_count=deleted_dxf
                )
                if saved_deleted_path:
                    print(f"💾 Deleted part saved to: {saved_deleted_path}")
            elif save_deleted_part and (deleted_xyz is None or len(deleted_xyz) == 0):
                print("⚠️ No deleted point-cloud points available to save as LAZ")

            progress.setValue(75)
            
            # ========================================
            # PART 3: Clear and Refresh (25%)
            # ========================================
            progress.setLabelText("Clearing selection...")
            progress.setValue(80)
            
            self._clear_rubber_band()
            self._clear_selection_highlight()
            
            self.selected_mask = None
            self.selected_count = 0
            self.selected_dxf_actors = []
            self.start_pos = None
            self.is_drawing = False
            
            progress.setLabelText("Refreshing display...")
            progress.setValue(85)
            
            # Refresh point cloud display
            if deleted_points > 0:
                from gui.pointcloud_display import update_pointcloud
                update_pointcloud(self.app, self.app.display_mode)
            
            if deleted_dxf > 0 and deleted_points == 0:
                self.app.vtk_widget.render()
            
            progress.setValue(95)
            
            # Update UI
            if hasattr(self.app, 'ribbon_manager'):
                identify_ribbon = self.app.ribbon_manager.ribbons.get('identify')
                if identify_ribbon:
                    identify_ribbon.update_selected_count(0)
            
            # Update statistics
            if hasattr(self.app, 'point_count_widget') and self.app.point_count_widget:
                from gui.point_count_widget import refresh_point_statistics
                refresh_point_statistics(self.app)
            
            progress.setValue(100)
            
            # ========================================
            # PART 4: Clear Undo/Redo Stacks (CRITICAL)
            # ========================================
            # Point deletion invalidates all stored indices/masks.
            # We must clear stacks to prevent IndexErrors on undo.
            if hasattr(self.app, 'undo_stack'): self.app.undo_stack.clear()
            if hasattr(self.app, 'undostack'):  self.app.undostack.clear()
            if hasattr(self.app, 'redo_stack'): self.app.redo_stack.clear()
            if hasattr(self.app, 'redostack'):  self.app.redostack.clear()
            print("   🧹 Undo/Redo stacks cleared (indices invalidated by deletion)")

            # Show summary

            
            if save_deleted_part and saved_deleted_path:
                message = (
                    f"✅ Deleted {deleted_points:,} points + {deleted_dxf} DXF entities "
                    f"(saved deleted part: {saved_deleted_path.name})"
                )
            elif save_deleted_part and not saved_deleted_path:
                message = (
                    f"⚠️ Deleted {deleted_points:,} points + {deleted_dxf} DXF entities "
                    f"(save failed)"
                )
            else:
                message = f"✅ Deleted {deleted_points:,} points + {deleted_dxf} DXF entities"

            self.app.statusBar().showMessage(message, 5000)
            print(f"\n{message}")
            
            # ✅ RESET: Clear ALL selection state and preview actors so tool is fresh for reuse
            self._cancel_selection()
            
        except Exception as e:
            print(f"❌ Deletion error: {e}")
            import traceback
            traceback.print_exc()
        
        finally:
            progress.close()
    
    def delete_selected_points(self):
        """Public method called from ribbon buttons to delete current selection"""
        if self.selected_mask is None and not self.selected_dxf_actors:
            print("⚠️ No active selection to delete")
            self.app.statusBar().showMessage("⚠️ No selection to delete", 2000)
            return
        
        total_selected = self.selected_count + len(self.selected_dxf_actors)
        if total_selected == 0:
            print("⚠️ Nothing selected")
            self.app.statusBar().showMessage("⚠️ Nothing selected", 2000)
            return
        
        # Show the same confirmation dialog
        self._show_delete_confirmation()
    
    def _save_modified_dataset_as_laz(self):
        """
        Save the CURRENT MODIFIED point cloud as a new .laz file
        using filename serial numbering: file_001.laz, file_002.laz, etc.
        """
        try:
            import os
            from gui.save_pointcloud import save_pointcloud

            # Use loaded file / last save path as base
            base_path = (
                getattr(self.app, 'last_save_path', None)
                or getattr(self.app, 'loaded_file', None)
                or getattr(self.app, 'current_file_path', None)
            )

            if not base_path:
                print("⚠️ No base file path found - opening Save As dialog")
                save_pointcloud(
                    self.app,
                    path=None,
                    file_format="laz",
                    las_version="1.4",
                    show_dialog=True
                )
                return None

            # Build serial filename
            if hasattr(self.app, '_get_auto_incremented_path'):
                output_path = self.app._get_auto_incremented_path(base_path)
            else:
                output_path = self._get_auto_incremented_laz_path(base_path)

            # Force .laz extension
            root, _ = os.path.splitext(output_path)
            output_path = root + ".laz"

            save_pointcloud(
                self.app,
                path=output_path,
                file_format="laz",
                las_version=getattr(self.app, 'last_save_version', "1.4"),
                show_dialog=False
            )

            print(f"💾 Modified point cloud saved to: {output_path}")
            return os.path.basename(output_path)

        except Exception as e:
            print(f"❌ Failed to save modified point cloud: {e}")
            import traceback
            traceback.print_exc()
            return None
        
    def _get_auto_incremented_laz_path(self, base_path):
        """
        Fallback serial generator if app._get_auto_incremented_path is unavailable.
        """
        import os
        import re

        directory = os.path.dirname(base_path)
        filename = os.path.basename(base_path)
        name, _ = os.path.splitext(filename)

        match = re.search(r'_(\d{3})$', name)

        if match:
            current_num = int(match.group(1))
            base_name = name[:match.start()]
            next_num = current_num + 1
            candidate = os.path.join(directory, f"{base_name}_{next_num:03d}.laz")
        else:
            candidate = os.path.join(directory, f"{name}_001.laz")

        counter = 1
        while os.path.exists(candidate):
            if match:
                candidate = os.path.join(directory, f"{base_name}_{current_num + counter:03d}.laz")
            else:
                candidate = os.path.join(directory, f"{name}_{counter:03d}.laz")
            counter += 1
            if counter > 999:
                raise Exception("Too many serial files already exist")

        return candidate

    def get_selection_summary(self):
        """Get summary of current selection"""
        return {
            'points': self.selected_count,
            'dxf_entities': len(self.selected_dxf_actors),
            'total': self.selected_count + len(self.selected_dxf_actors),
            'has_selection': (self.selected_count > 0) or (len(self.selected_dxf_actors) > 0)
        }

    def clear_selection(self):
        """Clear current selection without deleting"""
        self._cancel_selection()
        
        
        
    def _select_dxf_fast(self, xmin, xmax, ymin, ymax, progress, start_pct, end_pct):
        """
        ✅ NEW: Select DXF actors whose geometry falls within rectangle
        """
        self.selected_dxf_actors = []
        
        if not hasattr(self.app, 'dxf_actors') or not self.app.dxf_actors:
            print(f"   ⚠️ DEBUG: No dxf_actors attribute or empty list")
            print(f"   Has attribute: {hasattr(self.app, 'dxf_actors')}")
            if hasattr(self.app, 'dxf_actors'):
                print(f"   List length: {len(self.app.dxf_actors)}")
            progress.setValue(end_pct)
            return
        
        total_dxf = len(self.app.dxf_actors)
        print(f"   🔍 DEBUG: Found {total_dxf} DXF entities in app.dxf_actors")
        print(f"   🔍 DEBUG: Selection rectangle: x=[{xmin:.1f}, {xmax:.1f}], y=[{ymin:.1f}, {ymax:.1f}]")
        
        progress.setLabelText(f"Checking {total_dxf} DXF entities...")
        progress.setValue(start_pct + 5)
        
        renderer = self.app.vtk_widget.renderer
        
        for idx, dxf_data in enumerate(self.app.dxf_actors):
            # Update progress
            pct = start_pct + 5 + int((idx / total_dxf) * (end_pct - start_pct - 5))
            progress.setValue(pct)
            
            if progress.wasCanceled():
                return
            
            actors = dxf_data.get('actors', [])
            dxf_type = dxf_data.get('type', 'unknown')
            dxf_filename = dxf_data.get('filename', 'unknown')
            
            print(f"\n   📋 DEBUG: Entity {idx}:")
            print(f"      Type: {dxf_type}")
            print(f"      Filename: {dxf_filename}")
            print(f"      Number of actors: {len(actors)}")
            
            for actor_idx, actor in enumerate(actors):
                print(f"      🎭 Actor {actor_idx}:")
                
                # Skip if not visible
                if not actor.GetVisibility():
                    print(f"         ⚠️ Not visible - SKIPPING")
                    continue
                else:
                    print(f"         ✅ Visible")
                
                # Get actor bounds for debugging
                try:
                    bounds = actor.GetBounds()
                    print(f"         Bounds: {bounds}")
                except Exception:
                    print(f"         ⚠️ Could not get bounds")
                
                # Check if any part of this actor is in the rectangle
                print(f"         🔍 Checking if in rectangle...")
                is_in_rect = self._is_actor_in_rectangle(actor, xmin, xmax, ymin, ymax, renderer)
                
                if is_in_rect:
                    print(f"         ✅✅✅ SELECTED!")
                    self.selected_dxf_actors.append({
                        'dxf_data': dxf_data,
                        'actor': actor
                    })
                    break  # One actor per DXF entity is enough
                else:
                    print(f"         ❌ Not in rectangle")
        
        progress.setValue(end_pct)
        print(f"\n   ✅ Total DXF entities selected: {len(self.selected_dxf_actors)}")
        
        
        
    def _save_deleted_data_as_laz(
        self,
        deleted_points_xyz,
        deleted_points_classification=None,
        deleted_points_rgb=None,
        deleted_points_intensity=None,
        deleted_dxf_count=0
    ):
        """
        Save the DELETED POINT SUBSET as a serial-numbered .laz file.
        Example:
            123.las -> 123_deleted_001.laz
            123.las -> 123_deleted_002.laz
        """
        try:
            import os
            from pathlib import Path
            import laspy
            import numpy as np

            # Use the real app paths
            base_path = (
                getattr(self.app, "last_save_path", None)
                or getattr(self.app, "loaded_file", None)
                or getattr(self.app, "current_file_path", None)
            )

            if not base_path:
                print("⚠️ No base file path found - cannot save deleted data")
                return None

            base_path = Path(base_path)
            save_path = self._get_next_deleted_laz_path(base_path)

            print(f"\n💾 Saving deleted part to: {save_path}")

            if deleted_points_xyz is None or len(deleted_points_xyz) == 0:
                print("⚠️ No deleted point-cloud points to save")
                return None

            xyz = np.asarray(deleted_points_xyz)
            n = len(xyz)

            # Determine RGB availability
            has_rgb = deleted_points_rgb is not None
            point_format = 7 if has_rgb else 6   # LAS 1.4 / RGB or no RGB
            header = laspy.LasHeader(point_format=point_format, version="1.4")

            # Better offsets/scales
            mins = np.min(xyz, axis=0)
            header.offsets = np.array([mins[0], mins[1], mins[2]])
            header.scales = np.array([0.001, 0.001, 0.001])

            # Embed CRS if available
            try:
                import pyproj
                if getattr(self.app, "project_crs_wkt", None):
                    crs = pyproj.CRS.from_wkt(self.app.project_crs_wkt)
                    header.parse_crs(crs)
                elif getattr(self.app, "project_crs_epsg", None):
                    crs = pyproj.CRS.from_epsg(self.app.project_crs_epsg)
                    header.parse_crs(crs)
            except Exception as e:
                print(f"⚠️ CRS embedding skipped: {e}")

            las = laspy.LasData(header)
            las.x = xyz[:, 0]
            las.y = xyz[:, 1]
            las.z = xyz[:, 2]

            # Classification
            if deleted_points_classification is not None and len(deleted_points_classification) == n:
                las.classification = np.asarray(deleted_points_classification).astype(np.uint8, copy=False)
            else:
                las.classification = np.zeros(n, dtype=np.uint8)

            # RGB
            if has_rgb:
                rgb = np.asarray(deleted_points_rgb)

                if np.issubdtype(rgb.dtype, np.floating):
                    rgb = np.clip(rgb, 0.0, 1.0)
                    rgb16 = (rgb * 65535.0).round().astype(np.uint16)
                else:
                    mx = int(rgb.max()) if rgb.size else 0
                    if mx <= 255:
                        rgb16 = rgb.astype(np.uint16) * 256
                    else:
                        rgb16 = np.clip(rgb, 0, 65535).astype(np.uint16)

                las.red = rgb16[:, 0]
                las.green = rgb16[:, 1]
                las.blue = rgb16[:, 2]

            # Intensity
            if deleted_points_intensity is not None and len(deleted_points_intensity) == n:
                inten = np.asarray(deleted_points_intensity)
                if np.issubdtype(inten.dtype, np.floating):
                    mx = float(np.nanmax(inten)) if inten.size else 0.0
                    if mx <= 1.0:
                        inten = np.clip(inten, 0.0, 1.0) * 65535.0
                    inten = np.clip(inten, 0.0, 65535.0)
                    inten = inten.round().astype(np.uint16)
                else:
                    inten = np.clip(inten, 0, 65535).astype(np.uint16)

                las.intensity = inten

            las.write(str(save_path))
            print(f"✅ Saved deleted subset: {save_path}")

            # Optional PRJ sidecar
            try:
                prj_path = save_path.with_suffix(".prj")
                if getattr(self.app, "project_crs_wkt", None):
                    with open(prj_path, "w") as f:
                        f.write(self.app.project_crs_wkt)
                elif getattr(self.app, "project_crs_epsg", None):
                    with open(prj_path, "w") as f:
                        f.write(f"EPSG:{self.app.project_crs_epsg}")
            except Exception as e:
                print(f"⚠️ Failed writing PRJ sidecar: {e}")

            return save_path

        except Exception as e:
            print(f"❌ Failed to save deleted data as LAZ: {e}")
            import traceback
            traceback.print_exc()
            return None
        
    def _get_next_deleted_laz_path(self, base_path):
        """
        Return next serial-numbered deleted-subset LAZ file.
        Example:
            123.las -> 123_deleted_001.laz
            123.las -> 123_deleted_002.laz
        """
        import re
        from pathlib import Path

        directory = base_path.parent
        stem = base_path.stem

        match = re.match(r"^(.*?)(_deleted_\d{3})$", stem, re.IGNORECASE)
        if match:
            stem = match.group(1)

        counter = 1
        while counter <= 999:
            candidate = directory / f"{stem}_deleted_{counter:03d}.laz"
            if not candidate.exists():
                return candidate
            counter += 1

        raise Exception("Too many deleted-part files already exist")
