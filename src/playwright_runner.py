from __future__ import annotations

import traceback
from dataclasses import replace
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse, urlunparse
import os
import subprocess
import platform
import io
import contextlib
import sys

from playwright.sync_api import ProxySettings, BrowserContext, Page, Playwright
from patchright.sync_api import sync_playwright


from profiles_store import BrowserProfile
from fingerprint_consistency import (
    chromium_ua_metadata_from_user_agent,
    normalize_timezone_country,
    platform_from_user_agent,
    webgl_override_script,
)


@dataclass(slots=True)
class LaunchResult:
    ok: bool
    message: str


def _playwright_browsers_path() -> Path:
    """
    Ensure Playwright browsers are stored in a persistent per-app folder.

    PyInstaller onefile extracts the Playwright driver into a temp `_MEI...` dir.
    If we keep Playwright defaults, it can end up looking for browsers inside that
    temp dir, which breaks on next run. Using a fixed path avoids that.
    """
    # On Windows, Playwright/Patchright default cache is under LOCALAPPDATA.
    # Prefer that location so we can reuse already installed browsers and avoid
    # Roaming profile sync / permission edge-cases.
    if platform.system().lower() == "windows":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            local_pw = Path(local_appdata) / "ms-playwright"
            # If browsers already exist there, reuse them.
            if _chromium_executable_exists(local_pw):
                return local_pw

        # Fall back to per-app persistent folder in LocalAppData.
        if local_appdata:
            root = Path(local_appdata) / "AntidetectUI"
            p = root / "ms-playwright"
            p.mkdir(parents=True, exist_ok=True)
            return p

        # Very last resort: Roaming.
        roaming_appdata = os.environ.get("APPDATA")
        if roaming_appdata:
            root = Path(roaming_appdata) / "AntidetectUI"
            p = root / "ms-playwright"
            p.mkdir(parents=True, exist_ok=True)
            return p

        p = (Path(__file__).resolve().parent.parent / "data" / "ms-playwright")
        p.mkdir(parents=True, exist_ok=True)
        return p

    # Non-Windows: keep the previous "per-app persistent folder" behaviour.
    appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    root = Path(appdata) / "AntidetectUI" if appdata else (Path(__file__).resolve().parent.parent / "data")
    p = root / "ms-playwright"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _chromium_executable_exists(browsers_root: Path) -> bool:
    # Windows: chromium-*/chrome-win*/chrome.exe
    for exe in browsers_root.glob("chromium-*/chrome-win*/chrome.exe"):
        if exe.is_file():
            return True
    # macOS: chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium
    for exe in browsers_root.glob("chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"):
        if exe.is_file():
            return True
    # Linux: chromium-*/chrome-linux/chrome
    for exe in browsers_root.glob("chromium-*/chrome-linux/chrome"):
        if exe.is_file():
            return True
    return False


class _LogWriter(io.TextIOBase):
    def __init__(self, log: Callable[[str], None]) -> None:
        super().__init__()
        self._log = log
        self._buf = ""

    def write(self, s: str) -> int:  # type: ignore[override]
        if not s:
            return 0
        self._buf += s
        while True:
            nl = self._buf.find("\n")
            if nl < 0:
                break
            line = self._buf[:nl].rstrip("\r")
            self._buf = self._buf[nl + 1 :]
            if line.strip():
                self._log(line)
        return len(s)

    def flush(self) -> None:  # type: ignore[override]
        tail = self._buf.strip("\r\n")
        self._buf = ""
        if tail.strip():
            self._log(tail)


def ensure_playwright_chromium_installed(log: Callable[[str], None]) -> bool:
    browsers_root = _playwright_browsers_path()
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_root)

    if _chromium_executable_exists(browsers_root):
        return True

    log(f"Playwright browsers not found in: {browsers_root}")
    log("Installing Chromium (playwright install chromium)...")
    try:
        # playwright CLI's entrypoint reads arguments from sys.argv.
        from playwright.__main__ import main as playwright_main  # type: ignore

        lw = _LogWriter(log)
        with contextlib.redirect_stdout(lw), contextlib.redirect_stderr(lw):
            try:
                old_argv = sys.argv[:]
                sys.argv = ["patchright", "install", "--force", "chromium"]
                try:
                    playwright_main()
                finally:
                    sys.argv = old_argv
            except SystemExit as e:
                code = int(getattr(e, "code", 1) or 0)
                if code != 0:
                    log(f"Playwright install failed with exit code {code}")
                    return False

        if _chromium_executable_exists(browsers_root):
            log("Playwright Chromium installed successfully.")
            return True

        log("Playwright install finished, but Chromium executable was not found.")
        return False
    except Exception as e:
        log(f"Failed to install Playwright Chromium automatically: {e}")
        log(traceback.format_exc())
        return False


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


def get_proxy_ip(proxy_server: str, proxy_username: str = None, proxy_password: str = None) -> Optional[str]:
    """
    Получает IP адрес прокси для подстановки в WebRTC
    """
    try:
        import requests
        proxy_url = proxy_server
        if not proxy_url.startswith(('http://', 'https://', 'socks5://')):
            proxy_url = 'http://' + proxy_url

        proxies = {
            'http': proxy_url,
            'https': proxy_url,
        }

        if proxy_username and proxy_password:
            # Форматируем прокси с авторизацией
            parsed = urlparse(proxy_url)
            auth_proxy = f"{parsed.scheme}://{proxy_username}:{proxy_password}@{parsed.netloc}"
            proxies = {
                'http': auth_proxy,
                'https': auth_proxy,
            }

        response = requests.get('https://api.ipify.org?format=json', proxies=proxies, timeout=10)
        proxy_ip = response.json()['ip']
        return proxy_ip
    except Exception as e:
        print(f"Failed to get proxy IP: {e}")
        return None


def geoip_from_ip(ip: str) -> dict[str, object] | None:
    """
    Best-effort GeoIP lookup: countryCode, timezone, lat, lon.
    Uses ip-api.com (no key) and fails gracefully.
    """
    ip2 = (ip or "").strip()
    if not ip2:
        return None
    try:
        import requests

        # Keep payload small; ip-api supports selecting fields.
        url = f"http://ip-api.com/json/{ip2}?fields=status,countryCode,timezone,lat,lon,message"
        r = requests.get(url, timeout=8)
        data = r.json()
        if not isinstance(data, dict):
            return None
        if data.get("status") != "success":
            return None
        out: dict[str, object] = {}
        if data.get("countryCode"):
            out["country_code"] = str(data["countryCode"]).strip().upper()
        if data.get("timezone"):
            out["timezone_id"] = str(data["timezone"]).strip()
        if data.get("lat") is not None and data.get("lon") is not None:
            try:
                out["geo_lat"] = float(data["lat"])
                out["geo_lon"] = float(data["lon"])
            except Exception:
                pass
        return out or None
    except Exception:
        return None


def inject_webrtc_ip_override(page: Page, proxy_ip: str, log: Callable[[str], None]) -> None:
    """
    Внедряет скрипт для полной подмены WebRTC IP на IP прокси
    """
    log(f"Setting WebRTC IP to proxy IP: {proxy_ip}")

    # page.add_init_script(f"""
    #     (function() {{
    #         const PROXY_IP = '{proxy_ip}';
    #         console.log('WebRTC IP override enabled, using IP:', PROXY_IP);
    #
    #         // Перехватываем и подменяем все ICE кандидаты
    #         const originalRTCPeerConnection = window.RTCPeerConnection;
    #
    #         window.RTCPeerConnection = function(config) {{
    #             // Блокируем реальные ICE серверы
    #             if (config && config.iceServers) {{
    #                 config.iceServers = config.iceServers.filter(server => {{
    #                     // Оставляем только STUN/TURN серверы, но подменим IP
    #                     return true;
    #                 }});
    #             }}
    #
    #             const pc = new originalRTCPeerConnection(config);
    #
    #             // Перехватываем создание ICE кандидатов
    #             const originalAddIceCandidate = pc.addIceCandidate;
    #             pc.addIceCandidate = function(candidate) {{
    #                 if (candidate && candidate.candidate) {{
    #                     // Подменяем IP в кандидате
    #                     let modifiedCandidate = candidate.candidate;
    #                     // Заменяем реальный IP на IP прокси
    #                     modifiedCandidate = modifiedCandidate.replace(/\\d{{1,3}}\\.\\d{{1,3}}\\.\\d{{1,3}}\\.\\d{{1,3}}/g, PROXY_IP);
    #                     modifiedCandidate = modifiedCandidate.replace(/[a-fA-F0-9:]+:+[a-fA-F0-9:]+/g, ''); // Удаляем IPv6
    #
    #                     const modifiedCandidateObj = {{
    #                         candidate: modifiedCandidate,
    #                         sdpMid: candidate.sdpMid,
    #                         sdpMLineIndex: candidate.sdpMLineIndex
    #                     }};
    #                     return originalAddIceCandidate.call(this, modifiedCandidateObj);
    #                 }}
    #                 return originalAddIceCandidate.call(this, candidate);
    #             }};
    #
    #             // Перехватываем создание оффера/ответа
    #             const originalCreateOffer = pc.createOffer;
    #             pc.createOffer = function(options) {{
    #                 return originalCreateOffer.call(this, options).then(offer => {{
    #                     // Подменяем IP в SDP
    #                     let sdp = offer.sdp;
    #                     sdp = sdp.replace(/c=IN IP4 \\d{{1,3}}\\.\\d{{1,3}}\\.\\d{{1,3}}\\.\\d{{1,3}}/g, `c=IN IP4 ${{PROXY_IP}}`);
    #                     sdp = sdp.replace(/c=IN IP6 [a-fA-F0-9:]+/g, '');
    #                     offer.sdp = sdp;
    #                     return offer;
    #                 }});
    #             }};
    #
    #             const originalCreateAnswer = pc.createAnswer;
    #             pc.createAnswer = function(options) {{
    #                 return originalCreateAnswer.call(this, options).then(answer => {{
    #                     let sdp = answer.sdp;
    #                     sdp = sdp.replace(/c=IN IP4 \\d{{1,3}}\\.\\d{{1,3}}\\.\\d{{1,3}}\\.\\d{{1,3}}/g, `c=IN IP4 ${{PROXY_IP}}`);
    #                     sdp = sdp.replace(/c=IN IP6 [a-fA-F0-9:]+/g, '');
    #                     answer.sdp = sdp;
    #                     return answer;
    #                 }});
    #             }};
    #
    #             // Перехватываем onicecandidate событие
    #             const originalSetOnIceCandidate = Object.getOwnPropertyDescriptor(RTCPeerConnection.prototype, 'onicecandidate');
    #             Object.defineProperty(pc, 'onicecandidate', {{
    #                 set: function(callback) {{
    #                     const wrappedCallback = function(event) {{
    #                         if (event.candidate && event.candidate.candidate) {{
    #                             let modifiedCandidate = event.candidate.candidate;
    #                             modifiedCandidate = modifiedCandidate.replace(/\\d{{1,3}}\\.\\d{{1,3}}\\.\\d{{1,3}}\\.\\d{{1,3}}/g, PROXY_IP);
    #                             event.candidate.candidate = modifiedCandidate;
    #                         }}
    #                         if (callback) callback(event);
    #                     }};
    #                     originalSetOnIceCandidate.set.call(this, wrappedCallback);
    #                 }},
    #                 get: function() {{
    #                     return originalSetOnIceCandidate.get.call(this);
    #                 }}
    #             }});
    #
    #             return pc;
    #         }};
    #
    #         window.RTCPeerConnection.prototype = originalRTCPeerConnection.prototype;
    #
    #         // Также подменяем для WebKit браузеров
    #         if (window.webkitRTCPeerConnection) {{
    #             window.webkitRTCPeerConnection = window.RTCPeerConnection;
    #         }}
    #
    #         console.log('WebRTC IP override injected successfully');
    #     }})();
    # """)


def run_profile(
        profile: BrowserProfile,
        *,
        start_url: str,
        log: Callable[[str], None],
        script_path: Optional[str] = None,
        protect_webrtc: bool = True,
        force_webrtc_proxy_ip: bool = True,  # Принудительно подменяем IP на прокси
        stop_requested: Callable[[], bool] | None = None,
) -> LaunchResult:
    """
    Launches a persistent Chromium context for a profile.
    WebRTC IP будет подменен на IP прокси
    """

    # Получаем IP прокси для подмены
    proxy_ip = None
    if force_webrtc_proxy_ip and profile.proxy_server:
        proxy_ip = get_proxy_ip(
            profile.proxy_server,
            profile.proxy_username,
            profile.proxy_password
        )
        if proxy_ip:
            log(f"Detected proxy IP: {proxy_ip}")
        else:
            log("Warning: Could not detect proxy IP, WebRTC protection may not work")

    # Аргументы для максимальной защиты WebRTC
    extra_args = []
    if protect_webrtc:
        log("Enabling WebRTC protection...")
        extra_args = [
            '--disable-webrtc',
            '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
            '--disable-features=WebRtcHideLocalIpsWithMdns,IsolateOrigins,site-per-process',
            '--force-fieldtrials=WebRTC/Disabled/',
            '--webrtc-ip-handling-policy=disable_non_proxied_udp',
            '--disable-blink-features=AutomationControlled',
            '--disable-site-isolation-trials',
            '--no-sandbox',
            '--remote-allow-origins=*',
            '--disable-dev-shm-usage',
            '--disable-breakpad',
            '--disable-crash-reporter',
            '--disable-logging',
            '--log-level=3',
            '--silent-debugger-extension-api',
            '--disable-webgl'
        ]

    try:
        # If proxy is present, align geo/timezone/country with the proxy IP (best-effort).
        if profile.proxy_server and proxy_ip:
            geo = geoip_from_ip(proxy_ip)
            if geo:
                profile = replace(
                    profile,
                    country_code=str(geo.get("country_code") or profile.country_code or "").strip() or None,
                    timezone_id=str(geo.get("timezone_id") or profile.timezone_id or "").strip() or None,
                    # Force locale to be re-derived from country (avoid stale/random locale from profile).
                    locale=None,
                    geo_lat=geo.get("geo_lat") if geo.get("geo_lat") is not None else profile.geo_lat,
                    geo_lon=geo.get("geo_lon") if geo.get("geo_lon") is not None else profile.geo_lon,
                )

        profile = normalize_timezone_country(profile)
        user_data_dir = profile_user_data_dir(profile.profile_id)

        # Ensure Playwright browsers are available before trying to launch.
        if not ensure_playwright_chromium_installed(log):
            return LaunchResult(ok=False, message="Playwright Chromium is not installed (playwright install chromium).")

        with sync_playwright() as pw:
            # UI no longer exposes engine choice; default to Chromium.
            browser_type = pw.chromium
            device_opts = _device_options(pw, profile.device_preset)

            desktop_vp = None
            if not device_opts.get("is_mobile"):
                desktop_vp = _desktop_viewport_from_work_area()

            # Если используем прокси, обязательно применяем его
            proxy_settings = _proxy_settings(profile)
            if proxy_settings:
                log(f"Using proxy: {proxy_settings['server']}")

            launch_args = list(extra_args)
            # Desktop: let CDP set window bounds after launch (work-area sized).
            # For mobile presets we keep explicit viewport.
            if not device_opts.get("is_mobile"):
                # Keep a stable top-left; CDP will adjust further.
                launch_args.append("--window-position=0,0")

            context: BrowserContext = browser_type.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=False,
                proxy=proxy_settings,
                viewport=(desktop_vp or _launch_viewport(profile, device_opts)),
                user_agent=profile.user_agent or device_opts.get("user_agent"),
                locale=(profile.locale if profile.proxy_server else None),
                timezone_id=(profile.timezone_id if profile.proxy_server else None),
                color_scheme=profile.color_scheme,
                geolocation=_geolocation(profile),
                permissions=(["geolocation"] if _geolocation(profile) else None),
                device_scale_factor=device_opts.get("device_scale_factor"),
                is_mobile=device_opts.get("is_mobile"),
                has_touch=device_opts.get("has_touch"),
                args=launch_args
            )

            page: Page
            if context.pages:
                page = context.pages[0]
            else:
                page = context.new_page()

            # Keep any newly opened tabs/popups at the same size.
            if desktop_vp:
                def _size_new_page(p: Page) -> None:
                    try:
                        p.set_viewport_size({"width": int(desktop_vp["width"]), "height": int(desktop_vp["height"])})
                    except Exception:
                        pass

                try:
                    context.on("page", _size_new_page)
                except Exception:
                    pass

            # Best-effort: make the window fill the screen width (not F11 fullscreen).
            if not device_opts.get("is_mobile"):
                _try_set_window_to_work_area_chromium(context, page, log)

            # Fingerprint consistency: platform (UA-aligned) + WebGL overrides.
            effective_ua = profile.user_agent or device_opts.get("user_agent")
            platform_value = platform_from_user_agent(effective_ua)

            # Chromium-only: also align UA-CH metadata where possible.
            try:
                if (profile.engine or "chromium").lower() == "chromium" and effective_ua:
                    meta = chromium_ua_metadata_from_user_agent(effective_ua)
                    if meta:
                        sess = context.new_cdp_session(page)
                        sess.send("Emulation.setUserAgentOverride", {"userAgent": effective_ua, "userAgentMetadata": meta})
            except Exception:
                # Best-effort; don't block launch if CDP is unavailable.
                pass

            # Внедряем подмену WebRTC IP на IP прокси
            if protect_webrtc and force_webrtc_proxy_ip and proxy_ip:
                inject_webrtc_ip_override(page, proxy_ip, log)
            elif protect_webrtc:
                log("Warning: WebRTC protection enabled but proxy IP not available")

            log(f"Open: {start_url}")
            page.goto(start_url, wait_until="domcontentloaded")
            # page.add_init_script(
            #     webgl_override_script(
            #         vendor=profile.webgl_vendor,
            #         renderer=profile.webgl_renderer,
            #         platform_value=platform_value,
            #         webgl_version=profile.webgl_version,
            #         webgl_shading_language_version=profile.webgl_shading_language_version,
            #     )
            # )

            if script_path:
                _run_user_script(script_path, page, log)

            log("Browser running. Close the browser window to end the session.")
            context.on("close", lambda: log("Context closed."))

            # block until closed by user (or stop requested)
            try:
                while True:
                    if stop_requested and stop_requested():
                        log("Stop requested — closing context...")
                        break
                    page.wait_for_timeout(500)
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


def _try_set_window_to_work_area_chromium(context: BrowserContext, page: Page, log: Callable[[str], None]) -> None:
    """
    Chromium-only, best-effort.
    Resizes the current window to the OS work-area (screen minus taskbar/docks).

    This yields a "max size" window without using F11 fullscreen.
    """
    try:
        sess = context.new_cdp_session(page)
        info = sess.send("Browser.getWindowForTarget")
        win_id = info.get("windowId")
        if not win_id:
            return

        left, top, width, height = _work_area_logical()
        if width is None or height is None:
            return
        if width < 640 or height < 480:
            return

        # Ensure the window is resizable via explicit bounds.
        sess.send("Browser.setWindowBounds", {"windowId": win_id, "bounds": {"windowState": "normal"}})
        sess.send(
            "Browser.setWindowBounds",
            {
                "windowId": win_id,
                "bounds": {
                    "left": int(left or 0),
                    "top": int(top or 0),
                    "width": int(width),
                    "height": int(height),
                },
            },
        )

        # Align JS-visible viewport with the window sizing (CSS px).
        try:
            page.set_viewport_size({"width": int(width), "height": int(height)})
        except Exception:
            pass
    except Exception:
        # Don't block launch if CDP/permission is unavailable.
        log("Warning: could not resize window to work area; using default window state")


def _work_area_logical() -> tuple[int | None, int | None, int | None, int | None]:
    """
    Returns (left, top, width, height) for the primary work area in logical pixels.
    """
    sysname = (platform.system() or "").strip().lower()
    if sysname == "windows":
        try:
            import ctypes
            from ctypes import wintypes

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", wintypes.LONG),
                    ("top", wintypes.LONG),
                    ("right", wintypes.LONG),
                    ("bottom", wintypes.LONG),
                ]

            SPI_GETWORKAREA = 0x0030
            rect = RECT()
            ok = ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)  # type: ignore[attr-defined]
            if not ok:
                return None, None, None, None
            left = int(rect.left)
            top = int(rect.top)
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            return left, top, width, height
        except Exception:
            return None, None, None, None

    # Fallback (Linux/macOS): approximate with full screen.
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        w = int(root.winfo_screenwidth())
        h = int(root.winfo_screenheight())
        root.destroy()
        return 0, 0, w, h
    except Exception:
        return None, None, None, None


def _desktop_viewport_from_work_area() -> dict | None:
    """
    Desktop viewport based on OS work-area (logical pixels).
    Using a context-level viewport ensures *new tabs* inherit the same size.
    """
    left, top, width, height = _work_area_logical()
    if width is None or height is None:
        return None
    if width < 640 or height < 480:
        return None
    return {"width": int(width), "height": int(height)}


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
    if "viewport" in device_opts:
        return device_opts["viewport"]
    return None


def _launch_viewport(profile: BrowserProfile, device_opts: dict) -> dict | None:
    # If we're on a mobile preset, keep the device viewport (emulation matters).
    if device_opts.get("is_mobile"):
        return _viewport(profile, device_opts)
    # Desktop: let the browser window decide (pairs with --start-maximized).
    return None


def _geolocation(profile: BrowserProfile) -> dict | None:
    if profile.geo_lat is None or profile.geo_lon is None:
        return None
    return {"latitude": float(profile.geo_lat), "longitude": float(profile.geo_lon)}


def _run_user_script(script_path: str, page: Page, log: Callable[[str], None]) -> None:
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
        fn(page, log)
    except TypeError:
        fn(page)