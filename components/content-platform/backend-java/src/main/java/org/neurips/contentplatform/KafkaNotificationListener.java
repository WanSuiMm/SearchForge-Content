package org.neurips.contentplatform;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.common.header.internals.RecordHeader;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Component;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Map;
import java.util.Set;
import java.util.HexFormat;
import java.util.concurrent.TimeUnit;

@Component
@ConditionalOnProperty(name = "contentplatform.events.delivery", havingValue = "kafka")
public class KafkaNotificationListener {
    private final ObjectMapper mapper;
    private final NotificationDeliveryService notifications;
    private final KafkaTemplate<String, String> kafka;
    private final EventDeliveryProperties events;

    public KafkaNotificationListener(
            ObjectMapper mapper,
            NotificationDeliveryService notifications,
            KafkaTemplate<String, String> kafka,
            EventDeliveryProperties events) {
        this.mapper = mapper;
        this.notifications = notifications;
        this.kafka = kafka;
        this.events = events;
    }

    @org.springframework.kafka.annotation.KafkaListener(
            topics = "${contentplatform.events.topic:content.events}",
            groupId = "${contentplatform.events.consumer-group:contentplatform-notifications-v1}")
    public void consume(ConsumerRecord<String, String> record, Acknowledgment acknowledgment) throws Exception {
        final Map<String, Object> envelope;
        try {
            if (record.value() == null) throw new JsonProcessingException("null Kafka value") {};
            envelope = mapper.readValue(record.value(), new TypeReference<>() {});
        } catch (JsonProcessingException | IllegalArgumentException exception) {
            sendToDeadLetter(record, "invalid_json", acknowledgment);
            return;
        }
        if (envelope == null) {
            sendToDeadLetter(record, "invalid_envelope", acknowledgment);
            return;
        }

        Object eventIdValue = envelope.get("event_id");
        Object eventTypeValue = envelope.get("event_type");
        Object payload = envelope.get("payload");
        Object createdAtValue = envelope.get("created_at");
        if (!envelope.keySet().equals(Set.of("event_id", "event_type", "payload", "created_at"))
                || !(eventIdValue instanceof String eventId) || eventId.isBlank() || eventId.length() > 200
                || !(eventTypeValue instanceof String eventType) || !(createdAtValue instanceof String createdAt)
                || createdAt.isBlank() || !(payload instanceof Map<?, ?>)) {
            sendToDeadLetter(record, "invalid_envelope", acknowledgment);
            return;
        }
        if (!eventId.equals(record.key())) {
            sendToDeadLetter(record, "key_mismatch", acknowledgment);
            return;
        }
        if (!"content.published".equals(eventType)) {
            sendToDeadLetter(record, "unsupported_event", acknowledgment);
            return;
        }

        try {
            String payloadJson = mapper.writeValueAsString(payload);
            notifications.deliver(eventId, eventType, payloadJson);
        } catch (PoisonMessageException poison) {
            sendToDeadLetter(record, poison.failureCode(), acknowledgment);
            return;
        }
        // The proxied database operation commits before it returns. Manual Kafka
        // acknowledgement happens only after that commit has completed.
        acknowledgment.acknowledge();
    }

    private void sendToDeadLetter(ConsumerRecord<String, String> source, String failureCode,
                                 Acknowledgment acknowledgment) throws Exception {
        String coordinate = source.topic() + ":" + source.partition() + ":" + source.offset();
        String stableKey = sha256(coordinate);
        ProducerRecord<String, String> record = new ProducerRecord<>(events.getDlqTopic(), stableKey, source.value());
        record.headers().add(new RecordHeader("failure-code", failureCode.getBytes(StandardCharsets.UTF_8)));
        record.headers().add(new RecordHeader("source-topic", source.topic().getBytes(StandardCharsets.UTF_8)));
        record.headers().add(new RecordHeader("source-partition",
                Integer.toString(source.partition()).getBytes(StandardCharsets.US_ASCII)));
        record.headers().add(new RecordHeader("source-offset",
                Long.toString(source.offset()).getBytes(StandardCharsets.US_ASCII)));
        try {
            // If the DLQ broker write fails, the exception escapes. Since auto
            // commit is disabled and no acknowledgement has happened, the source
            // offset stays in place and the record is retried.
            kafka.send(record).get(15, TimeUnit.SECONDS);
        } catch (Exception exception) {
            throw new IllegalStateException("dead-letter publish failed; source offset remains unacknowledged");
        }
        acknowledgment.acknowledge();
    }

    private static String sha256(String value) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception exception) {
            throw new IllegalStateException("SHA-256 is unavailable", exception);
        }
    }
}
