// Runs inside the in-flight UI (MasterPage_InGame). Camera add-ons such as ChasePlane switch
// to the free six-DoF camera (CAMERA STATE 7) when the pause menu opens, and nothing switches
// back on resume once the zoom-out is gone, which keeps the cockpit sound mix off until the
// view changes. When the pause menu has stayed closed for a moment, this asks the game to
// catch the plane if the camera is the free camera. Catching the plane hands back to the
// normal camera, as the stock zoom-in on resume always does.
// It runs from an SVG <img onerror>; see build.py for how it's injected.
// __STORE_EXPORT__ is replaced at build time with lib.js's export name for the UI data store.
(function () {
    var FREE_CAMERA = 7;
    // A catch that starts while the game is pausing again turns into a slow fly-around, so
    // only act once the menu has stayed closed this long (pressing Escape repeatedly reopens
    // it sooner, which cancels).
    var SETTLE_MS = 400;
    // Keep looking a little longer in case the add-on switches cameras late.
    var WATCH_MS = 1500;
    var RETRY_MS = 100;

    // One watcher per UI view, however often the in-flight page is rebuilt.
    if (window.noWhooshCameraFix) {
        return;
    }
    window.noWhooshCameraFix = true;

    import('/Global/lib.js').then(function (lib) {
        var camera = lib.__STORE_EXPORT__.Game.InGamePanelCamera;
        function cameraState() {
            return camera.FetchCurrentCameraState.request().then(function () {
                return camera.CurrentCameraState();
            });
        }

        function menuOpen() {
            return !!document.querySelector('[name=MasterPage_Pause]');
        }

        // Bumped on every menu open or close; a pending check only acts if it's still current.
        var generation = 0;
        var wasOpen = menuOpen();

        function check(gen, deadline) {
            if (gen !== generation || menuOpen()) {
                return;
            }
            // Quitting to the main menu also closes the pause menu; leave that alone.
            if (document.querySelector('[name=MasterPage]')) {
                return;
            }
            cameraState().then(function (state) {
                if (gen !== generation || menuOpen()) {
                    return;
                }
                if (state === FREE_CAMERA) {
                    engine.trigger('LAUNCH_FLOW_EVENT_TO_GLOBAL_FLOW_FROM_VIEW', 'CAMERA_CATCH_PLANE', 'NoWhoosh');
                } else if (Date.now() < deadline) {
                    setTimeout(function () { check(gen, deadline); }, RETRY_MS);
                }
            });
        }

        new MutationObserver(function () {
            var open = menuOpen();
            if (open === wasOpen) {
                return;
            }
            wasOpen = open;
            var gen = ++generation;
            if (!open) {
                setTimeout(function () { check(gen, Date.now() + WATCH_MS); }, SETTLE_MS);
            }
        }).observe(document.body, { childList: true, subtree: true });
    });
})();
