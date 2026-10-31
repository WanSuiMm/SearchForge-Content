package org.neurips.contentplatform;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

import java.util.List;

public final class ApiTypes {
    private ApiTypes() {}

    public record RegisterRequest(
            @JsonProperty("user_id") @NotBlank @Size(max = 100) String userId,
            @JsonProperty("display_name") @NotBlank @Size(max = 200) String displayName,
            @NotBlank @Size(min = 12, max = 256) String password) {}

    public record LoginRequest(
            @JsonProperty("user_id") @NotBlank @Size(max = 100) String userId,
            @NotBlank @Size(min = 12, max = 256) String password) {}

    public record FollowRequest(
            @JsonProperty("followee_id") @NotBlank @Size(max = 100) String followeeId) {}

    public record PublishRequest(@NotBlank @Size(max = 20000) String body) {}

    public record ProjectionAckRequest(
            @JsonProperty("consumer_id") @NotBlank @Size(max = 200) String consumerId,
            @JsonProperty("event_id") @NotBlank @Size(max = 200) String eventId) {}

    public record ResolveRequest(@JsonProperty("content_ids") List<Object> contentIds) {}

    public record ContentDto(
            @JsonProperty("content_id") String contentId,
            @JsonProperty("author_id") String authorId,
            @JsonProperty("author_name") String authorName,
            String body,
            @JsonProperty("published_at") String publishedAt,
            @JsonProperty("like_count") long likeCount) {}

    public record PublishResponse(ContentDto content, boolean replayed) {}

    public record FollowResponse(
            @JsonProperty("follower_id") String followerId,
            @JsonProperty("followee_id") String followeeId,
            boolean created) {}

    public record LikeResponse(boolean liked, @JsonProperty("like_count") long likeCount) {}

    public record FeedResponse(List<ContentDto> items,
                               @JsonProperty("next_cursor") String nextCursor) {}

    public record SessionResponse(
            @JsonProperty("user_id") String userId,
            @JsonProperty("access_token") String accessToken,
            @JsonProperty("token_type") String tokenType,
            @JsonProperty("expires_at") double expiresAt) {}
}
