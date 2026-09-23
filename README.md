# electrical-circuit-agent

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/VibeBB/electrical-circuit-agent)

Part of the [VibeBB](https://github.com/VibeBB) agent family:
[bard-agent](https://github.com/VibeBB/bard-agent) ·
[electrical-circuit-agent](https://github.com/VibeBB/electrical-circuit-agent) ·
[mechanical-agent](https://github.com/VibeBB/mechanical-agent) ·
[wire-agent](https://github.com/VibeBB/wire-agent)

[English](#english) | [日本語](#日本語)

## English

An [OpenHands](https://github.com/OpenHands) plugin and tool image for
conversational schematic and PCB design using KiCad 11 nightly — a
requirements conversation becomes a verified schematic and a routed board.

### What it does

- **Conversational intake** — clarifies design intent and fixes it as a JSON
  design brief (parts, nets, board dimensions, optional placement) plus an
  intake sidecar that binds every requirement and assumption to an id.
- **Deterministic authoring** — validates the brief, then builds the
  schematic with Konnect 0.12.1, updates/places/routes the PCB through
  `kicad-cli api-server`, and exports manufacturing files.
- **Deterministic verification** — ERC/DRC verdicts come solely from
  `kicad-cli` JSON output; `circuit_connectivity_check` compares the
  produced netlist against the design brief. LLM self-reports and Konnect
  explanatory text are never promoted to a verdict — missing tools and
  unknowns fail closed.

### Install

The plugin lives in `plugins/circuit` and follows the OpenHands Software
Agent SDK plugin layout (skills, agents, commands, hooks, `.mcp.json`).
Install it from the OpenHands plugin UI (Agent Canvas → Customize →
Plugins → Add plugin) with:

| Field | Value |
| --- | --- |
| Source | `github:VibeBB/electrical-circuit-agent` |
| Ref | the latest tag from [Releases](https://github.com/VibeBB/electrical-circuit-agent/releases) |
| Path | `plugins/circuit` |

The plugin includes the `circuit` runtime MCP, a Konnect MCP configuration,
brief/schematic/layout/review sub-agents, doctor/design/ERC/DRC/export
commands, lifecycle hooks, and six skills. Because MCP configuration is not
automatically inherited by sub-agents from the parent, each AgentDefinition
declares the same server map explicitly.

Prebuilt container images are published to GHCR
(`ghcr.io/vibebb/circuit-tools`, `ghcr.io/vibebb/circuit-server`;
digest-locked via `docker/image-digests.json`). `latest` is a convenience
alias; runtime uses the SHA-256 digest-pinned references. No lock file is
created until the first publish, and pull verification is fail-closed in
environments without a lock. The tools image defaults to root for SDK
server image build compatibility; specify the `circuit` user for
standalone runs.

```bash
docker run --rm --user circuit \
  -v "$PWD/fixtures/smoke-board:/work:ro" \
  ghcr.io/vibebb/circuit-tools:latest \
  python3 /opt/circuit/bin/smoke_kicad11_konnect.py
```

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/) for development.

### Using the plugin

Commands (agent-facing):

- `/circuit:doctor` — probe the KiCad/Konnect execution environment
- `/circuit:design` — drive brief → schematic → PCB → gates → report
- `/circuit:erc` — run the electrical rules check on a schematic
- `/circuit:drc` — run the design rules check on a board
- `/circuit:export` — regenerate manufacturing artifacts only

Sub-agents (`task` tool): `circuit-brief` (requirement/intake
conversation), `circuit-schematic` and `circuit-layout` (authoring), and
`circuit-review` (advisory review — no pass/fail authority).

### Using the core directly

The deterministic entry point is `scripts/e2e_authoring.py`; the `circuit`
MCP server (`python3 -m circuit.mcp_server`) exposes the same deterministic
tools over stdio. See [docs/operations.md](docs/operations.md) for the full
command surface.

### Architecture

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
ACD's Design Graph, Evidence, and L1-L3 gate mechanisms are out of scope for
this product.

### Development

```bash
uv sync
uv run python scripts/verify_all.py --stage fast
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [docs/architecture.md](docs/architecture.md),
and the ADR index in [docs/README.md](docs/README.md).

### License and third-party components

This project itself is licensed under BSD-3-Clause — see [LICENSE](LICENSE)
and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the list of
third-party components, their licenses, sources, and redistribution
boundaries.

Konnect runs as an unmodified AGPL-3.0-only binary in a separate process.
Its source is release commit `fa62e1ccb9eba359519bf8e3eab53a6cffeee33c` of
[mixelpixx/Konnect](https://github.com/mixelpixx/Konnect). The Konnect README
notes that "commercial licenses are available" for corporate use cases where
the AGPL does not fit. This statement is not legal advice; users should verify
the license conditions that apply to their own usage.

## 日本語

[OpenHands](https://github.com/OpenHands) 向けの、KiCad 11 nightly を使う
会話型回路図・基板設計プラグインとツールイメージです — 要件の会話から
検証済みの回路図と配線済み基板を生成します。

### できること

- **対話による要件取り込み** — 設計意図を明確化し、JSON 設計ブリーフ
  （部品・net・基板寸法・任意の配置）と intake サイドカー（全要件・仮定を
  ID に紐付け）として固定します。
- **決定論的オーサリング** — ブリーフを検証し、Konnect 0.12.1 で回路図を
  構築、`kicad-cli api-server` 経由で PCB の更新・配置・配線を行い、
  製造ファイルをエクスポートします。
- **決定論的検証** — ERC/DRC の合否は `kicad-cli` の JSON 出力のみで決定。
  `circuit_connectivity_check` は生成 netlist を設計ブリーフと照合します。
  LLM の自己申告や Konnect の説明文を合否へ昇格させることはなく、
  ツール欠落・不明は fail-closed で不合格です。

### インストール

プラグインは `plugins/circuit` にあり、OpenHands Software Agent SDK の
プラグイン構成（skills・agents・commands・hooks・`.mcp.json`）に従います。
OpenHands プラグイン UI（Agent Canvas → Customize → Plugins → Add plugin）
から次の値でインストールします:

| 項目 | 値 |
| --- | --- |
| Source | `github:VibeBB/electrical-circuit-agent` |
| Ref | [Releases](https://github.com/VibeBB/electrical-circuit-agent/releases) の最新タグ |
| Path | `plugins/circuit` |

プラグインには `circuit` runtime MCP、Konnect MCP 設定、
brief・schematic・layout・review の各サブエージェント、
doctor/design/ERC/DRC/export コマンド、ライフサイクル hook、6つのスキルが
含まれます。サブエージェントは親から MCP 設定を自動継承しないため、
各 AgentDefinition に同じ server map を明示しています。

ビルド済みコンテナイメージは GHCR に公開されています
（`ghcr.io/vibebb/circuit-tools`、`ghcr.io/vibebb/circuit-server`。
`docker/image-digests.json` で digest 固定）。`latest` は利便性のための
別名であり、実行時は SHA-256 digest 固定参照を使います。初回 publish
までは lock ファイルを作成せず、lock が無い環境では pull 検証を
fail-closed にします。tools image は SDK server image build 互換のため
root を既定 user とし、standalone 実行時は `circuit` user を明示します。

```bash
docker run --rm --user circuit \
  -v "$PWD/fixtures/smoke-board:/work:ro" \
  ghcr.io/vibebb/circuit-tools:latest \
  python3 /opt/circuit/bin/smoke_kicad11_konnect.py
```

開発には Python ≥ 3.12 と [uv](https://docs.astral.sh/uv/) が必要です。

### プラグインの使い方

コマンド（エージェント向け）:

- `/circuit:doctor` — KiCad/Konnect 実行環境の診断
- `/circuit:design` — brief → 回路図 → PCB → ゲート → レポートを実行
- `/circuit:erc` — 回路図の電気ルール検査を実行
- `/circuit:drc` — 基板のデザインルール検査を実行
- `/circuit:export` — 製造成果物のみ再生成

サブエージェント（`task` ツール）: `circuit-brief`（要件・intake 対話）、
`circuit-schematic` と `circuit-layout`（オーサリング）、
`circuit-review`（助言レビュー — 合否権限なし）。

### コアの直接使用

決定論的エントリポイントは `scripts/e2e_authoring.py` です。
`circuit` MCP サーバー（`python3 -m circuit.mcp_server`）は同じ決定論
ツールを stdio 経由で公開します。コマンド一覧は
[docs/operations.md](docs/operations.md) を参照してください。

### 構成

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
ACD の Design Graph、Evidence、L1-L3 gate 機構は本製品の範囲に含めません。

### 開発

```bash
uv sync
uv run python scripts/verify_all.py --stage fast
```

[CONTRIBUTING.md](CONTRIBUTING.md)、[docs/architecture.md](docs/architecture.md)、
[docs/README.md](docs/README.md) の ADR 索引を参照してください。

### ライセンスと第三者コンポーネント

本プロジェクト自身のライセンスは BSD-3-Clause です — [LICENSE](LICENSE) と
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)（第三者コンポーネントの
一覧・ライセンス・取得元・再配布境界）を参照してください。

Konnect は AGPL-3.0-only の無改変バイナリを別プロセスとして実行します。
ソースは [mixelpixx/Konnect](https://github.com/mixelpixx/Konnect) の
release commit `fa62e1ccb9eba359519bf8e3eab53a6cffeee33c` です。Konnect の
README は、企業利用で AGPL が適合しない場合について
「commercial licenses are available」と案内しています。本記載は法的助言では
なく、利用者は自身の利用形態に適用されるライセンス条件を確認してください。
