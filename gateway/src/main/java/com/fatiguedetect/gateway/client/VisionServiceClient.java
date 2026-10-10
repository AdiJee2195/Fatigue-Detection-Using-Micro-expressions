package com.fatiguedetect.gateway.client;

import com.fatiguedetect.gateway.dto.FrameRequest;
import com.fatiguedetect.gateway.dto.InferenceResult;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Mono;

import java.time.Duration;

/**
 * Non-blocking HTTP client for the Python vision service.
 * Uses WebFlux WebClient (never RestTemplate) so no thread is blocked
 * while waiting for the 3D-CNN/TimeSformer inference to complete.
 */
@Slf4j
@Component
public class VisionServiceClient {

    private final WebClient webClient;
    private final Duration timeout;

    public VisionServiceClient(WebClient.Builder builder,
                               @Value("${vision.service.url}") String baseUrl,
                               @Value("${vision.service.timeout-seconds:2}") int timeoutSeconds) {
        this.webClient = builder.baseUrl(baseUrl).build();
        this.timeout = Duration.ofSeconds(timeoutSeconds);
    }

    /**
     * Sends a frame to the vision service for inference.
     * Returns a Mono that completes with the inference result or errors on timeout/5xx.
     * The caller should .subscribe() — never .block().
     */
    public Mono<InferenceResult> analyzeFrame(FrameRequest request) {
        return webClient.post()
                .uri("/api/v1/analyze")
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue(request)
                .retrieve()
                .bodyToMono(InferenceResult.class)
                .timeout(timeout)
                .doOnError(e -> log.warn("vision_service_error error={} class={}",
                        e.getMessage(), e.getClass().getSimpleName()));
    }
}
