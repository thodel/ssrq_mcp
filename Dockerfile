FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy server code
COPY db.py server.py ./

# ssrq.db is mounted at runtime (see docker-compose.yml). It is built by the SSRQ
# project's ETL from the RDF-TTL dump, not by this image.

EXPOSE 8002

CMD ["python", "server.py", "--db", "/data/ssrq.db", "--host", "0.0.0.0", "--port", "8002"]
