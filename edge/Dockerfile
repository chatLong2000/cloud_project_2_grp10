FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY edge.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

VOLUME /data
ENTRYPOINT ["./entrypoint.sh"]
