FROM python:3.11

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl build-essential git postgresql-client && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY ./src /app/src

COPY ./entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

COPY ./schemas /app/schemas

WORKDIR /app/src
EXPOSE ${PORT:-8000}

ENTRYPOINT ["/app/entrypoint.sh"]
