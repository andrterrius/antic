from __future__ import annotations

from dataclasses import replace

from fingerprint_consistency import normalize_timezone_country
from playwright_runner import geoip_from_ip, get_proxy_ip
from profiles_store import BrowserProfile


def parse_host_port_user_pass_line(line: str) -> tuple[str, str, str, str] | None:
    """
    One line: host:port:username:password (IPv4 host).
    Password may contain ':' — everything after the 3rd colon is the password.
    Empty lines and lines starting with # are skipped (caller treats None as skip).
    """
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return None
    parts = s.split(":")
    if len(parts) < 4:
        return None
    host, port, user = parts[0].strip(), parts[1].strip(), parts[2].strip()
    password = ":".join(parts[3:]).strip()
    if not host or not port or not user:
        return None
    return host, port, user, password


def proxy_server_url(host: str, port: str, scheme: str = "http") -> str:
    sch = (scheme or "http").strip().lower()
    if sch == "https":
        sch = "http"
    return f"{sch}://{host}:{port}"


def apply_proxy_and_sync_geo(
    p: BrowserProfile,
    *,
    proxy_server: str,
    proxy_username: str | None,
    proxy_password: str | None,
) -> BrowserProfile:
    """Set proxy fields and align country/tz/geo with proxy exit IP (same idea as CLI/UI on new profile)."""
    p = replace(
        p,
        proxy_server=proxy_server,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
    )
    proxy_ip = get_proxy_ip(proxy_server, proxy_username, proxy_password)
    geo = geoip_from_ip(proxy_ip) if proxy_ip else None
    if geo:
        p = replace(
            p,
            country_code=str(geo.get("country_code") or "").strip().upper() or None,
            timezone_id=str(geo.get("timezone_id") or "").strip() or None,
            locale=None,
            geo_lat=geo.get("geo_lat") if geo.get("geo_lat") is not None else p.geo_lat,
            geo_lon=geo.get("geo_lon") if geo.get("geo_lon") is not None else p.geo_lon,
        )
    p = normalize_timezone_country(p)
    return replace(p, viewport_width=None, viewport_height=None)
