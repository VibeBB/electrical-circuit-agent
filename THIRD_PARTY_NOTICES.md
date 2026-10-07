# Third-Party Notices

circuit is licensed BSD-3-Clause (see LICENSE). This file records the
licenses, sources, pins, and redistribution boundaries of the third-party
components the project bundles or runs at execution time. This file is not
legal advice.

## Konnect

- License: AGPL-3.0-only
- Version: v0.13.0
- Release commit: `6bbe3e4f890ba1d37c0e5d5f38ccd03d90958c9e`
- Asset: `konnect-v0.13.0-x86_64-unknown-linux-gnu.tar.gz`
- SHA-256: `9c9d28e7d905d8e8519339a78890193f252bc8134993349b40d80e3a67b93512`
- Source: <https://github.com/mixelpixx/Konnect/releases/tag/v0.13.0>
- LICENSE: <https://raw.githubusercontent.com/mixelpixx/Konnect/6bbe3e4f890ba1d37c0e5d5f38ccd03d90958c9e/LICENSE>
- Redistribution: the release binary is fetched unmodified and executed as a
  separate-process MCP stdio server.
- In-image LICENSE location: `/usr/share/doc/konnect/LICENSE`

## KiCad and kicad-cli

- License: GPL-3.0-or-later
- Source: <https://ppa.launchpadcontent.net/kicad/kicad-dev-nightly/>
- Package: `kicad-nightly`
- Version: `202609302019+55110814ee~189~ubuntu26.04.1`
- Package source: [Launchpad librarian package](https://launchpad.net/~kicad/+archive/ubuntu/kicad-dev-nightly/+files/kicad-nightly_202609302019+55110814ee~189~ubuntu26.04.1_amd64.deb)
- Package SHA-256: `63e5e2b3b8a2b627e8a4a0b0c5fc33b0ac875cab1c796e3ef806df27cc620c8e`
- Executable: `/usr/lib/kicad-nightly/bin/kicad-cli`

## KiCad official symbol / footprint libraries

- License: CC-BY-SA-4.0 with the KiCad library exception
- License page: <https://www.kicad.org/libraries/license/>
- Exception summary: to the extent that electronic designs and generated
  files using Licensed Material constitute Adapted Material, the licensor
  waives Section 3 of CC-BY-SA.
- Packages: `kicad-nightly-symbols`, `kicad-nightly-footprints`
- Versions:
  - symbols `202609271717+716edc43f~12~ubuntu26.04.1`
  - footprints `202609302018+9326b9efd~14~ubuntu26.04.1`

## KiCad Library Convention checker

- License: GPL-3.0
- Source: <https://gitlab.com/kicad/libraries/kicad-library-utils>
- Commit: `90b0af91eaffcd91552027c3bfd166896f78c7de`
- Acquisition: depth-one Git fetch of the pinned commit; checkout `HEAD` is
  verified against the commit before `.git` is removed from the image.
- Redistribution: the pinned source is used only as an unmodified subprocess;
  no checker code is imported or copied into the Python runtime.
- In-image LICENSE and source record:
  `/usr/share/doc/kicad-library-utils/LICENSE` and `SOURCE`

## KiCad footprint generator numeric references

- License: GPL-3.0
- Source: <https://gitlab.com/kicad/libraries/kicad-footprint-generator>
- Commit: `eaee2837c34188adbf652ce7e5b2374541108cd1`
- Numeric facts referenced by the built-in `builtin:kicad-generator` profile:
  fabrication tolerance `0.1 mm`, placement tolerance `0.05 mm`, and minimum
  exposed-pad-to-pad clearance `0.2 mm`.
- No source code from this project is copied into this repository.

## CERN KiCad libraries

- License: CERN-OHL-P-2.0
- Source: <https://gitlab.com/ohwr/cern-kicad-libs>
- Commit: `eec34374e810d4253b6a2764687efbfbb8ad29a5`
- Commit date: 2026-10-03 UTC
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
- Packages: `openhands-sdk==1.53.0`, `openhands-tools==1.53.0`
- Source: <https://pypi.org/project/openhands-sdk/1.53.0/>
- The project uses the SDK as a dependency and does not vendor SDK code.

## Python runtime dependencies

The tools image installs the runtime dependencies from `uv.lock`. The main
direct dependencies are:

- `cadquery-ocp-novtk==8.0.1.0.0`: OCP bindings under Apache-2.0 and Open CASCADE
  Technology under LGPL-2.1 with the OCCT exception; the PyPI wheel is installed
  without modification. The installed wheel contains no separate OCP/OCCT
  license files; its distribution metadata identifies Apache-2.0. It bundles
  51 `libTK*.so.8.0.1` OCCT shared libraries under
  `cadquery_ocp_novtk.libs/`, dynamically linked by the OCP extension.
  License sources: <https://github.com/CadQuery/OCP/blob/master/LICENSE>,
  <https://github.com/Open-Cascade-SAS/OCCT/blob/master/LICENSE_LGPL_21.txt>,
  and <https://github.com/Open-Cascade-SAS/OCCT/blob/master/OCCT_LGPL_EXCEPTION.txt>.
- `mcp>=1.29,<2`: MIT — <https://pypi.org/project/mcp/>
- `pydantic>=2`: MIT — <https://pypi.org/project/pydantic/>
- `openhands-sdk==1.53.0`: MIT — <https://pypi.org/project/openhands-sdk/1.53.0/>
- `openhands-tools==1.53.0`: MIT — <https://pypi.org/project/openhands-tools/1.53.0/>

Transitive dependencies (anyio, httpx, starlette, etc.) follow the pins in
`uv.lock` and each PyPI distribution's own metadata. This file is not legal
advice.
