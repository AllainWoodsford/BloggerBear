"""Test helpers for the vision worker: an in-memory tiled GeoTIFF writer and a fetch over it.

The writer produces what `sentinel-cogs` serves: single-band, tiled, DEFLATE with the horizontal
predictor, georeferenced by pixel scale and tiepoint with an EPSG GeoKey (see
tests/test_vision_cog_geo.py for how that layout was checked against a real file). It can chain
reduced-resolution overviews and a 1-bit mask image after the full image, as a COG does.
"""

from __future__ import annotations

import struct
import zlib

import numpy as np


def block_mean(image: np.ndarray, factor: int) -> np.ndarray:
    """`image` reduced by `factor`: each output pixel is the rounded mean of a factor x factor
    block, and a partial block at the right or bottom edge is the mean of what is there, so the
    result is `ceil(size / factor)` across."""
    h, w = image.shape
    h2, w2 = -(-h // factor), -(-w // factor)
    total = np.zeros((h2, w2), np.float64)
    count = np.zeros((h2, w2), np.int64)
    for dy in range(factor):
        for dx in range(factor):
            part = image[dy::factor, dx::factor].astype(np.float64)
            total[: part.shape[0], : part.shape[1]] += part
            count[: part.shape[0], : part.shape[1]] += 1
    return np.round(total / count).astype(image.dtype)


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
    overviews: tuple[int, ...] = (),
    mask_ifd: bool = False,
) -> bytes:
    """A tiled single-band GeoTIFF of `image` (uint8/uint16/int16/float32).

    `overviews` are reduction factors (2, 4, ...): each adds a reduced image (NewSubfileType 1)
    that is the block mean of the full image, with the base's compression and predictor.
    `mask_ifd` adds a 1-bit transparency mask (NewSubfileType 4) at the end of the chain, which a
    reader must skip. The directories come first, then `gap` bytes, then every image's tiles.
    """
    images = [(image, None, compression, predictor)]
    for factor in overviews:
        images.append((block_mean(image, factor), 1, compression, predictor))
    if mask_ifd:
        images.append((np.full(image.shape, 255, np.uint8), 4, 1, 1))

    ptr, cnt_fmt = ("Q", "Q") if big else ("I", "H")
    entry_size, inline = (20, 8) if big else (12, 4)
    header_size = 16 if big else 8
    codes = {2: "c", 3: "H", 4: "I", 12: "d", 16: "Q"}

    def pack(ftype, values):
        if ftype == 2:
            return values
        return struct.pack(order + codes[ftype] * len(values), *values)

    # Per image: its tiles and its directory entries (tile offsets filled in once laid out).
    prepared = []
    for index, (img, subfile, comp, pred) in enumerate(images):
        one_bit = subfile == 4
        tiles = _tiles(img, tile, comp, pred, order, one_bit)
        bits = 1 if one_bit else img.dtype.itemsize * 8
        fmt = 1 if one_bit else {"u": 1, "i": 2, "f": 3}[img.dtype.kind]
        h, w = img.shape
        entries = [
            (256, 3, [w]), (257, 3, [h]), (258, 3, [bits]), (259, 3, [comp]),
            (277, 3, [1]), (317, 3, [pred]), (322, 3, [tile]), (323, 3, [tile]),
            (324, 16 if big else 4, None), (325, 4, [len(t) for t in tiles]), (339, 3, [fmt]),
        ]  # fmt: skip
        if subfile is not None:
            entries.append((254, 4, [subfile]))
        if index == 0:
            geokeys = [1, 1, 0, 1, 3072, 0, 1, epsg]
            entries += [
                (33550, 12, [pixel, pixel, 0.0]),
                (33922, 12, [0.0, 0.0, 0.0, origin[0], origin[1], 0.0]),
                (34735, 3, geokeys),
            ]
            if nodata is not None:
                entries.append((42113, 2, nodata.encode() + b"\x00"))
        entries.sort(key=lambda e: e[0])
        prepared.append((tiles, entries, index == 0))

    # Lay out the directories: each one's entries, then its out-of-line arrays.
    def ifd_size(entries):
        return (8 if big else 2) + len(entries) * entry_size + (8 if big else 4)

    positions, pos = [], header_size
    for tiles, entries, _ in prepared:
        extra = 0
        for tag, ftype, values in entries:
            size = len(values) if ftype == 2 else len(values or tiles) * struct.calcsize(codes[ftype])
            if size > inline:
                extra += size + size % 2
        positions.append((pos, pos + ifd_size(entries)))
        pos += ifd_size(entries) + extra
    tile_data_offset = pos + gap  # room past the directories, and optionally past the header block

    offsets_per_image, pos = [], tile_data_offset
    for tiles, _, is_base in prepared:
        offsets = []
        for i, t in enumerate(tiles):
            offsets.append(0 if is_base and i in drop_tiles else pos)
            pos += len(t)
        offsets_per_image.append(offsets)

    out = b""
    if big:
        out += (b"II" if order == "<" else b"MM") + struct.pack(order + "HHHQ", 43, 8, 0, header_size)
    else:
        out += (b"II" if order == "<" else b"MM") + struct.pack(order + "HI", 42, header_size)
    for k, (tiles, entries, _) in enumerate(prepared):
        ifd_offset, extra_offset = positions[k]
        next_offset = positions[k + 1][0] if k + 1 < len(prepared) else 0
        body, extra = b"", b""
        for tag, ftype, values in entries:
            if tag == 324:
                values = offsets_per_image[k]
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
        assert len(out) == ifd_offset
        out += struct.pack(order + cnt_fmt, len(entries)) + body
        out += struct.pack(order + ptr, next_offset) + extra
    out = out.ljust(tile_data_offset, b"\x00")
    for tiles, _, _ in prepared:
        out += b"".join(tiles)
    return out


def _tiles(image, tile, compression, predictor, order, one_bit):
    """The tile payloads of `image`, in row-major tile order."""
    h, w = image.shape
    tiles = []
    for r in range(0, h, tile):
        for c in range(0, w, tile):
            if one_bit:
                tiles.append(b"\xff" * (tile * tile // 8))
                continue
            block = np.zeros((tile, tile), dtype=image.dtype)
            part = image[r : r + tile, c : c + tile]
            block[: part.shape[0], : part.shape[1]] = part
            if predictor == 2:
                block = np.diff(block, axis=1, prepend=np.zeros((tile, 1), block.dtype)).astype(block.dtype)
            raw = block.astype(image.dtype.newbyteorder(order)).tobytes()
            tiles.append(zlib.compress(raw) if compression == 8 else raw)
    return tiles


def fetcher(blob: bytes, log: list | None = None):
    def fetch(url, start, end):
        if log is not None:
            log.append((start, end))
        return blob[start : end + 1]

    return fetch
