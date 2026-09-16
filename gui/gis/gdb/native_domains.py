"""Real FileGDB field-domain write support using GDAL's dataset API.

The legacy dialog stored edits in a sidecar JSON.  This module writes coded and
range domains into the actual FileGDB when the installed OpenFileGDB driver
supports Add/Update/DeleteFieldDomain (GDAL 3.6+ write-capable builds).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from osgeo import gdal, ogr
except Exception:
    gdal = None
    ogr = None


FIELD_TYPE_MAP = {
    "SmallInteger": "OFTInteger",
    "Integer": "OFTInteger",
    "Long": "OFTInteger",
    "LongInteger": "OFTInteger",
    "BigInteger": "OFTInteger64",
    "Single": "OFTReal",
    "Double": "OFTReal",
    "Float": "OFTReal",
    "String": "OFTString",
    "Text": "OFTString",
    "Date": "OFTDateTime",
    "DateOnly": "OFTDate",
    "TimeOnly": "OFTTime",
    "GUID": "OFTString",
    "GlobalID": "OFTString",
}


def _require():
    if gdal is None or ogr is None:
        raise RuntimeError("GDAL Python bindings are required for native domain editing.")


def _field_type(name: str):
    _require()
    attr = FIELD_TYPE_MAP.get(str(name or "String"), "OFTString")
    return getattr(ogr, attr, ogr.OFTString)


def _field_subtype(_name: str):
    return getattr(ogr, "OFSTNone", 0)


def _split_policy(name: str) -> int:
    text = str(name or "DefaultValue").lower()
    if "duplicate" in text:
        return getattr(ogr, "OFDSP_DUPLICATE", getattr(ogr, "OFDS_DUPLICATE", 1))
    if "geometry" in text or "ratio" in text:
        return getattr(ogr, "OFDSP_GEOMETRY_RATIO", getattr(ogr, "OFDS_GEOMETRY_RATIO", 2))
    return getattr(ogr, "OFDSP_DEFAULT_VALUE", getattr(ogr, "OFDS_DEFAULT_VALUE", 0))


def _merge_policy(name: str) -> int:
    text = str(name or "DefaultValue").lower()
    if "sum" in text:
        return getattr(ogr, "OFDMP_SUM", 1)
    if "geometry" in text or "weighted" in text:
        return getattr(ogr, "OFDMP_GEOMETRY_WEIGHTED", 2)
    return getattr(ogr, "OFDMP_DEFAULT_VALUE", 0)


def _typed_code(code: str, field_type: int):
    text = str(code)
    if field_type in {getattr(ogr, "OFTInteger", -1), getattr(ogr, "OFTInteger64", -2)}:
        try:
            return str(int(float(text)))
        except Exception:
            return text
    if field_type == getattr(ogr, "OFTReal", -3):
        try:
            return str(float(text))
        except Exception:
            return text
    return text


def build_field_domain(meta: dict):
    _require()
    name = str(meta.get("name", "") or "").strip()
    if not name:
        raise ValueError("Domain name cannot be empty.")
    description = str(meta.get("description", "") or "")
    ftype = _field_type(str(meta.get("field_type", "String") or "String"))
    fsubtype = _field_subtype(str(meta.get("field_subtype", "") or ""))
    dtype = str(meta.get("domain_type", "CodedValue") or "CodedValue").lower()

    if dtype.startswith("coded"):
        enumeration = {}
        for cv in list(meta.get("coded_values", []) or []):
            code = str(cv.get("code", "") or "").strip()
            if not code:
                continue
            enumeration[_typed_code(code, ftype)] = str(cv.get("description", "") or "")
        if not hasattr(ogr, "CreateCodedFieldDomain"):
            raise RuntimeError("This GDAL Python binding does not expose CreateCodedFieldDomain().")
        dom = ogr.CreateCodedFieldDomain(name, description, ftype, fsubtype, enumeration)
    else:
        min_value = meta.get("min_value")
        max_value = meta.get("max_value")
        def num(v):
            if v in (None, ""):
                return None
            if ftype in {getattr(ogr, "OFTInteger", -1), getattr(ogr, "OFTInteger64", -2)}:
                return int(float(v))
            return float(v)
        mn = num(min_value)
        mx = num(max_value)
        if not hasattr(ogr, "CreateRangeFieldDomain"):
            raise RuntimeError("This GDAL Python binding does not expose CreateRangeFieldDomain().")
        dom = ogr.CreateRangeFieldDomain(name, description, ftype, fsubtype, mn, True, mx, True)

    if dom is None:
        raise RuntimeError(f"GDAL could not construct field domain '{name}'.")
    try:
        dom.SetSplitPolicy(_split_policy(meta.get("split")))
    except Exception:
        pass
    try:
        dom.SetMergePolicy(_merge_policy(meta.get("merge")))
    except Exception:
        pass
    return dom


def open_gdb_update(gdb_path: str):
    _require()
    drivers = [d for d in ("OpenFileGDB", "FileGDB") if gdal.GetDriverByName(d)]
    try:
        ds = gdal.OpenEx(gdb_path, gdal.OF_VECTOR | gdal.OF_UPDATE,
                         allowed_drivers=drivers or None)
    except TypeError:
        ds = gdal.OpenEx(gdb_path, gdal.OF_VECTOR | gdal.OF_UPDATE)
    if ds is None:
        raise RuntimeError("The FileGDB could not be opened for update. It may be locked or the driver may be read-only.")
    return ds


def apply_domains(gdb_path: str, domains_meta: List[dict], *, delete_missing: bool = False) -> Tuple[bool, List[str]]:
    """Add/update actual GDB domains. Returns (success, messages)."""
    ds = open_gdb_update(gdb_path)
    messages: List[str] = []
    ok = True
    try:
        if not hasattr(ds, "AddFieldDomain"):
            raise RuntimeError("Installed GDAL does not support dataset field-domain writes.")
        try:
            current = set(ds.GetFieldDomainNames() or [])
        except Exception:
            current = set()
        desired = {str(m.get("name", "")).strip() for m in domains_meta if str(m.get("name", "")).strip()}

        transaction = False
        try:
            transaction = (ds.StartTransaction(force=True) == 0)
        except Exception:
            transaction = False

        try:
            for meta in domains_meta:
                name = str(meta.get("name", "") or "").strip()
                if not name:
                    continue
                dom = build_field_domain(meta)
                if name in current:
                    if hasattr(ds, "UpdateFieldDomain") and ds.UpdateFieldDomain(dom):
                        messages.append(f"Updated domain: {name}")
                    else:
                        messages.append(f"Could not update domain: {name}")
                        ok = False
                else:
                    if ds.AddFieldDomain(dom):
                        messages.append(f"Added domain: {name}")
                        current.add(name)
                    else:
                        messages.append(f"Could not add domain: {name}")
                        ok = False

            if delete_missing and hasattr(ds, "DeleteFieldDomain"):
                for name in sorted(current - desired):
                    if ds.DeleteFieldDomain(name):
                        messages.append(f"Deleted domain: {name}")
                    else:
                        messages.append(f"Could not delete domain: {name}")
                        ok = False

            if transaction:
                if ok:
                    if ds.CommitTransaction() != 0:
                        raise RuntimeError("Could not commit domain transaction.")
                else:
                    ds.RollbackTransaction()
            try:
                ds.FlushCache()
            except Exception:
                pass
        except Exception:
            if transaction:
                try:
                    ds.RollbackTransaction()
                except Exception:
                    pass
            raise
    finally:
        ds = None
    return ok, messages
