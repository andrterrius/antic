import { Component, FormEvent, type ErrorInfo, type ReactNode, useEffect, useState } from "react";
import { ApiError, api, clearToken, getToken, setToken, type AuthUser } from "./api/client";
import { ProfilesPage } from "./pages/ProfilesPage";
import { ProxiesPage } from "./pages/ProxiesPage";
import { UsersPage } from "./pages/UsersPage";

type Section = "profiles" | "proxies" | "users";

class SectionErrorBoundary extends Component<
  { children: ReactNode; onReset?: () => void },
  { error: string | null }
> {
  state: { error: string | null } = { error: null };

  static getDerivedStateFromError(err: Error): { error: string } {
    return { error: err?.message || String(err) };
  }

  componentDidCatch(err: Error, info: ErrorInfo): void {
    console.error("Section crash:", err, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="error">
          Ошибка вкладки: {this.state.error}
          <div style={{ marginTop: 10 }}>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => {
                this.setState({ error: null });
                this.props.onReset?.();
              }}
            >
              Повторить
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

export default function App() {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [section, setSection] = useState<Section>("profiles");

  useEffect(() => {
    let cancelled = false;
    const token = getToken();
    if (!token) {
      setNeedsAuth(true);
      setReady(false);
      setChecking(false);
      return;
    }
    setChecking(true);
    setError("");
    void (async () => {
      try {
        const me = await api.me();
        if (cancelled) return;
        setUser(me);
        setNeedsAuth(false);
        setReady(true);
      } catch (e) {
        if (cancelled) return;
        setReady(false);
        setUser(null);
        if (e instanceof ApiError && e.status === 401) {
          setNeedsAuth(true);
          clearToken();
          setError("Сессия истекла — войдите снова");
        } else {
          setNeedsAuth(true);
          setError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        if (!cancelled) setChecking(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setChecking(true);
    try {
      const res = await api.login(username.trim(), password);
      setToken(res.token);
      setUser(res.user);
      setPassword("");
      setNeedsAuth(false);
      setReady(true);
    } catch (err) {
      setReady(false);
      setNeedsAuth(true);
      if (err instanceof ApiError && err.status === 401) {
        setError("Неверный логин или пароль");
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setChecking(false);
    }
  }

  async function onLogout() {
    try {
      await api.logout();
    } catch {
      /* ignore */
    }
    clearToken();
    setUser(null);
    setReady(false);
    setNeedsAuth(true);
    setSection("profiles");
  }

  if (!ready) {
    if (checking && !needsAuth && !error) {
      return (
        <div className="app token-gate">
          <div className="token-card">
            <div className="brand-block">
              <div className="brand-mark" aria-hidden />
              <div>
                <h1>Antidetect</h1>
                <p className="brand-sub">Подключение…</p>
              </div>
            </div>
          </div>
        </div>
      );
    }

    return (
      <div className="app token-gate">
        <form className="token-card" onSubmit={onSubmit}>
          <div className="brand-block">
            <div className="brand-mark" aria-hidden />
            <div>
              <h1>Antidetect</h1>
              <p className="brand-sub">Вход в веб-панель</p>
            </div>
          </div>
          {error ? <div className="error">{error}</div> : null}
          <label htmlFor="login-user">Логин</label>
          <input
            id="login-user"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
          />
          <label htmlFor="login-pass">Пароль</label>
          <input
            id="login-pass"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          <button className="btn" type="submit" disabled={checking}>
            {checking ? "Вход…" : "Войти"}
          </button>
        </form>
      </div>
    );
  }

  const isAdmin = Boolean(user?.is_admin);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark" aria-hidden />
          <div>
            <h1 className="brand">Antidetect</h1>
            <p className="brand-sub">
              {user?.username
                ? `${user.username}${isAdmin ? " · админ" : ""} · профили · прокси`
                : "Профили · прокси · импорт / экспорт"}
            </p>
          </div>
        </div>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onLogout}>
          Выйти
        </button>
      </header>

      <nav className="section-nav" aria-label="Разделы">
        <button
          type="button"
          className={`section-tab${section === "profiles" ? " active" : ""}`}
          onClick={() => setSection("profiles")}
        >
          Профили
        </button>
        <button
          type="button"
          className={`section-tab${section === "proxies" ? " active" : ""}`}
          onClick={() => setSection("proxies")}
        >
          Прокси
        </button>
        {isAdmin ? (
          <button
            type="button"
            className={`section-tab${section === "users" ? " active" : ""}`}
            onClick={() => setSection("users")}
          >
            Пользователи
          </button>
        ) : null}
      </nav>

      <SectionErrorBoundary key={section}>
        {section === "profiles" ? <ProfilesPage /> : null}
        {section === "proxies" ? <ProxiesPage /> : null}
        {section === "users" && user && isAdmin ? <UsersPage currentUser={user} /> : null}
      </SectionErrorBoundary>
    </div>
  );
}
