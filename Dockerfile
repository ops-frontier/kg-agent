FROM python:3.12-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends git gh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/kg-collector

COPY pyproject.toml ./
COPY src/ ./src/

RUN python -m pip install --no-cache-dir .

ENV KG_OUTPUT_DIR=/knowledge
ENV PORT=8080
EXPOSE 8080

ENTRYPOINT ["python", "-m", "kg_collector.web"]
