package org.neurips.contentplatform;

public final class PoisonMessageException extends RuntimeException {
    private final String failureCode;

    public PoisonMessageException(String failureCode) {
        super("Kafka message failed validation");
        this.failureCode = failureCode;
    }

    public String failureCode() { return failureCode; }
}
