"""
SNT Pipeline - DGN to SNT conversion module.
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
    _REPO_ROOT = _APP_ROOT  # dgn_to_snt.py lives alongside nakshaapp_dgn_converter/

for _path in (_REPO_ROOT,):
    _p = str(_path)
    if _path.exists() and _p not in sys.path:
        sys.path.insert(0, _p)


def _guarded_convert(dgn_path, snt_path=None, **kwargs):
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
        from nakshaapp_dgn_converter._snt_core import convert as _real_convert
    except Exception as exc:
        raise RuntimeError(
            f"SNT conversion engine not available: {type(exc).__name__}: {exc}"
        ) from exc
    result = _real_convert(dgn_path, snt_path, **kwargs)

    try:
        if _m:
            _m.record_conversion()
    except Exception:
        pass

    return result


def main():
    mgr = DgnLicenseManager()

    ap = argparse.ArgumentParser(description="DGN -> SNT converter")
    ap.add_argument("dgn_path", help="Input .dgn file")
    ap.add_argument("snt_path", nargs="?", help="Output .snt (default: <dgn_stem>.snt)")
    ap.add_argument("--activate-key", default="", help="Activate with key")

    args = ap.parse_args()

    if args.activate_key:
        success, msg = mgr.activate_key(args.activate_key)
        print(f"\n{'='*60}")
        print(f"  {msg}")
        print(f"{'='*60}\n")
        sys.exit(0 if success else 1)

    try:
        result = _guarded_convert(args.dgn_path, args.snt_path)
        print(f"[SUCCESS] SNT conversion complete!")
        print(f"  Output written to: {result.snt_path}")
        print(f"  Entities converted: {result.entity_count}")
        print(f"  Layers written: {result.layer_count}")
    except Exception as e:
        print(f"Error during conversion: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
