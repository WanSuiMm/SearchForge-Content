package org.neurips.contentplatform;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import org.apache.kafka.common.KafkaException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.dao.DataAccessException;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.sql.SQLException;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CompletionException;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.TimeUnit;

@Component
@ConditionalOnProperty(name = "contentplatform.events.delivery", havingValue = "kafka")
public class KafkaOutboxRelay {
    private static final Logger logger = LoggerFactory.getLogger(KafkaOutboxRelay.class);
    private final OutboxRelayRepository repository;
    private final KafkaTemplate<String, String> kafka;
    private final ObjectMapper mapper;
    private final EventDeliveryProperties events;
    private final MeterRegistry meterRegistry;
    private final String workerId = "java-relay-" + UUID.randomUUID();

    public KafkaOutboxRelay(
            OutboxRelayRepository repository,
            KafkaTemplate<String, String> kafka,
            ObjectMapper mapper,
            EventDeliveryProperties events,
            MeterRegistry meterRegistry) {
        this.repository = repository;
        this.kafka = kafka;
        this.mapper = mapper;
        this.events = events;
        this.meterRegistry = meterRegistry;
    }

    @Scheduled(fixedDelayString = "${contentplatform.events.relay-delay:500ms}")
    public void relayBatch() {
        for (OutboxRelayRepository.OutboxEvent event : repository.claim(workerId, 20)) {
            try {
                Map<String, Object> envelope = new LinkedHashMap<>();
                envelope.put("event_id", event.eventId());
                envelope.put("event_type", event.eventType());
                envelope.put("payload", mapper.readValue(event.payloadJson(), new TypeReference<Map<String, Object>>() {}));
                envelope.put("created_at", event.createdAt());
                String value = mapper.writeValueAsString(envelope);
                kafka.send(events.getTopic(), event.eventId(), value).get(15, TimeUnit.SECONDS);
                repository.acknowledge(event.eventId(), workerId);
            } catch (Exception exception) {
                String category = failureCategory(exception);
                Counter.builder("contentplatform.outbox.publish.failures")
                        .description("Outbox event publish failures by bounded failure category")
                        .tag("category", category)
                        .register(meterRegistry)
                        .increment();
                // Keep diagnostics useful without logging exception messages or payload data.
                logger.warn("Kafka outbox publish failed; event will be retried (category={})", category);
                repository.retry(event.eventId(), workerId, event.attempts());
            }
        }
    }

    private static String failureCategory(Throwable exception) {
        Throwable cause = exception;
        while ((cause instanceof ExecutionException || cause instanceof CompletionException)
                && cause.getCause() != null) {
            cause = cause.getCause();
        }
        if (cause instanceof TimeoutException
                || cause instanceof org.apache.kafka.common.errors.TimeoutException) return "timeout";
        if (cause instanceof InterruptedException) return "interrupted";
        if (cause instanceof JsonProcessingException) return "invalid_outbox_payload";
        if (cause instanceof DataAccessException || cause instanceof SQLException) return "database";
        if (cause instanceof KafkaException) return "kafka";
        return "internal";
    }
}
