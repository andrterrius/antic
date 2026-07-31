import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, type ProxyGroup } from "../api/client";

function healthLabel(g: ProxyGroup): { text: string; cls: string } {
  if (g.health_ok === true) return { text: "Рабочий", cls: "on" };
  if (g.health_ok === false) return { text: "Нерабочий", cls: "fail" };
  return { text: "Не проверен", cls: "off" };
}

function maskPassword(pw: string | null | undefined): string {
  const s = (pw || "").trim();
  if (!s) return "—";
  return "•".repeat(Math.min(10, Math.max(4, s.length)));
}

function groupKey(g: Pick<ProxyGroup, "proxy_server" | "proxy_username" | "proxy_password">): string {
  return `${g.proxy_server || ""}::${g.proxy_username || ""}::${g.proxy_password || ""}`;
}

type EditState = {
  match: ProxyGroup;
  proxy_server: string;
  proxy_username: string;
  proxy_password: string;
};

export function ProxiesPage() {
  const [groups, setGroups] = useState<ProxyGroup[]>([]);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [busy, setBusy] = useState(false);
  const [checkingKey, setCheckingKey] = useState<string | null>(null);
  const [scheme, setScheme] = useState<"http" | "socks5">("http");
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const [edit, setEdit] = useState<EditState | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    setError("");
    try {
      const data = await api.listProxies();
      setGroups(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setGroups([]);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const filtered = useMemo(() => {
    const list = Array.isArray(groups) ? groups : [];
    const tokens = search.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (!tokens.length) return list;
    return list.filter((g) => {
      const profiles = Array.isArray(g.profiles) ? g.profiles : [];
      const hay = [
        g.proxy_server || "",
        g.proxy_username || "",
        ...profiles.map((p) => `${p.profile_id} ${p.name}`),
      ]
        .join(" ")
        .toLowerCase();
      return tokens.every((t) => hay.includes(t));
    });
  }, [groups, search]);

  const stats = useMemo(() => {
    let ok = 0;
    let fail = 0;
    let unknown = 0;
    for (const g of filtered) {
      if (g.health_ok === true) ok += 1;
      else if (g.health_ok === false) fail += 1;
      else unknown += 1;
    }
    return { total: filtered.length, ok, fail, unknown };
  }, [filtered]);

  function openEdit(g: ProxyGroup) {
    setEdit({
      match: g,
      proxy_server: g.proxy_server || "",
      proxy_username: g.proxy_username || "",
      proxy_password: g.proxy_password || "",
    });
    setError("");
  }

  async function saveEdit() {
    if (!edit) return;
    const proxy_server = edit.proxy_server.trim();
    if (!proxy_server) {
      setError("Адрес сервера не может быть пустым");
      return;
    }
    setBusy(true);
    setError("");
    setInfo("");
    try {
      const res = await api.updateProxy({
        match_proxy_server: edit.match.proxy_server,
        match_proxy_username: edit.match.proxy_username,
        match_proxy_password: edit.match.proxy_password,
        proxy_server,
        proxy_username: edit.proxy_username.trim() || null,
        proxy_password: edit.proxy_password.trim() || null,
      });
      setEdit(null);
      if (res.updated === 0) {
        setInfo("Без изменений");
      } else {
        setInfo(`Обновлено профилей: ${res.updated}`);
      }
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function checkOne(g: ProxyGroup) {
    const key = groupKey(g);
    setBusy(true);
    setCheckingKey(key);
    setError("");
    setInfo("");
    try {
      const updated = await api.checkProxy({
        proxy_server: g.proxy_server,
        proxy_username: g.proxy_username,
        proxy_password: g.proxy_password,
      });
      setGroups((prev) => {
        const list = Array.isArray(prev) ? prev : [];
        return list.map((x) => (groupKey(x) === key ? updated : x));
      });
      setInfo(updated.health_message || (updated.health_ok ? "OK" : "Ошибка"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setCheckingKey(null);
    }
  }

  async function checkAll() {
    setBusy(true);
    setError("");
    setInfo("");
    try {
      const res = await api.checkAllProxies();
      setGroups(Array.isArray(res.groups) ? res.groups : []);
      setInfo(`Проверено: ${res.checked} · OK ${res.ok} · FAIL ${res.fail}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function doImport(text: string) {
    const raw = text.trim();
    if (!raw) {
      setError("Вставьте строки host:port:user:pass");
      return;
    }
    setBusy(true);
    setError("");
    setInfo("");
    try {
      const res = await api.importProxies(raw, scheme);
      setInfo(`Создано профилей: ${res.created}` + (res.skipped ? ` (пропущено строк: ${res.skipped})` : ""));
      setImportOpen(false);
      setImportText("");
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function onFile(file: File | undefined) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const text = typeof reader.result === "string" ? reader.result : "";
      setImportText(text);
      setImportOpen(true);
    };
    reader.onerror = () => setError("Не удалось прочитать файл");
    reader.readAsText(file);
  }

  return (
    <>
      <div className="stats">
        <div className="stat">
          Прокси <strong>{stats.total}</strong>
        </div>
        <div className="stat stat-live">
          OK <strong>{stats.ok}</strong>
        </div>
        <div className="stat">
          FAIL <strong>{stats.fail}</strong>
        </div>
        <div className="stat">
          Не проверено <strong>{stats.unknown}</strong>
        </div>
      </div>

      <div className="toolbar">
        <div className="search-wrap">
          <input
            className="search"
            placeholder="Поиск: сервер, логин, id профиля…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => void refresh()}>
          Обновить
        </button>
        <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => setImportOpen(true)}>
          Импорт…
        </button>
        <button type="button" className="btn" disabled={busy || !groups.length} onClick={() => void checkAll()}>
          {busy && !checkingKey ? "Проверка…" : "Проверить все"}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".txt,text/plain"
          hidden
          onChange={(e) => {
            onFile(e.target.files?.[0]);
            if (fileRef.current) fileRef.current.value = "";
          }}
        />
      </div>

      {error ? <div className="error">{error}</div> : null}
      {info ? <div className="ok-msg">{info}</div> : null}

      {filtered.length === 0 ? (
        <div className="empty">
          <p className="empty-title">Нет прокси</p>
          <p>
            {search
              ? "Ничего не найдено по фильтру."
              : "Импортируйте файл host:port:user:pass или задайте прокси в профилях."}
          </p>
        </div>
      ) : (
        <div className="list">
          {filtered.map((g) => {
            const h = healthLabel(g);
            const key = groupKey(g);
            const checking = checkingKey === key;
            const profiles = Array.isArray(g.profiles) ? g.profiles : [];
            return (
              <div className="row proxy-row" key={key}>
                <div className="row-main">
                  <div className="row-title">
                    <span className="proxy-server">{g.proxy_server}</span>
                    <span className={`status ${h.cls}`}>
                      <span className="dot" />
                      {h.text}
                    </span>
                  </div>
                  <div className="row-meta">
                    {g.proxy_username || "—"} · {maskPassword(g.proxy_password)} · профилей:{" "}
                    {g.profile_count ?? profiles.length}
                    {g.health_message ? ` · ${g.health_message}` : ""}
                  </div>
                  {profiles.length ? (
                    <div className="row-tags">
                      {profiles.slice(0, 8).map((p) => (
                        <span className="pill" key={p.profile_id} title={p.profile_id}>
                          {p.name || p.profile_id}
                        </span>
                      ))}
                      {profiles.length > 8 ? (
                        <span className="pill">+{profiles.length - 8}</span>
                      ) : null}
                    </div>
                  ) : null}
                  <div className="row-actions proxy-actions">
                    <button
                      type="button"
                      className="btn btn-sm btn-secondary"
                      disabled={busy}
                      onClick={() => openEdit(g)}
                    >
                      Изменить
                    </button>
                    <button
                      type="button"
                      className="btn btn-sm btn-secondary"
                      disabled={busy}
                      onClick={() => void checkOne(g)}
                    >
                      {checking ? "Проверка…" : "Проверить"}
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {importOpen
        ? createPortal(
            <div className="modal-backdrop" onClick={() => !busy && setImportOpen(false)}>
              <div className="modal" onClick={(e) => e.stopPropagation()}>
                <h2>Импорт прокси</h2>
                <p>
                  По одной строке: <code>host:port:user:pass</code>. Для каждой строки создаётся
                  новый профиль.
                </p>
                <div className="import-scheme">
                  <span>Схема</span>
                  <select
                    value={scheme}
                    onChange={(e) => setScheme(e.target.value as "http" | "socks5")}
                    disabled={busy}
                  >
                    <option value="http">http</option>
                    <option value="socks5">socks5</option>
                  </select>
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    disabled={busy}
                    onClick={() => fileRef.current?.click()}
                  >
                    Из файла…
                  </button>
                </div>
                <textarea
                  className="import-textarea"
                  rows={10}
                  placeholder={"1.2.3.4:8080:user:pass\n5.6.7.8:1080:u:p"}
                  value={importText}
                  onChange={(e) => setImportText(e.target.value)}
                  disabled={busy}
                />
                <div className="modal-actions">
                  <button
                    type="button"
                    className="btn btn-secondary"
                    disabled={busy}
                    onClick={() => setImportOpen(false)}
                  >
                    Отмена
                  </button>
                  <button
                    type="button"
                    className="btn"
                    disabled={busy || !importText.trim()}
                    onClick={() => void doImport(importText)}
                  >
                    {busy ? "Импорт…" : "Импортировать"}
                  </button>
                </div>
              </div>
            </div>,
            document.body,
          )
        : null}

      {edit
        ? createPortal(
            <div className="modal-backdrop" onClick={() => !busy && setEdit(null)}>
              <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
                <h2>Изменить прокси</h2>
                <p>
                  Изменения применятся ко всем профилям группы (
                  {edit.match.profile_count || edit.match.profiles?.length || 0}).
                </p>
                <label className="field-label" htmlFor="edit-proxy-server">
                  Сервер
                </label>
                <input
                  id="edit-proxy-server"
                  className="field-input"
                  value={edit.proxy_server}
                  onChange={(e) => setEdit({ ...edit, proxy_server: e.target.value })}
                  placeholder="http://host:port или socks5://host:port"
                  disabled={busy}
                  autoFocus
                />
                <label className="field-label" htmlFor="edit-proxy-user">
                  Логин
                </label>
                <input
                  id="edit-proxy-user"
                  className="field-input"
                  value={edit.proxy_username}
                  onChange={(e) => setEdit({ ...edit, proxy_username: e.target.value })}
                  placeholder="username"
                  disabled={busy}
                  autoComplete="off"
                />
                <label className="field-label" htmlFor="edit-proxy-pass">
                  Пароль
                </label>
                <input
                  id="edit-proxy-pass"
                  className="field-input"
                  type="text"
                  value={edit.proxy_password}
                  onChange={(e) => setEdit({ ...edit, proxy_password: e.target.value })}
                  placeholder="password"
                  disabled={busy}
                  autoComplete="off"
                />
                <div className="modal-actions">
                  <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => setEdit(null)}>
                    Отмена
                  </button>
                  <button
                    type="button"
                    className="btn"
                    disabled={busy || !edit.proxy_server.trim()}
                    onClick={() => void saveEdit()}
                  >
                    {busy ? "Сохранение…" : "Сохранить"}
                  </button>
                </div>
              </div>
            </div>,
            document.body,
          )
        : null}
    </>
  );
}
