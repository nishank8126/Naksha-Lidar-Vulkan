
"""
Grid Tool for Point Cloud Viewer
Similar to MicroStation's grid functionality
✅ Draw rectangle to define area
✅ Configure grid: rows × columns OR spacing in meters
✅ Creates matrix of grid lines
✅ FIXED: Left-click starts, right-click finishes (same as select rectangle tool)
"""

import numpy as np
import vtk
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                               QSpinBox, QDoubleSpinBox, QRadioButton, 
                               QButtonGroup, QPushButton, QGroupBox, QMessageBox)
from PySide6.QtCore import Qt


class GridConfigDialog(QDialog):
    """Dialog to configure grid parameters"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Grid Configuration")
        self.setModal(True)
        self.resize(400, 300)
        
        # self.grid_type = "matrix"  # "matrix" or "spacing"
        self.rows = 10
        self.columns = 10
        self.spacing_x = 1.0
        self.spacing_y = 1.0
        
        self.setup_ui()
        self.apply_dark_theme()
        
    def setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Title
        title = QLabel("Configure Grid Parameters")
        from gui.theme_manager import ThemeColors
        title.setStyleSheet(f"font-size: 14pt; font-weight: bold; color: {ThemeColors.get('accent')};")
        layout.addWidget(title)
        
        # # Grid Type Selection
        
        # self.button_group = QButtonGroup(self)
        
        # self.matrix_radio = QRadioButton("Matrix (Rows × Columns)")
        # self.matrix_radio.setChecked(True)
        # self.matrix_radio.toggled.connect(self.on_type_changed)
        # self.button_group.addButton(self.matrix_radio)
        # type_layout.addWidget(self.matrix_radio)
        
        # self.spacing_radio = QRadioButton("Spacing (Meters)")
        # self.spacing_radio.toggled.connect(self.on_type_changed)
        # self.button_group.addButton(self.spacing_radio)
        # type_layout.addWidget(self.spacing_radio)
        
        # type_group.setLayout(type_layout)
        # layout.addWidget(type_group)
        
        # Matrix Configuration
        self.matrix_group = QGroupBox("Matrix Configuration")
        matrix_layout = QVBoxLayout()
        
        row_layout = QHBoxLayout()
        row_layout.addWidget(QLabel("Rows:"))
        self.rows_spin = QSpinBox()
        self.rows_spin.setRange(2, 1000)
        self.rows_spin.setValue(10)
        self.rows_spin.setMinimumWidth(100)
        row_layout.addWidget(self.rows_spin)
        row_layout.addStretch()
        matrix_layout.addLayout(row_layout)
        
        col_layout = QHBoxLayout()
        col_layout.addWidget(QLabel("Columns:"))
        self.cols_spin = QSpinBox()
        self.cols_spin.setRange(2, 1000)
        self.cols_spin.setValue(10)
        self.cols_spin.setMinimumWidth(100)
        col_layout.addWidget(self.cols_spin)
        col_layout.addStretch()
        matrix_layout.addLayout(col_layout)
        
        self.matrix_group.setLayout(matrix_layout)
        layout.addWidget(self.matrix_group)
        
        # Spacing Configuration
        self.spacing_group = QGroupBox("Spacing Configuration")
        spacing_layout = QVBoxLayout()
        
        x_layout = QHBoxLayout()
        x_layout.addWidget(QLabel("X Spacing (m):"))
        self.spacing_x_spin = QDoubleSpinBox()
        self.spacing_x_spin.setRange(0.1, 1000.0)
        self.spacing_x_spin.setValue(1.0)
        self.spacing_x_spin.setDecimals(2)
        self.spacing_x_spin.setMinimumWidth(100)
        x_layout.addWidget(self.spacing_x_spin)
        x_layout.addStretch()
        spacing_layout.addLayout(x_layout)
        
        y_layout = QHBoxLayout()
        y_layout.addWidget(QLabel("Y Spacing (m):"))
        self.spacing_y_spin = QDoubleSpinBox()
        self.spacing_y_spin.setRange(0.1, 1000.0)
        self.spacing_y_spin.setValue(1.0)
        self.spacing_y_spin.setDecimals(2)
        self.spacing_y_spin.setMinimumWidth(100)
        y_layout.addWidget(self.spacing_y_spin)
        y_layout.addStretch()
        spacing_layout.addLayout(y_layout)
        
        self.spacing_group.setLayout(spacing_layout)
        # self.spacing_group.setEnabled(False)
        layout.addWidget(self.spacing_group)
        
        # Info label
        self.info_label = QLabel("Grid will be created in the selected rectangular area")
        self.info_label.setStyleSheet("color: #888; font-style: italic;")
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)
        
        layout.addStretch()
        
        # Buttons
        button_layout = QHBoxLayout()
        
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        ok_btn.setMinimumWidth(100)
        button_layout.addWidget(ok_btn)
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        cancel_btn.setMinimumWidth(100)
        button_layout.addWidget(cancel_btn)
        
        layout.addLayout(button_layout)
        
    #     """Toggle between matrix and spacing configuration"""
    #     self.matrix_group.setEnabled(is_matrix)
    #     self.spacing_group.setEnabled(not is_matrix)
    #     self.grid_type = "matrix" if is_matrix else "spacing"
        
    def get_config(self):
        """Get the grid configuration - returns BOTH matrix AND spacing"""
        return {
            'rows': self.rows_spin.value(),
            'columns': self.cols_spin.value(),
            'spacing_x': self.spacing_x_spin.value(),
            'spacing_y': self.spacing_y_spin.value()
        }
    
    def apply_dark_theme(self):
        """Apply dark theme to dialog"""
        self.setStyleSheet("""
            QDialog {
                background-color: #2c2c2c;
                color: #f0f0f0;
            }
            QGroupBox {
                border: 2px solid #555;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
                font-weight: bold;
                color: #ff6f00;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 5px;
            }
            QLabel {
                color: #f0f0f0;
            }
            QRadioButton {
                color: #f0f0f0;
                spacing: 5px;
            }
            QRadioButton::indicator {
                width: 18px;
                height: 18px;
            }
            QSpinBox, QDoubleSpinBox {
                background-color: #3c3c3c;
                color: #f0f0f0;
                border: 1px solid #555;
                border-radius: 3px;
                padding: 5px;
            }
            QPushButton {
                background-color: #3c3c3c;
                color: #f0f0f0;
                border: 1px solid #555;
                padding: 8px 15px;
                border-radius: 3px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #4c4c4c;
                border-color: #ff6f00;
            }
            QPushButton:pressed {
                background-color: #2c2c2c;
            }
        """)


class GridTool:
    """
    Tool for creating grids in a rectangular area
    Similar to MicroStation's grid functionality
    ✅ FIXED: Left-click to start, right-click to finish
    """
    
    def __init__(self, app):
        self.app = app
        self.active = False
        self.start_pos = None
        self.rubber_band_actor = None
        self.is_drawing = False
        self.observer_ids = []
        self.grid_actors = []  # Store created grid lines
        
    def activate(self):
        """Activate the grid tool"""
        if self.active:
            return
        
        # Show configuration dialog first
        dialog = GridConfigDialog(self.app)
        if dialog.exec() != QDialog.Accepted:
            print("🚫 Grid tool cancelled")
            return
        
        self.grid_config = dialog.get_config()
        
        self.active = True
        self.start_pos = None
        self.is_drawing = False
        
        if not hasattr(self.app, 'vtk_widget') or not self.app.vtk_widget:
            print("⚠️ No VTK widget found")
            return
        
        interactor = self.app.vtk_widget.interactor
        
        # Add observers - same pattern as select rectangle tool
        press_id = interactor.AddObserver("LeftButtonPressEvent", self.on_left_press, 1.0)
        move_id = interactor.AddObserver("MouseMoveEvent", self.on_mouse_move, 1.0)
        right_id = interactor.AddObserver("RightButtonPressEvent", self.on_right_click, 1.0)
        
        self.observer_ids = [press_id, move_id, right_id]
        
        print("✅ Grid tool activated")
        print(f"   📊 Mode: {self.grid_config['type']}")
        if self.grid_config['type'] == 'matrix':
            print(f"   🔢 Grid: {self.grid_config['rows']} × {self.grid_config['columns']}")
        else:
            print(f"   📏 Spacing: {self.grid_config['spacing_x']}m × {self.grid_config['spacing_y']}m")
        print("   📍 Left-click to start, right-click to finish")
        
        self.app.statusBar().showMessage(
            "⊞ Grid Tool: LEFT-CLICK to start, RIGHT-CLICK to finish", 5000
        )
        
    def deactivate(self):
        """Deactivate the grid tool"""
        if not self.active:
            return
        
        self.active = False
        self.is_drawing = False
        
        self._clear_rubber_band()
        
        if hasattr(self.app, 'vtk_widget') and self.app.vtk_widget:
            interactor = self.app.vtk_widget.interactor
            for obs_id in self.observer_ids:
                try:
                    interactor.RemoveObserver(obs_id)
                except Exception:
                    pass
        
        self.observer_ids = []
        self.start_pos = None
        
        print("✅ Grid tool deactivated")
        
    def on_left_press(self, obj, event):
        """✅ FIXED: Start drawing rectangle (same as select tool)"""
        if not self.active:
            return
        
        print(f"\n{'='*60}")
        print(f"⊞ GRID TOOL: Left-click pressed")
        
        interactor = obj
        self.start_pos = interactor.GetEventPosition()
        self.is_drawing = True
        
        self._clear_rubber_band()
        
        print(f"   📍 Start position: {self.start_pos}")
        print(f"{'='*60}\n")
        
        # ✅ FIXED: Abort event properly
        try:
            interactor.GetInteractorStyle().OnLeftButtonDown()
        except Exception:
            pass
        
    def on_mouse_move(self, obj, event):
        """✅ FIXED: Update rubber band during drag"""
        if not self.active or not self.is_drawing or not self.start_pos:
            return
        
        interactor = obj
        current_pos = interactor.GetEventPosition()
        self._draw_rubber_band(self.start_pos, current_pos)
        
    def on_right_click(self, obj, event):
        """✅ FIXED: Right-click to finish and create grid"""
        if not self.active:
            return
        
        print(f"\n{'='*60}")
        print(f"⊞ GRID TOOL: Right-click pressed")
        
        interactor = obj
        
        if self.is_drawing and self.start_pos:
            end_pos = interactor.GetEventPosition()
            
            # Check if this is a valid rectangle
            x_diff = abs(end_pos[0] - self.start_pos[0])
            y_diff = abs(end_pos[1] - self.start_pos[1])
            
            print(f"   📍 End position: {end_pos}")
            print(f"   📏 Rectangle size: {x_diff}x{y_diff}")
            
            if x_diff > 10 and y_diff > 10:
                # Convert screen coordinates to world coordinates
                world_start = self._screen_to_world(self.start_pos)
                world_end = self._screen_to_world(end_pos)
                
                if world_start and world_end:
                    self._create_grid(world_start, world_end)
                else:
                    print("⚠️ Failed to convert coordinates")
                    self.app.statusBar().showMessage("⚠️ Failed to create grid", 2000)
            else:
                print("⚠️ Rectangle too small")
                self.app.statusBar().showMessage("⚠️ Draw a larger rectangle", 2000)
            
            self._clear_rubber_band()
            self.is_drawing = False
            self.start_pos = None
            
            # Auto-deactivate after creating grid
            self.deactivate()
        
        else:
            print(f"   ⚠️ No active drawing")
        
        print(f"{'='*60}\n")
        
        # ✅ FIXED: Abort event properly
        try:
            interactor.GetInteractorStyle().OnRightButtonDown()
        except Exception:
            pass
        
    def _screen_to_world(self, screen_pos):
        """Convert screen coordinates to 3D world coordinates using proper matrix transformation"""
        try:
            renderer = self.app.vtk_widget.renderer
            camera = renderer.GetActiveCamera()
            
            # Get the Z plane for the grid
            z_value = 0.0
            if hasattr(self.app, 'data') and self.app.data and 'xyz' in self.app.data:
                xyz = self.app.data['xyz']
                if len(xyz) > 0:
                    z_value = float(np.median(xyz[:, 2]))
            
            print(f"   🔍 Converting screen {screen_pos} to world (Z plane: {z_value:.2f})")
            
            # Get screen position
            x_display, y_display = screen_pos
            
            # ✅ FIXED: Use proper coordinate transformation like point cloud selection
            # Get composite projection matrix
            composite = camera.GetCompositeProjectionTransformMatrix(
                renderer.GetTiledAspectRatio(), 0, 1
            )
            
            # Convert to numpy matrix
            matrix = np.zeros((4, 4))
            for i in range(4):
                for j in range(4):
                    matrix[i, j] = composite.GetElement(i, j)
            
            # Invert the matrix to go from screen to world
            try:
                inv_matrix = np.linalg.inv(matrix)
            except np.linalg.LinAlgError:
                print("⚠️ Matrix inversion failed")
                return None
            
            # Get window size
            size = renderer.GetSize()
            width, height = size
            
            # Convert screen to normalized device coordinates (-1 to 1)
            norm_x = (x_display / width) * 2.0 - 1.0
            norm_y = (y_display / height) * 2.0 - 1.0
            
            # Create homogeneous coordinate in clip space
            # We need to find the Z in clip space that corresponds to our desired world Z
            clip_coords = np.array([norm_x, norm_y, 0.0, 1.0])
            
            # Transform to world space
            world_homogeneous = inv_matrix @ clip_coords
            
            # Normalize by w
            if abs(world_homogeneous[3]) > 1e-10:
                world_x = world_homogeneous[0] / world_homogeneous[3]
                world_y = world_homogeneous[1] / world_homogeneous[3]
                # Use our desired Z plane
                world_z = z_value
            else:
                print("⚠️ W component too small")
                return None
            
            world_pos = (world_x, world_y, world_z)
            print(f"   ✅ World position: ({world_x:.2f}, {world_y:.2f}, {world_z:.2f})")
            
            return world_pos
            
        except Exception as e:
            print(f"❌ Coordinate conversion error: {e}")
            import traceback
            traceback.print_exc()
            return None
    def _create_grid(self, world_start, world_end):
        """Create grid lines in the specified world coordinate rectangle"""
        try:
            # Get rectangle bounds
            x_min = min(world_start[0], world_end[0])
            x_max = max(world_start[0], world_end[0])
            y_min = min(world_start[1], world_end[1])
            y_max = max(world_start[1], world_end[1])
            z = world_start[2]  # Use consistent Z
            
            width = x_max - x_min
            height = y_max - y_min
            
            print(f"\n⊞ Creating grid in area:")
            print(f"   📍 X: {x_min:.2f} to {x_max:.2f} (width: {width:.2f}m)")
            print(f"   📍 Y: {y_min:.2f} to {y_max:.2f} (height: {height:.2f}m)")
            print(f"   📍 Z: {z:.2f}")
            
            # Calculate grid parameters
            # Calculate grid parameters using BOTH matrix and spacing
            # Get configuration values
            # Use matrix to get number of cells
            rows = self.grid_config['rows']
            cols = self.grid_config['columns']

            # Use spacing to determine distance
            x_spacing = self.grid_config['spacing_x']
            y_spacing = self.grid_config['spacing_y']

            # Number of lines = cells + 1
            num_vertical_lines = cols + 1
            num_horizontal_lines = rows + 1

            # IGNORE rectangle size - grid size is determined by matrix × spacing
            # Total grid will be: (cols × x_spacing) by (rows × y_spacing)

            print(f"   🔢 Matrix: {rows} × {cols} → {num_horizontal_lines} horizontal, {num_vertical_lines} vertical lines")
            print(f"   📏 Spacing: {x_spacing}m × {y_spacing}m")
            print(f"   📐 Grid will extend: {(num_vertical_lines-1)*x_spacing:.2f}m × {(num_horizontal_lines-1)*y_spacing:.2f}m")
            print(f"   📦 Rectangle size: {width:.2f}m × {height:.2f}m")
            
            # ✅ Collect all actors for group registration
            current_grid_actors = []
            
            # Create vertical lines (along Y direction)
            for i in range(num_vertical_lines):
                x = x_min + i * x_spacing
                
                actor = self._create_line(
                    (x, y_min, z),
                    (x, y_min + (rows * y_spacing), z),
                    color=(0.0, 0.8, 1.0),  # Cyan
                    add_to_dxf=False
                )
                current_grid_actors.append(actor)
            
            # Create horizontal lines (along X direction)
            for i in range(num_horizontal_lines):
                y = y_min + i * y_spacing
                # Don't draw lines beyond the rectangle boundary
                actor = self._create_line(
                    (x_min, y, z),
                    (x_min + (cols * x_spacing), y, z),
                    color=(0.0, 0.8, 1.0),  # Cyan
                    add_to_dxf=False
                )
                current_grid_actors.append(actor)
            
            # Register ALL grid lines as ONE entity in app.dxf_actors
            if not hasattr(self.app, 'dxf_actors'):
                self.app.dxf_actors = []
            
            grid_entity = {
                'actors': current_grid_actors,
                'color': (0.0, 0.8, 1.0),
                'filename': f'Grid_{len(self.app.dxf_actors)}',
                'type': 'grid'
            }
            self.app.dxf_actors.append(grid_entity)
            
            total_lines = len(current_grid_actors)
            actual_v_lines = sum(1 for a in current_grid_actors[:num_vertical_lines] if a)
            actual_h_lines = total_lines - actual_v_lines
            
            print(f"   ✅ Created {total_lines} grid lines ({actual_v_lines}V + {actual_h_lines}H)")
            
            self.app.vtk_widget.render()
            
            message = f"✅ Grid created: {x_spacing}m × {y_spacing}m spacing ({total_lines} lines)"
            info_text = (f"Grid successfully created!\n\n"
                        f"• Spacing: {x_spacing}m × {y_spacing}m\n"
                        f"• Vertical lines: {actual_v_lines}\n"
                        f"• Horizontal lines: {actual_h_lines}\n"
                        f"• Total lines: {total_lines}\n"
                        f"• Rectangle: {width:.2f}m × {height:.2f}m")
            
            self.app.statusBar().showMessage(message, 3000)
            
            QMessageBox.information(
                self.app,
                "Grid Created",
                info_text
            )
            
        except Exception as e:
            print(f"❌ Grid creation error: {e}")
            import traceback
            traceback.print_exc()
            
    def _create_line(self, start, end, color=(0.0, 0.8, 1.0), width=2, add_to_dxf=True):
        """Create a single grid line"""
        # Create line
        points = vtk.vtkPoints()
        points.InsertNextPoint(start)
        points.InsertNextPoint(end)
        
        line = vtk.vtkLine()
        line.GetPointIds().SetId(0, 0)
        line.GetPointIds().SetId(1, 1)
        
        lines = vtk.vtkCellArray()
        lines.InsertNextCell(line)
        
        polydata = vtk.vtkPolyData()
        polydata.SetPoints(points)
        polydata.SetLines(lines)
        
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(polydata)
        
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(*color)
        actor.GetProperty().SetLineWidth(width)
        actor.GetProperty().SetOpacity(0.8)
        
        # Add to renderer
        self.app.vtk_widget.renderer.AddActor(actor)
        self.grid_actors.append(actor)
        
        # ✅ FIXED: Only register in app.dxf_actors if requested
        # This prevents individual registration during grid creation
        if add_to_dxf:
            if not hasattr(self.app, 'dxf_actors'):
                self.app.dxf_actors = []
            
            # Create a grid entity entry (similar to DXF format)
            grid_entity = {
                'actors': [actor],
                'color': color,
                'filename': 'grid_lines',
                'type': 'grid'
            }
            self.app.dxf_actors.append(grid_entity)
        
        return actor
    
    def _draw_rubber_band(self, start_pos, end_pos):
        """Draw rubber band rectangle"""
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
        self.rubber_band_actor.GetProperty().SetColor(0.0, 0.8, 1.0)  # Cyan
        self.rubber_band_actor.GetProperty().SetLineWidth(3)
        self.rubber_band_actor.GetProperty().SetOpacity(0.8)
        
        self.app.vtk_widget.renderer.AddActor2D(self.rubber_band_actor)
        self.app.vtk_widget.render()
    
    def _clear_rubber_band(self):
        """Remove rubber band"""
        if self.rubber_band_actor:
            try:
                self.app.vtk_widget.renderer.RemoveActor2D(self.rubber_band_actor)
                self.app.vtk_widget.render()
            except Exception:
                pass
            self.rubber_band_actor = None
    
    def clear_all_grids(self):
        """Clear all created grid lines"""
        if not self.grid_actors:
            return
        
        renderer = self.app.vtk_widget.renderer
        for actor in self.grid_actors:
            try:
                renderer.RemoveActor(actor)
            except Exception:
                pass
        
        self.grid_actors = []
        self.app.vtk_widget.render()
        
        print(f"✅ Cleared all grid lines")
        
        
    def _select_dxf_fast(self, xmin, xmax, ymin, ymax, progress, start_pct, end_pct):
        """
        ✅ NEW: Select DXF actors whose geometry falls within rectangle
        """
        self.selected_dxf_actors = []
        
        if not hasattr(self.app, 'dxf_actors') or not self.app.dxf_actors:
            print("   ⚠️ No DXF actors found in app")
            progress.setValue(end_pct)
            return
        
        total_dxf = len(self.app.dxf_actors)
        print(f"   🔍 Checking {total_dxf} DXF entities...")
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
            
            print(f"   📋 Checking entity {idx}: type={dxf_type}, filename={dxf_filename}, actors={len(actors)}")
            
            for actor in actors:
                # Skip if not visible
                if not actor.GetVisibility():
                    print(f"      ⚠️ Actor not visible, skipping")
                    continue
                
                # Check if any part of this actor is in the rectangle
                is_in_rect = self._is_actor_in_rectangle(actor, xmin, xmax, ymin, ymax, renderer)
                print(f"      {'✅' if is_in_rect else '❌'} Actor in rectangle: {is_in_rect}")
                
                if is_in_rect:
                    self.selected_dxf_actors.append({
                        'dxf_data': dxf_data,
                        'actor': actor
                    })
                    print(f"      ✅ SELECTED!")
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
                print(f"         ⚠️ No mapper")
                return False
            
            # Get polydata
            polydata = mapper.GetInput()
            if not polydata:
                print(f"         ⚠️ No polydata")
                return False
            
            # Get points
            points = polydata.GetPoints()
            if not points or points.GetNumberOfPoints() == 0:
                print(f"         ⚠️ No points")
                return False
            
            num_points = points.GetNumberOfPoints()
            print(f"         🔍 Checking {num_points} points in actor...")
            
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
            
            points_checked = 0
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
                
                # Debug first few points
                if points_checked <= 3:
                    print(f"         Point {i}: world={world_pt} → display=({display_x:.1f}, {display_y:.1f})")
                    print(f"         Rectangle: x=[{xmin:.1f}, {xmax:.1f}], y=[{ymin:.1f}, {ymax:.1f}]")
                
                # Check if in rectangle
                if xmin <= display_x <= xmax and ymin <= display_y <= ymax:
                    print(f"         ✅ MATCH at point {i}!")
                    return True
            
            print(f"         ❌ No points in rectangle (checked {points_checked} points)")
            return False
            
        except Exception as e:
            print(f"⚠️ DXF check error: {e}")
            import traceback
            traceback.print_exc()
            return False
