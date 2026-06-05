FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py config.yaml ./
COPY templates ./templates
COPY static ./static
COPY sql ./sql

EXPOSE 8090

CMD ["python", "main.py"]
