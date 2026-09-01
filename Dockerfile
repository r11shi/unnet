# Single-stage: the dashboard is one static file, so there is nothing to build.
FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY unnet ./unnet
RUN pip install --no-cache-dir -e .

COPY web ./web
COPY data ./data
COPY docs ./docs
COPY Makefile ./

# Generate the fixtures and run once at build time, so the container starts with
# a completed run to show rather than an empty database. Offline: the committed
# cassettes supply the model output, so no API key is needed to build or serve.
ENV UNNET_LLM_PROVIDER=offline
RUN python -m unnet.cli gen \
 && python -m unnet.cli gen --profile messy \
 && python -m unnet.cli recon

EXPOSE 8000
CMD ["sh", "-c", "python -m uvicorn unnet.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
