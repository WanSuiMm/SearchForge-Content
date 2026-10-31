package org.neurips.contentplatform;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.neurips.contentplatform.ApiTypes.ContentDto;
import org.neurips.contentplatform.ApiTypes.FeedResponse;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.connection.RedisConnection;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import jakarta.annotation.PostConstruct;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ThreadLocalRandom;
import java.util.function.Supplier;

@Service
public class CachedReadService {
    private final PlatformService platform;
    private final JdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final CacheStats stats;
    private final ObjectProvider<StringRedisTemplate> redisProvider;
    private final String mode;
    private final String keyPrefix;
    private final Duration ttl;

    public CachedReadService(
            PlatformService platform,
            JdbcTemplate jdbc,
            ObjectMapper mapper,
            CacheStats stats,
            ObjectProvider<StringRedisTemplate> redisProvider,
            @Value("${contentplatform.cache.mode:off}") String mode,
            @Value("${contentplatform.cache.key-prefix:contentplatform:v1}") String keyPrefix,
            @Value("${contentplatform.cache.ttl:2s}") Duration ttl) {
        this.platform = platform;
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.stats = stats;
        this.redisProvider = redisProvider;
        this.mode = mode.toLowerCase(Locale.ROOT);
        this.keyPrefix = keyPrefix;
        this.ttl = ttl;
        if (!this.mode.equals("off") && !this.mode.equals("redis")) {
            throw new IllegalArgumentException("cache mode must be off or redis");
        }
        if (ttl.isNegative() || ttl.isZero()) throw new IllegalArgumentException("cache TTL must be positive");
    }

    @PostConstruct
    void verifyRedisAtStartup() {
        if (mode.equals("redis")) {
            try {
                RedisConnection connection = redis().getConnectionFactory().getConnection();
                try {
                    String response = connection.ping();
                    if (!"PONG".equalsIgnoreCase(response)) throw new IllegalStateException("Redis startup PING failed");
                } finally {
                    connection.close();
                }
            } catch (Exception exception) {
                throw new IllegalStateException("Redis cache mode requires a reachable Redis service at startup", exception);
            }
        }
    }

    public FeedResponse feed(String userId, int limit, String cursor) {
        return read("feed", userId + "\u0000" + limit + "\u0000" + (cursor == null ? "" : cursor),
                FeedResponse.class, () -> platform.feed(userId, limit, cursor));
    }

    public List<ContentDto> search(String query, int limit) {
        return read("search", query + "\u0000" + limit, new TypeReference<>() {},
                () -> platform.search(query, limit));
    }

    private <T> T read(String namespace, String requestKey, Class<T> type, Supplier<T> loader) {
        return read(namespace, requestKey, type, null, loader);
    }

    private <T> T read(String namespace, String requestKey, TypeReference<T> type, Supplier<T> loader) {
        return read(namespace, requestKey, null, type, loader);
    }

    private <T> T read(String namespace, String requestKey, Class<T> classType,
                       TypeReference<T> genericType, Supplier<T> loader) {
        if (mode.equals("off")) return loader.get();
        String epoch = jdbc.queryForObject("SELECT version FROM cache_epochs WHERE namespace = ?", String.class, namespace);
        String key = keyPrefix + ":" + namespace + ":" + digest(epoch + "\u0000" + requestKey);
        StringRedisTemplate redis = redis();
        try {
            String cached = redis.opsForValue().get(key);
            if (cached != null) {
                stats.hit();
                return classType != null ? mapper.readValue(cached, classType) : mapper.readValue(cached, genericType);
            }
            stats.miss();
        } catch (Exception exception) {
            stats.error();
            stats.bypass();
            return loader.get();
        }

        T value = loader.get();
        try {
            long baseMillis = Math.max(100, ttl.toMillis());
            long jittered = Math.max(100, (long) (baseMillis * ThreadLocalRandom.current().nextDouble(0.9, 1.1)));
            redis.opsForValue().set(key, mapper.writeValueAsString(value), Duration.ofMillis(jittered));
        } catch (Exception exception) {
            stats.error();
            stats.bypass();
        }
        return value;
    }

    private StringRedisTemplate redis() {
        StringRedisTemplate template = redisProvider.getIfAvailable();
        if (template == null) throw new IllegalStateException("Redis cache mode is enabled but Redis is not configured");
        return template;
    }

    private static String digest(String value) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception exception) {
            throw new IllegalStateException("SHA-256 is unavailable", exception);
        }
    }
}
