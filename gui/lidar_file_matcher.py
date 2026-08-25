"""Safe LAS/LAZ filename matching for grid and block loads.

Automatic grid loading must never guess from only one coordinate component.
For example, ``613000_676000`` and ``609000_676000`` are different grids even
though their final numeric token is the same.
"""

from pathlib import Path
import re
from typing import Iterable, Optional, Sequence, Tuple


_LIDAR_EXTENSIONS = (".las", ".laz")


def strip_lidar_extension(name: str) -> str:
    text = str(name).strip()
    if text.lower().endswith(_LIDAR_EXTENSIONS):
        return text[:-4]
    return text


def normalized_stem(name: str) -> str:
    return "".join(ch.casefold() for ch in strip_lidar_extension(name) if ch.isalnum())


def numeric_identity(name: str) -> Tuple[str, ...]:
    """Return canonical numeric tokens, ignoring insignificant leading zeroes."""
    tokens = re.findall(r"\d+", strip_lidar_extension(name))
    return tuple(token.lstrip("0") or "0" for token in tokens)


def names_share_numeric_identity(primary_name: str, candidate_name: str) -> bool:
    """Whether an alias preserves a multi-part grid/block numeric identity.

    Names without a multi-part numeric identity cannot be checked this way and
    remain eligible for the normal exact matcher.
    """
    primary = numeric_identity(primary_name)
    if len(primary) < 2:
        return True
    candidate = numeric_identity(candidate_name)
    return len(candidate) >= len(primary) and candidate[-len(primary):] == primary


def _unique_best(matches: Sequence[Path]) -> Optional[Path]:
    """Return a deterministic unique match, or None when the name is ambiguous."""
    unique = {str(path).casefold(): path for path in matches}
    if len(unique) != 1:
        return None
    return next(iter(unique.values()))


def find_matching_lidar_file(
    files: Iterable[Path],
    requested_name: str,
) -> Optional[Path]:
    """Resolve ``requested_name`` without ever substituting a neighbouring grid.

    Match order:

    1. exact filename stem (case-insensitive);
    2. exact normalized stem (separator-insensitive);
    3. a unique filename containing the complete numeric identity as its suffix.

    The third rule supports safe prefixes such as
    ``survey_613000_677000.laz``. It deliberately requires *all* numeric tokens,
    so ``609000_677000.laz`` cannot satisfy ``613000_677000``. Ambiguous matches
    return ``None`` so the caller can open a file-selection dialog.
    """
    requested_stem = strip_lidar_extension(requested_name)
    if not requested_stem:
        return None

    paths = [Path(path) for path in files]
    requested_lower = requested_stem.casefold()

    exact = [path for path in paths if path.stem.casefold() == requested_lower]
    match = _unique_best(exact)
    if match is not None:
        return match

    requested_normalized = normalized_stem(requested_stem)
    if requested_normalized:
        normalized = [
            path for path in paths
            if normalized_stem(path.stem) == requested_normalized
        ]
        match = _unique_best(normalized)
        if match is not None:
            return match

    requested_numbers = numeric_identity(requested_stem)
    if not requested_numbers:
        return None

    numeric_matches = []
    for path in paths:
        file_numbers = numeric_identity(path.stem)
        if (
            len(file_numbers) >= len(requested_numbers)
            and file_numbers[-len(requested_numbers):] == requested_numbers
        ):
            numeric_matches.append(path)

    return _unique_best(numeric_matches)
