# MSFS2024NoWhoosh
MSFS2024 plugin that disables the "whoosh" animation and sound when pausing the game with esc

With **NoWhoosh** installed, pressing Escape in flight opens the pause menu right where you
are, over the paused view, instead of zooming the camera out to the globe. Resuming
continues from the same view. The camera "whoosh" sound is gone in both directions.

It covers the pause menus of Free Flight, career, certification, challenges, discovery,
activities, spotlight and trainings. The Reno and Red Bull race pauses never zoomed out and
are unchanged.

## Build and install

Requires Python 3 and a Steam install of MSFS 2024.

```bash
python build.py --deploy
```

This builds `package/NoWhoosh` from your installed sim's files and copies it to
`<Community>/NoWhoosh` (the Community folder is read from `UserCfg.opt`). Restart the sim
afterwards. Pass `--sim-dir` or `--community-dir` if they aren't found automatically, or
leave out `--deploy` to only build.

**Re-run it after every sim update.** The package replaces whole Asobo flow files, which are
generated from the current install rather than stored here, so a stale build could override
newer Asobo changes. The build fails loudly if anything it patches no longer exists.

To uninstall, delete `<Community>/NoWhoosh`.

## How it works

The sim's menus are driven by Asobo's flow files (`asobo-flow` package, compiled `.spb`).
`tools/spb.py` reads and writes them; text in them is XOR-obfuscated with a fixed keystream
that the tool recovers from the files themselves.

- **Pause:** each mode's pause state sends `GOTO_WORLDMAP_FOCUS_LOCATION_VIEW_CODE`, which
  flies the camera out to the globe, and switches the flight path display to the map
  (`WORLDMAP_SHOW_3DPATH`, `FORCE_WORLDMAP_PATH`). NoWhoosh replaces those events with one
  nothing listens to, and drops the references to the camera sounds `WwiseCamMovementStart`
  and `WwiseCamMovement`.
- **Resume:** `WorldMap_TransitionToGame`, entered from a pause state, would replay the sounds
  and send `CAMERA_CATCH_PLANE` to fly the camera back. The camera never left, so NoWhoosh
  drops the sounds there and raises `EXIT_CAMERA_TRANSITION` instead, which ends the
  transition the same way a finished catch does. The start-of-flight transition is untouched.
- **Camera add-ons:** ChasePlane (and possibly other camera add-ons) switches to the free
  six-DoF camera (`CAMERA STATE` 7) when the pause menu opens. Without the zoom back in,
  nothing switches back, and that camera mutes the cockpit sound mix until the view changes.
  `src/pause-camera-fix.js` runs in the in-flight UI (injected into the `MasterPage_InGame`
  component) and watches the pause menu. Once the menu has stayed closed for 400 ms, it raises
  `CAMERA_CATCH_PLANE` if the camera is the free camera, which hands back to the normal camera
  (as the stock resume always does). The delay matters: a catch that starts while the game is
  pausing again turns into a slow fly-around, so reopening the menu sooner (e.g. pressing
  Escape repeatedly) cancels it. Without such an add-on it never has anything to do.

The UI script bundles (`html_ui/Global/pkg/*.bundle.js`) load during boot, before Community
packages are mounted, so they can't be overridden. Scripts are injected into cooked UI
components instead, wrapped in an SVG whose `<img onerror>` runs them, and reach the UI data
store with `import('/Global/lib.js')`.

## Files

- `build.py`: patch list, build and deploy
- `src/pause-camera-fix.js`: script injected into the in-flight UI
- `tools/spb.py`: reads and writes `.spb` (SimProp binary) files
- `tools/spbdump.py`: prints `.spb` files as readable trees, for finding things to patch, e.g.
  `python tools/spbdump.py "<sim>/Packages/asobo-flow/Flow/Asobo/WorldMap/Pause_Freeflight.spb"`
- `tools/coherent.js`: evaluates JavaScript in the running sim's UI through the Coherent
  inspector, which the sim serves on `127.0.0.1:19999` in Developer Mode, e.g.
  `node tools/coherent.js eval Toolbar "SimVar.GetSimVarValue('CAMERA STATE', 'number')"`.
  `node tools/coherent.js list` shows the available views.
