# Libraries

## Official KiCad libraries

`kicad-nightly-symbols` and `kicad-nightly-footprints` from the KiCad nightly
PPA are installed into the image. Versions are pinned in
`THIRD_PARTY_NOTICES.md` and the Dockerfile. The official libraries are
CC-BY-SA-4.0 with the KiCad library exception; see
<https://www.kicad.org/libraries/license/> for the license text.

## CERN KiCad libraries

- Source: <https://gitlab.com/ohwr/cern-kicad-libs>
- Commit: `eec34374e810d4253b6a2764687efbfbb8ad29a5`
- Commit date: 2026-10-03 UTC
- License: CERN-OHL-P-2.0
- Location: `libraries/cern-kicad-libs`

The submodule is fetched with `--depth 1`; upstream LICENSE and LICENSES are
not modified.
