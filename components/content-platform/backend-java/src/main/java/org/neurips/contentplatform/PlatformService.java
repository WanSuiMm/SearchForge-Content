package org.neurips.contentplatform;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.neurips.contentplatform.ApiTypes.ContentDto;
import org.neurips.contentplatform.ApiTypes.FeedResponse;
import org.neurips.contentplatform.ApiTypes.FollowResponse;
import org.neurips.contentplatform.ApiTypes.LikeResponse;
import org.neurips.contentplatform.ApiTypes.PublishResponse;
import org.neurips.contentplatform.ApiTypes.SessionResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.TreeMap;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

@Service
public class PlatformService {
    private static final SecureRandom RANDOM = new SecureRandom();
    private static final Pattern SEARCH_TOKEN = Pattern.compile("[\\p{L}\\p{N}]+");
    private static final RowMapper<ContentDto> CONTENT_ROW = (rs, rowNum) -> new ContentDto(
            rs.getString("content_id"), rs.getString("author_id"), rs.getString("author_name"),
            rs.getString("body"), rs.getString("published_at"), rs.getLong("like_count"));

    private final JdbcTemplate jdbc;
    private final PasswordEncoder passwordEncoder;
    private final ObjectMapper mapper;
    private final long sessionTtlSeconds;
    private final boolean postgresSearch;
    private final String dummyPasswordHash;

    public PlatformService(
            JdbcTemplate jdbc,
            PasswordEncoder passwordEncoder,
            ObjectMapper mapper,
            @Value("${contentplatform.session-ttl-seconds:86400}") long sessionTtlSeconds,
            @Value("${spring.datasource.url:}") String jdbcUrl) {
        if (sessionTtlSeconds < 60) {
            throw new IllegalArgumentException("session TTL must be at least 60 seconds");
        }
        this.jdbc = jdbc;
        this.passwordEncoder = passwordEncoder;
        this.mapper = mapper;
        this.sessionTtlSeconds = sessionTtlSeconds;
        this.postgresSearch = jdbcUrl.startsWith("jdbc:postgresql:");
        this.dummyPasswordHash = passwordEncoder.encode("invalid-account-password");
    }

    @Transactional
    public SessionResponse register(String userId, String displayName, String password) {
        String normalizedId = requiredText(userId, "user_id", 100);
        String normalizedName = requiredText(displayName, "display_name", 200);
        validatePassword(password);
        String now = now();
        String token = newToken();
        double expiresAt = System.currentTimeMillis() / 1000.0 + sessionTtlSeconds;
        try {
            jdbc.update("INSERT INTO users(user_id, display_name, created_at) VALUES (?, ?, ?)",
                    normalizedId, normalizedName, now);
            jdbc.update("INSERT INTO auth_credentials(user_id, password_hash, created_at) VALUES (?, ?, ?)",
                    normalizedId, passwordEncoder.encode(password), now);
            jdbc.update("INSERT INTO auth_sessions(token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                    sha256(token), normalizedId, now, timestampFromEpoch(expiresAt));
            jdbc.update("DELETE FROM auth_sessions WHERE expires_at <= CURRENT_TIMESTAMP");
        } catch (DuplicateKeyException exception) {
            throw PlatformException.conflict("conflict", "user already exists");
        }
        return new SessionResponse(normalizedId, token, "Bearer", expiresAt);
    }

    @Transactional
    public SessionResponse login(String userId, String password) {
        String normalizedId = requiredText(userId, "user_id", 100);
        validatePassword(password);
        List<String> hashes = jdbc.query("SELECT password_hash FROM auth_credentials WHERE user_id = ?",
                (rs, rowNum) -> rs.getString(1), normalizedId);
        String stored = hashes.isEmpty() ? dummyPasswordHash : hashes.getFirst();
        boolean valid = passwordEncoder.matches(password, stored);
        if (hashes.isEmpty() || !valid) {
            throw PlatformException.unauthorized();
        }
        String token = newToken();
        String now = now();
        double expiresAt = System.currentTimeMillis() / 1000.0 + sessionTtlSeconds;
        jdbc.update("INSERT INTO auth_sessions(token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                sha256(token), normalizedId, now, timestampFromEpoch(expiresAt));
        jdbc.update("DELETE FROM auth_sessions WHERE expires_at <= CURRENT_TIMESTAMP");
        return new SessionResponse(normalizedId, token, "Bearer", expiresAt);
    }

    public String authenticate(String token) {
        if (token == null || token.length() < 32 || token.length() > 256 || token.chars().anyMatch(Character::isWhitespace)) {
            throw PlatformException.unauthorized();
        }
        String candidate = sha256(token);
        List<Map<String, Object>> sessions = jdbc.queryForList(
                "SELECT token_hash, user_id FROM auth_sessions WHERE token_hash = ? AND expires_at > CURRENT_TIMESTAMP",
                candidate);
        if (sessions.isEmpty()) {
            constantTimeEquals(candidate, "0".repeat(64));
            throw PlatformException.unauthorized();
        }
        Map<String, Object> session = sessions.getFirst();
        String stored = String.valueOf(session.get("token_hash")).trim();
        if (!constantTimeEquals(candidate, stored)) {
            throw PlatformException.unauthorized();
        }
        return String.valueOf(session.get("user_id"));
    }

    @Transactional
    public void revoke(String token) {
        String candidate = sha256(token);
        List<String> known = jdbc.query("SELECT token_hash FROM auth_sessions WHERE token_hash = ?",
                (rs, rowNum) -> rs.getString(1), candidate);
        if (known.isEmpty() || !constantTimeEquals(candidate, known.getFirst().trim())) {
            throw PlatformException.unauthorized();
        }
        jdbc.update("DELETE FROM auth_sessions WHERE token_hash = ?", candidate);
    }

    @Transactional
    public FollowResponse follow(String followerId, String followeeId) {
        String follower = requiredText(followerId, "follower_id", 100);
        String followee = requiredText(followeeId, "followee_id", 100);
        if (follower.equals(followee)) {
            throw PlatformException.invalid("a user cannot follow themselves");
        }
        requireUser(follower);
        lockUser(followee);
        int created;
        if (postgresSearch) {
            created = jdbc.update("""
                    INSERT INTO follows(follower_id, followee_id, created_at) VALUES (?, ?, ?)
                    ON CONFLICT(follower_id, followee_id) DO NOTHING
                    """, follower, followee, now());
        } else {
            Integer existing = jdbc.queryForObject("SELECT COUNT(*) FROM follows WHERE follower_id = ? AND followee_id = ?",
                    Integer.class, follower, followee);
            created = existing != null && existing > 0 ? 0 : jdbc.update(
                    "INSERT INTO follows(follower_id, followee_id, created_at) VALUES (?, ?, ?)", follower, followee, now());
        }
        if (created == 1) {
            jdbc.update("""
                    INSERT INTO feed_entries(user_id, content_id, published_at)
                    SELECT ?, content_id, published_at FROM contents WHERE author_id = ?
                    """, follower, followee);
            bumpCacheEpoch("feed");
        }
        return new FollowResponse(follower, followee, created == 1);
    }

    @Transactional
    public PublishResponse publish(String authorId, String rawBody, String rawIdempotencyKey) {
        String author = requiredText(authorId, "author_id", 100);
        if (rawBody == null || rawBody.length() > 20_000) {
            throw PlatformException.invalid("body must contain 1 to 20000 characters");
        }
        String body = rawBody.strip();
        if (body.isEmpty() || body.length() > 20_000) {
            throw PlatformException.invalid("body must contain 1 to 20000 characters");
        }
        String idempotencyKey = requiredText(rawIdempotencyKey, "idempotency_key", 200);
        lockUser(author);
        String requestHash = sha256("{\"body\":" + jsonString(body) + "}");
        List<Map<String, Object>> prior = jdbc.queryForList("""
                SELECT c.content_id, c.author_id, u.display_name AS author_name,
                       c.body, c.published_at, c.like_count, c.request_hash
                FROM contents c JOIN users u ON u.user_id = c.author_id
                WHERE c.author_id = ? AND c.idempotency_key = ?
                """, author, idempotencyKey);
        if (!prior.isEmpty()) {
            Map<String, Object> row = prior.getFirst();
            if (!requestHash.equals(String.valueOf(row.get("request_hash")).trim())) {
                throw PlatformException.conflict("idempotency_conflict", "idempotency key conflicts with an earlier request");
            }
            return new PublishResponse(toContent(row), true);
        }

        String contentId = UUID.randomUUID().toString();
        String publishedAt = now();
        jdbc.update("""
                INSERT INTO contents(content_id, author_id, body, published_at, idempotency_key, request_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                """, contentId, author, body, publishedAt, idempotencyKey, requestHash);

        List<String> followers = jdbc.query("SELECT follower_id FROM follows WHERE followee_id = ? ORDER BY follower_id",
                (rs, rowNum) -> rs.getString(1), author);
        List<String> feedUsers = new ArrayList<>(followers.size() + 1);
        feedUsers.add(author);
        feedUsers.addAll(followers);
        for (String userId : feedUsers) {
            jdbc.update("INSERT INTO feed_entries(user_id, content_id, published_at) VALUES (?, ?, ?)",
                    userId, contentId, publishedAt);
        }

        String eventId = UUID.randomUUID().toString();
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("content_id", contentId);
        payload.put("author_id", author);
        payload.put("recipient_ids", followers);
        try {
            jdbc.update("""
                    INSERT INTO outbox(event_id, event_type, payload_json, created_at, available_at)
                    VALUES (?, 'content.published', ?, ?, ?)
                    """, eventId, mapper.writeValueAsString(payload), publishedAt, System.currentTimeMillis() / 1000.0);
        } catch (JsonProcessingException exception) {
            throw new IllegalStateException("content event could not be serialized", exception);
        }
        jdbc.update("""
                INSERT INTO projection_events(event_id, schema_version, event_type, content_id, version,
                    visibility, author_id, body, published_at, occurred_at)
                VALUES (?, 1, 'content.published', ?, 1, 'public', ?, ?, ?, ?)
                """, UUID.randomUUID().toString(), contentId, author, body, publishedAt, publishedAt);
        bumpCacheEpoch("feed");
        bumpCacheEpoch("search");
        return new PublishResponse(getContentOrThrow(contentId), false);
    }

    @Transactional(readOnly = true)
    public FeedResponse feed(String userId, int limit, String cursor) {
        requireLimit(limit, 100);
        String owner = requiredText(userId, "user_id", 100);
        Cursor after = decodeCursor(cursor);
        List<ContentDto> rows;
        if (after == null) {
            rows = jdbc.query("""
                    SELECT c.content_id, c.author_id, u.display_name AS author_name,
                           c.body, c.published_at, c.like_count
                    FROM feed_entries f
                    JOIN contents c ON c.content_id = f.content_id
                    JOIN users u ON u.user_id = c.author_id
                    WHERE f.user_id = ?
                    ORDER BY f.published_at DESC, f.content_id DESC LIMIT ?
                    """, CONTENT_ROW, owner, limit + 1);
        } else {
            rows = jdbc.query("""
                    SELECT c.content_id, c.author_id, u.display_name AS author_name,
                           c.body, c.published_at, c.like_count
                    FROM feed_entries f
                    JOIN contents c ON c.content_id = f.content_id
                    JOIN users u ON u.user_id = c.author_id
                    WHERE f.user_id = ? AND
                        (f.published_at < ? OR (f.published_at = ? AND f.content_id < ?))
                    ORDER BY f.published_at DESC, f.content_id DESC LIMIT ?
                    """, CONTENT_ROW, owner, after.publishedAt(), after.publishedAt(), after.contentId(), limit + 1);
        }
        boolean more = rows.size() > limit;
        List<ContentDto> items = more ? List.copyOf(rows.subList(0, limit)) : List.copyOf(rows);
        String next = more && !items.isEmpty() ? encodeCursor(items.getLast().publishedAt(), items.getLast().contentId()) : null;
        return new FeedResponse(items, next);
    }

    @Transactional(readOnly = true)
    public List<ContentDto> search(String query, int limit) {
        requireLimit(limit, 100);
        if (query == null || query.strip().isEmpty() || query.strip().length() > 200) {
            throw PlatformException.invalid("query must contain 1 to 200 characters");
        }
        String normalized = query.strip().toLowerCase(Locale.ROOT);
        Matcher matcher = SEARCH_TOKEN.matcher(normalized);
        List<String> terms = new ArrayList<>();
        while (matcher.find()) terms.add(matcher.group());
        if (terms.isEmpty() || terms.size() > 20) {
            throw PlatformException.invalid("query must contain 1 to 20 searchable terms");
        }
        if (postgresSearch) {
            String tsQuery = String.join(" & ", terms);
            return jdbc.query("""
                    SELECT c.content_id, c.author_id, u.display_name AS author_name,
                           c.body, c.published_at, c.like_count
                    FROM contents c JOIN users u ON u.user_id = c.author_id
                    WHERE c.search_vector @@ to_tsquery('simple', ?)
                    ORDER BY ts_rank(c.search_vector, to_tsquery('simple', ?)) DESC,
                             c.published_at DESC, c.content_id DESC LIMIT ?
                    """, CONTENT_ROW, tsQuery, tsQuery, limit);
        }
        String predicates = String.join(" AND ", terms.stream().map(ignored -> "lower(CAST(c.body AS VARCHAR(20000))) LIKE ?").toList());
        String sql = """
                SELECT c.content_id, c.author_id, u.display_name AS author_name,
                       c.body, c.published_at, c.like_count
                FROM contents c JOIN users u ON u.user_id = c.author_id
                WHERE
                """ + predicates + " ORDER BY c.published_at DESC, c.content_id DESC LIMIT ?";
        List<Object> args = new ArrayList<>();
        for (String term : terms) args.add("%" + term + "%");
        args.add(limit);
        return jdbc.query(sql, CONTENT_ROW, args.toArray());
    }

    @Transactional
    public LikeResponse like(String userId, String contentId) {
        String owner = requiredText(userId, "user_id", 100);
        String content = requiredText(contentId, "content_id", 200);
        Long count = jdbc.query("SELECT like_count FROM contents WHERE content_id = ?",
                rs -> rs.next() ? rs.getLong(1) : null, content);
        if (count == null) throw PlatformException.notFound();
        int added;
        if (postgresSearch) {
            added = jdbc.update("""
                    INSERT INTO likes(user_id, content_id, created_at) VALUES (?, ?, ?)
                    ON CONFLICT(user_id, content_id) DO NOTHING
                    """, owner, content, now());
        } else {
            Integer existing = jdbc.queryForObject("SELECT COUNT(*) FROM likes WHERE user_id = ? AND content_id = ?",
                    Integer.class, owner, content);
            added = existing != null && existing > 0 ? 0 : jdbc.update(
                    "INSERT INTO likes(user_id, content_id, created_at) VALUES (?, ?, ?)", owner, content, now());
        }
        if (added == 1) {
            jdbc.update("UPDATE contents SET like_count = like_count + 1 WHERE content_id = ?", content);
            bumpCacheEpoch("feed");
            bumpCacheEpoch("search");
            count += 1;
        }
        return new LikeResponse(added == 1, count);
    }

    @Transactional(readOnly = true)
    public List<Map<String, Object>> notifications(String userId, int limit) {
        requireLimit(limit, 100);
        String owner = requiredText(userId, "user_id", 100);
        return jdbc.query("""
                SELECT n.event_id, n.content_id, n.created_at,
                       c.author_id, u.display_name AS author_name, c.body
                FROM notifications n
                JOIN contents c ON c.content_id = n.content_id
                JOIN users u ON u.user_id = c.author_id
                WHERE n.recipient_id = ?
                ORDER BY n.created_at DESC, n.event_id DESC LIMIT ?
                """, (rs, rowNum) -> {
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("event_id", rs.getString("event_id"));
            item.put("content_id", rs.getString("content_id"));
            item.put("created_at", rs.getString("created_at"));
            item.put("author_id", rs.getString("author_id"));
            item.put("author_name", rs.getString("author_name"));
            item.put("body", rs.getString("body"));
            return item;
        }, owner, limit);
    }

    @Transactional(readOnly = true)
    public ContentDto content(String contentId) {
        return getContentOrThrow(requiredText(contentId, "content_id", 200));
    }

    private ContentDto getContentOrThrow(String contentId) {
        List<ContentDto> rows = jdbc.query("""
                SELECT c.content_id, c.author_id, u.display_name AS author_name,
                       c.body, c.published_at, c.like_count
                FROM contents c JOIN users u ON u.user_id = c.author_id
                WHERE c.content_id = ?
                """, CONTENT_ROW, contentId);
        if (rows.isEmpty()) throw PlatformException.notFound();
        return rows.getFirst();
    }

    private void lockUser(String userId) {
        List<String> users = jdbc.query("SELECT user_id FROM users WHERE user_id = ? FOR UPDATE",
                (rs, rowNum) -> rs.getString(1), userId);
        if (users.isEmpty()) throw PlatformException.notFound();
    }

    private void requireUser(String userId) {
        Integer count = jdbc.queryForObject("SELECT COUNT(*) FROM users WHERE user_id = ?", Integer.class, userId);
        if (count == null || count == 0) {
            throw PlatformException.notFound();
        }
    }

    private void bumpCacheEpoch(String namespace) {
        int changed = jdbc.update("UPDATE cache_epochs SET version = ? WHERE namespace = ?",
                UUID.randomUUID().toString().replace("-", ""), namespace);
        if (changed != 1) throw new IllegalStateException("cache epoch row is missing");
    }

    private static ContentDto toContent(Map<String, Object> row) {
        return new ContentDto(String.valueOf(row.get("content_id")), String.valueOf(row.get("author_id")),
                String.valueOf(row.get("author_name")), String.valueOf(row.get("body")),
                String.valueOf(row.get("published_at")), ((Number) row.get("like_count")).longValue());
    }

    private static void requireLimit(int limit, int maximum) {
        if (limit < 1 || limit > maximum) throw PlatformException.invalid("limit must be from 1 to " + maximum);
    }

    private static String requiredText(String value, String field, int maximum) {
        if (value == null) throw PlatformException.invalid(field + " must contain 1 to " + maximum + " characters");
        String normalized = value.strip();
        if (normalized.isEmpty() || normalized.length() > maximum) {
            throw PlatformException.invalid(field + " must contain 1 to " + maximum + " characters");
        }
        return normalized;
    }

    private static void validatePassword(String password) {
        if (password == null || password.length() < 12 || password.length() > 256) {
            throw PlatformException.invalid("password must contain 12 to 256 characters");
        }
    }

    private static String now() { return UtcTimestamps.now(); }

    private static String newToken() {
        byte[] bytes = new byte[32];
        RANDOM.nextBytes(bytes);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
    }

    private static java.sql.Timestamp timestampFromEpoch(double epoch) {
        long seconds = (long) epoch;
        long nanos = (long) ((epoch - seconds) * 1_000_000_000L);
        return java.sql.Timestamp.from(java.time.Instant.ofEpochSecond(seconds, nanos));
    }

    private static String sha256(String value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 is unavailable", exception);
        }
    }

    private static boolean constantTimeEquals(String left, String right) {
        return MessageDigest.isEqual(left.getBytes(StandardCharsets.US_ASCII), right.getBytes(StandardCharsets.US_ASCII));
    }

    private String jsonString(String value) {
        try {
            return mapper.writeValueAsString(value);
        } catch (JsonProcessingException exception) {
            throw new IllegalArgumentException("content body could not be encoded", exception);
        }
    }

    private String encodeCursor(String publishedAt, String contentId) {
        Map<String, String> cursor = new TreeMap<>();
        cursor.put("published_at", publishedAt);
        cursor.put("content_id", contentId);
        try {
            return Base64.getUrlEncoder().withoutPadding().encodeToString(mapper.writeValueAsBytes(cursor));
        } catch (JsonProcessingException exception) {
            throw new IllegalStateException("feed cursor could not be encoded", exception);
        }
    }

    private Cursor decodeCursor(String encoded) {
        if (encoded == null) return null;
        if (encoded.isEmpty() || encoded.length() > 1024) throw PlatformException.invalid("cursor is empty or too long");
        try {
            byte[] decoded = Base64.getUrlDecoder().decode(encoded + "=".repeat((4 - encoded.length() % 4) % 4));
            Map<String, Object> value = mapper.readValue(decoded, new TypeReference<>() {});
            Object publishedAt = value.get("published_at");
            Object contentId = value.get("content_id");
            if (!(publishedAt instanceof String p) || p.isEmpty() || !(contentId instanceof String c) || c.isEmpty()) {
                throw new IllegalArgumentException("invalid cursor fields");
            }
            return new Cursor(p, c);
        } catch (Exception exception) {
            throw PlatformException.invalid("cursor is malformed");
        }
    }

    private record Cursor(String publishedAt, String contentId) {}
}
