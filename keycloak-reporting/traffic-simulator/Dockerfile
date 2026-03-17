FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir requests urllib3

COPY simulator.py .

ENV PYTHONUNBUFFERED=1

STOPSIGNAL SIGTERM

CMD ["python", "simulator.py"]

