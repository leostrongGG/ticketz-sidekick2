# Dockerfile
FROM postgres:16-alpine

RUN apk update && apk add --no-cache \
    tar \
    bash \
    findutils \
    python3

WORKDIR /app

COPY sidekick2.sh /app/sidekick2.sh
COPY ticketz-filter.py /app/ticketz-filter.py
COPY ticketz-import.py /app/ticketz-import.py

RUN chmod +x /app/sidekick2.sh

ENTRYPOINT ["bash", "/app/sidekick2.sh"]
