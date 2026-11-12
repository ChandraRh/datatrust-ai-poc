# DataTrust AI web app (dashboard + AI agent + Data Estate portal)
# Build:  docker build -t datatrust-web .
# Run:    docker run -p 8000:8000 --env-file .env -e PG_HOST=<db host> datatrust-web
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8000

WORKDIR /app
COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

# optional: Microsoft ODBC driver, only needed for SQL Server sources in the Data Estate portal
#   docker build --build-arg WITH_MSSQL=true -t datatrust-web .
ARG WITH_MSSQL=false
RUN if [ "$WITH_MSSQL" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates && \
      curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg && \
      curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list -o /etc/apt/sources.list.d/mssql-release.list && \
      apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc && \
      pip install --no-cache-dir "pyodbc>=5.1" && \
      apt-get purge -y curl gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*; \
    fi

# application code only: config.py, dq_rules.py + web/ (no data, no .env, no secrets)
COPY config.py .
COPY batch_status.py .
COPY dq_rules.py .
COPY llm_gemini.py .
COPY web/ web/

# Catch missing files and imports during the image build, rather than at container startup.
RUN python -c "import sys; sys.path.insert(0, '/app/web'); import alerts, agent, appdb, autofix, batch_status, briefing, data_service, dq, dq_routes, schema_guard; from estate import drafting, routes, service, source_types, store; from adf import client, collector, diagnose, insights, probes, resolve, routes, store"

# run as a non-root user
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\") or os.getenv(\"WEB_PORT\",\"8000\")}/health', timeout=4)"
CMD ["python", "web/server.py"]
