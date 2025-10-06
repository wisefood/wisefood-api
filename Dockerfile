FROM python:3.11

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY ./src /app/src

WORKDIR /app/src
EXPOSE ${PORT:-8000}
CMD /bin/sh -c 'uvicorn main:api --host 0.0.0.0 --port ${PORT:-8000}'