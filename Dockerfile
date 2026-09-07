FROM python:3.12-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends git gh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/kg-collector

COPY pyproject.toml ./
COPY src/ ./src/

RUN python -m pip install --no-cache-dir .

ENV PORT=8081
ENV NEO4J_URI=bolt://neo4j:7687
ENV NEO4J_USER=neo4j
ENV NEO4J_PASSWORD=kgpassword
EXPOSE 8081

ENTRYPOINT ["python", "-m", "kg_collector.web"]
