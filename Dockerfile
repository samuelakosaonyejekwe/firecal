# FireCal (server edition): harmonized MODIS + VIIRS burning activity calendar.
#   docker build -t firecal .
#   docker run -p 8765:8765 -v firecal-data:/data firecal      -> http://127.0.0.1:8765
# Countries are fetched on demand into the /data volume. Inside the container there is no `gh`
# login, so it works locally only (no publishing to GitHub).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 FIRECAL_DATA=/data FIRECAL_NO_GITHUB=1
RUN useradd --create-home --uid 1000 firecal && mkdir -p /data && chown firecal:firecal /data
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY pipeline ./pipeline

USER firecal
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers"]
