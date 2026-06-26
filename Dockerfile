FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY counters/ counters/

RUN pip install --no-cache-dir -e .

# Data lives on a mounted volume
ENV COUNTER_DATA_DIR=/data

# Flush stdout/stderr immediately so `docker compose logs` shows output live
ENV PYTHONUNBUFFERED=1

EXPOSE 8081

VOLUME ["/data"]

ENTRYPOINT ["counters"]
CMD ["server", "--host", "0.0.0.0", "--port", "8081"]
