FROM ghcr.io/astral-sh/uv:0.12.17 AS uv

FROM ubuntu:26.04

ARG DEBIAN_FRONTEND=noninteractive
ARG KICAD_NIGHTLY_VERSION=202609190245+6d837080a5~189~ubuntu26.04.1
ARG KICAD_NIGHTLY_FOOTPRINTS_VERSION=202609171319+1df46f29b~14~ubuntu26.04.1
ARG KICAD_NIGHTLY_SYMBOLS_VERSION=202609181937+a82391d3d~12~ubuntu26.04.1
ARG KICAD_NIGHTLY_DEB_URL=https://launchpad.net/~kicad/+archive/ubuntu/kicad-dev-nightly/+files/kicad-nightly_202609190245+6d837080a5~189~ubuntu26.04.1_amd64.deb
ARG KICAD_NIGHTLY_DEB_SHA256=0300ee4330d7cb07800830cb7196155cca5385aa77b65234e42158b7b701e218
ARG KONNECT_VERSION=0.12.1
ARG KONNECT_SHA256=8a546fc949d11edbb55096a9b1f2c8f9147b26a9916b47a5c4990ee9e9441fb6
ARG KONNECT_COMMIT=fa62e1ccb9eba359519bf8e3eab53a6cffeee33c
ARG CERN_COMMIT=unknown
ARG IMAGE_REVISION=unknown

ENV DEBIAN_FRONTEND=noninteractive
ENV PATH=/usr/lib/kicad-nightly/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ENV LD_LIBRARY_PATH=/usr/lib/kicad-nightly/lib/x86_64-linux-gnu

LABEL org.opencontainers.image.source="https://github.com/VibeBB/electrical-circuit-agent" \
      org.opencontainers.image.licenses="BSD-3-Clause" \
      org.opencontainers.image.revision="${IMAGE_REVISION}" \
      circuit.konnect.version="${KONNECT_VERSION}" \
      circuit.konnect.commit="${KONNECT_COMMIT}" \
      circuit.cern.commit="${CERN_COMMIT}" \
      circuit.kicad.nightly="${KICAD_NIGHTLY_VERSION}"

COPY --from=uv /uv /uvx /usr/local/bin/

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        curl \
        git \
        python3 \
        software-properties-common \
        xz-utils \
    && add-apt-repository --yes ppa:kicad/kicad-dev-nightly \
    && apt-get update \
    && curl --fail --location --silent --show-error \
        --output /tmp/kicad-nightly.deb \
        "${KICAD_NIGHTLY_DEB_URL}" \
    && echo "${KICAD_NIGHTLY_DEB_SHA256}  /tmp/kicad-nightly.deb" | sha256sum --check \
    && apt-get install --no-install-recommends -y \
        "kicad-nightly-footprints=${KICAD_NIGHTLY_FOOTPRINTS_VERSION}" \
        "kicad-nightly-symbols=${KICAD_NIGHTLY_SYMBOLS_VERSION}" \
    && cd /tmp \
    && apt-get install --no-install-recommends -y ./kicad-nightly.deb \
    && rm -f /tmp/kicad-nightly.deb \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/circuit/bin /opt/circuit/libraries \
    && curl --fail --location --silent --show-error \
        --output /tmp/konnect.tar.gz \
        "https://github.com/mixelpixx/Konnect/releases/download/v${KONNECT_VERSION}/konnect-v${KONNECT_VERSION}-x86_64-unknown-linux-gnu.tar.gz" \
    && echo "${KONNECT_SHA256}  /tmp/konnect.tar.gz" | sha256sum --check \
    && tar -xzf /tmp/konnect.tar.gz -C /tmp \
    && install -m 0755 /tmp/konnect /usr/local/bin/konnect \
    && rm -f /tmp/konnect.tar.gz /tmp/konnect \
    && mkdir -p /usr/share/doc/konnect \
    && curl --fail --location --silent --show-error \
        --output /usr/share/doc/konnect/LICENSE \
        "https://raw.githubusercontent.com/mixelpixx/Konnect/${KONNECT_COMMIT}/LICENSE" \
    && printf '%s\n' \
        "source=https://github.com/mixelpixx/Konnect" \
        "commit=${KONNECT_COMMIT}" \
        "version=v${KONNECT_VERSION}" \
        > /usr/share/doc/konnect/SOURCE

COPY libraries/cern-kicad-libs /opt/circuit/libraries/cern-kicad-libs
RUN printf '%s\n' "${CERN_COMMIT}" > /opt/circuit/libraries/cern-kicad-libs.commit
COPY scripts/smoke_kicad11_konnect.py /opt/circuit/bin/smoke_kicad11_konnect.py
COPY scripts/konnect_client.py /opt/circuit/bin/konnect_client.py
COPY scripts/e2e_authoring.py /opt/circuit/bin/e2e_authoring.py
COPY pyproject.toml uv.lock /opt/circuit/
COPY src /opt/circuit/src
COPY plugins/circuit /opt/circuit/plugins/circuit

RUN if [ -f /usr/share/doc/kicad-nightly-symbols/LICENSE.md ]; then \
        cp /usr/share/doc/kicad-nightly-symbols/LICENSE.md \
          /opt/circuit/libraries/kicad-official-LICENSE.md; \
    else \
        curl --fail --location --silent --show-error \
          --output /opt/circuit/libraries/kicad-official-LICENSE.md \
          https://gitlab.com/kicad/libraries/kicad-symbols/-/raw/master/LICENSE.md; \
    fi \
    && if ! getent group circuit >/dev/null; then groupadd circuit; fi \
    && if getent passwd 1000 >/dev/null; then \
         existing="$(getent passwd 1000 | cut -d: -f1)"; \
         if [ "$existing" != circuit ]; then usermod --login circuit "$existing"; fi; \
         usermod --home /home/circuit --gid circuit circuit; \
       else \
         useradd --uid 1000 --gid circuit --create-home --shell /bin/bash circuit; \
       fi \
    && mkdir -p /home/circuit/.config/kicad /home/circuit/.cache/kicad \
    && chown -R circuit:circuit /home/circuit \
    && cd /opt/circuit \
    && uv export --frozen --no-dev --no-emit-project --format requirements-txt \
        --output-file /tmp/circuit-requirements.txt \
    && uv pip install --system --break-system-packages \
        --requirement /tmp/circuit-requirements.txt \
    && uv pip install --system --break-system-packages --no-deps /opt/circuit \
    && kicad-cli --version | grep -E '^10\.99\.' \
    && kicad-cli api-server --help \
    && konnect --version | grep -F "${KONNECT_VERSION}" \
    && python3 -c "import circuit, mcp, pydantic; print(circuit.__version__)" \
    && python3 -m circuit.doctor --warn \
    && rm -f /tmp/circuit-requirements.txt \
    && chown -R circuit:circuit /home/circuit

WORKDIR /opt/circuit
