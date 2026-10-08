import sys
import time
import os

sys.path.insert(0, 'H:\naksha-lidar 2')

import laspy
import numpy as np
from gui.data_loader import load_lidar_file

# Find a LAS/LAZ file
data_dir = r'H:\naksha-lidar 2'
laz_files = []
for root, dirs, files in os.walk(data_dir):
    for f in files:
        if f.lower().endswith(('.laz', '.las')):
            laz_files.append(os.path.join(root, f))

print("Found {} LAS/LAZ files".format(len(laz_files)))
if laz_files:
    test_file = laz_files[0]
    print("Testing with: {}".format(test_file))
    print("File size: {} bytes".format(os.path.getsize(test_file)))
    
    t0 = time.perf_counter()
    result = load_lidar_file(test_file, prompt_user=False, disabled_attrs=None)
    t1 = time.perf_counter()
    
    if result:
        print("LOADING COMPLETE in {:.3f}s".format(t1-t0))
        print("Points: {:,}".format(len(result['xyz'])))
        print("XYZ dtype: {}".format(result['xyz'].dtype))
        rgb_present = result['rgb'] is not None
        print("RGB: present={}, dtype={}".format(rgb_present, result['rgb'].dtype if rgb_present else None))
        intensity_present = result['intensity'] is not None
        print("Intensity: present={}, dtype={}".format(intensity_present, result['intensity'].dtype if intensity_present else None))
        class_present = result['classification'] is not None
        print("Classification: present={}, dtype={}".format(class_present, result['classification'].dtype if class_present else None))
        print("CRS WKT: {}".format(result.get('crs_wkt', 'N/A')))
        print("CRS EPSG: {}".format(result.get('crs_epsg', 'N/A')))
        print("Point format: {}".format(result.get('input_point_format', 'N/A')))
        print("Format version: {}".format(result.get('input_format_version', 'N/A')))
    else:
        print("LOADING returned None (user cancelled or error)")
else:
    print("No LAS/LAZ files found")