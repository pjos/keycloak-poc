FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir requests

COPY setup_dashboard.py .

ENV PYTHONUNBUFFERED=1

CMD ["python", "setup_dashboard.py"]

