# Third-Party Notices

circuit is licensed BSD-3-Clause (see LICENSE). This file records the
licenses, sources, pins, and redistribution boundaries of the third-party
components the project bundles or runs at execution time. This file is not
legal advice.

## Konnect

- License: AGPL-3.0-only
- Version: v0.12.1
- Release commit: `fa62e1ccb9eba359519bf8e3eab53a6cffeee33c`
- Asset: `konnect-v0.12.1-x86_64-unknown-linux-gnu.tar.gz`
- SHA-256: `8a546fc949d11edbb55096a9b1f2c8f9147b26a9916b47a5c4990ee9e9441fb6`
- Source: <https://github.com/mixelpixx/Konnect/releases/tag/v0.12.1>
- LICENSE: <https://raw.githubusercontent.com/mixelpixx/Konnect/fa62e1ccb9eba359519bf8e3eab53a6cffeee33c/LICENSE>
- Redistribution: the release binary is fetched unmodified and executed as a
  separate-process MCP stdio server.
- In-image LICENSE location: `/usr/share/doc/konnect/LICENSE`

## KiCad and kicad-cli

- License: GPL-3.0-or-later
- Source: <https://ppa.launchpadcontent.net/kicad/kicad-dev-nightly/>
- Package: `kicad-nightly`
- Version: `202609250241+83b5faf3d5~189~ubuntu26.04.1`
- Package source: [Launchpad librarian package](https://launchpad.net/~kicad/+archive/ubuntu/kicad-dev-nightly/+files/kicad-nightly_202609250241+83b5faf3d5~189~ubuntu26.04.1_amd64.deb)
- Package SHA-256: `5a3fccf1bff078e130c4719af247dd5f27ab91c8c8b64a0a9908b1e90e2b93bd`
- Executable: `/usr/lib/kicad-nightly/bin/kicad-cli`

## KiCad official symbol / footprint libraries

- License: CC-BY-SA-4.0 with the KiCad library exception
- License page: <https://www.kicad.org/libraries/license/>
- Exception summary: to the extent that electronic designs and generated
  files using Licensed Material constitute Adapted Material, the licensor
  waives Section 3 of CC-BY-SA.
- Packages: `kicad-nightly-symbols`, `kicad-nightly-footprints`
- Versions:
  - symbols `202609251227+151fb6a8c~12~ubuntu26.04.1`
  - footprints `202609222017+55d9dd1a3~14~ubuntu26.04.1`

## CERN KiCad libraries

- License: CERN-OHL-P-2.0
- Source: <https://gitlab.com/ohwr/cern-kicad-libs>
- Commit: `4fc6742b43f7b8d59f48c80de7c424fe7841b40b`
- Commit date: 2026-09-25 UTC
- Location: `libraries/cern-kicad-libs`
- The upstream LICENSE is preserved inside the submodule.

## FreeRouting

- License: GPL-3.0
- Version: v2.4.1
- Asset: `freerouting-2.4.1.jar`
- SHA-256: `251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9`
- Source: <https://github.com/freerouting/freerouting/releases/tag/v2.4.1>
- LICENSE: <https://raw.githubusercontent.com/freerouting/freerouting/v2.4.1/LICENSE>
- Redistribution: the release JAR is placed unmodified at
  `/opt/freerouting/freerouting.jar` and launched by Konnect as a separate
  `java -jar` process. It is not imported or linked into Python.
- In-image LICENSE location: `/usr/share/doc/freerouting/LICENSE`

## IBM Semeru Runtime Open Edition (Eclipse OpenJ9)

- License: EPL-2.0 / Apache-2.0 / GPL-2.0-with-classpath-exception
  (per-module licenses ship inside the tarball at `/opt/jre/legal/`)
- Version: 27.0.0.0 (OpenJ9 0.62.0)
- Asset: `ibm-semeru-open-jre_x64_linux_27.0.0.0.tar.gz`
- SHA-256: `9e6d9c1131da124bd08eb4183f7787a9f90111fc3d62c1231976c2d37372d59e`
- Source: <https://github.com/ibmruntimes/semeru27-binaries/releases/tag/jdk-27.0.0.0>
- Redistribution: the release tarball is extracted unmodified to `/opt/jre`
  and resolved as the FreeRouting execution JRE via `JAVA_HOME`/`PATH`.
- In-image provenance: `/usr/share/doc/semeru-jre/SOURCE`

## Ubuntu base image

- Image: `ubuntu:26.04`
- Source: <https://hub.docker.com/_/ubuntu>
- Ubuntu copyright notices and licenses follow the base image.

## OpenHands Software Agent SDK

- License: MIT
- Packages: `openhands-sdk==1.49.6`, `openhands-tools==1.49.6`
- Source: <https://pypi.org/project/openhands-sdk/1.49.6/>
- The project uses the SDK as a dependency and does not vendor SDK code.

## Python runtime dependencies

The tools image installs the runtime dependencies from `uv.lock`. The main
direct dependencies are:

- `mcp>=1.29,<2`: MIT — <https://pypi.org/project/mcp/>
- `pydantic>=2`: MIT — <https://pypi.org/project/pydantic/>
- `openhands-sdk==1.49.6`: MIT — <https://pypi.org/project/openhands-sdk/1.49.6/>
- `openhands-tools==1.49.6`: MIT — <https://pypi.org/project/openhands-tools/1.49.6/>

Transitive dependencies (anyio, httpx, starlette, etc.) follow the pins in
`uv.lock` and each PyPI distribution's own metadata. This file is not legal
advice.
