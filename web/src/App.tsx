import { Component, FormEvent, type ErrorInfo, type ReactNode, useEffect, useState } from "react";
import { ApiError, api, getToken, setToken } from "./api/client";
import { ProfilesPage } from "./pages/ProfilesPage";
import { ProxiesPage } from "./pages/ProxiesPage";

type Section = "profiles" | "proxies";

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
  const [token, setTokenState] = useState(() => getToken());
  const [draft, setDraft] = useState(() => getToken());
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [section, setSection] = useState<Section>("profiles");

  useEffect(() => {
    let cancelled = false;
    setChecking(true);
    setError("");
    void (async () => {
      try {
        await api.listProfiles();
        if (cancelled) return;
        setNeedsAuth(false);
        setReady(true);
      } catch (e) {
        if (cancelled) return;
        setReady(false);
        if (e instanceof ApiError && e.status === 401) {
          setNeedsAuth(true);
          if (token) setError("Неверный токен");
        } else {
          setNeedsAuth(Boolean(token));
          setError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        if (!cancelled) setChecking(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const next = draft.trim();
    setToken(next);
    setTokenState(next);
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
              <p className="brand-sub">Веб-панель профилей</p>
            </div>
          </div>
          <p>
            {needsAuth
              ? "API защищён токеном. Введите Bearer из вывода serve / ANTIDETECT_API_TOKEN."
              : "Не удалось подключиться к API. Если включена авторизация — введите токен."}
          </p>
          {error ? <div className="error">{error}</div> : null}
          <label htmlFor="api-token">API token</label>
          <input
            id="api-token"
            type="password"
            autoComplete="off"
            placeholder="Вставьте токен…"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <button className="btn" type="submit" disabled={checking || !draft.trim()}>
            {checking ? "Проверка…" : "Войти"}
          </button>
        </form>
      </div>
    );
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark" aria-hidden />
          <div>
            <h1 className="brand">Antidetect</h1>
            <p className="brand-sub">Профили · прокси · импорт / экспорт</p>
          </div>
        </div>
        {needsAuth || token ? (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => {
              setToken("");
              setTokenState("");
              setDraft("");
              setReady(false);
              setNeedsAuth(true);
            }}
          >
            Сменить токен
          </button>
        ) : null}
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
      </nav>

      <SectionErrorBoundary key={section}>
        {section === "profiles" ? <ProfilesPage /> : <ProxiesPage />}
      </SectionErrorBoundary>
    </div>
  );
}
