package org.neurips.contentplatform;

import java.io.IOException;

public final class PayloadTooLargeException extends IOException {
    public PayloadTooLargeException() { super("request body exceeded the configured limit"); }
}
