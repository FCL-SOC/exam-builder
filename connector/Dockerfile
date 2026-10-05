# Build from the repository root (the connector reads docs/, examples/ and schema/):
#   docker build -f connector/Dockerfile -t exam-connector .
#   docker run -p 8000:8000 -v exam-links:/data -e PUBLIC_URL=http://localhost:8000 exam-connector
FROM python:3.12-slim

WORKDIR /app
COPY connector/requirements.txt connector/
RUN pip install --no-cache-dir -r connector/requirements.txt

COPY schema/ schema/
COPY docs/exam-format.md docs/question-style-guide.md docs/
COPY examples/ examples/
COPY connector/*.py connector/

# Links live in /data: mount a persistent volume there, or links stop working when the container restarts.
ENV PORT=8000 DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request, os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"
CMD ["python", "connector/server.py"]
