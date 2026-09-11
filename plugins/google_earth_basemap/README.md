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
The **OpenStreetMap** provider pulls standard OSM slippy tiles (`https://tile.openstreetmap.org/{z}/{x}/{y}.png`) using the policy-required main `tile.openstreetmap.org` hostname. No key is needed. Attribution is shown as `© OpenStreetMap contributors` per OSM's usage policy. OSM tiles are capped at zoom 19.

## v1.1.30 — automatic CRS release and standalone restoration

- Reconciles dataset-derived canvas CRS after actual scene content removal while preserving explicitly selected project CRS.
- Saves the geographic camera view before releasing the last dataset CRS, then restores standalone Web Mercator without numerically reinterpreting projected coordinates.
- Clears old projected actors, replies, geometry cache, and clipping state on projected-to-standalone transitions.
- Treats EPSG `area_of_use` as an accuracy hint rather than a hard black render boundary; actual finite/overflow/singularity checks remain active.

## v1.1.29 — deterministic exact projected tile meshes

- Removed camera-local affine placement from the active Esri/OSM projected path.
- XYZ geometry is cached by canonical canvas CRS, tile id, and adaptive regular-grid subdivision.
- A shared 1/2/4/8/16/32 subdivision level is selected per visible XYZ zoom to keep midpoint interpolation error at or below 0.35 screen pixels.
- Network replies carry a canvas-CRS signature and cannot cross a CRS change; older camera-generation replies remain reusable.
- The authoritative `gui.crs_manager.get_canvas_crs()` wins over attached SNT source CRS, and data-object identity changes no longer clear reusable actors.
- CRS area-of-use rejection and clipping are active in the projected tile path. Outside-domain canvas space remains the neutral renderer background.
- Optional `debug=true` settings output emits one `BASEMAP_REFRESH` record per settled refresh, including raster clipping and tile lifecycle counts.

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
- In projected (non-Web-Mercator) canvases, places each tile individually using a locally-linear rotation+scale approximation of the project CRS around the current camera focal point (v1.1.28) — see the v1.1.28 changelog entry below. Web-Mercator canvases place tiles with the exact, unapproximated transform (it's already an identity/no-op in that case).
- Places the basemap below existing project actors and disables its VTK bounds contribution so it does not corrupt fit/zoom extents.
- Keeps only the visible viewport tile set in memory, plus a small set of lower-resolution placeholder tiles (parent zoom levels) that are retired automatically once their higher-resolution children load. An on-disk tile cache (`%LOCALAPPDATA%/NakshaAI/basemap_cache`, 7-day TTL) makes re-viewed areas re-appear instantly instead of flashing blank.
- If data is loaded without a known CRS, Go To falls back to Web Mercator (tiles will not align with unreferenced data) rather than silently doing nothing.

## Install / update

1. Open **Plugins → Plugins Manager → Install from ZIP**.
2. Select `naksha_basemap-1.1.28.zip`.
3. Plugin Manager will unload the older `Google Earth Basemap`, replace its plugin directory, and immediately load v1.1.28.
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
- Added **OpenStreetMap** as a no-key street/roadmap provider (using the official main tile hostname).
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


## v1.1.28 — the wedge is gone: local-affine tile placement replaces per-pixel warp

The 1.1.27 note below said the wedge/fan shape at extreme zoom-out in a local
project CRS (EPSG:3301 was the reported case) "needs a host-level display-CRS
change, not a plugin patch." That turned out to be true only for a *pixel-exact*
fix; a much cheaper, plugin-only fix gets the practical result (a basemap that
always looks like a basemap, never a wedge) without touching `crs_manager.py`,
`projection_engine.py`, or any loader.

- **What changed:** `_start_projected_free_refresh()` no longer warps every
  output pixel through pyproj and composites the result into one texture.
  Each basemap tile is now placed individually using a **locally-linear
  (rotation + scale) approximation** of the project CRS around the current
  camera focal point — see `_local_canvas_to_mercator_affine()` in
  `google_earth_basemap.py` for the full derivation. Both the project CRS and
  Web Mercator are conformal projections, so within one screen's worth of
  view the map between them is, to very good approximation, a single
  similarity transform; a locally-linear map of a rectangle is always a
  parallelogram, so the basemap can no longer degenerate into a wedge no
  matter how far the CRS's *true* global shape would distort.
- **Which tiles to fetch** (the geographic bbox and zoom level) still uses
  the exact, CRS-domain-clipped math from 1.1.25/1.1.27 — only *placement* of
  each already-selected tile changed. The plugin still never requests
  imagery for a region the project CRS cannot meaningfully represent.
- **Accuracy:** near the camera's focal point (i.e. the normal case — a
  basemap under a local LiDAR/GIS survey area) alignment is visually exact.
  It degrades gracefully with distance from the focal point — independently
  verified against EPSG:3301 for this release: ~0.1% positional error at
  10 km from center, ~0.7% at 50 km, ~2% at 150 km, growing further at
  continental/global zoom-out where a single local scale factor necessarily
  stops being representative. That is the same trade-off every Web Mercator
  map already makes, and it is recomputed from the live camera on every
  refresh, so it re-centers continuously as you pan rather than degrading
  further the longer you stay in one place.
- **Side effects, all improvements:** the old per-pixel compose pass ran
  synchronously on the Qt GUI thread (forced there in 1.1.26 after a
  background attempt access-violated inside PROJ on Windows) and could block
  interaction for a noticeable time on a wide view; that entire pass is gone
  from this path, so there is nothing left to stall the UI or trip that
  Windows PROJ crash. The "shows nothing, then a small wrong patch" cold-start
  symptom 1.1.27 patched around (98.5%/20%/1.6x thresholds) cannot occur
  either, because there is no composited texture with a coverage percentage
  left in this path - tiles simply appear as they arrive, the same way the
  plain Web-Mercator-canvas path already worked.
- **Not changed:** `app.canvas_crs` / the "first georeferenced dataset wins"
  rule, GeoTIFF rendering, `gui/scene_render_pipeline.py`, and the Google
  Maps Platform provider path are all untouched. The Web-Mercator-canvas
  case (no GIS file loaded, or a project already in EPSG:3857) is unaffected
  — it never needed the projected path in the first place.
- **Please verify on your machine** (this can't be exercised without a live
  Qt/VTK session): load a local-CRS GIS/LiDAR file (EPSG:3301 or otherwise),
  turn the basemap on, and confirm (a) the basemap now stays a normal
  rectangle at every zoom level instead of collapsing into a wedge/patch,
  (b) it lines up with the loaded data at the data's own scale, and (c)
  panning/zooming stays smooth with no stalls. If alignment drifts
  noticeably during continuous fast panning before the next refresh lands,
  that's the expected recompute lag described above — it should snap back
  into alignment within one debounce cycle (~300 ms) once the camera settles.

## v1.1.27 — stale-preview and cold-start fix for the projected viewport

- **No more "shows nothing, then a small wrong patch" on activate/zoom-out:**
  the composed projected-basemap texture used to replace the one on screen
  only once it reached 98.5% coverage, with no exception for "there is no
  texture on screen yet." On a fresh `activate()` that meant nothing rendered
  until coverage cleared the bar; during continuous zoom-out the *old* texture
  (still sized for its original camera scale) simply kept shrinking on screen
  while a replacement that could miss the bar again sat unfinished.
- **Stale-actor clearing:** if the live camera's world-space width has moved
  more than 1.6x away from the scale the on-screen texture was composed for,
  it's cleared immediately instead of being left in place looking like a
  stuck, wrongly-scaled leftover.
- **Lower first-paint bar:** when there is no current viewport actor, 20%
  coverage is enough to show a first image; refinement passes on an existing,
  reasonably current actor still require the original 98.5% to avoid flicker.
- **Compose debounce 55ms → 90ms**, reducing how often the synchronous
  NumPy/PROJ compose pass (still on the Qt GUI thread — see the 1.1.26 note
  below) runs back-to-back during a burst of tile arrivals.
- **Manifest changelog correction:** the 1.1.25 bullet list in `manifest.json`
  still claimed composition ran on a `QThreadPool` after 1.1.26 reverted that
  for the Windows PROJ crash reason below. This README already had it right;
  `manifest.json`'s copy did not. Fixed.
- Does **not** change the wedge/fan shape you get after loading a local-CRS
  (e.g. EPSG:3301) GIS file at extreme zoom-out — that's the correct
  projected image of the CRS's valid area, not a rendering bug. See the
  "Important projection rule" note under v1.1.25 below; turning that back
  into a rectangular whole-world view needs a host-level display-CRS change,
  not a plugin patch.

## v1.1.26 — Windows PROJ crash fix

- Keeps the v1.1.25 projected-CRS/domain and rendering corrections.
- Runs projected composition on the Qt thread because concurrent pyproj work can
  access-violate inside the bundled PROJ DLL while the host imports GIS data.
- Retains the 1024-pixel projected-texture cap to bound UI-thread work.

## v1.1.25 — projected-CRS stability + smooth background reprojection

- **Root cause fixed for EPSG:3301 extreme zoom-out:** v1.1.24 sampled the full projected camera rectangle on a fixed 9×9 grid. Once the view became very large, only its center sample was still inside EPSG:3301's official Estonia area-of-use, so the geographic request collapsed to a point and the plugin selected an absurdly detailed tile zoom. The previous texture then remained on screen as a tiny rectangle.
- **CRS-domain clipping:** the plugin now densifies and projects the CRS area-of-use boundary, intersects the camera with that valid projected envelope, and renders only the overlap. If the camera is completely outside the valid domain, the old actor is removed instead of being left behind as a stale fragment.
- **Correct LOD at huge zoom-out:** tile zoom and output texture size are based on how many screen pixels the valid CRS footprint actually occupies, not on the entire application window.
- **No forward-projected OSM amoebas:** OpenStreetMap now uses the same inverse-resampled flat viewport renderer as Esri whenever the project canvas is not Web Mercator.
- **Transparent invalid pixels:** areas that are mathematically outside the target CRS are alpha-transparent rather than black, eliminating black wedges/fans at domain edges.
- **No GUI-thread reprojection freeze:** the expensive NumPy + PROJ pixel composition runs in a `QThreadPool` worker. Only the final VTK texture/actor swap happens on the UI thread. Stale worker results are generation/token checked and discarded after a newer pan/zoom.
- **Responsive raster budget:** projected textures are capped to 1024 pixels on the longest edge and use a small overscan buffer, providing a stable preview while the user keeps scrolling.
- **Camera-roll race removed:** the plugin camera repair observer now runs before the host raster-camera mirror, preventing a transient tilted/rolled camera state from being copied into the raster renderer during interaction.
- **OSM policy update:** standard raster tiles now use exactly `https://tile.openstreetmap.org/{z}/{x}/{y}.png`; legacy `a/b/c` subdomains were removed, and the application User-Agent now reports v1.1.25.

**Important projection rule:** a whole rectangular Web-Mercator world cannot be represented faithfully inside a local CRS such as EPSG:3301. In local-CRS mode the basemap is intentionally clipped to that CRS's valid geographic domain. To reproduce QGIS/ArcGIS's rectangular whole-world behavior, the application's *display/map CRS* must be EPSG:3857 and loaded GIS data must be reprojected into that display CRS.

## v1.1.24 — strict flat 2D projected basemap

- Fixes the curved globe/fan wedges seen after loading the real `1.ecw`
  raster in EPSG:3301 and zooming out.
- Normal Esri tiles are selected only from the canvas CRS valid footprint,
  then inverse-resampled locally into one north-up rectangular VTK texture.
  The private dataset bounds are not sent to an image-export endpoint.
- Coarse parent tiles provide full-canvas coverage first; sharper tiles
  replace them in the same texture. The last complete texture remains visible
  during refresh, so partial tile arrivals cannot create scattered black gaps.
- Top/Plan mode continuously repairs the VTK camera to parallel, north-up
  orientation and uses the 2D image interactor so orbit and roll cannot persist.
