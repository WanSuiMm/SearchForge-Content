"""Versioned SQLite schema migrations.

The migration runner applies each migration and its version record in one
SQLite transaction. Imports only define SQL; they never open a database.
"""

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE users (
            user_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE follows (
            follower_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            followee_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (follower_id, followee_id),
            CHECK (follower_id <> followee_id)
        )
        """,
        """
        CREATE TABLE contents (
            content_id TEXT PRIMARY KEY,
            author_id TEXT NOT NULL REFERENCES users(user_id),
            body TEXT NOT NULL,
            published_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            like_count INTEGER NOT NULL DEFAULT 0 CHECK (like_count >= 0),
            UNIQUE (author_id, idempotency_key)
        )
        """,
        """
        CREATE TABLE feed_entries (
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            content_id TEXT NOT NULL REFERENCES contents(content_id) ON DELETE CASCADE,
            published_at TEXT NOT NULL,
            PRIMARY KEY (user_id, content_id)
        )
        """,
        """
        CREATE INDEX feed_entries_by_time
        ON feed_entries (user_id, published_at DESC, content_id DESC)
        """,
        """
        CREATE TABLE likes (
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            content_id TEXT NOT NULL REFERENCES contents(content_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, content_id)
        )
        """,
        """
        CREATE TRIGGER likes_count_insert
        AFTER INSERT ON likes
        BEGIN
            UPDATE contents SET like_count = like_count + 1
            WHERE content_id = NEW.content_id;
        END
        """,
        """
        CREATE TRIGGER likes_count_delete
        AFTER DELETE ON likes
        BEGIN
            UPDATE contents SET like_count = like_count - 1
            WHERE content_id = OLD.content_id;
        END
        """,
        """
        CREATE TABLE outbox (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            available_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'claimed', 'delivered', 'dead')),
            lease_owner TEXT,
            lease_until REAL,
            delivered_at TEXT,
            last_error TEXT,
            CHECK (
                (status = 'claimed' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
                OR
                (status <> 'claimed' AND lease_owner IS NULL AND lease_until IS NULL)
            )
        )
        """,
        """
        CREATE INDEX outbox_ready
        ON outbox (status, available_at, created_at, event_id)
        """,
        """
        CREATE TABLE consumer_receipts (
            consumer_name TEXT NOT NULL,
            event_id TEXT NOT NULL REFERENCES outbox(event_id) ON DELETE CASCADE,
            processed_at TEXT NOT NULL,
            PRIMARY KEY (consumer_name, event_id)
        )
        """,
        """
        CREATE TABLE notifications (
            event_id TEXT NOT NULL REFERENCES outbox(event_id) ON DELETE CASCADE,
            recipient_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            content_id TEXT NOT NULL REFERENCES contents(content_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (event_id, recipient_id)
        )
        """,
    ),
    2: (
        """
        CREATE TABLE auth_credentials (
            user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE auth_sessions (
            token_hash TEXT PRIMARY KEY CHECK (length(token_hash) = 64),
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            expires_at REAL NOT NULL
        )
        """,
        """
        CREATE INDEX auth_sessions_by_user_expiry
        ON auth_sessions (user_id, expires_at)
        """,
        """
        CREATE VIRTUAL TABLE content_search USING fts5(body, content='contents', content_rowid='rowid')
        """,
        """
        CREATE TRIGGER content_search_insert AFTER INSERT ON contents BEGIN
            INSERT INTO content_search(rowid, body) VALUES (new.rowid, new.body);
        END
        """,
        """
        CREATE TRIGGER content_search_delete AFTER DELETE ON contents BEGIN
            INSERT INTO content_search(content_search, rowid, body)
            VALUES ('delete', old.rowid, old.body);
        END
        """,
        """
        CREATE TRIGGER content_search_update AFTER UPDATE OF body ON contents BEGIN
            INSERT INTO content_search(content_search, rowid, body)
            VALUES ('delete', old.rowid, old.body);
            INSERT INTO content_search(rowid, body) VALUES (new.rowid, new.body);
        END
        """,
        "INSERT INTO content_search(content_search) VALUES ('rebuild')",
    ),
    3: (
        """
        CREATE TABLE cache_epochs (
            namespace TEXT PRIMARY KEY CHECK (namespace IN ('feed', 'search')),
            version TEXT NOT NULL CHECK (length(version) = 32)
        )
        """,
        """
        INSERT INTO cache_epochs(namespace, version)
        VALUES ('feed', lower(hex(randomblob(16))))
        """,
        """
        INSERT INTO cache_epochs(namespace, version)
        VALUES ('search', lower(hex(randomblob(16))))
        """,
    ),
    4: (
        """
        CREATE TABLE projection_events (
            event_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL CHECK (schema_version = 1),
            event_type TEXT NOT NULL CHECK (
                event_type IN ('content.published', 'content.withdrawn')
            ),
            content_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version >= 1),
            visibility TEXT NOT NULL CHECK (visibility IN ('public', 'withdrawn')),
            author_id TEXT NOT NULL,
            body TEXT NOT NULL,
            published_at TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            UNIQUE (content_id, version),
            CHECK (
                (event_type = 'content.published' AND visibility = 'public' AND body <> '')
                OR
                (event_type = 'content.withdrawn' AND visibility = 'withdrawn' AND body = '')
            )
        )
        """,
        """
        CREATE INDEX projection_events_by_occurred_at
        ON projection_events (occurred_at, event_id)
        """,
        """
        CREATE TABLE projection_event_receipts (
            consumer_id TEXT NOT NULL CHECK (length(consumer_id) BETWEEN 1 AND 200),
            event_id TEXT NOT NULL REFERENCES projection_events(event_id),
            processed_at TEXT NOT NULL,
            PRIMARY KEY (consumer_id, event_id)
        )
        """,
        """
        CREATE TRIGGER projection_events_immutable_update
        BEFORE UPDATE ON projection_events
        BEGIN
            SELECT RAISE(ABORT, 'projection events are immutable');
        END
        """,
        """
        CREATE TRIGGER projection_events_immutable_delete
        BEFORE DELETE ON projection_events
        BEGIN
            SELECT RAISE(ABORT, 'projection events are immutable');
        END
        """,
    ),
}


POSTGRES_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE users (
            user_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE follows (
            follower_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            followee_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (follower_id, followee_id),
            CHECK (follower_id <> followee_id)
        )
        """,
        """
        CREATE TABLE contents (
            content_id TEXT PRIMARY KEY,
            author_id TEXT NOT NULL REFERENCES users(user_id),
            body TEXT NOT NULL,
            published_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            like_count INTEGER NOT NULL DEFAULT 0 CHECK (like_count >= 0),
            UNIQUE (author_id, idempotency_key)
        )
        """,
        """
        CREATE TABLE feed_entries (
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            content_id TEXT NOT NULL REFERENCES contents(content_id) ON DELETE CASCADE,
            published_at TEXT NOT NULL,
            PRIMARY KEY (user_id, content_id)
        )
        """,
        """
        CREATE INDEX feed_entries_by_time
        ON feed_entries (user_id, published_at DESC, content_id DESC)
        """,
        """
        CREATE TABLE likes (
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            content_id TEXT NOT NULL REFERENCES contents(content_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, content_id)
        )
        """,
        """
        CREATE FUNCTION content_platform_like_count_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                UPDATE contents SET like_count = like_count + 1
                WHERE content_id = NEW.content_id;
                RETURN NEW;
            END IF;
            UPDATE contents SET like_count = like_count - 1
            WHERE content_id = OLD.content_id;
            RETURN OLD;
        END;
        $$
        """,
        """
        CREATE TRIGGER likes_count_insert AFTER INSERT ON likes
        FOR EACH ROW EXECUTE FUNCTION content_platform_like_count_change()
        """,
        """
        CREATE TRIGGER likes_count_delete AFTER DELETE ON likes
        FOR EACH ROW EXECUTE FUNCTION content_platform_like_count_change()
        """,
        """
        CREATE TABLE outbox (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            available_at DOUBLE PRECISION NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'claimed', 'delivered', 'dead')),
            lease_owner TEXT,
            lease_until DOUBLE PRECISION,
            delivered_at TEXT,
            last_error TEXT,
            CHECK (
                (status = 'claimed' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
                OR
                (status <> 'claimed' AND lease_owner IS NULL AND lease_until IS NULL)
            )
        )
        """,
        """
        CREATE INDEX outbox_ready
        ON outbox (status, available_at, created_at, event_id)
        """,
        """
        CREATE TABLE consumer_receipts (
            consumer_name TEXT NOT NULL,
            event_id TEXT NOT NULL REFERENCES outbox(event_id) ON DELETE CASCADE,
            processed_at TEXT NOT NULL,
            PRIMARY KEY (consumer_name, event_id)
        )
        """,
        """
        CREATE TABLE notifications (
            event_id TEXT NOT NULL REFERENCES outbox(event_id) ON DELETE CASCADE,
            recipient_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            content_id TEXT NOT NULL REFERENCES contents(content_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (event_id, recipient_id)
        )
        """,
    ),
    2: (
        """
        CREATE TABLE auth_credentials (
            user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE auth_sessions (
            token_hash TEXT PRIMARY KEY CHECK (char_length(token_hash) = 64),
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL
        )
        """,
        """
        CREATE INDEX auth_sessions_by_user_expiry
        ON auth_sessions (user_id, expires_at)
        """,
        "ALTER TABLE contents ADD COLUMN search_vector tsvector NOT NULL DEFAULT ''::tsvector",
        "UPDATE contents SET search_vector = to_tsvector('simple', body)",
        "CREATE INDEX contents_search_vector_gin ON contents USING GIN (search_vector)",
        """
        CREATE FUNCTION content_platform_search_vector_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.search_vector := to_tsvector('simple', NEW.body);
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE TRIGGER contents_search_vector_change
        BEFORE INSERT OR UPDATE OF body ON contents
        FOR EACH ROW EXECUTE FUNCTION content_platform_search_vector_change()
        """,
    ),
    3: (
        """
        CREATE TABLE cache_epochs (
            namespace TEXT PRIMARY KEY CHECK (namespace IN ('feed', 'search')),
            version TEXT NOT NULL CHECK (char_length(version) = 32)
        )
        """,
        """
        INSERT INTO cache_epochs(namespace, version)
        VALUES ('feed', md5(random()::text || clock_timestamp()::text))
        """,
        """
        INSERT INTO cache_epochs(namespace, version)
        VALUES ('search', md5(random()::text || clock_timestamp()::text))
        """,
    ),
    4: (
        """
        CREATE TABLE projection_events (
            event_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL CHECK (schema_version = 1),
            event_type TEXT NOT NULL CHECK (
                event_type IN ('content.published', 'content.withdrawn')
            ),
            content_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version >= 1),
            visibility TEXT NOT NULL CHECK (visibility IN ('public', 'withdrawn')),
            author_id TEXT NOT NULL,
            body TEXT NOT NULL,
            published_at TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            UNIQUE (content_id, version),
            CHECK (
                (event_type = 'content.published' AND visibility = 'public' AND body <> '')
                OR
                (event_type = 'content.withdrawn' AND visibility = 'withdrawn' AND body = '')
            )
        )
        """,
        "CREATE INDEX projection_events_by_occurred_at ON projection_events (occurred_at, event_id)",
        """
        CREATE TABLE projection_event_receipts (
            consumer_id TEXT NOT NULL CHECK (char_length(consumer_id) BETWEEN 1 AND 200),
            event_id TEXT NOT NULL REFERENCES projection_events(event_id),
            processed_at TEXT NOT NULL,
            PRIMARY KEY (consumer_id, event_id)
        )
        """,
        """
        CREATE FUNCTION content_platform_projection_event_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'projection events are immutable';
        END;
        $$
        """,
        """
        CREATE TRIGGER projection_events_immutable
        BEFORE UPDATE OR DELETE ON projection_events
        FOR EACH ROW EXECUTE FUNCTION content_platform_projection_event_immutable()
        """,
    ),
}
