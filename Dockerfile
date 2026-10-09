# A phase: ECS on one x86_64 EC2 host, using that host's Docker daemon.
FROM public.ecr.aws/docker/library/docker:29-cli@sha256:1a4c7cb63513f349bdad01fcc6e0f3f2f67d37b9da86f14dc0d4a0942eecda00 AS docker-cli
FROM public.ecr.aws/aws-cli/aws-cli:latest@sha256:3dacc5db57c923c4223e949795f538ecf1f2212b2b7d5a028b47b97f91564c0d AS aws-cli

FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 AWS_PAGER=""
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git dnsutils \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=aws-cli /usr/local/aws-cli /usr/local/aws-cli
RUN ln -s /usr/local/aws-cli/v2/current/bin/aws /usr/local/bin/aws
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
# Root is needed for the host Docker socket in the internal A-phase deployment.
# The EC2 host is dedicated to Sky; do not expose this service unrestricted.
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"
ENTRYPOINT ["sky-service"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--state-dir", "/.sky"]

# Linux regression checks; never runs the live AWS/OpenAI test suite.
FROM runtime AS test
COPY tests ./tests
RUN pip install --no-cache-dir '.[dev]'
ENTRYPOINT ["pytest"]

# Keep a plain docker build pointed at the service image, not the test image.
FROM runtime AS service
