# FireCal: harmonized MODIS + VIIRS burning calendar
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY pipeline ./pipeline
# processed grids live on a volume so they survive redeploys; empty is fine
# (countries are downloaded and harmonized on first request)
ENV FIRECAL_DATA=/data
VOLUME /data
EXPOSE 8765
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers"]
