# circuit-agent tools image

## Purpose

`circuit-tools.Dockerfile` bundles KiCad 11 nightly, Konnect v0.12.1, IBM
Semeru Open JRE (Eclipse OpenJ9), FreeRouting, the official KiCad libraries,
and the CERN KiCad libraries into a single execution environment. Konnect is
launched as a separate process as an unmodified AGPL binary and is never
imported into the Python package; FreeRouting is likewise executed only as a
`java -jar` subprocess spawned by Konnect (see ADR-0021 for the current
upstream Specctra export limitation).

## Contents

| Content | Pin |
|---|---|
| Ubuntu | `26.04` |
| KiCad | `202609230242+3f88267300~189~ubuntu26.04.1` |
| KiCad footprints | `202609222017+55d9dd1a3~14~ubuntu26.04.1` |
| KiCad symbols | `202609221218+1565b6644~12~ubuntu26.04.1` |
| Konnect | `v0.12.1`, release commit and SHA-256 in the Dockerfile |
| IBM Semeru Open JRE | `27.0.0.0`, release tarball SHA-256 in the Dockerfile |
| FreeRouting | `v2.4.1`, release JAR SHA-256 in the Dockerfile |
| CERN libraries | submodule commit of `libraries/cern-kicad-libs` (also recorded in `cern-kicad-libs.commit` inside the image and the OCI label `circuit.cern.commit`) |

## Build and smoke

```bash
docker build -f docker/circuit-tools.Dockerfile -t circuit-tools:dev .
docker run --rm \
  --user circuit \
  -v "$PWD/fixtures/smoke-board:/work:ro" \
  circuit-tools:dev \
  python3 /opt/circuit/bin/smoke_kicad11_konnect.py
```

The tools image's default user is root for SDK server image build
compatibility. For standalone runs specify `--user circuit`; in the server
image use the `openhands` user created by the SDK.

Docker itself does not guarantee determinism. The publish workflow on main
publishes the tools/server images to GHCR and records the actual digests in
`docker/image-digests.json`.

## GHCR publishing and lock

Publishing is done by `.github/workflows/publish-circuit-images.yml`. After the
tools image is verified, the OpenHands SDK v1.49.4 server image is built, with
the immutable tags `<commit>-tools` and `<commit>-latest-source` plus the
`latest` alias. Lock updates are separated into bot PRs that auto-merge only
when that PR's CI succeeds.

```bash
uv run python scripts/print_locked_image.py --entry circuit_tools
uv run python scripts/pull_locked_image.py --entry circuit_tools
```

Before the first publish, when no lock exists, the commands above exit with a
clear error. `locked-image-check` is skipped while no lock exists; once present,
it runs smoke and Docker integration against the digest-pinned tools image.
