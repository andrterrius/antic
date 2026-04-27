from __future__ import annotations

import random
from dataclasses import replace

from profiles_store import BrowserProfile


_LOCALES = [
    ("en-US", "America/New_York"),
    ("en-GB", "Europe/London"),
    ("de-DE", "Europe/Berlin"),
    ("fr-FR", "Europe/Paris"),
    ("es-ES", "Europe/Madrid"),
    ("pl-PL", "Europe/Warsaw"),
    ("tr-TR", "Europe/Istanbul"),
    ("ru-RU", "Europe/Moscow"),
]

_DESKTOP_VIEWPORTS = [
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1600, 900),
    (1920, 1080),
    (2560, 1440),
]

_ENGINES = ["chromium", "firefox", "webkit"]

_DEVICES = [
    None,
    "iPhone 13",
    "Pixel 7",
]

_COLOR_SCHEMES = [None, "light", "dark"]

_UA_DESKTOP_CHROME = [
    # Keep it simple: reasonable modern UA strings for QA/testing.
    # (Not used for stealth; only for server-side UA gating tests.)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

_UA_DESKTOP_FIREFOX = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) Gecko/20100101 Firefox/124.0",
]

_UA_DESKTOP_SAFARI = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]


def generate_test_fingerprint(profile: BrowserProfile, *, seed: str | None = None) -> BrowserProfile:
    """
    Generates a varied set of Playwright context options for QA/testing.
    This is NOT stealth fingerprint spoofing. It's a "persona preset" generator.
    """
    rnd = random.Random(seed or profile.profile_id)

    engine = rnd.choice(_ENGINES)
    device = rnd.choice(_DEVICES)
    locale, tz = rnd.choice(_LOCALES)
    color = rnd.choice(_COLOR_SCHEMES)

    if device is None:
        vw, vh = rnd.choice(_DESKTOP_VIEWPORTS)
    else:
        # Device preset supplies viewport; keep ours unset to avoid conflicts.
        vw, vh = (None, None)

    ua = _pick_ua(rnd, engine)

    return replace(
        profile,
        engine=engine,
        device_preset=device,
        locale=locale,
        timezone_id=tz,
        color_scheme=color,
        viewport_width=vw if vw is not None else profile.viewport_width,
        viewport_height=vh if vh is not None else profile.viewport_height,
        user_agent=ua,
    )


def _pick_ua(rnd: random.Random, engine: str) -> str | None:
    e = (engine or "chromium").lower()
    if e == "firefox":
        return rnd.choice(_UA_DESKTOP_FIREFOX)
    if e == "webkit":
        return rnd.choice(_UA_DESKTOP_SAFARI)
    return rnd.choice(_UA_DESKTOP_CHROME)

