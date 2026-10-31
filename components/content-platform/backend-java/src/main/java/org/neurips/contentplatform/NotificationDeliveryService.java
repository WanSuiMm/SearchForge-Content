package org.neurips.contentplatform;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.Map;

@Service
public class NotificationDeliveryService {
    private static final String CONSUMER_NAME = "notifications-v1";
    private final JdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final boolean postgres;

    public NotificationDeliveryService(JdbcTemplate jdbc, ObjectMapper mapper,
                                       @Value("${spring.datasource.url:}") String jdbcUrl) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.postgres = jdbcUrl.startsWith("jdbc:postgresql:");
    }

    @Transactional
    public boolean deliver(String eventId, String eventType, String payloadJson) {
        if (eventId == null || eventId.isBlank() || eventId.length() > 200
                || !"content.published".equals(eventType)) {
            throw new PoisonMessageException("unsupported_event");
        }
        final Map<String, Object> payload;
        try {
            payload = mapper.readValue(payloadJson, new TypeReference<>() {});
        } catch (Exception exception) {
            throw new PoisonMessageException("invalid_payload");
        }
        Object contentIdValue = payload.get("content_id");
        Object recipientValue = payload.get("recipient_ids");
        Object authorIdValue = payload.get("author_id");
        if (!(contentIdValue instanceof String contentId) || contentId.isBlank()
                || !(authorIdValue instanceof String authorId) || authorId.isBlank() || authorId.length() > 100
                || !(recipientValue instanceof List<?> recipients) || recipients.size() > 100_000) {
            throw new PoisonMessageException("invalid_payload");
        }
        for (Object recipient : recipients) {
            if (!(recipient instanceof String id) || id.isBlank() || id.length() > 100) {
                throw new PoisonMessageException("invalid_payload");
            }
        }

        String now = UtcTimestamps.now();
        int inserted;
        try {
            if (postgres) {
                inserted = jdbc.update("""
                        INSERT INTO consumer_receipts(consumer_name, event_id, processed_at)
                        VALUES (?, ?, ?) ON CONFLICT(consumer_name, event_id) DO NOTHING
                        """, CONSUMER_NAME, eventId, now);
            } else {
                Integer existing = jdbc.queryForObject("SELECT COUNT(*) FROM consumer_receipts WHERE consumer_name = ? AND event_id = ?",
                        Integer.class, CONSUMER_NAME, eventId);
                inserted = existing != null && existing > 0 ? 0 : jdbc.update(
                        "INSERT INTO consumer_receipts(consumer_name, event_id, processed_at) VALUES (?, ?, ?)",
                        CONSUMER_NAME, eventId, now);
            }
        } catch (DataIntegrityViolationException exception) {
            throw new PoisonMessageException("invalid_references");
        }
        if (inserted == 0) return false;
        for (Object recipient : recipients) {
            try {
                if (postgres) {
                    jdbc.update("""
                            INSERT INTO notifications(event_id, recipient_id, content_id, created_at)
                            VALUES (?, ?, ?, ?) ON CONFLICT(event_id, recipient_id) DO NOTHING
                            """, eventId, (String) recipient, contentId, now);
                } else {
                    jdbc.update("INSERT INTO notifications(event_id, recipient_id, content_id, created_at) VALUES (?, ?, ?, ?)",
                            eventId, (String) recipient, contentId, now);
                }
            } catch (DataIntegrityViolationException exception) {
                throw new PoisonMessageException("invalid_references");
            }
        }
        return true;
    }
}
