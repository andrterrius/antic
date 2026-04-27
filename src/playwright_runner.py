from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse, urlunparse
import os

from playwright.sync_api import sync_playwright, ProxySettings, BrowserContext, Page, Playwright

from profiles_store import BrowserProfile


@dataclass(slots=True)
class LaunchResult:
    ok: bool
    message: str


def profile_user_data_dir(profile_id: str) -> Path:
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) / "AntidetectUI" if appdata else (Path(__file__).resolve().parent.parent / "data")
    d = root / "user-data" / profile_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _proxy_settings(p: BrowserProfile) -> ProxySettings | None:
    if not p.proxy_server:
        return None

    server = p.proxy_server.strip()
    username = (p.proxy_username or "").strip() or None
    password = (p.proxy_password or "").strip() or None

    # Allow pasting proxy as http://user:pass@host:port
    # Playwright expects credentials in separate fields, not in the server URL.
    if "://" in server:
        parsed = urlparse(server)
        if parsed.username and not username:
            username = parsed.username
        if parsed.password and not password:
            password = parsed.password

        if parsed.username or parsed.password:
            # strip creds from server
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            server = urlunparse((parsed.scheme, netloc, parsed.path or "", "", "", ""))

    proxy: ProxySettings = {"server": server}
    if username:
        proxy["username"] = username
    if password:
        proxy["password"] = password
    return proxy


def run_profile(
    profile: BrowserProfile,
    *,
    start_url: str,
    log: Callable[[str], None],
    script_path: Optional[str] = None,
) -> LaunchResult:
    """
    Launches a persistent Chromium context for a profile.
    Intended for QA/testing automation (Playwright).
    """

    try:
        user_data_dir = profile_user_data_dir(profile.profile_id)

        with sync_playwright() as pw:
            browser_type = _browser_type(pw, profile.engine)
            device_opts = _device_options(pw, profile.device_preset)

            context: BrowserContext = browser_type.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=False,
                proxy=_proxy_settings(profile),
                viewport=_viewport(profile, device_opts),
                user_agent=profile.user_agent or device_opts.get("user_agent"),
                locale=profile.locale,
                timezone_id=profile.timezone_id,
                color_scheme=profile.color_scheme,
                geolocation=_geolocation(profile),
                permissions=(["geolocation"] if _geolocation(profile) else None),
                device_scale_factor=device_opts.get("device_scale_factor"),
                is_mobile=device_opts.get("is_mobile"),
                has_touch=device_opts.get("has_touch"),
            )

            page: Page
            if context.pages:
                page = context.pages[0]
            else:
                page = context.new_page()

            log(f"Open: {start_url}")
            page.goto(start_url, wait_until="domcontentloaded")

            if script_path:
                _run_user_script(script_path, page, log)

            log("Browser running. Close the browser window to end the session.")
            context.on("close", lambda: log("Context closed."))

            # block until closed by user
            try:
                page.wait_for_timeout(10**9)
            except Exception:
                pass
            finally:
                try:
                    context.close()
                except Exception:
                    pass

        return LaunchResult(ok=True, message="Closed")
    except Exception as e:
        log("ERROR:")
        log(str(e))
        log(traceback.format_exc())
        return LaunchResult(ok=False, message=str(e))


def _browser_type(pw: Playwright, engine: str | None):
    eng = (engine or "chromium").strip().lower()
    if eng == "firefox":
        return pw.firefox
    if eng == "webkit":
        return pw.webkit
    return pw.chromium


def _device_options(pw: Playwright, preset: str | None) -> dict:
    if not preset:
        return {}
    try:
        d = pw.devices.get(preset)
    except Exception:
        d = None
    if not d:
        return {}

    # Normalize to snake_case keys used by Playwright python context options
    # Playwright devices dict uses "userAgent" etc.
    out: dict = {}
    if "userAgent" in d:
        out["user_agent"] = d["userAgent"]
    if "viewport" in d:
        out["viewport"] = d["viewport"]
    if "deviceScaleFactor" in d:
        out["device_scale_factor"] = d["deviceScaleFactor"]
    if "isMobile" in d:
        out["is_mobile"] = d["isMobile"]
    if "hasTouch" in d:
        out["has_touch"] = d["hasTouch"]
    return out


def _viewport(profile: BrowserProfile, device_opts: dict) -> dict | None:
    if profile.viewport_width and profile.viewport_height:
        return {"width": int(profile.viewport_width), "height": int(profile.viewport_height)}
    if "viewport" in device_opts:
        return device_opts["viewport"]
    return None


def _geolocation(profile: BrowserProfile) -> dict | None:
    if profile.geo_lat is None or profile.geo_lon is None:
        return None
    return {"latitude": float(profile.geo_lat), "longitude": float(profile.geo_lon)}


def _run_user_script(script_path: str, page: Page, log: Callable[[str], None]) -> None:
    """
    Loads a Python script from disk and executes `run(page, log)` or `run(page)`.
    This is a generic automation hook (not stealth / not fingerprint evasion).
    """
    p = Path(script_path)
    if not p.exists():
        log(f"Script not found: {script_path}")
        return

    ns: dict[str, object] = {}
    code = p.read_text(encoding="utf-8")
    exec(compile(code, str(p), "exec"), ns, ns)

    fn = ns.get("run")
    if not callable(fn):
        log("Script must define function: run(page, log=None)")
        return

    log(f"Run script: {script_path}")
    try:
        # try run(page, log)
        fn(page, log)
    except TypeError:
        # fallback run(page)
        fn(page)

