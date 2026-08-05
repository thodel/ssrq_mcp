FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY db.py server.py ./

EXPOSE 8002

CMD ["python", "server.py", "--db", "/data/ssrq.db", "--host", "0.0.0.0", "--port", "8002"]
