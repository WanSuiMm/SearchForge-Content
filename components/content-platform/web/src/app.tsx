import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  apiRequest,
  ApiError,
  type AuthSession,
  type ContentItem,
  type CurrentUserResponse,
  type FeedResponse,
  type FollowResponse,
  type ItemsResponse,
  type LikeResponse,
  type NotificationItem,
  type PublishResponse,
  type ApiRequestOptions,
} from "./api";

const SESSION_TOKEN_KEY = "content-platform.session-token";
const SESSION_USER_KEY = "content-platform.user-id";

type Feedback = { message: string; kind?: "error" | "success" };
type AuthMode = "login" | "register";
type PublishRetry = { body: string; key: string };

function storedValue(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function makeIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "web-" + Date.now() + "-" + Math.random().toString(36).slice(2);
}

function formatTime(value?: string | null): string {
  const date = new Date(value || "");
  if (!value || Number.isNaN(date.getTime())) return "刚刚";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function FeedbackText({ value, className }: { value: Feedback; className: string }) {
  const classes = [
    className,
    value.kind === "success" && value.message ? "is-success" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <p className={classes} role="status" aria-live="polite">
      {value.message}
    </p>
  );
}

function PostCard({
  item,
  liked,
  busy,
  onLike,
}: {
  item: ContentItem;
  liked: boolean;
  busy: boolean;
  onLike: (contentId: string) => void;
}) {
  const contentId = String(item.content_id);
  const author = item.author_name || item.author_id || "未知作者";
  const publishedAt = item.published_at || item.created_at;
  const count =
    typeof item.like_count === "number" && Number.isFinite(item.like_count)
      ? item.like_count
      : 0;
  return (
    <article className="post-card">
      <div className="post-meta">
        <span className="avatar" aria-hidden="true">
          {author.trim().slice(0, 1).toUpperCase()}
        </span>
        <div className="post-author">
          <strong>{author}</strong>
          <time dateTime={publishedAt || undefined}>{formatTime(publishedAt)}</time>
        </div>
      </div>
      <p className="post-body">{item.body || ""}</p>
      <div className="post-actions">
        <button
          type="button"
          className={"like-button" + (liked ? " is-liked" : "")}
          disabled={liked || busy}
          aria-label={liked ? "已点赞" : "点赞这条内容"}
          onClick={() => onLike(contentId)}
        >
          {liked ? "♥ 已点赞" : "♡ 点赞"} · {count}
        </button>
        <span className="post-id">内容 {contentId}</span>
      </div>
    </article>
  );
}

function NotificationCard({ item }: { item: NotificationItem }) {
  return (
    <article className="notification-card">
      <span className="notification-icon" aria-hidden="true">
        ↗
      </span>
      <div className="notification-copy">
        <strong>{(item.author_name || item.author_id || "有人") + " 发布了新内容"}</strong>
        <p>{item.body || ""}</p>
        <time dateTime={item.created_at}>{formatTime(item.created_at)}</time>
      </div>
    </article>
  );
}

export function App() {
  const [token, setToken] = useState<string | null>(() =>
    storedValue(SESSION_TOKEN_KEY),
  );
  const [userId, setUserId] = useState<string | null>(() =>
    storedValue(SESSION_USER_KEY),
  );
  const tokenRef = useRef(token);
  const initialUserId = useRef(userId);
  const feedCursorRef = useRef<string | null>(null);
  const publishRetry = useRef<PublishRetry | null>(null);

  const [serviceOnline, setServiceOnline] = useState(false);
  const [authMode, setAuthMode] = useState<AuthMode>("login");
  const [authBusy, setAuthBusy] = useState(false);
  const [authUserId, setAuthUserId] = useState("");
  const [authDisplayName, setAuthDisplayName] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [authFeedback, setAuthFeedback] = useState<Feedback>({ message: "" });
  const [logoutBusy, setLogoutBusy] = useState(false);

  const [publishBody, setPublishBody] = useState("");
  const [publishBusy, setPublishBusy] = useState(false);
  const [publishFeedback, setPublishFeedback] = useState<Feedback>({ message: "" });
  const [followeeId, setFolloweeId] = useState("");
  const [followBusy, setFollowBusy] = useState(false);
  const [followFeedback, setFollowFeedback] = useState<Feedback>({ message: "" });
  const [searchInput, setSearchInput] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchItems, setSearchItems] = useState<ContentItem[]>([]);
  const [searchVisible, setSearchVisible] = useState(false);
  const [searchBusy, setSearchBusy] = useState(false);
  const [searchFeedback, setSearchFeedback] = useState<Feedback>({ message: "" });
  const [globalFeedback, setGlobalFeedback] = useState<Feedback>({ message: "" });

  const [feedItems, setFeedItems] = useState<ContentItem[]>([]);
  const [feedCursor, setFeedCursor] = useState<string | null>(null);
  const [feedBusy, setFeedBusy] = useState(false);
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [notificationsFeedback, setNotificationsFeedback] = useState<Feedback>({
    message: "",
  });
  const [likedIds, setLikedIds] = useState<Set<string>>(() => new Set());
  const [pendingLikeIds, setPendingLikeIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [refreshBusy, setRefreshBusy] = useState(false);

  useEffect(() => {
    tokenRef.current = token;
  }, [token]);

  const clearWorkspace = useCallback(() => {
    setPublishBody("");
    setFolloweeId("");
    setSearchInput("");
    setSearchQuery("");
    setSearchItems([]);
    setSearchVisible(false);
    setFeedItems([]);
    feedCursorRef.current = null;
    setFeedCursor(null);
    setNotifications([]);
    setLikedIds(new Set());
    setPendingLikeIds(new Set());
    setPublishFeedback({ message: "" });
    setFollowFeedback({ message: "" });
    setSearchFeedback({ message: "" });
    setNotificationsFeedback({ message: "" });
    setGlobalFeedback({ message: "" });
    publishRetry.current = null;
  }, []);

  const clearSession = useCallback(() => {
    tokenRef.current = null;
    setToken(null);
    setUserId(null);
    try {
      sessionStorage.removeItem(SESSION_TOKEN_KEY);
      sessionStorage.removeItem(SESSION_USER_KEY);
    } catch {
      // The in-memory session still ends if browser storage is unavailable.
    }
    clearWorkspace();
    setAuthUserId("");
    setAuthDisplayName("");
    setAuthPassword("");
  }, [clearWorkspace]);

  const request = useCallback(
    async <T,>(
      path: string,
      options: ApiRequestOptions = {},
    ): Promise<T> => {
      const requestToken = tokenRef.current;
      try {
        const result = await apiRequest<T>(path, {
          ...options,
          token: requestToken,
        });
        setServiceOnline(true);
        return result;
      } catch (error) {
        setServiceOnline(!(error instanceof TypeError));
        if (
          error instanceof ApiError &&
          error.status === 401 &&
          tokenRef.current
        ) {
          clearSession();
          setAuthMode("login");
          setAuthFeedback({
            message: "登录状态已失效，请重新登录。",
            kind: "error",
          });
        }
        throw error;
      }
    },
    [clearSession],
  );

  const loadFeed = useCallback(
    async ({ append }: { append: boolean }) => {
      const params = new URLSearchParams({ limit: "20" });
      if (append && feedCursorRef.current) {
        params.set("cursor", feedCursorRef.current);
      }
      setFeedBusy(true);
      try {
        const result = await request<FeedResponse>(
          "/v1/feed?" + params.toString(),
        );
        const items = Array.isArray(result.items) ? result.items : [];
        setFeedItems((current) => (append ? current.concat(items) : items));
        feedCursorRef.current = result.next_cursor || null;
        setFeedCursor(feedCursorRef.current);
      } finally {
        setFeedBusy(false);
      }
    },
    [request],
  );

  const loadNotifications = useCallback(async () => {
    const result = await request<ItemsResponse<NotificationItem>>(
      "/v1/notifications?limit=20",
    );
    setNotifications(Array.isArray(result.items) ? result.items : []);
    setNotificationsFeedback({ message: "" });
  }, [request]);

  const refreshWorkspace = useCallback(async () => {
    if (!tokenRef.current) return;
    setRefreshBusy(true);
    await Promise.allSettled([
      loadFeed({ append: false }).catch((error: unknown) => {
        setGlobalFeedback({
          message: "Feed：" + errorMessage(error, "加载失败。"),
        });
      }),
      loadNotifications().catch((error: unknown) => {
        setNotificationsFeedback({
          message: errorMessage(error, "通知加载失败，请稍后重试。"),
        });
      }),
    ]);
    setRefreshBusy(false);
  }, [loadFeed, loadNotifications]);

  const establishSession = useCallback(
    async (session: AuthSession) => {
      tokenRef.current = session.access_token;
      setToken(session.access_token);
      setUserId(session.user_id);
      let storageWarning = false;
      try {
        sessionStorage.setItem(SESSION_TOKEN_KEY, session.access_token);
        sessionStorage.setItem(SESSION_USER_KEY, session.user_id);
      } catch {
        storageWarning = true;
      }
      feedCursorRef.current = null;
      setFeedCursor(null);
      setFeedItems([]);
      setLikedIds(new Set());
      setGlobalFeedback(
        storageWarning
          ? { message: "浏览器无法保存会话；刷新页面后需要重新登录。" }
          : { message: "", kind: "success" },
      );
      await refreshWorkspace();
    },
    [refreshWorkspace],
  );

  useEffect(() => {
    let active = true;
    const restore = async () => {
      if (!tokenRef.current || !initialUserId.current) {
        clearSession();
        try {
          await request("/healthz");
        } catch {
          // The connection indicator is updated by request().
        }
        return;
      }
      try {
        const current = await request<CurrentUserResponse>("/v1/me");
        if (!active) return;
        setUserId(current.user_id);
        try {
          sessionStorage.setItem(SESSION_USER_KEY, current.user_id);
        } catch {
          // The current tab remains authenticated in memory.
        }
        await refreshWorkspace();
      } catch (error) {
        if (active && tokenRef.current) {
          setGlobalFeedback({
            message: "暂时无法读取内容：" + errorMessage(error, "请求失败。"),
          });
        }
      }
    };
    void restore();
    return () => {
      active = false;
    };
  }, [clearSession, refreshWorkspace, request]);

  const submitAuth = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const registering = authMode === "register";
    const normalizedUserId = authUserId.trim();
    const normalizedDisplayName = authDisplayName.trim();
    if (
      !normalizedUserId ||
      authPassword.length < 12 ||
      (registering && !normalizedDisplayName)
    ) {
      setAuthFeedback({
        message: "请填写完整信息；密码至少需要 12 个字符。",
      });
      return;
    }

    setAuthBusy(true);
    setAuthFeedback({ message: "" });
    try {
      const body: {
        user_id: string;
        password: string;
        display_name?: string;
      } = { user_id: normalizedUserId, password: authPassword };
      if (registering) body.display_name = normalizedDisplayName;
      const session = await request<AuthSession>(
        registering ? "/v1/users" : "/v1/sessions",
        { method: "POST", body: JSON.stringify(body) },
      );
      setAuthUserId("");
      setAuthDisplayName("");
      setAuthPassword("");
      await establishSession(session);
    } catch (error) {
      setAuthFeedback({
        message: errorMessage(error, "无法连接服务，请稍后重试。"),
      });
    } finally {
      setAuthBusy(false);
    }
  };

  const submitPost = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const body = publishBody.trim();
    if (!body) {
      setPublishFeedback({ message: "内容不能为空。" });
      return;
    }
    if (!publishRetry.current || publishRetry.current.body !== body) {
      publishRetry.current = { body, key: makeIdempotencyKey() };
    }
    setPublishBusy(true);
    setPublishFeedback({ message: "" });
    try {
      const result = await request<PublishResponse>("/v1/content", {
        method: "POST",
        headers: { "Idempotency-Key": publishRetry.current.key },
        body: JSON.stringify({ body }),
      });
      publishRetry.current = null;
      setPublishBody("");
      setPublishFeedback({
        message: result.replayed ? "已恢复上次发布结果。" : "发布成功。",
        kind: "success",
      });
      setGlobalFeedback({
        message: "新内容已写入服务并刷新 Feed。",
        kind: "success",
      });
      try {
        await loadFeed({ append: false });
      } catch (error) {
        setGlobalFeedback({
          message:
            "内容已发布，但 Feed 刷新失败：" +
            errorMessage(error, "请求失败。"),
        });
      }
    } catch (error) {
      setPublishFeedback({
        message:
          errorMessage(error, "发布失败。") +
          " 再次提交会沿用同一个幂等键。",
      });
    } finally {
      setPublishBusy(false);
    }
  };

  const submitFollow = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const target = followeeId.trim();
    if (!target) return;
    setFollowBusy(true);
    setFollowFeedback({ message: "" });
    try {
      const result = await request<FollowResponse>("/v1/follows", {
        method: "POST",
        body: JSON.stringify({ followee_id: target }),
      });
      setFolloweeId("");
      setFollowFeedback({
        message: result.created
          ? "现在会看到 " + target + " 的内容。"
          : "你已经关注 " + target + "。",
        kind: "success",
      });
      try {
        await loadFeed({ append: false });
      } catch (error) {
        setGlobalFeedback({
          message:
            "关注关系已保存，但 Feed 刷新失败：" +
            errorMessage(error, "请求失败。"),
        });
      }
    } catch (error) {
      setFollowFeedback({
        message: errorMessage(error, "关注失败，请稍后重试。"),
      });
    } finally {
      setFollowBusy(false);
    }
  };

  const likeContent = async (contentId: string) => {
    if (likedIds.has(contentId) || pendingLikeIds.has(contentId)) return;
    setPendingLikeIds((current) => new Set(current).add(contentId));
    try {
      const result = await request<LikeResponse>(
        "/v1/content/" + encodeURIComponent(contentId) + "/likes",
        { method: "POST" },
      );
      const count = Number.isFinite(result.like_count)
        ? Number(result.like_count)
        : undefined;
      setLikedIds((current) => new Set(current).add(contentId));
      if (count !== undefined) {
        const updateCount = (items: ContentItem[]) =>
          items.map((item) =>
            String(item.content_id) === contentId
              ? { ...item, like_count: count }
              : item,
          );
        setFeedItems(updateCount);
        setSearchItems(updateCount);
      }
    } catch (error) {
      setGlobalFeedback({
        message: errorMessage(error, "点赞失败，请稍后重试。"),
      });
    } finally {
      setPendingLikeIds((current) => {
        const next = new Set(current);
        next.delete(contentId);
        return next;
      });
    }
  };

  const submitSearch = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const query = searchInput.trim();
    if (!query) return;
    setSearchBusy(true);
    setSearchFeedback({ message: "" });
    try {
      const params = new URLSearchParams({ q: query, limit: "20" });
      const result = await request<ItemsResponse<ContentItem>>(
        "/v1/search?" + params.toString(),
      );
      setSearchItems(Array.isArray(result.items) ? result.items : []);
      setSearchVisible(true);
      setSearchQuery(query);
    } catch (error) {
      setSearchFeedback({
        message: errorMessage(error, "搜索失败，请稍后重试。"),
      });
    } finally {
      setSearchBusy(false);
    }
  };

  const logout = async () => {
    if (logoutBusy) return;
    setLogoutBusy(true);
    let serverRevocationConfirmed = false;
    try {
      await request("/v1/sessions/current", { method: "DELETE" });
      serverRevocationConfirmed = true;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        serverRevocationConfirmed = true;
      } else {
        setGlobalFeedback({
          message: "退出请求未确认：" + errorMessage(error, "请求失败。"),
        });
      }
    } finally {
      clearSession();
      setAuthMode("login");
      setAuthFeedback({
        message: serverRevocationConfirmed
          ? "已退出登录。"
          : "已清除当前标签页会话，但服务端撤销状态未确认。",
        kind: serverRevocationConfirmed ? "success" : "error",
      });
      setLogoutBusy(false);
    }
  };

  const clearSearch = () => {
    setSearchQuery("");
    setSearchInput("");
    setSearchItems([]);
    setSearchVisible(false);
    setSearchFeedback({ message: "" });
  };

  const isSignedIn = Boolean(token);
  const registering = authMode === "register";
  return (
    <div className="page-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="ContentPlatform 首页">
          <span className="brand-mark" aria-hidden="true">C</span>
          <span>
            <strong>ContentPlatform</strong>
            <small>内容社区</small>
          </span>
        </a>
        <div className="topbar-status">
          <span
            className={"service-indicator" + (serviceOnline ? " is-online" : "")}
            aria-hidden="true"
          />
          <span>{serviceOnline ? "本地服务已连接" : "等待本地服务"}</span>
        </div>
        {isSignedIn && (
          <div className="account-tools">
            <span className="user-chip">{userId || "已登录"}</span>
            <button
              className="button button-quiet button-small"
              type="button"
              disabled={logoutBusy}
              onClick={() => void logout()}
            >
              {logoutBusy ? "正在退出…" : "退出登录"}
            </button>
          </div>
        )}
      </header>

      <main>
        {!isSignedIn ? (
          <section className="auth-layout" aria-labelledby="auth-title">
            <div className="intro-copy">
              <span className="eyebrow">分享 · 发现 · 连接</span>
              <h1 id="auth-title">让好内容<br /><span>自然流动</span></h1>
              <p>发布你的想法，关注创作者，在实时 Feed 和全文搜索里发现值得阅读的内容。</p>
              <div className="feature-list" aria-label="产品功能">
                <div><span className="feature-dot">01</span><span>按关注关系生成个人 Feed</span></div>
                <div><span className="feature-dot">02</span><span>搜索真实发布的内容</span></div>
                <div><span className="feature-dot">03</span><span>点赞与作者通知一目了然</span></div>
              </div>
            </div>

            <div className="auth-card panel">
              <div className="auth-heading">
                <div>
                  <span className="eyebrow">欢迎回来</span>
                  <h2>{registering ? "创建账号" : "登录账号"}</h2>
                </div>
                <div className="auth-tabs" role="tablist" aria-label="账号操作">
                  {(["login", "register"] as const).map((mode) => (
                    <button
                      key={mode}
                      className={"auth-tab" + (authMode === mode ? " is-active" : "")}
                      type="button"
                      role="tab"
                      aria-selected={authMode === mode}
                      onClick={() => {
                        setAuthMode(mode);
                        setAuthFeedback({ message: "" });
                      }}
                    >
                      {mode === "login" ? "登录" : "注册"}
                    </button>
                  ))}
                </div>
              </div>
              <form onSubmit={(event) => void submitAuth(event)} noValidate>
                <label className="field" htmlFor="auth-user-id">
                  <span>账号 ID</span>
                  <input
                    id="auth-user-id"
                    name="user_id"
                    autoComplete="username"
                    maxLength={100}
                    required
                    placeholder="例如：alice"
                    value={authUserId}
                    onChange={(event) => setAuthUserId(event.currentTarget.value)}
                  />
                </label>
                {registering && (
                  <label className="field" htmlFor="auth-display-name">
                    <span>显示名称</span>
                    <input
                      id="auth-display-name"
                      name="display_name"
                      autoComplete="name"
                      maxLength={200}
                      required
                      placeholder="社区里展示的名字"
                      value={authDisplayName}
                      onChange={(event) => setAuthDisplayName(event.currentTarget.value)}
                    />
                  </label>
                )}
                <label className="field" htmlFor="auth-password">
                  <span>密码</span>
                  <input
                    id="auth-password"
                    name="password"
                    type="password"
                    autoComplete={registering ? "new-password" : "current-password"}
                    minLength={12}
                    maxLength={256}
                    required
                    placeholder="至少 12 个字符"
                    value={authPassword}
                    onChange={(event) => setAuthPassword(event.currentTarget.value)}
                  />
                </label>
                <p className="field-hint">
                  {registering
                    ? "密码长度需要为 12–256 个字符。"
                    : "密码长度为 12–256 个字符。"}
                </p>
                <button
                  className="button button-primary button-wide"
                  type="submit"
                  disabled={authBusy}
                >
                  {authBusy
                    ? registering ? "正在创建…" : "正在登录…"
                    : registering ? "创建并登录" : "登录"}
                </button>
                <FeedbackText value={authFeedback} className="form-feedback" />
              </form>
              <p className="privacy-note">会话仅保存在当前浏览器标签页，退出后立即撤销。</p>
            </div>
          </section>
        ) : (
          <section className="workspace" aria-label="内容工作台">
            <aside className="workspace-sidebar">
              <section className="panel compose-panel" aria-labelledby="compose-title">
                <div className="section-heading">
                  <div>
                    <span className="eyebrow">开始分享</span>
                    <h2 id="compose-title">发布内容</h2>
                  </div>
                  <span className="heading-icon" aria-hidden="true">✎</span>
                </div>
                <form onSubmit={(event) => void submitPost(event)}>
                  <label className="visually-hidden" htmlFor="publish-body">内容正文</label>
                  <textarea
                    id="publish-body"
                    name="body"
                    rows={5}
                    maxLength={20000}
                    required
                    placeholder="今天有什么想法？"
                    value={publishBody}
                    onChange={(event) => setPublishBody(event.currentTarget.value)}
                  />
                  <div className="compose-footer">
                    <span className="field-hint">发布后会进入关注者的 Feed</span>
                    <button className="button button-primary" type="submit" disabled={publishBusy}>
                      {publishBusy ? "正在发布…" : "发布"}
                    </button>
                  </div>
                  <FeedbackText value={publishFeedback} className="form-feedback" />
                </form>
              </section>

              <section className="panel follow-panel" aria-labelledby="follow-title">
                <div className="section-heading">
                  <div>
                    <span className="eyebrow">扩展你的 Feed</span>
                    <h2 id="follow-title">关注创作者</h2>
                  </div>
                  <span className="heading-icon heading-icon-soft" aria-hidden="true">＋</span>
                </div>
                <form className="inline-form" onSubmit={(event) => void submitFollow(event)}>
                  <label className="visually-hidden" htmlFor="follow-user-id">创作者账号 ID</label>
                  <input
                    id="follow-user-id"
                    name="followee_id"
                    maxLength={100}
                    required
                    placeholder="输入账号 ID"
                    value={followeeId}
                    onChange={(event) => setFolloweeId(event.currentTarget.value)}
                  />
                  <button className="button button-secondary" type="submit" disabled={followBusy}>
                    {followBusy ? "处理中…" : "关注"}
                  </button>
                </form>
                <FeedbackText value={followFeedback} className="form-feedback" />
              </section>

              <section className="panel search-panel" aria-labelledby="search-title">
                <div className="section-heading">
                  <div>
                    <span className="eyebrow">全文搜索</span>
                    <h2 id="search-title">发现内容</h2>
                  </div>
                  <span className="heading-icon heading-icon-soft" aria-hidden="true">⌕</span>
                </div>
                <form className="search-form" onSubmit={(event) => void submitSearch(event)}>
                  <label className="visually-hidden" htmlFor="search-query">搜索正文</label>
                  <input
                    id="search-query"
                    name="q"
                    type="search"
                    maxLength={200}
                    placeholder="输入关键词"
                    required
                    value={searchInput}
                    onChange={(event) => setSearchInput(event.currentTarget.value)}
                  />
                  <button
                    className="icon-button"
                    type="submit"
                    aria-label="搜索"
                    disabled={searchBusy}
                  >
                    {searchBusy ? "…" : "⌕"}
                  </button>
                </form>
                <FeedbackText value={searchFeedback} className="form-feedback" />
              </section>
            </aside>

            <div className="workspace-main">
              <div className="welcome-strip">
                <div>
                  <span className="eyebrow">你的社区动态</span>
                  <h1>今天，读点新鲜的。</h1>
                </div>
                <button
                  className="button button-quiet button-small"
                  type="button"
                  onClick={() => void refreshWorkspace()}
                  disabled={refreshBusy}
                >
                  {refreshBusy ? "正在刷新…" : "刷新内容"}
                </button>
              </div>
              <FeedbackText value={globalFeedback} className="global-feedback" />

              {searchVisible && (
                <section className="panel results-panel" aria-labelledby="search-results-title">
                  <div className="section-heading">
                    <div>
                      <span className="eyebrow">搜索结果</span>
                      <h2 id="search-results-title">“{searchQuery}”的搜索结果</h2>
                    </div>
                    <button className="text-button" type="button" onClick={clearSearch}>
                      清除搜索
                    </button>
                  </div>
                  <div className="post-list">
                    {searchItems.map((item) => (
                      <PostCard
                        key={item.content_id}
                        item={item}
                        liked={likedIds.has(String(item.content_id))}
                        busy={pendingLikeIds.has(String(item.content_id))}
                        onLike={(contentId) => void likeContent(contentId)}
                      />
                    ))}
                  </div>
                  {searchItems.length === 0 && (
                    <p className="empty-state">没有匹配的内容。</p>
                  )}
                </section>
              )}

              <div className="feed-layout">
                <section className="panel feed-panel" aria-labelledby="feed-title">
                  <div className="section-heading">
                    <div>
                      <span className="eyebrow">只看你关注的人</span>
                      <h2 id="feed-title">为你推荐</h2>
                    </div>
                    <span className="count-pill">{feedItems.length}</span>
                  </div>
                  <div className="post-list" aria-live="polite">
                    {feedItems.map((item) => (
                      <PostCard
                        key={item.content_id}
                        item={item}
                        liked={likedIds.has(String(item.content_id))}
                        busy={pendingLikeIds.has(String(item.content_id))}
                        onLike={(contentId) => void likeContent(contentId)}
                      />
                    ))}
                  </div>
                  {feedItems.length === 0 && (
                    <p className="empty-state">这里还没有内容。关注一位创作者，或先发布一条内容。</p>
                  )}
                  {feedCursor && (
                    <button
                      className="button button-quiet load-more-button"
                      type="button"
                      onClick={() =>
                        void loadFeed({ append: true }).catch((error: unknown) =>
                          setGlobalFeedback({
                            message:
                              "无法加载下一页：" +
                              errorMessage(error, "请求失败。"),
                          }),
                        )
                      }
                      disabled={feedBusy}
                    >
                      {feedBusy ? "正在加载…" : "加载更多"}
                    </button>
                  )}
                </section>

                <aside className="panel notifications-panel" aria-labelledby="notifications-title">
                  <div className="section-heading">
                    <div>
                      <span className="eyebrow">来自你的关注者</span>
                      <h2 id="notifications-title">通知</h2>
                    </div>
                    <span className="heading-icon heading-icon-soft" aria-hidden="true">♡</span>
                  </div>
                  <div className="notification-list" aria-live="polite">
                    {notifications.map((item, index) => (
                      <NotificationCard
                        key={item.created_at + ":" + item.author_id + ":" + index}
                        item={item}
                      />
                    ))}
                  </div>
                  {notifications.length === 0 && (
                    <p className="empty-state empty-state-compact">
                      有人关注并阅读你的新内容后，通知会显示在这里。
                    </p>
                  )}
                  <FeedbackText
                    value={notificationsFeedback}
                    className="form-feedback"
                  />
                </aside>
              </div>
            </div>
          </section>
        )}
      </main>

      <footer className="site-footer">
        <span>ContentPlatform · 本地内容社区</span>
        <span>真实服务数据 · 当前标签页会话</span>
      </footer>
    </div>
  );
}
