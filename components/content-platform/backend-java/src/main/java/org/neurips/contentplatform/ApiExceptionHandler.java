package org.neurips.contentplatform;

import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.CannotGetJdbcConnectionException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.http.converter.HttpMessageNotReadableException;

import java.util.Map;

@RestControllerAdvice
public class ApiExceptionHandler {
    @ExceptionHandler(PlatformException.class)
    ResponseEntity<Map<String, String>> platform(PlatformException exception) {
        return response(exception.status(), exception.code(), exception.getMessage());
    }

    @ExceptionHandler({MethodArgumentNotValidException.class, HttpMessageNotReadableException.class})
    ResponseEntity<Map<String, String>> invalidRequest(Exception exception) {
        Throwable cause = exception;
        while (cause != null) {
            if (cause instanceof PayloadTooLargeException) {
                return response(413, "payload_too_large", "request body exceeds 64 KiB");
            }
            cause = cause.getCause();
        }
        return response(400, "invalid_request", "request was not accepted");
    }

    @ExceptionHandler(DataIntegrityViolationException.class)
    ResponseEntity<Map<String, String>> conflict(DataIntegrityViolationException ignored) {
        return response(409, "conflict", "request conflicts with stored data");
    }

    @ExceptionHandler({CannotGetJdbcConnectionException.class, org.springframework.dao.TransientDataAccessException.class})
    ResponseEntity<Map<String, String>> unavailable(Exception ignored) {
        return response(503, "storage_unavailable", "storage is temporarily unavailable");
    }

    @ExceptionHandler(Exception.class)
    ResponseEntity<Map<String, String>> internal(Exception ignored) {
        return response(500, "internal_error", "request could not be completed");
    }

    private static ResponseEntity<Map<String, String>> response(int status, String code, String message) {
        return ResponseEntity.status(HttpStatus.valueOf(status)).body(Map.of("error", code, "message", message));
    }
}
