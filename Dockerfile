FROM python:3.12-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir .

ENTRYPOINT ["python", "-m", "aiomoqt.examples.moq_interop_client"]
