============================================================
PATCHES README - Point Cloud File Loading Fixes
============================================================
Date: 2026-07-01
Issue: Clicking different spots in same block gives different results
       (some positions load file, others open file explorer)
============================================================

FILES MODIFIED:
  1. grid_label_system.py  (7 patches)
  2. snt_attachment.py     (1 patch)
  3. app_window.py         (2 patches)

============================================================
PATCH LIST (apply in order):
============================================================

PATCH 01: grid_label_system.py - _find_matching_las_file()
  - Strip .laz/.las extension from grid_name before scoring
  - Add reverse match scoring (+50 pts)
  - Add debug logging showing available files

PATCH 02: grid_label_system.py - load_grid_las()
  - Accept alt_names parameter
  - Try ALL candidates (primary + alts)
  - Best-score fallback scan when all candidates fail

PATCH 03: grid_label_system.py - _find_las_folder_near_path()
  - Search ALL immediate subfolders (not just hardcoded names)

PATCH 04: grid_label_system.py - _find_las_folder_from_dxf()
  - Search ALL immediate subfolders (not just hardcoded names)

PATCH 05: grid_label_system.py - Block click handler (CASE A.5)
  - Collect ALL overlapping polygons' names
  - Merge alt_names from all polygons
  - File existence check before direct load
  - Pass merged alts to load_grid_las

PATCH 06: grid_label_system.py - _get_alt_names_for_grid()
  - New helper method to look up alt_names for a grid

PATCH 07: grid_label_system.py - Grid label menu (CASE A)
  - Pass alt_names when loading from grid label context menu

PATCH 08: grid_label_system.py - show_grid_label_menu()
  - Pass alt_names when loading from external menu

PATCH 09: snt_attachment.py - build_snt_block_polygons()
  - Store ALL text labels inside each polygon as alt_names
  - Collect both filename_text and other_text

PATCH 10: app_window.py - _candidate_score()
  - Strip .laz/.las extension before scoring
  - Add reverse match scoring
  - Add debug logging

PATCH 11: app_window.py - DXF folder search
  - Search ALL immediate subfolders

PATCH 12: grid_label_system.py - Closest polygon selection
  - Use insideness (min edge distance) to select polygon
  - Collect alt_names from ALL overlapping polygons

PATCH 13: grid_label_system.py - Nearest boundary selection
  - Among overlapping polygons, pick the one whose edge is
    CLOSEST to the click point (smallest min_edge_dist)
  - At corners, click is nearer to correct block's boundary
  - Works for all SNT types (rectangular, irregular, etc.)

============================================================
ROOT CAUSES FIXED:
============================================================

1. OVERLAPPING BL POLYGONS:
   Multiple BL polygons can overlap at the same click point.
   Before: Only first polygon's grid_name was used.
   After:  ALL polygons' names collected and tried.

2. GRID NAME vs FILE NAME MISMATCH:
   SNT text labels (e.g., "DU3035596_0040.laz") don't match
   actual file names (e.g., "ACQUEDOTTO000004.laz").
   Before: Only primary grid_name tried.
   After:  ALL text labels from ALL overlapping polygons tried.

3. LIMITED FOLDER SEARCH:
   Only 6 hardcoded subfolder names checked.
   After: ALL immediate subfolders searched.

4. EXTENSION IN GRID NAME:
   Grid names ending in .laz/.las didn't match file stems.
   After: Extension stripped before comparison.

============================================================
HOW TO APPLY:
============================================================
Each patch shows OLD BLOCK and NEW BLOCK.
Search for the OLD BLOCK in the file and replace with NEW BLOCK.

Order matters - apply patches 01-11 in sequence.
