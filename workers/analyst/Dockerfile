FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY worker_api.py .
ENV PORT=8003
CMD ["uvicorn", "worker_api:app", "--host", "0.0.0.0", "--port", "8003"]
