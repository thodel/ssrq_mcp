FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy server code
# embeddings.py is imported by search_semantic. Listing modules one by one
# keeps the image small and means a new one has to be added here too —
# hls_mcp shipped without its and the container crash-looped on the
# import, with a clean build and no warning.
COPY db.py server.py corpus.py embeddings.py ./

# ssrq.db is mounted at runtime (see docker-compose.yml). It is built by the SSRQ
# project's ETL from the RDF-TTL dump, not by this image.

EXPOSE 8002

# embeddings.py is vendored across the fleet and reads SSRQ_* settings by this prefix.
ENV MCP_ENV_PREFIX=SSRQ

CMD ["python", "server.py", "--db", "/data/ssrq.db", "--host", "0.0.0.0", "--port", "8002"]
