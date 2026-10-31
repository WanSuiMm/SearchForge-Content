package org.neurips.contentplatform;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.core.ProducerFactory;

@Configuration
@ConditionalOnProperty(name = "contentplatform.events.delivery", havingValue = "kafka")
public class KafkaProducerConfiguration {
    @Bean(name = "kafkaTemplate")
    @SuppressWarnings({"unchecked", "rawtypes"})
    KafkaTemplate<String, String> kafkaTemplate(ProducerFactory<?, ?> producerFactory) {
        // application.yml sets the producer key/value serializers to StringSerializer.
        // The cast supplies precise application types to the relay and DLQ listener.
        return new KafkaTemplate((ProducerFactory) producerFactory);
    }
}
