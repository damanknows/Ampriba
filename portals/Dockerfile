FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# These are the only project packages needed by the portal applications.
COPY shared /app/shared
COPY portals /app/portals

# The applications do not need root access or writable project files.
USER 10001:10001
