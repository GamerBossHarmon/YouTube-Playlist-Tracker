# Debian's apt only ships Node 20, Yt-dlp needs 22+, so
# download then copy the binary from the official Node image.
FROM node:24-slim AS node

FROM python:3.12-slim

COPY --from=node /usr/local/bin/node /usr/local/bin/node

ENV PYTHONUNBUFFERED=1 \
	PYTHONDONTWRITEBYTECODE=1 \
	PIP_NO_CACHE_DIR=1 \
	PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends eatmydata \
	&& eatmydata apt-get install -y \
	ffmpeg \
	&& apt-get purge -y --auto-remove eatmydata \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY core/ core/
COPY cli/ cli/
COPY api/ api/
COPY migrations/ migrations/
COPY alembic.ini ytpt.py ./

RUN mkdir -p config data
VOLUME ["/app/config", "/app/data"]

EXPOSE 8000

ENTRYPOINT ["python", "ytpt.py"]
CMD ["run"]
