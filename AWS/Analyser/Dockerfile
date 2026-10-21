FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
    && useradd --create-home --uid 10001 analyser \
    && mkdir -p /data/alerts && chown analyser /data/alerts

USER analyser
ENV ANALYSER_API_HOST=0.0.0.0 \
    ANALYSER_API_PORT=8000 \
    PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://localhost:8000/health', timeout=3).status != 200)"]

CMD ["python", "-m", "analyser"]
