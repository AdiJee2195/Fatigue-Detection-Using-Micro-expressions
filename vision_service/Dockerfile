# Build context is the REPO ROOT (see docker-compose.yml) because VISION_MODE=onnx
# reuses xai_narrative/fatigue_xai/cv_adapter.py rather than duplicating softmax.
FROM python:3.11-slim
WORKDIR /app

# Stub mode needs only the web layer; the ML extras are what make the image big,
# so install them from the same file the local dev flow uses.
COPY vision_service/requirements.txt ./vision_service/requirements.txt
RUN pip install --no-cache-dir -r vision_service/requirements.txt

COPY vision_service ./vision_service
COPY xai_narrative/fatigue_xai ./xai_narrative/fatigue_xai

# Run unprivileged. This service decodes driver camera frames, so it is the last
# process that should be running as root; it writes nothing and binds 8000 (>1024).
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --no-create-home app \
    && chown -R app:app /app
USER app

ENV PYTHONPATH=/app/vision_service \
    XAI_NARRATIVE_PATH=/app/xai_narrative \
    VISION_MODE=stub \
    VISION_PORT=8000

EXPOSE 8000
# stdlib only: the slim image has no curl. Copied frames are never logged, and this
# probe reads only counters.
HEALTHCHECK --interval=10s --timeout=3s --retries=5 --start-period=15s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1
CMD ["python", "-m", "vision_service.app"]
