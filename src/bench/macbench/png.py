"""A small PNG reader and writer (8-bit gray, gray+alpha, RGB, RGBA; not interlaced), standard library only.
simctl's screenshots are of that kind. No Pillow."""
import hashlib
import struct
import zlib


class Image:
    def __init__(self, width, height, channels, rows):
        self.w, self.h, self.ch, self.rows = width, height, channels, rows  # rows: list of bytes

    def px(self, x, y):
        """(r, g, b) of a pixel."""
        r = self.rows[y]
        i = x * self.ch
        if self.ch >= 3:
            return r[i], r[i + 1], r[i + 2]
        return r[i], r[i], r[i]

    def luma(self, x, y):
        r, g, b = self.px(x, y)
        return (r + g + b) // 3

    def pixel_hash(self):
        """sha256 over the RGB values of every pixel (alpha ignored), with the size mixed in: two screenshots
        have the same hash only when every pixel is the same."""
        h = hashlib.sha256()
        h.update(struct.pack(">II", self.w, self.h))
        for y in range(self.h):
            r = self.rows[y]
            if self.ch == 3:
                h.update(r)
            elif self.ch == 4:
                h.update(bytes(v for i, v in enumerate(r) if i % 4 != 3))
            elif self.ch == 2:
                h.update(bytes(r[i] for i in range(0, len(r), 2)))
            else:
                h.update(r)
        return h.hexdigest()


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def read_png(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, w = 8, [], None
    while pos + 8 <= len(data):
        n, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if typ == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or interlace != 0 or ctype not in (0, 2, 4, 6):
                raise ValueError("unsupported PNG (need 8-bit, not interlaced, gray/RGB/RGBA)")
            ch = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
    if w is None:
        raise ValueError("no IHDR")
    raw = zlib.decompress(b"".join(idat))
    stride = w * ch
    rows, prev = [], bytearray(stride)
    p = 0
    for _ in range(h):
        ft = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        if ft == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                left = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                left = line[i - ch] if i >= ch else 0
                ul = prev[i - ch] if i >= ch else 0
                line[i] = (line[i] + _paeth(left, prev[i], ul)) & 255
        elif ft != 0:
            raise ValueError("bad PNG filter %d" % ft)
        rows.append(bytes(line))
        prev = line
    return Image(w, h, ch, rows)


def write_png(img_or_rows, width=None, height=None, channels=3):
    """PNG bytes from an Image, or from rows (a list of bytes, each width*channels long). For tests and fixtures."""
    if isinstance(img_or_rows, Image):
        rows, width, height, channels = img_or_rows.rows, img_or_rows.w, img_or_rows.h, img_or_rows.ch
    else:
        rows = img_or_rows
    ctype = {1: 0, 2: 4, 3: 2, 4: 6}[channels]

    def chunk(t, b):
        c = struct.pack(">I", len(b)) + t + b
        return c + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, ctype, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
