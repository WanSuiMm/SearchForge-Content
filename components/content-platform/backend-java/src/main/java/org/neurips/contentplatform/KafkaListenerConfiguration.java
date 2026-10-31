package org.neurips.contentplatform;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.autoconfigure.kafka.ConcurrentKafkaListenerContainerFactoryConfigurer;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.kafka.config.ConcurrentKafkaListenerContainerFactory;
import org.springframework.kafka.core.ConsumerFactory;
import org.springframework.kafka.listener.DefaultErrorHandler;
import org.springframework.util.backoff.FixedBackOff;

@Configuration
@ConditionalOnProperty(name = "contentplatform.events.delivery", havingValue = "kafka")
public class KafkaListenerConfiguration {
    private final long retryDelayMillis;

    public KafkaListenerConfiguration(EventDeliveryProperties events) {
        this.retryDelayMillis = Math.max(100L, events.getRetryDelay().toMillis());
    }

    @Bean
    ConcurrentKafkaListenerContainerFactory<Object, Object> kafkaListenerContainerFactory(
            ConcurrentKafkaListenerContainerFactoryConfigurer configurer,
            ConsumerFactory<Object, Object> consumerFactory) {
        ConcurrentKafkaListenerContainerFactory<Object, Object> factory = new ConcurrentKafkaListenerContainerFactory<>();
        configurer.configure(factory, consumerFactory);
        // Failed DB work or failed DLQ publication retries indefinitely with a
        // bounded delay. No exhausted-retry recovery path commits the source offset.
        factory.setCommonErrorHandler(new DefaultErrorHandler(
                new FixedBackOff(retryDelayMillis, FixedBackOff.UNLIMITED_ATTEMPTS)));
        return factory;
    }
}
