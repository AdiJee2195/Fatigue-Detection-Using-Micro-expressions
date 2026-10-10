package com.fatiguedetect.gateway.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * CORS for the gateway's read-only REST endpoints ({@code /health}, {@code /sessions}).
 *
 * The WebSocket handshake has its own origin check in {@link WebSocketConfig}
 * ({@code setAllowedOrigins}); this only covers plain XHR/fetch from the dashboard,
 * which lives on a different port during development.
 *
 * No reactive server is introduced here — Spring MVC already serves these.
 */
@Configuration
public class WebConfig implements WebMvcConfigurer {

    private final String[] allowedOrigins;

    public WebConfig(@Value("${gateway.allowed-origins:http://localhost:5173,http://localhost:3000}")
                     String allowedOrigins) {
        this.allowedOrigins = allowedOrigins.split("\\s*,\\s*");
    }

    @Override
    public void addCorsMappings(CorsRegistry registry) {
        registry.addMapping("/health")
                .allowedOrigins(allowedOrigins)
                .allowedMethods("GET", "OPTIONS")
                .maxAge(60);
        registry.addMapping("/sessions")
                .allowedOrigins(allowedOrigins)
                .allowedMethods("GET", "OPTIONS")
                .maxAge(60);
    }
}
