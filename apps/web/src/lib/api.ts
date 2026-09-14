const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

export class ApiError extends Error {
  status: number;
  /** Machine-readable reason, when the backend supplies one. */
  code?: string;
  constructor(status: number, message: string, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("trader_token");
}

export function setToken(token: string | null) {
  if (token) localStorage.setItem("trader_token", token);
  else localStorage.removeItem("trader_token");
}

export async function api<T = unknown>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (res.status === 401 && typeof window !== "undefined" && !path.startsWith("/auth/")) {
    setToken(null);
    window.location.href = "/login";
    throw new ApiError(401, "Session expired");
  }
  if (!res.ok) {
    let detail = res.statusText;
    let code: string | undefined;
    try {
      const body = await res.json();
      if (body.detail && typeof body.detail === "object") {
        code = body.detail.code;
        detail = body.detail.message ?? JSON.stringify(body.detail);
      } else {
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
      }
    } catch {
      /* keep statusText */
    }
    // Two-factor is mandatory: a user who hasn't enrolled can reach nothing
    // but the setup flow, so send them there rather than showing an error on
    // a page that will never load.
    if (
      code === "totp_setup_required" &&
      typeof window !== "undefined" &&
      !window.location.pathname.startsWith("/security/setup")
    ) {
      window.location.href = "/security/setup";
    }
    throw new ApiError(res.status, detail, code);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}
