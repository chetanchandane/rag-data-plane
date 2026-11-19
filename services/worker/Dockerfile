# Ingest worker Lambda image. Build from the repo root, for Lambda's x86_64 architecture:
#   docker build --platform linux/amd64 --provenance=false -f services/worker/Dockerfile -t ragdp-worker .
# --provenance=false: otherwise buildx wraps the image in an index with an attestation
# manifest, which Lambda refuses to deploy.

# Stage 1: turn uv.lock into a pinned, hashed requirements file (runtime dependencies only).
FROM ghcr.io/astral-sh/uv:0.11.29 AS uv
FROM public.ecr.aws/lambda/python:3.12 AS requirements
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --format requirements-txt -o /tmp/requirements.txt

# Stage 2: the image Lambda runs. --require-hashes: only the exact files in uv.lock install.
FROM public.ecr.aws/lambda/python:3.12
COPY --from=requirements /tmp/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY services/shared ${LAMBDA_TASK_ROOT}/shared
COPY services/worker ${LAMBDA_TASK_ROOT}/worker
CMD ["worker.handler.handler"]
