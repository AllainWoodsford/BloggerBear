"""Reading a window of a tiled (Cloud-Optimized) GeoTIFF with HTTP range requests.

Sentinel-2 L2A on AWS Open Data (`sentinel-cogs`) is one single-band, tiled, DEFLATE-compressed
GeoTIFF per band. A site is a few kilometres across, a scene ~110 km, so the worker reads only the
tiles under the site: one range request for the header and one per tile, never the whole file.

GDAL does this in general, but GDAL (via rasterio) and OpenCV together are over Lambda's 250 MB
zip limit. This reads the subset of TIFF that COGs use and nothing more:

- classic TIFF and BigTIFF, either byte order;
- tiled, one sample per pixel, the first image (full resolution) and its reduced-resolution
  overviews (`CogInfo.levels`), so a city can be read at 20 m from a 10 m band in a fraction of
  the bytes; mask images in the chain are skipped;
- no compression, DEFLATE (8 or 32946) or LZW is refused, with horizontal predictor (2) undone;
- 8/16/32-bit unsigned or signed integers and 32/64-bit floats;
- georeferencing from ModelPixelScale + ModelTiepoint, and the EPSG code from the GeoKey
  directory's ProjectedCSTypeGeoKey.

Anything else raises `CogError`, which the worker reports as an unreadable scene.

`fetch(url, start, end)` (inclusive byte range, returning bytes) is injected, so tests read files
built in memory and the worker can use any HTTP client.
"""

from __future__ import annotations

import dataclasses
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

Fetch = Callable[[str, int, int], bytes]

HEADER_BYTES = 65536
# How far down the directory chain to look for overviews: a COG has one per halving of the full
# resolution down to one tile, so a 10980 px Sentinel-2 band with 1024 px tiles has four.
MAX_IFDS = 8

_TAG_SUBFILE_TYPE = 254
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
# NewSubfileType bits: 1 marks a reduced-resolution image (an overview), 4 a transparency mask.
_SUBFILE_REDUCED, _SUBFILE_MASK = 1, 4

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
    # The file's overviews, finest first and coarsest last, each a `CogInfo` that `read_window`
    # takes with a window in that level's own pixels. Empty on a level and on a file without any.
    levels: tuple[CogInfo, ...] = ()

    @property
    def tiles_across(self) -> int:
        return -(-self.width // self.tile_width)

    @property
    def pixel_size(self) -> float:
        return abs(self.transform[1])


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
    """Parse the first image's directory, and the overviews chained after it."""
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

    tags, next_ifd = _parse_ifd(src, ifd, order, big)
    base = _info_from_tags(tags, order)
    levels: list[CogInfo] = []
    seen = {ifd}
    while next_ifd and next_ifd not in seen and len(seen) < MAX_IFDS:
        seen.add(next_ifd)
        try:
            tags, next_ifd = _parse_ifd(src, next_ifd, order, big)
        except (CogError, struct.error):
            break  # a directory that can't be parsed ends the chain; the base image is still good
        kind = tags.get(_TAG_SUBFILE_TYPE, (0,))[0]
        if not kind & _SUBFILE_REDUCED or kind & _SUBFILE_MASK:
            continue
        try:
            levels.append(_info_from_tags(tags, order, base))
        except CogError:
            continue  # an overview in a form this reader can't decode is left out, never fatal
    levels.sort(key=lambda level: level.pixel_size)
    return dataclasses.replace(base, levels=tuple(levels)), src


def _parse_ifd(src: _Source, offset: int, order: str, big: bool) -> tuple[dict[int, tuple], int]:
    """The tags of the image file directory at `offset` and the offset of the next directory in
    the chain (0 after the last)."""
    count_fmt, count_size = ("Q", 8) if big else ("H", 2)
    entry_size, inline = (20, 8) if big else (12, 4)
    (n,) = struct.unpack(order + count_fmt, src.read(offset, count_size))
    raw = src.read(offset + count_size, n * entry_size)
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
            value_offset = struct.unpack(order + ("Q" if big else "I"), value_field)[0]
            data = src.read(value_offset, nbytes)
        if ftype == 2:
            tags[tag] = (data.rstrip(b"\x00").decode("ascii", "replace"),)
        elif len(code) == 1:
            tags[tag] = struct.unpack(order + code * count, data)
        else:
            tags[tag] = _rationals(order, code, data)
    next_fmt, next_size = ("Q", 8) if big else ("I", 4)
    after_entries = offset + count_size + n * entry_size
    (next_offset,) = struct.unpack(order + next_fmt, src.read(after_entries, next_size))
    return tags, next_offset


def _info_from_tags(tags: dict[int, tuple], order: str, base: CogInfo | None = None) -> CogInfo:
    """A `CogInfo` from one directory's tags. With `base` the directory is one of its overviews:
    the georeferencing is the base's, scaled by the ratio of the widths, and the EPSG code and
    nodata are the base's too (overviews carry neither)."""

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
    width, height = one(_TAG_WIDTH), one(_TAG_HEIGHT)
    if not width or not height:
        raise CogError("no image size")

    if base is None:
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
        epsg = _epsg(tags.get(_TAG_GEOKEYS))
    else:
        factor = base.width / width
        c, a, _, f, _, e = base.transform
        transform = (c, a * factor, 0.0, f, 0.0, e * factor)
        nodata, epsg = base.nodata, base.epsg

    return CogInfo(
        width=width,
        height=height,
        tile_width=one(_TAG_TILE_W),
        tile_height=one(_TAG_TILE_H),
        dtype=np.dtype(order + dtype),
        compression=compression,
        predictor=predictor,
        tile_offsets=tuple(tags[_TAG_TILE_OFFSETS]),
        tile_counts=tuple(tags[_TAG_TILE_COUNTS]),
        transform=transform,
        epsg=epsg,
        nodata=nodata,
    )


def level_for_pixel_size(info: CogInfo, pixel_size_m: float, tolerance: float = 0.01) -> CogInfo | None:
    """The base image or the overview whose pixel is `pixel_size_m`, to within `tolerance` as a
    fraction of it (a 10980 px band's third overview is 1373 px, 79.97 m rather than 80); the
    finest such level, or None when the file has no level at that size."""
    if pixel_size_m <= 0:
        raise ValueError("pixel_size_m must be positive")
    for level in (info, *info.levels):
        if abs(level.pixel_size - pixel_size_m) <= tolerance * pixel_size_m:
            return level
    return None


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
