from __future__ import annotations

import re
from dataclasses import replace

from profiles_store import BrowserProfile


# Minimal "good enough" presets to keep timezone/locale/geo coherent.
# This is not an exhaustive geo database.
_COUNTRY_DEFAULTS: dict[str, dict[str, object]] = {
    "RU": {"locale": "ru-RU", "timezone_id": "Europe/Moscow", "geo": (55.7558, 37.6173)},
    "UA": {"locale": "uk-UA", "timezone_id": "Europe/Kyiv", "geo": (50.4501, 30.5234)},
    "PL": {"locale": "pl-PL", "timezone_id": "Europe/Warsaw", "geo": (52.2297, 21.0122)},
    "TR": {"locale": "tr-TR", "timezone_id": "Europe/Istanbul", "geo": (41.0082, 28.9784)},
    "DE": {"locale": "de-DE", "timezone_id": "Europe/Berlin", "geo": (52.5200, 13.4050)},
    "FR": {"locale": "fr-FR", "timezone_id": "Europe/Paris", "geo": (48.8566, 2.3522)},
    "ES": {"locale": "es-ES", "timezone_id": "Europe/Madrid", "geo": (40.4168, -3.7038)},
    "GB": {"locale": "en-GB", "timezone_id": "Europe/London", "geo": (51.5074, -0.1278)},
    "US": {"locale": "en-US", "timezone_id": "America/New_York", "geo": (40.7128, -74.0060)},
    "CA": {"locale": "en-CA", "timezone_id": "America/Toronto", "geo": (43.6532, -79.3832)},
}

_LOCALE_TO_COUNTRY: dict[str, str] = {
    "ru-RU": "RU",
    "uk-UA": "UA",
    "pl-PL": "PL",
    "tr-TR": "TR",
    "de-DE": "DE",
    "fr-FR": "FR",
    "es-ES": "ES",
    "en-GB": "GB",
    "en-US": "US",
    "en-CA": "CA",
}


def normalize_timezone_country(profile: BrowserProfile) -> BrowserProfile:
    """
    Best-effort consistency:
    - If country_code is set, fill missing locale/timezone/geo from presets.
    - Else if locale is set and country_code missing, derive country_code.
    Does not overwrite explicitly provided values.
    """
    cc = (profile.country_code or "").strip().upper() or None
    loc = (profile.locale or "").strip() or None

    if not cc and loc:
        cc = _LOCALE_TO_COUNTRY.get(loc)

    if cc:
        preset = _COUNTRY_DEFAULTS.get(cc)
        if preset:
            geo = preset.get("geo")
            lat = profile.geo_lat
            lon = profile.geo_lon
            if geo and (lat is None or lon is None):
                lat = lat if lat is not None else float(geo[0])
                lon = lon if lon is not None else float(geo[1])

            return replace(
                profile,
                country_code=cc,
                locale=profile.locale or str(preset.get("locale") or ""),
                timezone_id=profile.timezone_id or str(preset.get("timezone_id") or ""),
                geo_lat=lat,
                geo_lon=lon,
            )

        return replace(profile, country_code=cc)

    return profile


def platform_from_user_agent(user_agent: str | None) -> str | None:
    """
    Returns a navigator.platform-like value that matches the UA.
    """
    ua = (user_agent or "").strip()
    if not ua:
        return None

    u = ua.lower()
    if "iphone" in u or "ipad" in u or "ipod" in u:
        return "iPhone"
    if "android" in u:
        return "Linux armv8l"
    if "mac os x" in u or "macintosh" in u:
        return "MacIntel"
    if "windows nt" in u:
        return "Win32"
    if "linux" in u:
        return "Linux x86_64"
    return None


def chromium_ua_metadata_from_user_agent(user_agent: str | None) -> dict | None:
    """
    Creates a minimal User-Agent Client Hints metadata object for Chromium CDP
    (Emulation.setUserAgentOverride). Best-effort, not exhaustive.
    """
    ua = (user_agent or "").strip()
    if not ua:
        return None

    u = ua.lower()
    if "windows nt" in u:
        platform_name = "Windows"
    elif "mac os x" in u or "macintosh" in u:
        platform_name = "macOS"
    elif "android" in u:
        platform_name = "Android"
    elif "iphone" in u or "ipad" in u or "ipod" in u:
        platform_name = "iOS"
    elif "linux" in u:
        platform_name = "Linux"
    else:
        platform_name = "Windows"

    mobile = "mobile" in u or "android" in u or "iphone" in u

    # Chromium brand/version parsing (Chrome/123.0.0.0). If absent, skip.
    m = re.search(r"chrome/(\d+)\.", u, re.IGNORECASE)
    major = m.group(1) if m else None
    if not major:
        return {
            "platform": platform_name,
            "mobile": bool(mobile),
        }

    return {
        "brands": [
            {"brand": "Chromium", "version": major},
            {"brand": "Google Chrome", "version": major},
            {"brand": "Not;A=Brand", "version": "99"},
        ],
        "platform": platform_name,
        "platformVersion": "10.0.0",
        "architecture": "x86",
        "model": "",
        "mobile": bool(mobile),
    }


def _escape_js_string(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")


_DEFAULT_WEBGL1_VERSION = "WebGL 1.0 (OpenGL ES 2.0 Chromium)"
_DEFAULT_WEBGL1_SLV = "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)"
_DEFAULT_WEBGL2_VERSION = "WebGL 2.0 (OpenGL ES 3.0 Chromium)"
_DEFAULT_WEBGL2_SLV = "WebGL GLSL ES 3.00 (OpenGL ES GLSL ES 3.00 Chromium)"


def webgl_override_script(
    *,
    vendor: str | None,
    renderer: str | None,
    platform_value: str | None,
    webgl_version: str | None = None,
    webgl_shading_language_version: str | None = None,
) -> str:
    """
    Injects:
    - navigator.platform override (best-effort)
    - WebGL vendor/renderer + common getParameter / getShaderPrecisionFormat hooks (WebGL1 + WebGL2)

    When vendor or renderer is set, also applies Chromium-typical VERSION / SHADING_LANGUAGE_VERSION
    and stable precision formats so checks that correlate these with UNMASKED_* stay consistent.
    """
    v = _escape_js_string(vendor or "")
    r = _escape_js_string(renderer or "")
    p = _escape_js_string(platform_value or "")

    patch_gl = bool((vendor or "").strip() or (renderer or "").strip())
    v1 = _escape_js_string(
        (webgl_version or _DEFAULT_WEBGL1_VERSION) if patch_gl else ""
    )
    slv1 = _escape_js_string(
        (webgl_shading_language_version or _DEFAULT_WEBGL1_SLV) if patch_gl else ""
    )
    v2 = _escape_js_string(_DEFAULT_WEBGL2_VERSION if patch_gl else "")
    slv2 = _escape_js_string(_DEFAULT_WEBGL2_SLV if patch_gl else "")

    # Use empty strings to indicate "no override" inside the script.
    return f"""
(() => {{
  const PLATFORM = '{p}';
  const WEBGL_VENDOR = '{v}';
  const WEBGL_RENDERER = '{r}';
  const WEBGL_PATCH = !!(WEBGL_VENDOR || WEBGL_RENDERER);
  const WEBGL_V1 = '{v1}';
  const WEBGL_SLV1 = '{slv1}';
  const WEBGL_V2 = '{v2}';
  const WEBGL_SLV2 = '{slv2}';

  const GL_VERSION = 7938;
  const GL_SHADING_LANGUAGE_VERSION = 35724;
  const UNMASKED_VENDOR_WEBGL = 37445;
  const UNMASKED_RENDERER_WEBGL = 37446;
  const GL_VENDOR = 7936;
  const GL_RENDERER = 7937;

  const HIGH_FLOAT = 0x8DF2;
  const MEDIUM_FLOAT = 0x8DF1;
  const LOW_FLOAT = 0x8DF0;
  const HIGH_INT = 0x8DF5;
  const MEDIUM_INT = 0x8DF4;
  const LOW_INT = 0x8DF3;

  const defineGetter = (obj, prop, value) => {{
    try {{
      const desc = Object.getOwnPropertyDescriptor(obj, prop);
      if (desc && desc.configurable === false) return;
      Object.defineProperty(obj, prop, {{ get: () => value, configurable: true }});
    }} catch (_) {{}}
  }};

  if (PLATFORM) {{
    defineGetter(Navigator.prototype, 'platform', PLATFORM);
  }}

  const patchWebGL = (proto) => {{
    if (!proto || !proto.getParameter) return;
    const original = proto.getParameter;
    Object.defineProperty(proto, 'getParameter', {{
      value: function(parameter) {{
        if (WEBGL_VENDOR && parameter === UNMASKED_VENDOR_WEBGL) return WEBGL_VENDOR;
        if (WEBGL_RENDERER && parameter === UNMASKED_RENDERER_WEBGL) return WEBGL_RENDERER;
        if (WEBGL_VENDOR && parameter === GL_VENDOR) return WEBGL_VENDOR;
        if (WEBGL_RENDERER && parameter === GL_RENDERER) return WEBGL_RENDERER;

        if (WEBGL_PATCH) {{
          const webgl2 = typeof WebGL2RenderingContext !== 'undefined'
            && this instanceof WebGL2RenderingContext;
          if (parameter === GL_VERSION) return webgl2 ? WEBGL_V2 : WEBGL_V1;
          if (parameter === GL_SHADING_LANGUAGE_VERSION) return webgl2 ? WEBGL_SLV2 : WEBGL_SLV1;
        }}
        return original.apply(this, arguments);
      }},
      configurable: true,
      writable: true,
    }});
  }};

  patchWebGL(WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchWebGL(WebGL2RenderingContext && WebGL2RenderingContext.prototype);

  const patchPrecision = (proto) => {{
    if (!proto || !proto.getShaderPrecisionFormat) return;
    const orig = proto.getShaderPrecisionFormat;
    Object.defineProperty(proto, 'getShaderPrecisionFormat', {{
      value: function(shaderType, precisionType) {{
        if (!WEBGL_PATCH) return orig.apply(this, arguments);
        if (precisionType === HIGH_FLOAT || precisionType === MEDIUM_FLOAT)
          return {{ rangeMin: 127, rangeMax: 127, precision: 23 }};
        if (precisionType === LOW_FLOAT)
          return {{ rangeMin: 127, rangeMax: 127, precision: 15 }};
        if (precisionType === HIGH_INT)
          return {{ rangeMin: 31, rangeMax: 30, precision: 0 }};
        if (precisionType === MEDIUM_INT || precisionType === LOW_INT)
          return {{ rangeMin: 15, rangeMax: 14, precision: 0 }};
        return orig.apply(this, arguments);
      }},
      configurable: true,
      writable: true,
    }});
  }};

  patchPrecision(WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchPrecision(WebGL2RenderingContext && WebGL2RenderingContext.prototype);

  const patchGetExtension = (proto) => {{
    if (!proto || !proto.getExtension) return;
    const orig = proto.getExtension;
    Object.defineProperty(proto, 'getExtension', {{
      value: function(name) {{
        const ext = orig.apply(this, arguments);
        if (String(name).toLowerCase() === 'webgl_debug_renderer_info') {{
          return ext || {{
            UNMASKED_VENDOR_WEBGL: 37445,
            UNMASKED_RENDERER_WEBGL: 37446,
          }};
        }}
        return ext;
      }},
      configurable: true,
      writable: true,
    }});
  }};

  patchGetExtension(WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchGetExtension(WebGL2RenderingContext && WebGL2RenderingContext.prototype);
}})();
""".strip()

