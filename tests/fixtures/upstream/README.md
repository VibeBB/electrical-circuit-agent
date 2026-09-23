# Upstream contract fixtures

`board.connectivity.json` is a golden copy of the wire-agent
`ConnectivitySource` payload (wire repo `tests/fixtures/upstream/`). It is the
canonical example of what `circuit.connectivity` / `circuit_connectivity_export`
emit; the wire importer is the schema authority (wire ADR-0003). Keep the two
repositories' copies byte-identical — the tests parse it with a mirror of the
importer's validation so drift fails here before reaching wire.
