package org.neurips.contentplatform;

import org.springframework.stereotype.Component;

import java.util.HashMap;
import java.util.Map;

@Component
public class AuthRateLimiter {
    private static final int MAX_PEERS = 4096;
    private static final double BURST = 120.0;
    private static final double TOKENS_PER_SECOND = 60.0;
    private final Map<String, Bucket> buckets = new HashMap<>();

    public synchronized boolean allow(String peer) {
        long now = System.nanoTime();
        Bucket bucket = buckets.get(peer);
        if (bucket == null) {
            if (buckets.size() >= MAX_PEERS) return false;
            bucket = new Bucket(BURST, now);
            buckets.put(peer, bucket);
        }
        double elapsed = Math.max(0, now - bucket.lastRefill) / 1_000_000_000.0;
        bucket.tokens = Math.min(BURST, bucket.tokens + elapsed * TOKENS_PER_SECOND);
        bucket.lastRefill = now;
        if (bucket.tokens < 1.0) return false;
        bucket.tokens -= 1.0;
        return true;
    }

    private static final class Bucket {
        private double tokens;
        private long lastRefill;
        private Bucket(double tokens, long lastRefill) {
            this.tokens = tokens;
            this.lastRefill = lastRefill;
        }
    }
}
