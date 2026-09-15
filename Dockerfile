FROM python:3.11-slim
ARG PIP_INDEX_URL
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=30 PIP_RETRIES=5
WORKDIR /app
COPY requirements.txt ./
RUN pip install --timeout 30 --retries 5 -r requirements.txt
COPY . .
RUN useradd --uid 10001 --no-create-home --home-dir /app appuser \
    && mkdir -p /app/usage /data && chown -R appuser:appuser /app /data
USER appuser
EXPOSE 50062
CMD ["python3", "-m", "spine.server"]
