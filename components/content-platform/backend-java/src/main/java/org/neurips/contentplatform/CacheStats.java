package org.neurips.contentplatform;

import org.springframework.stereotype.Component;

import java.util.Map;
import java.util.concurrent.atomic.LongAdder;

@Component
public class CacheStats {
    private final LongAdder hits = new LongAdder();
    private final LongAdder misses = new LongAdder();
    private final LongAdder errors = new LongAdder();
    private final LongAdder bypasses = new LongAdder();

    public void hit() { hits.increment(); }
    public void miss() { misses.increment(); }
    public void error() { errors.increment(); }
    public void bypass() { bypasses.increment(); }

    public Map<String, Long> snapshot() {
        return Map.of("cache_hits", hits.sum(), "cache_misses", misses.sum(),
                "cache_errors", errors.sum(), "cache_bypasses", bypasses.sum(),
                "cache_invalidation_failures", 0L);
    }
}
