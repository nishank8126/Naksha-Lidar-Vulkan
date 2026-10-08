"""Safe Morton implementations for intra-page ordering (Part 1/2).

Two EXPLICIT entry points with hard bit-width limits, because a uint64 key
cannot hold more than 64 interleaved bits:

    morton2d_u64(x, y, bits)   -> bits <= 31   (2 * bits <= 62)
    morton3d_u64(x, y, z, bits) -> bits <= 21   (3 * bits <= 63)

Both REJECT an over-wide request instead of silently wrapping. The earlier
benchmark overflowed at 32 bits/axis and produced garbage ordering with no
error at all, which is why the limit is enforced here rather than documented.

PART 2: coordinates are quantised against the PHYSICAL PAGE bounds, never the
dataset or shard bounds. Using a coarse global extent spends the bit budget on
empty space outside the page.
"""
import numpy as np

MAX_BITS_2D = 31
MAX_BITS_3D = 21


def _quantize(v, side):
    """Map a coordinate to uint64 in [0, side] using ITS OWN min/max.

    Quantisation is relative to the data actually being ordered (the physical
    page), so the full bit budget is spent inside that page.
    """
    v = np.asarray(v, dtype=np.float64)
    lo = float(v.min())
    span = float(v.max() - lo)
    if span <= 0:
        return np.zeros(v.shape, dtype=np.uint64)
    q = np.floor((v - lo) / span * float(side))
    return np.clip(q, 0, float(side)).astype(np.uint64)


def morton2d_u64(x, y, bits=24):
    """Interleave two axes into a uint64 key. bits <= 31."""
    if not (1 <= bits <= MAX_BITS_2D):
        raise ValueError(f"morton2d bits must be 1..{MAX_BITS_2D}, got {bits}")
    side = np.uint64((1 << bits) - 1)
    qx = _quantize(x, side)
    qy = _quantize(y, side)
    code = np.zeros(qx.shape, dtype=np.uint64)
    for b in range(bits):
        code |= (qx >> np.uint64(b)) << np.uint64(2 * b)
        code |= (qy >> np.uint64(b)) << np.uint64(2 * b + 1)
    return code


def morton3d_u64(x, y, z, bits=21):
    """Interleave three axes into a uint64 key. bits <= 21."""
    if not (1 <= bits <= MAX_BITS_3D):
        raise ValueError(f"morton3d bits must be 1..{MAX_BITS_3D}, got {bits}")
    side = np.uint64((1 << bits) - 1)
    qx = _quantize(x, side)
    qy = _quantize(y, side)
    qz = _quantize(z, side)
    code = np.zeros(qx.shape, dtype=np.uint64)
    for b in range(bits):
        code |= (qx >> np.uint64(b)) << np.uint64(3 * b)
        code |= (qy >> np.uint64(b)) << np.uint64(3 * b + 1)
        code |= (qz >> np.uint64(b)) << np.uint64(3 * b + 2)
    return code


def sort_order(x, y, z, variant="XY21"):
    """Permutation that orders points for a named variant."""
    table = {"XYZ16": ("3d", 16), "XYZ21": ("3d", 21), "XY16": ("2d", 16),
             "XY21": ("2d", 21), "XY24": ("2d", 24), "XY28": ("2d", 28)}
    if variant not in table:
        raise ValueError(f"unknown variant {variant}")
    kind, bits = table[variant]
    code = (morton3d_u64(x, y, z, bits) if kind == "3d"
            else morton2d_u64(x, y, bits))
    return np.argsort(code, kind="stable")


def sort_order_secondary(x, y, z, xy_bits=24, z_bits=10):
    """POLICY 2: XY Morton primary, quantised Z as a SECONDARY tie-break.

    LiDAR visibility is driven by XY; folding Z into the primary key spends XY
    resolution for no culling benefit. Sorting by (xy_code, z_code) keeps XY
    neighbours adjacent while still ordering points sensibly within a cell.
    """
    xy = morton2d_u64(x, y, xy_bits)
    qz = _quantize(z, np.uint64((1 << z_bits) - 1))
    return np.argsort((xy << np.uint64(z_bits)) | qz, kind="stable")