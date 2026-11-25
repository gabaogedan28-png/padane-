# PADANE — Assistive Device Safety Platform

Flask backend for the PADANE assistive walking-stick / SOS tracking system:
device pairing, live GNSS location, caregiver alerts, an admin console, and
an audit trail.

## Project structure

```
padane/
├── app.py                     # Flask app: routes, API, DB helpers, geocoding
├── instance/
│   └── database.db            # SQLite DB (created on first run, git-ignored)
├── scripts/                   # One-off maintenance / debug scripts
│   ├── check_db.py            # Inspect the devices table schema
│   ├── debug_db.py            # List every device serial in the DB
│   └── register_device.py     # Manually register a test device
├── static/
│   ├── css/
│   │   └── theme.css          # Shared design system (see below)
│   ├── js/                    # (reserved for future shared JS)
│   └── img/                   # (reserved for icons / favicon)
└── templates/
    ├── auth/
    │   ├── login.html
    │   └── register.html
    ├── dashboard/              # End-user pages
    │   ├── index.html          # Overview / live tracking
    │   ├── trigger.html        # Live device map
    │   ├── nearest-help.html
    │   ├── alert-caregivers.html
    │   ├── server-ping.html
    │   ├── support-help.html
    │   ├── account-settings.html
    │   ├── user_setting.html
    │   └── real_alert.html
    └── admin/                  # Admin-only pages
        ├── admin.html
        ├── audit_trail.html
        ├── user_profiles.html
        └── device_management.html
```

Routes are unchanged (`/login.html`, `/index.html`, `/admin.html`, …) — only
where the template *files* live on disk changed, grouped by feature instead
of sitting flat in one folder. Every in-page link, `fetch()` call, and
`window.location.href` redirect in the original app already used these flat
paths, so nothing else needed to change for the app to keep working.

## Running it

```bash
pip install flask requests werkzeug
python app.py
```

The database now lives in `instance/database.db` (Flask's standard location
for local, non-versioned data) instead of a bare `database.db` next to the
code. It's created automatically on first run, along with a seeded default
admin (`admin` / `admin123` — change this before any real deployment).

The maintenance scripts in `scripts/` resolve that same path automatically,
so `python scripts/check_db.py` etc. still work from anywhere.

## Design system — "Warm ink & amber"

The old UI used a generic dark-sidebar-plus-violet SaaS template. PADANE is
a safety product used by elderly and disabled users and the caregivers
looking after them, so the redesign leans calmer and warmer instead of
"techy": a paper-toned background, a deep pine-teal brand color, and a
single reserved amber accent for anything that signals attention — SOS
alerts, live status, open tickets.

- **Ink / paper neutrals** — warm parchment surfaces (`#F7F3EA`), warm sand
  borders, and a near-black warm ink for text, instead of cool slate greys.
- **Brand** — deep pine teal (`#1F5C56`) replaces the violet used for every
  primary action, active nav item, and link.
- **The one accent** — amber (`#D98E2B`) is reserved for alerts and signal
  states, echoing the SOS light on the physical device.
- **Typography** — [Fraunces](https://fonts.google.com/specimen/Fraunces),
  a warm serif with real personality, for headings and display text;
  Inter stays for body copy; IBM Plex Mono for serials, timestamps, and
  coordinates.
- **Signature element** — a small amber "beacon" pulses on the brand mark
  in the sidebar and on the auth screens (`static/css/theme.css`), a quiet
  nod to the device's own SOS light. It's pure CSS, riding on classes that
  already existed on every page, and it respects `prefers-reduced-motion`.
- **Accessibility** — a visible 3px amber focus ring on every interactive
  element, since this is a product elderly and disabled users depend on.

All of this lives in `static/css/theme.css`, loaded on every page after its
own inline styles so it can safely refine typography and add the shared
motifs without fighting page-specific rules. The palette itself was applied
directly to each page's existing styles (rather than introduced only via
theme.css) so every card, badge, table, and status color across all 15
pages shares the same system — not just the shell.

## Live theme customizer

Every page now reads its 7 core colors as CSS custom properties
(`--pd-brand`, `--pd-brand-dark`, `--pd-amber`, `--pd-paper`, `--pd-sidebar`,
`--pd-ink`, `--pd-ink-muted`) with the current palette as the fallback, e.g.
`var(--pd-brand, #1F5C56)`. That makes the whole app re-themeable at
runtime without touching any HTML.

**Where it's edited:** Account Settings → **Appearance** card. Color
pickers for all 7 tokens, with a live preview as you drag.

- **Everyone** can save a personal look (`Save my appearance`) or clear it
  (`Reset to default`).
- **Admins** get one more button (`Set as default for all users`) that
  publishes a new site-wide default. Anyone who hasn't personally
  overridden a given color inherits it automatically.

**How the layers combine** — three layers, each optional:
`hardcoded defaults → admin's site default → the user's own override`.
Any color left unset at a layer just falls through to the next one, so an
admin only ever has to save the colors they actually changed, and so does
each user.

**Backend:** `app_theme` (one row, the site default) and `user_theme` (one
row per user who's customized their own) in `instance/database.db`.
Endpoints: `GET /api/theme/effective` (public — merges all layers for
whoever's asking, used by every page on load), `GET /api/theme/mine`
(the raw layers, for populating the settings form), `POST`/`DELETE
/api/theme/personal`, and `POST`/`DELETE /api/theme/global` (admin-only).
Every color is validated server-side as `#RRGGBB` before it's stored.

**Frontend:** `static/js/theme-loader.js`, included as early as possible in
every page's `<head>`. It applies whatever was cached in `localStorage`
immediately (no flash of the old colors), then fetches
`/api/theme/effective` in the background and re-applies + re-caches it.
