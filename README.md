# electrical-circuit-agent

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/VibeBB/electrical-circuit-agent)

[English](#english) | [日本語](#日本語)

## English

An OpenHands plugin and tool image for conversational schematic and PCB design
using KiCad 11 nightly. The project is currently in **phase 1 development**,
building out the distributable plugin skeleton and the KiCad/Konnect execution
boundary.

### Architecture

The OpenHands Agent Canvas handles conversation and delegation, and the planned
`plugins/circuit` becomes the plugin distribution entry point. KiCad tools and
`kicad-cli api-server` run in a digest-pinned Docker image, and Konnect v0.12.1
connects as a separate MCP stdio process.

```text
user
  -> OpenHands Agent Canvas
  -> plugins/circuit
  -> circuit-brief
  -> circuit-schematic / circuit-layout / circuit-review
  -> Konnect MCP + kicad-cli
  -> KiCad project files
```

KiCad 11 nightly is installed from Ubuntu 26.04's `ppa:kicad/kicad-dev-nightly`
and provides headless IPC via `kicad-cli api-server` without a GUI or Xvfb.
The planned plugin install entry is:

```text
github:VibeBB/electrical-circuit-agent
path: plugins/circuit
```

The plugin includes the `circuit` runtime MCP, Konnect MCP configuration,
brief/schematic/layout/review sub-agents, doctor/design/ERC/DRC/export commands,
lifecycle hooks, and six skills. Because MCP configuration is not automatically
inherited by sub-agents from the parent, each AgentDefinition declares the same
server map explicitly.

ERC/DRC verdicts are determined solely by parsing `kicad-cli` JSON output, not
by LLM self-reporting. ACD's Design Graph, Evidence, and L1-L3 gate mechanisms
are out of scope for this product.

### License and third-party components

This project itself is licensed under BSD-3-Clause. The list of third-party
components, their licenses, sources, and redistribution boundaries is recorded
in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

Konnect runs as an unmodified AGPL-3.0-only binary in a separate process.
Its source is release commit `fa62e1ccb9eba359519bf8e3eab53a6cffeee33c` of
[mixelpixx/Konnect](https://github.com/mixelpixx/Konnect). The Konnect README
notes that "commercial licenses are available" for corporate use cases where
the AGPL does not fit. This statement is not legal advice; users should verify
the license conditions that apply to their own usage.

For detailed responsibility partitioning see [`docs/architecture.md`](docs/architecture.md);
for operating procedures see [`docs/operations.md`](docs/operations.md).

### Design brief authoring

Design intent is fixed as a JSON design brief containing parts, nets, board
dimensions, and optional placement. Conversation-derived requirements and
assumptions are recorded in an intake sidecar; authoring does not proceed until
intake is `ready` and library verification is `pass`. See
[`tests/data/brief_led_loop.json`](tests/data/brief_led_loop.json) for an
example. After validation with `circuit_brief_validate`, the schematic is
created with Konnect 0.12.1's `batch_place_components` and
`batch_connect_to_net` (net labels on pin endpoints).
`circuit_connectivity_check` is an authoritative gate that compares the netlist
produced by `kicad-cli` against the design brief; Konnect explanatory text is
never promoted to a verdict. ERC, PCB update/placement/routing via the
api-server, DRC, manufacturing file export, and `circuit_design_report` follow.

### Distribution

We publish `ghcr.io/VibeBB/circuit-tools` and `ghcr.io/VibeBB/circuit-server`
to GHCR. `latest` is a convenience alias; at runtime the SHA-256 digest-pinned
references recorded in `docker/image-digests.json` are used. No lock file is
created until the first publish, and pull verification is fail-closed in
environments without a lock. The tools image defaults to root for SDK server
image build compatibility; specify the `circuit` user for standalone runs.

```bash
docker run --rm --user circuit \
  -v "$PWD/fixtures/smoke-board:/work:ro" \
  ghcr.io/VibeBB/circuit-tools:latest \
  python3 /opt/circuit/bin/smoke_kicad11_konnect.py
```

## 日本語

KiCad 11 nightly を使う、会話型の回路図・基板設計向け OpenHands プラグインと
ツールイメージです。現在は**フェーズ1 開発中**であり、配布用プラグインの骨格と
KiCad/Konnect の実行境界を整備しています。

### 構成

OpenHands Agent Canvas が会話と委譲を担当し、予定している
`plugins/circuit` がプラグイン配布入口になります。KiCad ツールと
`kicad-cli api-server` は digest 固定 Docker イメージで実行し、Konnect
v0.12.1 は MCP stdio の別プロセスとして接続します。

```text
ユーザー
  -> OpenHands Agent Canvas
  -> plugins/circuit
  -> circuit-brief
  -> circuit-schematic / circuit-layout / circuit-review
  -> Konnect MCP + kicad-cli
  -> KiCad project files
```

KiCad 11 nightly は Ubuntu 26.04 の `ppa:kicad/kicad-dev-nightly` から導入し、
GUI や Xvfb を使わず `kicad-cli api-server` で headless IPC を提供します。
プラグインのインストール入口は次の予定です。

```text
github:VibeBB/electrical-circuit-agent
path: plugins/circuit
```

pluginには`circuit` runtime MCP、Konnect MCP設定、ブリーフ・回路図・レイアウト・レビュー
sub-agent、doctor/設計/ ERC/DRC/export command、ライフサイクルhook、6つのSkillを
含めます。sub-agent間のMCP設定は親から自動継承されないため、各AgentDefinitionに
同じserver mapを明示します。

ERC/DRC の合否は LLM の自己申告ではなく、`kicad-cli` の JSON 出力をパースした
結果だけで決定します。ACD の Design Graph、Evidence、L1-L3 gate 機構は本製品の
範囲に含めません。

### ライセンスと第三者コンポーネント

本プロジェクト自身のライセンスは BSD-3-Clause です。第三者コンポーネントの
一覧、ライセンス、取得元、再配布境界は
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) に記録しています。

Konnect は AGPL-3.0-only の無改変バイナリを別プロセスとして実行します。
ソースは [mixelpixx/Konnect](https://github.com/mixelpixx/Konnect) の
release commit `fa62e1ccb9eba359519bf8e3eab53a6cffeee33c` です。Konnect の
README は、企業利用で AGPL が適合しない場合について
「commercial licenses are available」と案内しています。本記載は法的助言では
なく、利用者は自身の利用形態に適用されるライセンス条件を確認してください。

詳細な責務分割は [`docs/architecture.md`](docs/architecture.md)、運用手順は
[`docs/operations.md`](docs/operations.md) を参照してください。

### 設計ブリーフ authoring

設計意図は、部品、net、基板寸法、任意の配置を含むJSON設計ブリーフで固定します。
会話由来の要件と仮定はintake sidecarへ記録し、intakeが`ready`、ライブラリ検証が
`pass`になるまでauthoringへ進みません。
例は [`tests/data/brief_led_loop.json`](tests/data/brief_led_loop.json) です。
`circuit_brief_validate`で検証した後、Konnect 0.12.1の
`batch_place_components`と`batch_connect_to_net`（pin endpointのnet label）で
回路図を作成します。`circuit_connectivity_check`は`kicad-cli`が出力したnetlistを
設計ブリーフと比較するauthoritative gateであり、Konnectの説明文を合否へ昇格させません。
その後にERC、api-server経由のPCB更新・配置・配線、DRC、製造ファイルexport、
`circuit_design_report`を実行します。

### 配布

GHCRには`ghcr.io/VibeBB/circuit-tools`と
`ghcr.io/VibeBB/circuit-server`を公開します。`latest`は利便性のための
別名であり、実行時は`docker/image-digests.json`に記録されたSHA-256 digest固定
参照を使います。初回publishまではlockファイルを作成せず、lockが無い環境では
pull検証をfail-closedにします。tools imageはSDK server image build互換のため
rootを既定userとし、standalone実行時は`circuit` userを明示します。

```bash
docker run --rm --user circuit \
  -v "$PWD/fixtures/smoke-board:/work:ro" \
  ghcr.io/VibeBB/circuit-tools:latest \
  python3 /opt/circuit/bin/smoke_kicad11_konnect.py
```
