FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Frontend is one directory up in the repo; copy it in too.
COPY ../frontend ./frontend
ENV PAPERREPO_FRONTEND=/app/frontend
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
