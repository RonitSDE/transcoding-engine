export type JobStatus = "awaiting_upload" | "uploaded" | "transcoding" | "ready" | "failed" | "expired";

export type Job = {
  id: number;
  filename: string;
  content_type: string;
  size_bytes: number;
  status: JobStatus;
  error: string | null;
  playlist_url: string | null;
};

export type Session = {
  user: { id: number; email: string };
  csrf: string;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", ...init });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : detail;
    } catch {
      /* empty body */
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function signup(email: string, password: string) {
  return request("/api/signup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
}

export function login(email: string, password: string) {
  return request<Session>("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
}

export function session() {
  return request<Session>("/api/session");
}

export function logout(csrf: string) {
  return request("/api/logout", { method: "POST", headers: { "X-CSRF-Token": csrf } });
}

export function createVideo(csrf: string, key: string, file: File) {
  return request<{ job: Job; upload_url: string }>("/api/videos", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrf,
      "Idempotency-Key": key,
    },
    body: JSON.stringify({
      filename: file.name,
      content_type: file.type,
      size_bytes: file.size,
    }),
  });
}

export function putFile(uploadUrl: string, file: File) {
  return request(uploadUrl, {
    method: "PUT",
    headers: { "Content-Type": file.type },
    body: file,
  });
}

export function completeVideo(csrf: string, id: number) {
  return request<Job>(`/api/videos/${id}/complete`, {
    method: "POST",
    headers: { "X-CSRF-Token": csrf },
  });
}

export function getVideo(id: number) {
  return request<Job>(`/api/videos/${id}`);
}

export function listVideos() {
  return request<Job[]>("/api/videos");
}
