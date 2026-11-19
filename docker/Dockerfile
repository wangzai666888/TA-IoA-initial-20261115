FROM python:3.11-slim
ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CO_IOA_RUNS_DIR=/app/runs/api
COPY requirements.lock.txt /app/requirements.lock.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --timeout 120 --retries 5 -r requirements.lock.txt
COPY pyproject.toml LICENSE /app/
COPY co_ioa /app/co_ioa
COPY common /app/common
COPY im_client /app/im_client
COPY im_server/*.py /app/im_server/
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --timeout 120 --retries 5 --no-deps .
EXPOSE 8000
CMD ["python", "-m", "co_ioa.api", "--host", "0.0.0.0", "--port", "8000", "--output", "/app/runs/api"]
