package org.neurips.contentplatform;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

@Repository
public class OutboxRelayRepository {
    private final JdbcTemplate jdbc;

    public OutboxRelayRepository(JdbcTemplate jdbc) { this.jdbc = jdbc; }

    @Transactional
    public List<OutboxEvent> claim(String workerId, int limit) {
        double now = System.currentTimeMillis() / 1000.0;
        List<String> ids = jdbc.query("""
                SELECT event_id FROM outbox
                WHERE available_at <= ?
                  AND (status = 'pending' OR (status = 'claimed' AND lease_until <= ?))
                ORDER BY created_at, event_id LIMIT ? FOR UPDATE SKIP LOCKED
                """, (rs, rowNum) -> rs.getString(1), now, now, limit);
        List<OutboxEvent> events = new java.util.ArrayList<>(ids.size());
        for (String id : ids) {
            jdbc.update("""
                    UPDATE outbox SET status = 'claimed', attempts = attempts + 1,
                        lease_owner = ?, lease_until = ? WHERE event_id = ?
                    """, workerId, now + 30.0, id);
            events.add(jdbc.queryForObject("""
                    SELECT event_id, event_type, payload_json, created_at, attempts
                    FROM outbox WHERE event_id = ?
                    """, (rs, rowNum) -> new OutboxEvent(rs.getString("event_id"), rs.getString("event_type"),
                    rs.getString("payload_json"), rs.getString("created_at"), rs.getInt("attempts")), id));
        }
        return events;
    }

    @Transactional
    public boolean acknowledge(String eventId, String workerId) {
        int changed = jdbc.update("""
                UPDATE outbox SET status = 'delivered', delivered_at = ?, lease_owner = NULL, lease_until = NULL
                WHERE event_id = ? AND status = 'claimed' AND lease_owner = ? AND lease_until > ?
                """, UtcTimestamps.now(), eventId, workerId,
                System.currentTimeMillis() / 1000.0);
        return changed == 1;
    }

    @Transactional
    public void retry(String eventId, String workerId, int attempts) {
        double delay = Math.min(300.0, Math.pow(2.0, Math.max(0, attempts - 1)));
        jdbc.update("""
                UPDATE outbox SET status = 'pending', available_at = ?, last_error = 'kafka_publish_failed',
                    lease_owner = NULL, lease_until = NULL
                WHERE event_id = ? AND status = 'claimed' AND lease_owner = ?
                """, System.currentTimeMillis() / 1000.0 + delay, eventId, workerId);
    }

    public record OutboxEvent(String eventId, String eventType, String payloadJson, String createdAt, int attempts) {}
}
