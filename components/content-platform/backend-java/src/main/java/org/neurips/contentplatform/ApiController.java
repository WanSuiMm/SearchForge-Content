package org.neurips.contentplatform;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import org.neurips.contentplatform.ApiTypes.FeedResponse;
import org.neurips.contentplatform.ApiTypes.RegisterRequest;
import org.neurips.contentplatform.ApiTypes.LoginRequest;
import org.neurips.contentplatform.ApiTypes.FollowRequest;
import org.neurips.contentplatform.ApiTypes.PublishRequest;
import org.neurips.contentplatform.ApiTypes.ProjectionAckRequest;
import org.neurips.contentplatform.ApiTypes.ResolveRequest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.List;
import java.util.Map;
import java.util.Set;

@RestController
@RequestMapping
public class ApiController {
    private final PlatformService platform;
    private final CachedReadService cachedReads;
    private final ProjectionService projections;
    private final AuthRateLimiter authRateLimiter;
    private final CacheStats cacheStats;
    private final String projectionToken;
    private final String projectionConsumerId;

    public ApiController(
            PlatformService platform,
            CachedReadService cachedReads,
            ProjectionService projections,
            AuthRateLimiter authRateLimiter,
            CacheStats cacheStats,
            @Value("${contentplatform.projection.token:}") String projectionToken,
            @Value("${contentplatform.projection.consumer-id:searchforge-content-v1}") String projectionConsumerId) {
        this.platform = platform;
        this.cachedReads = cachedReads;
        this.projections = projections;
        this.authRateLimiter = authRateLimiter;
        this.cacheStats = cacheStats;
        this.projectionToken = projectionToken;
        this.projectionConsumerId = projectionConsumerId;
    }

    @GetMapping("/healthz")
    public Map<String, String> health() {
        return Map.of("status", "ok");
    }

    @GetMapping("/metrics")
    public Map<String, Long> metrics() {
        return cacheStats.snapshot();
    }

    @PostMapping("/v1/users")
    public ResponseEntity<ApiTypes.SessionResponse> register(
            @Valid @RequestBody RegisterRequest request, HttpServletRequest servletRequest) {
        limitAuth(servletRequest);
        return ResponseEntity.status(HttpStatus.CREATED)
                .header("Cache-Control", "no-store")
                .body(platform.register(request.userId(), request.displayName(), request.password()));
    }

    @PostMapping("/v1/sessions")
    public ResponseEntity<ApiTypes.SessionResponse> login(
            @Valid @RequestBody LoginRequest request, HttpServletRequest servletRequest) {
        limitAuth(servletRequest);
        return ResponseEntity.ok().header("Cache-Control", "no-store")
                .body(platform.login(request.userId(), request.password()));
    }

    @DeleteMapping("/v1/sessions/current")
    public Map<String, Boolean> revoke(HttpServletRequest request) {
        String token = requiredSession(request);
        platform.revoke(token);
        return Map.of("revoked", true);
    }

    @GetMapping("/v1/me")
    public Map<String, String> me(HttpServletRequest request) {
        checkQuery(request, Set.of());
        return Map.of("user_id", platform.authenticate(requiredSession(request)));
    }

    @PostMapping("/v1/follows")
    public ApiTypes.FollowResponse follow(@Valid @RequestBody FollowRequest body, HttpServletRequest request) {
        String userId = platform.authenticate(requiredSession(request));
        return platform.follow(userId, body.followeeId());
    }

    @PostMapping("/v1/content")
    public ResponseEntity<ApiTypes.PublishResponse> publish(
            @Valid @RequestBody PublishRequest body,
            HttpServletRequest request,
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey) {
        requireSingleHeader(request, "Idempotency-Key");
        String userId = platform.authenticate(requiredSession(request));
        if (idempotencyKey == null || idempotencyKey.isBlank()) {
            throw PlatformException.invalid("Idempotency-Key is required");
        }
        ApiTypes.PublishResponse response = platform.publish(userId, body.body(), idempotencyKey);
        return ResponseEntity.status(response.replayed() ? HttpStatus.OK : HttpStatus.CREATED).body(response);
    }

    @GetMapping("/v1/feed")
    public FeedResponse feed(HttpServletRequest request) {
        String userId = platform.authenticate(requiredSession(request));
        checkQuery(request, Set.of("limit", "cursor"));
        int limit = queryLimit(request, 20, 100);
        return cachedReads.feed(userId, limit, singleQuery(request, "cursor"));
    }

    @GetMapping("/v1/search")
    public Map<String, Object> search(HttpServletRequest request) {
        platform.authenticate(requiredSession(request));
        checkQuery(request, Set.of("q", "limit"));
        String query = singleQuery(request, "q");
        if (query == null) throw PlatformException.invalid("q is required");
        return Map.of("items", cachedReads.search(query, queryLimit(request, 20, 100)));
    }

    @GetMapping("/v1/notifications")
    public Map<String, Object> notifications(HttpServletRequest request) {
        String userId = platform.authenticate(requiredSession(request));
        checkQuery(request, Set.of("limit"));
        return Map.of("items", platform.notifications(userId, queryLimit(request, 20, 100)));
    }

    @PostMapping("/v1/content/{contentId}/likes")
    public ApiTypes.LikeResponse like(@PathVariable String contentId, HttpServletRequest request) {
        requireEmptyBody(request);
        String userId = platform.authenticate(requiredSession(request));
        return platform.like(userId, contentId);
    }

    @GetMapping("/v1/content/{contentId}")
    public ApiTypes.ContentDto content(@PathVariable String contentId, HttpServletRequest request) {
        platform.authenticate(requiredSession(request));
        checkQuery(request, Set.of());
        return platform.content(contentId);
    }

    @GetMapping("/internal/v1/content-projection/events")
    public ResponseEntity<Map<String, Object>> projectionEvents(HttpServletRequest request) {
        authorizeProjection(request);
        checkQuery(request, Set.of("consumer_id", "limit"));
        String consumerId = singleQuery(request, "consumer_id");
        requireProjectionConsumer(consumerId);
        return ResponseEntity.ok().header("Cache-Control", "no-store")
                .body(Map.of("events", projections.pending(consumerId, queryLimit(request, 100, 100))));
    }

    @PostMapping("/internal/v1/content-projection/ack")
    public ResponseEntity<Map<String, Boolean>> projectionAck(
            @Valid @RequestBody ProjectionAckRequest body, HttpServletRequest request) {
        authorizeProjection(request);
        requireProjectionConsumer(body.consumerId());
        return ResponseEntity.ok().header("Cache-Control", "no-store")
                .body(Map.of("acknowledged", projections.acknowledge(body.consumerId(), body.eventId())));
    }

    @GetMapping("/internal/v1/content-projection/content/{contentId}")
    public ResponseEntity<Map<String, Object>> projectionContent(
            @PathVariable String contentId, HttpServletRequest request) {
        authorizeProjection(request);
        checkQuery(request, Set.of());
        return ResponseEntity.ok().header("Cache-Control", "no-store")
                .body(projections.publicContent(contentId));
    }

    @PostMapping("/internal/v1/content-projection/resolve")
    public ResponseEntity<Map<String, Object>> projectionResolve(
            @Valid @RequestBody ResolveRequest body, HttpServletRequest request) {
        authorizeProjection(request);
        return ResponseEntity.ok().header("Cache-Control", "no-store")
                .body(Map.of("items", projections.publicContentMany(body.contentIds())));
    }

    private void limitAuth(HttpServletRequest request) {
        if (!authRateLimiter.allow(request.getRemoteAddr())) {
            throw new PlatformException(429, "rate_limited", "request rate is temporarily limited");
        }
    }

    private void authorizeProjection(HttpServletRequest request) {
        if (projectionToken == null || projectionToken.isBlank()) throw PlatformException.notFound();
        String token = requiredSession(request);
        if (!constantTimeDigestEquals(projectionToken, token)) throw PlatformException.unauthorized();
    }

    private void requireProjectionConsumer(String consumerId) {
        if (consumerId == null || consumerId.isBlank()) {
            throw PlatformException.invalid("consumer_id is required");
        }
        if (!consumerId.equals(projectionConsumerId)) {
            throw new PlatformException(403, "forbidden", "consumer is not enabled for this source");
        }
    }

    private static String requiredSession(HttpServletRequest request) {
        requireSingleHeader(request, "Authorization");
        String header = request.getHeader("Authorization");
        if (header == null || !header.startsWith("Bearer ") || header.length() <= 7) {
            throw PlatformException.unauthorized();
        }
        return header.substring(7);
    }

    private static void requireSingleHeader(HttpServletRequest request, String name) {
        List<String> values = java.util.Collections.list(request.getHeaders(name));
        if (values.size() > 1) throw PlatformException.invalid("repeated " + name + " header is not supported");
    }

    private static void requireEmptyBody(HttpServletRequest request) {
        if (request.getContentLengthLong() > 0 || request.getHeader("Transfer-Encoding") != null) {
            throw PlatformException.invalid("request body must be empty");
        }
    }

    private static void checkQuery(HttpServletRequest request, Set<String> allowed) {
        for (Map.Entry<String, String[]> item : request.getParameterMap().entrySet()) {
            if (!allowed.contains(item.getKey()) || item.getValue().length != 1) {
                throw PlatformException.invalid("unsupported or repeated query parameter");
            }
        }
    }

    private static String singleQuery(HttpServletRequest request, String name) {
        String[] values = request.getParameterValues(name);
        if (values == null) return null;
        if (values.length != 1) throw PlatformException.invalid("repeated query parameter");
        return values[0];
    }

    private static int queryLimit(HttpServletRequest request, int defaultValue, int maximum) {
        String raw = singleQuery(request, "limit");
        if (raw == null) return defaultValue;
        try {
            int value = Integer.parseInt(raw);
            if (value < 1 || value > maximum) throw PlatformException.invalid("limit must be from 1 to " + maximum);
            return value;
        } catch (NumberFormatException exception) {
            throw PlatformException.invalid("limit must be an integer");
        }
    }

    private static boolean constantTimeDigestEquals(String expected, String supplied) {
        return MessageDigest.isEqual(digest(expected), digest(supplied));
    }

    private static byte[] digest(String value) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
        } catch (java.security.NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 is unavailable", exception);
        }
    }
}
