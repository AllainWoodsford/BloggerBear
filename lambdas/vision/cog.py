"""Reading a window of a tiled (Cloud-Optimized) GeoTIFF with HTTP range requests.

Sentinel-2 L2A on AWS Open Data (`sentinel-cogs`) is one single-band, tiled, DEFLATE-compressed
GeoTIFF per band. A site is a few kilometres across, a scene ~110 km, so the worker reads only the
tiles under the site: one range request for the header and one per tile, never the whole file.

GDAL does this in general, but GDAL (via rasterio) and OpenCV together are over Lambda's 250 MB
zip limit. This reads the subset of TIFF that COGs use and nothing more:

- classic TIFF and BigTIFF, either byte order;
- tiled, one sample per pixel, the first image (full resolution; overviews are ignored);
- no compression, DEFLATE (8 or 32946) or LZW is refused, with horizontal predictor (2) undone;
- 8/16/32-bit unsigned or signed integers and 32/64-bit floats;
- georeferencing from ModelPixelScale + ModelTiepoint, and the EPSG code from the GeoKey
  directory's ProjectedCSTypeGeoKey.

Anything else raises `CogError`, which the worker reports as an unreadable scene.

`fetch(url, start, end)` (inclusive byte range, returning bytes) is injected, so tests read files
built in memory and the worker can use any HTTP client.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

Fetch = Callable[[str, int, int], bytes]

HEADER_BYTES = 65536

_TAG_WIDTH, _TAG_HEIGHT = 256, 257
_TAG_BITS, _TAG_COMPRESSION = 258, 259
_TAG_SAMPLES = 277
_TAG_PREDICTOR = 317
_TAG_TILE_W, _TAG_TILE_H = 322, 323
_TAG_TILE_OFFSETS, _TAG_TILE_COUNTS = 324, 325
_TAG_SAMPLE_FORMAT = 339
_TAG_PIXEL_SCALE, _TAG_TIEPOINT = 33550, 33922
_TAG_GEOKEYS = 34735
_TAG_NODATA = 42113
_GEOKEY_PROJECTED_CS = 3072

# TIFF field type -> (struct code, size)
_TYPES = {
    1: ("B", 1), 2: ("c", 1), 3: ("H", 2), 4: ("I", 4), 5: ("II", 8), 6: ("b", 1), 7: ("B", 1),
    8: ("h", 2), 9: ("i", 4), 10: ("ii", 8), 11: ("f", 4), 12: ("d", 8), 16: ("Q", 8), 17: ("q", 8),
    18: ("Q", 8),
}  # fmt: skip
_DTYPES = {(1, 8): "u1", (1, 16): "u2", (1, 32): "u4", (2, 8): "i1", (2, 16): "i2", (2, 32): "i4",
           (3, 32): "f4", (3, 64): "f8"}  # fmt: skip


class CogError(ValueError):
    """The file isn't a GeoTIFF this reader can read."""


@dataclass(frozen=True)
class CogInfo:
    width: int
    height: int
    tile_width: int
    tile_height: int
    dtype: np.dtype
    compression: int
    predictor: int
    tile_offsets: tuple[int, ...]
    tile_counts: tuple[int, ...]
    transform: tuple[float, float, float, float, float, float]
    epsg: int | None
    nodata: float | None

    @property
    def tiles_across(self) -> int:
        return -(-self.width // self.tile_width)


class _Source:
    """The file's bytes, from the header block already fetched or from a further range."""

    def __init__(self, url: str, fetch: Fetch):
        self.url, self.fetch = url, fetch
        self.head = fetch(url, 0, HEADER_BYTES - 1)

    def read(self, offset: int, length: int) -> bytes:
        if offset + length <= len(self.head):
            return self.head[offset : offset + length]
        data = self.fetch(self.url, offset, offset + length - 1)
        if len(data) != length:
            raise CogError("short read")
        return data


def read_info(url: str, fetch: Fetch) -> tuple[CogInfo, _Source]:
    """Parse the first image's directory."""
    src = _Source(url, fetch)
    head = src.head
    if len(head) < 16:
        raise CogError("file too short")
    order = {b"II": "<", b"MM": ">"}.get(head[:2])
    if order is None:
        raise CogError("not a TIFF")
    magic = struct.unpack(order + "H", head[2:4])[0]
    if magic == 42:
        big, ifd = False, struct.unpack(order + "I", head[4:8])[0]
    elif magic == 43:
        big, ifd = True, struct.unpack(order + "Q", head[8:16])[0]
    else:
        raise CogError("not a TIFF")

    count_fmt, count_size = ("Q", 8) if big else ("H", 2)
    entry_size, inline = (20, 8) if big else (12, 4)
    (n,) = struct.unpack(order + count_fmt, src.read(ifd, count_size))
    raw = src.read(ifd + count_size, n * entry_size)
    tags: dict[int, tuple] = {}
    for i in range(n):
        entry = raw[i * entry_size : (i + 1) * entry_size]
        tag, ftype = struct.unpack(order + "HH", entry[:4])
        count = struct.unpack(order + ("Q" if big else "I"), entry[4 : 4 + (8 if big else 4)])[0]
        if ftype not in _TYPES:
            continue
        code, size = _TYPES[ftype]
        nbytes = size * count
        value_field = entry[entry_size - inline :]
        if nbytes <= inline:
            data = value_field[:nbytes]
        else:
            offset = struct.unpack(order + ("Q" if big else "I"), value_field)[0]
            data = src.read(offset, nbytes)
        if ftype == 2:
            tags[tag] = (data.rstrip(b"\x00").decode("ascii", "replace"),)
        elif len(code) == 1:
            tags[tag] = struct.unpack(order + code * count, data)
        else:
            tags[tag] = _rationals(order, code, data)

    def one(tag, default=None):
        value = tags.get(tag)
        return value[0] if value else default

    if _TAG_TILE_W not in tags or _TAG_TILE_OFFSETS not in tags:
        raise CogError("not tiled")
    if one(_TAG_SAMPLES, 1) != 1:
        raise CogError("only single-band files are supported")
    compression = one(_TAG_COMPRESSION, 1)
    if compression not in (1, 8, 32946):
        raise CogError(f"compression {compression} is not supported")
    predictor = one(_TAG_PREDICTOR, 1)
    if predictor not in (1, 2):
        raise CogError(f"predictor {predictor} is not supported")
    dtype = _DTYPES.get((one(_TAG_SAMPLE_FORMAT, 1), one(_TAG_BITS, 8)))
    if dtype is None:
        raise CogError("sample type is not supported")

    scale, tie = tags.get(_TAG_PIXEL_SCALE), tags.get(_TAG_TIEPOINT)
    if not scale or not tie or len(tie) < 6:
        raise CogError("no georeferencing")
    sx, sy = scale[0], scale[1]
    i, j, x, y = tie[0], tie[1], tie[3], tie[4]
    transform = (x - i * sx, sx, 0.0, y + j * sy, 0.0, -sy)

    nodata_text = one(_TAG_NODATA)
    try:
        nodata = float(nodata_text) if nodata_text not in (None, "") else None
    except ValueError:
        nodata = None

    info = CogInfo(
        width=one(_TAG_WIDTH),
        height=one(_TAG_HEIGHT),
        tile_width=one(_TAG_TILE_W),
        tile_height=one(_TAG_TILE_H),
        dtype=np.dtype(order + dtype),
        compression=compression,
        predictor=predictor,
        tile_offsets=tuple(tags[_TAG_TILE_OFFSETS]),
        tile_counts=tuple(tags[_TAG_TILE_COUNTS]),
        transform=transform,
        epsg=_epsg(tags.get(_TAG_GEOKEYS)),
        nodata=nodata,
    )
    return info, src


def read_window(
    url: str, window: tuple[int, int, int, int], fetch: Fetch, info_and_source=None
) -> tuple[np.ndarray, CogInfo]:
    """The pixels of `window` = (col0, row0, col1, row1), end-exclusive, and the file's info.
    Only the tiles the window touches are fetched; tiles absent from the file (offset 0) read as
    nodata, or 0."""
    info, src = info_and_source or read_info(url, fetch)
    col0, row0, col1, row1 = window
    if not (0 <= col0 < col1 <= info.width and 0 <= row0 < row1 <= info.height):
        raise CogError("window outside the image")
    tw, th = info.tile_width, info.tile_height
    out = np.full((row1 - row0, col1 - col0), info.nodata or 0, dtype=info.dtype.newbyteorder("="))
    for trow in range(row0 // th, (row1 - 1) // th + 1):
        for tcol in range(col0 // tw, (col1 - 1) // tw + 1):
            index = trow * info.tiles_across + tcol
            offset, count = info.tile_offsets[index], info.tile_counts[index]
            if not offset or not count:
                continue
            tile = _decode(src.read(offset, count), info)
            # The overlap of this tile with the window, in image coordinates.
            r0, r1 = max(row0, trow * th), min(row1, (trow + 1) * th)
            c0, c1 = max(col0, tcol * tw), min(col1, (tcol + 1) * tw)
            out[r0 - row0 : r1 - row0, c0 - col0 : c1 - col0] = tile[
                r0 - trow * th : r1 - trow * th, c0 - tcol * tw : c1 - tcol * tw
            ]
    return out, info


def window_transform(info: CogInfo, window: tuple[int, int, int, int]) -> tuple[float, ...]:
    """The affine transform of a window's top-left pixel."""
    c, a, b, f, d, e = info.transform
    col0, row0 = window[0], window[1]
    return (c + a * col0, a, b, f + e * row0, d, e)


def _decode(data: bytes, info: CogInfo) -> np.ndarray:
    if info.compression in (8, 32946):
        try:
            data = zlib.decompress(data)
        except zlib.error as exc:
            raise CogError("bad DEFLATE tile") from exc
    expected = info.tile_width * info.tile_height * info.dtype.itemsize
    if len(data) < expected:
        raise CogError("tile is short")
    tile = np.frombuffer(data[:expected], dtype=info.dtype).reshape(info.tile_height, info.tile_width)
    tile = tile.astype(info.dtype.newbyteorder("="))
    if info.predictor == 2:
        if info.dtype.kind == "f":
            raise CogError("horizontal predictor on floats is not supported")
        # Each row stores differences from the pixel to its left; wrap-around addition undoes it.
        tile = np.cumsum(tile, axis=1, dtype=tile.dtype)
    return tile


def _rationals(order: str, code: str, data: bytes) -> tuple[float, ...]:
    values = struct.unpack(order + code[0] * (len(data) // 4), data)
    return tuple(values[k] / values[k + 1] if values[k + 1] else 0.0 for k in range(0, len(values), 2))


def _epsg(keys: tuple | None) -> int | None:
    if not keys or len(keys) < 4:
        return None
    for k in range(4, len(keys) - 3, 4):
        key_id, location, _count, value = keys[k : k + 4]
        if key_id == _GEOKEY_PROJECTED_CS and location == 0:
            return int(value)
    return None
