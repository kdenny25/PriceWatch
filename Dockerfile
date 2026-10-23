FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PRICEWATCH_DB=/data/pricewatch.db
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd -m pw && mkdir /data && chown pw /data
USER pw
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
