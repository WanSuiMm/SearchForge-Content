package org.neurips.contentplatform;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

@Service
public class ProjectionService {
    private final JdbcTemplate jdbc;
    private final boolean postgres;

    public ProjectionService(JdbcTemplate jdbc,
                             @org.springframework.beans.factory.annotation.Value("${spring.datasource.url:}") String jdbcUrl) {
        this.jdbc = jdbc;
        this.postgres = jdbcUrl.startsWith("jdbc:postgresql:");
    }

    @Transactional(readOnly = true)
    public List<Map<String, Object>> pending(String consumerId, int limit) {
        requireConsumer(consumerId);
        if (limit < 1 || limit > 100) throw PlatformException.invalid("limit must be from 1 to 100");
        return jdbc.query("""
                SELECT e.schema_version, e.event_id, e.event_type, e.content_id,
                       e.version, e.visibility, e.author_id, e.body,
                       e.published_at, e.occurred_at
                FROM projection_events e
                WHERE NOT EXISTS (
                    SELECT 1 FROM projection_event_receipts r
                    WHERE r.consumer_id = ? AND r.event_id = e.event_id
                )
                ORDER BY e.occurred_at ASC, e.event_id ASC LIMIT ?
                """, (rs, rowNum) -> {
            String contentId = rs.getString("content_id");
            Map<String, Object> event = new LinkedHashMap<>();
            event.put("schema_version", rs.getInt("schema_version"));
            event.put("event_id", rs.getString("event_id"));
            event.put("event_type", rs.getString("event_type"));
            event.put("content_id", contentId);
            event.put("version", rs.getLong("version"));
            event.put("visibility", rs.getString("visibility"));
            event.put("author_id", rs.getString("author_id"));
            event.put("body", rs.getString("body"));
            event.put("published_at", rs.getString("published_at"));
            event.put("occurred_at", rs.getString("occurred_at"));
            event.put("source_uri", "/v1/content/" + contentId);
            return event;
        }, consumerId, limit);
    }

    @Transactional
    public boolean acknowledge(String consumerId, String eventId) {
        requireConsumer(consumerId);
        requireText(eventId, "event_id", 200);
        Integer known = jdbc.queryForObject("SELECT COUNT(*) FROM projection_events WHERE event_id = ?",
                Integer.class, eventId);
        if (known == null || known == 0) return false;
        if (postgres) {
            jdbc.update("""
                    INSERT INTO projection_event_receipts(consumer_id, event_id, processed_at)
                    VALUES (?, ?, ?) ON CONFLICT(consumer_id, event_id) DO NOTHING
                    """, consumerId, eventId, UtcTimestamps.now());
        } else {
            Integer receipt = jdbc.queryForObject("SELECT COUNT(*) FROM projection_event_receipts WHERE consumer_id = ? AND event_id = ?",
                    Integer.class, consumerId, eventId);
            if (receipt == null || receipt == 0) {
                jdbc.update("INSERT INTO projection_event_receipts(consumer_id, event_id, processed_at) VALUES (?, ?, ?)",
                        consumerId, eventId, UtcTimestamps.now());
            }
        }
        return true;
    }

    @Transactional(readOnly = true)
    public Map<String, Object> publicContent(String contentId) {
        requireText(contentId, "content_id", 200);
        List<Map<String, Object>> rows = jdbc.query("""
                SELECT content_id, author_id, body, published_at FROM contents WHERE content_id = ?
                """, (rs, rowNum) -> publicDocument(rs.getString("content_id"), rs.getString("author_id"),
                rs.getString("body"), rs.getString("published_at")), contentId);
        if (rows.isEmpty()) throw PlatformException.notFound();
        return rows.getFirst();
    }

    @Transactional(readOnly = true)
    public List<Map<String, Object>> publicContentMany(List<?> contentIds) {
        if (contentIds == null || contentIds.size() > 20) {
            throw PlatformException.invalid("content resolution requires at most 20 distinct identifiers");
        }
        if (contentIds.isEmpty()) return List.of();
        List<String> validatedIds = new ArrayList<>(contentIds.size());
        for (Object value : contentIds) {
            if (!(value instanceof String id)) throw PlatformException.invalid("content_ids must contain strings");
            requireText(id, "content_id", 200);
            validatedIds.add(id);
        }
        if (validatedIds.stream().distinct().count() != validatedIds.size()) {
            throw PlatformException.invalid("content resolution requires distinct identifiers");
        }
        String placeholders = String.join(",", java.util.Collections.nCopies(validatedIds.size(), "?"));
        List<Object> args = new ArrayList<>(validatedIds);
        return jdbc.query("""
                SELECT content_id, author_id, body, published_at FROM contents
                WHERE content_id IN (""" + placeholders + ")", (rs, rowNum) -> publicDocument(
                rs.getString("content_id"), rs.getString("author_id"), rs.getString("body"),
                rs.getString("published_at")), args.toArray());
    }

    private static Map<String, Object> publicDocument(String contentId, String authorId, String body, String publishedAt) {
        Map<String, Object> document = new LinkedHashMap<>();
        document.put("content_id", contentId);
        document.put("author_id", authorId);
        document.put("body", body);
        document.put("published_at", publishedAt);
        document.put("version", 1);
        document.put("visibility", "public");
        document.put("source_uri", "/v1/content/" + contentId);
        return document;
    }

    private static void requireConsumer(String consumerId) {
        requireText(consumerId, "consumer_id", 200);
    }

    private static void requireText(String value, String field, int maximum) {
        if (value == null || value.isEmpty() || !value.equals(value.strip()) || value.length() > maximum) {
            throw PlatformException.invalid(field + " must contain 1 to " + maximum + " non-whitespace characters");
        }
    }
}
