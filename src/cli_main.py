from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Callable, Optional

from profiles_store import BrowserProfile, load_profiles, save_profiles
from fingerprint_generator import generate_test_fingerprint
from fingerprint_consistency import normalize_timezone_country
from playwright_runner import (
    ensure_playwright_chromium_installed,
    geoip_from_ip,
    get_proxy_ip,
    profile_user_data_dir,
    run_profile,
)


def _eprint(s: str) -> None:
    sys.stderr.write(s.rstrip("\n") + "\n")


class _Logger:
    def __init__(self, *, log_file: str | None = None) -> None:
        self._lock = threading.Lock()
        self._fp = open(log_file, "a", encoding="utf-8") if log_file else None

    def close(self) -> None:
        if self._fp:
            try:
                self._fp.close()
            except Exception:
                pass
            self._fp = None

    def log(self, s: str) -> None:
        line = s.rstrip("\n")
        with self._lock:
            print(line, flush=True)
            if self._fp:
                try:
                    self._fp.write(line + "\n")
                    self._fp.flush()
                except Exception:
                    # Logging must never break the run.
                    pass


def _find_profile(profiles: list[BrowserProfile], profile_id: str) -> BrowserProfile | None:
    pid = (profile_id or "").strip()
    if not pid:
        return None
    for p in profiles:
        if p.profile_id == pid:
            return p
    return None


def _require_profile(profiles: list[BrowserProfile], profile_id: str) -> BrowserProfile:
    p = _find_profile(profiles, profile_id)
    if not p:
        raise SystemExit(f"Profile not found: {profile_id}")
    return p


def _json_dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _blank_to_none(s: str | None) -> str | None:
    v = (s or "").strip()
    return v if v else None


def cmd_profiles_list(args: argparse.Namespace) -> int:
    profiles = load_profiles()
    if args.format == "json":
        print(_json_dump([asdict(x) for x in profiles]))
        return 0

    if not profiles:
        print("No profiles.")
        return 0

    for p in profiles:
        proxy = (p.proxy_server or "").strip() or "-"
        print(f"{p.profile_id}\t{p.name}\tproxy={proxy}")
    return 0


def cmd_profiles_show(args: argparse.Namespace) -> int:
    profiles = load_profiles()
    p = _require_profile(profiles, args.profile_id)
    print(_json_dump(asdict(p)))
    return 0


def cmd_profiles_new(args: argparse.Namespace) -> int:
    profiles = load_profiles()
    profile_id = (args.profile_id or "").strip() or uuid.uuid4().hex[:12]

    if _find_profile(profiles, profile_id):
        raise SystemExit(f"Profile already exists: {profile_id}")

    base = BrowserProfile(profile_id=profile_id, name=(args.name or "").strip() or f"Profile {len(profiles) + 1}")
    p = generate_test_fingerprint(base)

    # Optional proxy-derived persona (like UI best-effort when proxy is edited).
    proxy_server = _blank_to_none(args.proxy_server)
    proxy_user = _blank_to_none(args.proxy_username)
    proxy_pass = _blank_to_none(args.proxy_password)
    if proxy_server:
        p = replace(p, proxy_server=proxy_server, proxy_username=proxy_user, proxy_password=proxy_pass)
        proxy_ip = get_proxy_ip(proxy_server, proxy_user, proxy_pass)
        if proxy_ip:
            geo = geoip_from_ip(proxy_ip)
        else:
            geo = None
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
        p = replace(p, viewport_width=None, viewport_height=None)

    profiles.append(p)
    save_profiles(profiles)

    if args.quiet:
        return 0

    if args.format == "json":
        print(_json_dump(asdict(p)))
    else:
        print(f"Created profile: {p.profile_id} ({p.name})")
    return 0


def cmd_profiles_delete(args: argparse.Namespace) -> int:
    profiles = load_profiles()
    p = _require_profile(profiles, args.profile_id)

    profiles2 = [x for x in profiles if x.profile_id != p.profile_id]
    save_profiles(profiles2)

    if args.purge_data:
        try:
            import shutil

            shutil.rmtree(profile_user_data_dir(p.profile_id), ignore_errors=True)
        except Exception:
            pass

    if not args.quiet:
        print(f"Deleted profile: {p.profile_id}")
    return 0


def cmd_profiles_set(args: argparse.Namespace) -> int:
    profiles = load_profiles()
    p = _require_profile(profiles, args.profile_id)

    # Mirror UI constraints: engine is chromium; viewport is system-default unless user wants otherwise.
    proxy_server = _blank_to_none(args.proxy_server) if args.proxy_server is not None else p.proxy_server
    no_proxy = not (proxy_server or "").strip()

    updated = replace(
        p,
        name=(args.name.strip() if args.name else p.name),
        proxy_server=proxy_server,
        proxy_username=(_blank_to_none(args.proxy_username) if args.proxy_username is not None else p.proxy_username),
        proxy_password=(_blank_to_none(args.proxy_password) if args.proxy_password is not None else p.proxy_password),
        engine="chromium",
        device_preset=(_blank_to_none(args.device_preset) if args.device_preset is not None else p.device_preset),
        user_agent=(_blank_to_none(args.user_agent) if args.user_agent is not None else p.user_agent),
        locale=(None if no_proxy else (_blank_to_none(args.locale) if args.locale is not None else p.locale)),
        timezone_id=(None if no_proxy else (_blank_to_none(args.timezone_id) if args.timezone_id is not None else p.timezone_id)),
        country_code=(_blank_to_none(args.country_code) if args.country_code is not None else p.country_code),
        color_scheme=(_blank_to_none(args.color_scheme) if args.color_scheme is not None else p.color_scheme),
        viewport_width=(int(args.viewport_width) if args.viewport_width is not None else p.viewport_width),
        viewport_height=(int(args.viewport_height) if args.viewport_height is not None else p.viewport_height),
        geo_lat=(float(args.geo_lat) if args.geo_lat is not None else p.geo_lat),
        geo_lon=(float(args.geo_lon) if args.geo_lon is not None else p.geo_lon),
        webgl_vendor=(_blank_to_none(args.webgl_vendor) if args.webgl_vendor is not None else p.webgl_vendor),
        webgl_renderer=(_blank_to_none(args.webgl_renderer) if args.webgl_renderer is not None else p.webgl_renderer),
        webgl_version=(_blank_to_none(args.webgl_version) if args.webgl_version is not None else p.webgl_version),
        webgl_shading_language_version=(
            _blank_to_none(args.webgl_shading_language_version)
            if args.webgl_shading_language_version is not None
            else p.webgl_shading_language_version
        ),
    )

    # Optional: align geo/tz/country with proxy (like UI when proxy edited).
    if args.sync_proxy_geo and updated.proxy_server:
        proxy_ip = get_proxy_ip(updated.proxy_server, updated.proxy_username, updated.proxy_password)
        geo = geoip_from_ip(proxy_ip) if proxy_ip else None
        if geo:
            updated = replace(
                updated,
                country_code=str(geo.get("country_code") or updated.country_code or "").strip().upper() or None,
                timezone_id=str(geo.get("timezone_id") or updated.timezone_id or "").strip() or None,
                locale=None,
                geo_lat=geo.get("geo_lat") if geo.get("geo_lat") is not None else updated.geo_lat,
                geo_lon=geo.get("geo_lon") if geo.get("geo_lon") is not None else updated.geo_lon,
            )
        updated = normalize_timezone_country(updated)
        updated = replace(updated, viewport_width=None, viewport_height=None)

    profiles2 = [updated if x.profile_id == updated.profile_id else x for x in profiles]
    save_profiles(profiles2)

    if args.format == "json":
        print(_json_dump(asdict(updated)))
    elif not args.quiet:
        print(f"Updated profile: {updated.profile_id}")
    return 0


def _run_one_profile(
    profile: BrowserProfile,
    *,
    url: str,
    script_path: str | None,
    protect_webrtc: bool,
    force_webrtc_proxy_ip: bool,
    log: Callable[[str], None],
    stop_evt: threading.Event,
) -> int:
    res = run_profile(
        profile,
        start_url=url,
        script_path=script_path,
        protect_webrtc=protect_webrtc,
        force_webrtc_proxy_ip=force_webrtc_proxy_ip,
        log=log,
        stop_requested=stop_evt.is_set,
    )
    return 0 if res.ok else 2


def cmd_run(args: argparse.Namespace) -> int:
    profiles = load_profiles()
    ids: list[str] = list(dict.fromkeys([x.strip() for x in (args.profile_ids or []) if (x or "").strip()]))
    if not ids:
        raise SystemExit("No profile ids provided.")

    selected: list[BrowserProfile] = []
    for pid in ids:
        selected.append(_require_profile(profiles, pid))

    logger = _Logger(log_file=args.log_file)
    stop_evt = threading.Event()

    def mklog(prefix: str) -> Callable[[str], None]:
        return lambda s: logger.log(f"{prefix} {s}".rstrip())

    def run_worker(p: BrowserProfile) -> int:
        prefix = f"[{p.name}:{p.profile_id}]"
        return _run_one_profile(
            p,
            url=args.url,
            script_path=args.script,
            protect_webrtc=not args.no_protect_webrtc,
            force_webrtc_proxy_ip=not args.no_force_webrtc_proxy_ip,
            log=mklog(prefix),
            stop_evt=stop_evt,
        )

    threads: list[threading.Thread] = []
    codes: dict[str, int] = {}

    def wrap(p: BrowserProfile) -> None:
        codes[p.profile_id] = run_worker(p)

    try:
        if args.parallel and len(selected) > 1:
            for p in selected:
                t = threading.Thread(target=wrap, args=(p,), daemon=False)
                threads.append(t)
                t.start()
            while any(t.is_alive() for t in threads):
                time.sleep(0.2)
        else:
            for p in selected:
                wrap(p)
    except KeyboardInterrupt:
        logger.log("Stop requested (Ctrl+C) — closing contexts...")
        stop_evt.set()
        for t in threads:
            try:
                t.join(timeout=10)
            except Exception:
                pass
    finally:
        logger.close()

    # If any failed -> non-zero
    return 0 if all(code == 0 for code in codes.values()) else 2


def cmd_run_all(args: argparse.Namespace) -> int:
    profiles = load_profiles()
    if not profiles:
        raise SystemExit("No profiles. Create at least one profile first.")
    args2 = argparse.Namespace(**vars(args))
    args2.profile_ids = [p.profile_id for p in profiles]
    return cmd_run(args2)


def cmd_install_chromium(args: argparse.Namespace) -> int:
    logger = _Logger(log_file=args.log_file)
    try:
        ok = ensure_playwright_chromium_installed(logger.log)
        return 0 if ok else 2
    finally:
        logger.close()


def cmd_proxy_ip(args: argparse.Namespace) -> int:
    ip = get_proxy_ip(args.proxy_server, args.proxy_username, args.proxy_password)
    if not ip:
        return 2
    print(ip)
    return 0


def cmd_geoip(args: argparse.Namespace) -> int:
    data = geoip_from_ip(args.ip)
    if not data:
        return 2
    print(_json_dump(data))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="antidetect-cli",
        description="Antidetect UI features in command line (profiles + Playwright runner).",
    )
    p.add_argument("--log-file", default=None, help="Append logs to a file (utf-8).")

    sub = p.add_subparsers(dest="cmd", required=True)

    # profiles
    sp = sub.add_parser("profiles", help="Manage stored profiles.")
    psub = sp.add_subparsers(dest="profiles_cmd", required=True)

    sp_list = psub.add_parser("list", help="List profiles.")
    sp_list.add_argument("--format", choices=["text", "json"], default="text")
    sp_list.set_defaults(func=cmd_profiles_list)

    sp_show = psub.add_parser("show", help="Show one profile as JSON.")
    sp_show.add_argument("profile_id")
    sp_show.set_defaults(func=cmd_profiles_show)

    sp_new = psub.add_parser("new", help="Create a new profile.")
    sp_new.add_argument("--profile-id", default=None)
    sp_new.add_argument("--name", default=None)
    sp_new.add_argument("--proxy-server", default=None, help="http://host:port or socks5://host:port (or host:port)")
    sp_new.add_argument("--proxy-username", default=None)
    sp_new.add_argument("--proxy-password", default=None)
    sp_new.add_argument("--format", choices=["text", "json"], default="text")
    sp_new.add_argument("--quiet", action="store_true")
    sp_new.set_defaults(func=cmd_profiles_new)

    sp_del = psub.add_parser("delete", help="Delete a profile.")
    sp_del.add_argument("profile_id")
    sp_del.add_argument("--purge-data", action="store_true", default=True, help="Also delete user-data dir (default: true).")
    sp_del.add_argument("--no-purge-data", dest="purge_data", action="store_false", help="Keep user-data dir.")
    sp_del.add_argument("--quiet", action="store_true")
    sp_del.set_defaults(func=cmd_profiles_delete)

    sp_set = psub.add_parser("set", help="Update profile fields.")
    sp_set.add_argument("profile_id")
    sp_set.add_argument("--name", default=None)
    sp_set.add_argument("--proxy-server", default=None)
    sp_set.add_argument("--proxy-username", default=None)
    sp_set.add_argument("--proxy-password", default=None)
    sp_set.add_argument("--device-preset", default=None)
    sp_set.add_argument("--user-agent", default=None)
    sp_set.add_argument("--locale", default=None)
    sp_set.add_argument("--timezone-id", default=None)
    sp_set.add_argument("--country-code", default=None)
    sp_set.add_argument("--color-scheme", default=None, choices=[None, "light", "dark", "no-preference"], nargs="?")
    sp_set.add_argument("--viewport-width", default=None, type=int)
    sp_set.add_argument("--viewport-height", default=None, type=int)
    sp_set.add_argument("--geo-lat", default=None, type=float)
    sp_set.add_argument("--geo-lon", default=None, type=float)
    sp_set.add_argument("--webgl-vendor", default=None)
    sp_set.add_argument("--webgl-renderer", default=None)
    sp_set.add_argument("--webgl-version", default=None)
    sp_set.add_argument("--webgl-shading-language-version", default=None)
    sp_set.add_argument("--sync-proxy-geo", action="store_true", help="Align geo/tz/country with proxy IP (best-effort).")
    sp_set.add_argument("--format", choices=["text", "json"], default="text")
    sp_set.add_argument("--quiet", action="store_true")
    sp_set.set_defaults(func=cmd_profiles_set)

    # run
    sp_run = sub.add_parser("run", help="Run one or more profiles.")
    sp_run.add_argument("profile_ids", nargs="+")
    sp_run.add_argument("--url", default="https://2ip.ru")
    sp_run.add_argument("--script", default=None, help="Path to .py script with run(page, log=None).")
    sp_run.add_argument("--parallel", action="store_true", help="Run multiple profiles in parallel.")
    sp_run.add_argument("--no-protect-webrtc", action="store_true", help="Disable WebRTC protection flags.")
    sp_run.add_argument("--no-force-webrtc-proxy-ip", action="store_true", help="Do not try to detect proxy IP.")
    sp_run.set_defaults(func=cmd_run)

    sp_run_all = sub.add_parser("run-all", help="Run all stored profiles.")
    sp_run_all.add_argument("--url", default="https://2ip.ru")
    sp_run_all.add_argument("--script", default=None)
    sp_run_all.add_argument("--parallel", action="store_true")
    sp_run_all.add_argument("--no-protect-webrtc", action="store_true")
    sp_run_all.add_argument("--no-force-webrtc-proxy-ip", action="store_true")
    sp_run_all.set_defaults(func=cmd_run_all)

    sp_inst = sub.add_parser("install-chromium", help="Install Patchright Chromium if missing.")
    sp_inst.set_defaults(func=cmd_install_chromium)

    sp_pip = sub.add_parser("proxy-ip", help="Detect proxy exit IP via ipify.")
    sp_pip.add_argument("proxy_server")
    sp_pip.add_argument("--proxy-username", default=None)
    sp_pip.add_argument("--proxy-password", default=None)
    sp_pip.set_defaults(func=cmd_proxy_ip)

    sp_geo = sub.add_parser("geoip", help="GeoIP lookup for an IP.")
    sp_geo.add_argument("ip")
    sp_geo.set_defaults(func=cmd_geoip)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BrokenPipeError:
        # Allow piping output (e.g., | head) without stack traces.
        return 0
    except SystemExit:
        raise
    except Exception as e:
        _eprint(f"ERROR: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

