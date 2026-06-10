FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml ./
COPY app ./app
RUN uv pip install --system --no-cache ".[dev]"

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
