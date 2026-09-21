# gui/curve_settings_dialog.py
# Curve Tool Settings — style persistence for curves
 
from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor
 
DEFAULT_CURVE_STYLE = {'color': (0.0, 1.0, 0.0), 'width': 2}
 
def vtk_color_to_qcolor(vtk_color):
     """Convert VTK float (0-1) color tuple to QColor."""
     return QColor(
         int(vtk_color[0] * 255),
         int(vtk_color[1] * 255),
         int(vtk_color[2] * 255),
     )
 
def qcolor_to_vtk(qcolor):
     """Convert QColor to VTK float (0-1) tuple."""
     return (qcolor.redF(), qcolor.greenF(), qcolor.blueF())
 
def load_curve_settings():
     """Load persisted curve tool style from QSettings, falling back to defaults."""
     settings = QSettings("NakshaAI", "LidarApp")
     default = DEFAULT_CURVE_STYLE
     color_name = settings.value("curve_style/color", None)
     if color_name:
         qc = QColor(color_name)
         color = (qc.redF(), qc.greenF(), qc.blueF())
     else:
         color = default['color']
     
     width = int(settings.value("curve_style/width", default['width']))
     return {'color': color, 'width': width}
 
def save_curve_settings(style):
     """Persist curve tool style to QSettings."""
     settings = QSettings("NakshaAI", "LidarApp")
     qc = vtk_color_to_qcolor(style['color'])
     settings.setValue("curve_style/color", qc.name())
     settings.setValue("curve_style/width", style['width'])