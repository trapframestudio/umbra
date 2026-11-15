"""
Microsoft cabinet (.cab) files, stored or MSZIP: enough to take the game's
runtime DLLs out of Microsoft's own downloads (vc_redist's bundles, the Windows
SDK's cabinets). Not LZX or Quantum (raises CabError).

    for off in find(data):          # a .cab, or an .exe with cabinets inside
        cab = Cab(data, off)
        cab.names(); cab.read(name)
"""
import struct
import zlib

SIG = b"MSCF\0\0\0\0"
STORED, MSZIP = 0, 1
METHODS = {0: "stored", 1: "MSZIP", 2: "Quantum", 3: "LZX"}


class CabError(Exception):
    pass


def find(data, start=0):
    """Where cabinets start in data (each a whole cabinet, version 1.3)."""
    out = []
    i = start
    while True:
        i = data.find(SIG, i)
        if i < 0 or i + 36 > len(data):
            return out
        size = struct.unpack_from("<I", data, i + 8)[0]
        minor, major = data[i + 24], data[i + 25]
        if (major, minor) == (1, 3) and 36 <= size <= len(data) - i:
            out.append(i)
            i += size
        else:
            i += 4


class Cab:
    def __init__(self, data, offset=0):
        self.data = data
        self.base = offset
        try:
            self._parse()
        except (struct.error, IndexError, UnicodeDecodeError) as e:
            raise CabError("not a readable cabinet: %s" % e)
        self._folders = {}

    def _parse(self):
        d, b = self.data, self.base
        if d[b:b + 8] != SIG:
            raise CabError("not a cabinet")
        self.size, files_off = struct.unpack_from("<I4xI", d, b + 8)
        n_folders, n_files, flags = struct.unpack_from("<HHH", d, b + 26)
        p = b + 36
        self.res_folder = self.res_data = 0
        if flags & 4:
            res_header, self.res_folder, self.res_data = struct.unpack_from("<HBB", d, p)
            p += 4 + res_header
        for bit in (1, 2):                              # previous / next cabinet names
            if flags & bit:
                for _ in range(2):
                    p = d.index(b"\0", p) + 1
        self.folders = []
        for _ in range(n_folders):
            start, blocks, method = struct.unpack_from("<IHH", d, p)
            self.folders.append((b + start, blocks, method))
            p += 8 + self.res_folder
        self.files = {}
        self.order = []
        p = b + files_off
        for _ in range(n_files):
            size, at, folder, _date, _time, attrs = struct.unpack_from("<IIHHHH", d, p)
            end = d.index(b"\0", p + 16)
            raw = bytes(d[p + 16:end])
            name = raw.decode("utf-8" if attrs & 0x80 else "latin-1")
            p = end + 1
            if folder >= 0xFFFD:
                raise CabError("%s continues in another cabinet" % name)
            self.files[name] = (folder, at, size)
            self.order.append(name)

    def names(self):
        return list(self.order)

    def method(self, name):
        return METHODS.get(self.folders[self.files[name][0]][2] & 15, "?")

    def _folder(self, i):
        if i in self._folders:
            return self._folders[i]
        start, blocks, method = self.folders[i]
        kind = method & 15
        if kind not in (STORED, MSZIP):
            raise CabError("compressed with %s, which this can't unpack" % METHODS.get(kind, kind))
        out = bytearray()
        p = start
        for _ in range(blocks):
            n, plain = struct.unpack_from("<HH", self.data, p + 4)
            p += 8 + self.res_data
            blk = self.data[p:p + n]
            p += n
            if kind == STORED:
                out += blk
                continue
            if blk[:2] != b"CK":
                raise CabError("damaged MSZIP block")
            # each block is its own deflate stream, which may refer back to the last 32 KB
            z = zlib.decompressobj(-15, zdict=bytes(out[-32768:])) if out else zlib.decompressobj(-15)
            try:
                got = z.decompress(bytes(blk[2:])) + z.flush()
            except zlib.error as e:
                raise CabError("damaged MSZIP block: %s" % e)
            if len(got) != plain:
                raise CabError("damaged MSZIP block (%d bytes, not %d)" % (len(got), plain))
            out += got
        self._folders[i] = bytes(out)
        return self._folders[i]

    def read(self, name):
        folder, at, size = self.files[name]
        data = self._folder(folder)[at:at + size]
        if len(data) != size:
            raise CabError("%s is cut short" % name)
        return data


def build(files, method=MSZIP, block=32768):
    """A cabinet holding files ({name: bytes}) in one folder, stored or MSZIP (each block
    its own deflate stream that may refer back into the 32 KB before it). For the
    self-test and the tests."""
    data = b"".join(files.values())
    blocks = []
    for i in range(0, len(data), block) or [0]:
        chunk = data[i:i + block]
        if method == MSZIP:
            hist = data[max(0, i - 32768):i]
            z = zlib.compressobj(9, zlib.DEFLATED, -15, zdict=hist) if hist else zlib.compressobj(9, zlib.DEFLATED, -15)
            packed = b"CK" + z.compress(chunk) + z.flush()
        else:
            packed = chunk
        blocks.append((packed, len(chunk)))
    entries, at = b"", 0
    for name, b in files.items():
        entries += struct.pack("<IIHHHH", len(b), at, 0, 0, 0, 0x20) + name.encode("latin-1") + b"\0"
        at += len(b)
    files_off = 36 + 8
    data_off = files_off + len(entries)
    body = b"".join(struct.pack("<IHH", 0, len(p), n) + p for p, n in blocks)
    head = struct.pack("<4sIIIIIBBHHHHH", b"MSCF", 0, data_off + len(body), 0, files_off, 0, 3, 1, 1, len(files),
                       0, 0, 0)
    return head + struct.pack("<IHH", data_off, len(blocks), method) + entries + body


def walk(data, depth=2):
    """Every file in data's cabinets, and in cabinets inside those (depth levels):
    [(name, bytes)]. Skips cabinets this can't unpack."""
    out = []
    for off in find(data):
        try:
            cab = Cab(data, off)
            for n in cab.names():
                b = cab.read(n)
                out.append((n, b))
                if depth > 0 and b[:8] == SIG:
                    out.extend(walk(b, depth - 1))
        except CabError:
            continue
    return out
