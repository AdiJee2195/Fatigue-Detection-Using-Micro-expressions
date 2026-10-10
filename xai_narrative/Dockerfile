FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY fatigue_xai ./fatigue_xai
COPY rules.default.json ./
ENV PORT=8100 NARRATIVE_LLM=none OLLAMA_URL=http://ollama:11434
EXPOSE 8100
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8100/health')" || exit 1
CMD ["python", "-m", "fatigue_xai.api"]
