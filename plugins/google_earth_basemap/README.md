# Naksha Basemap plugin — Esri + OpenStreetMap (both free) + optional Google

Version **1.1.6** teaches the basemap to read three more CRS sources that real-world SNT/DGN/LAZ deliveries actually carry, so the map lands on the correct survey site instead of the open ocean:

- **TerraScan `.prj` `ProjectionSystem=<EPSG>`** — the EPSG code TerraScan writes into the LiDAR project's `.prj` next to every SNT/DGN. Previously this file was rejected (it starts with `[TerraScan project]`).
- **LAZ/LAS GeoKey Directory VLR (record 34735)** — keys 3072 / 2048 give the EPSG directly when stored inline.
- **LAZ/LAS OGC WKT VLR (record 2112)** — the LAS 1.4 way to embed a full WKT projection definition inside the file header.
- A same-folder `.prj` fallback that mirrors the SNT loader's convention for deliveries where the `.prj` has a different stem than the SNT.
- An SNT → referenced-LAZ chain: when the SNT's own `.prj` lacks `ProjectionSystem`, the plugin reads the LAZ filenames listed in its TerraScan block list and extracts the CRS from the first LAZ that carries one.

Tested against the user's `S:\SoftWare\dng test` deliveries:

| Folder                | Source                            | EPSG      | Result |
| --------------------- | --------------------------------- | --------- | ------ |
| `1034` (Belgian allotment) | TerraScan `ProjectionSystem=31370` | EPSG:31370 (BD72 / Belgian Lambert 72) | ✅ fixed |
| `932` (Belgian N4 block)   | TerraScan `ProjectionSystem=3812` (also confirmed by LAZ WKT VLR `ETRS89 / Belgian Lambert 2008`) | EPSG:3812 | ✅ fixed |
| `1011`, `298 GRUPPIGNANO`, `471` | none — no ProjectionSystem, no GeoKey, no WKT VLR | — | ⚠️ see below |

Version **1.1.5** was the first cut: it only handled OGC WKT `.prj`. It correctly mapped your UTM 33N test case to lon=15.0, lat=40.65 (near Bari, Italy), but it couldn't read TerraScan `.prj` files or LAZ projection VLRs.

Version **1.1.5** fixes the basemap-locating-the-wrong-place bug for SNT/DGN attachments (and any data whose CRS the host hadn't yet declared):

- **SNT/DGN attachments now show their real-world location.** Previously, the basemap reprojected the SNT's local-UTM/state-plane coordinates as if they were already Web Mercator, which often dropped the map over the open ocean (UTM 33N (500000, 4500000) — Italian survey origin — was being mapped to roughly lon=4.5°, lat=37.4°, i.e. the Mediterranean Sea east of Algeria). Two complementary fixes:
  1. **Plugin side:** `_project_crs()` now auto-discovers the CRS from any loaded `.snt` / `.dgn` / `.laz` / `.las` / `.ply` path it can find on the app (including the `_naksha_snt_filename` tag that `snt_attachment.py` stamps onto every SNT actor), reads the adjacent OGC WKT `.prj`, rejects TerraScan block files, and caches the result back onto `app.project_crs_wkt`. So the plugin works correctly even when the host hasn't been updated.
  2. **Host side (`gui/snt_attachment.py`):** `MultiSNTAttachmentDialog._render_snt_in_vtk` now calls `_resolve_snt_crs_from_adjacent_prj` before the camera reset, sets `app.project_crs_wkt` / `app.crs` / `app.project_crs_epsg`, and force-refreshes the basemap plugin so the first paint is already at the correct location.

- **No more blink/flicker on zoom in/out.** Old tiles are kept on screen as lower-resolution placeholders (drawn underneath) until their higher-resolution replacements arrive — exactly like web maps. An on-disk tile cache makes re-viewed areas re-appear instantly, and rendering is coalesced to one paint per ~120 ms instead of one per tile.

Version **1.1.3** added a second free, no-key provider (**OpenStreetMap** street/roadmap) alongside the existing **Esri World Imagery** satellite source, and made basemap tiles **auto-refresh on every zoom and pan**. This mirrors the reference *Urban Surve Platform* project, which also pulls Esri + OSM tiles without any API key.

Version **1.1.0** updated the earlier Google-only plugin into a provider-based basemap plugin while keeping the same manifest name (`Google Earth Basemap`) so Naksha's current ZIP installer can replace the previous version instead of creating a duplicate plugin.

## Free providers — no API key required

### Esri World Imagery (satellite) — default
The plugin defaults to **Esri World Imagery** using the public ArcGIS World Imagery tiled MapServer endpoint. No Google account, Google API key, or Google billing setup is needed.

The plugin also requests the ArcGIS service metadata and displays the returned copyright/source text when available. If metadata cannot be retrieved, it displays `Esri | World Imagery` as a fallback. Public service access is still subject to Esri's service terms and operational limits; "no API key" does not mean unlimited/offline redistribution.

### OpenStreetMap (street / roadmap) — no API key
The **OpenStreetMap** provider pulls standard OSM slippy tiles (`https://tile.openstreetmap.org/{z}/{x}/{y}.png`) plus the `a/b/c` tile subdomains as fallbacks. No key is needed. Attribution is shown as `© OpenStreetMap contributors` per OSM's usage policy. OSM tiles are capped at zoom 19.

## Auto-refresh on zoom / pan

The basemap observes the VTK camera's `ModifiedEvent`, so tiles re-request automatically whenever the camera moves — whether you pan/zoom with the mouse **or** the host app changes the camera programmatically (its wheel/pan handlers). A transform signature (focal point + parallel scale + camera Z) prevents our own clipping-range resets from retriggering a refresh, so there is no render loop. The reference web project does the same thing with MapLibre's `map.on("moveend", refresh)`.

## Optional Google providers

Users who want Google can select:

- Google Satellite
- Google Roadmap
- Google Terrain

Those three options continue to use the **official Google Maps Platform Map Tiles API**. Google providers require a valid API key and billing-enabled Google Cloud project. The plugin does not scrape undocumented Google tile URLs.

`GOOGLE_MAPS_API_KEY` can be set in the environment instead of storing a key in QSettings.

## UI

Plugins ribbon → **Basemap** section:

- Provider combo box
- `Basemap` on/off
- `Refresh`
- `Go To`
- `Settings`

`Ctrl+Alt+G` toggles the active basemap.

## Rendering behavior

- Renders tiles directly inside the existing Naksha VTK main canvas.
- Uses the app's normal pan/zoom controls — no embedded browser or QtWebEngine.
- Works in 2D Top/Plan view and pauses outside that view.
- Reprojects tile geometry from WGS84/Web Mercator into the loaded project CRS with `pyproj`.
- Places the basemap below existing project actors and disables its VTK bounds contribution so it does not corrupt fit/zoom extents.
- Keeps only the visible viewport tile set in memory, plus a small set of lower-resolution placeholder tiles (parent zoom levels) that are retired automatically once their higher-resolution children load. An on-disk tile cache (`%LOCALAPPDATA%/NakshaAI/basemap_cache`, 7-day TTL) makes re-viewed areas re-appear instantly instead of flashing blank.
- If data is loaded without a known CRS, Go To falls back to Web Mercator (tiles will not align with unreferenced data) rather than silently doing nothing.

## Install / update

1. Open **Plugins → Plugins Manager → Install from ZIP**.
2. Select `naksha_basemap-1.1.6.zip`.
3. Your current PluginManager will unload the older `Google Earth Basemap` version if active, replace its plugin directory, and immediately load v1.1.6.
4. Open **Plugins** and pick a provider: **Esri World Imagery (No API Key)** for satellite or **OpenStreetMap (No API Key)** for street/roadmap.
5. Turn **Basemap** on. Tiles refresh automatically as you zoom and pan.

No modification to `app_window.py`, `plugin_manager.py`, or the plugin loader dialog is required.

## Dependencies

Uses libraries already expected by the host application:

- PySide6
- `PySide6.QtNetwork`
- VTK
- NumPy
- pyproj

For a PyInstaller build, `PySide6.QtNetwork` must exist in the host bundle because compiled Qt modules cannot be supplied as ordinary Python-only plugin files after the EXE is frozen.

## Files

- `manifest.json` — plugin metadata / entrypoint.
- `google_earth_basemap.py` — lifecycle, ribbon UI, providers, networking, CRS integration and VTK rendering.
- `tile_math.py` — provider-independent slippy-map/Web-Mercator helpers.


## v1.1.1 reliability fix
- Uses VTK-native in-memory JPEG/PNG decoding instead of optional Qt image-format plugins.
- Retries Esri World Imagery through both `server.arcgisonline.com` and `services.arcgisonline.com`.
- Emits the final failed tile URL/error to Naksha status/console for easier diagnosis.

## v1.1.3 — free street map + auto-refresh
- Added **OpenStreetMap** as a no-key street/roadmap provider (with `a/b/c` subdomain fallbacks).
- Provider picker now lists: Esri World Imagery, OpenStreetMap, Google Satellite, Google Roadmap, Google Terrain.
- Basemap tiles auto-refresh on every camera pan/zoom via the camera `ModifiedEvent` observer (covers programmatic zoom done by the host app), with a transform-signature guard to avoid render loops.
- Version bump 1.1.2 → 1.1.3.

## v1.1.4 — smooth zoom + working Go To
- **Anti-blink tile strategy (web-map pattern):** on a zoom/pan, previously shown tiles are kept on screen as lower-resolution placeholders until the matching higher-zoom tile lands. This build's `vtkActor` exposes no `SetRenderOrder`/`SetLayerNumber`, so ordering is done with a real Z separation — placeholders are pushed ~10% of the view height *behind* the active tiles (depth-only in the top/plan view, so no screen parallax), which reliably beats depth-buffer precision so the sharper tile always wins the depth test. Prevents the blank flash / pop-in.
- **On-disk tile cache** (`%LOCALAPPDATA%/NakshaAI/basemap_cache`, 7-day TTL) so re-viewed areas render instantly from cache instead of after a network round-trip.
- **Coalesced rendering:** tile arrivals trigger at most one `render()` per ~120 ms instead of one render per tile, removing per-tile flicker.
- **Go To fix:** no longer returns silently when a project has data but no CRS — it falls back to Web Mercator and shows a status note; and it auto-enables the basemap (in 2D Top view) so the moved camera is actually visible. Camera signature is re-captured after the move to avoid a double auto-refresh.
- **LAZ/LAS "map disappears" fix:** loading a point cloud used to make the basemap vanish. Two root causes, both fixed:
  1. `_compute_viewport_request` returned `None` (blanking the map) whenever a project had data but **no CRS**. It now falls back to Web Mercator (same as Go To) and shows a *"not aligned with the unreferenced loaded data"* status note, so tiles keep rendering.
  2. The basemap's background Z was derived from `renderer.ComputeVisiblePropBounds()`, which **includes the basemap tiles themselves**. Each refresh therefore pushed the basemap further below the data; once the host re-framed the point cloud the camera far clip no longer reached the basemap and it disappeared. Now `_choose_background_z` uses `_data_z_bounds`, which computes the Z extent from **all non-basemap actors only** (tile actors are excluded), so the basemap sits just below the point cloud (`zmin - max(1e-3, span*0.01)`) and **never drifts** across refreshes.
- Version bump 1.1.3 → 1.1.4.

## v1.1.5 — SNT/DGN attachments now show their real-world location
- **Symptom:** loading an SNT (with embedded LAZ/LAS) or a DGN made the basemap appear over the open ocean instead of at the actual site of the survey. Reprojection example (UTM 33N origin 500000, 4500000 — should be near Bari, Italy): pre-fix the basemap fell back to Web Mercator and mapped those local UTM coordinates to roughly lon=4.49°, lat=37.44° — Mediterranean Sea, east of Algeria. With the fix it correctly reprojects to lon=15.00°, lat=40.65°.
- **Root cause:** SNT/DGN entities are rendered into the VTK world in the SNT's local CRS (e.g., UTM), but `MultiSNTAttachmentDialog._render_snt_in_vtk` never set `app.project_crs_wkt` (only the standalone LAZ loader does). The basemap therefore treated the local coords as if they were already Web Mercator.
- **Fix 1 (plugin, `google_earth_basemap.py`):** `_project_crs()` now also calls `_discover_crs_from_app_files()`, which scans `self.app` for any loaded `.snt` / `.dgn` / `.laz` / `.las` / `.ply` file path (`app.loaded_file`, `app.snt_attachments`, etc., plus the `_naksha_snt_filename` actor tag) and reads the adjacent OGC WKT `.prj`. TerraScan block `.prj` files (start with `Block ...`) are rejected — they carry block geometry, not a CRS. The discovered CRS is cached back onto `app.project_crs_wkt` / `app.crs` / `app.project_crs_epsg` so future refreshes skip the scan.
- **Fix 2 (host, `gui/snt_attachment.py`):** new `MultiSNTAttachmentDialog._resolve_snt_crs_from_adjacent_prj(fpath)` reads the OGC WKT `.prj` next to the SNT, sets `self.app.project_crs_wkt` / `self.app.crs` / `self.app.project_crs_epsg`, and is called inside `_render_snt_in_vtk` *before* the camera reset so the basemap sees the correct CRS on the first paint. If a CRS was newly declared, the host also iterates `app.loaded_plugins` / `app.plugins` and calls each plugin's `refresh()` / `_schedule_refresh()` to force an immediate basemap re-fetch.
- Verified end-to-end: 7 CRS-discovery unit cases pass (WKT `.prj` parsed, TerraScan `.prj` rejected, no `.prj` → None, `.dgn` supported, actor-tag discovery → EPSG cached, second call cache hit, wrong-extension rejected) plus the UTM 33N reprojection test (UTM (500000, 4500000) → lon=15.0000, lat=40.6509, pre-fix result lon=4.4916, lat=37.4356).
- Version bump 1.1.4 → 1.1.5.

## v1.1.6 — read TerraScan `ProjectionSystem`, LAZ GeoKeys, LAZ WKT VLR
- **Symptom:** v1.1.5 only handled OGC WKT `.prj` files. Real-world SNT/DGN/LAZ deliveries commonly carry their CRS in **other** formats — TerraScan project files (`[TerraScan project]`) with a `ProjectionSystem=<EPSG>` line, LAS GeoKey Directory VLRs (record 34735), and LAS 1.4 OGC WKT VLRs (record 2112). Without reading these, the basemap still fell back to Web Mercator for those deliveries and showed the survey over open water.
- **Diagnosis on the user's `S:\SoftWare\dng test` set:** only 2 of the 5 folders (`1034`, `932`) carry machine-readable CRS at all; the other 3 (`1011`, `298 GRUPPIGNANO`, `471`) have no ProjectionSystem, no GeoKey, and no WKT VLR anywhere in the LAZ or in their TerraScan `.prj`. Nothing for any plugin to discover.
- **Fix (plugin, `google_earth_basemap.py`):**
  - `_read_adjacent_wkt_prj` now also recognises TerraScan project files and extracts `ProjectionSystem=<EPSG>`.
  - Added `_read_laz_crs(path)`: parses the LAZ/LAS header directly (the LAZ header is stored uncompressed) and reads the OGC WKT VLR (record 2112/2113) or the GeoKey Directory VLR (record 34735, keys 3072 / 2048).
  - Added `_snt_referenced_laz_paths(snt_path)`: when the SNT's own `.prj` is TerraScan without `ProjectionSystem`, read its `Block <name>` list and return the `.laz` / `.las` paths that actually exist on disk; the resolution chain then reads the CRS from the first LAZ that carries one.
  - Added `_resolve_crs_for_path` orchestrator and a same-folder `.prj` fallback (mirrors the SNT loader's convention for deliveries where the `.prj` has a different stem than the SNT, e.g. folder `471`'s `tscan_tiles.prj`).
- **Fix (host, `gui/snt_attachment.py`):** `_resolve_snt_crs_from_adjacent_prj` now mirrors the same chain (TerraScan `ProjectionSystem`, then SNT → referenced-LAZ VLR read via a new `_read_laz_projection_crs` helper), plus the same `.prj` fallback.
- **Verified end-to-end against all 5 user folders:**
  | Folder                | Source                                       | EPSG      | Status |
  | --------------------- | -------------------------------------------- | --------- | ------ |
  | `1034` (Belgian allotment) | TerraScan `ProjectionSystem=31370`         | EPSG:31370 (BD72 / Belgian Lambert 72) | ✅ on land |
  | `932` (Belgian N4 block)   | TerraScan `ProjectionSystem=3812` (LAZ WKT VLR confirms `ETRS89 / Belgian Lambert 2008`) | EPSG:3812 | ✅ on land |
  | `1011`, `298 GRUPPIGNANO`, `471` | none — no CRS metadata in any file | — | ⚠️ see below |
- **About the 3 unresolved folders:** `1011`, `298 GRUPPIGNANO`, and `471` genuinely have **no machine-readable CRS in any of their files** (no TerraScan `ProjectionSystem`, no GeoKey, no WKT VLR, no OGC `.prj`). The basemap cannot guess a location from nothing. To get those basemaps on the correct site, either:
  1. Place an OGC WKT `.prj` next to the SNT/DGN (the plugin and host will read it automatically), **or**
  2. Regenerate the LAZ/LAS with CRS metadata (GeoKeys or WKT VLR 2112), **or**
  3. Use the **Go To** dialog to enter the known lat/lon, **or**
  4. Set `app.project_crs_wkt` on the host programmatically.
- **REUSABLE LESSON:** always check for `ProjectionSystem=<EPSG>` in TerraScan `.prj` files, and parse LAZ projection VLRs directly (2112 = WKT, 34735 = GeoKeys) before assuming a CRS is missing. Also: differently-stemmed `.prj` files are a valid TerraScan delivery convention.
- Version bump 1.1.5 → 1.1.6.
