# FinTS-Service als Container (Cloud Run / Fly.io / Render / Railway)
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY db_fints_service.py .

# Cloud Run/Fly setzen $PORT; Default 8080
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn db_fints_service:app --host 0.0.0.0 --port ${PORT}"]
