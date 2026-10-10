# Build stage: resolve dependencies first so source changes do not re-download them.
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /build
COPY pom.xml .
RUN mvn -B -q dependency:go-offline
COPY src ./src
RUN mvn -B -q clean package -DskipTests

FROM eclipse-temurin:17-jre
WORKDIR /app

# curl for the container healthcheck. The base image ships neither curl nor wget,
# so a healthcheck written against either one would fail on a healthy container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Run unprivileged. The gateway only reads its jar and binds 8080 (>1024), so it
# needs no special capabilities — and a service that inspects camera frames should
# not be running as root.
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --no-create-home app

COPY --from=build /build/target/gateway-0.1.0-SNAPSHOT.jar app.jar
RUN chown app:app /app/app.jar
USER app

# Container-internal defaults; docker-compose overrides these with service names.
ENV VISION_SERVICE_URL=http://vision:8000 \
    NARRATIVE_WS_URL=ws://narrative-xai:8100 \
    GATEWAY_ALLOWED_ORIGINS=http://localhost:5173,http://localhost:3000

EXPOSE 8080

# A JVM takes a few seconds to start; start-period keeps the container from being
# reported unhealthy while it is still booting.
HEALTHCHECK --interval=10s --timeout=3s --retries=5 --start-period=25s \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

ENTRYPOINT ["java", "-jar", "/app/app.jar"]
