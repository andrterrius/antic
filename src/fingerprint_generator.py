from __future__ import annotations

import random
from dataclasses import replace
from typing import TypeVar

from profiles_store import BrowserProfile

T = TypeVar("T")


def _wchoice(rnd: random.Random, weighted: list[tuple[T, float]]) -> T:
    options, weights = zip(*weighted)
    return rnd.choices(options, weights=weights, k=1)[0]


# Locales + IANA timezone (roughly popular desktop traffic; weights are relative).
_LOCALES_WEIGHTED: list[tuple[tuple[str, str], float]] = [
    (("en-US", "America/New_York"), 26.0),
    (("en-US", "America/Los_Angeles"), 8.0),
    (("en-GB", "Europe/London"), 6.0),
    (("de-DE", "Europe/Berlin"), 7.0),
    (("fr-FR", "Europe/Paris"), 5.0),
    (("es-ES", "Europe/Madrid"), 4.0),
    (("it-IT", "Europe/Rome"), 4.0),
    (("pl-PL", "Europe/Warsaw"), 3.5),
    (("nl-NL", "Europe/Amsterdam"), 2.5),
    (("pt-BR", "America/Sao_Paulo"), 4.0),
    (("ru-RU", "Europe/Moscow"), 5.0),
    (("uk-UA", "Europe/Kyiv"), 2.0),
    (("tr-TR", "Europe/Istanbul"), 3.0),
    (("ja-JP", "Asia/Tokyo"), 4.0),
    (("ko-KR", "Asia/Seoul"), 2.5),
    (("zh-CN", "Asia/Shanghai"), 5.0),
    (("zh-TW", "Asia/Taipei"), 1.5),
    (("en-IN", "Asia/Kolkata"), 3.0),
    (("en-CA", "America/Toronto"), 2.0),
    (("sv-SE", "Europe/Stockholm"), 1.2),
]

# Common inner sizes (not full screen); 1920×1080 dominates.
_DESKTOP_VIEWPORTS_WEIGHTED: list[tuple[tuple[int, int], float]] = [
    ((1920, 1080), 38.0),
    ((1366, 768), 12.0),
    ((1536, 864), 10.0),
    ((1440, 900), 7.0),
    ((1280, 720), 6.0),
    ((1600, 900), 5.0),
    ((2560, 1440), 8.0),
    ((1680, 1050), 4.0),
    ((3840, 2160), 3.0),
    ((1280, 800), 4.0),
    ((1920, 1200), 3.0),
]

_DEVICES_WEIGHTED: list[tuple[str | None, float]] = [
    (None, 0.80),
    ("iPhone 13", 0.10),
    ("Pixel 7", 0.10),
]

_COLOR_SCHEMES_WEIGHTED: list[tuple[str | None, float]] = [
    ("dark", 0.42),
    ("light", 0.38),
    (None, 0.20),
]

# Chromium-style defaults (match fingerprint_consistency.py defaults for getParameter 7938 / 35724)
_WGL_CR_VERSION = "WebGL 1.0 (OpenGL ES 2.0 Chromium)"
_WGL_CR_SLV = "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)"

# Windows Chrome — typical ANGLE + D3D11 stacks (very common in the wild)
_WGL_CHROME_WIN: list[tuple[str, str]] = [
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (AMD)",
        "ANGLE (AMD, AMD Radeon RX 580 Series Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (AMD)",
        "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (AMD)",
        "ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
]

_WGL_CHROME_MAC: list[tuple[str, str]] = [
    ("Google Inc. (Apple)", "ANGLE (Apple, Apple M1, OpenGL 4.1)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, Apple M2, OpenGL 4.1)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, Apple M3, OpenGL 4.1)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, Apple M1 Pro, OpenGL 4.1)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, Apple M2 Pro, OpenGL 4.1)"),
]

_WGL_FIREFOX_WIN: list[tuple[str, str]] = [
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (AMD)",
        "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
]

_WGL_SAFARI_MAC: list[tuple[str, str, str, str]] = [
    (
        "Apple Inc.",
        "Apple GPU",
        "WebGL 1.0 (OpenGL ES 2.0 Metal - 90.0)",
        "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.00)",
    ),
]

_WGL_IPHONE: list[tuple[str, str, str, str]] = [
    (
        "Apple Inc.",
        "Apple GPU",
        "WebGL 1.0 (OpenGL ES 2.0 Metal - 90.0)",
        "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.00)",
    ),
]

_WGL_PIXEL: list[tuple[str, str, str, str]] = [
    (
        "Qualcomm",
        "Adreno (TM) 730",
        "WebGL 1.0 (OpenGL ES 2.0 Chromium)",
        "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)",
    ),
    (
        "Google Inc. (Qualcomm)",
        "ANGLE (Qualcomm, Adreno (TM) 730, OpenGL ES 3.2)",
        "WebGL 1.0 (OpenGL ES 2.0 Chromium)",
        "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)",
    ),
]


def _chrome_ua_windows(rnd: random.Random) -> str:
    major = _wchoice(
        rnd,
        [
            (128, 0.08),
            (130, 0.12),
            (131, 0.18),
            (132, 0.22),
            (133, 0.22),
            (134, 0.12),
            (135, 0.06),
        ],
    )
    patch = rnd.randint(0, 6478)
    build = f"{major}.0.{patch}.0"
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{build} Safari/537.36"
    )


def _chrome_ua_macos(rnd: random.Random) -> str:
    major = _wchoice(
        rnd,
        [
            (128, 0.08),
            (130, 0.12),
            (131, 0.18),
            (132, 0.22),
            (133, 0.22),
            (134, 0.12),
            (135, 0.06),
        ],
    )
    patch = rnd.randint(0, 6478)
    build = f"{major}.0.{patch}.0"
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{build} Safari/537.36"
    )


def _firefox_ua_windows(rnd: random.Random) -> str:
    rv = _wchoice(
        rnd,
        [
            ("128.0", 0.12),
            ("130.0", 0.18),
            ("131.0", 0.22),
            ("132.0", 0.22),
            ("133.0", 0.16),
            ("134.0", 0.10),
        ],
    )
    return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{rv}) Gecko/20100101 Firefox/{rv}"


def _firefox_ua_macos(rnd: random.Random) -> str:
    rv = _wchoice(
        rnd,
        [
            ("128.0", 0.12),
            ("130.0", 0.18),
            ("131.0", 0.22),
            ("132.0", 0.22),
            ("133.0", 0.16),
            ("134.0", 0.10),
        ],
    )
    return f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:{rv}) Gecko/20100101 Firefox/{rv}"


def _safari_ua_macos(rnd: random.Random) -> str:
    ver = _wchoice(
        rnd,
        [
            ("17.2", 0.15),
            ("17.4", 0.2),
            ("17.6", 0.25),
            ("18.0", 0.25),
            ("18.1", 0.15),
        ],
    )
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        f"Version/{ver} Safari/605.1.15"
    )


def generate_test_fingerprint(profile: BrowserProfile, *, seed: str | None = None) -> BrowserProfile:
    """
    Generates a varied set of Playwright context options for QA/testing.
    This is NOT stealth fingerprint spoofing. It's a "persona preset" generator.
    """
    rnd = random.Random(seed or profile.profile_id)

    engine = "chromium"
    device = _wchoice(rnd, _DEVICES_WEIGHTED)
    locale, tz = _wchoice(rnd, _LOCALES_WEIGHTED)
    color = _wchoice(rnd, _COLOR_SCHEMES_WEIGHTED)

    if device is None:
        vw, vh = _wchoice(rnd, _DESKTOP_VIEWPORTS_WEIGHTED)
    else:
        vw, vh = (None, None)

    ua = _pick_ua(rnd, engine)
    wv, wr, wver, wslv = _webgl_params(rnd, engine=engine, device=device, user_agent=ua)

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
        webgl_vendor=wv,
        webgl_renderer=wr,
        webgl_version=wver,
        webgl_shading_language_version=wslv,
    )


def _pick_ua(rnd: random.Random, engine: str) -> str | None:
    e = (engine or "chromium").lower()
    if e == "firefox":
        return _firefox_ua_windows(rnd) if rnd.random() < 0.82 else _firefox_ua_macos(rnd)
    if e == "webkit":
        return _safari_ua_macos(rnd)
    return _chrome_ua_windows(rnd) if rnd.random() < 0.88 else _chrome_ua_macos(rnd)


def _webgl_params(
    rnd: random.Random,
    *,
    engine: str,
    device: str | None,
    user_agent: str | None,
) -> tuple[str, str, str | None, str | None]:
    """
    Coherent WebGL vendor/renderer + optional VERSION / SHADING_LANGUAGE_VERSION for the persona.
    """
    eng = (engine or "chromium").lower()
    ua = (user_agent or "").lower()

    if device == "iPhone 13":
        v, r, ver, slv = rnd.choice(_WGL_IPHONE)
        return v, r, ver, slv

    if device == "Pixel 7":
        v, r, ver, slv = rnd.choice(_WGL_PIXEL)
        return v, r, ver, slv

    if eng == "webkit":
        v, r, ver, slv = rnd.choice(_WGL_SAFARI_MAC)
        return v, r, ver, slv

    if eng == "firefox":
        v, r = rnd.choice(_WGL_FIREFOX_WIN)
        return (
            v,
            r,
            "WebGL 1.0 (OpenGL ES 2.0)",
            "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0)",
        )

    if "macintosh" in ua or "mac os x" in ua:
        v, r = rnd.choice(_WGL_CHROME_MAC)
        return v, r, _WGL_CR_VERSION, _WGL_CR_SLV

    v, r = rnd.choice(_WGL_CHROME_WIN)
    return v, r, _WGL_CR_VERSION, _WGL_CR_SLV
