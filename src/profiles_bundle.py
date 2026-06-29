from __future__ import annotations

import json
import shutil
import uuid
import zipfile
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from cookies_io import cookies_from_json, read_profile_cookies
from profiles_store import BrowserProfile, backup_profiles_db, load_profiles, profiles_from_json_list, upsert_profiles
from playwright_runner import profile_user_data_dir

BUNDLE_FORMAT = "antidetect-profiles-v1"
BUNDLE_FORMAT_COOKIES = "antidetect-profiles-cookies-v1"
KNOWN_BUNDLE_FORMATS = frozenset({BUNDLE_FORMAT, BUNDLE_FORMAT_COOKIES})
MANIFEST_NAME = "manifest.json"
PROFILES_JSON = "profiles.json"
USERDATA_PREFIX = "user-data/"
COOKIES_PREFIX = "cookies/"


def is_safe_profile_id(s: str) -> bool:
    if not s or len(s) > 64:
        return False
    for c in s:
        if not (c.isalnum() or c in "_-"):
            return False
    return True


def _emit(progress: Callable[[str], None] | None, msg: str) -> None:
    if progress:
        try:
            progress(msg)
        except Exception:
            pass


def _bundle_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _read_manifest(zf: zipfile.ZipFile) -> dict:
    try:
        raw = json.loads(_read_manifest_entry(zf, MANIFEST_NAME).decode("utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def export_profiles_zip(
    destination_dir: Path,
    profiles: list[BrowserProfile],
    *,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """
    Создаёт ZIP в destination_dir: manifest.json, profiles.json, user-data/<id>/...
    """
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    out_path = destination_dir / f"antidetect_profiles_{_bundle_timestamp()}.zip"

    manifest = {
        "format": BUNDLE_FORMAT,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "profile_count": len(profiles),
    }

    payload = [asdict(p) for p in profiles]
    n_files = 0
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        zf.writestr(PROFILES_JSON, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

        for p in profiles:
            if not is_safe_profile_id(p.profile_id):
                continue
            udir = profile_user_data_dir(p.profile_id)
            if not udir.is_dir():
                continue
            for file in udir.rglob("*"):
                if not file.is_file():
                    continue
                rel = file.relative_to(udir).as_posix()
                arcname = f"{USERDATA_PREFIX}{p.profile_id}/{rel}"
                zf.write(file, arcname)
                n_files += 1
                if n_files % 200 == 0:
                    _emit(progress, f"Добавлено файлов в архив: {n_files}…")

    _emit(progress, f"Готово: {n_files} файлов данных, {len(profiles)} профилей.")
    return out_path


def export_profiles_cookies_zip(
    destination_dir: Path,
    profiles: list[BrowserProfile],
    hosts: set[str],
    *,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """
    Лёгкий ZIP: profiles.json + cookies/<profile_id>.json (только выбранные домены).
    """
    if not hosts:
        raise ValueError("Выберите хотя бы один сайт для экспорта cookies")

    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    out_path = destination_dir / f"antidetect_cookies_{_bundle_timestamp()}.zip"

    manifest = {
        "format": BUNDLE_FORMAT_COOKIES,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "profile_count": len(profiles),
        "selected_hosts": sorted(hosts),
    }

    payload = [asdict(p) for p in profiles]
    total_cookies = 0
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        zf.writestr(PROFILES_JSON, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

        for i, p in enumerate(profiles):
            if not is_safe_profile_id(p.profile_id):
                continue
            _emit(progress, f"Экспорт cookies: {i + 1} / {len(profiles)} — {p.name}")
            cookies = read_profile_cookies(p.profile_id, hosts)
            total_cookies += len(cookies)
            arcname = f"{COOKIES_PREFIX}{p.profile_id}.json"
            zf.writestr(
                arcname,
                json.dumps(cookies, ensure_ascii=False, indent=2) + "\n",
            )

    _emit(
        progress,
        f"Готово: {total_cookies} cookies, {len(profiles)} профилей, {len(hosts)} сайтов.",
    )
    return out_path


def _read_bundle_profiles(zf: zipfile.ZipFile) -> list[BrowserProfile]:
    try:
        raw = json.loads(_read_manifest_entry(zf, PROFILES_JSON).decode("utf-8"))
    except KeyError as e:
        raise ValueError("В архиве нет profiles.json") from e
    except json.JSONDecodeError as e:
        raise ValueError("Некорректный JSON в profiles.json") from e
    profiles = profiles_from_json_list(raw)
    if not profiles:
        raise ValueError("В profiles.json нет ни одного профиля с валидным profile_id")
    return profiles


def _normalize_zip_path(name: str) -> str:
    return name.replace("\\", "/")


def _zip_paths(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """(normalized, original) для каждой записи в архиве."""
    return [(_normalize_zip_path(n), n) for n in zf.namelist()]


def _zip_has_userdata(zf: zipfile.ZipFile) -> bool:
    return any(
        norm.startswith(USERDATA_PREFIX) and not norm.endswith("/")
        for norm, _ in _zip_paths(zf)
    )


def _zip_has_cookies_pack(zf: zipfile.ZipFile) -> bool:
    return any(
        norm.startswith(COOKIES_PREFIX) and norm.endswith(".json") and not norm.endswith("/")
        for norm, _ in _zip_paths(zf)
    )


def _detect_bundle_format(zf: zipfile.ZipFile) -> str:
    """
    Определяет тип архива. Приоритет у содержимого: user-data/ → полный импорт
    (обратная совместимость со старыми ZIP без manifest или с любым format).
    """
    has_userdata = _zip_has_userdata(zf)
    has_cookies = _zip_has_cookies_pack(zf)
    manifest = _read_manifest(zf)
    fmt = manifest.get("format")

    if has_userdata:
        return BUNDLE_FORMAT

    if fmt == BUNDLE_FORMAT_COOKIES or has_cookies:
        return BUNDLE_FORMAT_COOKIES

    if fmt in KNOWN_BUNDLE_FORMATS:
        return str(fmt)

    if fmt is None:
        return BUNDLE_FORMAT

    names = {_normalize_zip_path(n) for n in zf.namelist()}
    if PROFILES_JSON in names:
        return BUNDLE_FORMAT

    raise ValueError(f"Неизвестный формат архива: {fmt!r}")


def _read_manifest_entry(zf: zipfile.ZipFile, norm_path: str) -> bytes:
    for norm, orig in _zip_paths(zf):
        if norm == norm_path:
            return zf.read(orig)
    raise KeyError(norm_path)


def _compute_import_additions(
    existing: list[BrowserProfile], imported: list[BrowserProfile]
) -> tuple[list[BrowserProfile], dict[str, str], int]:
    """
    Возвращает список новых профилей (уже с финальными profile_id),
    карту исходный_id -> финальный_id для распаковки user-data из ZIP,
    и число профилей, получивших новый ID из‑за конфликта.
    """
    existing_ids = {p.profile_id for p in existing}
    orig_to_final: dict[str, str] = {}
    additions: list[BrowserProfile] = []
    remapped = 0

    for p in imported:
        if not is_safe_profile_id(p.profile_id):
            continue
        orig = p.profile_id
        if orig not in existing_ids:
            additions.append(p)
            orig_to_final[orig] = orig
            existing_ids.add(orig)
        else:
            new_id = uuid.uuid4().hex[:12]
            while new_id in existing_ids:
                new_id = uuid.uuid4().hex[:12]
            new_name = f"{p.name} [{orig}]"
            additions.append(replace(p, profile_id=new_id, name=new_name))
            orig_to_final[orig] = new_id
            existing_ids.add(new_id)
            remapped += 1

    return additions, orig_to_final, remapped


def _extract_userdata_from_zip(
    zf: zipfile.ZipFile, orig_to_final: dict[str, str], *, progress: Callable[[str], None] | None
) -> None:
    """Пишет файлы из ZIP (пути user-data/<orig>/...) в каталоги profile_user_data_dir(final)."""
    entries = [
        (norm, orig)
        for norm, orig in _zip_paths(zf)
        if norm.startswith(USERDATA_PREFIX) and not norm.endswith("/")
    ]
    total = len(entries)
    for i, (norm, name) in enumerate(entries):
        rest = norm[len(USERDATA_PREFIX) :]
        parts = rest.split("/", 1)
        if not parts or not parts[0]:
            continue
        orig = parts[0]
        if orig not in orig_to_final:
            continue
        if not is_safe_profile_id(orig):
            continue
        final = orig_to_final[orig]
        sub = parts[1] if len(parts) > 1 else ""
        # zip-slip: sub must not escape
        if sub.startswith("/") or ".." in Path(sub).parts:
            continue
        dest = profile_user_data_dir(final) / sub
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(name, "r") as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
        if total and i % 300 == 0:
            _emit(progress, f"Распаковка: {i + 1} / {total}…")


def _import_cookies_from_zip(
    zf: zipfile.ZipFile,
    additions: list[BrowserProfile],
    orig_to_final: dict[str, str],
    *,
    progress: Callable[[str], None] | None,
) -> int:
    from playwright_runner import inject_cookies_into_profile

    by_id = {p.profile_id: p for p in additions}
    norm_map = {norm: orig for norm, orig in _zip_paths(zf)}
    imported_count = 0
    for orig, final in orig_to_final.items():
        arcname = f"{COOKIES_PREFIX}{orig}.json"
        zip_name = norm_map.get(arcname)
        if zip_name is None:
            continue
        profile = by_id.get(final)
        if profile is None:
            continue
        try:
            raw = json.loads(zf.read(zip_name).decode("utf-8"))
            cookies = cookies_from_json(raw)
        except (json.JSONDecodeError, ValueError) as e:
            raise ValueError(f"Некорректный файл cookies для профиля {orig}") from e
        if not cookies:
            continue
        _emit(progress, f"Запись cookies в профиль «{profile.name}» ({len(cookies)} шт.)…")

        def _log(msg: str) -> None:
            _emit(progress, msg)

        inject_cookies_into_profile(profile, cookies, log=_log)
        imported_count += len(cookies)
    return imported_count


def import_profiles_zip(
    zip_path: Path,
    existing: list[BrowserProfile] | None = None,
    *,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[BrowserProfile], int, int]:
    """
    Добавляет профили из архива к уже сохранённым, при конфликте profile_id назначает новый ID.
    Всегда читает текущую базу (не полагается на снимок из UI).
    """
    zip_path = Path(zip_path)
    backup_profiles_db()
    db_existing = load_profiles()
    if existing:
        by_id = {p.profile_id: p for p in db_existing}
        for p in existing:
            if p.profile_id and p.profile_id not in by_id:
                by_id[p.profile_id] = p
        db_existing = list(by_id.values())

    with zipfile.ZipFile(zip_path, "r") as zf:
        fmt = _detect_bundle_format(zf)
        imported = _read_bundle_profiles(zf)
        additions, orig_to_final, remapped = _compute_import_additions(db_existing, imported)
        if not additions:
            raise ValueError("Не удалось импортировать профили (пустой или недопустимый набор)")

        upsert_profiles(additions)

        if fmt == BUNDLE_FORMAT_COOKIES:
            _emit(progress, "Импорт cookies в профили…")
            _import_cookies_from_zip(zf, additions, orig_to_final, progress=progress)
            _emit(progress, "Сохранено.")
            return load_profiles(), len(additions), remapped

        _emit(progress, "Распаковка каталогов user-data…")
        _extract_userdata_from_zip(zf, orig_to_final, progress=progress)
        _emit(progress, "Сохранено.")
    return load_profiles(), len(additions), remapped
