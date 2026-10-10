"""Test helpers for the vision worker: an in-memory tiled GeoTIFF writer and a fetch over it.

The writer produces what `sentinel-cogs` serves: single-band, tiled, DEFLATE with the horizontal
predictor, georeferenced by pixel scale and tiepoint with an EPSG GeoKey (see
tests/test_vision_cog_geo.py for how that layout was checked against a real file).
"""

from __future__ import annotations

import struct
import zlib

import numpy as np


def write_tiff(
    image: np.ndarray,
    tile: int = 16,
    compression: int = 8,
    predictor: int = 2,
    big: bool = False,
    order: str = "<",
    origin=(300000.0, 6300040.0),
    pixel=10.0,
    epsg: int = 32756,
    nodata: str | None = "0",
    drop_tiles: tuple[int, ...] = (),
    gap: int = 4096,
) -> bytes:
    """A tiled single-band GeoTIFF of `image` (uint8/uint16/int16/float32)."""
    h, w = image.shape
    dtype = image.dtype.newbyteorder(order)
    tiles = []
    for r in range(0, h, tile):
        for c in range(0, w, tile):
            block = np.zeros((tile, tile), dtype=image.dtype)
            part = image[r : r + tile, c : c + tile]
            block[: part.shape[0], : part.shape[1]] = part
            if predictor == 2:
                block = np.diff(block, axis=1, prepend=np.zeros((tile, 1), block.dtype)).astype(block.dtype)
            raw = block.astype(dtype).tobytes()
            tiles.append(zlib.compress(raw) if compression == 8 else raw)
    fmt = {"u": 1, "i": 2, "f": 3}[image.dtype.kind]
    ptr, cnt_fmt = ("Q", "Q") if big else ("I", "H")
    entry_size, inline = (20, 8) if big else (12, 4)
    header_size = 16 if big else 8

    # (tag, type, values); arrays are laid out after the IFD.
    geokeys = [1, 1, 0, 1, 3072, 0, 1, epsg]
    entries = [
        (256, 3, [w]), (257, 3, [h]), (258, 3, [image.dtype.itemsize * 8]), (259, 3, [compression]),
        (277, 3, [1]), (317, 3, [predictor]), (322, 3, [tile]), (323, 3, [tile]),
        (324, 16 if big else 4, None), (325, 4, [len(t) for t in tiles]), (339, 3, [fmt]),
        (33550, 12, [pixel, pixel, 0.0]), (33922, 12, [0.0, 0.0, 0.0, origin[0], origin[1], 0.0]),
        (34735, 3, geokeys),
    ]  # fmt: skip
    if nodata is not None:
        entries.append((42113, 2, nodata.encode() + b"\x00"))
    entries.sort(key=lambda e: e[0])
    codes = {2: "c", 3: "H", 4: "I", 12: "d", 16: "Q"}
    ifd_offset = header_size
    ifd_size = (8 if big else 2) + len(entries) * entry_size + (8 if big else 4)
    extra_offset = ifd_offset + ifd_size
    tile_data_offset = extra_offset + gap  # room for the arrays, and optionally past the header block
    offsets, pos = [], tile_data_offset
    for i, t in enumerate(tiles):
        offsets.append(0 if i in drop_tiles else pos)
        pos += len(t)

    def pack(ftype, values):
        if ftype == 2:
            return values
        return struct.pack(order + codes[ftype] * len(values), *values)

    body, extra = b"", b""
    for tag, ftype, values in entries:
        if tag == 324:
            values = offsets
        data = pack(ftype, values)
        count = len(values)
        if len(data) <= inline:
            field = data.ljust(inline, b"\x00")
        else:
            field = struct.pack(order + ptr, extra_offset + len(extra))
            extra += data
            if len(extra) % 2:
                extra += b"\x00"
        count_field = struct.pack(order + ("Q" if big else "I"), count)
        body += struct.pack(order + "HH", tag, ftype) + count_field + field
    assert len(extra) <= gap
    if big:
        header = (b"II" if order == "<" else b"MM") + struct.pack(order + "HHHQ", 43, 8, 0, ifd_offset)
    else:
        header = (b"II" if order == "<" else b"MM") + struct.pack(order + "HI", 42, ifd_offset)
    ifd = struct.pack(order + cnt_fmt, len(entries)) + body + struct.pack(order + ptr, 0)
    out = header + ifd + extra
    out = out.ljust(tile_data_offset, b"\x00") + b"".join(tiles)
    return out


def fetcher(blob: bytes, log: list | None = None):
    def fetch(url, start, end):
        if log is not None:
            log.append((start, end))
        return blob[start : end + 1]

    return fetch
