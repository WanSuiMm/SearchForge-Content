package org.neurips.contentplatform;

import java.time.Instant;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeFormatterBuilder;

public final class UtcTimestamps {
    private static final DateTimeFormatter SORTABLE_UTC = new DateTimeFormatterBuilder()
            .appendInstant(9)
            .toFormatter();

    private UtcTimestamps() {}

    public static String now() { return SORTABLE_UTC.format(Instant.now()); }
}
