"""Read and write MSFS 2024 SimProp binary (.spb) files.

File layout:
  0x00  26-byte header (magic 0x0014EBAC), constant apart from the symbol count at 0x1A
  0x1E  symbol table: `count` entries of (GUID, int32 size); size -1 means variable length
  ...   record stream: uint32 symbol index, then either `size` raw bytes or, for variable
        records, a uint32 byte length followed by the payload. Set payloads are themselves
        record streams, terminated by symbol 0 (null GUID, size 0).

TEXT values are XOR-obfuscated with a fixed keystream that restarts at every string. The
keystream is recovered from the files themselves: every flow file stores its own name in
a Filename record, which gives known plaintext.
"""
import glob
import os
from collections import Counter
import re
import struct
import uuid

MAGIC = 0x0014EBAC
COUNT_OFFSET = 0x1A
TABLE_OFFSET = 0x1E


class Record:
    __slots__ = ("sym", "children", "raw")

    def __init__(self, sym, children=None, raw=b""):
        self.sym = sym
        self.children = children
        self.raw = raw


class PropDefs:
    """GUID -> (kind, name, type) lookup built from the sim's Propdefs folder."""

    def __init__(self, propdefs_dir):
        self.by_guid = {}
        for path in glob.glob(os.path.join(propdefs_dir, "**", "*.xml"), recursive=True):
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            for m in re.finditer(r"<(PropertyDef|SetDef)\b([^>]*)>", text):
                attrs = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', m.group(2)))
                try:
                    guid = uuid.UUID(attrs["id"]).bytes_le
                except (KeyError, ValueError):
                    continue
                self.by_guid[guid] = (m.group(1), attrs.get("name"), attrs.get("type"))

    def kind(self, guid):
        return self.by_guid.get(guid, (None, None, None))[0]

    def name(self, guid):
        return self.by_guid.get(guid, (None, None, None))[1]

    def is_text(self, guid):
        return self.by_guid.get(guid, (None, None, None))[2] in ("TEXT", "STRING")


class SpbFile:
    def __init__(self, data, defs):
        magic, = struct.unpack_from("<I", data, 0)
        if magic != MAGIC:
            raise ValueError("not an spb file (magic %#x)" % magic)
        self.defs = defs
        self.header = data[:COUNT_OFFSET]
        count, = struct.unpack_from("<I", data, COUNT_OFFSET)
        self.symbols = []
        p = TABLE_OFFSET
        for _ in range(count):
            guid = data[p:p + 16]
            size, = struct.unpack_from("<i", data, p + 16)
            self.symbols.append((guid, size))
            p += 20
        self.records = self._parse(data, p, len(data))

    @classmethod
    def load(cls, path, defs):
        with open(path, "rb") as f:
            return cls(f.read(), defs)

    def _is_set(self, guid, data, start, end):
        kind = self.defs.kind(guid)
        if kind:
            return kind == "SetDef"
        # Unknown definition: treat as a set only if the payload parses cleanly as records.
        p = start
        while p < end:
            if p + 4 > end:
                return False
            sym, = struct.unpack_from("<I", data, p)
            p += 4
            if sym == 0 or sym >= len(self.symbols):
                return sym == 0 and p == end
            size = self.symbols[sym][1]
            if size < 0:
                if p + 4 > end:
                    return False
                n, = struct.unpack_from("<I", data, p)
                p += 4 + n
            else:
                p += size
        return p == end

    def _parse(self, data, start, end):
        records = []
        p = start
        while p < end:
            sym, = struct.unpack_from("<I", data, p)
            p += 4
            if sym >= len(self.symbols):
                raise ValueError("bad symbol index %d at %#x" % (sym, p - 4))
            guid, size = self.symbols[sym]
            if size >= 0:
                records.append(Record(sym, raw=data[p:p + size]))
                p += size
                continue
            n, = struct.unpack_from("<I", data, p)
            p += 4
            if self._is_set(guid, data, p, p + n):
                records.append(Record(sym, children=self._parse(data, p, p + n)))
            else:
                records.append(Record(sym, raw=data[p:p + n]))
            p += n
        if p != end:
            raise ValueError("record stream overran its container")
        return records

    def _serialize(self, records):
        out = bytearray()
        for r in records:
            out += struct.pack("<I", r.sym)
            size = self.symbols[r.sym][1]
            payload = self._serialize(r.children) if r.children is not None else r.raw
            if size >= 0:
                if len(payload) != size:
                    raise ValueError("fixed-size record has wrong length")
            else:
                out += struct.pack("<I", len(payload))
            out += payload
        return bytes(out)

    def to_bytes(self):
        out = bytearray(self.header)
        out += struct.pack("<I", len(self.symbols))
        for guid, size in self.symbols:
            out += guid + struct.pack("<i", size)
        out += self._serialize(self.records)
        return bytes(out)

    def iter_records(self, records=None):
        for r in self.records if records is None else records:
            yield r
            if r.children is not None:
                yield from self.iter_records(r.children)

    def text_records(self):
        for r in self.iter_records():
            if r.children is None and self.defs.is_text(self.symbols[r.sym][0]):
                yield r


class TextCodec:
    def __init__(self, keystream):
        self.ks = keystream

    def _xor(self, b):
        if len(b) > len(self.ks):
            raise ValueError("string longer than recovered keystream (%d > %d)" % (len(b), len(self.ks)))
        return bytes(x ^ y for x, y in zip(b, self.ks))

    def decode(self, raw):
        if len(raw) > len(self.ks):
            return None
        return self._xor(raw).rstrip(b"\0").decode("latin-1")

    def encode(self, text):
        return self._xor(text.encode("latin-1") + b"\0")


def recover_keystream(spb_paths, defs):
    """Recover the keystream from the Filename records of `spb_paths`.

    Each file stores its own basename, so every file contributes known plaintext. A file
    renamed after compiling would contribute garbage, so each byte is a majority vote.
    """
    candidates = []
    for path in spb_paths:
        try:
            spb = SpbFile.load(path, defs)
        except (OSError, ValueError, struct.error):
            continue
        plain = os.path.basename(path).encode("latin-1") + b"\0"
        for r in spb.iter_records():
            if r.children is None and defs.name(spb.symbols[r.sym][0]) == "Filename" and len(r.raw) == len(plain):
                candidates.append(bytes(a ^ b for a, b in zip(r.raw, plain)))
                break
    keystream = bytearray()
    for i in range(max(map(len, candidates), default=0)):
        votes = Counter(c[i] for c in candidates if len(c) > i)
        keystream.append(votes.most_common(1)[0][0])
    return bytes(keystream)
