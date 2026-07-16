FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY counters2/ counters2/

RUN pip install --no-cache-dir -e .

# Data lives on a mounted volume
ENV COUNTER_DATA_DIR=/data

# Flush stdout/stderr immediately so `docker compose logs` shows output live
ENV PYTHONUNBUFFERED=1

# Deployed revision, surfaced by the server on /status (and the explorer
# footer). The CI pipeline passes the short git hash; defaults to "dev".
ARG GIT_COMMIT=dev
ENV COUNTER_GIT_COMMIT=$GIT_COMMIT

EXPOSE 8081

VOLUME ["/data"]

ENTRYPOINT ["counters"]
CMD ["server", "--host", "0.0.0.0", "--port", "8081"]
