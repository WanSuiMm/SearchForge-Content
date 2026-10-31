package org.neurips.contentplatform;

import jakarta.servlet.ReadListener;
import jakarta.servlet.ServletInputStream;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletRequestWrapper;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;

@Component
@Order(Ordered.HIGHEST_PRECEDENCE + 20)
public class RequestSizeLimitFilter extends OncePerRequestFilter {
    private static final long MAX_BYTES = 64 * 1024;

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                                    jakarta.servlet.FilterChain filterChain)
            throws jakarta.servlet.ServletException, IOException {
        if (request.getContentLengthLong() > MAX_BYTES) {
            response.setStatus(413);
            response.setContentType("application/json; charset=utf-8");
            response.setHeader("Cache-Control", "no-store");
            response.getWriter().write("{\"error\":\"payload_too_large\",\"message\":\"request body exceeds 64 KiB\"}");
            return;
        }
        filterChain.doFilter(new LimitedRequest(request), response);
    }

    private static final class LimitedRequest extends HttpServletRequestWrapper {
        private ServletInputStream limited;
        private long count;

        private LimitedRequest(HttpServletRequest request) { super(request); }

        @Override
        public ServletInputStream getInputStream() throws IOException {
            if (limited == null) {
                ServletInputStream original = super.getInputStream();
                limited = new ServletInputStream() {
                    @Override public int read() throws IOException {
                        int value = original.read();
                        if (value >= 0 && ++count > MAX_BYTES) throw new PayloadTooLargeException();
                        return value;
                    }
                    @Override public int read(byte[] bytes, int offset, int length) throws IOException {
                        int read = original.read(bytes, offset, (int) Math.min(length, MAX_BYTES - count + 1));
                        if (read > 0) {
                            count += read;
                            if (count > MAX_BYTES) throw new PayloadTooLargeException();
                        }
                        return read;
                    }
                    @Override public boolean isFinished() { return original.isFinished(); }
                    @Override public boolean isReady() { return original.isReady(); }
                    @Override public void setReadListener(ReadListener listener) { original.setReadListener(listener); }
                };
            }
            return limited;
        }

        @Override
        public BufferedReader getReader() throws IOException {
            String encoding = getCharacterEncoding();
            return new BufferedReader(new InputStreamReader(getInputStream(),
                    encoding == null ? StandardCharsets.UTF_8 : java.nio.charset.Charset.forName(encoding)));
        }
    }
}
