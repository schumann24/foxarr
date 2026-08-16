FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN mkdir -p /data && useradd --create-home --uid 10001 foxarr \
    && chown -R foxarr:foxarr /app /data
USER foxarr

ENV FOXARR_DATABASE=/data/foxarr.db
EXPOSE 7878

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os, urllib.parse, urllib.request; key=urllib.parse.quote(os.environ.get('FOXARR_API_KEY', ''), safe=''); urllib.request.urlopen('http://127.0.0.1:7878/api/v3/system/status?apikey=' + key, timeout=2).read()"

CMD ["uvicorn", "foxarr.app:app", "--host", "0.0.0.0", "--port", "7878"]
