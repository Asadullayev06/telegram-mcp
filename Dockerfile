# Use an official Python runtime as a parent image (Alpine-based for minimal vulnerabilities)
FROM python:3.13-alpine

# Set the working directory in the container
WORKDIR /app

# Prevent Python from writing pyc files to disc
ENV PYTHONDONTWRITEBYTECODE=1
# Ensure Python output is sent straight to terminal (useful for logs)
ENV PYTHONUNBUFFERED=1

# Install system dependencies if needed (e.g., for certain Python packages)
# RUN apt-get update && apt-get install -y --no-install-recommends some-package && rm -rf /var/lib/apt/lists/*

# Copy dependency definition files
# If using Poetry:
# COPY pyproject.toml poetry.lock* ./
# RUN pip install --no-cache-dir poetry
# RUN poetry config virtualenvs.create false && poetry install --no-dev --no-interaction --no-ansi
# If using pip with requirements.txt:
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY main.py sanitize.py ./
COPY telegram_mcp ./telegram_mcp
# COPY session_string_generator.py . # Optional: if needed within the container, otherwise can be run outside

# Create a non-root user and switch to it
RUN adduser --disabled-password --gecos "" appuser && chown -R appuser:appuser /app
USER appuser

# Define environment variables needed by the application
# These should be provided at runtime, not hardcoded (especially secrets)
ENV TELEGRAM_API_ID=""
ENV TELEGRAM_API_HASH=""
# Provide the session string at runtime (required for remote/HTTP deployments):
ENV TELEGRAM_SESSION_STRING=""

# Force SSE transport in containerized deployments. Railway/other PaaS will
# inject PORT; HOST defaults to 0.0.0.0 so the server binds publicly.
ENV MCP_TRANSPORT="sse"
ENV PORT="8000"
ENV HOST="0.0.0.0"

# Remote MCP clients don't negotiate Roots, so file-path tools
# (download_media, upload_file) need a default writable directory.
ENV MCP_DEFAULT_ROOT="/tmp/downloads"

EXPOSE 8000

# Define the command to run the application
CMD ["python", "main.py"]
