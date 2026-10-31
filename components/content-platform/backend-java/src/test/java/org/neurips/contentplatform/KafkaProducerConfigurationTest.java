package org.neurips.contentplatform;

import org.apache.kafka.clients.producer.KafkaProducer;
import org.junit.jupiter.api.Test;
import org.springframework.boot.autoconfigure.kafka.KafkaProperties;
import org.springframework.boot.env.YamlPropertySourceLoader;
import org.springframework.core.env.MapPropertySource;
import org.springframework.core.env.StandardEnvironment;
import org.springframework.core.io.ClassPathResource;
import org.springframework.boot.context.properties.bind.Bindable;
import org.springframework.boot.context.properties.bind.Binder;

import java.io.IOException;
import java.time.Duration;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;

class KafkaProducerConfigurationTest {
    @Test
    void configuredProducerCanBeConstructedAndClosedWithoutBrokerAccess() throws IOException {
        StandardEnvironment environment = new StandardEnvironment();
        environment.getPropertySources().addFirst(new MapPropertySource("offline-kafka-bootstrap", Map.of(
                "spring.kafka.bootstrap-servers", "127.0.0.1:1")));
        new YamlPropertySourceLoader().load("application", new ClassPathResource("application.yml"))
                .forEach(environment.getPropertySources()::addLast);

        KafkaProperties kafkaProperties = Binder.get(environment)
                .bind("spring.kafka", Bindable.of(KafkaProperties.class))
                .get();
        Map<String, Object> producerProperties = kafkaProperties.buildProducerProperties();
        assertEquals(15000, Integer.parseInt(producerProperties.get("delivery.timeout.ms").toString()));
        assertEquals(10000, Integer.parseInt(producerProperties.get("request.timeout.ms").toString()));
        assertEquals(10000, Integer.parseInt(producerProperties.get("max.block.ms").toString()));

        KafkaProducer<String, String> producer = new KafkaProducer<>(producerProperties);
        producer.close(Duration.ofMillis(100));
    }
}
