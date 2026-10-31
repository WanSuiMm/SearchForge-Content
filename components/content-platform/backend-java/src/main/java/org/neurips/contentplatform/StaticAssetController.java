package org.neurips.contentplatform;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Locale;
import java.util.Set;

@RestController
public class StaticAssetController {
    private static final Set<String> FLAT_ASSETS = Set.of("app.js", "styles.css");
    private final String staticRoot;

    public StaticAssetController(@Value("${contentplatform.static-root:}") String staticRoot) {
        this.staticRoot = staticRoot == null ? "" : staticRoot.strip();
    }

    @GetMapping(value = "/", produces = MediaType.TEXT_HTML_VALUE)
    public ResponseEntity<byte[]> index() {
        return serve("index.html");
    }

    @GetMapping("/assets/{*assetPath}")
    public ResponseEntity<byte[]> asset(@PathVariable String assetPath) {
        String relative = assetPath.startsWith("/") ? assetPath.substring(1) : assetPath;
        if (relative.isBlank() || relative.contains("\\") || relative.indexOf('\0') >= 0) {
            throw PlatformException.notFound();
        }
        if (FLAT_ASSETS.contains(relative)
                && !isSafeFile("assets/" + relative)) {
            return serve(relative);
        }
        return serve("assets/" + relative);
    }

    private boolean isSafeFile(String relative) {
        if (staticRoot.isBlank()) return false;
        try {
            Path root = Path.of(staticRoot).toRealPath();
            Path candidate = root.resolve(relative).normalize();
            if (!candidate.startsWith(root) || !Files.isRegularFile(candidate)) return false;
            return candidate.toRealPath().startsWith(root);
        } catch (IOException | IllegalArgumentException exception) {
            return false;
        }
    }

    private ResponseEntity<byte[]> serve(String relative) {
        if (staticRoot.isBlank()) throw PlatformException.notFound();
        try {
            Path root = Path.of(staticRoot).toRealPath();
            Path candidate = root.resolve(relative).normalize();
            if (!candidate.startsWith(root) || !Files.isRegularFile(candidate)) throw PlatformException.notFound();
            Path real = candidate.toRealPath();
            if (!real.startsWith(root)) throw PlatformException.notFound();
            String contentType = contentType(real.getFileName().toString());
            HttpHeaders headers = new HttpHeaders();
            headers.setContentType(MediaType.parseMediaType(contentType));
            headers.setCacheControl("no-store");
            headers.set("X-Content-Type-Options", "nosniff");
            headers.set("X-Frame-Options", "DENY");
            headers.set("Content-Security-Policy", "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'");
            return ResponseEntity.ok().headers(headers).body(Files.readAllBytes(real));
        } catch (IOException | IllegalArgumentException exception) {
            throw PlatformException.notFound();
        }
    }

    private static String contentType(String filename) {
        String lower = filename.toLowerCase(Locale.ROOT);
        if (lower.endsWith(".html")) return "text/html; charset=utf-8";
        if (lower.endsWith(".js") || lower.endsWith(".mjs")) return "text/javascript; charset=utf-8";
        if (lower.endsWith(".css")) return "text/css; charset=utf-8";
        if (lower.endsWith(".json")) return "application/json; charset=utf-8";
        if (lower.endsWith(".svg")) return "image/svg+xml";
        if (lower.endsWith(".png")) return "image/png";
        if (lower.endsWith(".jpg") || lower.endsWith(".jpeg")) return "image/jpeg";
        if (lower.endsWith(".woff2")) return "font/woff2";
        return "application/octet-stream";
    }
}
