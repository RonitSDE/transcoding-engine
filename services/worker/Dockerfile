FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/transcode/services/worker

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py db.py settings.py ./

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]