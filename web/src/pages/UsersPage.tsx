import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, type AuthUser } from "../api/client";

type Props = {
  currentUser: AuthUser;
};

export function UsersPage({ currentUser }: Props) {
  const [users, setUsers] = useState<AuthUser[]>([]);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);

  const refresh = useCallback(async () => {
    setError("");
    try {
      const list = await api.listUsers();
      setUsers(Array.isArray(list) ? list : []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setUsers([]);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function onCreate(e: FormEvent) {
    e.preventDefault();
    setError("");
    setInfo("");
    setBusy(true);
    try {
      await api.createUser({
        username: username.trim(),
        password,
        is_admin: isAdmin,
      });
      setUsername("");
      setPassword("");
      setIsAdmin(false);
      setInfo("Пользователь создан.");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function onDelete(u: AuthUser) {
    if (u.username === currentUser.username) return;
    if (
      !window.confirm(
        `Удалить пользователя «${u.username}»? Сессии и данные профилей/прокси будут удалены.`,
      )
    ) {
      return;
    }
    setError("");
    setInfo("");
    setBusy(true);
    try {
      await api.deleteUser(u.username);
      setInfo(`Пользователь «${u.username}» удалён.`);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack" style={{ gap: 16, padding: "0 4px" }}>
      <div className="toolbar">
        <div>
          <h2 className="brand" style={{ fontSize: 18, margin: 0 }}>
            Пользователи
          </h2>
          <p className="brand-sub" style={{ margin: "4px 0 0" }}>
            Управление доступом к веб-панели и отдельным хранилищам профилей
          </p>
        </div>
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => void refresh()}>
          Обновить
        </button>
      </div>

      {error ? <div className="error">{error}</div> : null}
      {info ? <p className="brand-sub">{info}</p> : null}

      <div className="token-card" style={{ maxWidth: "100%", margin: 0 }}>
        <h3 style={{ margin: "0 0 12px", fontSize: 15 }}>Создать пользователя</h3>
        <form
          onSubmit={onCreate}
          style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "end" }}
        >
          <div style={{ minWidth: 160, flex: 1 }}>
            <label htmlFor="new-user">Логин</label>
            <input
              id="new-user"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              minLength={2}
              autoComplete="off"
            />
          </div>
          <div style={{ minWidth: 160, flex: 1 }}>
            <label htmlFor="new-pass">Пароль</label>
            <input
              id="new-pass"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={4}
              autoComplete="new-password"
            />
          </div>
          <label className="check" style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
            <input
              type="checkbox"
              checked={isAdmin}
              onChange={(e) => setIsAdmin(e.target.checked)}
            />
            Админ
          </label>
          <button className="btn" type="submit" disabled={busy}>
            {busy ? "Создание…" : "Создать"}
          </button>
        </form>
      </div>

      <div className="table-wrap" style={{ overflowX: "auto" }}>
        <table className="data-table" style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left", padding: "10px 12px" }}>Логин</th>
              <th style={{ textAlign: "left", padding: "10px 12px" }}>Язык</th>
              <th style={{ textAlign: "left", padding: "10px 12px" }}>Роль</th>
              <th style={{ textAlign: "right", padding: "10px 12px" }} />
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.username}>
                <td style={{ padding: "10px 12px" }}>
                  {u.username}
                  {u.username === currentUser.username ? (
                    <span className="brand-sub"> (вы)</span>
                  ) : null}
                </td>
                <td style={{ padding: "10px 12px" }}>{u.locale || "ru"}</td>
                <td style={{ padding: "10px 12px" }}>
                  {u.is_admin ? "Админ" : "Пользователь"}
                </td>
                <td style={{ padding: "10px 12px", textAlign: "right" }}>
                  {u.username === currentUser.username ? null : (
                    <button
                      type="button"
                      className="btn btn-danger btn-sm"
                      disabled={busy}
                      onClick={() => void onDelete(u)}
                    >
                      Удалить
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {users.length === 0 ? (
              <tr>
                <td colSpan={4} style={{ padding: "16px 12px" }} className="brand-sub">
                  Список пуст
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}
