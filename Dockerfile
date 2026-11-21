# One image for the API and the watcher (different commands). Research tool, personal use.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
# CPU-only torch first: sentence-transformers would otherwise pull the multi-GB CUDA build.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
COPY pyproject.toml ./
COPY src ./src
COPY config ./config
COPY alembic ./alembic
COPY alembic.ini ./
# Editable install: the code finds config/ and rule files relative to the source tree.
RUN pip install -e .
# 0.0.0.0 is only the container's own interface. Never publish this port to the internet:
# the API has no login and POST /agents/{id}/rerun changes state.
ENV IER_API_HOST=0.0.0.0 IER_API_PORT=8000
EXPOSE 8000
USER nobody
CMD ["python", "-m", "ier.api"]
# Watcher:  python -m ier.runtime watch      Migrations:  python -m alembic upgrade head
