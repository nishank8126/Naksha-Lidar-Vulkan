# GDB Universal Reader Patch Notes

This patch set improves the Naksha GDB reader toward ArcGIS/QGIS-style File Geodatabase compatibility.

## Files changed

- `reader.py`
- `engine.py`
- `features_panel.py`
- `domains_dialog.py`
- `import_panel.py`
- `gdb_picker.py` is included unchanged for completeness.

## Main fixes

1. Broader input support
   - Accepts normal `.gdb` folders.
   - Accepts `.gdb.zip` for read-only loading.
   - Accepts direct `.gdbtable` files for read-only loading.
   - Uses `gdal.OpenEx()` with explicit OpenFileGDB/FileGDB fallback instead of relying only on `ogr.Open()`.

2. Safer update behavior
   - Blocks update attempts on `.gdb.zip` and direct `.gdbtable` sources.
   - Logs `.lock` files before update open, so failures are easier to understand.

3. Better complex geometry reading
   - Handles `Triangle`, `TIN`, `PolyhedralSurface`, `MultiSurface`, `CurvePolygon`, `CompoundCurve`, and `GeometryCollection` recursion.
   - Linearizes curve geometries when GDAL exposes `GetLinearGeometry()`.
   - Prevents multipart line features from being joined into one false continuous line.
   - Preserves real vertex Z values for 3D polygons where possible instead of flattening everything to median Z.

4. Better feature writing
   - `_to_ogr_geometry()` now respects Point, LineString, and Polygon hints.
   - Fixes point/line creation from the Create Features panel.
   - Accepts lowercase UI attribute keys and maps them back to real field names.
   - Adds safer value coercion for integer, big integer, floating, string, GUID/XML/date-style fields.

5. Better schema reading
   - Reads field defaults from `GPFieldInfoEx/DefaultValue`.
   - Reads subtype-specific defaults from `SubtypeFieldInfo/DefaultValue`.
   - Stores layer `FeatureType`, `HasZ`, and `HasM` from GDB XML.
   - Adds schema support for ArcGIS Pro newer field types: `BigInteger`, `DateOnly`, `TimeOnly`, `TimestampOffset`.
   - Improves internal/system-table detection for attachments and spatial reference tables.

6. UI/domain support
   - Create Features panel now validates required fields before writing.
   - Subtype selection applies subtype-specific default values.
   - Domains dialog includes newer field types.

## Still not identical to ArcGIS Pro

These are real platform/data limitations, not simple Python bugs:

- ArcGIS symbology, labeling, annotation placement, topology/network behavior, parcel fabric behavior, relationship-class editing, and attachment viewing need separate application-level modules.
- Some compressed FileGDB formats and sparse 64-bit ObjectID layouts are limited by GDAL driver capability.
- A FileGDB raster reader/display path is not included in this vector VTK pipeline.
- Huge GDBs should still be tested with spatial filters / progressive loading later to avoid UI freezes.

## Recommended test matrix

Test these GDBs after applying the patch:

1. Simple 2D points, lines, polygons.
2. MultiPoint, MultiLineString, MultiPolygon.
3. Feature dataset groups.
4. Subtype + coded-value domain + range-domain layers.
5. Layers with required fields and default values.
6. 3D polygon Z / multipatch / TIN style layers.
7. Curve / circular arc / compound curve data.
8. `.gdb.zip` read-only load.
9. Direct `.gdbtable` read-only load.
10. GDB with attachment tables and relationship classes.
11. ArcGIS Pro 3.2+ field types: BigInteger, DateOnly, TimeOnly, TimestampOffset.
