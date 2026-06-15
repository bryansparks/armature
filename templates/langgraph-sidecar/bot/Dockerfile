FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt && \
    useradd --no-create-home --shell /bin/false appuser && \
    chown -R appuser /app
COPY . .
USER appuser
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
