/**
 * PADANE theme loader — runs on every page.
 *
 * 1. Applies whatever theme was cached from the last successful load
 *    immediately, so there's no flash of the default colors while the
 *    network request is in flight.
 * 2. Fetches the effective theme (defaults -> site default -> the
 *    current user's personal override, in that order) and re-applies +
 *    re-caches it, in case anything changed since the last visit.
 *
 * The values themselves are just CSS custom properties — every page's
 * own stylesheet already reads colors as var(--pd-brand, #1F5C56) etc.,
 * so setting these on <html> is enough to re-theme the whole page.
 */
(function () {
    "use strict";

    var CACHE_KEY = "padaneThemeCache";
    var VAR_MAP = {
        brand: "--pd-brand",
        brand_dark: "--pd-brand-dark",
        amber: "--pd-amber",
        paper_bg: "--pd-paper",
        sidebar_ink: "--pd-sidebar",
        text_ink: "--pd-ink",
        muted_text: "--pd-ink-muted",
    };

    function applyTheme(theme) {
        if (!theme) return;
        var root = document.documentElement;
        Object.keys(VAR_MAP).forEach(function (key) {
            if (theme[key]) root.style.setProperty(VAR_MAP[key], theme[key]);
        });
    }

    function readCache() {
        try {
            return JSON.parse(localStorage.getItem(CACHE_KEY) || "null");
        } catch (e) {
            return null;
        }
    }

    function writeCache(theme) {
        try {
            localStorage.setItem(CACHE_KEY, JSON.stringify(theme));
        } catch (e) {
            /* private browsing / storage full — theme still applies, just isn't cached */
        }
    }

    // Step 1: instant paint from cache
    applyTheme(readCache());

    // Step 2: refresh from the server
    fetch("/api/theme/effective")
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (theme) {
            if (!theme) return;
            applyTheme(theme);
            writeCache(theme);
        })
        .catch(function () {
            /* offline or logged out mid-request — cached/default theme stays visible */
        });

    // Exposed so the Account Settings "Appearance" panel can re-apply live
    // previews and refresh the cache the instant a save succeeds.
    window.PadaneTheme = { apply: applyTheme, cache: writeCache, VAR_MAP: VAR_MAP };
})();
