package org.neurips.contentplatform;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.BeforeEach;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;

import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest(properties = {
        "spring.datasource.url=jdbc:h2:mem:contentplatform;MODE=PostgreSQL;DB_CLOSE_DELAY=-1;DATABASE_TO_LOWER=TRUE",
        "spring.datasource.username=sa",
        "spring.datasource.password=",
        "spring.datasource.driver-class-name=org.h2.Driver",
        "spring.flyway.locations=classpath:db/migration/common",
        "contentplatform.projection.token=test-projection-token",
        "contentplatform.projection.consumer-id=searchforge-content-v1",
        "contentplatform.cache.mode=off",
        "contentplatform.events.delivery=db"
})
@AutoConfigureMockMvc
class ContentPlatformHttpIntegrationTest {
    @Autowired MockMvc mvc;
    @Autowired ObjectMapper mapper;
    @Autowired JdbcTemplate jdbc;
    @Autowired LocalOutboxDispatcher localOutbox;

    @BeforeEach
    void clearDatabase() {
        jdbcExecute("DELETE FROM projection_event_receipts");
        jdbcExecute("DELETE FROM projection_events");
        jdbcExecute("DELETE FROM notifications");
        jdbcExecute("DELETE FROM consumer_receipts");
        jdbcExecute("DELETE FROM outbox");
        jdbcExecute("DELETE FROM likes");
        jdbcExecute("DELETE FROM feed_entries");
        jdbcExecute("DELETE FROM follows");
        jdbcExecute("DELETE FROM contents");
        jdbcExecute("DELETE FROM auth_sessions");
        jdbcExecute("DELETE FROM auth_credentials");
        jdbcExecute("DELETE FROM users");
    }

    @Test
    void accountContentFeedSearchLikesNotificationsAndSessionRevocationWorkOverHttp() throws Exception {
        String suffix = UUID.randomUUID().toString().substring(0, 8);
        Session owner = register("owner_" + suffix, "Owner");
        Session follower = register("follower_" + suffix, "Follower");

        mvc.perform(post("/v1/follows").header("Authorization", bearer(follower.token()))
                        .contentType("application/json").content(json(Map.of("followee_id", owner.userId()))))
                .andExpect(status().isOk()).andExpect(jsonPath("$.created").value(true));

        MvcResult published = mvc.perform(post("/v1/content")
                        .header("Authorization", bearer(owner.token()))
                        .header("Idempotency-Key", "first-post")
                        .contentType("application/json")
                        .content(json(Map.of("body", "Practical Java platform search marker"))))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.replayed").value(false))
                .andReturn();
        String contentId = mapper.readTree(published.getResponse().getContentAsString()).path("content").path("content_id").asText();

        mvc.perform(post("/v1/content")
                        .header("Authorization", bearer(owner.token()))
                        .header("Idempotency-Key", "first-post")
                        .contentType("application/json")
                        .content(json(Map.of("body", "Practical Java platform search marker"))))
                .andExpect(status().isOk()).andExpect(jsonPath("$.replayed").value(true));
        mvc.perform(post("/v1/content")
                        .header("Authorization", bearer(owner.token()))
                        .header("Idempotency-Key", "first-post")
                        .contentType("application/json").content(json(Map.of("body", "different payload"))))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.error").value("idempotency_conflict"));

        mvc.perform(get("/v1/me").header("Authorization", bearer(follower.token())))
                .andExpect(status().isOk()).andExpect(jsonPath("$.user_id").value(follower.userId()));
        mvc.perform(get("/v1/feed").header("Authorization", bearer(follower.token())))
                .andExpect(status().isOk()).andExpect(jsonPath("$.items[0].content_id").value(contentId));
        mvc.perform(get("/v1/search").param("q", "Java marker")
                        .header("Authorization", bearer(follower.token())))
                .andExpect(status().isOk()).andExpect(jsonPath("$.items[0].content_id").value(contentId));
        mvc.perform(get("/v1/content/" + contentId).header("Authorization", bearer(follower.token())))
                .andExpect(status().isOk()).andExpect(jsonPath("$.body").value("Practical Java platform search marker"));

        mvc.perform(post("/v1/content/" + contentId + "/likes")
                        .header("Authorization", bearer(follower.token())))
                .andExpect(status().isOk()).andExpect(jsonPath("$.liked").value(true))
                .andExpect(jsonPath("$.like_count").value(1));
        mvc.perform(post("/v1/content/" + contentId + "/likes")
                        .header("Authorization", bearer(follower.token())))
                .andExpect(status().isOk()).andExpect(jsonPath("$.liked").value(false))
                .andExpect(jsonPath("$.like_count").value(1));

        localOutbox.drainBatch();
        mvc.perform(get("/v1/notifications").header("Authorization", bearer(follower.token())))
                .andExpect(status().isOk()).andExpect(jsonPath("$.items[0].content_id").value(contentId));
        mvc.perform(delete("/v1/sessions/current").header("Authorization", bearer(follower.token())))
                .andExpect(status().isOk()).andExpect(jsonPath("$.revoked").value(true));
        mvc.perform(get("/v1/me").header("Authorization", bearer(follower.token())))
                .andExpect(status().isUnauthorized());
    }

    @Test
    void projectionHasDedicatedAuthAndExactSourceContract() throws Exception {
        String suffix = UUID.randomUUID().toString().substring(0, 8);
        Session owner = register("projection_" + suffix, "Projection Owner");
        MvcResult published = mvc.perform(post("/v1/content")
                        .header("Authorization", bearer(owner.token()))
                        .header("Idempotency-Key", "projection-post")
                        .contentType("application/json")
                        .content(json(Map.of("body", "source contract marker"))))
                .andExpect(status().isCreated()).andReturn();
        String contentId = mapper.readTree(published.getResponse().getContentAsString()).path("content").path("content_id").asText();

        mvc.perform(get("/internal/v1/content-projection/events").param("consumer_id", "searchforge-content-v1"))
                .andExpect(status().isUnauthorized());
        String projectionAuth = bearer("test-projection-token");
        MvcResult pending = mvc.perform(get("/internal/v1/content-projection/events")
                        .header("Authorization", projectionAuth)
                        .param("consumer_id", "searchforge-content-v1").param("limit", "10"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.events[0].schema_version").value(1))
                .andExpect(jsonPath("$.events[0].event_type").value("content.published"))
                .andExpect(jsonPath("$.events[0].visibility").value("public"))
                .andExpect(jsonPath("$.events[0].content_id").value(contentId))
                .andExpect(jsonPath("$.events[0].source_uri").value("/v1/content/" + contentId))
                .andReturn();

        JsonNode event = mapper.readTree(pending.getResponse().getContentAsString()).path("events").get(0);
        assertThat(fieldNames(event)).containsExactlyInAnyOrder(
                "schema_version", "event_id", "event_type", "content_id", "version", "visibility",
                "author_id", "body", "published_at", "occurred_at", "source_uri");
        String eventId = event.path("event_id").asText();

        MvcResult detail = mvc.perform(get("/internal/v1/content-projection/content/" + contentId)
                        .header("Authorization", projectionAuth))
                .andExpect(status().isOk()).andExpect(jsonPath("$.version").value(1))
                .andExpect(jsonPath("$.visibility").value("public"))
                .andExpect(jsonPath("$.source_uri").value("/v1/content/" + contentId)).andReturn();
        JsonNode document = mapper.readTree(detail.getResponse().getContentAsString());
        assertThat(fieldNames(document)).containsExactlyInAnyOrder(
                "content_id", "author_id", "body", "published_at", "version", "visibility", "source_uri");

        mvc.perform(post("/internal/v1/content-projection/resolve").header("Authorization", projectionAuth)
                        .contentType("application/json").content(json(Map.of("content_ids", java.util.List.of(contentId)))))
                .andExpect(status().isOk()).andExpect(jsonPath("$.items[0].content_id").value(contentId));
        String ack = json(Map.of("consumer_id", "searchforge-content-v1", "event_id", eventId));
        mvc.perform(post("/internal/v1/content-projection/ack").header("Authorization", projectionAuth)
                        .contentType("application/json").content(ack))
                .andExpect(status().isOk()).andExpect(jsonPath("$.acknowledged").value(true));
        mvc.perform(get("/internal/v1/content-projection/events").header("Authorization", projectionAuth)
                        .param("consumer_id", "searchforge-content-v1"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.events.length()").value(0));
    }

    @Test
    void rejectsOversizeBodiesUnknownFieldsAndUnscopedProjectionCredentials() throws Exception {
        byte[] huge = ("{\"body\":\"" + "x".repeat(70_000) + "\"}").getBytes(StandardCharsets.UTF_8);
        mvc.perform(post("/v1/users").contentType("application/json").content(huge))
                .andExpect(status().isPayloadTooLarge());
        mvc.perform(post("/v1/users").contentType("application/json")
                        .content("{\"user_id\":\"u\",\"display_name\":\"User\",\"password\":\"long-enough-password\",\"unexpected\":true}"))
                .andExpect(status().isBadRequest());
        mvc.perform(post("/v1/users").contentType("application/json")
                        .content("{\"user_id\":\"first\",\"user_id\":\"second\",\"display_name\":\"User\",\"password\":\"long-enough-password\"}"))
                .andExpect(status().isBadRequest());
        mvc.perform(get("/internal/v1/content-projection/events")
                        .header("Authorization", bearer("wrong-projection-token"))
                        .param("consumer_id", "searchforge-content-v1"))
                .andExpect(status().isUnauthorized());
        mvc.perform(get("/internal/v1/content-projection/events")
                        .header("Authorization", bearer("test-projection-token"))
                        .param("consumer_id", "other-consumer"))
                .andExpect(status().isForbidden());
    }

    private Session register(String userId, String displayName) throws Exception {
        MvcResult result = mvc.perform(post("/v1/users").contentType("application/json")
                        .content(json(Map.of("user_id", userId, "display_name", displayName,
                                "password", "correct-horse-battery-staple"))))
                .andExpect(status().isCreated()).andReturn();
        JsonNode response = mapper.readTree(result.getResponse().getContentAsString());
        return new Session(response.path("user_id").asText(), response.path("access_token").asText());
    }

    private String json(Object value) throws Exception { return mapper.writeValueAsString(value); }
    private void jdbcExecute(String sql) { jdbc.update(sql); }
    private static String bearer(String token) { return "Bearer " + token; }
    private static Set<String> fieldNames(JsonNode node) {
        java.util.HashSet<String> names = new java.util.HashSet<>();
        node.fieldNames().forEachRemaining(names::add);
        return names;
    }
    private record Session(String userId, String token) {}
}
