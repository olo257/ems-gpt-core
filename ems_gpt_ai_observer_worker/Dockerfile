FROM python:3.13-alpine
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY worker.py /app/worker.py
RUN mkdir -p /data
VOLUME ["/data"]
CMD ["python", "/app/worker.py"]
