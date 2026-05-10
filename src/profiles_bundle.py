from __future__ import annotations

import json
import shutil
import uuid
import zipfile
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from profiles_store import BrowserProfile, profiles_from_json_list, save_profiles
from playwright_runner import profile_user_data_dir

BUNDLE_FORMAT = "antidetect-profiles-v1"
MANIFEST_NAME = "manifest.json"
PROFILES_JSON = "profiles.json"
USERDATA_PREFIX = "user-data/"


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
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = destination_dir / f"antidetect_profiles_{ts}.zip"

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


def _read_bundle_profiles(zf: zipfile.ZipFile) -> list[BrowserProfile]:
    try:
        raw = json.loads(zf.read(PROFILES_JSON).decode("utf-8"))
    except KeyError as e:
        raise ValueError("В архиве нет profiles.json") from e
    except json.JSONDecodeError as e:
        raise ValueError("Некорректный JSON в profiles.json") from e
    profiles = profiles_from_json_list(raw)
    if not profiles:
        raise ValueError("В profiles.json нет ни одного профиля с валидным profile_id")
    return profiles


def _validate_manifest(zf: zipfile.ZipFile) -> None:
    try:
        raw = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
    except Exception:
        return
    if not isinstance(raw, dict):
        return
    fmt = raw.get("format")
    if fmt is not None and fmt != BUNDLE_FORMAT:
        raise ValueError(f"Неизвестный формат архива: {fmt!r}")


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
            additions.append(replace(p, profile_id=new_id))
            orig_to_final[orig] = new_id
            existing_ids.add(new_id)
            remapped += 1

    return additions, orig_to_final, remapped


def _extract_userdata_from_zip(
    zf: zipfile.ZipFile, orig_to_final: dict[str, str], *, progress: Callable[[str], None] | None
) -> None:
    """Пишет файлы из ZIP (пути user-data/<orig>/...) в каталоги profile_user_data_dir(final)."""
    names = [n for n in zf.namelist() if n.startswith(USERDATA_PREFIX) and not n.endswith("/")]
    total = len(names)
    for i, name in enumerate(names):
        rest = name[len(USERDATA_PREFIX) :]
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


def import_profiles_zip(
    zip_path: Path,
    existing: list[BrowserProfile],
    *,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[BrowserProfile], int, int]:
    """
    Добавляет профили из архива к existing, при конфликте profile_id назначает новый ID.
    Возвращает (полный список для save_profiles, число импортированных, число с переназначенным ID).
    """
    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path, "r") as zf:
        _validate_manifest(zf)
        imported = _read_bundle_profiles(zf)
        additions, orig_to_final, remapped = _compute_import_additions(existing, imported)
        if not additions:
            raise ValueError("Не удалось импортировать профили (пустой или недопустимый набор)")
        _emit(progress, "Распаковка каталогов user-data…")
        _extract_userdata_from_zip(zf, orig_to_final, progress=progress)
        merged = existing + additions
        save_profiles(merged)
        _emit(progress, "Сохранено.")
    return merged, len(additions), remapped
