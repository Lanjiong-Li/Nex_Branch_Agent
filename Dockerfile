FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
COPY branch_agent ./branch_agent
COPY docs ./docs
COPY migrations ./migrations
RUN pip install --no-cache-dir -e .
ENV BRANCH_DATA_DIR=/data BRANCH_EMBEDDED_WORKER=1
EXPOSE 8767
CMD ["python", "-m", "uvicorn", "branch_agent.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8767", "--proxy-headers", "--forwarded-allow-ips=*"]
