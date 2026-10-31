package org.neurips.contentplatform;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.apache.kafka.common.KafkaException;
import org.junit.jupiter.api.Test;
import org.springframework.kafka.core.KafkaTemplate;

import java.util.List;
import java.util.concurrent.CompletableFuture;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class KafkaOutboxRelayTest {
    @Test
    void brokerFailureRecordsBoundedCategoryAndKeepsOutboxEventPendingForRetry() {
        OutboxRelayRepository repository = mock(OutboxRelayRepository.class);
        KafkaTemplate<String, String> kafka = mock(KafkaTemplate.class);
        SimpleMeterRegistry metrics = new SimpleMeterRegistry();
        EventDeliveryProperties events = new EventDeliveryProperties();
        OutboxRelayRepository.OutboxEvent event = new OutboxRelayRepository.OutboxEvent(
                "event-safe-id", "content.published", "{}", "2026-10-24T00:00:00Z", 3);
        when(repository.claim(anyString(), eq(20))).thenReturn(List.of(event));
        when(kafka.send(anyString(), anyString(), anyString())).thenReturn(
                CompletableFuture.failedFuture(new KafkaException("must not be logged or stored")));

        new KafkaOutboxRelay(repository, kafka, new ObjectMapper(), events, metrics).relayBatch();

        assertEquals(1.0, metrics.get("contentplatform.outbox.publish.failures")
                .tag("category", "kafka").counter().count());
        verify(repository).retry(eq(event.eventId()), anyString(), eq(event.attempts()));
    }
}
