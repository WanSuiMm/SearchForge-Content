package org.neurips.contentplatform;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.Map;

@Component
@ConditionalOnProperty(name = "contentplatform.events.delivery", havingValue = "db", matchIfMissing = true)
public class LocalOutboxDispatcher {
    private final JdbcTemplate jdbc;
    private final NotificationDeliveryService notifications;

    public LocalOutboxDispatcher(JdbcTemplate jdbc, NotificationDeliveryService notifications) {
        this.jdbc = jdbc;
        this.notifications = notifications;
    }

    @Scheduled(fixedDelayString = "${contentplatform.events.relay-delay:500ms}")
    @Transactional
    public void drainBatch() {
        double now = System.currentTimeMillis() / 1000.0;
        List<Map<String, Object>> events = jdbc.queryForList("""
                SELECT event_id, event_type, payload_json
                FROM outbox
                WHERE status = 'pending' AND available_at <= ?
                ORDER BY created_at, event_id LIMIT 25
                FOR UPDATE SKIP LOCKED
                """, now);
        for (Map<String, Object> event : events) {
            String eventId = String.valueOf(event.get("event_id"));
            notifications.deliver(eventId, String.valueOf(event.get("event_type")),
                    String.valueOf(event.get("payload_json")));
            jdbc.update("UPDATE outbox SET status = 'delivered', delivered_at = ? WHERE event_id = ?",
                    UtcTimestamps.now(), eventId);
        }
    }
}
