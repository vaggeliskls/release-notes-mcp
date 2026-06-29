# --- build stage: install deps into a self-contained dir -------------------- #
# Match the Python minor version of the distroless runtime (debian12 → 3.11).
FROM python:3.11-slim AS build

WORKDIR /build
COPY requirements.txt .
# Install into /deps so we can copy just the packages into the distroless image.
RUN pip install --no-cache-dir --target=/deps -r requirements.txt

# --- runtime stage: distroless, no shell, no pip ---------------------------- #
FROM gcr.io/distroless/python3-debian12:nonroot

WORKDIR /app

# Third-party packages and the server code.
COPY --from=build /deps /deps
COPY server.py .

ENV PYTHONPATH=/deps \
    PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    RELEASE_MCP_CONFIG=/app/config.json

EXPOSE 8000

# distroless python3 image's entrypoint is already `python3`.
CMD ["server.py"]
