"""
DXF Pipeline - DGN to DXF conversion module.
"""

from nakshaapp_dgn_converter.security import DgnLicenseManager
import sys
import argparse
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    _APP_ROOT = Path(getattr(sys, "_MEIPASS", _HERE.parent))
    _REPO_ROOT = _APP_ROOT
else:
    _APP_ROOT = _HERE.parent
    _REPO_ROOT = _APP_ROOT  # dgn_to_dxf/ lives alongside nakshaapp_dgn_converter/

for _path in (_REPO_ROOT, _REPO_ROOT / "dgn_to_dxf"):
    _p = str(_path)
    if _path.exists() and _p not in sys.path:
        sys.path.insert(0, _p)


def _guarded_convert_dxf(dgn_path, dxf_path=None, **kwargs):
    """
    Python API for GUI use. Raises RuntimeError on processing failure.
    No external terminology exposed.
    """
    _m = None
    try:
        _m = DgnLicenseManager()
        if not _m.validate_conversion():
            raise RuntimeError(
                "Processing capacity reached. Please contact support to continue."
            )
    except RuntimeError:
        raise
    except Exception:
        pass

    try:
        from nakshaapp_dgn_converter._dxf_core import convert_dgn_to_dxf as _real_dxf
    except Exception as exc:
        raise RuntimeError(
            f"DXF conversion engine not available: {type(exc).__name__}: {exc}"
        ) from exc
    _real_dxf(dgn_path, dxf_path, **kwargs)

    try:
        if _m:
            _m.record_conversion()
    except Exception:
        pass


def main():
    mgr = DgnLicenseManager()

    ap = argparse.ArgumentParser(description="DGN -> DXF converter")
    ap.add_argument("dgn_path", help="Input .dgn file")
    ap.add_argument("dxf_path", nargs="?", help="Output .dxf (default: <dgn_stem>.dxf)")
    ap.add_argument("--activate-key", default="", help="Activate with key")

    args = ap.parse_args()

    if args.activate_key:
        success, msg = mgr.activate_key(args.activate_key)
        print(f"\n{'='*60}")
        print(f"  {msg}")
        print(f"{'='*60}\n")
        sys.exit(0 if success else 1)

    try:
        _guarded_convert_dxf(args.dgn_path, args.dxf_path)
        print(f"[SUCCESS] DXF conversion complete!")
        out_path = Path(args.dxf_path) if args.dxf_path else Path(args.dgn_path).with_suffix(".dxf")
        print(f"  Output written to: {out_path}")
    except Exception as e:
        print(f"Error during conversion: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
