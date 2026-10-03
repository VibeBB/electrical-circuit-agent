# Repackage layer applied to the SDK-built circuit-server image before the
# digest is locked. Nothing invokes pip at runtime (dependencies are
# uv-installed at image build), but the shipped pip payload vendors
# urllib3/msgpack/setuptools copies the Trivy gate flags. Strip it, plus the
# dead uv cache that would keep scanning stale package copies.
ARG BASE_IMAGE=ghcr.io/vibebb/circuit-server:latest
FROM ${BASE_IMAGE}
USER root
RUN rm -rf \
        /agent-server/uv-managed-python/cpython-*/bin/pip* \
        /agent-server/uv-managed-python/cpython-*/lib/python3*/site-packages/pip \
        /agent-server/uv-managed-python/cpython-*/lib/python3*/site-packages/pip-*.dist-info \
        /agent-server/uv-managed-python/cpython-*/lib/python3*/ensurepip \
        /agent-server/.venv/bin/pip* \
        /agent-server/.venv/lib/python3*/site-packages/pip \
        /agent-server/.venv/lib/python3*/site-packages/pip-*.dist-info \
        /root/.cache/uv
USER openhands
