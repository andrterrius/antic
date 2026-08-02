const TOKEN_KEY = "antidetect_api_token";

export type AuthUser = {
  username: string;
  locale: string;
  is_admin?: boolean;
};

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(token: string): void {
  const t = token.trim();
  if (!t) {
    localStorage.removeItem(TOKEN_KEY);
    return;
  }
  localStorage.setItem(TOKEN_KEY, t);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

async function parseDetail(res: Response): Promise<string> {
  try {
    const data = await res.json();
    if (typeof data.detail === "string") return data.detail;
    return JSON.stringify(data.detail ?? data);
  } catch {
    return res.statusText || `HTTP ${res.status}`;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers || {});
  const isForm = typeof FormData !== "undefined" && init.body instanceof FormData;
  if (!headers.has("Content-Type") && init.body && !isForm) {
    headers.set("Content-Type", "application/json");
  }
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const res = await fetch(path, { ...init, headers });
  if (!res.ok) {
    if (res.status === 401) {
      clearToken();
    }
    throw new ApiError(res.status, await parseDetail(res));
  }
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return (await res.json()) as T;
  }
  // HTML/SPA fallback instead of JSON — treat as missing API route
  const preview = (await res.text()).slice(0, 80);
  throw new ApiError(
    res.status,
    `Expected JSON from ${path}, got ${ct || "unknown"}: ${JSON.stringify(preview)}`,
  );
}

export type Profile = {
  profile_id: string;
  name: string;
  tags: string[];
  description?: string | null;
  proxy_server?: string | null;
  proxy_username?: string | null;
  proxy_password?: string | null;
  proxy_health_ok?: boolean | null;
  proxy_health_checked_at?: string | null;
  proxy_health_message?: string | null;
  country_code?: string | null;
  running?: boolean;
};

export type ImportResult = {
  imported: number;
  remapped: number;
  total: number;
};

export type DeleteProfilesResult = {
  deleted: number;
  deleted_ids: string[];
  total: number;
};

export type CookieHost = { host: string; count: number };

export type ProxyProfileRef = {
  profile_id: string;
  name: string;
};

export type ProxyGroup = {
  proxy_server: string;
  proxy_username?: string | null;
  proxy_password?: string | null;
  profile_count: number;
  profiles: ProxyProfileRef[];
  health_ok?: boolean | null;
  health_checked_at?: string | null;
  health_message?: string | null;
};

export type ProxyImportResult = {
  created: number;
  skipped: number;
  profiles: Profile[];
};

export const api = {
  health: () => request<{ status: string }>("/health", { method: "GET" }),

  login: (username: string, password: string) =>
    request<{ token: string; user: AuthUser }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),

  logout: () => request<{ ok: boolean }>("/auth/logout", { method: "POST" }),

  me: () => request<AuthUser>("/auth/me"),

  createUser: (body: {
    username: string;
    password: string;
    locale?: string;
    is_admin?: boolean;
  }) =>
    request<AuthUser>("/auth/users", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listUsers: () => request<AuthUser[]>("/auth/users"),

  deleteUser: (username: string) =>
    request<{ ok: boolean; username: string; purged_data: boolean }>(
      `/auth/users/${encodeURIComponent(username)}`,
      { method: "DELETE" },
    ),

  listProfiles: () => request<Profile[]>("/profiles"),

  listProxies: async () => {
    const data = await request<ProxyGroup[]>("/proxies");
    return Array.isArray(data) ? data : [];
  },

  checkProxy: (body: {
    proxy_server: string;
    proxy_username?: string | null;
    proxy_password?: string | null;
  }) =>
    request<ProxyGroup>("/proxies/check", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  checkAllProxies: () =>
    request<{ checked: number; ok: number; fail: number; groups: ProxyGroup[] }>(
      "/proxies/check-all",
      { method: "POST" },
    ),

  importProxies: (text: string, proxy_scheme: "http" | "socks5" = "http") =>
    request<ProxyImportResult>("/proxies/import", {
      method: "POST",
      body: JSON.stringify({ text, proxy_scheme }),
    }),

  updateProxy: (body: {
    match_proxy_server: string;
    match_proxy_username?: string | null;
    match_proxy_password?: string | null;
    proxy_server: string;
    proxy_username?: string | null;
    proxy_password?: string | null;
  }) =>
    request<{ updated: number; group: ProxyGroup }>("/proxies", {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  launchProfile: (profileId: string, body: Record<string, unknown> = {}) =>
    request<{ session_id: string }>(`/profiles/${encodeURIComponent(profileId)}/launch`, {
      method: "POST",
      body: JSON.stringify({
        headless: false,
        expose_cdp: true,
        start_url: "https://studio.youtube.com",
        ...body,
      }),
    }),

  stopProfile: (profileId: string) =>
    request<{ status: string }>(`/profiles/${encodeURIComponent(profileId)}/stop`, {
      method: "POST",
    }),

  importArchive: async (file: File): Promise<ImportResult> => {
    const fd = new FormData();
    fd.append("file", file);
    return request<ImportResult>("/profiles/import", { method: "POST", body: fd });
  },

  deleteProfiles: (profileIds: string[], purgeData = true) =>
    request<DeleteProfilesResult>("/profiles/delete", {
      method: "POST",
      body: JSON.stringify({ profile_ids: profileIds, purge_data: purgeData }),
    }),

  cookieHosts: (profileIds: string[]) =>
    request<{ hosts: CookieHost[] }>("/profiles/cookie-hosts", {
      method: "POST",
      body: JSON.stringify({ profile_ids: profileIds }),
    }),

  exportArchive: async (
    profileIds: string[],
    mode: "full" | "cookies",
    hosts: string[] = [],
  ): Promise<Blob> => {
    const headers = new Headers({ "Content-Type": "application/json" });
    const token = getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const res = await fetch("/profiles/export", {
      method: "POST",
      headers,
      body: JSON.stringify({ profile_ids: profileIds, mode, hosts }),
    });
    if (!res.ok) {
      throw new ApiError(res.status, await parseDetail(res));
    }
    const ct = (res.headers.get("content-type") || "").toLowerCase();
    if (ct.includes("application/json")) {
      const data = await res.json();
      throw new ApiError(
        500,
        typeof data.detail === "string" ? data.detail : "Export returned JSON instead of ZIP",
      );
    }
    const blob = await res.blob();
    if (!blob.size) {
      throw new ApiError(500, "Empty ZIP from server");
    }
    return blob;
  },
};

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  window.setTimeout(() => {
    a.remove();
    URL.revokeObjectURL(url);
  }, 1500);
}
