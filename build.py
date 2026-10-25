"""Build the NoWhoosh Community package from the installed sim's own files.

NoWhoosh makes the Escape pause menu open in place: no fast zoom-out to the globe, no
"whoosh", and resuming continues from the same view. It overrides some of Asobo's menu flow
files (asobo-flow) and one UI component (fs-base-ui). Those files change with sim updates,
so they are never stored here: this script reads the current ones from the install, applies
the patches below and writes the package. Re-run it after a sim update.

    python build.py            build into ./package/NoWhoosh
    python build.py --deploy   build, then copy it into <Community>/NoWhoosh
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys
import time
from xml.sax.saxutils import quoteattr

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
import spb  # noqa: E402

PACKAGE_NAME = "NoWhoosh"
HERE = os.path.dirname(os.path.abspath(__file__))

# Sent instead of a flow event we want to suppress: nothing listens for it.
UNHEARD = "NOWHOOSH_UNHEARD"


class Unreference:
    """Replacement value: drop ObjectReferences to the object with this id instead, so
    whatever listed it (an ExecuteListAction, a state's actions) no longer runs it. With
    `from_state`, only inside From/ToStateCases whose StateList names that state."""

    def __init__(self, from_state=None):
        self.from_state = from_state


UNREFERENCE = Unreference()


class InStateCase:
    """Replacement value: rename to `value`, but only inside From/ToStateCases whose
    StateList names `from_state`."""

    def __init__(self, value, from_state):
        self.value = value
        self.from_state = from_state


# Each mode's pause state sends the world map to FOCUS_LOCATION_VIEW (the fast zoom-out to the
# globe) and switches the flight path display to the map. Sending UNHEARD instead leaves the
# camera where it was, so the menu opens in place. The Reno and Red Bull race pauses never
# zoom out and are left alone.
PAUSE_CAMERA = ["GOTO_WORLDMAP_FOCUS_LOCATION_VIEW_CODE"]
PAUSE_PATH = ["WORLDMAP_SHOW_3DPATH", "FORCE_WORLDMAP_PATH"]
# The camera "whoosh" (WorldMap_Sound's env_flightplan_cam_opening/_movement) is played by
# the flows themselves, on pause and again on resume.
WHOOSH = {"WwiseCamMovementStart": UNREFERENCE, "WwiseCamMovement": UNREFERENCE}

FLOW_PATCHES = [
    ("Flow/Asobo/WorldMap/%s.spb" % state, "Pause menu (%s): open in place, silently" % state[len("Pause_"):],
     dict({event: UNHEARD for event in events}, **WHOOSH))
    for state, events in [
        ("Pause_Freeflight", PAUSE_CAMERA + PAUSE_PATH),
        ("Pause_Career", PAUSE_CAMERA + PAUSE_PATH),
        ("Pause_Certification", PAUSE_CAMERA + PAUSE_PATH),
        ("Pause_Challenge", PAUSE_CAMERA + PAUSE_PATH),
        ("Pause_Discovery", PAUSE_CAMERA),
        ("Pause_Discovery_Legacy", PAUSE_CAMERA + PAUSE_PATH),
        ("Pause_Activity_KH", PAUSE_CAMERA + PAUSE_PATH),
        ("Pause_Spotlight", PAUSE_CAMERA + PAUSE_PATH),
        ("Pause_Trainings", PAUSE_CAMERA + PAUSE_PATH),
    ]
] + [
    # Resuming goes through WorldMap_TransitionToGame. Its case for coming from a pause state
    # replays the whoosh and sends CAMERA_CATCH_PLANE to fly the map camera back from the
    # globe. The camera never left, so raise EXIT_CAMERA_TRANSITION instead, which ends the
    # transition the way a finished catch would. The start-of-flight case is unchanged.
    ("Flow/Asobo/WorldMap/WorldMap_Transition_To_Game.spb", "Resume from pause: no whoosh, no camera catch",
     dict({sound: Unreference(from_state="Pause_Freeflight") for sound in WHOOSH},
          CAMERA_CATCH_PLANE=InStateCase("EXIT_CAMERA_TRANSITION", from_state="Pause_Freeflight"))),
]

# Scripts run inside a cooked UI component (fs-base-ui/cookedcomps): (component, script in
# src/, description). The script ships wrapped in an SVG whose <img onerror> runs it, and an
# <icon-element> that loads the SVG is appended to the component. The UI's script bundles
# (html_ui/Global/pkg/*.bundle.js) load during boot, before Community packages are mounted,
# so they can't be overridden; behavior changes go through scripts like these instead.
SCRIPTS = [
    ("MasterPage_InGame", "pause-camera-fix.js",
     "In flight: if a camera add-on leaves the free camera after a pause, catch the plane"),
]


def find_sim_dir():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam = winreg.QueryValueEx(key, "SteamPath")[0]
    except OSError:
        return None
    libraries = [steam]
    vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
    if os.path.exists(vdf):
        with open(vdf, encoding="utf-8", errors="replace") as f:
            libraries += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
    for lib in libraries:
        candidate = os.path.join(lib, "steamapps", "common", "MSFS2024")
        if os.path.exists(os.path.join(candidate, "FlightSimulator2024.exe")):
            return os.path.normpath(candidate)
    return None


def find_community_dir():
    appdata = os.environ.get("APPDATA", "")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    for cfg in (os.path.join(appdata, "Microsoft Flight Simulator 2024", "UserCfg.opt"),
                os.path.join(localappdata, "Packages", "Microsoft.Limitless_8wekyb3d8bbwe",
                             "LocalCache", "UserCfg.opt")):
        if os.path.exists(cfg):
            with open(cfg, encoding="utf-8", errors="replace") as f:
                m = re.search(r'^InstalledPackagesPath\s+"([^"]+)"', f.read(), re.M)
            if m:
                return os.path.join(m.group(1), "Community")
    return None


def filetime(path):
    return int((os.path.getmtime(path) + 11644473600) * 10_000_000)


def case_states(flow, case, codec):
    """State names listed in a From/ToStateCase's StateList."""
    names = set()
    for child in case.children:
        if child.children is not None and flow.defs.name(flow.symbols[child.sym][0]) == "StateList":
            names.update(codec.decode(r.raw) for r in flow.iter_records(child.children) if r.children is None)
    return names


def drop_references(flow, records, unreference, codec, counts, states=frozenset()):
    """Remove ObjectReference records whose id is a key of `unreference`, at any depth.
    `states` holds the StateList names of the enclosing state case, if any."""
    kept = []
    for rec in records:
        if rec.children is not None:
            name = flow.defs.name(flow.symbols[rec.sym][0])
            if name == "ObjectReference":
                ref = [codec.decode(c.raw) for c in rec.children
                       if c.children is None and flow.defs.name(flow.symbols[c.sym][0]) == "id"]
                rule = unreference.get(ref[0]) if ref else None
                if rule and (rule.from_state is None or rule.from_state in states):
                    counts[ref[0]] += 1
                    continue
            inner = case_states(flow, rec, codec) if name in ("FromStateCase", "ToStateCase") else states
            rec.children = drop_references(flow, rec.children, unreference, codec, counts, inner)
        kept.append(rec)
    return kept


def text_records_with_states(flow, records, codec, states=frozenset()):
    """Yield (TEXT record, StateList names of its enclosing state case)."""
    for rec in records:
        if rec.children is None:
            if flow.defs.is_text(flow.symbols[rec.sym][0]):
                yield rec, states
            continue
        name = flow.defs.name(flow.symbols[rec.sym][0])
        inner = case_states(flow, rec, codec) if name in ("FromStateCase", "ToStateCase") else states
        yield from text_records_with_states(flow, rec.children, codec, inner)


def patch_file(src, replacements, defs, codec):
    data = open(src, "rb").read()
    flow = spb.SpbFile(data, defs)
    counts = dict.fromkeys(replacements, 0)
    unreference = {k: v for k, v in replacements.items() if isinstance(v, Unreference)}
    flow.records = drop_references(flow, flow.records, unreference, codec, counts)
    for rec, states in text_records_with_states(flow, flow.records, codec):
        value = codec.decode(rec.raw)
        rule = replacements.get(value)
        if rule is None or isinstance(rule, Unreference):
            continue
        if isinstance(rule, InStateCase):
            if rule.from_state not in states:
                continue
            rule = rule.value
        rec.raw = codec.encode(rule)
        counts[value] += 1
    missing = [k for k, n in counts.items() if n == 0]
    if missing:
        raise SystemExit("%s: expected strings not found (sim update changed the flow?): %s"
                         % (src, ", ".join(missing)))
    out = flow.to_bytes()
    # Re-parse the result so a broken encoding fails here rather than in the sim.
    check = spb.SpbFile(out, defs)
    renamed = {k for k, v in replacements.items() if isinstance(v, str)}
    remaining = [codec.decode(r.raw) for r in check.text_records() if codec.decode(r.raw) in renamed]
    assert not remaining, remaining
    return out, counts


def cooked_components_by_name(ui_pkg):
    """Map each cooked component's root element name to its .html path."""
    names = {}
    for path in glob.glob(os.path.join(ui_pkg, "cookedcomps", "*.html")):
        with open(path, "rb") as f:
            m = re.search(rb'<ui-resource-element\b[^>]*?\bname="([^"]*)"', f.read(4096))
        if m:
            names.setdefault(m.group(1).decode(), []).append(path)
    return names


def store_export_name(ui_pkg):
    """lib.js's (minified) export name for the UI data store, as base.bundle.js imports it.
    Injected scripts reach the store with import('/Global/lib.js').then(m => m[<name>])."""
    with open(os.path.join(ui_pkg, "html_ui", "Global", "pkg", "base.bundle.js"), encoding="utf-8") as f:
        bundle = f.read()
    aliases = set(re.findall(r"\b([\w$]+)\.Game\.Aircraft\.Configurator\b", bundle))
    imports = re.search(r'import\{([^}]*)\}from"\.\./lib\.js"', bundle)
    names = dict((alias, export) for export, alias in
                 re.findall(r"([\w$]+) as ([\w$]+)", imports.group(1))) if imports else {}
    exports = {names[a] for a in aliases if a in names}
    if len(exports) != 1:
        raise SystemExit("can't find the UI data store export in lib.js (sim update changed the bundle?)")
    return exports.pop()


def script_svg(js):
    """Wrap a script in an SVG that runs it (with `this` as the <img>) when loaded."""
    # Drop comment lines and keep newlines as entities, since attribute values may be
    # whitespace-normalized.
    code = "\n".join(line for line in js.splitlines() if not line.strip().startswith("//"))
    attr = quoteattr(code.strip(), {"\n": "&#10;"})
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1">\n'
            '  <foreignObject width="1" height="1">\n'
            '    <img src onerror=%s />\n'
            '  </foreignObject>\n'
            '</svg>\n' % attr).encode()


def inject_icon(html, url):
    """Append an invisible <icon-element> loading `url` as the root element's last child."""
    end = html.rfind(b"</ui-resource-element>")
    if end < 0:
        raise SystemExit("cooked component has no root element")
    icon = b'<icon-element data-url="%s" class="absolute" style="width: 0px; height: 0px;"></icon-element>' % url.encode()
    return html[:end] + icon + html[end:]


def write(pkg_dir, rel, data):
    dst = os.path.join(pkg_dir, *rel.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(data)


def build(sim_dir, out_root):
    defs = spb.PropDefs(os.path.join(sim_dir, "Propdefs"))
    flow_pkg = os.path.join(sim_dir, "Packages", "asobo-flow")
    ui_pkg = os.path.join(sim_dir, "Packages", "fs-base-ui")
    codec = spb.TextCodec(spb.recover_keystream(
        glob.glob(os.path.join(flow_pkg, "**", "*.spb"), recursive=True), defs))

    pkg_dir = os.path.join(out_root, PACKAGE_NAME)
    if os.path.exists(pkg_dir):
        shutil.rmtree(pkg_dir)
    os.makedirs(pkg_dir)

    for rel, descr, replacements in FLOW_PATCHES:
        out, counts = patch_file(os.path.join(flow_pkg, *rel.split("/")), replacements, defs, codec)
        write(pkg_dir, rel, out)
        print("  %-48s %s" % (rel.split("/")[-1], descr))
        for old, n in counts.items():
            rule = replacements[old]
            new = ("(unreferenced)" if isinstance(rule, Unreference)
                   else "%s (from %s)" % (rule.value, rule.from_state) if isinstance(rule, InStateCase)
                   else rule)
            print("      %s -> %s (x%d)" % (old, new, n))

    components = cooked_components_by_name(ui_pkg)
    for component, script, descr in SCRIPTS:
        paths = components.get(component, [])
        if len(paths) != 1:
            raise SystemExit("expected one cooked component named %s, found %d" % (component, len(paths)))
        rel = "html_ui/%s/%s.svg" % (PACKAGE_NAME, os.path.splitext(script)[0])
        with open(os.path.join(HERE, "src", script), encoding="utf-8") as f:
            js = f.read()
        if "__STORE_EXPORT__" in js:
            js = js.replace("__STORE_EXPORT__", store_export_name(ui_pkg))
        write(pkg_dir, rel, script_svg(js))
        with open(paths[0], "rb") as f:
            html = f.read()
        component_rel = "cookedcomps/" + os.path.basename(paths[0])
        write(pkg_dir, component_rel, inject_icon(html, "coui://" + rel))
        # Ship the component's untouched .json too so the pair always comes from one build.
        with open(paths[0][:-len(".html")] + ".json", "rb") as f:
            write(pkg_dir, component_rel[:-len(".html")] + ".json", f.read())
        print("  %-48s %s" % (component, descr))

    content = []
    for root, _, files in os.walk(pkg_dir):
        for name in sorted(files):
            path = os.path.join(root, name)
            content.append({"path": os.path.relpath(path, pkg_dir).replace("\\", "/"),
                            "size": os.path.getsize(path),
                            "date": filetime(path)})
    with open(os.path.join(pkg_dir, "layout.json"), "w", newline="\n") as f:
        json.dump({"content": content}, f, indent=2)

    with open(os.path.join(flow_pkg, "manifest.json"), encoding="utf-8") as f:
        flow_manifest = json.load(f)
    manifest = {
        "dependencies": [],
        "content_type": "MISC",
        "title": "NoWhoosh",
        "manufacturer": "",
        "creator": "hakasapl",
        "package_version": "0.1.0",
        "minimum_game_version": flow_manifest.get("minimum_game_version", "1.0.0"),
        "release_notes": {"neutral": {
            "LastUpdate": "Built %s against asobo-flow %s" % (
                time.strftime("%Y-%m-%d"), flow_manifest.get("package_version", "?")),
            "OlderHistory": ""}},
        "total_package_size": "%020d" % sum(c["size"] for c in content),
    }
    with open(os.path.join(pkg_dir, "manifest.json"), "w", newline="\n") as f:
        json.dump(manifest, f, indent=2)
    return pkg_dir


def deploy(pkg_dir, community_dir):
    target = os.path.join(community_dir, PACKAGE_NAME)
    # Only ever replace our own package folder.
    assert os.path.basename(target) == PACKAGE_NAME and os.path.dirname(target) == community_dir
    if os.path.exists(target):
        shutil.rmtree(target)
    shutil.copytree(pkg_dir, target)
    return target


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim-dir", help="MSFS 2024 install folder (default: auto-detect Steam install)")
    ap.add_argument("--community-dir", help="Community folder (default: from UserCfg.opt)")
    ap.add_argument("--deploy", action="store_true", help="copy the built package into the Community folder")
    args = ap.parse_args()

    sim_dir = args.sim_dir or find_sim_dir()
    if not sim_dir or not os.path.isdir(os.path.join(sim_dir, "Packages", "asobo-flow")):
        raise SystemExit("MSFS 2024 install not found; pass --sim-dir")
    print("Sim install: %s" % sim_dir)
    pkg_dir = build(sim_dir, os.path.join(HERE, "package"))
    print("Built: %s" % pkg_dir)

    if args.deploy:
        community_dir = os.path.normpath(args.community_dir or find_community_dir() or "")
        if not os.path.isdir(community_dir):
            raise SystemExit("Community folder not found; pass --community-dir")
        print("Deployed: %s" % deploy(pkg_dir, community_dir))


if __name__ == "__main__":
    main()
