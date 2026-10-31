package org.neurips.contentplatform;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.UUID;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

class StaticAssetControllerTest {
    @TempDir Path staticRoot;

    @Test
    void flatReactRootServesKnownAssetsWithMimeTypesAndRejectsUnknownOrEscapingPaths() throws Exception {
        Files.writeString(staticRoot.resolve("index.html"), "<!doctype html><html>flat-root</html>");
        Files.writeString(staticRoot.resolve("app.js"), "globalThis.flatAsset = true;");
        Files.writeString(staticRoot.resolve("styles.css"), "body { color: navy; }");
        MockMvc mvc = MockMvcBuilders.standaloneSetup(new StaticAssetController(staticRoot.toString()))
                .setControllerAdvice(new ApiExceptionHandler())
                .build();

        mvc.perform(get("/"))
                .andExpect(status().isOk())
                .andExpect(content().contentTypeCompatibleWith(MediaType.TEXT_HTML))
                .andExpect(content().string("<!doctype html><html>flat-root</html>"));
        mvc.perform(get("/assets/app.js"))
                .andExpect(status().isOk())
                .andExpect(header().string(HttpHeaders.CONTENT_TYPE, "text/javascript;charset=utf-8"))
                .andExpect(content().string("globalThis.flatAsset = true;"));
        mvc.perform(get("/assets/styles.css"))
                .andExpect(status().isOk())
                .andExpect(header().string(HttpHeaders.CONTENT_TYPE, "text/css;charset=utf-8"))
                .andExpect(content().string("body { color: navy; }"));
        mvc.perform(get("/assets/unlisted.js")).andExpect(status().isNotFound());

        Path outside = staticRoot.getParent().resolve("outside-" + UUID.randomUUID() + ".txt");
        try {
            Files.writeString(outside, "must not be served");
            mvc.perform(get("/assets/%2e%2e/" + outside.getFileName()))
                    .andExpect(status().is4xxClientError());
        } finally {
            Files.deleteIfExists(outside);
        }

        Path assets = Files.createDirectory(staticRoot.resolve("assets"));
        Files.writeString(assets.resolve("app.js"), "globalThis.standardAsset = true;");
        mvc.perform(get("/assets/app.js"))
                .andExpect(status().isOk())
                .andExpect(content().string("globalThis.standardAsset = true;"));
    }
}
