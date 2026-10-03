FROM ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 AS uv

FROM ubuntu:26.04@sha256:da6fc2be547864451aa253836dd926da33623312df4a9a243e35dc877c378a78

ARG DEBIAN_FRONTEND=noninteractive
ARG KICAD_NIGHTLY_VERSION=202609302019+55110814ee~189~ubuntu26.04.1
ARG KICAD_NIGHTLY_FOOTPRINTS_VERSION=202609302018+9326b9efd~14~ubuntu26.04.1
ARG KICAD_NIGHTLY_SYMBOLS_VERSION=202609271717+716edc43f~12~ubuntu26.04.1
ARG KICAD_NIGHTLY_DEB_URL=https://launchpad.net/~kicad/+archive/ubuntu/kicad-dev-nightly/+files/kicad-nightly_202609302019+55110814ee~189~ubuntu26.04.1_amd64.deb
ARG KICAD_NIGHTLY_DEB_SHA256=63e5e2b3b8a2b627e8a4a0b0c5fc33b0ac875cab1c796e3ef806df27cc620c8e
ARG KICAD_NIGHTLY_SYMBOLS_DEB_URL=https://launchpad.net/~kicad/+archive/ubuntu/kicad-dev-nightly/+files/kicad-nightly-symbols_202609271717+716edc43f~12~ubuntu26.04.1_all.deb
ARG KICAD_NIGHTLY_SYMBOLS_DEB_SHA256=4c3c894591a0f7d95db7627dae0941dec483722a5629eacb5200e9c99cf44295
ARG KICAD_NIGHTLY_FOOTPRINTS_DEB_URL=https://launchpad.net/~kicad/+archive/ubuntu/kicad-dev-nightly/+files/kicad-nightly-footprints_202609302018+9326b9efd~14~ubuntu26.04.1_all.deb
ARG KICAD_NIGHTLY_FOOTPRINTS_DEB_SHA256=2c044ca4df15de79fcc96a9e1518ed978208d4aa0f397d07574ff88a5da2ee7d
ARG KONNECT_VERSION=0.12.1
ARG KONNECT_SHA256=8a546fc949d11edbb55096a9b1f2c8f9147b26a9916b47a5c4990ee9e9441fb6
ARG KONNECT_COMMIT=fa62e1ccb9eba359519bf8e3eab53a6cffeee33c
ARG SEMERU_JRE_VERSION=27.0.0.0
ARG SEMERU_JRE_SHA256=9e6d9c1131da124bd08eb4183f7787a9f90111fc3d62c1231976c2d37372d59e
ARG FREEROUTING_VERSION=2.4.1
ARG FREEROUTING_SHA256=251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9
ARG KICAD_LIBRARY_UTILS_COMMIT=90b0af91eaffcd91552027c3bfd166896f78c7de
ARG CERN_COMMIT=unknown
ARG IMAGE_REVISION=unknown

# Fail the build when the left side of a verification pipe (curl|sha256sum)
# breaks instead of silently passing the right side.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV DEBIAN_FRONTEND=noninteractive
ENV JAVA_HOME=/opt/jre
ENV PATH=/opt/jre/bin:/usr/lib/kicad-nightly/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ENV LD_LIBRARY_PATH=/usr/lib/kicad-nightly/lib/x86_64-linux-gnu

LABEL org.opencontainers.image.source="https://github.com/VibeBB/electrical-circuit-agent" \
      org.opencontainers.image.licenses="BSD-3-Clause" \
      org.opencontainers.image.revision="${IMAGE_REVISION}" \
      circuit.konnect.version="${KONNECT_VERSION}" \
      circuit.konnect.commit="${KONNECT_COMMIT}" \
      circuit.semeru.version="${SEMERU_JRE_VERSION}" \
      circuit.freerouting.version="${FREEROUTING_VERSION}" \
      circuit.klc.commit="${KICAD_LIBRARY_UTILS_COMMIT}" \
      circuit.cern.commit="${CERN_COMMIT}" \
      circuit.kicad.nightly="${KICAD_NIGHTLY_VERSION}"

COPY --from=uv /uv /uvx /usr/local/bin/

# Serve every suite from the master archive: it carries the same -security
# pocket, while security.ubuntu.com can briefly publish an index ahead of its
# pool and 404 packages the index still lists.
RUN grep -q "^URIs: http://security\.ubuntu\.com/ubuntu" \
        /etc/apt/sources.list.d/ubuntu.sources \
    && sed -i "s|^URIs: http://security\.ubuntu\.com/ubuntu/|URIs: http://archive.ubuntu.com/ubuntu/|" \
        /etc/apt/sources.list.d/ubuntu.sources \
    && apt_install_retry() { \
        for attempt in 1 2 3 4 5; do \
            apt-get -o Acquire::Retries=5 update \
            && apt-get -o Acquire::Retries=5 install --no-install-recommends -y "$@" \
            && return 0; \
            [ "$attempt" = 5 ] && return 1; \
            sleep $((attempt * 10)); \
        done; \
    } \
    && apt_install_retry \
        ca-certificates \
        curl \
        git \
        librsvg2-bin \
        poppler-utils \
        python3 \
        software-properties-common \
        xz-utils \
    && for attempt in 1 2 3 4 5; do \
        add-apt-repository --yes ppa:kicad/kicad-dev-nightly && break; \
        [ "$attempt" = 5 ] && exit 1; \
        sleep $((attempt * 10)); \
       done \
    && apt-get -o Acquire::Retries=5 update \
    && curl --fail --location --silent --show-error \
        --retry 5 --retry-delay 10 --retry-all-errors \
        --output /tmp/kicad-nightly.deb \
        "${KICAD_NIGHTLY_DEB_URL}" \
    && echo "${KICAD_NIGHTLY_DEB_SHA256}  /tmp/kicad-nightly.deb" | sha256sum --check \
    && curl --fail --location --silent --show-error \
        --retry 5 --retry-delay 10 --retry-all-errors \
        --output /tmp/kicad-nightly-symbols.deb \
        "${KICAD_NIGHTLY_SYMBOLS_DEB_URL}" \
    && echo "${KICAD_NIGHTLY_SYMBOLS_DEB_SHA256}  /tmp/kicad-nightly-symbols.deb" | sha256sum --check \
    && curl --fail --location --silent --show-error \
        --retry 5 --retry-delay 10 --retry-all-errors \
        --output /tmp/kicad-nightly-footprints.deb \
        "${KICAD_NIGHTLY_FOOTPRINTS_DEB_URL}" \
    && echo "${KICAD_NIGHTLY_FOOTPRINTS_DEB_SHA256}  /tmp/kicad-nightly-footprints.deb" | sha256sum --check \
    && apt_install_retry \
        /tmp/kicad-nightly.deb \
        /tmp/kicad-nightly-symbols.deb \
        /tmp/kicad-nightly-footprints.deb \
    && rm -f /tmp/kicad-nightly*.deb \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/circuit/bin /opt/circuit/libraries \
    && curl --fail --location --silent --show-error \
        --retry 5 --retry-delay 10 --retry-all-errors \
        --output /tmp/konnect.tar.gz \
        "https://github.com/mixelpixx/Konnect/releases/download/v${KONNECT_VERSION}/konnect-v${KONNECT_VERSION}-x86_64-unknown-linux-gnu.tar.gz" \
    && echo "${KONNECT_SHA256}  /tmp/konnect.tar.gz" | sha256sum --check \
    && tar -xzf /tmp/konnect.tar.gz -C /tmp \
    && install -m 0755 /tmp/konnect /usr/local/bin/konnect \
    && rm -f /tmp/konnect.tar.gz /tmp/konnect \
    && mkdir -p /usr/share/doc/konnect \
    && curl --fail --location --silent --show-error \
        --retry 5 --retry-delay 10 --retry-all-errors \
        --output /usr/share/doc/konnect/LICENSE \
        "https://raw.githubusercontent.com/mixelpixx/Konnect/${KONNECT_COMMIT}/LICENSE" \
    && printf '%s\n' \
        "source=https://github.com/mixelpixx/Konnect" \
        "commit=${KONNECT_COMMIT}" \
        "version=v${KONNECT_VERSION}" \
        > /usr/share/doc/konnect/SOURCE

RUN mkdir -p /opt/jre /opt/freerouting \
    && curl --fail --location --silent --show-error \
        --retry 5 --retry-delay 10 --retry-all-errors \
        --output /tmp/semeru-jre.tar.gz \
        "https://github.com/ibmruntimes/semeru27-binaries/releases/download/jdk-${SEMERU_JRE_VERSION}/ibm-semeru-open-jre_x64_linux_${SEMERU_JRE_VERSION}.tar.gz" \
    && echo "${SEMERU_JRE_SHA256}  /tmp/semeru-jre.tar.gz" | sha256sum --check \
    && tar -xzf /tmp/semeru-jre.tar.gz -C /opt/jre --strip-components=1 \
    && rm -f /tmp/semeru-jre.tar.gz \
    && command -v java | grep -E '^/opt/jre/bin/java$' \
    && java -version 2>&1 | grep -F 'Eclipse OpenJ9 VM' >/dev/null \
    && java -version 2>&1 | grep -F "IBM Semeru Runtime Open Edition ${SEMERU_JRE_VERSION}" \
    && curl --fail --location --silent --show-error \
        --retry 5 --retry-delay 10 --retry-all-errors \
        --output /opt/freerouting/freerouting.jar \
        "https://github.com/freerouting/freerouting/releases/download/v${FREEROUTING_VERSION}/freerouting-${FREEROUTING_VERSION}.jar" \
    && echo "${FREEROUTING_SHA256}  /opt/freerouting/freerouting.jar" | sha256sum --check \
    && java -Djava.awt.headless=true -jar /opt/freerouting/freerouting.jar --version 2>&1 \
        | grep -F "Freerouting v${FREEROUTING_VERSION}" \
    && mkdir -p /usr/share/doc/freerouting /usr/share/doc/semeru-jre \
    && curl --fail --location --silent --show-error \
        --retry 5 --retry-delay 10 --retry-all-errors \
        --output /usr/share/doc/freerouting/LICENSE \
        "https://raw.githubusercontent.com/freerouting/freerouting/v${FREEROUTING_VERSION}/LICENSE" \
    && printf '%s\n' \
        "source=https://github.com/freerouting/freerouting" \
        "version=v${FREEROUTING_VERSION}" \
        > /usr/share/doc/freerouting/SOURCE \
    && printf '%s\n' \
        "source=https://github.com/ibmruntimes/semeru27-binaries" \
        "version=jdk-${SEMERU_JRE_VERSION}" \
        > /usr/share/doc/semeru-jre/SOURCE

RUN mkdir -p /opt/kicad-library-utils /usr/share/doc/kicad-library-utils \
    && git -C /opt/kicad-library-utils init \
    && git -C /opt/kicad-library-utils fetch --depth 1 \
        https://gitlab.com/kicad/libraries/kicad-library-utils.git \
        "${KICAD_LIBRARY_UTILS_COMMIT}" \
    && git -C /opt/kicad-library-utils checkout FETCH_HEAD \
    && test "$(git -C /opt/kicad-library-utils rev-parse HEAD)" \
        = "${KICAD_LIBRARY_UTILS_COMMIT}" \
    && cp /opt/kicad-library-utils/COPYING \
        /usr/share/doc/kicad-library-utils/LICENSE \
    && printf '%s\n' \
        "source=https://gitlab.com/kicad/libraries/kicad-library-utils" \
        "commit=${KICAD_LIBRARY_UTILS_COMMIT}" \
        > /usr/share/doc/kicad-library-utils/SOURCE \
    && rm -rf /opt/kicad-library-utils/.git

COPY libraries/cern-kicad-libs /opt/circuit/libraries/cern-kicad-libs
RUN printf '%s\n' "${CERN_COMMIT}" > /opt/circuit/libraries/cern-kicad-libs.commit
COPY scripts/smoke_kicad11_konnect.py /opt/circuit/bin/smoke_kicad11_konnect.py
COPY scripts/konnect_client.py /opt/circuit/bin/konnect_client.py
COPY scripts/e2e_authoring.py /opt/circuit/bin/e2e_authoring.py
COPY pyproject.toml uv.lock /opt/circuit/
COPY src /opt/circuit/src
COPY plugins/circuit /opt/circuit/plugins/circuit

WORKDIR /opt/circuit

RUN if [ -f /usr/share/doc/kicad-nightly-symbols/LICENSE.md ]; then \
        cp /usr/share/doc/kicad-nightly-symbols/LICENSE.md \
          /opt/circuit/libraries/kicad-official-LICENSE.md; \
    else \
        curl --fail --location --silent --show-error \
          --retry 5 --retry-delay 10 --retry-all-errors \
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
    && uv export --frozen --no-dev --no-emit-project --format requirements-txt \
        --output-file /tmp/circuit-requirements.txt \
    && uv pip install --system --break-system-packages \
        --requirement /tmp/circuit-requirements.txt \
    && uv pip install --system --break-system-packages --no-deps /opt/circuit \
    && kicad-cli --version | grep -E '^10\.99\.' \
    && kicad-cli api-server --help \
    && pdftoppm -v 2>&1 | grep -i poppler \
    && rsvg-convert --version | grep -i rsvg \
    && konnect --version | grep -F "${KONNECT_VERSION}" \
    && python3 -c "import OCP, circuit, mcp, pydantic; print(circuit.__version__)" \
    && python3 -m circuit.doctor --warn \
    && rm -f /tmp/circuit-requirements.txt \
    && chown -R circuit:circuit /home/circuit
