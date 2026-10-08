import sys
print("Python executable:", sys.executable)
try:
    import laspy
    print("laspy version:", laspy.__version__)
except Exception as e:
    print("laspy import error:", e)

try:
    import numpy as np
    print("numpy version:", np.__version__)
except Exception as e:
    print("numpy import error:", e)

try:
    import open3d as o3d
    print("open3d version:", o3d.__version__)
except Exception as e:
    print("open3d import error:", e)

try:
    from PySide6.QtWidgets import QApplication
    print("PySide6 loaded")
except Exception as e:
    print("PySide6 import error:", e)