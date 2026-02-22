# Dockerfile
FROM postgres:16-alpine

RUN apk update && apk add --no-cache \
    tar \
    bash \
    findutils \
    python3

WORKDIR /app

COPY sidekick.sh /app/sidekick.sh
COPY ticketz-filter.py /app/ticketz-filter.py
COPY retrieve-tables.sh /app/retrieve-tables.sh

RUN chmod +x /app/sidekick.sh

ENTRYPOINT ["bash", "/app/sidekick.sh"]
