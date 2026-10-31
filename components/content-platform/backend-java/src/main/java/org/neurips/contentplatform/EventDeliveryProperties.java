package org.neurips.contentplatform;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.validation.annotation.Validated;

import java.time.Duration;

@Validated
@ConfigurationProperties(prefix = "contentplatform.events")
public class EventDeliveryProperties {
    @Pattern(regexp = "db|kafka")
    private String delivery = "db";
    @NotBlank
    private String topic = "content.events";
    @NotBlank
    private String dlqTopic = "content.events.dlq";
    @NotBlank
    private String consumerGroup = "contentplatform-notifications-v1";
    private Duration relayDelay = Duration.ofMillis(500);
    private Duration retryDelay = Duration.ofSeconds(1);

    public String getDelivery() { return delivery; }
    public void setDelivery(String delivery) { this.delivery = delivery; }
    public String getTopic() { return topic; }
    public void setTopic(String topic) { this.topic = topic; }
    public String getDlqTopic() { return dlqTopic; }
    public void setDlqTopic(String dlqTopic) { this.dlqTopic = dlqTopic; }
    public String getConsumerGroup() { return consumerGroup; }
    public void setConsumerGroup(String consumerGroup) { this.consumerGroup = consumerGroup; }
    public Duration getRelayDelay() { return relayDelay; }
    public void setRelayDelay(Duration relayDelay) { this.relayDelay = relayDelay; }
    public Duration getRetryDelay() { return retryDelay; }
    public void setRetryDelay(Duration retryDelay) { this.retryDelay = retryDelay; }
}
