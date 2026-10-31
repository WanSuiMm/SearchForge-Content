CREATE TABLE users (
    user_id VARCHAR(100) PRIMARY KEY,
    display_name VARCHAR(200) NOT NULL,
    created_at VARCHAR(128) NOT NULL
);

CREATE TABLE auth_credentials (
    user_id VARCHAR(100) PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    password_hash VARCHAR(512) NOT NULL,
    created_at VARCHAR(128) NOT NULL
);

CREATE TABLE auth_sessions (
    token_hash CHAR(64) PRIMARY KEY,
    user_id VARCHAR(100) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at VARCHAR(128) NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX auth_sessions_by_user_expiry ON auth_sessions(user_id, expires_at);

CREATE TABLE follows (
    follower_id VARCHAR(100) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    followee_id VARCHAR(100) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at VARCHAR(128) NOT NULL,
    PRIMARY KEY (follower_id, followee_id),
    CHECK (follower_id <> followee_id)
);

CREATE TABLE contents (
    content_id VARCHAR(200) PRIMARY KEY,
    author_id VARCHAR(100) NOT NULL REFERENCES users(user_id),
    body TEXT NOT NULL CHECK (char_length(body) BETWEEN 1 AND 20000),
    published_at VARCHAR(128) NOT NULL,
    idempotency_key VARCHAR(200) NOT NULL,
    request_hash CHAR(64) NOT NULL,
    like_count BIGINT NOT NULL DEFAULT 0 CHECK (like_count >= 0),
    UNIQUE (author_id, idempotency_key)
);

CREATE TABLE feed_entries (
    user_id VARCHAR(100) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    content_id VARCHAR(200) NOT NULL REFERENCES contents(content_id) ON DELETE CASCADE,
    published_at VARCHAR(128) NOT NULL,
    PRIMARY KEY (user_id, content_id)
);
CREATE INDEX feed_entries_by_time ON feed_entries(user_id, published_at DESC, content_id DESC);

CREATE TABLE likes (
    user_id VARCHAR(100) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    content_id VARCHAR(200) NOT NULL REFERENCES contents(content_id) ON DELETE CASCADE,
    created_at VARCHAR(128) NOT NULL,
    PRIMARY KEY (user_id, content_id)
);

CREATE TABLE outbox (
    event_id VARCHAR(200) PRIMARY KEY,
    event_type VARCHAR(100) NOT NULL,
    payload_json TEXT NOT NULL,
    created_at VARCHAR(128) NOT NULL,
    available_at DOUBLE PRECISION NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'claimed', 'delivered', 'dead')),
    lease_owner VARCHAR(100),
    lease_until DOUBLE PRECISION,
    delivered_at VARCHAR(128),
    last_error VARCHAR(500),
    CHECK ((status = 'claimed' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
        OR (status <> 'claimed' AND lease_owner IS NULL AND lease_until IS NULL))
);
CREATE INDEX outbox_ready ON outbox(status, available_at, created_at, event_id);

CREATE TABLE consumer_receipts (
    consumer_name VARCHAR(100) NOT NULL,
    event_id VARCHAR(200) NOT NULL REFERENCES outbox(event_id) ON DELETE CASCADE,
    processed_at VARCHAR(128) NOT NULL,
    PRIMARY KEY (consumer_name, event_id)
);

CREATE TABLE notifications (
    event_id VARCHAR(200) NOT NULL REFERENCES outbox(event_id) ON DELETE CASCADE,
    recipient_id VARCHAR(100) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    content_id VARCHAR(200) NOT NULL REFERENCES contents(content_id) ON DELETE CASCADE,
    created_at VARCHAR(128) NOT NULL,
    PRIMARY KEY (event_id, recipient_id)
);
CREATE INDEX notifications_by_recipient_time ON notifications(recipient_id, created_at DESC, event_id DESC);

CREATE TABLE cache_epochs (
    namespace VARCHAR(16) PRIMARY KEY CHECK (namespace IN ('feed', 'search')),
    version CHAR(32) NOT NULL
);
INSERT INTO cache_epochs(namespace, version) VALUES
    ('feed', '00000000000000000000000000000001'),
    ('search', '00000000000000000000000000000001');

CREATE TABLE projection_events (
    event_id VARCHAR(200) PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    event_type VARCHAR(64) NOT NULL CHECK (event_type IN ('content.published', 'content.withdrawn')),
    content_id VARCHAR(200) NOT NULL,
    version BIGINT NOT NULL CHECK (version >= 1),
    visibility VARCHAR(16) NOT NULL CHECK (visibility IN ('public', 'withdrawn')),
    author_id VARCHAR(100) NOT NULL,
    body TEXT NOT NULL,
    published_at VARCHAR(128) NOT NULL,
    occurred_at VARCHAR(128) NOT NULL,
    UNIQUE (content_id, version),
    CHECK ((event_type = 'content.published' AND visibility = 'public' AND body <> '')
        OR (event_type = 'content.withdrawn' AND visibility = 'withdrawn' AND body = ''))
);
CREATE INDEX projection_events_by_occurred_at ON projection_events(occurred_at, event_id);

CREATE TABLE projection_event_receipts (
    consumer_id VARCHAR(200) NOT NULL,
    event_id VARCHAR(200) NOT NULL REFERENCES projection_events(event_id),
    processed_at VARCHAR(128) NOT NULL,
    PRIMARY KEY (consumer_id, event_id)
);
