package org.neurips.contentplatform;

public class PlatformException extends RuntimeException {
    private final int status;
    private final String code;

    public PlatformException(int status, String code, String safeMessage) {
        super(safeMessage);
        this.status = status;
        this.code = code;
    }

    public int status() { return status; }
    public String code() { return code; }

    public static PlatformException invalid(String message) {
        return new PlatformException(400, "invalid_request", message);
    }

    public static PlatformException unauthorized() {
        return new PlatformException(401, "authentication_required", "credentials were not accepted");
    }

    public static PlatformException notFound() {
        return new PlatformException(404, "not_found", "resource was not found");
    }

    public static PlatformException conflict(String code, String message) {
        return new PlatformException(409, code, message);
    }
}
