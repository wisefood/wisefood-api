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

# The retention CronJob runs out of here. Without this the job's image has no
# script to run, and the failure only shows up on the first scheduled night.
COPY ./scripts /app/scripts

WORKDIR /app/src
EXPOSE ${PORT:-8000}

ENTRYPOINT ["/app/entrypoint.sh"]
