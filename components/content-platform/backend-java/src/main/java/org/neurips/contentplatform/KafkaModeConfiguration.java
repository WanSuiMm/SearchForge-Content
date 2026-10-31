package org.neurips.contentplatform;

import jakarta.annotation.PostConstruct;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Configuration;

@Configuration
public class KafkaModeConfiguration {
    private final String bootstrapServers;
    private final EventDeliveryProperties events;

    public KafkaModeConfiguration(
            EventDeliveryProperties events,
            @Value("${spring.kafka.bootstrap-servers:}") String bootstrapServers) {
        this.events = events;
        this.bootstrapServers = bootstrapServers;
    }

    @PostConstruct
    void validateMode() {
        if ("kafka".equals(events.getDelivery()) && (bootstrapServers == null || bootstrapServers.isBlank())) {
            throw new IllegalStateException("Kafka delivery mode requires CONTENTPLATFORM_KAFKA_BOOTSTRAP_SERVERS");
        }
        if (events.getRelayDelay() == null || events.getRelayDelay().isNegative() || events.getRelayDelay().isZero()
                || events.getRetryDelay() == null || events.getRetryDelay().isNegative() || events.getRetryDelay().isZero()) {
            throw new IllegalStateException("event relay and retry delays must be positive");
        }
    }
}
