# ADR-0002 KiCad 11 nightly and headless IPC

- Status: Accepted
- Date: 2026-09-20
- Related: `/home/ubuntu/spike-konnect-kicad11/REPORT.md`

## Context

Docker Hub's `kicad/kicad:nightly` is an old KiCad 10.0.0 and does not provide
`api-server`. The Ubuntu PPA's KiCad 11 nightly provides `10.99.0` and a
headless API server.

## Decision

Use Ubuntu 26.04 as the base image and pin all three packages (core,
symbols, footprints) to Launchpad librarian downloads verified by SHA-256;
see the addenda for the currently pinned versions. Start `kicad-cli api-server` per `.kicad_pcb`; no GUI or
Xvfb is used. The CLI `--socket` takes a path; the client environment variable
takes an `ipc://` URL.

## Consequences

In the Round 2 spike, the `.kicad_pcb` server started in about 0.5 seconds with
an initial RSS of about 110 MB. The `.kicad_pro` server started, but
kicad-python's `GetOpenDocuments` was unsupported. The tools image defaults to
root to satisfy the OpenHands SDK server image build's apt/useradd
requirements. Because a root-launched socket is owned by root, the standalone
runtime explicitly runs as the `circuit` user, and the server image uses the
`openhands` user created by the SDK. It operates without `DISPLAY`.

### Addendum: upstream ERC kiface build failure and interim pin

As of 2026-09-20, running `kicad-cli sch erc` on the resolute package
`202609200244+21f1f53428~189~ubuntu26.04.1` produced the following error.

```text
Failed to load shared library '/usr/lib/kicad-nightly/bin/_cvpcb.kiface':
undefined symbol: _ZN18PCB_TUNING_PATTERN10SetNetCodeEi
```

Upstream `21f1f53428` moved `PCB_TUNING_PATTERN::SetNetCode` from a header
inline to an out-of-line `.cpp` implementation, and the immediately following
`7e4fac2d` moved it back to header inline as a build failure fix. The 09-20
package's `_cvpcb.kiface` requires the out-of-line symbol, but the runtime
library does not define it; the same build commit's Ubuntu 24.04 package
reproduced the issue.

Because the fixed 09-19 core package no longer remains in the PPA index, we use
the Launchpad librarian `202609190245+6d837080a5~189~ubuntu26.04.1` pinned by
SHA-256. ERC and netlist export succeed on this build. Since the librarian's
retention period is not guaranteed, we return to the PPA pin once a fixed
nightly lands back in the PPA and the dependency check reports it.

### Addendum: 2026-09-22 pin update to the fixed 09-21 build

The resolute package `202609210241+6e93fd642e~189~ubuntu26.04.1` contains the
`7e4fac2d` revert, and `kicad-cli sch erc` plus the docker integration tests
pass on it, so the pins moved forward to that build. The symbols and
footprints packages also moved to librarian + SHA-256 downloads
(`202609211218+422f3fe0e~12~ubuntu26.04.1` and
`202609171319+1df46f29b~14~ubuntu26.04.1`), making the build independent of
PPA retention — the mechanism that broke on 2026-09-22 when the PPA dropped
the previously pinned symbols version.
