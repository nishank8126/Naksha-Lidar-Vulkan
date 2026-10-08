"""ptc_table.py - the ONE point-class-table (.ptc) parser.

WHY THIS MODULE EXISTS. The .ptc record shape was already known and already
parsed - twice, inline, in two different places (AppWindow._load_ptc_file and
DisplayModeDialog.load_classes_from_path). Neither copy could:

  * report WHERE a file was malformed (no line number, so "Invalid colour
    definition at line 42" was impossible);
  * parse WITHOUT mutating live state, because the loading method assigned
    `current_ptc_path` and cleared the class table BEFORE it started parsing.
    A malformed file therefore replaced the active PTC with a broken one.

So this is not "another parser": it is the single canonical one, pure (it takes
text and returns a palette or raises), with the file-reading, encoding and
default-palette policy in one auditable place.

RECORD SHAPE (as written back out by DisplayModeDialog._write_ptc, which is the
authoritative producer in this codebase) - two lines per class, tab separated,
a blank line between records:

    code<TAB>description<TAB>lvl
    *<TAB>draw<TAB>code<TAB>R,G,B<TAB>show<TAB>weight

  fields: header[0]=code, header[1]=description, header[2]=lvl
          detail[0]=*, detail[1]=draw, detail[2]=code again, detail[3]="R,G,B"
          detail[4]=show (1/0), detail[5]=weight (optional)

Blank lines are skipped. Lines whose first non-space character is '#', ';' or a
'//' prefix are treated as comments and skipped - the writer never emits them,
but real TerraScan/third-party tables do, and rejecting a whole file over a
comment would be worse than skipping it.
"""
from __future__ import annotations

import os
from typing import Dict, List, Tuple

# The documented Naksha default: one flat grey for every class, every class
# shown, weight 1.0. Matches display_modes.CLASS_DEFAULT_RGB and the legacy
# behaviour ("default class col (128,128,128)").
CLASS_DEFAULT_RGB = (128, 128, 128)
DEFAULT_WEIGHT = 1.0
# Weights are clamped to the range the LUT builder accepts (display_modes
# CLASS_WEIGHT_MIN/MAX), so a table can never push the LUT out of range.
WEIGHT_MIN, WEIGHT_MAX = 0.1, 12.0
# A table is class ids 0..255 - one per LUT entry.
CLASS_ID_MIN, CLASS_ID_MAX = 0, 255


class PtcParseError(Exception):
    """A precise, user-presentable failure: file, line and reason.

    Raised for structural problems so the caller can show exactly this and
    leave the active palette untouched. `line` is 1-based and refers to the
    file as written.
    """

    def __init__(self, path: str, line: int, reason: str):
        self.path = str(path or "")
        self.line = int(line)
        self.reason = str(reason)
        super().__init__(self.format_message())

    def format_message(self) -> str:
        name = os.path.basename(self.path) if self.path else "(text)"
        return f"{name}: line {self.line}: {self.reason}"

    def as_dialog_text(self) -> str:
        """The exact text shown to the user (Phase 6B.2)."""
        return (f"File:\n{self.path or '(text)'}\n\n"
                f"Reason:\nline {self.line}: {self.reason}\n\n"
                f"Previous PTC remains active.")


def _is_comment(line: str) -> bool:
    s = line.lstrip()
    return s.startswith("#") or s.startswith(";") or s.startswith("//")


def _parse_code(field: str, line_no: int, path: str) -> int:
    text = field.strip()
    try:
        code = int(text)
    except Exception:
        raise PtcParseError(path, line_no,
                            f"class id {text!r} is not an integer") from None
    if not (CLASS_ID_MIN <= code <= CLASS_ID_MAX):
        raise PtcParseError(path, line_no,
                            f"class id {code} is outside "
                            f"{CLASS_ID_MIN}-{CLASS_ID_MAX}")
    return code


def _parse_rgb(field: str, line_no: int, path: str) -> Tuple[int, int, int]:
    parts = [p.strip() for p in str(field).split(",")]
    if len(parts) != 3:
        raise PtcParseError(path, line_no,
                            f"colour {field!r} is not 'R,G,B' "
                            f"({len(parts)} component(s))")
    out = []
    for p in parts:
        try:
            v = int(p)
        except Exception:
            raise PtcParseError(path, line_no,
                                f"colour component {p!r} is not an "
                                f"integer") from None
        if not (0 <= v <= 255):
            raise PtcParseError(path, line_no,
                                f"colour component {v} is outside 0-255")
        out.append(v)
    return (out[0], out[1], out[2])


def _parse_weight(field, line_no: int, path: str, warnings: List[str]) -> float:
    if field is None or str(field).strip() == "":
        return DEFAULT_WEIGHT
    try:
        w = float(str(field).strip())
    except Exception:
        warnings.append(f"line {line_no}: weight {field!r} is not a number; "
                        f"using {DEFAULT_WEIGHT}")
        return DEFAULT_WEIGHT
    if w < WEIGHT_MIN or w > WEIGHT_MAX:
        clamped = max(WEIGHT_MIN, min(w, WEIGHT_MAX))
        warnings.append(f"line {line_no}: weight {w} clamped to {clamped}")
        return clamped
    return w


def parse_ptc_text(text: str, path: str = "") -> Tuple[Dict[int, dict], List[str]]:
    """(palette, warnings). Raises PtcParseError - never returns a partial one.

    Pure: no I/O, no global state, nothing the caller already holds is touched,
    so a malformed file cannot damage the active palette (Phase 6B.2).
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [(i + 1, ln) for i, ln in enumerate(raw.split("\n"))]
    # Blank and comment lines carry no record structure, so they are removed
    # BEFORE pairing - otherwise a single blank line inside a record shifts
    # every subsequent pair and every later class mis-parses.
    body = [(n, ln) for n, ln in lines
            if ln.strip() and not _is_comment(ln)]

    palette: Dict[int, dict] = {}
    warnings: List[str] = []
    i = 0
    while i < len(body):
        h_line, header_raw = body[i]
        if i + 1 >= len(body):
            raise PtcParseError(
                path, h_line,
                "class header has no detail line (the file ends mid-record)")
        d_line, detail_raw = body[i + 1]
        header = header_raw.split("\t")
        detail = detail_raw.split("\t")
        if len(header) < 2:
            raise PtcParseError(
                path, h_line,
                f"class header needs at least 'id<TAB>description' "
                f"({len(header)} tab-separated field(s))")
        if len(detail) < 5:
            raise PtcParseError(
                path, d_line,
                f"detail line needs at least 5 tab-separated fields "
                f"(draw, id, R,G,B, show) - found {len(detail)}")
        code = _parse_code(header[0], h_line, path)
        description = header[1]
        lvl = header[2] if len(header) > 2 else ""
        draw = detail[1]
        rgb = _parse_rgb(detail[3], d_line, path)
        show = str(detail[4]).strip() == "1"
        weight = _parse_weight(detail[5] if len(detail) > 5 else None,
                               d_line, path, warnings)
        if code in palette:
            warnings.append(f"line {h_line}: class {code} redefined; "
                            f"the later entry wins")
        palette[code] = {
            "show": show,
            "description": str(description),
            "draw": str(draw),
            "lvl": str(lvl),
            "color": rgb,
            "weight": weight,
        }
        i += 2
    if not palette:
        raise PtcParseError(path, 1,
                            "no class records found (expected pairs of "
                            "tab-separated lines)")
    return palette, warnings


def parse_ptc_file(path: str) -> Tuple[Dict[int, dict], List[str]]:
    """Read + parse a .ptc file. Raises PtcParseError for ANY failure.

    A file that cannot be read is reported the same way as a malformed one, so
    the caller has exactly one failure path and one way to keep the active
    palette intact. Paths with spaces or non-ASCII characters work: the path is
    used as given, never normalised into a template.
    """
    p = str(path or "")
    if not p.strip():
        raise PtcParseError(p, 0, "no file selected")
    if not os.path.isfile(p):
        raise PtcParseError(p, 0, "file does not exist")
    try:
        with open(p, "r", encoding="utf-8-sig", errors="strict") as fh:
            text = fh.read()
    except UnicodeDecodeError:
        # Legacy tables are not always UTF-8; read the bytes rather than refuse
        # a usable file. Reported as a warning, not an error.
        try:
            with open(p, "r", encoding="latin-1") as fh:
                text = fh.read()
        except OSError as exc:
            raise PtcParseError(p, 0, f"cannot read file: {exc}") from None
        palette, warnings = parse_ptc_text(text, p)
        warnings.insert(0, "file is not UTF-8; read as latin-1")
        return palette, warnings
    except OSError as exc:
        raise PtcParseError(p, 0, f"cannot read file: {exc}") from None
    return parse_ptc_text(text, p)


def default_palette(codes=None) -> Dict[int, dict]:
    """The built-in Naksha palette: every class grey, shown, weight 1.0.

    Used by "Use Default Palette" and by "Clear PTC", so both restore the SAME
    documented state rather than each inventing one. `codes` limits the result
    to those class ids when the caller knows them (the dialog's current rows);
    with no codes every class 0..255 is defined, which is what the LUT needs to
    be fully specified.
    """
    ids = list(range(CLASS_ID_MIN, CLASS_ID_MAX + 1)) if codes is None \
        else [int(c) for c in codes]
    return {int(c): {"show": True, "description": "", "draw": "",
                     "lvl": "", "color": CLASS_DEFAULT_RGB,
                     "weight": DEFAULT_WEIGHT} for c in ids}


def palette_from_app(app) -> Dict[int, dict]:
    """Read a palette back out of whatever the app currently holds.

    Only used to decide class VISIBILITY for a table that omits it; the PTC
    file itself is authoritative for everything it defines.
    """
    pal = getattr(app, "class_palette", None)
    return dict(pal) if isinstance(pal, dict) else {}


# ── Project binding (Phase 6B.5) ─────────────────────────────────────────────
#
# QSettings already remembered ONE last PTC path for the whole application and
# applied it at startup no matter which dataset was being opened, so loading a
# palette for project A silently re-coloured project B the next morning. The
# path is still remembered - the file picker should reopen where the user last
# was - but it no longer applies itself: a binding is honoured only when it was
# recorded FOR THIS DATASET. These helpers are pure (map in, map out) so the
# policy is testable without Qt settings.
PTC_BINDING_SETTING = "ptc_project_bindings"
PTC_BINDING_LIMIT = 50


def dataset_key(path) -> str:
    """Stable identity for a dataset path (case/separator-insensitive)."""
    p = str(path or "").strip()
    if not p:
        return ""
    try:
        return os.path.normcase(os.path.abspath(p))
    except Exception:
        return os.path.normcase(p)


def resolve_project_ptc(bindings, dataset) -> str:
    """The PTC recorded for THIS dataset, or "" - never another project's."""
    key = dataset_key(dataset)
    if not key or not isinstance(bindings, dict):
        return ""
    value = str(bindings.get(key, "") or "").strip()
    return value if os.path.isfile(value) else ""


def bind_project_ptc(bindings, dataset, ptc_path) -> dict:
    """Return a NEW binding map with dataset -> ptc_path recorded."""
    out = dict(bindings) if isinstance(bindings, dict) else {}
    key = dataset_key(dataset)
    path = str(ptc_path or "").strip()
    if not key or not path:
        return out
    out.pop(key, None)
    out[key] = path
    while len(out) > PTC_BINDING_LIMIT:
        out.pop(next(iter(out)))
    return out


def unbind_project_ptc(bindings, dataset=None) -> dict:
    """Drop one dataset's binding, or every binding when dataset is None."""
    out = dict(bindings) if isinstance(bindings, dict) else {}
    if dataset is None:
        return {}
    out.pop(dataset_key(dataset), None)
    return out


def recent_ptc_dir(settings_value) -> str:
    """The folder a file picker should open in, from a remembered path."""
    p = str(settings_value or "").strip()
    if not p:
        return ""
    if os.path.isdir(p):
        return p
    return os.path.dirname(p)
