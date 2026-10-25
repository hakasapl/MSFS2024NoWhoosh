"""Print .spb files as readable XML-ish trees, for finding things worth patching.

    python tools/spbdump.py <file.spb>... [--sim-dir DIR]
"""
import argparse
import glob
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import spb  # noqa: E402
from build import find_sim_dir  # noqa: E402


def render(flow, codec, records, depth, out):
    for r in records:
        if r.sym == 0:
            continue
        guid = flow.symbols[r.sym][0]
        name = flow.defs.name(guid) or "UNK_%s" % uuid.UUID(bytes_le=guid)
        pad = "  " * depth
        if r.children is not None:
            out.append("%s<%s>" % (pad, name))
            render(flow, codec, r.children, depth + 1, out)
            out.append("%s</%s>" % (pad, name))
            continue
        if flow.defs.is_text(guid):
            value = codec.decode(r.raw)
            value = value if value is not None else "<%d bytes, beyond keystream>" % len(r.raw)
        elif len(r.raw) == 16:
            value = "{%s}" % str(uuid.UUID(bytes_le=r.raw)).upper()
        else:
            value = r.raw.hex()
        out.append("%s<%s>%s</%s>" % (pad, name, value, name))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--sim-dir")
    args = ap.parse_args()
    sim_dir = args.sim_dir or find_sim_dir()
    defs = spb.PropDefs(os.path.join(sim_dir, "Propdefs"))
    # Keystream from every flow file in the install gives the most known plaintext.
    codec = spb.TextCodec(spb.recover_keystream(
        glob.glob(os.path.join(sim_dir, "Packages", "**", "*.spb"), recursive=True), defs))
    for path in args.files:
        flow = spb.SpbFile.load(path, defs)
        out = []
        render(flow, codec, flow.records, 0, out)
        print("<!-- %s -->" % path)
        print("\n".join(out))


if __name__ == "__main__":
    main()
