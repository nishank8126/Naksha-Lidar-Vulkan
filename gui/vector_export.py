# ============================================================================
# FILE: vector_export.py
# Complete vector drawing export/import system for NakshaAI-Lidar
# Supports DXF, GeoJSON, and Shapefile formats with full metadata
# ============================================================================

import os
import json
import shutil
import tempfile
import numpy as np
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from PySide6.QtWidgets import QMessageBox, QFileDialog


def diagnose_app_data(app):
    """Debug helper to find where data is stored in the app"""
    print(f"\n{'='*60}")
    print(f"🔍 DIAGNOSING APP DATA STRUCTURE")
    print(f"{'='*60}")
    
    # Check all attributes
    print(f"\n📋 App attributes:")
    for attr in dir(app):
        if not attr.startswith('_'):
            try:
                val = getattr(app, attr)
                if hasattr(val, '__len__') and not isinstance(val, str):
                    print(f"   • {attr}: {type(val).__name__} (length: {len(val)})")
                elif not callable(val):
                    print(f"   • {attr}: {type(val).__name__}")
            except Exception:
                pass
    
    # Check for point cloud specifically
    print(f"\n☁️ Point Cloud Search:")
    for attr in ['point_cloud', 'points', 'cloud', 'pc', 'vtk_points', 'actor']:
        if hasattr(app, attr):
            val = getattr(app, attr)
            print(f"   ✅ Found app.{attr}: {type(val)}")
            if hasattr(val, 'GetNumberOfPoints'):
                print(f"      → VTK object with {val.GetNumberOfPoints()} points")
            elif hasattr(val, '__len__'):
                print(f"      → Length: {len(val)}")
    
    # Check for drawings specifically
    print(f"\n✏️ Drawings Search:")
    if hasattr(app, 'digitizer'):
        print(f"   ✅ app.digitizer exists: {type(app.digitizer)}")
        if hasattr(app.digitizer, 'drawings'):
            print(f"   ✅ app.digitizer.drawings exists: {type(app.digitizer.drawings)}")
            print(f"      → Length: {len(app.digitizer.drawings) if app.digitizer.drawings else 0}")
            if app.digitizer.drawings:
                print(f"      → First drawing: {app.digitizer.drawings[0]}")
        
        # Check other possible attributes
        for attr in dir(app.digitizer):
            if 'draw' in attr.lower() and not attr.startswith('_'):
                val = getattr(app.digitizer, attr, None)
                if val is not None and not callable(val):
                    print(f"   • digitizer.{attr}: {type(val)}")
    else:
        print(f"   ❌ app.digitizer does not exist")
    
    print(f"{'='*60}\n")

# ============================================================================
# EXPORT FUNCTIONS
# ============================================================================

def export_drawings_to_shapefile(app, output_path: str) -> bool:
    """
    Export drawings to Shapefile format (ArcGIS/QGIS compatible).
    Creates separate shapefiles for points, lines, and polygons.
    """
    try:
        import geopandas as gpd
        from shapely.geometry import Point, LineString, Polygon
        import pandas as pd
        
        print(f"\n{'='*60}")
        print(f"📤 EXPORTING DRAWINGS TO SHAPEFILE")
        print(f"   Path: {output_path}")
        
        # Validate digitizer
        if not hasattr(app, 'digitizer') or not app.digitizer:
            print("   ⚠️ No digitizer found")
            return False
        
        drawings = app.digitizer.drawings
        if not drawings:
            print("   ⚠️ No drawings to export")
            return False
        
        print(f"   📊 Processing {len(drawings)} drawing(s)...")
        
        # ============================================================
        # DIAGNOSTIC: Print full drawing data
        # ============================================================
        print(f"\n   🔍 FULL DRAWING DATA:")
        for idx, drawing in enumerate(drawings):
            print(f"\n   Drawing {idx+1}:")
            for key, value in drawing.items():
                if key == 'coordinates':
                    print(f"      {key}: {len(value) if isinstance(value, list) else 'N/A'} items")
                    if isinstance(value, list) and len(value) > 0:
                        print(f"         First coord: {value[0]}")
                        if len(value) > 1:
                            print(f"         Last coord: {value[-1]}")
                else:
                    print(f"      {key}: {value}")
        
        # Separate by geometry type
        points = []
        lines = []
        polygons = []


        for idx, drawing in enumerate(drawings):
            shape_type = drawing.get('type', 'unknown')
            coords = drawing.get('coordinates', [])
            
            print(f"\n   🔍 Processing Drawing {idx+1}:")
            print(f"      Type: {shape_type}")
            print(f"      Coords: {coords}")
            print(f"      Coords length: {len(coords)}")
            
            # Build properties dictionary
            props = {
                'type': shape_type,
                'color_r': int(drawing.get('color', (255, 255, 255))[0]),
                'color_g': int(drawing.get('color', (255, 255, 255))[1]),
                'color_b': int(drawing.get('color', (255, 255, 255))[2]),
                'text': str(drawing.get('text', '')),
                'radius': float(drawing.get('radius', 0))
            }
            
            try:
                # ✅ Check for alternative coordinate keys
                coords_empty = False
                try:
                    coords_empty = (coords is None or 
                                (isinstance(coords, list) and len(coords) == 0) or
                                (hasattr(coords, '__len__') and len(coords) == 0))
                except Exception:
                    coords_empty = True
                
                if coords_empty:
                    print(f"      ⚠️ Empty 'coordinates', checking alternatives...")
                    
                    # Check for 'coords' key (your system uses this)
                    for alt_key in ['coords', 'points', 'vertices', 'corners']:
                        if alt_key in drawing:
                            coords = drawing[alt_key]
                            # Convert numpy array to list if needed
                            if hasattr(coords, 'tolist'):
                                coords = coords.tolist() if hasattr(coords, 'tolist') else list(coords)
                            print(f"      ✅ Found coords in '{alt_key}': {len(coords)} points")
                            break
                
                # ✅ Ensure coords is a Python list (convert numpy arrays)
                if hasattr(coords, 'tolist'):
                    coords = coords.tolist() if callable(coords.tolist) else list(coords)
                elif not isinstance(coords, list):
                    try:
                        coords = list(coords)
                    except Exception:
                        coords = []
                
                # ✅ Convert each coordinate from numpy array to list/tuple
                clean_coords = []
                for c in coords:
                    if hasattr(c, 'tolist'):
                        # Numpy array
                        clean_coords.append(tuple(float(x) for x in c.tolist()))
                    elif isinstance(c, (list, tuple)):
                        # Already a list/tuple
                        clean_coords.append(tuple(float(x) for x in c[:2]))  # Use only X, Y
                    else:
                        print(f"      ⚠️ Skipping invalid coordinate: {c}")
                        continue
                
                coords = clean_coords
                
                if not coords or len(coords) < 2:
                    print(f"      ❌ No valid coordinates after conversion")
                    continue
                
                print(f"      ✅ Converted {len(coords)} coordinates")
                
                # ✅ GEOMETRY CREATION (with type mapping)
                
                # Map custom types to standard types
                if shape_type in ['smartline', 'centerline', 'line_segment', 'freehand']:
                    # All line-based drawings → LineString
                    if len(coords) >= 2:
                        line_coords = [c[:2] for c in coords]
                        geom = LineString(line_coords)
                        lines.append({'geometry': geom, **props})
                        print(f"      ✅ Added as LineString ({shape_type} → line)")
                
                elif shape_type == 'polyline':
                    # Polyline → Polygon (closed shape)
                    if len(coords) >= 3:
                        poly_coords = [(c[0], c[1]) for c in coords]
                        # Ensure closed
                        if poly_coords[0] != poly_coords[-1]:
                            poly_coords.append(poly_coords[0])
                        geom = Polygon(poly_coords)
                        if geom.is_valid:
                            polygons.append({'geometry': geom, **props})
                            print(f"      ✅ Added as Polygon (closed polyline)")
                        else:
                            geom = geom.buffer(0)
                            if geom.is_valid:
                                polygons.append({'geometry': geom, **props})
                                print(f"      ✅ Fixed and added polygon")
                
                elif shape_type == 'polygon':
                    if len(coords) >= 3:
                        poly_coords = [(c[0], c[1]) for c in coords]
                        geom = Polygon(poly_coords)
                        if geom.is_valid:
                            polygons.append({'geometry': geom, **props})
                            print(f"      ✅ Added as Polygon")
                        else:
                            geom = geom.buffer(0)
                            if geom.is_valid:
                                polygons.append({'geometry': geom, **props})
                
                elif shape_type == 'rectangle':
                    if len(coords) == 5:
                        # 5-point closed format
                        rect_coords = [(c[0], c[1]) for c in coords[:4]]
                        geom = Polygon(rect_coords)
                        if geom.is_valid:
                            polygons.append({'geometry': geom, **props})
                            print(f"      ✅ Added as Polygon (rectangle)")
                    elif len(coords) >= 2:
                        # 2-corner format
                        p1, p2 = coords[0], coords[1]
                        rect_coords = [
                            (p1[0], p1[1]),
                            (p2[0], p1[1]),
                            (p2[0], p2[1]),
                            (p1[0], p2[1])
                        ]
                        geom = Polygon(rect_coords)
                        if geom.is_valid:
                            polygons.append({'geometry': geom, **props})
                            print(f"      ✅ Added as Polygon (rectangle from 2 corners)")
                
                elif shape_type in ['circle', 'text']:
                    # Points
                    if len(coords) >= 1:
                        coord = coords[0]
                        geom = Point(coord[0], coord[1])
                        points.append({'geometry': geom, **props})
                        print(f"      ✅ Added as Point ({shape_type})")
                
                else:
                    print(f"      ⚠️ Unsupported type: {shape_type}")
                    
            except Exception as e:
                print(f"      ❌ Failed to convert {shape_type}: {e}")
                import traceback
                traceback.print_exc()
                continue
                
                # Ensure coords is a list (convert from numpy if needed)
                if hasattr(coords, 'tolist'):
                    coords = coords.tolist() if callable(coords.tolist) else list(coords)
                elif not isinstance(coords, list):
                    coords = list(coords)
                
                # ============================================================
                # POINT GEOMETRIES
                # ============================================================
                if shape_type in ['circle', 'text'] and len(coords) >= 1:
                    coord = coords[0]
                    if isinstance(coord, (list, tuple)) and len(coord) >= 2:
                        geom = Point(coord[:2])
                        points.append({'geometry': geom, **props})
                        print(f"      ✅ Added as Point")
                    else:
                        print(f"      ⚠️ Invalid point coordinate: {coord}")
                
                # ============================================================
                # LINE GEOMETRIES
                # ============================================================
                elif shape_type == 'line' and len(coords) >= 2:
                    line_coords = []
                    for c in coords:
                        if isinstance(c, (list, tuple)) and len(c) >= 2:
                            line_coords.append(c[:2])
                    
                    if len(line_coords) >= 2:
                        geom = LineString(line_coords)
                        lines.append({'geometry': geom, **props})
                        print(f"      ✅ Added as LineString with {len(line_coords)} points")
                    else:
                        print(f"      ⚠️ Not enough valid coordinates for line")
                
                elif shape_type in ['polyline', 'freehand'] and len(coords) >= 2:
                    line_coords = []
                    for c in coords:
                        if isinstance(c, (list, tuple)) and len(c) >= 2:
                            line_coords.append(c[:2])
                    
                    if len(line_coords) >= 2:
                        geom = LineString(line_coords)
                        lines.append({'geometry': geom, **props})
                        print(f"      ✅ Added as LineString with {len(line_coords)} points")
                    else:
                        print(f"      ⚠️ Not enough valid coordinates for polyline")
                
                # ============================================================
                # POLYGON GEOMETRIES
                # ============================================================
                elif shape_type == 'polygon' and len(coords) >= 3:
                    poly_coords = []
                    for c in coords:
                        if isinstance(c, (list, tuple)) and len(c) >= 2:
                            poly_coords.append((c[0], c[1]))
                    
                    if len(poly_coords) >= 3:
                        geom = Polygon(poly_coords)
                        if geom.is_valid:
                            polygons.append({'geometry': geom, **props})
                            print(f"      ✅ Added as Polygon with {len(poly_coords)} vertices")
                        else:
                            print(f"      ⚠️ Invalid polygon geometry")
                            # Try to fix invalid polygon
                            geom = geom.buffer(0)
                            if geom.is_valid:
                                polygons.append({'geometry': geom, **props})
                                print(f"      ✅ Fixed and added polygon")
                    else:
                        print(f"      ⚠️ Not enough valid points for polygon (need 3+, got {len(poly_coords)})")
                
                elif shape_type == 'rectangle' and len(coords) >= 2:
                    # HANDLE BOTH FORMATS:
                    # Format 1: 2 corner points [p1, p2]
                    # Format 2: 5 points forming closed rectangle [p1, p2, p3, p4, p1]
                    
                    if len(coords) == 5:
                        # Closed rectangle polygon - use first 4 points
                        print(f"      🔍 Rectangle has 5 points (closed polygon), using first 4")
                        rect_coords = []
                        for i in range(4):
                            c = coords[i]
                            # Handle numpy float64 or regular tuples
                            if isinstance(c, (list, tuple)) and len(c) >= 2:
                                # Convert numpy types to regular Python floats
                                x = float(c[0])
                                y = float(c[1])
                                rect_coords.append((x, y))
                            else:
                                print(f"      ⚠️ Invalid coordinate at index {i}: {c}")
                                break
                        
                        if len(rect_coords) == 4:
                            geom = Polygon(rect_coords)
                            if geom.is_valid:
                                polygons.append({'geometry': geom, **props})
                                print(f"      ✅ Added as Polygon (rectangle from 5 points)")
                                print(f"         Corners: {rect_coords}")
                            else:
                                print(f"      ⚠️ Invalid rectangle geometry, attempting to fix...")
                                geom = geom.buffer(0)
                                if geom.is_valid:
                                    polygons.append({'geometry': geom, **props})
                                    print(f"      ✅ Fixed and added polygon")
                        else:
                            print(f"      ⚠️ Could not extract 4 valid corners")
                    
                    else:
                        # Standard 2-corner format
                        p1 = coords[0]
                        p2 = coords[1]
                        
                        # Validate coordinates
                        if not (isinstance(p1, (list, tuple)) and len(p1) >= 2):
                            print(f"      ⚠️ Invalid first corner: {p1}")
                            continue
                        if not (isinstance(p2, (list, tuple)) and len(p2) >= 2):
                            print(f"      ⚠️ Invalid second corner: {p2}")
                            continue
                        
                        # Convert numpy types to regular Python floats
                        x1, y1 = float(p1[0]), float(p1[1])
                        x2, y2 = float(p2[0]), float(p2[1])
                        
                        # Build rectangle from 2 corners
                        rect_coords = [
                            (x1, y1),
                            (x2, y1),
                            (x2, y2),
                            (x1, y2)
                        ]
                        
                        geom = Polygon(rect_coords)
                        if geom.is_valid:
                            polygons.append({'geometry': geom, **props})
                            print(f"      ✅ Added as Polygon (rectangle from 2 corners)")
                            print(f"         Corners: {rect_coords}")
                        else:
                            print(f"      ⚠️ Invalid rectangle geometry")
                
                else:
                    print(f"      ⚠️ Unsupported type '{shape_type}' or insufficient coords (have {len(coords)})")
                    
            except Exception as e:
                print(f"      ❌ Failed to convert {shape_type}: {e}")
                import traceback
                traceback.print_exc()
                continue
        
        # ============================================================
        # SUMMARY
        # ============================================================
        print(f"\n   📊 Geometry Summary:")
        print(f"      Points: {len(points)}")
        print(f"      Lines: {len(lines)}")
        print(f"      Polygons: {len(polygons)}")
        
        if not (points or lines or polygons):
            print(f"\n   ❌ No valid geometries to export")
            print(f"   💡 Tip: Check how drawings are being stored in app.digitizer.drawings")
            return False
        
        # ============================================================
        # SAVE FILES
        # ============================================================
        from pathlib import Path
        base_path = Path(output_path).with_suffix('')
        
        # Get CRS
        crs = f"EPSG:{app.project_crs_epsg}" if hasattr(app, 'project_crs_epsg') and app.project_crs_epsg else "EPSG:4326"
        print(f"\n   🌍 Using CRS: {crs}")
        
        saved_files = []
        
        # SAVE POINTS
        if points:
            try:
                print(f"\n   💾 Saving points...")
                gdf = gpd.GeoDataFrame(points, crs=crs)
                point_path = f"{base_path}_points.shp"
                gdf.to_file(point_path, driver='ESRI Shapefile')
                saved_files.append(point_path)
                print(f"      ✅ Saved {len(points)} points to: {point_path}")
            except Exception as e:
                print(f"      ❌ Failed to save points: {e}")
                import traceback
                traceback.print_exc()
        
        # SAVE LINES
        if lines:
            try:
                print(f"\n   💾 Saving lines...")
                gdf = gpd.GeoDataFrame(lines, crs=crs)
                line_path = f"{base_path}_lines.shp"
                gdf.to_file(line_path, driver='ESRI Shapefile')
                saved_files.append(line_path)
                print(f"      ✅ Saved {len(lines)} lines to: {line_path}")
            except Exception as e:
                print(f"      ❌ Failed to save lines: {e}")
                import traceback
                traceback.print_exc()
        
        # SAVE POLYGONS
        if polygons:
            try:
                print(f"\n   💾 Saving polygons...")
                gdf = gpd.GeoDataFrame(polygons, crs=crs)
                poly_path = f"{base_path}_polygons.shp"
                gdf.to_file(poly_path, driver='ESRI Shapefile')
                saved_files.append(poly_path)
                print(f"      ✅ Saved {len(polygons)} polygons to: {poly_path}")
            except Exception as e:
                print(f"      ❌ Failed to save polygons: {e}")
                import traceback
                traceback.print_exc()
        
        # ============================================================
        # FINAL SUMMARY
        # ============================================================
        if saved_files:
            print(f"\n   📁 Successfully Saved Files:")
            for f in saved_files:
                print(f"      • {f}")
            print(f"{'='*60}\n")
            return True
        else:
            print(f"\n   ❌ No files were saved")
            print(f"{'='*60}\n")
            return False
        
    except ImportError as e:
        print(f"   ❌ Missing required library: {e}")
        print(f"   💡 Install: pip install geopandas shapely")
        return False
    except Exception as e:
        print(f"   ❌ Shapefile export failed: {e}")
        import traceback
        traceback.print_exc()
        return False

# ============================================================================
# ============================================================================


def import_drawings_from_shapefile(app, input_path: str) -> bool:
    """Import drawings from Shapefile with proper rendering"""
    try:
        import geopandas as gpd
        from pathlib import Path
        
        print(f"\n{'='*60}")
        print(f"📥 IMPORTING DRAWINGS FROM SHAPEFILE")
        print(f"   Path: {input_path}")
        
        base_path = Path(input_path).with_suffix('')
        
        # Try to load all geometry types
        imported_count = 0
        
        for suffix in ['_points', '_lines', '_polygons', '']:
            try:
                shp_path = f"{base_path}{suffix}.shp"
                if not Path(shp_path).exists():
                    continue
                
                print(f"   📂 Loading: {shp_path}")
                gdf = gpd.read_file(shp_path)
                print(f"   📊 Found {len(gdf)} features")
                
                for idx, row in gdf.iterrows():
                    geom = row.geometry
                    
                    drawing = {
                        'color': (
                            int(row.get('color_r', 255)),
                            int(row.get('color_g', 255)),
                            int(row.get('color_b', 255))
                        ),
                        'text': str(row.get('text', '')),
                        'radius': float(row.get('radius', 0))
                    }
                    
                    if geom.geom_type == 'Point':
                        drawing['type'] = row.get('type', 'circle')
                        drawing['coordinates'] = [[geom.x, geom.y, 0]]
                    
                    elif geom.geom_type == 'LineString':
                        drawing['type'] = row.get('type', 'polyline')
                        drawing['coordinates'] = [
                            [x, y, 0] for x, y in geom.coords
                        ]
                    
                    elif geom.geom_type == 'Polygon':
                        drawing['type'] = row.get('type', 'polygon')
                        coords = list(geom.exterior.coords)[:-1]
                        drawing['coordinates'] = [
                            [x, y, 0] for x, y in coords
                        ]
                    
                    if hasattr(app, 'digitizer'):
                        # Use proper import method
                        if hasattr(app.digitizer, 'add_imported_drawing'):
                            app.digitizer.add_imported_drawing(drawing)
                        else:
                            app.digitizer.add_drawing_from_data(drawing)
                        imported_count += 1
                        print(f"   ✅ Added {drawing['type']} from import")
                        
            except Exception as e:
                print(f"   ⚠️ Could not load {suffix}: {e}")
                import traceback
                traceback.print_exc()
                continue
        
        # CRITICAL: Force render update
        if imported_count > 0:
            print(f"\n   🎨 Updating display...")
            
            # Try multiple render methods
            if hasattr(app, 'vtk_widget') and app.vtk_widget:
                try:
                    # Throttled/mutex-guarded render, not a raw GetRenderWindow().Render()
                    # (see import_geotiff_as_texture for why the raw call is unsafe here).
                    app.vtk_widget.render()
                    print(f"   ✅ VTK render triggered")
                except Exception:
                    pass
            
            # Try calling update method
            if hasattr(app, 'update_display'):
                try:
                    app.update_display()
                    print(f"   ✅ Display update called")
                except Exception:
                    pass
            
            # Try refresh method
            if hasattr(app, 'refresh'):
                try:
                    app.refresh()
                    print(f"   ✅ Refresh called")
                except Exception:
                    pass
            
            print(f"   ✅ Imported {imported_count} drawings")
        else:
            print(f"   ⚠️ No drawings imported")
        
        print(f"{'='*60}\n")
        
        return imported_count > 0
        
    except ImportError:
        print("   ⚠️ geopandas not installed")
        return False
    except Exception as e:
        print(f"   ❌ Shapefile import failed: {e}")
        import traceback
        traceback.print_exc()
        return False


# Normalized export path used by the File ribbon actions.
def _normalize_drawing_coords(raw_coords) -> List[Tuple[float, float, float]]:
    coords: List[Tuple[float, float, float]] = []
    if raw_coords is None:
        return coords

    if hasattr(raw_coords, "tolist"):
        raw_coords = raw_coords.tolist()

    for coord in raw_coords:
        if hasattr(coord, "tolist"):
            coord = coord.tolist()
        if not isinstance(coord, (list, tuple)) or len(coord) < 2:
            continue

        x = float(coord[0])
        y = float(coord[1])
        z = float(coord[2]) if len(coord) > 2 else 0.0
        coords.append((x, y, z))
    return coords


def _normalize_rgb_triplet(color) -> Tuple[int, int, int]:
    if not isinstance(color, (list, tuple)) or len(color) < 3:
        return (255, 255, 255)

    try:
        values = [float(color[0]), float(color[1]), float(color[2])]
    except (TypeError, ValueError):
        return (255, 255, 255)

    if max(abs(v) for v in values) <= 1.0:
        values = [v * 255.0 for v in values]

    return tuple(max(0, min(255, int(round(v)))) for v in values)


def _extract_drawing_color(drawing: dict) -> Tuple[int, int, int]:
    # Prefer the SBM class RGB which is the authoritative 0-255 color
    # assigned from the .sbm file (same source the DXF exporter uses).
    sbm_class = drawing.get("sbm_class")
    if sbm_class and isinstance(sbm_class, dict):
        sbm_rgb = sbm_class.get("rgb")
        if sbm_rgb and isinstance(sbm_rgb, (list, tuple)) and len(sbm_rgb) >= 3:
            return _normalize_rgb_triplet(sbm_rgb)

    return _normalize_rgb_triplet(
        drawing.get("color")
        or drawing.get("original_color")
        or drawing.get("original_text_color")
        or (255, 255, 255)
    )


def _extract_circle_center_and_radius(
    drawing: dict,
    coords: List[Tuple[float, float, float]],
) -> Tuple[Optional[Tuple[float, float]], float]:
    center = drawing.get("center")
    radius = float(drawing.get("radius", 0.0) or 0.0)

    if isinstance(center, (list, tuple)) and len(center) >= 2:
        center_xy = (float(center[0]), float(center[1]))
    elif coords:
        ring = coords[:-1] if len(coords) > 1 and coords[0][:2] == coords[-1][:2] else coords
        center_xy = (
            float(sum(pt[0] for pt in ring) / len(ring)),
            float(sum(pt[1] for pt in ring) / len(ring)),
        )
    else:
        center_xy = None

    if center_xy is not None and radius <= 0.0 and coords:
        ring = coords[:-1] if len(coords) > 1 and coords[0][:2] == coords[-1][:2] else coords
        radius = max(
            (
                float(np.hypot(pt[0] - center_xy[0], pt[1] - center_xy[1]))
                for pt in ring
            ),
            default=0.0,
        )

    return center_xy, radius


def _build_polygon_geometry(coords, polygon_cls):
    # ── FIX: preserve Z if coords carry it ─────────────────────────────────
    has_z = bool(coords) and len(coords[0]) >= 3
    ring = (
        [(pt[0], pt[1], pt[2]) for pt in coords]
        if has_z
        else [(pt[0], pt[1]) for pt in coords]
    )
    if len(ring) < 3:
        return None
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    if len(ring) < 4:
        return None

    geom = polygon_cls(ring)
    if geom.is_empty:
        return None

    if not geom.is_valid:
        geom = geom.buffer(0)
        if geom.is_empty:
            return None

    if geom.geom_type == "MultiPolygon":
        geom = max(geom.geoms, key=lambda part: part.area, default=None)

    if geom is None or geom.geom_type != "Polygon" or not geom.is_valid:
        return None
    return geom


def _drawing_to_shapefile_feature(drawing: dict):
    """
    Convert ONE digitize drawing dict into a (bucket, feature) pair ready
    for GeoDataFrame insertion.
 
    bucket  : "points" | "lines" | "polygons" | None
    feature : dict with 'geometry' + property columns, or None on failure
 
    Geometry mapping (matches how digitize_tools.py stores shapes):
      text        → Point   (anchor position)
      circle      → Polygon (the stored circle outline in coords)
      rectangle   → Polygon (the 5-point closed outline in coords)
      polygon     → Polygon
      polyline    → Polygon (digitize polyline is ALWAYS a closed loop)
      freehand    → Polygon when closed, LineString otherwise
      smartline   → LineString
      centerline  → LineString
      line        → LineString
      line_segment→ LineString
    """
    from shapely.geometry import LineString, Point, Polygon
 
    shape_type = str(drawing.get("type", "unknown") or "unknown")
 
    # Read coords from whichever key the drawing uses
    # (digitize tools use "coords"; imported drawings also set "coordinates")
    raw = drawing.get("coordinates") or drawing.get("coords") or []
    coords = _normalize_drawing_coords(raw)
 
    color_r, color_g, color_b = _extract_drawing_color(drawing)
 
    props = {
        "type":    shape_type,
        "color_r": color_r,
        "color_g": color_g,
        "color_b": color_b,
        "text":    str(drawing.get("text", "") or ""),
        "radius":  float(drawing.get("radius", 0.0) or 0.0),
        # Preserve line width and style so a future re-import can restore them
        "lwidth":  int(drawing.get("original_width", 2) or 2),
        "lstyle":  str(drawing.get("original_style", "solid") or "solid"),
    }
 
    # ── TEXT ─────────────────────────────────────────────────────────────────
    if shape_type == "text":
        if not coords:
            return None, None
        x, y, _ = coords[0]
        return "points", {"geometry": Point(x, y), **props}
 
    # ── CIRCLE ───────────────────────────────────────────────────────────────
    # digitize_tools stores the full circle outline (n+1 closed points) in
    # drawing["coords"].  Export that as a Polygon so the shape is preserved
    # exactly.  We also keep the radius attribute for reference.
    if shape_type == "circle":
        if len(coords) >= 3:
            geom = _build_polygon_geometry(coords, Polygon)
            if geom is not None:
                props["radius"] = float(drawing.get("radius", 0.0) or 0.0)
                return "polygons", {"geometry": geom, **props}
        # Fallback: center point only (e.g. imported circle with just 1 coord)
        center_xy, radius = _extract_circle_center_and_radius(drawing, coords)
        if center_xy is None:
            return None, None
        props["radius"] = radius
        return "points", {"geometry": Point(center_xy[0], center_xy[1]), **props}
 
    # ── RECTANGLE ────────────────────────────────────────────────────────────
    # Two-corner format → expand to 5-point closed outline first
    if shape_type == "rectangle" and len(coords) == 2:
        p1, p2 = coords[0], coords[1]
        coords = [
            (p1[0], p1[1], p1[2]),
            (p2[0], p1[1], p1[2]),
            (p2[0], p2[1], p2[2]),
            (p1[0], p2[1], p2[2]),
            (p1[0], p1[1], p1[2]),
        ]
 
    if shape_type in {"polygon", "rectangle"}:
        geom = _build_polygon_geometry(coords, Polygon)
        if geom is None:
            return None, None
        return "polygons", {"geometry": geom, **props}
 
    # ── POLYLINE ─────────────────────────────────────────────────────────────
    # In digitize_tools, the polyline tool ALWAYS produces a closed loop
    # (first point == last point).  Export as Polygon, not LineString, so
    # the enclosed area is preserved and the round-trip import is correct.
    if shape_type == "polyline":
        if len(coords) >= 3:
            geom = _build_polygon_geometry(coords, Polygon)
            if geom is not None:
                return "polygons", {"geometry": geom, **props}
        # Safety fallback for very short or degenerate polylines
        if len(coords) >= 2:
            return "lines", {
                "geometry": LineString([(pt[0], pt[1]) for pt in coords]),
                **props,
            }
        return None, None
 
    # ── FREEHAND ─────────────────────────────────────────────────────────────
    # digitize freehand is auto-closed on finalisation; export as Polygon.
    if shape_type == "freehand":
        if len(coords) >= 3 and coords[0][:2] == coords[-1][:2]:
            geom = _build_polygon_geometry(coords, Polygon)
            if geom is not None:
                return "polygons", {"geometry": geom, **props}
        if len(coords) >= 2:
            return "lines", {
                "geometry": LineString([(pt[0], pt[1]) for pt in coords]),
                **props,
            }
        return None, None
 
    # ── LINE TYPES ────────────────────────────────────────────────────────────
    if shape_type in {"line", "line_segment", "smartline", "centerline"}:
        if len(coords) < 2:
            return None, None
        return "lines", {
            "geometry": LineString([(pt[0], pt[1]) for pt in coords]),
            **props,
        }
 
    # ── GENERIC FALLBACK ─────────────────────────────────────────────────────
    if len(coords) >= 3 and coords[0][:2] == coords[-1][:2]:
        geom = _build_polygon_geometry(coords, Polygon)
        if geom is not None:
            return "polygons", {"geometry": geom, **props}
 
    if len(coords) >= 2:
        return "lines", {
            "geometry": LineString([(pt[0], pt[1]) for pt in coords]),
            **props,
        }
 
    if len(coords) == 1:
        x, y, _ = coords[0]
        return "points", {"geometry": Point(x, y), **props}
 
    return None, None


def _geometry_to_linestring(geom, shape_type: str):
    """
    Convert ANY Shapely geometry to a LineString for single-file SHP export.
    This is the MicroStation "linework" approach — everything is represented as
    line work in the output Shapefile so all shapes go into one file.

    Conversion rules:
      LineString  → returned as-is
      Polygon     → exterior ring as LineString (closing point removed)
      Point       → two-point stub line [(x,y),(x+tiny,y)] so SHP accepts it
    """
    from shapely.geometry import LineString, Polygon, Point

    if geom is None or geom.is_empty:
        return None

    gtype = geom.geom_type

    if gtype == "LineString":
        return geom

    if gtype == "Polygon":
        # Extract exterior ring; drop the repeated closing point so shapely
        # gives us a clean open ring which LineString handles correctly.
        ring = list(geom.exterior.coords)
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring = ring[:-1]
        # Re-close it so the outline looks identical to the drawn polygon.
        ring.append(ring[0])
        if len(ring) < 2:
            return None
        return LineString([(pt[0], pt[1]) for pt in ring])

    if gtype == "Point":
        # Represent a point as an infinitesimally small line stub.
        # The 1e-9 offset is invisible at any real-world scale but satisfies
        # the Shapefile requirement that a LineString has at least 2 different points.
        x, y = geom.x, geom.y
        return LineString([(x, y), (x + 1e-9, y)])

    # MultiGeometry fallback — convert first part
    if hasattr(geom, "geoms"):
        for part in geom.geoms:
            result = _geometry_to_linestring(part, shape_type)
            if result is not None:
                return result

    return None


def export_drawings_to_shapefile(app, output_path: str) -> bool:
    """
    Export ALL drawings to a SINGLE Shapefile (.shp).

    Uses the MicroStation "linework" approach: every geometry type
    (point, line, polyline, polygon, rectangle, circle, text) is converted
    to a LineString so that all features share one common geometry type and
    can be stored in a single .shp file without any splitting.

    Attribute columns preserved per feature:
      type, color_r, color_g, color_b, text, radius, lwidth, lstyle
    """
    try:
        import geopandas as gpd
        from shapely.geometry import LineString

        print(f"\n{'='*60}")
        print("📤 EXPORTING DRAWINGS TO SINGLE SHAPEFILE (Linework mode)")
        print(f"   Output: {output_path}")

        digitizer = getattr(app, "digitizer", None)
        drawings = list(getattr(digitizer, "drawings", []) or [])
        if not drawings:
            print("   ⚠️  No drawings to export")
            return False

        print(f"   📊 Total drawings: {len(drawings)}")

        all_features = []   # Everything goes here — single list, single file
        skipped = 0

        for idx, drawing in enumerate(drawings, start=1):
            shape_type = drawing.get("type", "unknown")
            try:
                # ── Step 1: classify into bucket + get native geometry ──
                bucket, feature = _drawing_to_shapefile_feature(drawing)
                if bucket is None or feature is None:
                    print(f"   ⚠️  Skipping drawing {idx} ({shape_type}): could not build geometry")
                    skipped += 1
                    continue

                native_geom = feature["geometry"]
                props = {k: v for k, v in feature.items() if k != "geometry"}

                # ── Step 2: force to LineString ─────────────────────────
                line_geom = _geometry_to_linestring(native_geom, shape_type)
                if line_geom is None or line_geom.is_empty:
                    print(f"   ⚠️  Skipping drawing {idx} ({shape_type}): linework conversion failed")
                    skipped += 1
                    continue

                all_features.append({"geometry": line_geom, **props})
                print(f"   ✅ Drawing {idx} ({shape_type}) → LineString "
                      f"({len(list(line_geom.coords))} pts)")

            except Exception as e:
                print(f"   ❌ Drawing {idx} ({shape_type}) failed: {e}")
                import traceback
                traceback.print_exc()
                skipped += 1

        if not all_features:
            print("   ❌ No valid geometries to export")
            return False

        # ── Step 3: build GeoDataFrame and save ────────────────────────
        crs = (f"EPSG:{app.project_crs_epsg}"
               if getattr(app, "project_crs_epsg", None) else "EPSG:4326")
        print(f"   🌍 CRS: {crs}")

        # Ensure output path ends in .shp
        shp_path = str(Path(output_path).with_suffix(".shp"))

        gdf = gpd.GeoDataFrame(all_features, crs=crs)
        gdf.to_file(shp_path, driver="ESRI Shapefile")

        print(f"\n   ✅ Saved {len(all_features)} feature(s) to single file:")
        print(f"      {shp_path}")
        if skipped:
            print(f"   ⚠️  {skipped} drawing(s) were skipped")
        print(f"{'='*60}\n")
        return True

    except ImportError as e:
        print(f"   ❌ Missing required library: {e}")
        print("   💡 Install: pip install geopandas shapely")
        return False
    except Exception as e:
        print(f"   ❌ Shapefile export failed: {e}")
        import traceback
        traceback.print_exc()
        return False


# ============================================================================
# SNT EXPORT HELPERS
# ============================================================================

def _coerce_xyz_tuple(raw_point: Any) -> Optional[Tuple[float, float, float]]:
    """Best-effort XYZ conversion from arbitrary coordinate-like values."""
    try:
        if raw_point is None:
            return None
        if isinstance(raw_point, np.ndarray):
            vals = raw_point.tolist()
        else:
            vals = list(raw_point)
        if len(vals) < 2:
            return None
        x = float(vals[0])
        y = float(vals[1])
        z = float(vals[2]) if len(vals) > 2 else 0.0
        if not (np.isfinite(x) and np.isfinite(y) and np.isfinite(z)):
            return None
        return (x, y, z)
    except Exception:
        return None


def _same_point_2d(a: Tuple[float, float, float], b: Tuple[float, float, float], tol: float = 1e-6) -> bool:
    return abs(float(a[0]) - float(b[0])) <= tol and abs(float(a[1]) - float(b[1])) <= tol


def _normalize_layer_name(raw_layer: Any, fallback: str = "0") -> str:
    name = str(raw_layer or "").replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
    if not name:
        name = fallback
    if len(name) > 255:
        name = name[:255]
    return name


def _actor_is_visible(actor) -> bool:
    try:
        if actor is None:
            return False
        getter = getattr(actor, "GetVisibility", None)
        if callable(getter):
            return bool(getter())
        return True
    except Exception:
        return False


def _drawing_is_visible(drawing: dict) -> bool:
    actor = drawing.get("actor")
    if actor is None:
        return True
    return _actor_is_visible(actor)


def _collect_visible_digitizer_entities_for_snt_export(app) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    digitizer = getattr(app, "digitizer", None)
    drawings = list(getattr(digitizer, "drawings", []) or [])
    if not drawings:
        return out

    closed_shape_types = {"polygon", "rectangle", "circle", "polyline", "freehand"}

    for drawing in drawings:
        if not _drawing_is_visible(drawing):
            continue

        shape_type = str(drawing.get("type", "") or "").strip().lower()
        layer_name = _normalize_layer_name(
            drawing.get("layer")
            or drawing.get("layer_name")
            or f"DIGITIZER_{shape_type or 'vector'}",
            fallback="DIGITIZER",
        )
        color = _extract_drawing_color(drawing)
        coords_raw = drawing.get("coordinates") or drawing.get("coords") or []
        coords = [_coerce_xyz_tuple(pt) for pt in _normalize_drawing_coords(coords_raw)]
        coords = [pt for pt in coords if pt is not None]

        if shape_type == "text":
            if not coords:
                continue
            text_value = str(drawing.get("text", "") or "").strip()
            if not text_value:
                continue
            height = drawing.get("height", drawing.get("text_height", 2.5))
            try:
                height = float(height)
            except Exception:
                height = 2.5
            if height <= 0:
                height = 2.5
            out.append(
                {
                    "entity_type": "TEXT",
                    "layer": layer_name,
                    "color": color,
                    "position": coords[0],
                    "text": text_value,
                    "height": height,
                    "rotation": float(drawing.get("rotation", 0.0) or 0.0),
                }
            )
            continue

        if shape_type == "point":
            if not coords:
                continue
            out.append(
                {
                    "entity_type": "POINT",
                    "layer": layer_name,
                    "color": color,
                    "position": coords[0],
                }
            )
            continue

        if len(coords) < 2:
            continue

        closed = shape_type in closed_shape_types
        if not closed and len(coords) >= 3:
            closed = _same_point_2d(coords[0], coords[-1])

        out.append(
            {
                "entity_type": "POLYLINE",
                "layer": layer_name,
                "color": color,
                "points": coords,
                "closed": bool(closed),
            }
        )

    return out


def _attachment_aliases(attachment: Dict[str, Any]) -> Set[str]:
    aliases: Set[str] = set()
    filename = str(attachment.get("filename", "") or "").strip()
    full_path = str(attachment.get("full_path", "") or "").strip()
    if filename:
        aliases.add(filename.lower())
        aliases.add(os.path.basename(filename).lower())
    if full_path:
        try:
            p = Path(full_path).resolve()
            aliases.add(str(p).lower())
            aliases.add(p.name.lower())
        except Exception:
            aliases.add(full_path.lower())
    return aliases


def _build_snt_item_state_map(app) -> Dict[str, Dict[str, Any]]:
    """
    Build visibility/selection state map from SNT attachment UI items when available.
    Keys include both absolute path and basename aliases.
    """
    out: Dict[str, Dict[str, Any]] = {}
    dlg = getattr(app, "snt_dialog", None)
    items = getattr(dlg, "snt_items", None)
    if not items:
        return out

    for item in items:
        try:
            aliases: Set[str] = set()
            snt_path = getattr(item, "snt_path", None)
            if snt_path is not None:
                p = Path(str(snt_path)).resolve()
                aliases.add(str(p).lower())
                aliases.add(p.name.lower())
            else:
                fallback_name = str(getattr(item, "filename", "") or "").strip()
                if fallback_name:
                    aliases.add(fallback_name.lower())
                    aliases.add(os.path.basename(fallback_name).lower())

            selected_layers = getattr(item, "selected_layers", None)
            if selected_layers is None:
                selected_layers_norm = None
            else:
                selected_layers_norm = {str(x) for x in selected_layers}

            state = {
                "checked": bool(item.is_checked()) if hasattr(item, "is_checked") else True,
                "selected_layers": selected_layers_norm,
            }

            for key in aliases:
                out[key] = state
        except Exception:
            continue
    return out


def _attachment_has_visible_actor(app, attachment: Dict[str, Any]) -> bool:
    aliases = _attachment_aliases(attachment)
    if not aliases:
        return False
    for store_name in ("snt_actors", "dxf_actors"):
        for actor_group in list(getattr(app, store_name, []) or []):
            filename = str(actor_group.get("filename", "") or "")
            full_path = str(actor_group.get("full_path", "") or "")
            group_aliases = {
                filename.lower(),
                os.path.basename(filename).lower(),
                full_path.lower(),
                os.path.basename(full_path).lower(),
            }
            if not (aliases & group_aliases):
                continue
            for actor in list(actor_group.get("actors", []) or []):
                if _actor_is_visible(actor):
                    return True
    return False


def _dedupe_attachment_records(app) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for attr_name in ("snt_attachments", "dxf_attachments"):
        for rec in list(getattr(app, attr_name, []) or []):
            if isinstance(rec, dict):
                merged.append(rec)

    unique: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for rec in merged:
        full_path = str(rec.get("full_path", "") or "").strip()
        if full_path:
            key = f"path:{full_path.lower()}"
        else:
            filename = str(rec.get("filename", "") or "").strip()
            if not filename:
                continue
            key = f"name:{os.path.basename(filename).lower()}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(rec)
    return unique


def _collect_raw_entities_from_attachment(attachment: Dict[str, Any]) -> List[Dict[str, Any]]:
    parsed = attachment.get("parsed") or {}

    # SBM-sourced attachments: parse_sbm_file() returns a list of category
    # dicts [{title, menu_index, subclasses:[{lv, ...}]}], not {entities:[...]}.
    # Detect this and skip to the render-normalized fallback below.
    if not isinstance(parsed, dict):
        parsed = {}

    raw_entities = parsed.get("entities")
    if isinstance(raw_entities, list) and raw_entities:
        return raw_entities

    # Fallback: attachment["entities"] is already render-normalized.
    # NOTE: SBM subclasses store the layer name under "lv", not "layer".
    # Remap it here so DXF export preserves per-layer structure.
    converted: List[Dict[str, Any]] = []
    for ent in list(attachment.get("entities", []) or []):
        etype = str(ent.get("type", "")).lower()
        layer = ent.get("layer") or ent.get("lv") or "0"   # ← key fix
        color = ent.get("color", (255, 255, 255))
        if etype in ("line", "polyline"):
            converted.append(
                {
                    "type": "POLYLINE",
                    "layer": layer,
                    "color": color,
                    "vertices": ent.get("points", []),
                    "closed": bool(ent.get("closed", False)),
                }
            )
        elif etype == "point":
            converted.append(
                {
                    "type": "POINT",
                    "layer": layer,
                    "color": color,
                    "position": ent.get("position", (0.0, 0.0, 0.0)),
                }
            )
        elif etype == "text":
            converted.append(
                {
                    "type": "TEXT",
                    "layer": layer,
                    "color": color,
                    "position": ent.get("position", (0.0, 0.0, 0.0)),
                    "text": ent.get("text", ""),
                    "height": ent.get("height", 2.5),
                    "rotation": ent.get("rotation", 0.0),
                }
            )
        elif etype == "3dface":
            converted.append(
                {
                    "type": "3DFACE",
                    "layer": layer,
                    "color": color,
                    "vertices": ent.get("vertices", []),
                }
            )
    return converted


def _collect_visible_snt_entities_for_export(app) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    attachments = _dedupe_attachment_records(app)
    if not attachments:
        return out

    item_state_map = _build_snt_item_state_map(app)

    for attachment in attachments:
        aliases = _attachment_aliases(attachment)
        state = None
        for a in aliases:
            if a in item_state_map:
                state = item_state_map[a]
                break

        if state is not None:
            is_checked = bool(state.get("checked", True))
            selected_layers = state.get("selected_layers", attachment.get("selected_layers"))
        else:
            is_checked = _attachment_has_visible_actor(app, attachment)
            selected_layers = attachment.get("selected_layers")

        if not is_checked:
            continue

        if selected_layers is None:
            selected_set = None
        else:
            selected_set = {str(x) for x in selected_layers}

        raw_entities = _collect_raw_entities_from_attachment(attachment)
        if not raw_entities:
            continue

        for ent in raw_entities:
            etype = str(ent.get("type", "") or "").upper().strip()
            layer = _normalize_layer_name(ent.get("layer", "0"), fallback="0")

            if selected_set is not None and layer not in selected_set:
                continue

            color = _normalize_rgb_triplet(ent.get("color", (255, 255, 255)))

            if etype == "POLYLINE":
                verts = [_coerce_xyz_tuple(pt) for pt in list(ent.get("vertices", []) or [])]
                verts = [v for v in verts if v is not None]
                if len(verts) < 2:
                    continue
                out.append(
                    {
                        "entity_type": "POLYLINE",
                        "layer": layer,
                        "color": color,
                        "points": verts,
                        "closed": bool(ent.get("closed", False)),
                    }
                )
            elif etype == "POINT":
                pos = _coerce_xyz_tuple(ent.get("position", (0.0, 0.0, 0.0)))
                if pos is None:
                    continue
                out.append(
                    {
                        "entity_type": "POINT",
                        "layer": layer,
                        "color": color,
                        "position": pos,
                    }
                )
            elif etype == "TEXT":
                pos = _coerce_xyz_tuple(ent.get("position", (0.0, 0.0, 0.0)))
                if pos is None:
                    continue
                text_value = str(ent.get("text", "") or "").strip()
                if not text_value:
                    continue
                try:
                    height = float(ent.get("height", 2.5) or 2.5)
                except Exception:
                    height = 2.5
                if height <= 0:
                    height = 2.5
                out.append(
                    {
                        "entity_type": "TEXT",
                        "layer": layer,
                        "color": color,
                        "position": pos,
                        "text": text_value,
                        "height": height,
                        "rotation": float(ent.get("rotation", 0.0) or 0.0),
                    }
                )
            elif etype == "3DFACE":
                verts = [_coerce_xyz_tuple(pt) for pt in list(ent.get("vertices", []) or [])]
                verts = [v for v in verts if v is not None]
                if len(verts) < 3:
                    continue
                if len(verts) > 4:
                    verts = verts[:4]
                out.append(
                    {
                        "entity_type": "3DFACE",
                        "layer": layer,
                        "color": color,
                        "vertices": verts,
                    }
                )

    return out


def _rgb_to_true_color_int(color: Tuple[int, int, int]) -> int:
    r, g, b = _normalize_rgb_triplet(color)
    return (int(r) << 16) | (int(g) << 8) | int(b)


def _ensure_ezdxf_layer(doc, layer_name: str) -> None:
    try:
        doc.layers.get(layer_name)
    except Exception:
        try:
            doc.layers.add(layer_name)
        except Exception:
            pass


def _entity_dxf_attribs(layer_name: str, color_rgb: Tuple[int, int, int]) -> Dict[str, Any]:
    attribs: Dict[str, Any] = {"layer": layer_name}
    try:
        attribs["true_color"] = _rgb_to_true_color_int(color_rgb)
        # Set ACI color to a non-BYLAYER value (7=white) so snt_core.convert
        # does not fall back to layer color.  The true_color (group 420) carries
        # the exact RGB; ACI is just a hint for readers that ignore true_color.
        attribs["color"] = _nearest_aci(color_rgb)
    except Exception:
        pass
    return attribs


def _nearest_aci(rgb: Tuple[int, int, int]) -> int:
    """Find the nearest ACI color index for an RGB triplet.
    
    Falls back to 7 (white) if no close match is found.
    Used as a hint for converters that prefer ACI over true_color.
    """
    _ACI_TABLE = {
          1: (255,   0,   0),    2: (255, 255,   0),    3: (  0, 255,   0),
          4: (  0, 255, 255),    5: (  0,   0, 255),    6: (255,   0, 255),
          7: (255, 255, 255),    8: (128, 128, 128),    9: (192, 192, 192),
         10: (255,   0,   0),   20: (255, 127,   0),   30: (255, 191,   0),
         40: (255, 255,   0),   50: (127, 255,   0),   60: (  0, 255,   0),
         70: (  0, 255, 127),   80: (  0, 255, 255),   90: (  0, 127, 255),
        100: (  0,   0, 255),  110: (127,   0, 255),  120: (255,   0, 255),
        130: (255,   0, 127),  140: (255, 127, 127),  150: (255, 200, 127),
        160: (255, 255, 127),  170: (200, 255, 127),  180: (127, 255, 127),
        190: (127, 255, 200),  200: (127, 255, 255),  210: (127, 200, 255),
        220: (127, 127, 255),  230: (200, 127, 255),  240: (255, 127, 255),
    }
    r, g, b = rgb
    best_aci = 7
    best_dist = float('inf')
    for aci, (ar, ag, ab) in _ACI_TABLE.items():
        dist = (r - ar) ** 2 + (g - ag) ** 2 + (b - ab) ** 2
        if dist < best_dist:
            best_dist = dist
            best_aci = aci
            if dist == 0:
                break
    return best_aci


def _emit_entity_to_dxf(msp, doc, entity: Dict[str, Any]) -> bool:
    etype = str(entity.get("entity_type", "") or "").upper()
    layer = _normalize_layer_name(entity.get("layer", "0"), fallback="0")
    color = _normalize_rgb_triplet(entity.get("color", (255, 255, 255)))
    _ensure_ezdxf_layer(doc, layer)
    attribs = _entity_dxf_attribs(layer, color)

    if etype == "POINT":
        pos = entity.get("position")
        if pos is None:
            return False
        msp.add_point(tuple(pos), dxfattribs=attribs)
        return True

    if etype == "TEXT":
        pos = entity.get("position")
        txt = str(entity.get("text", "") or "")
        if pos is None or not txt:
            return False
        try:
            height = float(entity.get("height", 2.5) or 2.5)
        except Exception:
            height = 2.5
        if height <= 0:
            height = 2.5
        try:
            rotation = float(entity.get("rotation", 0.0) or 0.0)
        except Exception:
            rotation = 0.0
        txt_ent = msp.add_text(
            txt,
            dxfattribs={
                **attribs,
                "height": height,
                "rotation": rotation,
            },
        )
        txt_ent.dxf.insert = tuple(pos)
        return True

    if etype == "3DFACE":
        verts = [tuple(v) for v in list(entity.get("vertices", []) or [])]
        if len(verts) < 3:
            return False
        while len(verts) < 4:
            verts.append(verts[-1])
        msp.add_3dface(verts[:4], dxfattribs=attribs)
        return True

    if etype == "POLYLINE":
        points = [tuple(p) for p in list(entity.get("points", []) or [])]
        if len(points) < 2:
            return False
        closed = bool(entity.get("closed", False))
        if closed and len(points) >= 3 and _same_point_2d(points[0], points[-1]):
            points = points[:-1]
        if len(points) < 2:
            return False

        has_z = any(abs(float(p[2])) > 1e-9 for p in points)
        if len(points) == 2 and not closed:
            msp.add_line(points[0], points[1], dxfattribs=attribs)
            return True
        if has_z:
            pl = msp.add_polyline3d(points, dxfattribs=attribs)
            if closed:
                try:
                    pl.close(True)
                except Exception:
                    pass
            return True

        msp.add_lwpolyline([(p[0], p[1]) for p in points], format="xy", close=closed, dxfattribs=attribs)
        return True

    return False


def _collect_visible_vector_entities_for_snt_export(app) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    snt_entities = _collect_visible_snt_entities_for_export(app)
    digitizer_entities = _collect_visible_digitizer_entities_for_snt_export(app)
    all_entities = snt_entities + digitizer_entities
    stats = {
        "snt_entities": len(snt_entities),
        "digitizer_entities": len(digitizer_entities),
        "total_entities": len(all_entities),
    }
    return all_entities, stats


def _existing_snt_export_candidates(app) -> List[Path]:
    """
    Candidate list for "use same path/name" export.
    Prefer currently visible/checked attachments, then all attached files.
    """
    attachments = _dedupe_attachment_records(app)
    if not attachments:
        return []

    item_state_map = _build_snt_item_state_map(app)
    checked_paths: List[Path] = []
    all_paths: List[Path] = []

    for attachment in attachments:
        full_path = str(attachment.get("full_path", "") or "").strip()
        if not full_path:
            continue
        try:
            p = Path(full_path).resolve()
        except Exception:
            p = Path(full_path)

        all_paths.append(p)

        aliases = _attachment_aliases(attachment)
        state = None
        for a in aliases:
            if a in item_state_map:
                state = item_state_map[a]
                break
        if state is not None:
            is_checked = bool(state.get("checked", True))
        else:
            is_checked = _attachment_has_visible_actor(app, attachment)
        if is_checked:
            checked_paths.append(p)

    def _dedupe_paths(paths: List[Path]) -> List[Path]:
        out: List[Path] = []
        seen: Set[str] = set()
        for p in paths:
            key = str(p).lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        return out

    checked_paths = _dedupe_paths(checked_paths)
    all_paths = _dedupe_paths(all_paths)
    return checked_paths or all_paths


def _resolve_snt_export_output_path(app, default_dir: Path, default_base_name: str) -> Optional[str]:
    from PySide6.QtWidgets import QInputDialog

    candidates = _existing_snt_export_candidates(app)

    if candidates:
        if len(candidates) == 1:
            target = candidates[0]
            choice = QMessageBox.question(
                app,
                "Export SNT",
                "Use the existing attached SNT path/name?\n\n"
                f"{target}\n\n"
                "Yes = overwrite this file\n"
                "No = choose a different path/name",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes,
            )
            if choice == QMessageBox.Yes:
                return str(target)
            if choice == QMessageBox.Cancel:
                return None
        else:
            choice = QMessageBox.question(
                app,
                "Export SNT",
                "Use an existing attached SNT path/name, or export to a new file?\n\n"
                "Yes = choose existing SNT target\n"
                "No = choose custom path/name",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes,
            )
            if choice == QMessageBox.Yes:
                option_items = [str(p) for p in candidates]
                picked, ok = QInputDialog.getItem(
                    app,
                    "Select Existing SNT",
                    "Choose SNT file to overwrite:",
                    option_items,
                    0,
                    False,
                )
                if not ok or not picked:
                    return None
                return str(Path(picked))
            if choice == QMessageBox.Cancel:
                return None

    default_target = candidates[0] if candidates else (default_dir / f"{default_base_name}_drawings.snt")
    output_path, _ = QFileDialog.getSaveFileName(
        app,
        "Export SNT",
        str(default_target),
        "SNT Files (*.snt)",
    )
    if not output_path:
        return None
    return str(Path(output_path).with_suffix(".snt"))


def export_visible_vectors_to_snt(app, output_path: str) -> bool:
    """
    Export all currently visible vector data in the main view to SNT:
    - checked/visible attached SNT layers/entities
    - visible digitizer drawings (line/polygon/polyline/text/etc.)
    """
    try:
        import ezdxf
        import snt_core  # type: ignore
        import snt_core.snt_writer as _snt_writer  # type: ignore
    except Exception as exc:
        print(f"   ❌ SNT export backend missing: {exc}")
        return False

    # Monkey-patch snt_core to preserve true_color (RGB) from DXF entities.
    # snt_core 1.2.0's _entity_color only reads ACI color index and ignores
    # group code 420 (true_color), causing color loss on SNT export.
    _orig_entity_color = _snt_writer._entity_color

    def _patched_entity_color(entity):
        try:
            tc = getattr(entity.dxf, 'true_color', None)
            if tc is not None and int(tc) != 0:
                return (snt_core.ColorMode.RGB, int(tc))
        except Exception:
            pass
        return _orig_entity_color(entity)

    _snt_writer._entity_color = _patched_entity_color

    entities, stats = _collect_visible_vector_entities_for_snt_export(app)
    if not entities:
        print("   ⚠️ No visible vector entities to export")
        return False

    target = Path(output_path).with_suffix(".snt")
    temp_root = Path(tempfile.mkdtemp(prefix="naksha_snt_export_"))
    temp_dxf = temp_root / f"{target.stem}.dxf"
    temp_snt = temp_root / f"{target.stem}.snt"

    try:
        print(f"\n{'='*60}")
        print("📤 EXPORTING VISIBLE VECTOR DATA TO SNT")
        print(f"   Target: {target}")
        print(f"   Visible SNT entities: {stats['snt_entities']:,}")
        print(f"   Visible digitizer entities: {stats['digitizer_entities']:,}")
        print(f"   Total input entities: {stats['total_entities']:,}")

        doc = ezdxf.new("R2010")
        msp = doc.modelspace()

        emitted = 0
        for entity in entities:
            if _emit_entity_to_dxf(msp, doc, entity):
                emitted += 1

        if emitted <= 0:
            print("   ❌ No valid entities could be emitted for SNT conversion")
            return False

        doc.saveas(temp_dxf)

        result = None
        try:
            result = snt_core.convert(
                dxf_path=temp_dxf,
                snt_path=temp_snt,
                description=target.stem,
            )
        except TypeError as exc:
            # Backward compatibility for older wheel signatures.
            if "snt_path" not in str(exc):
                raise
            result = snt_core.convert(
                dxf_path=temp_dxf,
                description=target.stem,
            )

        produced = Path(getattr(result, "snt_path", temp_snt))
        if not produced.exists():
            raise RuntimeError(f"SNT conversion did not produce output file: {produced}")

        target.parent.mkdir(parents=True, exist_ok=True)

        temp_target = target.with_suffix(target.suffix + ".tmp_export")
        try:
            if temp_target.exists():
                temp_target.unlink()
        except OSError:
            pass

        shutil.copy2(produced, temp_target)
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass
        temp_target.replace(target)

        app._last_snt_export_summary = {
            "output_path": str(target),
            "input_entities": int(stats["total_entities"]),
            "snt_entities": int(stats["snt_entities"]),
            "digitizer_entities": int(stats["digitizer_entities"]),
            "emitted_entities": int(emitted),
            "backend_entities": int(getattr(result, "entity_count", emitted)),
            "backend_layers": int(getattr(result, "layer_count", 0)),
        }

        print(f"   ✅ SNT export complete")
        print(f"      Output: {target}")
        print(f"      Emitted entities: {emitted:,}")
        print(f"      Backend reported: {getattr(result, 'entity_count', emitted):,} entities")
        print(f"{'='*60}\n")
        return True

    except Exception as exc:
        print(f"   ❌ SNT export failed: {exc}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        _snt_writer._entity_color = _orig_entity_color
        shutil.rmtree(temp_root, ignore_errors=True)


# ============================================================================
# MAIN EXPORT DIALOG
# ============================================================================

def show_export_dialog(app):
    """
    Show export format selection dialog and perform export.
    """
    from PySide6.QtWidgets import QDialog, QVBoxLayout, QRadioButton, QDialogButtonBox
    from PySide6.QtCore import Qt
    from gui.theme_manager import ThemeManager, get_dialog_stylesheet
    
    dialog = QDialog(app)
    dialog.setProperty("themeStyledDialog", True)
    dialog.setWindowTitle("Export Drawings")
    dialog.setModal(True)
    dialog.setStyleSheet(get_dialog_stylesheet())
    ThemeManager.apply_native_window_theme(dialog)
    
    layout = QVBoxLayout()
    
    # Format selection

    shp_radio = QRadioButton("Shapefile")
    shp_radio.setChecked(True)
    tiff_radio = QRadioButton("GeoTIFF")
    snt_radio = QRadioButton("SNT (All Visible Vectors)")

    layout.addWidget(shp_radio)
    layout.addWidget(tiff_radio)
    layout.addWidget(snt_radio)
    # Buttons
    buttons = QDialogButtonBox(
        QDialogButtonBox.Ok | QDialogButtonBox.Cancel
    )
    ok_btn = buttons.button(QDialogButtonBox.Ok)
    cancel_btn = buttons.button(QDialogButtonBox.Cancel)
    if ok_btn:
        ok_btn.setObjectName("primaryBtn")
        ok_btn.setAutoDefault(False)
        ok_btn.setDefault(False)
        ok_btn.setFocusPolicy(Qt.NoFocus)
    if cancel_btn:
        cancel_btn.setAutoDefault(False)
        cancel_btn.setDefault(False)
        cancel_btn.setFocusPolicy(Qt.NoFocus)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    
    dialog.setLayout(layout)
    
    if dialog.exec() == QDialog.Accepted:
        # Get base filename from current file
        if hasattr(app, 'last_save_path') and app.last_save_path:
            base_name = Path(app.last_save_path).stem
            base_dir = Path(app.last_save_path).parent
        else:
            base_name = "drawings"
            base_dir = Path.home()

        output_path = None
        success = False

        if snt_radio.isChecked():
            output_path = _resolve_snt_export_output_path(app, base_dir, base_name)
            if not output_path:
                return
            success = export_visible_vectors_to_snt(app, output_path)
        else:
            if shp_radio.isChecked():
                ext = "shp"
                filter_str = "Shapefile (*.shp)"
            else:
                ext = "tif"
                filter_str = "GeoTIFF Files (*.tif *.tiff)"

            output_path, _ = QFileDialog.getSaveFileName(
                app,
                "Export Drawings",
                str(base_dir / f"{base_name}_drawings.{ext}"),
                filter_str
            )
            if not output_path:
                return

            if shp_radio.isChecked():
                success = export_drawings_to_shapefile(app, output_path)
            else:
                success = export_drawings_to_tiff(app, output_path)

        if success:
            if snt_radio.isChecked():
                summary = getattr(app, "_last_snt_export_summary", None) or {}
                backend_entities = summary.get("backend_entities", 0)
                backend_layers = summary.get("backend_layers", 0)
                QMessageBox.information(
                    app,
                    "Export Successful",
                    "Visible vector data exported to SNT:\n"
                    f"{Path(output_path).with_suffix('.snt')}\n\n"
                    f"Entities: {int(backend_entities):,}\n"
                    f"Layers: {int(backend_layers):,}"
                )
            else:
                QMessageBox.information(
                    app,
                    "Export Successful",
                    f"Drawings exported to:\n{output_path}"
                )
        else:
            QMessageBox.warning(
                app,
                "Export Failed",
                "Failed to export data. Check console for details."
            )

import vtk
import numpy as np

def create_vtk_actor_from_drawing(drawing, renderer ,z_offset=10.0):
    """
    Create a VTK actor from drawing data and add it to the renderer.
    Returns the actor so it can be stored in the drawing dict.
    """
    shape_type = drawing.get('type', 'unknown')
    coords = drawing.get('coordinates', [])
    color = drawing.get('color', (255, 255, 255))
    if z_offset != 0:
        coords = [[c[0], c[1], (c[2] if len(c) > 2 else 0) + z_offset] for c in coords]
    
    # Normalize color to 0-1 range
    color_norm = tuple(c / 255.0 for c in color)
    
    actor = None
    
    try:
        # ============================================================
        # CIRCLE
        # ============================================================
        if shape_type == 'circle' and len(coords) >= 1:
            center = coords[0]
            radius = drawing.get('radius', 5.0)
            
            # Create circle using vtkRegularPolygonSource
            circle_source = vtk.vtkRegularPolygonSource()
            circle_source.SetNumberOfSides(32)
            circle_source.SetRadius(radius)
            circle_source.SetCenter(center[0], center[1], center[2] if len(center) > 2 else 0)
            circle_source.GeneratePolygonOff()  # Just the outline
            circle_source.Update()
            
            # Create mapper and actor
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputConnection(circle_source.GetOutputPort())
            
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(*color_norm)
            actor.GetProperty().SetLineWidth(3)  # Thicker for visibility
            actor.GetProperty().SetRepresentationToWireframe()
            actor.VisibilityOn()  # Ensure visibility
            actor.PickableOn()    # Enable picking
        
        # ============================================================
        # LINE
        # ============================================================
        elif shape_type == 'line' and len(coords) >= 2:
            points = vtk.vtkPoints()
            for coord in coords:
                points.InsertNextPoint(
                    coord[0], 
                    coord[1], 
                    coord[2] if len(coord) > 2 else 0
                )
            
            line = vtk.vtkPolyLine()
            line.GetPointIds().SetNumberOfIds(len(coords))
            for i in range(len(coords)):
                line.GetPointIds().SetId(i, i)
            
            cells = vtk.vtkCellArray()
            cells.InsertNextCell(line)
            
            polydata = vtk.vtkPolyData()
            polydata.SetPoints(points)
            polydata.SetLines(cells)
            
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(polydata)
            
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(*color_norm)
            actor.GetProperty().SetLineWidth(3)  # Thicker for visibility
            actor.VisibilityOn()  # Ensure visibility
            actor.PickableOn()    # Enable picking
        
        # ============================================================
        # POLYLINE / FREEHAND
        # ============================================================
        elif shape_type in ['polyline', 'freehand'] and len(coords) >= 2:
            points = vtk.vtkPoints()
            for coord in coords:
                points.InsertNextPoint(
                    coord[0], 
                    coord[1], 
                    coord[2] if len(coord) > 2 else 0
                )
            
            polyline = vtk.vtkPolyLine()
            polyline.GetPointIds().SetNumberOfIds(len(coords))
            for i in range(len(coords)):
                polyline.GetPointIds().SetId(i, i)
            
            cells = vtk.vtkCellArray()
            cells.InsertNextCell(polyline)
            
            polydata = vtk.vtkPolyData()
            polydata.SetPoints(points)
            polydata.SetLines(cells)
            
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(polydata)
            
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(*color_norm)
            actor.GetProperty().SetLineWidth(3)  # Thicker for visibility
            actor.VisibilityOn()  # Ensure visibility
            actor.PickableOn()    # Enable picking
        
        # ============================================================
        # POLYGON
        # ============================================================
        elif shape_type == 'polygon' and len(coords) >= 3:
            points = vtk.vtkPoints()
            for coord in coords:
                points.InsertNextPoint(
                    coord[0], 
                    coord[1], 
                    coord[2] if len(coord) > 2 else 0
                )
            
            polygon = vtk.vtkPolygon()
            polygon.GetPointIds().SetNumberOfIds(len(coords))
            for i in range(len(coords)):
                polygon.GetPointIds().SetId(i, i)
            
            cells = vtk.vtkCellArray()
            cells.InsertNextCell(polygon)
            
            polydata = vtk.vtkPolyData()
            polydata.SetPoints(points)
            polydata.SetPolys(cells)
            
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(polydata)
            
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(*color_norm)
            actor.GetProperty().SetLineWidth(3)  # Thicker for visibility
            actor.GetProperty().SetRepresentationToWireframe()
            actor.VisibilityOn()  # Ensure visibility
            actor.PickableOn()    # Enable picking
        
        # ============================================================
        # RECTANGLE
        # ============================================================
        elif shape_type == 'rectangle' and len(coords) >= 2:
            p1, p2 = coords[0], coords[1]
            
            # Create 4 corners
            rect_coords = [
                [p1[0], p1[1], p1[2] if len(p1) > 2 else 0],
                [p2[0], p1[1], p1[2] if len(p1) > 2 else 0],
                [p2[0], p2[1], p2[2] if len(p2) > 2 else 0],
                [p1[0], p2[1], p2[2] if len(p2) > 2 else 0]
            ]
            
            points = vtk.vtkPoints()
            for coord in rect_coords:
                points.InsertNextPoint(coord[0], coord[1], coord[2])
            
            polygon = vtk.vtkPolygon()
            polygon.GetPointIds().SetNumberOfIds(4)
            for i in range(4):
                polygon.GetPointIds().SetId(i, i)
            
            cells = vtk.vtkCellArray()
            cells.InsertNextCell(polygon)
            
            polydata = vtk.vtkPolyData()
            polydata.SetPoints(points)
            polydata.SetPolys(cells)
            
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(polydata)
            
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(*color_norm)
            actor.GetProperty().SetLineWidth(3)  # Thicker for visibility
            actor.GetProperty().SetRepresentationToWireframe()
            actor.VisibilityOn()  # Ensure visibility
            actor.PickableOn()    # Enable picking
            
            # Print rectangle bounds for debugging
            print(f"       Rectangle bounds: X({p1[0]:.1f} to {p2[0]:.1f}), Y({p1[1]:.1f} to {p2[1]:.1f})")
        
        # ============================================================
        # ============================================================
        elif shape_type == 'text' and len(coords) >= 1:
            pos = coords[0]
            
            sphere = vtk.vtkSphereSource()
            sphere.SetCenter(pos[0], pos[1], pos[2] if len(pos) > 2 else 0)
            sphere.SetRadius(5.0)  # Larger for visibility
            sphere.SetThetaResolution(16)
            sphere.SetPhiResolution(16)
            
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputConnection(sphere.GetOutputPort())
            
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(*color_norm)
            actor.VisibilityOn()  # Ensure visibility
            actor.PickableOn()    # Enable picking
        
        # Add actor to renderer and verify
        if actor and renderer:
            renderer.AddActor(actor)
            
            # Verify actor was added
            num_actors = renderer.GetActors().GetNumberOfItems()
            print(f"   ✅ VTK actor created and added to renderer for {shape_type}")
            print(f"       Total actors in renderer: {num_actors}")
            print(f"       Actor visibility: {actor.GetVisibility()}")
            print(f"       Actor color: {actor.GetProperty().GetColor()}")
            
            return actor
        else:
            print(f"   ⚠️ Failed to create actor for {shape_type}")
            return None
            
    except Exception as e:
        print(f"   ❌ Error creating VTK actor for {shape_type}: {e}")
        import traceback
        traceback.print_exc()
        return None
    

def _infer_import_scene_z(app) -> float:
    """Choose a visible Z plane for imported vector data when the source has no Z."""
    try:
        data = getattr(app, "data", None)
        if isinstance(data, dict):
            xyz = data.get("xyz")
            if xyz is not None:
                xyz = np.asarray(xyz)
                if xyz.ndim == 2 and xyz.shape[1] >= 3 and len(xyz) > 0:
                    return float(np.median(xyz[:, 2]))
    except Exception:
        pass

    try:
        renderer = getattr(getattr(app, "vtk_widget", None), "renderer", None)
        if renderer is not None:
            return float(renderer.GetActiveCamera().GetFocalPoint()[2])
    except Exception:
        pass

    return 0.0

def _infer_scene_xy_bounds(app):
    """
    Best-effort (xmin, xmax, ymin, ymax) of the current scene, used to place a
    raster that has NO georeferencing so it lands on top of the real data
    instead of at pixel coordinates far away.

    Priority: point cloud xyz → existing renderer actor bounds → None.
    """
    # 1) Point cloud data
    try:
        data = getattr(app, "data", None)
        if isinstance(data, dict):
            xyz = data.get("xyz")
            if xyz is not None:
                xyz = np.asarray(xyz)
                if xyz.ndim == 2 and xyz.shape[1] >= 2 and len(xyz) > 0:
                    return (float(np.min(xyz[:, 0])), float(np.max(xyz[:, 0])),
                            float(np.min(xyz[:, 1])), float(np.max(xyz[:, 1])))
    except Exception:
        pass

    # 2) Whatever is already drawn (imported vectors etc.)
    xmin = ymin = float("inf")
    xmax = ymax = float("-inf")
    found = False
    try:
        renderer = getattr(getattr(app, "vtk_widget", None), "renderer", None)
        dz = getattr(app, "digitizer", None)
        ovl = getattr(dz, "overlay_renderer", None) if dz is not None else None
        for ren in (renderer, ovl):
            if ren is None:
                continue
            props = ren.GetViewProps()
            props.InitTraversal()
            for _ in range(props.GetNumberOfItems()):
                prop = props.GetNextProp()
                try:
                    b = prop.GetBounds()
                except Exception:
                    b = None
                if not b or len(b) < 6 or b[0] > b[1]:
                    continue
                found = True
                xmin, xmax = min(xmin, b[0]), max(xmax, b[1])
                ymin, ymax = min(ymin, b[2]), max(ymax, b[3])
    except Exception:
        pass

    if found and xmax > xmin and ymax > ymin:
        return (xmin, xmax, ymin, ymax)
    return None


def read_world_file_bounds(tif_path, width, height):
    """
    Look for a sidecar world file next to an image and return the real-world
    bounds (west, south, east, north) for a width×height raster, or None.

    World file (6 lines): A, D, B, E, C, F
        A = x pixel size,  E = y pixel size (usually negative)
        D, B = rotation terms (we only handle the unrotated case)
        C, F = world coords of the CENTRE of the top-left pixel
    Recognised extensions: .tfw .tifw .wld  and  <name>.tif.tfw / .wld
    """
    from pathlib import Path
    p = Path(str(tif_path))
    candidates = [
        p.with_suffix(".tfw"), p.with_suffix(".tifw"), p.with_suffix(".wld"),
        Path(str(p) + ".tfw"), Path(str(p) + ".wld"),
    ]
    wf = next((c for c in candidates if c.exists()), None)
    if wf is None:
        return None
    try:
        nums = []
        for line in wf.read_text().splitlines():
            line = line.strip()
            if line:
                nums.append(float(line))
            if len(nums) >= 6:
                break
        if len(nums) < 6:
            return None
        A, D, B, E, C, F = nums[:6]
        if abs(D) > 1e-9 or abs(B) > 1e-9:
            print(f"   ⚠️ World file {wf.name} has rotation terms — "
                  f"using axis-aligned bounds only (no rotation applied)")
        left = C - A / 2.0
        top = F - E / 2.0
        right = left + A * float(width)
        bottom = top + E * float(height)
        west, east = min(left, right), max(left, right)
        south, north = min(bottom, top), max(bottom, top)
        print(f"   🌍 World file found: {wf.name} → "
              f"X({west:.2f}→{east:.2f}) Y({south:.2f}→{north:.2f})")
        return (west, south, east, north)
    except Exception as exc:
        print(f"   ⚠️ Could not parse world file {wf}: {exc}")
        return None

def _coords_to_scene_z(coord_iterable, default_z: float):
    coords = []
    z_values = []
    for coord in coord_iterable:
        if len(coord) >= 3:
            z = float(coord[2])
            import math
            if z is not None and not math.isnan(z) and not math.isinf(z) and abs(z) > 1e-4 and -10000.0 <= z <= 10000.0:
                z_values.append(z)
                
    if len(z_values) > 0:
        import numpy as np
        flat_z = float(np.median(z_values))
    else:
        flat_z = default_z
        
    for coord in coord_iterable:
        coords.append([float(coord[0]), float(coord[1]), flat_z])
    return coords
    
def import_drawings_from_shapefile_with_rendering(app, input_path: str) -> bool:
    """
    Import shapefile and reconstruct VTK actors for every feature.
 
    Geometry → digitize type mapping:
      Point   → type from 'type' attribute (default 'circle')
      Line    → type from 'type' attribute (default 'smartline')
      Polygon → type from 'type' attribute (default 'polygon')
 
    Supports all types written by the export function above:
      circle, rectangle, polyline, freehand, polygon, smartline,
      centerline, line, line_segment, text.
    """
    try:
        import geopandas as gpd
        from pathlib import Path
 
        print(f"\n{'='*60}")
        print(f"📥 IMPORTING DRAWINGS FROM SHAPEFILE")
        print(f"   Path: {input_path}")
 
        if not hasattr(app, 'digitizer') or not app.digitizer:
            print(f"   ❌ app.digitizer not available")
            return False
 
        if not hasattr(app.digitizer, 'add_drawing_from_data'):
            print(f"   ❌ app.digitizer.add_drawing_from_data not available")
            return False
 
        base_path = Path(input_path).with_suffix('')
        scene_z   = _infer_import_scene_z(app)
        print(f"   📐 Scene Z = {scene_z:.2f}")
 
        imported_count = 0
        failed_count   = 0
        imported_points = []
 
        # Build the list of candidate SHP files to try.
        # Priority: single-file (the new format) first, then legacy split files
        single_file = str(base_path) + ".shp"
        legacy_suffixes = ['_points', '_lines', '_polygons']
        candidate_files = []

        if Path(single_file).exists():
            # New single-file export — load it directly
            candidate_files.append(single_file)
        else:
            # Fall back to the old split-file format
            for suffix in legacy_suffixes:
                p = f"{base_path}{suffix}.shp"
                if Path(p).exists():
                    candidate_files.append(p)
            # Also check bare stem (no suffix), just in case
            bare = f"{base_path}.shp"
            if Path(bare).exists() and bare not in candidate_files:
                candidate_files.append(bare)

        if not candidate_files:
            print(f"   ❌ No matching .shp file(s) found near: {input_path}")
            return False

        for shp_path in candidate_files:
            print(f"\n   📂 Loading: {shp_path}")
            try:
                gdf = gpd.read_file(shp_path)
            except Exception as e:
                print(f"   ⚠️ Could not read {shp_path}: {e}")
                continue
                
            # Establish/confirm the ONE authoritative canvas CRS from this
            # shapefile's own CRS (first trustworthy dataset wins), then
            # reproject into whatever canvas CRS is authoritative. Reading
            # app.project_crs_epsg directly (the old behaviour) missed the
            # case where THIS import is the first georeferenced dataset: the
            # canvas CRS would never get established, and every later
            # SNT/LAZ/basemap load would have nothing to align against.
            target_crs = None
            try:
                from gui.crs_manager import ensure_canvas_crs, get_canvas_crs
                if gdf.crs is not None:
                    ensure_canvas_crs(app, gdf.crs, source="Shapefile .prj",
                                      dataset=shp_path)
                target_crs = get_canvas_crs(app)
            except Exception as e:
                print(f"   ⚠️ canvas CRS unavailable: {e}")
            if target_crs is not None and gdf.crs is not None:
                try:
                    if not gdf.crs.equals(target_crs):
                        print(f"   🔄 Reprojecting shapefile from {gdf.crs} to canvas CRS {target_crs}")
                        gdf = gdf.to_crs(target_crs)
                except Exception as e:
                    print(f"   ⚠️ Could not reproject shapefile: {e}")
 
            print(f"   📊 {len(gdf)} feature(s)")
 
            for idx, row in gdf.iterrows():
                try:
                    geom = row.geometry
                    if geom is None or geom.is_empty:
                        continue
 
                    # ── read stored attributes ────────────────────────────
                    try:
                        color_r = int(row.get('color_r', 255))
                    except (TypeError, ValueError):
                        color_r = 255
                    try:
                        color_g = int(row.get('color_g', 255))
                    except (TypeError, ValueError):
                        color_g = 255
                    try:
                        color_b = int(row.get('color_b', 255))
                    except (TypeError, ValueError):
                        color_b = 255
                    try:
                        radius = float(row.get('radius', 0.0) or 0.0)
                    except (TypeError, ValueError):
                        radius = 0.0
                    try:
                        lwidth = int(row.get('lwidth', 2) or 2)
                    except (TypeError, ValueError):
                        lwidth = 2
                    try:
                        lstyle = str(row.get('lstyle', 'solid') or 'solid')
                    except (TypeError, ValueError):
                        lstyle = 'solid'
 
                    drawing_data = {
                        'color':          (color_r, color_g, color_b),
                        'text':           str(row.get('text', '') or ''),
                        'radius':         radius,
                        'original_width': lwidth,
                        'original_style': lstyle,
                    }
 
                    # ── convert geometry to coordinates + assign type ─────
                    if geom.geom_type == 'Point':
                        # Default: circle (text has its own type stored)
                        stored_type = str(row.get('type', '') or '')
                        drawing_data['type'] = stored_type or 'circle'
 
                        has_z = getattr(geom, 'has_z', False)
                        raw = [(geom.x, geom.y, geom.z)] if has_z else [(geom.x, geom.y)]
                        drawing_data['coordinates'] = _coords_to_scene_z(raw, scene_z)
                        imported_points.extend(drawing_data['coordinates'])
 
                    elif geom.geom_type == 'LineString':
                        # Default: smartline (open multi-point line)
                        # NOTE: 'polyline' is never a LineString after our fix —
                        # it's now a Polygon.  But if someone imports an external
                        # shapefile without our type attribute, default to smartline
                        # (an open line) rather than polyline (a closed loop).
                        stored_type = str(row.get('type', '') or '')
                        drawing_data['type'] = stored_type or 'smartline'
 
                        line_coords = _coords_to_scene_z(list(geom.coords), scene_z)
                        drawing_data['coordinates'] = line_coords
                        imported_points.extend(line_coords)
 
                    elif geom.geom_type == 'Polygon':
                        # Could be: circle, polyline, rectangle, freehand, polygon
                        stored_type = str(row.get('type', '') or '')
                        drawing_data['type'] = stored_type or 'polygon'
 
                        # Use exterior ring; shapely already closes it (first==last)
                        poly_coords = _coords_to_scene_z(
                            list(geom.exterior.coords), scene_z
                        )
                        drawing_data['coordinates'] = poly_coords
                        imported_points.extend(poly_coords)
 
                        if stored_type == 'circle' and radius > 0:
                            drawing_data['radius'] = radius
 
                    else:
                        # MultiPolygon, MultiLineString, etc. — skip
                        print(f"      ⚠️ Unsupported geometry: {geom.geom_type}")
                        failed_count += 1
                        continue
 
                    # ── send to digitizer ─────────────────────────────────
                    print(f"      ← {drawing_data['type']} "
                          f"({len(drawing_data['coordinates'])} pts)")
 
                    success = app.digitizer.add_drawing_from_data(drawing_data)
                    if success:
                        imported_count += 1
                    else:
                        failed_count += 1
                        print(f"      ❌ add_drawing_from_data returned False")
 
                except Exception as e:
                    failed_count += 1
                    print(f"      ❌ Feature error: {e}")
                    import traceback
                    traceback.print_exc()
 
        # ── final render + camera ─────────────────────────────────────────
        print(f"\n   📊 Import summary: ✅ {imported_count}  ❌ {failed_count}")
 
        if imported_count > 0:
            print(f"   🎨 Rendering scene...")
            try:
                if hasattr(app.digitizer, 'overlay_renderer') and app.digitizer.overlay_renderer:
                    app.digitizer.overlay_renderer.Modified()
                    app.digitizer.overlay_renderer.ResetCameraClippingRange()
                app.digitizer.renderer.Modified()
                app.digitizer.renderer.ResetCameraClippingRange()
            except Exception:
                pass
 
            if hasattr(app, 'vtk_widget') and app.vtk_widget:
                rw = app.vtk_widget.GetRenderWindow()
                renderer = rw.GetRenderers().GetFirstRenderer()
                if renderer:
                    renderer.ResetCameraClippingRange()
                rw.Render()
                try:
                    app.vtk_widget.render()
                except Exception:
                    pass
 
            # Fit view to imported drawings
            fit_view = getattr(app, 'fit_view', None)
            if imported_points and callable(fit_view):
                try:
                    fit_view()
                    print(f"   ✅ View fitted to imported drawings")
                except Exception as e:
                    print(f"   ⚠️ fit_view: {e}")
 
        print(f"{'='*60}\n")
        return imported_count > 0
 
    except ImportError as e:
        print(f"   ❌ Missing library: {e}")
        print(f"   💡 pip install geopandas shapely")
        return False
    except Exception as e:
        print(f"   ❌ Shapefile import failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
def show_import_dialog(app):
    """Show import dialog with texture option and multiple file selection"""
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QRadioButton, 
        QDialogButtonBox, QFileDialog, QMessageBox, QProgressDialog
    )
    from PySide6.QtCore import Qt, QCoreApplication
    from pathlib import Path
    from gui.theme_manager import ThemeManager, get_dialog_stylesheet
    
    dialog = QDialog(app)
    dialog.setProperty("themeStyledDialog", True)
    dialog.setWindowTitle("Import Drawings")
    dialog.setModal(True)
    dialog.setStyleSheet(get_dialog_stylesheet())
    ThemeManager.apply_native_window_theme(dialog)
    
    layout = QVBoxLayout()
    
    # Format selection
    shp_radio = QRadioButton("Shapefile (Vector)")
    tiff_texture_radio = QRadioButton("GeoTIFF (Textured Surface)")
    tiff_vector_radio = QRadioButton("GeoTIFF (Vectorized)")
    
    tiff_texture_radio.setChecked(True)  # Default to texture
    
    layout.addWidget(shp_radio)
    layout.addWidget(tiff_texture_radio)
    layout.addWidget(tiff_vector_radio)
    
    # Buttons
    buttons = QDialogButtonBox(
        QDialogButtonBox.Ok | QDialogButtonBox.Cancel
    )
    ok_btn = buttons.button(QDialogButtonBox.Ok)
    cancel_btn = buttons.button(QDialogButtonBox.Cancel)
    if ok_btn:
        ok_btn.setObjectName("primaryBtn")
        ok_btn.setAutoDefault(False)
        ok_btn.setDefault(False)
        ok_btn.setFocusPolicy(Qt.NoFocus)
    if cancel_btn:
        cancel_btn.setAutoDefault(False)
        cancel_btn.setDefault(False)
        cancel_btn.setFocusPolicy(Qt.NoFocus)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    
    dialog.setLayout(layout)
    
    if dialog.exec() == QDialog.Accepted:
        # Determine filter
        if shp_radio.isChecked():
            filter_str = "Shapefile (*.shp)"
        else:
            filter_str = "GeoTIFF Files (*.tif *.tiff)"
        
        # ✅ MULTIPLE FILE SELECTION
        input_paths, _ = QFileDialog.getOpenFileNames(
            app,
            "Import Files (Multiple Selection Enabled)",
            str(Path.home()),
            filter_str
        )
        
        if not input_paths:
            return  # User cancelled
        
        # Process multiple files
        total_files = len(input_paths)
        successful = 0
        failed = 0
        
        print(f"\n{'='*60}")
        print(f"📥 IMPORTING {total_files} FILE(S)")
        print(f"{'='*60}")
        
        # Progress dialog for multiple files
        progress = QProgressDialog(
            f"Importing files...",
            "Cancel",
            0,
            total_files,
            app
        )
        progress.setWindowTitle("Import Progress")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        
        for idx, input_path in enumerate(input_paths):
            if progress.wasCanceled():
                print(f"\n⚠️ Import cancelled by user")
                break
            
            progress.setValue(idx)
            progress.setLabelText(f"Importing {idx+1}/{total_files}: {Path(input_path).name}")
            QCoreApplication.processEvents()
            
            print(f"\n📂 File {idx+1}/{total_files}: {Path(input_path).name}")
            
            try:
                success = False
                
                if shp_radio.isChecked():
                    success = import_drawings_from_shapefile_with_rendering(app, input_path)
                elif tiff_texture_radio.isChecked():
                    success = import_geotiff_as_texture(app, input_path)
                else:
                    success = import_drawings_from_tiff(app, input_path)
                
                if success:
                    successful += 1
                    print(f"✅ Success")
                else:
                    failed += 1
                    print(f"❌ Failed")
                    
            except Exception as e:
                failed += 1
                print(f"❌ Error: {e}")
                import traceback
                traceback.print_exc()
        
        progress.setValue(total_files)
        progress.close()
        
        # Summary message
        print(f"\n{'='*60}")
        print(f"📊 IMPORT SUMMARY")
        print(f"   Total files: {total_files}")
        print(f"   ✅ Successful: {successful}")
        print(f"   ❌ Failed: {failed}")
        print(f"{'='*60}\n")
        
        if successful > 0:
            QMessageBox.information(
                app,
                "Import Complete",
                f"Successfully imported {successful} of {total_files} file(s).\n\n"
                f"✅ Successful: {successful}\n"
                f"❌ Failed: {failed}\n\n"
                f"Check console for details."
            )
        else:
            QMessageBox.warning(
                app,
                "Import Failed",
                f"Failed to import all {total_files} file(s).\n\n"
                f"Check console for details."
            )
        
        
   
def export_drawings_to_tiff(app, output_path: str) -> bool:
    """
    Export drawings AND point cloud data to GeoTIFF format.
    Creates a raster with point cloud as background and drawings overlaid.
    """
    try:
        import rasterio
        from rasterio.features import rasterize
        from shapely.geometry import Point, LineString, Polygon
        import numpy as np
        print("✅ All imports successful")
        
        # Now run diagnostics
        try:
            diagnose_app_data(app)
        except Exception as e:
            print(f"❌ Diagnostic failed: {e}")
            import traceback
            traceback.print_exc()
        # ADD THIS LINE HERE:
        
        print(f"\n{'='*60}")
        print(f"📤 EXPORTING TO TIFF (Point Cloud + Drawings)")
        print(f"   Path: {output_path}")
        
        # ============================================================
        # STEP 1: Get Point Cloud Data
        # ============================================================
        point_cloud_data = None

        print(f"\n   🔍 SEARCHING FOR POINT CLOUD DATA:")

        # Try common attribute names
        for attr_name in ['point_cloud', 'points', 'cloud', 'pc', 'vtk_points', 
                        'point_data', 'xyz_data', 'las_points', 'filtered_points',
                        'original_points', 'loaded_points']:
            if hasattr(app, attr_name):
                val = getattr(app, attr_name)
                print(f"   • Found app.{attr_name}: {type(val)}")
                
                if val is not None:
                    # Check if it's a VTK object
                    if hasattr(val, 'GetNumberOfPoints'):
                        num_points = val.GetNumberOfPoints()
                        print(f"      → VTK PolyData with {num_points} points")
                        # Extract points from VTK
                        point_cloud_data = []
                        for i in range(num_points):
                            pt = val.GetPoint(i)
                            point_cloud_data.append(pt)
                        print(f"   ✅ Extracted {len(point_cloud_data)} points from VTK")
                        break
                    
                    # Check if it's a numpy array
                    elif hasattr(val, 'shape'):
                        print(f"      → NumPy array: shape {val.shape}")
                        if len(val.shape) >= 2 and val.shape[1] >= 2:
                            point_cloud_data = val
                            print(f"   ✅ Using numpy array: {len(point_cloud_data)} points")
                            break
                    
                    # Check if it's a list
                    elif isinstance(val, list) and len(val) > 0:
                        print(f"      → List with {len(val)} items")
                        # Check if first item looks like a point
                        if len(val) > 0 and hasattr(val[0], '__len__') and len(val[0]) >= 2:
                            point_cloud_data = val
                            print(f"   ✅ Using list: {len(point_cloud_data)} points")
                            break
                        
        # ✅ ADD THE VTK CHECK HERE - RIGHT AFTER THE FOR LOOP ENDS
        # ✅ Check VTK widget for point cloud
        # ✅ Check VTK widget for point cloud (improved version)
        if point_cloud_data is None and hasattr(app, 'vtk_widget'):
            print(f"   • Searching ALL VTK actors for point clouds...")
            try:
                renderer = app.vtk_widget.GetRenderWindow().GetRenderers().GetFirstRenderer()
                actors = renderer.GetActors()
                actors.InitTraversal()
                
                largest_actor = None
                max_points = 0
                
                for i in range(actors.GetNumberOfItems()):
                    actor = actors.GetNextActor()
                    if not actor:
                        continue
                        
                    mapper = actor.GetMapper()
                    if not mapper:
                        continue
                        
                    input_data = mapper.GetInput()
                    if not input_data or not hasattr(input_data, 'GetNumberOfPoints'):
                        continue
                    
                    num_points = input_data.GetNumberOfPoints()
                    print(f"      → Actor {i}: {num_points} points")
                    
                    # Track largest actor (likely the point cloud)
                    if num_points > max_points:
                        max_points = num_points
                        largest_actor = input_data
                
                # Use largest actor if it has many points (likely point cloud)
                if largest_actor and max_points > 100:
                    print(f"      → Using largest actor: {max_points} points")
                    point_cloud_data = []
                    for j in range(max_points):
                        pt = largest_actor.GetPoint(j)
                        point_cloud_data.append(pt)
                    print(f"   ✅ Extracted {len(point_cloud_data)} points from VTK renderer")
            except Exception as e:
                print(f"      ⚠️ Error searching VTK actors: {e}")
                import traceback
                traceback.print_exc()

        # This line stays where it is
        if point_cloud_data is None:
            print(f"   ⚠️ No point cloud data found in any standard location")
        else:
            print(f"   ✅ Point cloud data loaded: {len(point_cloud_data)} points")
        
        
        # ============================================================
        # STEP 2: Get Drawings
        # ============================================================
        drawings = []
        if hasattr(app, 'digitizer') and app.digitizer:
            drawings = app.digitizer.drawings or []
            print(f"   📊 Drawings found: {len(drawings)}")
        else:
            print(f"   ⚠️ No drawings found")
        
        # Check if we have ANY data to export
        if point_cloud_data is None and not drawings:
            print(f"   ❌ No data to export (no point cloud or drawings)")
            return False
        
        # ============================================================
        # STEP 3: Calculate Bounds
        # ============================================================
        # ============================================================
        # STEP 3: Calculate Bounds
        # ============================================================
        all_coords = []

        # Add point cloud coordinates
        if point_cloud_data is not None:
            if hasattr(point_cloud_data, 'shape'):
                # NumPy array format
                if len(point_cloud_data.shape) == 2 and point_cloud_data.shape[1] >= 2:
                    all_coords.extend([(p[0], p[1]) for p in point_cloud_data[:, :2]])
            elif isinstance(point_cloud_data, list):
                # List format
                all_coords.extend([(p[0], p[1]) for p in point_cloud_data if len(p) >= 2])

        # Add drawing coordinates with diagnostics
        print(f"\n   🔍 EXTRACTING DRAWING COORDINATES:")
        for idx, drawing in enumerate(drawings):
            print(f"\n   Drawing {idx+1}:")
            print(f"      Type: {drawing.get('type', 'unknown')}")
            print(f"      Keys: {list(drawing.keys())}")
            
            # Try multiple possible coordinate keys
            coords = None
            for key in ['coordinates', 'coords', 'points', 'vertices']:
                if key in drawing:
                    coords = drawing[key]
                    print(f"      ✅ Found '{key}': {type(coords)}")
                    
                    # Handle numpy arrays
                    if hasattr(coords, 'tolist'):
                        coords = coords.tolist()
                        print(f"      → Converted from numpy to list")
                    
                    # Show first few coordinates
                    if coords and len(coords) > 0:
                        print(f"      → Length: {len(coords)}")
                        print(f"      → First coord: {coords[0]}")
                        if len(coords) > 1:
                            print(f"      → Last coord: {coords[-1]}")
                    
                    break
            
            if coords is None:
                print(f"      ⚠️ No coordinates found with any key!")
                print(f"      Full drawing data: {drawing}")
                continue
            
            # Extract valid coordinates
            extracted = 0
            for c in coords:
                try:
                    if hasattr(c, 'tolist'):
                        c = c.tolist()
                    
                    if isinstance(c, (list, tuple)) and len(c) >= 2:
                        all_coords.append((float(c[0]), float(c[1])))
                        extracted += 1
                except Exception as e:
                    print(f"      ⚠️ Failed to extract coord {c}: {e}")
                    continue
            
            print(f"      ✅ Extracted {extracted} valid coordinates")

        print(f"\n   📊 Total coordinates collected: {len(all_coords)}")

        if not all_coords:
            print(f"   ❌ No valid coordinates found")
            print(f"\n   💡 TIP: Check the drawing structure above")
            return False

        xs, ys = zip(*all_coords)
        minx, maxx = min(xs), max(xs)
        miny, maxy = min(ys), max(ys)

        # Add 10% buffer
        buffer = max((maxx - minx), (maxy - miny)) * 0.1
        minx -= buffer
        maxx += buffer
        miny -= buffer
        maxy += buffer

        print(f"   📐 Bounds: X({minx:.2f} to {maxx:.2f}), Y({miny:.2f} to {maxy:.2f})")
        # ============================================================
        # STEP 4: Set Resolution and Create Transform
        # ============================================================
        # ============================================================
        # STEP 4: Set Resolution and Create Transform
        # ============================================================
        # Calculate extent
        extent_x = maxx - minx
        extent_y = maxy - miny

        print(f"   📏 Extent: X={extent_x:.2f}m, Y={extent_y:.2f}m")

        # Auto-calculate resolution to get reasonable image size
        target_pixels = 2000  # Target size for larger dimension
        resolution = max(extent_x, extent_y) / target_pixels

        # Minimum resolution to prevent huge files
        min_resolution = 0.1
        resolution = max(resolution, min_resolution)

        width = int(extent_x / resolution)
        height = int(extent_y / resolution)

        # Ensure minimum size
        if width < 10:
            width = 10
            resolution = extent_x / width
        if height < 10:
            height = 10
            resolution = max(resolution, extent_y / height)

        # Limit maximum size to prevent memory issues
        max_dimension = 10000
        if width > max_dimension:
            width = max_dimension
            resolution = extent_x / width
        if height > max_dimension:
            height = max_dimension
            resolution = max(resolution, extent_y / height)

        # Recalculate final dimensions
        width = max(10, int(extent_x / resolution))
        height = max(10, int(extent_y / resolution))

        print(f"   📐 Raster size: {width}x{height} pixels @ {resolution:.3f}m/pixel")

        if width <= 0 or height <= 0:
            print(f"   ❌ Invalid raster dimensions: {width}x{height}")
            return False

        from rasterio.transform import from_bounds
        transform = from_bounds(minx, miny, maxx, maxy, width, height)
        
        # ============================================================
        # STEP 5: Rasterize Point Cloud (Background Layer)
        # ============================================================
        raster = np.zeros((height, width), dtype=np.uint8)
        
        if point_cloud_data is not None:
            print(f"   🔄 Rasterizing point cloud...")
            
            for point in point_cloud_data:
                try:
                    x, y = point[0], point[1]
                    
                    # Convert world coords to pixel coords
                    col = int((x - minx) / resolution)
                    row = int((maxy - y) / resolution)
                    
                    if 0 <= row < height and 0 <= col < width:
                        raster[row, col] = 128  # Gray for point cloud
                except Exception:
                    continue
            
            print(f"   ✅ Point cloud rasterized")
        
        # ============================================================
        # STEP 6: Rasterize Drawings (Overlay Layer)
        # ============================================================
        if drawings:
            print(f"   🔄 Rasterizing {len(drawings)} drawings...")
            
            shapes = []
            for drawing in drawings:
                shape_type = drawing.get('type')
                coords = drawing.get('coords', drawing.get('coordinates', []))
                
                try:
                    if shape_type in ['circle', 'text'] and len(coords) >= 1:
                        geom = Point(coords[0][:2])
                        shapes.append((geom.buffer(2.0), 255))  # White dots
                    
                    elif shape_type in ['line', 'polyline', 'freehand', 'smartline', 'centerline', 'line_segment'] and len(coords) >= 2:
                        geom = LineString([c[:2] for c in coords])
                        shapes.append((geom.buffer(0.5), 255))  # White lines
                    
                    elif shape_type in ['polygon', 'rectangle'] and len(coords) >= 3:
                        poly_coords = [c[:2] for c in coords]
                        if poly_coords[0] != poly_coords[-1]:
                            poly_coords.append(poly_coords[0])
                        geom = Polygon(poly_coords)
                        shapes.append((geom, 255))  # White polygons
                except Exception as e:
                    print(f"   ⚠️ Failed to rasterize {shape_type}: {e}")
                    continue
            
            if shapes:
                drawing_raster = rasterize(
                    shapes,
                    out_shape=(height, width),
                    transform=transform,
                    fill=0,
                    dtype=np.uint8
                )
                
                # Overlay drawings on point cloud (drawings take priority)
                raster = np.where(drawing_raster > 0, drawing_raster, raster)
                print(f"   ✅ Drawings overlaid")
        
        # ============================================================
        # STEP 7: Write GeoTIFF
        # ============================================================
        crs = f"EPSG:{app.project_crs_epsg}" if hasattr(app, 'project_crs_epsg') and app.project_crs_epsg else "EPSG:4326"
        print(f"   🌍 Using CRS: {crs}")
        
        with rasterio.open(
            output_path,
            'w',
            driver='GTiff',
            height=height,
            width=width,
            count=1,
            dtype=raster.dtype,
            crs=crs,
            transform=transform,
            compress='lzw'
        ) as dst:
            dst.write(raster, 1)
        
        print(f"   ✅ TIFF export successful!")
        print(f"   📊 Summary:")
        print(f"      - Point cloud: {'Yes' if point_cloud_data is not None else 'No'}")
        print(f"      - Drawings: {len(drawings)}")
        print(f"      - Size: {width}x{height} pixels")
        print(f"{'='*60}\n")
        return True
        
    except ImportError as e:
        print(f"   ⚠️ Missing library: {e}")
        print(f"   💡 Install: pip install rasterio shapely")
        return False
    except Exception as e:
        print(f"   ❌ TIFF export failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def import_drawings_from_tiff(app, input_path: str) -> bool:
    """
    Import drawings from GeoTIFF — optimized with progress bar for large files.
    Z coordinates are placed at the scene median so drawings are visible.
    """
    try:
        import rasterio
        from rasterio.features import shapes
        from shapely.geometry import shape
        from pathlib import Path
        from PySide6.QtWidgets import QProgressDialog, QMessageBox
        from PySide6.QtCore import Qt, QCoreApplication
        import numpy as np
 
        print(f"\n{'='*60}")
        print(f"📥 IMPORTING DRAWINGS FROM TIFF")
        print(f"   Path: {input_path}")
 
        if not Path(input_path).exists():
            print(f"   ❌ File does not exist: {input_path}")
            return False
 
        # ── infer Z so polygons appear on the point cloud ─────────────────
        scene_z = _infer_import_scene_z(app)
        print(f"   📐 Using scene Z = {scene_z:.2f}")
 
        progress = QProgressDialog("Loading GeoTIFF...", "Cancel", 0, 100, app)
        progress.setWindowTitle("Import GeoTIFF")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(5)
        QCoreApplication.processEvents()
 
        with rasterio.open(input_path) as src:
            print(f"   📊 Raster: {src.width}x{src.height} @ {src.crs}")
            print(f"   📊 Bands: {src.count}")
 
            is_rgb = src.count >= 3
            if is_rgb:
                red   = src.read(1)
                green = src.read(2)
                blue  = src.read(3)
                image = (red.astype(np.float32) +
                         green.astype(np.float32) +
                         blue.astype(np.float32)) / 3
                image = image.astype(np.uint8)
            else:
                image = src.read(1)
                red = green = blue = None
 
            transform = src.transform
 
            non_zero = (image > 0).sum()
            if non_zero == 0:
                progress.close()
                print(f"   ⚠️ TIFF is empty")
                return False
 
            progress.setLabelText(f"Vectorizing {non_zero:,} pixels...")
            progress.setValue(10)
            QCoreApplication.processEvents()
 
            mask    = image > 0
            results = list(shapes(image, mask=mask, transform=transform, connectivity=8))
            print(f"   📦 Found {len(results):,} raw shapes")
 
        if progress.wasCanceled():
            progress.close()
            return False
 
        progress.setValue(20)
        QCoreApplication.processEvents()
 
        # ── filter small noise ────────────────────────────────────────────
        pixel_area = abs(transform[0] * transform[4])
 
        if len(results) > 100_000:
            min_area = pixel_area * 500
        elif len(results) > 10_000:
            min_area = pixel_area * 100
        else:
            min_area = pixel_area * 25
 
        progress.setLabelText(f"Filtering {len(results):,} shapes...")
        progress.setValue(30)
        QCoreApplication.processEvents()
 
        filtered_results = []
        for g, v in results:
            try:
                poly = shape(g)
                if poly.area < min_area:
                    continue
 
                color = (255, 255, 0)
                if is_rgb and red is not None:
                    try:
                        cx = poly.centroid
                        col = int((cx.x - transform[2]) / transform[0])
                        row = int((cx.y - transform[5]) / transform[4])
                        if 0 <= row < red.shape[0] and 0 <= col < red.shape[1]:
                            r, g_c, b = int(red[row, col]), int(green[row, col]), int(blue[row, col])
                            if r > 0 or g_c > 0 or b > 0:
                                color = (r, g_c, b)
                    except Exception:
                        pass
 
                filtered_results.append((g, v, poly, color))
            except Exception:
                continue
 
        skipped = len(results) - len(filtered_results)
        print(f"   ✅ Filtered: {len(filtered_results):,} valid shapes (removed {skipped:,})")
 
        if not filtered_results:
            progress.close()
            QMessageBox.warning(app, "No Valid Shapes",
                                f"All {len(results):,} shapes were too small.")
            return False
 
        if progress.wasCanceled():
            progress.close()
            return False
 
        MAX_SHAPES = 10_000
        if len(filtered_results) > MAX_SHAPES:
            filtered_results.sort(key=lambda x: x[2].area, reverse=True)
            filtered_results = filtered_results[:MAX_SHAPES]
 
        progress.setValue(40)
        QCoreApplication.processEvents()
 
        if not (hasattr(app, 'digitizer') and app.digitizer):
            progress.close()
            print(f"   ❌ No digitizer found")
            return False
 
        imported_count = 0
        failed_count   = 0
        total          = len(filtered_results)
 
        progress.setLabelText("Importing shapes...")
        progress.setMaximum(total)
 
        for idx, (geom, value, poly, color) in enumerate(filtered_results):
            if progress.wasCanceled():
                break
 
            if idx % 100 == 0:
                progress.setValue(idx)
                progress.setLabelText(f"Importing shapes: {idx:,}/{total:,}")
                QCoreApplication.processEvents()
 
            try:
                if poly.geom_type == 'Polygon':
                    coords = list(poly.exterior.coords)
                    if len(coords) > 100:
                        poly = poly.simplify(min_area * 0.2, preserve_topology=True)
                        coords = list(poly.exterior.coords)
 
                    drawing = {
                        'type': 'polygon',
                        # ✅ Use scene_z instead of hard-coded 0.0
                        'coordinates': [[float(x), float(y), float(scene_z)]
                                        for x, y in coords[:-1]],
                        'color': color,
                    }
                    if app.digitizer.add_drawing_from_data(drawing):
                        imported_count += 1
                    else:
                        failed_count += 1
 
                elif poly.geom_type == 'MultiPolygon':
                    for sub in poly.geoms:
                        if sub.area < min_area:
                            continue
                        coords = list(sub.exterior.coords)
                        if len(coords) > 100:
                            sub = sub.simplify(min_area * 0.2, preserve_topology=True)
                            coords = list(sub.exterior.coords)
                        drawing = {
                            'type': 'polygon',
                            'coordinates': [[float(x), float(y), float(scene_z)]
                                            for x, y in coords[:-1]],
                            'color': color,
                        }
                        if app.digitizer.add_drawing_from_data(drawing):
                            imported_count += 1
                        else:
                            failed_count += 1
 
            except Exception:
                failed_count += 1
                continue
 
        # ── final render + camera reset ───────────────────────────────────
        progress.setLabelText(f"Rendering {imported_count:,} drawings...")
        progress.setValue(total)
        QCoreApplication.processEvents()
 
        print(f"   🎨 Rendering {imported_count:,} drawings...")
 
        if hasattr(app, 'vtk_widget') and app.vtk_widget:
            rw = app.vtk_widget.GetRenderWindow()
 
            # ✅ Reset camera so the drawings are visible
            renderer = rw.GetRenderers().GetFirstRenderer()
            if renderer:
                renderer.ResetCamera()
                renderer.ResetCameraClippingRange()
 
            rw.Render()
 
        # Also call fit_view if available
        fit_view = getattr(app, 'fit_view', None)
        if callable(fit_view):
            try:
                fit_view()
            except Exception:
                pass
 
        progress.close()
 
        print(f"\n   ✅ Import Complete:")
        print(f"      Imported : {imported_count:,}")
        if failed_count:
            print(f"      Failed   : {failed_count:,}")
        print(f"{'='*60}\n")
 
        return imported_count > 0
 
    except ImportError as e:
        if 'progress' in locals():
            progress.close()
        print(f"   ❌ Missing library: {e}")
        print(f"   💡 Install: pip install rasterio shapely")
        return False
    except Exception as e:
        if 'progress' in locals():
            progress.close()
        print(f"   ❌ TIFF import failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def _geotiff_texture_pixel_budget(app, hard_cap: int = 16_000_000) -> int:
    """
    Cap a new GeoTIFF texture's pixel count by remaining GPU VRAM headroom,
    not just a fixed resolution. This machine's 4GB card (T1000, ~2.8GB safe
    budget) can end up with several full-resolution orthophoto/DTM/DSM
    textures resident in the same session - the fixed 16M-pixel cap alone
    doesn't account for that. Give raster textures a bounded slice of the
    budget and shrink it by what's already loaded, independent of the
    reentrancy bug fixed in import_geotiff_as_texture (confirmed by WER as
    the actual cause of the production access-violation crashes).
    """
    try:
        from gui.gpu_support import get_memory_budget_mb
        budget_mb = float(get_memory_budget_mb() or 0) or 2048.0
    except Exception:
        budget_mb = 2048.0

    # Point cloud + basemap tiles need the rest of the budget; give GeoTIFF
    # textures a fixed slice of it rather than letting them consume it all.
    texture_budget_mb = min(600.0, budget_mb * 0.2)

    already_used_mb = 0.0
    for actor in getattr(app, "geotiff_actors", []) or []:
        try:
            texture = actor.GetTexture()
            image = texture.GetInput() if texture is not None else None
            if image is None:
                continue
            dims = image.GetDimensions()
            scalars = image.GetPointData().GetScalars()
            comps = scalars.GetNumberOfComponents() if scalars is not None else 3
            already_used_mb += (dims[0] * dims[1] * comps) / (1024.0 * 1024.0)
        except Exception:
            pass

    remaining_mb = max(16.0, texture_budget_mb - already_used_mb)
    # Worst case 4 bytes/pixel (RGBA) - convert the remaining MB into a pixel count.
    budget_pixels = int(remaining_mb * 1024.0 * 1024.0 / 4.0)
    return max(1_000_000, min(hard_cap, budget_pixels))


def import_geotiff_as_texture(app, input_path: str, world_bounds=None,
                              gcp_corners=None) -> bool:
    """
    Import GeoTIFF as a textured surface overlay on the point cloud.
    Displays the actual RGB imagery, not vectorized shapes.

    world_bounds: optional (west, south, east, north) in scene coordinates.
        When given, the image is placed exactly at those bounds (manual
        georeferencing / "align to layer"). Overrides auto placement.
    gcp_corners: optional ((blx,bly),(brx,bry),(tlx,tly)) world corners for the
        image's bottom-left / bottom-right / top-left — used by GCP
        georeferencing. Supports rotation/scale/translation (affine). Highest
        priority placement.
    """

    # Re-entrancy guard. QCoreApplication.processEvents() below pumps the whole
    # Qt event queue, including a second pending drag-drop/import event - without
    # this guard that event re-enters this function while the first call is still
    # on the stack, giving two live QProgressDialogs mutating app.geotiff_actors
    # concurrently. Production crash dumps (WER: Qt6Widgets.dll, access violation,
    # same offset both times) match this exact reentrancy pattern, seen right
    # after back-to-back GeoTIFF imports in one session.
    if getattr(app, "_geotiff_import_in_progress", False):
        print("   ⚠️ A GeoTIFF import is already in progress; ignoring duplicate request")
        return False
    app._geotiff_import_in_progress = True

    try:
        import rasterio
        from rasterio.enums import Resampling
        import vtk
        from vtk.util import numpy_support
        from pathlib import Path
        from PySide6.QtWidgets import QProgressDialog
        from PySide6.QtCore import Qt, QCoreApplication
        import numpy as np
 
        print(f"\n{'='*60}")
        print(f"📥 IMPORTING GEOTIFF AS TEXTURED SURFACE")
        print(f"   Path: {input_path}")
 
        if not Path(input_path).exists():
            print(f"   ❌ File does not exist")
            return False
 
        progress = QProgressDialog("Loading GeoTIFF texture...", "Cancel", 0, 100, app)
        progress.setWindowTitle("Import GeoTIFF")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(10)
        QCoreApplication.processEvents()
 
        # ── Step 1: read raster ───────────────────────────────────────────
        with rasterio.open(input_path) as src:
            print(f"   📊 Raster: {src.width}x{src.height}")
            print(f"   📊 Bands: {src.count}")
            print(f"   🌍 CRS: {src.crs}")
 
            bounds = src.bounds
            src_width = src.width
            src_height = src.height
            # A raster is usable for placement only if it carries real geo info.
            try:
                is_georef = (src.crs is not None) and (not src.transform.is_identity)
            except Exception:
                is_georef = False

            # Establish the canvas CRS from this GeoTIFF's own embedded CRS when
            # nothing else has claimed it yet (ensure_canvas_crs = first trustworthy
            # dataset wins, same as LAZ/SNT). Without this, a project with ONLY a
            # georeferenced TIFF loaded never sets app.project_crs_epsg, so the
            # basemap plugin finds no canvas CRS and silently assumes Web Mercator
            # - misaligning its tiles relative to this raster's real-world position
            # even though the TIFF itself carries perfectly good coordinates.
            if is_georef:
                try:
                    from pyproj import CRS as _CRS
                    from gui.crs_manager import ensure_canvas_crs
                    ensure_canvas_crs(
                        app, _CRS.from_wkt(src.crs.to_wkt()),
                        source="GeoTIFF header", dataset=input_path,
                    )
                except Exception as _crs_err:
                    print(f"   ⚠️ Could not register GeoTIFF CRS with canvas: {_crs_err}")

            # Guard against OOM by limiting texture pixel count before NumPy->VTK conversion.
            max_texture_pixels = _geotiff_texture_pixel_budget(app)  # VRAM-aware, capped at ~4k x 4k
            total_pixels = src_width * src_height
            width = src_width
            height = src_height
            read_kwargs = {}
            if total_pixels > max_texture_pixels:
                scale = (max_texture_pixels / float(total_pixels)) ** 0.5
                width = max(1, int(src_width * scale))
                height = max(1, int(src_height * scale))
                print(
                    f"   Large raster detected ({src_width}x{src_height}); "
                    f"downsampling to {width}x{height} for memory safety"
                )
                read_kwargs = {
                    "out_shape": (height, width),
                    "resampling": Resampling.bilinear,
                }

            if src.count >= 3:
                rgb_data = src.read([1, 2, 3], **read_kwargs)
            else:
                # Float single-band GeoTIFFs are normally elevation products.
                # Rendering their metre values directly as uint8 clips nearly
                # every pixel to white (e.g. elevations around 580 m).  Apply
                # the same blue→cyan→green→yellow→red elevation stretch used by
                # the point-cloud Elevation display. Integer image rasters keep
                # the established grayscale path.
                gray_data = src.read(1, masked=True, **read_kwargs)
                raw_gray = np.asarray(
                    gray_data.filled(np.nan)
                    if np.ma.isMaskedArray(gray_data)
                    else gray_data,
                    dtype=np.float32,
                )
                valid_gray = np.isfinite(raw_gray)
                is_elevation = (
                    np.issubdtype(src.dtypes[0], np.floating)
                    or bool(src.tags().get("SURFACE_MODEL"))
                    or "elevation" in str(src.descriptions[0] or "").lower()
                )
                if is_elevation and np.any(valid_gray):
                    from gui.elevation_models import render_elevation_relief_rgba

                    pixel_x = abs(float(src.transform.a)) * src_width / max(1, width)
                    pixel_y = abs(float(src.transform.e)) * src_height / max(1, height)
                    pixel_size = max(1e-9, 0.5 * (pixel_x + pixel_y))
                    rgb_image = render_elevation_relief_rgba(
                        raw_gray,
                        valid_mask=valid_gray,
                        pixel_size=pixel_size,
                        color_ramp=getattr(app, "elevation_color_ramp", None),
                    )
                    rgb_data = np.moveaxis(rgb_image, -1, 0)
                    del rgb_image
                else:
                    raw_gray = np.nan_to_num(
                        raw_gray, nan=0.0, posinf=255.0, neginf=0.0
                    )
                    rgb_data = np.repeat(raw_gray[np.newaxis, :, :], 3, axis=0)
                del gray_data
                del raw_gray

        # Convert to uint8 (VTK_UNSIGNED_CHAR) in a bounded way.
        if rgb_data.dtype != np.uint8:
            if np.issubdtype(rgb_data.dtype, np.integer):
                info = np.iinfo(rgb_data.dtype)
                if info.min >= 0 and info.max <= 255:
                    rgb_data = rgb_data.astype(np.uint8, copy=False)
                else:
                    scale = 255.0 / max(1, int(info.max) - int(info.min))
                    rgb_data = ((rgb_data.astype(np.float32) - float(info.min)) * scale)
                    rgb_data = np.clip(rgb_data, 0.0, 255.0).astype(np.uint8)
            else:
                rgb_data = np.nan_to_num(rgb_data, nan=0.0, posinf=255.0, neginf=0.0)
                max_val = float(np.max(rgb_data)) if rgb_data.size else 0.0
                if max_val <= 1.0:
                    rgb_data = rgb_data * 255.0
                rgb_data = np.clip(rgb_data, 0.0, 255.0).astype(np.uint8)
 
        progress.setValue(30)
        QCoreApplication.processEvents()
 
        # ── Step 2: build VTK image using numpy (FAST) ───────────────────
        print(f"   🎨 Creating VTK texture (numpy path)...")
 
        # Stack to (H, W, components), flip rows so VTK origin is bottom-left.
        # Elevation products use RGBA so NoData remains transparent.
        rgb_array = np.moveaxis(rgb_data, 0, -1)
        rgb_array = np.ascontiguousarray(rgb_array[::-1, :, :])
        color_components = int(rgb_array.shape[2])
        rgb_flat = rgb_array.reshape(-1, color_components)
 
        # Convert to VTK scalar array in one shot
        vtk_colors = numpy_support.numpy_to_vtk(
            rgb_flat, deep=True, array_type=vtk.VTK_UNSIGNED_CHAR
        )
        vtk_colors.SetNumberOfComponents(color_components)
        vtk_colors.SetName("Colors")
 
        vtk_image = vtk.vtkImageData()
        vtk_image.SetDimensions(width, height, 1)
        vtk_image.GetPointData().SetScalars(vtk_colors)

        # VTK owns a deep copy now; free large temporary NumPy buffers immediately.
        del rgb_flat
        del rgb_array
        del rgb_data
 
        progress.setValue(55)
        QCoreApplication.processEvents()
 
        # ── Step 3: infer scene Z so the plane sits on the point cloud ───
        # In this 3D scene there is no 2D "layer order" — what you see on top is
        # whatever is physically closer to the camera. Imported 2D vector data
        # (shapefiles with no Z) is placed at scene_z by _coords_to_scene_z(),
        # so if the raster plane also sat at scene_z the two would be coplanar
        # and the opaque image would hide the lines. Drop the raster a hair
        # BELOW the vectors so lines/polygons always render on top — the 3D
        # equivalent of "drag the shapefile to the top of the layer list".
        scene_z = _infer_import_scene_z(app)

        # ── Choose XY placement ──────────────────────────────────────────────
        # If the raster is georeferenced, use its real-world bounds. If NOT
        # (no CRS / identity transform — common for ECW→TIF conversions), its
        # "bounds" are just pixel coordinates that would land far from the data.
        # In that case, fit it onto the current scene extent (preserving aspect)
        # so it actually appears under the point cloud / vectors.
        left, right, bottom, top = (bounds.left, bounds.right,
                                    bounds.bottom, bounds.top)

        # Reproject raster bounds to project CRS if different
        target_crs_code = getattr(app, "project_crs_epsg", None)
        if target_crs_code and src.crs is not None:
            try:
                from rasterio.warp import transform_bounds
                target_crs = f"EPSG:{target_crs_code}"
                if src.crs.to_string() != target_crs:
                    print(f"   🔄 Reprojecting raster bounds from {src.crs.to_string()} to project CRS {target_crs}")
                    left, bottom, right, top = transform_bounds(src.crs, target_crs, left, bottom, right, top)
            except Exception as e:
                print(f"   ⚠️ Could not reproject raster bounds: {e}")
        # If no explicit bounds were passed and the file itself isn't
        # georeferenced, try a sidecar world file (.tfw/.wld) — this is the
        # standard georeference carrier for drone orthos and gives a CORRECT,
        # GCP-derived placement (unlike the stretch-to-fit fallback).
        if world_bounds is None and not is_georef:
            wf_bounds = read_world_file_bounds(input_path, src_width, src_height)
            if wf_bounds is not None:
                world_bounds = wf_bounds

        # Track how the raster ended up placed so the layer manager can decide
        # whether it still needs fitting (e.g. a raster imported before any
        # vectors lands at pixel coords and must be re-fitted once data exists).
        placement = "georef" if is_georef else "pixel"
        if gcp_corners is not None and len(gcp_corners) == 3:
            placement = "gcp"

        if world_bounds is not None and len(world_bounds) == 4:
            # Explicit / world-file georeferencing: place exactly at W/S/E/N.
            w, s, e, n = (float(v) for v in world_bounds)
            left, right = min(w, e), max(w, e)
            bottom, top = min(s, n), max(s, n)
            placement = "worldfile"
            print(f"   📌 Georeferenced placement — "
                  f"X({left:.2f}→{right:.2f}) Y({bottom:.2f}→{top:.2f})")
        elif not is_georef:
            scene_xy = _infer_scene_xy_bounds(app)
            if scene_xy is not None:
                sxmin, sxmax, symin, symax = scene_xy
                sdx = max(1e-6, sxmax - sxmin)
                sdy = max(1e-6, symax - symin)
                # Preserve the image's pixel aspect ratio inside the scene bbox.
                aspect = src_width / float(max(1, src_height))
                fit_w, fit_h = sdx, sdx / aspect
                if fit_h > sdy:
                    fit_h, fit_w = sdy, sdy * aspect
                cx, cy = (sxmin + sxmax) / 2.0, (symin + symax) / 2.0
                left, right = cx - fit_w / 2.0, cx + fit_w / 2.0
                bottom, top = cy - fit_h / 2.0, cy + fit_h / 2.0
                placement = "fit"
                print(f"   ⚠️ Not georeferenced — fitted to scene extent "
                      f"X({left:.1f}→{right:.1f}) Y({bottom:.1f}→{top:.1f})")
            else:
                placement = "pixel"
                print("   ⚠️ Not georeferenced and no scene data to fit to; "
                      "placing at pixel coordinates")

        try:
            app._last_raster_placement = placement
        except Exception:
            pass

        if gcp_corners is not None and len(gcp_corners) == 3:
            (blx, bly), (brx, bry), (tlx, tly) = gcp_corners
            xy_extent = max(abs(brx - blx), abs(tly - bly), abs(bry - bly), 1.0)
        else:
            xy_extent = max(abs(right - left), abs(top - bottom))
        raster_z_offset = max(xy_extent * 0.001, 1e-3)  # ~0.1% of extent, scale-aware
        raster_z = scene_z - raster_z_offset
        print(f"   📐 Placing texture plane at Z = {raster_z:.3f} "
              f"(vectors at {scene_z:.3f}, offset {raster_z_offset:.3f})")

        plane = vtk.vtkPlaneSource()
        if gcp_corners is not None and len(gcp_corners) == 3:
            # GCP / affine placement: arbitrary parallelogram (rotation-capable).
            (blx, bly), (brx, bry), (tlx, tly) = gcp_corners
            plane.SetOrigin(blx, bly, raster_z)   # image bottom-left
            plane.SetPoint1(brx, bry, raster_z)   # image bottom-right
            plane.SetPoint2(tlx, tly, raster_z)   # image top-left
            print(f"   🎯 GCP placement: BL({blx:.2f},{bly:.2f}) "
                  f"BR({brx:.2f},{bry:.2f}) TL({tlx:.2f},{tly:.2f})")
        else:
            plane.SetOrigin(left,  bottom, raster_z)
            plane.SetPoint1(right, bottom, raster_z)
            plane.SetPoint2(left,  top,    raster_z)
        plane.SetResolution(1, 1)
        plane.Update()
 
        texture = vtk.vtkTexture()
        texture.SetInputData(vtk_image)
        texture.InterpolateOn()
        texture.Update()
 
        progress.setValue(70)
        QCoreApplication.processEvents()
 
        # ── Step 4: add actor ─────────────────────────────────────────────
        print(f"   🎭 Adding to scene...")
 
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(plane.GetOutputPort())
 
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.SetTexture(texture)
        actor.GetProperty().SetOpacity(1.0)
 
        if not (hasattr(app, 'vtk_widget') and app.vtk_widget):
            print(f"   ❌ No VTK widget found")
            progress.close()
            return False
 
        from gui.scene_render_pipeline import (
            ROLE_DATA, add_raster_actor, renderer_for_role,
        )
        renderer = add_raster_actor(app, actor)
        if renderer is None:
            print("   Raster renderer pipeline is unavailable")
            progress.close()
            return False
 
        existing_geotiff_actors = getattr(app, "geotiff_actors", [])
        if not isinstance(existing_geotiff_actors, list):
            existing_geotiff_actors = []

        # Drop stale refs for overlays that are no longer present in renderer.
        live_geotiff_actors = []
        for old_actor in existing_geotiff_actors:
            try:
                if renderer.HasViewProp(old_actor):
                    live_geotiff_actors.append(old_actor)
                    continue
            except Exception:
                pass
            try:
                old_texture = old_actor.GetTexture()
                if old_texture is not None:
                    old_texture.SetInputData(None)
                    old_actor.SetTexture(None)
            except Exception:
                pass
            try:
                old_mapper = old_actor.GetMapper()
                if old_mapper is not None:
                    old_mapper.SetInputData(None)
                    old_actor.SetMapper(None)
            except Exception:
                pass

        live_geotiff_actors.append(actor)
        app.geotiff_actors = live_geotiff_actors
 
        progress.setValue(85)
        QCoreApplication.processEvents()
 
        # ── Step 5: reset camera so the imported image is actually visible ─
        print(f"   📷 Resetting camera to show imported texture...")
        camera_renderer = renderer_for_role(app, ROLE_DATA)
        if camera_renderer is not None:
            # Move the interactive/main camera to the combined raster extent.
            # The raster camera mirrors this view with independent clipping.
            camera_renderer.ResetCamera(renderer.ComputeVisiblePropBounds())

        # Route through the app's throttled/mutex-guarded render (GPURenderManager),
        # not a raw GetRenderWindow().Render(). The interactor's own auto-render is
        # disabled app-wide specifically so every render funnels through there;
        # stay consistent with that rather than bypassing it.
        app.vtk_widget.render()
 
        # Also call the app's fit_view if available
        fit_view = getattr(app, 'fit_view', None)
        if callable(fit_view):
            try:
                fit_view()
            except Exception:
                pass
 
        progress.setValue(100)
        progress.close()
 
        print(f"   ✅ GeoTIFF texture added successfully")
        print(f"   📏 Coverage: X({left:.2f} → {right:.2f})  "
              f"Y({bottom:.2f} → {top:.2f})  Z={scene_z:.2f}")
        print(f"{'='*60}\n")
        return True
 
    except ImportError as e:
        if 'progress' in locals():
            progress.close()
        print(f"   ❌ Missing library: {e}")
        return False
    except Exception as e:
        if 'progress' in locals():
            progress.close()
        print(f"   ❌ GeoTIFF texture import failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        app._geotiff_import_in_progress = False
