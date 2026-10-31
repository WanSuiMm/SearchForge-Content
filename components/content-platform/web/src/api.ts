export interface ContentItem {
  content_id: string | number;
  author_id: string;
  author_name?: string | null;
  body: string;
  published_at?: string | null;
  created_at?: string | null;
  like_count?: number;
}

export interface NotificationItem {
  author_id: string;
  author_name?: string | null;
  body: string;
  created_at: string;
}

export interface AuthSession {
  access_token: string;
  user_id: string;
}

export interface FeedResponse {
  items?: ContentItem[];
  next_cursor?: string | null;
}

export interface ItemsResponse<T> {
  items?: T[];
}

export interface CurrentUserResponse {
  user_id: string;
}

export interface PublishResponse {
  replayed?: boolean;
}

export interface FollowResponse {
  created?: boolean;
}

export interface LikeResponse {
  like_count?: number;
}

export interface ApiRequestOptions {
  method?: "GET" | "POST" | "DELETE";
  headers?: HeadersInit;
  body?: BodyInit | null;
  token?: string | null;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function responseMessage(payload: unknown, status: number): string {
  if (payload && typeof payload === "object" && "error" in payload) {
    const error = (payload as { error?: unknown }).error;
    if (error && typeof error === "object" && "message" in error) {
      const message = (error as { message?: unknown }).message;
      if (typeof message === "string") return message;
    }
  }
  return "请求失败（" + status + "）";
}

/** Calls the current origin only; the service owns authentication and data. */
export async function apiRequest<T>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body !== undefined && options.body !== null) {
    headers.set("Content-Type", "application/json");
  }
  if (options.token) {
    headers.set("Authorization", "Bearer " + options.token);
  }

  const response = await fetch(path, {
    method: options.method ?? "GET",
    headers,
    body: options.body,
    cache: "no-store",
  });

  let payload: unknown = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) {
    throw new ApiError(responseMessage(payload, response.status), response.status);
  }
  return payload as T;
}
