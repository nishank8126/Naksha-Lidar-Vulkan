# -*- mode: python ; coding: utf-8 -*-

# ==============================================================================
#  naksha.spec  -  PyInstaller spec for Naksha Point Cloud Tool
#  Build:  pyinstaller --clean --noconfirm naksha.spec
# ==============================================================================

from pathlib import Path
import sys
import os
import importlib.util

from PyInstaller.utils.hooks import collect_all


# -- config --------------------------------------------------------------------

block_cipher = None

# Keep the terminal visible in installed builds for live operational diagnostics.
DEBUG_CONSOLE = True

project_root = Path(SPECPATH).resolve()

# site-packages of the active venv / interpreter
site_packages = Path(sys.executable).parent.parent / "Lib" / "site-packages"


# -- helpers -------------------------------------------------------------------

def module_exists(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def collect_optional(name):
    """Run collect_all() only when the module is installed; skip silently."""
    if not module_exists(name):
        print(f"[SKIP] optional module not installed: {name}")
        return [], [], []

    try:
        return collect_all(name)
    except Exception as exc:
        print(f"[WARN] collect_all({name}) raised: {exc}")
        return [], [], []


def dedup(seq):
    """Remove duplicates while preserving insertion order."""
    seen = set()
    out = []

    for item in seq:
        key = item if isinstance(item, str) else tuple(item)

        if key not in seen:
            seen.add(key)
            out.append(item)

    return out


def remove_link_time_artifacts(entries):
    """Exclude compiler/linker outputs that are not used by a frozen runtime."""
    excluded_suffixes = (".lib", ".exp")
    kept = []
    for entry in entries:
        source = str(entry[0]).lower()
        if source.endswith(excluded_suffixes):
            print(f"[SIZE] excluded non-runtime artifact: {entry[0]}")
            continue
        kept.append(entry)
    return kept


# ==============================================================================
#  DATAS - non-Python assets bundled into the dist folder
# ==============================================================================

added_datas = [
    (str(project_root / 'gui' / 'icons'),           'gui/icons'),
    (str(project_root / 'icons' / 'naksha.ico'),    'gui/icons'),
    (str(project_root / 'icons' / 'Nakshatech_Logo-black.jpg'), 'gui/icons'),
    (str(project_root / 'gui' / 'theme.qss'),        'gui'),
    (str(project_root / 'gui' / 'theme_light.qss'),  'gui'),
    (str(project_root / 'models'),                   'models'),
    (str(project_root / 'Advance_Model'),            'Advance_Model'),
    (str(project_root / 'naksha.crt'),               '.'),
    # .snt file-type registration helpers (called by post_install + app on launch)
    (str(project_root / 'post_install.py'),          '.'),
    (str(project_root / 'register_snt_filetype.py'), '.'),
    # snt_file.ico bundled at root so register_snt_filetype._get_icon_path() finds it
    # via the exe_dir / "_internal" / "gui" / "icons" search path at runtime.
    # gui/icons already covers this — entry kept for clarity.
]

# Any .mnu menu definition files in the project root
for mnu in project_root.glob("*.mnu"):
    added_datas.append((str(mnu), "."))


# open3d ships shader/resource files that must sit next to its DLLs
open3d_pkg = site_packages / "open3d"

if open3d_pkg.exists():
    for folder in ("resources", "ml", ""):
        src = open3d_pkg / folder if folder else open3d_pkg

        if src.is_dir():
            for f in src.rglob("*"):
                if f.is_file() and f.suffix.lower() not in (".pyd", ".dll", ".lib", ".exp"):
                    rel = f.parent.relative_to(site_packages)
                    added_datas.append((str(f), str(rel)))
else:
    print("[WARN] open3d package directory not found")


# ==============================================================================
#  BINARIES - .dll / .pyd that PyInstaller cannot discover automatically
# ==============================================================================

added_binaries = []


# -- local Cython accelerator --------------------------------------------------

classify_accel = project_root / "classify_accel.cp310-win_amd64.pyd"

if classify_accel.exists():
    added_binaries.append((str(classify_accel), "."))
else:
    print("[SKIP] classify_accel.cp310-win_amd64.pyd not found")


# -- snt_core wheel ------------------------------------------------------------

# The wheel must be installed in the active Python 3.10 x64 environment before
# running PyInstaller. Native .pyd files cannot be imported directly from a
# compressed .whl archive. The wheel itself is also bundled for traceability and
# optional repair/reinstall support inside the installer payload.
snt_core_wheel = project_root / "snt_core-1.3.1-cp310-cp310-win_amd64.whl"

if snt_core_wheel.exists():
    added_datas.append((str(snt_core_wheel), "wheels"))
    print(f"[OK] snt_core wheel payload: {snt_core_wheel.name}")
else:
    print(f"[WARN] snt_core wheel not found: {snt_core_wheel}")

snt_core_found = False
for pyd in site_packages.rglob("snt_core*.pyd"):
    rel = pyd.parent.relative_to(site_packages)
    added_binaries.append((str(pyd), str(rel)))
    print(f"[OK] snt_core binary: {pyd.name} -> {rel}")
    snt_core_found = True

# Some wheels install the extension under a package directory with a generic
# module filename. collect_all('snt_core') below handles package data, while this
# explicit scan catches every native dependency shipped beside it.
snt_core_pkg = site_packages / "snt_core"
if snt_core_pkg.exists():
    for native in list(snt_core_pkg.rglob("*.pyd")) + list(snt_core_pkg.rglob("*.dll")):
        rel = native.parent.relative_to(site_packages)
        added_binaries.append((str(native), str(rel)))
        print(f"[OK] snt_core native dependency: {native.name} -> {rel}")
        snt_core_found = True

if not snt_core_found:
    print("[ERROR] snt_core is not installed in the active environment.")
    print(f"        Install it first with: {sys.executable} -m pip install --force-reinstall \"{snt_core_wheel}\"")


# -- CSF / Cloth-Simulation-Filter ---------------------------------------------

csf_found = False

for candidate in (
    "CSF.cp310-win_amd64.pyd",
    "CSF.pyd",
    "_CSF.pyd",
    "_CSF.cp310-win_amd64.pyd",
):
    pyd_path = site_packages / candidate

    if pyd_path.exists():
        added_binaries.append((str(pyd_path), "."))
        print(f"[OK] CSF binary bundled: {pyd_path.name}")
        csf_found = True
        break

if not csf_found:
    print("[WARN] CSF .pyd not found - cloth-simulation-filter may not work in EXE")


# -- open3d --------------------------------------------------------------------

if open3d_pkg.exists():
    for dll in open3d_pkg.rglob("*.dll"):
        rel = dll.parent.relative_to(site_packages)
        added_binaries.append((str(dll), str(rel)))

    for pyd in open3d_pkg.rglob("*.pyd"):
        rel = pyd.parent.relative_to(site_packages)
        added_binaries.append((str(pyd), str(rel)))

    print("[OK] open3d DLLs and .pyds collected manually")


# -- jakteristics --------------------------------------------------------------

jakteristics_pkg = site_packages / "jakteristics"

if jakteristics_pkg.exists():
    for pyd in jakteristics_pkg.rglob("*.pyd"):
        rel = pyd.parent.relative_to(site_packages)
        added_binaries.append((str(pyd), str(rel)))

    for dll in jakteristics_pkg.rglob("*.dll"):
        rel = dll.parent.relative_to(site_packages)
        added_binaries.append((str(dll), str(rel)))

    print("[OK] jakteristics binaries collected manually")
else:
    print("[WARN] jakteristics package directory not found")


# -- triangle ------------------------------------------------------------------

triangle_pkg = site_packages / "triangle"

if triangle_pkg.exists():
    for pyd in triangle_pkg.rglob("*.pyd"):
        rel = pyd.parent.relative_to(site_packages)
        added_binaries.append((str(pyd), str(rel)))

    for dll in triangle_pkg.rglob("*.dll"):
        rel = dll.parent.relative_to(site_packages)
        added_binaries.append((str(dll), str(rel)))

    print("[OK] triangle binaries collected manually")
else:
    print("[WARN] triangle package directory not found")


# -- llvmlite ------------------------------------------------------------------

llvmlite_pkg = site_packages / "llvmlite"

if llvmlite_pkg.exists():
    for dll in llvmlite_pkg.rglob("*.dll"):
        rel = dll.parent.relative_to(site_packages)
        added_binaries.append((str(dll), str(rel)))

    for pyd in llvmlite_pkg.rglob("*.pyd"):
        rel = pyd.parent.relative_to(site_packages)
        added_binaries.append((str(pyd), str(rel)))

    print("[OK] llvmlite binaries collected for numba")
else:
    print("[WARN] llvmlite not found - numba JIT will not work in EXE")


# -- numba ---------------------------------------------------------------------

numba_pkg = site_packages / "numba"

if numba_pkg.exists():
    for dll in numba_pkg.rglob("*.dll"):
        rel = dll.parent.relative_to(site_packages)
        added_binaries.append((str(dll), str(rel)))

    for pyd in numba_pkg.rglob("*.pyd"):
        rel = pyd.parent.relative_to(site_packages)
        added_binaries.append((str(pyd), str(rel)))

    print("[OK] numba binaries collected manually")
else:
    print("[WARN] numba package directory not found")


# ==============================================================================
#  OPTIONAL MODULE COLLECTION
# ==============================================================================

OPTIONAL_MODULES = [
    # 3-D / rendering
    "open3d",
    "vtk",
    "pyvista",
    "pyvistaqt",

    # geospatial
    "laspy",
    "shapely",
    "pyproj",
    "geopandas",
    "fiona",
    "rasterio",

    # data
    "pandas",
    "matplotlib",
    "plotly",

    # dash + flask
    "dash",
    "flask",

    # AI
    "torch",
    "torchvision",
    "jakteristics",
    "snt_core",

    # optional accelerators
    "onnxruntime",
    "numba",
    "triangle",

    # system
    "GPUtil",
    "psutil",
]

for mod in OPTIONAL_MODULES:
    d, b, h = collect_optional(mod)
    added_datas += d
    added_binaries += b


# ==============================================================================
#  HIDDEN IMPORTS
# ==============================================================================

hidden_imports = [
    # -- License system --------------------------------------------------------
    "license_client",

    # tkinter is used by license_client.py activation popup.
    # DO NOT exclude tkinter.
    "tkinter",
    "tkinter.simpledialog",
    "tkinter.messagebox",
    "tkinter.filedialog",
    "tkinter.ttk",
    "tkinter.constants",
    "tkinter.commondialog",
    "_tkinter",

    # -- App GUI modules -------------------------------------------------------
    "gui",
    "gui.ai_dialog",
    "gui.ai_inference",
    "gui.app_window",
    "gui.backup_settings_dialog",
    "gui.crash_reporter",
    "gui.cross_section",
    "gui.cross_section.backup_settings_dialog",
    "gui.cross_section.cut_section_controller",
    "gui.cross_section.interactor_classify",
    "gui.cross_section.interactor_slice",
    "gui.cross_section.section_controller",
    "gui.dialogs",
    "gui.dialogs.load_pointcloud_dialog",
    "gui.digitize_tools",
    "gui.display_mode",
    "gui.dwg_attachment",
    "gui.dxf_attachment",
    "gui.global_shortcuts",
    "gui.icon_provider",
    "gui.lidar_classification_tools",
    "gui.menu_sidebar_system",
    "gui.shading_display",
    "gui.shortcut_manager",
    "gui.snt_attachment",
    "gui.unified_actor_manager",
    "gui.vector_export",
    "gui.undo_context_manager",

    # -- Scientific / numerical ------------------------------------------------
    "numpy",
    "scipy",
    "scipy.spatial",
    "scipy.spatial.transform",
    "scipy.spatial.ckdtree",
    "scipy.sparse",
    "scipy.sparse.csgraph",
    "scipy.linalg",

    # -- Plotting --------------------------------------------------------------
    "matplotlib",
    "matplotlib.backends.backend_agg",
    "matplotlib.backends.backend_qt5agg",

    # -- 3-D / Point cloud -----------------------------------------------------
    "open3d",
    "pyvista",
    "pyvistaqt",

    # -- Geospatial ------------------------------------------------------------
    "pyproj",
    "pyproj.transformer",
    "geopandas",
    "fiona",
    "fiona.ogrext",
    "rasterio",
    "rasterio.drivers",
    "rasterio._shim",
    "ezdxf",
    "shapely",
    "shapely.geometry",
    "shapely.ops",
    "laspy",
    "laspy.compression",

    # -- Data ------------------------------------------------------------------
    "pandas",
    "pandas.io.formats.style",

    # -- Qt / PySide6 ----------------------------------------------------------
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    "PySide6.QtDataVisualization",

    # -- System / monitoring ---------------------------------------------------
    "GPUtil",
    "psutil",

    # -- AI / SNT --------------------------------------------------------------
    "torch",
    "jakteristics",
    "snt_core",
    "CSF",

    # -- Misc runtime ----------------------------------------------------------
    "pkg_resources",
    "certifi",
    "charset_normalizer",
    "zstandard",
    "lazrs",

    # open3d.visualization.draw_plotly imports dash -> flask at module load time
    "dash",
    "dash.html",
    "dash.dcc",
    "flask",
    "flask.helpers",

    # pyparsing.testing imports unittest at module level
    "unittest",
    "pyparsing",
    "pyparsing.testing",

    # -- Accelerators ----------------------------------------------------------
    "llvmlite",
    "llvmlite.binding",
    "numba.core",
    "numba.core.types",
    "numba.np",
    "numba.np.ufunc",
    "triangle",
    "triangle.core",

    # -- .snt file-type registration -------------------------------------------
    # Bundled as data files; imported by post_install.check_file_association_on_launch
    "winreg",
    "ctypes",
]

# Add optional modules only when actually installed
for mod in ("onnxruntime", "numba", "triangle", "_CSF", "torchvision"):
    if module_exists(mod):
        hidden_imports.append(mod)
        print(f"[OK] optional hidden import added: {mod}")


# -- deduplicate ---------------------------------------------------------------

added_datas     = dedup(remove_link_time_artifacts(added_datas))
added_binaries  = dedup(remove_link_time_artifacts(added_binaries))
hidden_imports  = dedup(hidden_imports)


# -- icon ----------------------------------------------------------------------

app_icon = project_root / "icons" / "naksha.ico"


# ==============================================================================
#  ANALYSIS
# ==============================================================================

a = Analysis(
    [str(project_root / "main.py")],

    pathex=[
        str(project_root),
        str(site_packages),
    ],

    binaries=added_binaries,
    datas=added_datas,
    hiddenimports=hidden_imports,

    hookspath=[],
    hooksconfig={},

    runtime_hooks=[str(project_root / "runtime_hook_numba.py")],

    excludes=[
        # Competing Qt bindings
        "PyQt5",
        "PyQt6",

        # Notebook / dev stack
        # IMPORTANT: tkinter is NOT excluded because license_client.py needs it.
        "notebook",
        "jupyter",
        "IPython",

        # Test frameworks
        "_pytest",
        "pytest",

        # Heavy optional ML tools not needed at runtime
        "tensorboard",
        "trame",
        "triton",
    ],

    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)


pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)


exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NakshaAI-LiDAR",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=DEBUG_CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(app_icon) if app_icon.exists() else None,
    version=str(project_root / "version_info.txt"),
)


coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Naksha",
)
