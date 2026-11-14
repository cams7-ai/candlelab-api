FROM python:3.12.14-slim-bookworm
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml README.md ./
COPY candlelab ./candlelab
RUN useradd --create-home --uid 10001 candlelab
USER candlelab
EXPOSE 8000
CMD ["uvicorn", "candlelab.api:app", "--host", "0.0.0.0", "--port", "8000"]
