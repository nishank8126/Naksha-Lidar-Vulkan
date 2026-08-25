import os
import sys
import laspy
import numpy as np
import open3d as o3d
from pyproj import CRS
from PySide6.QtWidgets import QApplication, QFileDialog


def load_lidar_file(filename):
    _, ext = os.path.splitext(filename.lower())
    xyz, rgb, intensity, classification = None, None, None, None
    epsg_code = None

    # --- Try to read CRS from .prj ---
    prj_file = os.path.splitext(filename)[0] + ".prj"
    if os.path.exists(prj_file):
        try:
            with open(prj_file, "r", encoding="utf-8", errors="ignore") as prj_fp:
                crs = CRS.from_wkt(prj_fp.read())
            epsg_code = crs.to_epsg()
            print(f" Loaded EPSG:{epsg_code} from {prj_file}")
        except Exception as e:
            print(f" Failed to parse .prj: {e}")

    # --- LAS / LAZ ---
    if ext in [".las", ".laz"]:
        las = laspy.read(filename)
        xyz = np.vstack((las.x, las.y, las.z)).T

        # intensity
        intensity = getattr(las, "intensity", np.zeros(len(xyz)))

        # classification
        classification = getattr(las, "classification", np.zeros(len(xyz), dtype=np.uint8))

        # rgb
        if hasattr(las, "red"):
            rgb = np.vstack((las.red, las.green, las.blue)).T.astype(np.float64)
            if rgb.size > 0 and np.nanmax(rgb) > 255:
                rgb /= 65535.0
            else:
                rgb /= 255.0
        else:
            rgb = np.tile([0.8, 0.8, 0.8], (len(xyz), 1))  # gray fallback

    # --- PLY ---
    elif ext == ".ply":
        pcd = o3d.io.read_point_cloud(filename)
        xyz = np.asarray(pcd.points)
        if len(pcd.colors) > 0:
            rgb = np.asarray(pcd.colors)
        else:
            rgb = np.tile([0.8, 0.8, 0.8], (len(xyz), 1))
        intensity = np.zeros(len(xyz))
        classification = np.zeros(len(xyz), dtype=np.uint8)

    else:
        raise ValueError("Unsupported format: " + ext)

    return {
        "xyz": xyz,
        "rgb": rgb,
        "intensity": intensity,
        "classification": classification,
        "epsg": epsg_code
    }


if __name__ == "__main__":
    app = QApplication(sys.argv)

    # File picker
    filename, _ = QFileDialog.getOpenFileName(
        None, "Select LiDAR file", "", "LiDAR Files (*.las *.laz *.ply)"
    )

    if not filename:
        print(" No file selected.")
        sys.exit()

    print(f" Loading: {filename}")
    data = load_lidar_file(filename)
    print(" Loaded", data["xyz"].shape[0], "points")

    # Build Open3D point cloud
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(data["xyz"])
    pcd.colors = o3d.utility.Vector3dVector(data["rgb"])

    # Show in Open3D window
    o3d.visualization.draw_geometries([pcd],
        window_name=os.path.basename(filename),
        width=1200, height=800,
        left=50, top=50,
        point_show_normal=False)
