"""
SNT to DXF Pipeline - SNT to DXF reverse conversion module with security.
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
    _REPO_ROOT = _APP_ROOT  # snt-main/ lives alongside nakshaapp_dgn_converter/

for _path in (
    _REPO_ROOT,
    _REPO_ROOT / "snt_converter",
    _REPO_ROOT / "snt-main" / "snt_package",
    _REPO_ROOT / "snt-main" / "SNT",
    _REPO_ROOT / "snt-main" / "SNT_format",
    _REPO_ROOT / "snt-main",
):
    _p = str(_path)
    if _path.exists() and _p not in sys.path:
        sys.path.insert(0, _p)

def _guarded_reverse_convert(snt_path, dxf_path=None, **kwargs):
    """
    Python API for GUI use. Raises RuntimeError on processing failure.
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
        from snt_core.snt_to_dxf import reverse_convert as _real_reverse_convert
    except Exception as exc:
        raise RuntimeError(
            "SNT-to-DXF reverse conversion engine not available: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    
    result = _real_reverse_convert(Path(snt_path), Path(dxf_path) if dxf_path else None, **kwargs)

    try:
        if _m:
            _m.record_conversion()
    except Exception:
        pass

    return result


def main():
    mgr = DgnLicenseManager()

    ap = argparse.ArgumentParser(description="SNT -> DXF reverse converter")
    ap.add_argument("snt_path", help="Input .snt file")
    ap.add_argument("dxf_path", nargs="?", help="Output .dxf (default: <snt_stem>.dxf)")
    ap.add_argument("--dxf-version", default="R2010", help="DXF format version (default: R2010)")
    ap.add_argument("--activate-key", default="", help="Activate with key")

    args = ap.parse_args()

    if args.activate_key:
        success, msg = mgr.activate_key(args.activate_key)
        print(f"\n{'='*60}")
        print(f"  {msg}")
        print(f"{'='*60}\n")
        sys.exit(0 if success else 1)

    if not mgr.validate_conversion():
        print(f"\n{'='*60}")
        print(f"  Processing capacity reached. Contact support.")
        print(f"{'='*60}\n")
        sys.exit(1)

    try:
        result = _guarded_reverse_convert(args.snt_path, args.dxf_path, dxf_version=args.dxf_version)
        print(f"[SUCCESS] SNT -> DXF conversion complete!")
        print(f"  Output written to: {result.dxf_path}")
        print(f"  Entities converted: {result.entity_count} (skipped: {result.skipped_count})")
        print(f"  Layers restored: {result.layer_count}")
    except Exception as e:
        print(f"Error during conversion: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
