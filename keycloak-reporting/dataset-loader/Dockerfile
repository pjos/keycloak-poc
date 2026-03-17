FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir requests urllib3

COPY loader.py .

# Ensure Python output is not buffered (live logs in docker)
ENV PYTHONUNBUFFERED=1

# Handle SIGTERM gracefully (docker stop sends SIGTERM)
STOPSIGNAL SIGTERM

CMD ["python", "loader.py"]

