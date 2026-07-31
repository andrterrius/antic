import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  api,
  downloadBlob,
  type CookieHost,
  type Profile,
} from "../api/client";

type ExportModal =
  | null
  | { step: "mode"; ids: string[] }
  | { step: "hosts"; ids: string[]; hosts: CookieHost[]; selected: Set<string> }
  | { step: "progress"; message: string };

/** Как в десктопе: ошибка — красный, успех — зелёный. */
function tagTone(tag: string): "error" | "success" | null {
  const low = tag.toLocaleLowerCase("ru");
  if (low.includes("ошибка")) return "error";
  if (low.includes("успех") || low.startsWith("успеш")) return "success";
  return null;
}

function tagPillClass(tag: string): string {
  const tone = tagTone(tag);
  if (tone === "error") return "pill pill-error";
  if (tone === "success") return "pill pill-success";
  return "pill";
}

function tagFilterClass(tag: string, active: boolean): string {
  const tone = tagTone(tag);
  const parts = ["tag-chip"];
  if (active) parts.push("active");
  if (tone === "error") parts.push("tag-chip-error");
  if (tone === "success") parts.push("tag-chip-success");
  return parts.join(" ");
}

export function ProfilesPage() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [tagFilter, setTagFilter] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [busy, setBusy] = useState(false);
  const [exportModal, setExportModal] = useState<ExportModal>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    setError("");
    try {
      const list = await api.listProfiles();
      setProfiles(list);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(t);
  }, [refresh]);

  const allTags = useMemo(() => {
    const s = new Set<string>();
    for (const p of profiles) for (const t of p.tags || []) if (t) s.add(t);
    return [...s].sort();
  }, [profiles]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return profiles.filter((p) => {
      if (tagFilter.size) {
        const tags = new Set(p.tags || []);
        for (const t of tagFilter) if (!tags.has(t)) return false;
      }
      if (!q) return true;
      const hay = `${p.name} ${p.profile_id} ${(p.tags || []).join(" ")} ${p.proxy_server || ""}`.toLowerCase();
      return hay.includes(q);
    });
  }, [profiles, search, tagFilter]);

  function toggleSelect(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleTag(tag: string) {
    setTagFilter((prev) => {
      const next = new Set(prev);
      if (next.has(tag)) next.delete(tag);
      else next.add(tag);
      return next;
    });
  }

  function exportIds(): string[] {
    if (selected.size) return [...selected];
    return filtered.map((p) => p.profile_id);
  }

  async function onImport(file: File | undefined) {
    if (!file) return;
    setBusy(true);
    setError("");
    setInfo("");
    try {
      const res = await api.importArchive(file);
      setInfo(
        `Импортировано: ${res.imported}` +
          (res.remapped ? ` (переназначено ID: ${res.remapped})` : "") +
          `. Всего в базе: ${res.total}.`,
      );
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function startExport() {
    const ids = exportIds();
    if (!ids.length) {
      setError("Нет профилей для экспорта");
      return;
    }
    setExportModal({ step: "mode", ids });
  }

  async function doFullExport(ids: string[]) {
    setBusy(true);
    setError("");
    setInfo("");
    setExportModal({ step: "progress", message: `Собираю ZIP (${ids.length} профил.)…` });
    try {
      const blob = await api.exportArchive(ids, "full");
      if (!blob || blob.size === 0) {
        throw new Error("Пустой ответ сервера при экспорте");
      }
      downloadBlob(blob, `antidetect_profiles_${stamp()}.zip`);
      setInfo(`Экспорт полного архива: ${ids.length} профил. (${Math.round(blob.size / 1024)} КБ)`);
      setExportModal(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setExportModal({ step: "mode", ids });
    } finally {
      setBusy(false);
    }
  }

  async function openCookiesExport(ids: string[]) {
    setBusy(true);
    setError("");
    setExportModal({ step: "progress", message: "Сканирую cookies…" });
    try {
      const res = await api.cookieHosts(ids);
      if (!res.hosts.length) {
        setError("В выбранных профилях нет cookies для экспорта");
        setExportModal(null);
        return;
      }
      setExportModal({
        step: "hosts",
        ids,
        hosts: res.hosts,
        selected: new Set(res.hosts.map((h) => h.host)),
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setExportModal({ step: "mode", ids });
    } finally {
      setBusy(false);
    }
  }

  async function doCookiesExport(ids: string[], hosts: string[]) {
    if (!hosts.length) {
      setError("Выберите хотя бы один сайт");
      return;
    }
    setBusy(true);
    setError("");
    setInfo("");
    setExportModal({ step: "progress", message: "Собираю cookies ZIP…" });
    try {
      const blob = await api.exportArchive(ids, "cookies", hosts);
      if (!blob || blob.size === 0) {
        throw new Error("Пустой ответ сервера при экспорте");
      }
      downloadBlob(blob, `antidetect_cookies_${stamp()}.zip`);
      setInfo(`Экспорт cookies: ${ids.length} профил., ${hosts.length} домен.`);
      setExportModal(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setExportModal(null);
    } finally {
      setBusy(false);
    }
  }

  async function toggleRun(p: Profile) {
    setBusy(true);
    setError("");
    try {
      if (p.running) {
        await api.stopProfile(p.profile_id);
      } else {
        await api.launchProfile(p.profile_id);
      }
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="stats">
        <div className="stat">
          Всего <strong>{profiles.length}</strong>
        </div>
        <div className="stat">
          Показано <strong>{filtered.length}</strong>
        </div>
        <div className="stat stat-live">
          В сети <strong>{profiles.filter((p) => p.running).length}</strong>
        </div>
        {selected.size ? (
          <div className="stat">
            Выбрано <strong>{selected.size}</strong>
          </div>
        ) : null}
      </div>

      <div className="toolbar">
        <div className="search-wrap">
          <input
            className="search"
            placeholder="Поиск по имени, id, тегу, прокси…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <button type="button" className="btn btn-secondary" onClick={() => void refresh()} disabled={busy}>
          Обновить
        </button>
        <button type="button" className="btn btn-secondary" onClick={() => fileRef.current?.click()} disabled={busy}>
          Импорт
        </button>
        <button type="button" className="btn" onClick={() => void startExport()} disabled={busy}>
          Экспорт{selected.size ? ` (${selected.size})` : ""}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".zip,application/zip"
          hidden
          onChange={(e) => void onImport(e.target.files?.[0])}
        />
      </div>

      {allTags.length ? (
        <div className="tags">
          {allTags.map((t) => (
            <button
              key={t}
              type="button"
              className={tagFilterClass(t, tagFilter.has(t))}
              onClick={() => toggleTag(t)}
            >
              {t}
            </button>
          ))}
        </div>
      ) : null}

      {error ? <div className="error">{error}</div> : null}
      {info ? <div className="ok-msg">{info}</div> : null}

      {filtered.length === 0 ? (
        <div className="empty">
          <p className="empty-title">Пусто</p>
          <p>Нет профилей{search || tagFilter.size ? " по текущему фильтру" : ""}.</p>
        </div>
      ) : (
        <div className="list">
          {filtered.map((p) => (
            <div className={`row${selected.has(p.profile_id) ? " selected" : ""}`} key={p.profile_id}>
              <input
                type="checkbox"
                checked={selected.has(p.profile_id)}
                onChange={() => toggleSelect(p.profile_id)}
                aria-label={`Выбрать ${p.name}`}
              />
              <div className="row-main">
                <div className="row-title">
                  <span>{p.name || "Без имени"}</span>
                  <span className={`status ${p.running ? "on" : "off"}`}>
                    <span className="dot" />
                    {p.running ? "Включён" : "Выкл"}
                  </span>
                </div>
                <div className="row-meta">
                  {p.profile_id}
                  {p.proxy_server ? ` · ${p.proxy_server}` : ""}
                  {p.country_code ? ` · ${p.country_code}` : ""}
                </div>
                {(p.tags || []).length ? (
                  <div className="row-tags">
                    {(p.tags || []).map((t) => (
                      <span className={tagPillClass(t)} key={t}>
                        {t}
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>
              <div className="row-actions">
                <button
                  type="button"
                  className={`btn btn-sm ${p.running ? "btn-danger" : "btn-secondary"}`}
                  disabled={busy}
                  onClick={() => void toggleRun(p)}
                >
                  {p.running ? "Остановить" : "Запустить"}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {exportModal
        ? createPortal(
            <div
              className="modal-backdrop"
              onClick={() => {
                if (exportModal.step !== "progress" && !busy) setExportModal(null);
              }}
            >
              <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
                {exportModal.step === "mode" ? (
                  <>
                    <h2>Экспорт профилей</h2>
                    <p>Выбрано: {exportModal.ids.length}. Полный архив или только cookies.</p>
                    <div className="modal-actions">
                      <button type="button" className="btn btn-secondary" onClick={() => setExportModal(null)}>
                        Отмена
                      </button>
                      <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={busy}
                        onClick={() => void openCookiesExport(exportModal.ids)}
                      >
                        Только cookies…
                      </button>
                      <button
                        type="button"
                        className="btn"
                        disabled={busy}
                        onClick={() => void doFullExport(exportModal.ids)}
                      >
                        Полный ZIP
                      </button>
                    </div>
                  </>
                ) : null}

                {exportModal.step === "hosts" ? (
                  <>
                    <h2>Сайты для cookies</h2>
                    <p>Отметьте домены, cookies которых нужно экспортировать.</p>
                    <div className="host-list">
                      {exportModal.hosts.map((h) => (
                        <label className="host-row" key={h.host}>
                          <input
                            type="checkbox"
                            checked={exportModal.selected.has(h.host)}
                            onChange={() => {
                              setExportModal((m) => {
                                if (!m || m.step !== "hosts") return m;
                                const selectedHosts = new Set(m.selected);
                                if (selectedHosts.has(h.host)) selectedHosts.delete(h.host);
                                else selectedHosts.add(h.host);
                                return { ...m, selected: selectedHosts };
                              });
                            }}
                          />
                          <span>{h.host}</span>
                          <span className="host-count">{h.count}</span>
                        </label>
                      ))}
                    </div>
                    <div className="modal-actions">
                      <button type="button" className="btn btn-secondary" onClick={() => setExportModal(null)}>
                        Отмена
                      </button>
                      <button
                        type="button"
                        className="btn"
                        disabled={busy}
                        onClick={() =>
                          void doCookiesExport(exportModal.ids, [...exportModal.selected])
                        }
                      >
                        Экспорт
                      </button>
                    </div>
                  </>
                ) : null}

                {exportModal.step === "progress" ? (
                  <>
                    <h2>Экспорт</h2>
                    <p className="export-progress">{exportModal.message}</p>
                    <div className="progress-bar" aria-hidden>
                      <div className="progress-bar-indeterminate" />
                    </div>
                  </>
                ) : null}
              </div>
            </div>,
            document.body,
          )
        : null}
    </>
  );
}

function stamp(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getUTCFullYear()}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}_` +
    `${p(d.getUTCHours())}${p(d.getUTCMinutes())}${p(d.getUTCSeconds())}`
  );
}
