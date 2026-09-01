"""
dgn_to_dxf.py
=============
Standalone DGN -> DXF converter using MicroStation SDK bridge.
Executes read_dgn.exe silently and converts printed geometry to DXF entities.
"""

import sys
import subprocess
import os
import math
from pathlib import Path

# -- Security/License validation -----------------------------------------------
from nakshaapp_dgn_converter._snt_core import _parse_dgn_nm_levels, detect_scale_from_grid_labels, detect_uor_scale

# -- Import dgn_reader C extension for metadata symmetry -----------------------
_dgn = None
try:
    import dgn_reader.dgn_reader as _dgn
except ImportError:
    try:
        import dgn_reader as _dgn
    except ImportError:
        _dgn = None

if _dgn is not None and not hasattr(_dgn, "open"):
    _dgn = None

# -- AutoCAD ACI Color Mapping from DGN Palette --------------------------------
_DGN_TO_ACI = {
    0:  7,  # White
    1:  7,  # White
    2:  1,  # Red
    3:  2,  # Yellow
    4:  3,  # Green
    5:  4,  # Cyan
    6:  5,  # Blue
    7:  6,  # Magenta
    8:  7,  # White
    9:  8,  # Dark Gray
    10: 9,  # Light Gray
}

# Level color cycle for DGN V8 files where dgn_reader reports color=0
_LEVEL_COLOR_CYCLE = [1, 3, 4, 5, 6, 2]  # Red, Green, Cyan, Blue, Magenta, Yellow

# -- UOR Scale Auto-detection (matches dgn_to_snt.py) ---------------------------
def _legacy_score_scale_candidate(scale: float, samples: list) -> float:
    """Score how plausible a UOR scale is given raw coordinate samples.

    Projection-neutral: accepts any coordinate system where scaled values
    fall between 0.1 (sub-millimetre engineering) and 1e9 (continental grid).
    No regional bonuses — the SDK-reported value is the authoritative source;
    this function only distinguishes 'plausible' from 'clearly wrong'.
    """
    score = 0.0
    for i in range(0, len(samples), 2):
        x = abs(samples[i] / scale)
        y = abs(samples[i+1] / scale)
        if 0.1 <= x <= 1e9:
            score += 1.0
        else:
            score -= 4.0
        if 0.1 <= y <= 1e9:
            score += 1.0
        else:
            score -= 4.0
    return score

def _legacy_detect_uor_scale_unused(samples: list, reported_scale=None) -> float:
    if reported_scale is not None and reported_scale > 0:
        return reported_scale

    if len(samples) < 4:
        return 1.0

    # Legacy fallback only when the bridge does not provide model UOR.
    candidate_scales = [1000000.0, 100000.0, 10000.0, 1000.0, 100.0, 10.0, 1.0]
    best_scale = 1.0
    best_score = float("-inf")

    for scale in candidate_scales:
        score = _legacy_score_scale_candidate(scale, samples)
        if score > best_score:
            best_score = score
            best_scale = scale

    return best_scale

# -- DXF Writer Class ----------------------------------------------------------
class DXFWriter:
    def __init__(self):
        self.entities = []
        self.layers = set()
        self.layer_colors = {}

    def register_layer(self, layer_name, color):
        self.layers.add(layer_name)
        if layer_name not in self.layer_colors or color != 7:
            self.layer_colors[layer_name] = color

    def add_line(self, layer, color, x1, y1, x2, y2):
        self.register_layer(layer, color)
        self.entities.append((
            "LINE",
            [
                (8, layer),
                (62, color),
                (10, x1), (20, y1), (30, 0.0),
                (11, x2), (21, y2), (31, 0.0)
            ]
        ))

    def add_polyline(self, layer, color, verts, closed=False):
        self.register_layer(layer, color)
        codes = [
            (100, "AcDbEntity"),
            (8, layer),
            (62, color),
            (100, "AcDbPolyline"),
            (90, len(verts)),
            (70, 1 if closed else 0),
            (38, 0.0) # Elevation
        ]
        for v in verts:
            codes.append((10, v[0]))
            codes.append((20, v[1]))
        self.entities.append(("LWPOLYLINE", codes))

    def add_point(self, layer, color, x, y):
        self.register_layer(layer, color)
        self.entities.append((
            "POINT",
            [
                (8, layer),
                (62, color),
                (10, x), (20, y), (30, 0.0)
            ]
        ))

    def add_arc(self, layer, color, cx, cy, radius, start_deg, sweep_deg):
        self.register_layer(layer, color)
        end_deg = (start_deg + sweep_deg) % 360.0
        self.entities.append((
            "ARC",
            [
                (8, layer),
                (62, color),
                (10, cx), (20, cy), (30, 0.0),
                (40, radius),
                (50, start_deg),
                (51, end_deg)
            ]
        ))

    def add_circle(self, layer, color, cx, cy, radius):
        self.register_layer(layer, color)
        self.entities.append((
            "CIRCLE",
            [
                (8, layer),
                (62, color),
                (10, cx), (20, cy), (30, 0.0),
                (40, radius),
            ]
        ))

    def add_text(self, layer, color, x, y, content, height=2.5):
        self.register_layer(layer, color)
        # Sanitize text
        content = str(content).replace("\r", " ").replace("\n", " ").replace("^", " ")
        self.entities.append((
            "TEXT",
            [
                (8, layer),
                (62, color),
                (10, x), (20, y), (30, 0.0),
                (40, height),
                (1, content)
            ]
        ))

    def write(self, fpath):
        import time as _time
        fpath = Path(fpath)
        # On Windows the file may still be held by antivirus / Explorer preview.
        for _attempt in range(5):
            try:
                with open(fpath, "w", encoding="ascii", errors="replace") as f:
                    # 1. Header Section
                    f.write("  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1015\n  0\nENDSEC\n")
                    
                    # 2. Tables Section (declaring layers)
                    f.write("  0\nSECTION\n  2\nTABLES\n  0\nTABLE\n  2\nLAYER\n 70\n0\n")
                    for layer in sorted(self.layers):
                        color = self.layer_colors.get(layer, 7)
                        f.write(f"  0\nLAYER\n  2\n{layer}\n 70\n0\n 62\n{color}\n  6\nCONTINUOUS\n")
                    f.write("  0\nENDTAB\n  0\nENDSEC\n")
                    
                    # 3. Entities Section
                    f.write("  0\nSECTION\n  2\nENTITIES\n")
                    for etype, codes in self.entities:
                        f.write(f"  0\n{etype}\n")
                        for code, val in codes:
                            f.write(f"{code:>3}\n{val}\n")
                    f.write("  0\nENDSEC\n  0\nEOF\n")
                break
            except PermissionError:
                if _attempt < 4:
                    try:
                        fpath.unlink(missing_ok=True)
                    except OSError:
                        pass
                    _time.sleep(0.2)
                else:
                    raise

# -- Conversion Driver ---------------------------------------------------------
def convert_dgn_to_dxf(dgn_path: Path, dxf_path: Path):
    dgn_path = Path(dgn_path)
    dxf_path = Path(dxf_path)
    script_dir = Path(__file__).parent
    bridge_exe = script_dir / "bin" / "read_dgn.exe"
    
    bridge_success = False
    result = None
    
    if bridge_exe.exists():
        print(f"Executing bridge scan on: {dgn_path.name}")
        bridge_env = os.environ.copy()
        # See dgn_to_snt.py for why we force the bundled lib/ and pre-set
        # _USTN_BENTLEYROOT: standalone (non-bootstrapped) SDK loads can't
        # auto-resolve their own config root, and fall back to a compiled-in
        # "C:\Program Files (x86)\Bentley\Program\" default that doesn't
        # exist on customer machines -> fatal exit=2.
        _bundled_lib = bridge_exe.parent.parent / "lib"
        if "MICROSTATION_DIR" not in bridge_env and (_bundled_lib / "toolsubs.dll").exists():
            bridge_env["MICROSTATION_DIR"] = str(_bundled_lib)
        if "_USTN_BENTLEYROOT" not in bridge_env and (_bundled_lib / "Workspace" / "users" / "untitled.ucf").exists():
            bridge_env["_USTN_BENTLEYROOT"] = str(_bundled_lib) + "\\"
        try:
            _cflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
            result = subprocess.run(
                [str(bridge_exe), str(dgn_path)],
                capture_output=True, text=True, timeout=300,
                env=bridge_env,
                creationflags=_cflags,
            )
            if result.returncode == 0 and result.stdout:
                bridge_success = True
            else:
                print(f"Bridge exited with code {result.returncode if result else 'unknown'}")
                if result and result.stderr:
                    print(f"Stderr: {result.stderr}")
        except Exception as e:
            print(f"Bridge execution failed: {e}")

    uor_scales   = {}
    model_samples = {}
    raw_elements  = []
    level_names   = {}   # (model_idx, level_code) -> name (from SDK LEVEL records)
    _nm_lvl_names = _parse_dgn_nm_levels(dgn_path)  # level_id -> name from binary Nm stream

    def add_sample(model, x, y):
        if model not in model_samples:
            model_samples[model] = []
        if len(model_samples[model]) < 128:
            model_samples[model].extend([x, y])

    if bridge_success and result is not None:
        lines = result.stdout.splitlines()
        # -- Parse Stdout ----------------------------------------------------------
        for line in lines:
            parts = line.split()
            if not parts:
                continue
            cmd = parts[0]
            if cmd == "LEVEL" and len(parts) >= 4:
                try:
                    m    = int(parts[1])
                    code = int(parts[2])
                    name = " ".join(parts[3:]).strip()
                    if name:
                        level_names[(m, code)] = name
                except (ValueError, IndexError):
                    pass
                continue

            if cmd == "UOR" and len(parts) >= 3:
                try:
                    model = int(parts[1])
                    scale = float(parts[2])
                    uor_scales[model] = scale
                except (ValueError, IndexError):
                    pass
                continue

            elif cmd == "LINE" and len(parts) >= 9:
                try:
                    model = int(parts[1])
                    level = int(parts[2])
                    wt = int(parts[3])
                    col = int(parts[4])
                    x1 = float(parts[5])
                    y1 = float(parts[6])
                    x2 = float(parts[7])
                    y2 = float(parts[8])
                    add_sample(model, x1, y1)
                    raw_elements.append({
                        'type': 'LINE', 'model': model, 'level': level, 'color': col,
                        'geom': (x1, y1, x2, y2)
                    })
                except (ValueError, IndexError):
                    pass
            
            elif cmd == "POLY" and len(parts) >= 6:
                try:
                    model = int(parts[1])
                    level = int(parts[2])
                    n = int(parts[3])
                    wt = int(parts[4])
                    col = int(parts[5])
                    elem_type = None
                    coord_offset = 6
                    if len(parts) >= 7 + n * 2:
                        elem_type = int(parts[6])
                        if elem_type == 4:  # SDK LINE_STRING_ELM -> DGN_LINESTRING (3)
                            elem_type = 3
                        coord_offset = 7
                    elif len(parts) < 6 + n * 2:
                        continue

                    verts = []
                    for i in range(n):
                        x = float(parts[coord_offset + i*2])
                        y = float(parts[coord_offset + i*2 + 1])
                        verts.append((x, y))
                        if i < 5:
                            add_sample(model, x, y)
                    if verts:
                        if elem_type is None:
                            closed = len(verts) >= 3 and verts[0] == verts[-1]
                        else:
                            closed = elem_type in (6, 14)
                        raw_elements.append({
                            'type': 'POLY', 'model': model, 'level': level, 'color': col,
                            'geom': verts, 'closed': closed
                        })
                except (ValueError, IndexError):
                    pass
                    
            elif cmd == "POINT" and len(parts) >= 6:
                try:
                    model = int(parts[1])
                    level = int(parts[2])
                    n = int(parts[3])
                    wt = int(parts[4])
                    col = int(parts[5])
                    verts = []
                    for i in range(n):
                        x = float(parts[6 + i*2])
                        y = float(parts[7 + i*2])
                        verts.append((x, y))
                        if i < 5:
                            add_sample(model, x, y)
                    for v in verts:
                        raw_elements.append({
                            'type': 'POINT', 'model': model, 'level': level, 'color': col,
                            'geom': v
                        })
                except (ValueError, IndexError):
                    pass
                    
            elif cmd == "ARC" and len(parts) >= 10:
                try:
                    model = int(parts[1])
                    level = int(parts[2])
                    wt = int(parts[3])
                    col = int(parts[4])
                    cx = float(parts[5])
                    cy = float(parts[6])
                    r = float(parts[7])
                    sa = float(parts[8])
                    sw = float(parts[9])
                    add_sample(model, cx, cy)
                    
                    raw_elements.append({
                        'type': 'ARC', 'model': model, 'level': level, 'color': col,
                        'geom': (cx, cy, r, sa * 3.14159265 / 180.0, sw * 3.14159265 / 180.0)
                    })
                except (ValueError, IndexError):
                    pass
                    
            elif cmd == "TEXT" and len(parts) >= 6:
                try:
                    model = int(parts[1])
                    level = int(parts[2])
                    x = float(parts[3])
                    y = float(parts[4])
                    
                    has_height = False
                    height = 1.0
                    
                    # Check for 8-part bridge output format (includes uor_height and user_height)
                    # Format: TEXT model level x y uor_height user_height text
                    if len(parts) >= 8:
                        try:
                            uor_h = float(parts[5])
                            user_h = float(parts[6])
                            content = " ".join(parts[7:])
                            height = uor_h if uor_h > 0 else user_h
                            has_height = True
                        except ValueError:
                            pass
                    
                    # Fallback to older 7-part format: TEXT model level x y height text
                    if not has_height and len(parts) >= 7:
                        try:
                            height = float(parts[5])
                            content = " ".join(parts[6:])
                            has_height = True
                        except ValueError:
                            pass
                            
                    if not has_height:
                        content = " ".join(parts[5:])
                        height = 1.0
                        
                    add_sample(model, x, y)
                    raw_elements.append({
                        'type': 'TEXT', 'model': model, 'level': level, 'color': 0,
                        'geom': (x, y, content, height)
                    })
                except (ValueError, IndexError):
                    pass
                    
            elif cmd == "CELL" and len(parts) >= 5:
                # Skip generating redundant origin markers since the bridge already
                # recurses and outputs the cell's child components.
                try:
                    model = int(parts[1])
                    x, y = float(parts[3]), float(parts[4])
                    add_sample(model, x, y)
                except (ValueError, IndexError):
                    pass
                continue
    else:
        if _dgn is not None:
            print("Bridge unavailable or failed. Falling back to dgn_reader C extension...")
            try:
                dgn_file = _dgn.open(str(dgn_path))
                if dgn_file is not None:
                    if dgn_file.get_format() == "OLE_V8":
                        dgn_file.close()
                        raise RuntimeError("Bridge crashed or failed to read V8 DGN file (fallback dgn_reader does not support V8). Missing VC++ redistributable or corrupt file.")
                    # Pre-cache level names
                    n_levels = dgn_file.get_level_count()
                    for li in range(n_levels):
                        info = dgn_file.get_level(li)
                        code = int(info["number"])
                        name = str(info.get("name") or f"Level{code}")
                        level_names[(0, code)] = name
                        
                    n_models = dgn_file.get_model_count()
                    for mi in range(n_models):
                        elements = dgn_file.scan_model(mi, follow_attachments=False)
                        
                        # Expand cell components
                        expanded = []
                        for el in elements:
                            dgn_type = int(el.get("type", 0))
                            if dgn_type in (34, 36, 37, 96) and "cell_components" in el:
                                for child in el["cell_components"]:
                                    child_copy = dict(child)
                                    if not child_copy.get("level") and el.get("level"):
                                        child_copy["level"] = el["level"]
                                    expanded.append(child_copy)
                            else:
                                expanded.append(el)
                                
                        for el in expanded:
                            dgn_type = int(el.get("type", 0))
                            level = int(el.get("level", 0))
                            symb = el.get("symbology", {}) or {}
                            col = int(symb.get("color", 7))
                            geo = el.get("geometry_type")
                            
                            # Skip administrative / non-geometry types
                            if dgn_type in (1, 9, 10, 21, 22, 26, 27, 35, 36, 38, 94, 98, 99):
                                continue

                            if dgn_type in (2, 3) or geo in ("line", "linestring"):
                                verts = el.get("vertices") or []
                                clean_verts = [(float(v[0]), float(v[1])) for v in verts]
                                if len(clean_verts) == 2:
                                    raw_elements.append({
                                        'type': 'LINE', 'model': mi, 'level': level, 'color': col,
                                        'geom': (clean_verts[0][0], clean_verts[0][1], clean_verts[1][0], clean_verts[1][1])
                                    })
                                elif len(clean_verts) > 2:
                                    closed = dgn_type in (5, 6, 14, 67)
                                    raw_elements.append({
                                        'type': 'POLY', 'model': mi, 'level': level, 'color': col,
                                        'geom': clean_verts, 'closed': closed
                                    })
                            elif dgn_type == 15 or geo == "pointstring":
                                verts = el.get("vertices") or []
                                for v in verts:
                                    raw_elements.append({
                                        'type': 'POINT', 'model': mi, 'level': level, 'color': col,
                                        'geom': (float(v[0]), float(v[1]))
                                    })
                            elif dgn_type in (16, 17) or geo == "arc":
                                center = el.get("center") or (0.0, 0.0, 0.0)
                                r = float(el.get("radius", 0.0))
                                sa = float(el.get("start_angle", 0.0))
                                sw = float(el.get("sweep_angle", 0.0))
                                raw_elements.append({
                                    'type': 'ARC', 'model': mi, 'level': level, 'color': col,
                                    'geom': (float(center[0]), float(center[1]), r, sa, sw)
                                })
                            elif dgn_type == 18 or geo == "text":
                                origin = el.get("origin") or (0.0, 0.0, 0.0)
                                content = el.get("text", "")
                                height = float(el.get("height", 1.0))
                                raw_elements.append({
                                    'type': 'TEXT', 'model': mi, 'level': level, 'color': col,
                                    'geom': (float(origin[0]), float(origin[1]), content, height)
                                })
                    dgn_file.close()
            except Exception as e:
                print(f"Fallback to dgn_reader failed: {e}")
        else:
            raise RuntimeError("Neither bridge nor dgn_reader available for: " + str(dgn_path))

    # -- Resolve UOR Scales ----------------------------------------------------
    # Translate raw_elements to bridge-style format for detect_scale_from_grid_labels
    bridge_style_elements = []
    for el in raw_elements:
        if el['type'] == 'TEXT':
            geom = el['geom']
            bridge_style_elements.append({
                'model_idx': el['model'],
                'type': 18,
                'text': geom[2] if len(geom) > 2 else '',
                'origin': (geom[0], geom[1])
            })

    detected_scales = {}
    for model_idx, samples in model_samples.items():
        reported = uor_scales.get(model_idx)
        scale_val = detect_uor_scale(samples, reported)
        scale_val = detect_scale_from_grid_labels(bridge_style_elements, model_idx, scale_val)
        detected_scales[model_idx] = scale_val
        if reported is not None and reported > 0:
            if math.isclose(scale_val, reported):
                print(f"Model {model_idx}: using SDK UOR scale {scale_val:.12g}")
            else:
                print(f"Model {model_idx}: adjusted SDK UOR scale {reported:.12g} -> {scale_val:.12g} from coordinate analysis")
        else:
            print(f"Model {model_idx}: bridge did not report UOR; using fallback scale {detected_scales[model_idx]:.12g}")

    # -- Generate DXF Entities -------------------------------------------------
    writer = DXFWriter()
    
    # Pre-cache all level definitions from dgn_reader if available
    _dgn_reader_lvl_names = {}
    _dgn_reader_lvl_colors = {}
    if _dgn is not None:
        try:
            dgn_file = _dgn.open(str(dgn_path))
            if dgn_file is not None:
                n_levels = dgn_file.get_level_count()
                for li in range(n_levels):
                    info = dgn_file.get_level(li)
                    code = int(info["number"])
                    _dgn_reader_lvl_names[code] = str(info.get("name") or f"Level{code}")
                    _dgn_reader_lvl_colors[code] = int(info.get("color", 7))
                dgn_file.close()
        except Exception:
            pass

    def _is_generic(n: str) -> bool:
        return not n or (n.startswith("Level") and n[5:].isdigit())

    def get_resolved_layer_name(model_idx: int, lnum: int) -> str:
        # Priority 1: bridge real name (non-generic)
        name = level_names.get((model_idx, lnum))
        if _is_generic(name):
            # Priority 2: dgn_reader C extension names (only if non-generic)
            dgn_name = _dgn_reader_lvl_names.get(lnum)
            if dgn_name and not _is_generic(dgn_name):
                name = dgn_name
            else:
                # Priority 3: Nm binary stream fallback
                nm_name = _nm_lvl_names.get(lnum)
                if nm_name:
                    name = nm_name
        if not name:
            name = f"Level{lnum}"
        return name

    # Collect all unique layer names actually used by elements (matches SNT logic)
    _level_aci_colors: dict = {}  # (model_idx, level_code) -> ACI color
    used_layers = set()
    for el in raw_elements:
        model = el['model']
        level = el['level']
        rn = get_resolved_layer_name(model, level)
        used_layers.add((model, level, rn))

    # Seed _level_aci_colors from dgn_reader and bridge (for color lookup only)
    for code, name in _dgn_reader_lvl_names.items():
        color = _dgn_reader_lvl_colors.get(code, 7)
        aci_color = _DGN_TO_ACI.get(color, color) or 7
        _level_aci_colors[(0, code)] = aci_color
    for (model_idx, code), bname in level_names.items():
        if (model_idx, code) not in _level_aci_colors:
            _level_aci_colors[(model_idx, code)] = 7

    # Pass 1: derive per-level colors from elements that carry a bridge color
    for el in raw_elements:
        raw_col = el.get('color', 0)
        if raw_col:
            lvl_key = (el['model'], el['level'])
            cur = _level_aci_colors.get(lvl_key)
            if cur is None or cur == 7:
                _level_aci_colors[lvl_key] = _DGN_TO_ACI.get(raw_col, raw_col) or 7

    # For levels still at white, assign cycle colors (matches SNT logic)
    cycle_idx = 0
    for (mi, code) in sorted(_level_aci_colors.keys()):
        if _level_aci_colors[(mi, code)] == 7:
            _level_aci_colors[(mi, code)] = _LEVEL_COLOR_CYCLE[cycle_idx % len(_LEVEL_COLOR_CYCLE)]
            cycle_idx += 1

    # Register only layers that have entities (matches SNT — no phantom layers)
    for (mi, code), aci in _level_aci_colors.items():
        rn = get_resolved_layer_name(mi, code)
        if any(ln == rn for _, _, ln in used_layers):
            writer.register_layer(rn, aci)

    # Also register used layers that weren't in the color map
    for (mi, code, rn) in used_layers:
        if rn not in writer.layer_colors:
            writer.register_layer(rn, _level_aci_colors.get((mi, code), 7))

    # Generate DXF entities using resolved colors
    for el in raw_elements:
        model = el['model']
        scale = detected_scales.get(model, 1.0)
        level = el['level']
        layer = get_resolved_layer_name(model, level)
        # Use element color if non-zero; otherwise fall back to level color
        raw_col = el['color']
        if raw_col:
            color = _DGN_TO_ACI.get(raw_col, raw_col) or 7
        else:
            color = _level_aci_colors.get((model, level),
                       _level_aci_colors.get((0, level), 7))
        
        etype = el['type']
        geom = el['geom']
        
        if etype == 'LINE':
            x1, y1, x2, y2 = geom
            writer.add_line(layer, color, x1 / scale, y1 / scale, x2 / scale, y2 / scale)
            
        elif etype == 'POLY':
            scaled_verts = [(v[0] / scale, v[1] / scale) for v in geom]
            closed = el.get('closed', False)
            if closed and len(scaled_verts) >= 2 and scaled_verts[0] == scaled_verts[-1]:
                scaled_verts = scaled_verts[:-1]
            writer.add_polyline(layer, color, scaled_verts, closed=closed)
            
        elif etype == 'POINT':
            x, y = geom
            writer.add_point(layer, color, x / scale, y / scale)
            
        elif etype == 'ARC':
            cx, cy, r, sa, sw = geom
            # Full circles (sweep = 2π) → DXF CIRCLE entity
            if abs(abs(sw) - 2.0 * math.pi) < 0.0017:
                writer.add_circle(layer, color, cx / scale, cy / scale, r / scale)
            else:
                sa_deg = sa * 180.0 / math.pi
                sw_deg = sw * 180.0 / math.pi
                writer.add_arc(layer, color, cx / scale, cy / scale, r / scale, sa_deg, sw_deg)
            
        elif etype == 'TEXT':
            x, y, content, height = geom
            writer.add_text(layer, color, x / scale, y / scale, content, height=height / scale)

    writer.write(dxf_path)
    print(f"Successfully wrote {len(writer.entities)} DXF entities to {dxf_path}")

# -- Command Line Interface ----------------------------------------------------
def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print("Usage: dgn-to-dxf <input.dgn> [output.dxf]")
        sys.exit(1)

    in_dgn = Path(sys.argv[1])
    out_dxf = Path(sys.argv[2]) if len(sys.argv) >= 3 else in_dgn.with_suffix(".dxf")

    try:
        convert_dgn_to_dxf(in_dgn, out_dxf)
        print(f"[OK] DXF Conversion complete!")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
