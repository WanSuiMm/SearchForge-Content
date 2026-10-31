package org.neurips.contentplatform;

import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.springframework.dao.DataAccessResourceFailureException;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.support.Acknowledgment;
import org.junit.jupiter.api.Test;

import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.CompletableFuture;

import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentCaptor.forClass;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class KafkaNotificationListenerTest {
    private static final ObjectMapper JSON = new ObjectMapper()
            .enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION);

    @Test
    void jsonNullMustReachDlqAsInvalidEnvelopeBeforeManualAcknowledgement() throws Exception {
        KafkaTemplate<String, String> kafka = mock(KafkaTemplate.class);
        NotificationDeliveryService delivery = mock(NotificationDeliveryService.class);
        Acknowledgment acknowledgment = mock(Acknowledgment.class);
        when(kafka.send(any(ProducerRecord.class))).thenReturn(CompletableFuture.completedFuture(null));
        KafkaNotificationListener listener = new KafkaNotificationListener(JSON, delivery, kafka, eventProperties());
        ConsumerRecord<String, String> record = new ConsumerRecord<>("content.events", 1, 14, "event-null", "null");

        listener.consume(record, acknowledgment);

        var capture = forClass(ProducerRecord.class);
        var order = inOrder(kafka, acknowledgment);
        order.verify(kafka).send(capture.capture());
        order.verify(acknowledgment).acknowledge();
        var failureHeader = capture.getValue().headers().lastHeader("failure-code");
        assertNotNull(failureHeader);
        assertEquals("invalid_envelope", new String(failureHeader.value(), StandardCharsets.UTF_8));
        verify(delivery, never()).deliver(any(), any(), any());
    }

    @Test
    void poisonKeyMismatchMustReachDlqBeforeManualAcknowledgement() throws Exception {
        KafkaTemplate<String, String> kafka = mock(KafkaTemplate.class);
        NotificationDeliveryService delivery = mock(NotificationDeliveryService.class);
        Acknowledgment acknowledgment = mock(Acknowledgment.class);
        when(kafka.send(any(ProducerRecord.class))).thenReturn(CompletableFuture.completedFuture(null));
        KafkaNotificationListener listener = new KafkaNotificationListener(JSON, delivery, kafka, eventProperties());

        String value = JSON.writeValueAsString(Map.of(
                "event_id", "event-1",
                "event_type", "content.published",
                "payload", Map.of("content_id", "content-1", "author_id", "alice", "recipient_ids", java.util.List.of()),
                "created_at", "2026-10-24T00:00:00Z"));
        ConsumerRecord<String, String> record = new ConsumerRecord<>("content.events", 2, 15, "wrong-key", value);
        listener.consume(record, acknowledgment);

        var order = inOrder(kafka, acknowledgment);
        order.verify(kafka).send(any(ProducerRecord.class));
        order.verify(acknowledgment).acknowledge();
        verify(delivery, never()).deliver(any(), any(), any());
    }

    @Test
    void transientDatabaseFailureMustLeaveOffsetUnacknowledgedAndAvoidDlq() throws Exception {
        KafkaTemplate<String, String> kafka = mock(KafkaTemplate.class);
        NotificationDeliveryService delivery = mock(NotificationDeliveryService.class);
        Acknowledgment acknowledgment = mock(Acknowledgment.class);
        KafkaNotificationListener listener = new KafkaNotificationListener(JSON, delivery, kafka, eventProperties());

        String eventId = "event-2";
        String value = JSON.writeValueAsString(Map.of(
                "event_id", eventId,
                "event_type", "content.published",
                "payload", Map.of("content_id", "content-2", "author_id", "alice", "recipient_ids", java.util.List.of()),
                "created_at", "2026-10-24T00:00:00Z"));
        ConsumerRecord<String, String> record = new ConsumerRecord<>("content.events", 0, 21, eventId, value);
        doThrow(new DataAccessResourceFailureException("temporary test database failure"))
                .when(delivery).deliver(eq(eventId), eq("content.published"), anyString());

        assertThrows(DataAccessResourceFailureException.class, () -> listener.consume(record, acknowledgment));
        verify(kafka, never()).send(any(ProducerRecord.class));
        verify(acknowledgment, never()).acknowledge();
    }

    @Test
    void failedDlqWriteMustNotAcknowledgeSourceOffset() throws Exception {
        KafkaTemplate<String, String> kafka = mock(KafkaTemplate.class);
        NotificationDeliveryService delivery = mock(NotificationDeliveryService.class);
        Acknowledgment acknowledgment = mock(Acknowledgment.class);
        when(kafka.send(any(ProducerRecord.class))).thenReturn(
                CompletableFuture.failedFuture(new IllegalStateException("broker unavailable")));
        KafkaNotificationListener listener = new KafkaNotificationListener(JSON, delivery, kafka, eventProperties());
        ConsumerRecord<String, String> record = new ConsumerRecord<>("content.events", 0, 22, "wrong-key", "not-json");

        assertThrows(IllegalStateException.class, () -> listener.consume(record, acknowledgment));
        verify(acknowledgment, never()).acknowledge();
    }

    private static EventDeliveryProperties eventProperties() {
        EventDeliveryProperties properties = new EventDeliveryProperties();
        properties.setDlqTopic("content.events.dlq");
        return properties;
    }
}
