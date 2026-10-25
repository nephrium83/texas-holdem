FROM python:3.13-slim@sha256:d879ed88565a7a3c614b980b369d4a5025a9d057b71f93d09fc6d0b2ca823a18

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY deploy/introduction/requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/requirements.lock
COPY holdem /app/holdem
COPY tools/run_introduction_service.py /app/tools/run_introduction_service.py
COPY LICENSE /app/LICENSE
USER 10001:10001
EXPOSE 8766/tcp
HEALTHCHECK --interval=30s --timeout=4s --start-period=5s --retries=3 CMD ["python", "-c", "import http.client; c=http.client.HTTPConnection('127.0.0.1',8766,timeout=2); c.request('GET','/healthz'); r=c.getresponse(); assert r.status==200; r.read(4096); c.close()"]
ENTRYPOINT ["python", "-m", "tools.run_introduction_service"]
CMD ["--service", "poker", "--host", "0.0.0.0", "--port", "8766"]
