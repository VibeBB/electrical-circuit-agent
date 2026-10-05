# electrical-circuit-agent

Part of the [VibeBB](https://vibebb.org/) family of AI hardware-design agents.
[English](#english) | [日本語](#日本語)

## English

### What this is

An [OpenHands](https://github.com/OpenHands) plugin that turns a plain-language
conversation about an electronic product into a real KiCad design: a checked
schematic, a routed printed circuit board, and the manufacturing files a fab
house needs. The heavy work — drawing, checking, exporting — is done by
deterministic tools, not by the model's imagination.

### What you can do with it

- Describe a gadget in plain words and get a manufacturable board design.
- Hand it photos, sketches, or part datasheets and have them understood.
- Drop in an existing KiCad project and ask for changes.
- Ask "why did you do that?" — every non-trivial choice is recorded with its
  reasoning.

### What you give it

- A short description of what you want to build (features, size, connectors).
- Optionally: photos or hand-drawn sketches, part datasheets (PDF), or an
  existing KiCad project to modify.

### What you get back

- A schematic (`.kicad_sch`) and a routed PCB (`.kicad_pcb`) plus pictures of
  them you can look at.
- ERC and DRC reports — electrical and layout rule checks decided only by
  KiCad itself, never by the model's opinion.
- Manufacturing exports: Gerber plots, drill files, bill of materials, and
  pick-and-place placement data.
- A pin map for firmware sister plugins, a connectivity file for simulation,
  and a design report summarizing every gate.
- A reasoning trail: decisions, stage impressions, and image reviews recorded
  under `observations/circuit/` so you can audit *why* the design looks this
  way.

### How it works

Work flows through stages — intake and brief, library parts, schematic,
layout, review, manufacturing export — each delegated to a specialist
sub-agent. Two checkpoints protect you: new library parts are packaged as a
hash-bound evidence packet a human approves before use, and ERC/DRC verdicts
are produced solely by `kicad-cli` JSON output; a model that "feels done"
cannot overrule them.

### Working with sister plugins

This plugin is one of the VibeBB sisters. UX-creator sends it work orders
through `liaison/*.ux-request.json` files, which it answers with
`*.ux-response.json` files. Firmware and FPGA sisters consume its MCU pin map;
simulation consumes its connectivity export; wire consumes connector envelope
data; mechanical, production-engineering, and document sisters consume its
exports and reports.

### Getting started in AgentCanvas / OpenHands

Install the plugin (Agent Canvas → Customize → Plugins → Add plugin) with
source `github:VibeBB/electrical-circuit-agent`, path `plugins/circuit`, and
the latest release tag. KiCad and Konnect run inside a published tools image —
Docker is required. Then try a first message like: "Design a USB-C temperature
logger board around an RP2040 with a JST battery connector."

### Limits

- KiCad 11 nightly tooling; the toolchain runs in the provided Docker image.
- Pass/fail is decided by KiCad checks only — visual reviews are advisory.
- Custom library parts require human approval before use.
- Large or unusual designs may need several conversation rounds.

### Safety

Never enter API keys, tokens, or secrets into prompts or project files.
Manufacturing files are advisory artifacts; have a professional review the
design before building products that touch mains voltage, batteries at
scale, or safety-critical systems.

### Links

- Documentation index: [docs/README.md](docs/README.md)
- Site: <https://vibebb.org/>
- Sister plugins: [bard-agent](https://github.com/VibeBB/bard-agent) ·
  [mechanical-agent](https://github.com/VibeBB/mechanical-agent) ·
  [wire-agent](https://github.com/VibeBB/wire-agent)

### License

BSD-3-Clause, © VibeBB — see [LICENSE](LICENSE) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Konnect runs as an
unmodified AGPL-3.0-only binary in a separate process.

## 日本語

### これは何ですか

[OpenHands](https://github.com/OpenHands) 向けプラグインで、電子製品についての
平易な会話から実際の KiCad 設計を生成します: 検査済みの回路図、配線済み
プリント基板、製造業者が必要とする製造ファイル。図面・検査・出力という重い
処理は、モデルの想像ではなく決定論的ツールが実行します。

### できること

- 作りたいものを平易な言葉で説明し、製造可能な基板設計を得る。
- 写真・手描きスケッチ・部品データシートを渡して理解させる。
- 既存の KiCad プロジェクトを渡して変更を依頼する。
- 「なぜそうしたの?」と聞く — 重要な選択はすべて理由とともに記録される。

### 何を渡すか

- 作りたいものの短い説明(機能・サイズ・コネクタ)。
- 任意で: 写真や手描きスケッチ、部品データシート(PDF)、変更したい既存の
  KiCad プロジェクト。

### 何が返ってくるか

- 回路図(`.kicad_sch`)と配線済み基板(`.kicad_pcb`)、および目視できる画像。
- ERC/DRC レポート — 電気・レイアウト規則検査は KiCad だけが判定し、
  モデルの感想は使われない。
- 製造エクスポート: ガーバー・ドリル・部品表・実装位置データ。
- firmware 系 sister プラグイン向けピンマップ、simulation 向け
  connectivity、各ゲートをまとめた設計レポート。
- 推論の記録: `observations/circuit/` 以下に残る決定・工程感想・画像
  レビュー。設計がこうなった理由を監査できる。

### どう動くか

作業は工程 — 要件取り込みとブリーフ、ライブラリ部品、回路図、レイアウト、
レビュー、製造エクスポート — ごとに専門サブエージェントへ委譲されます。
2つのチェックポイントが保護します: 新規ライブラリ部品は hash 拘束の根拠
パケットとして人間が承認するまで使われず、ERC/DRC の合否は `kicad-cli` の
JSON 出力のみで決まり、「完成した気がする」モデルでは覆せません。

### sister プラグインとの連携

このプラグインは VibeBB sister の一つです。UX-creator は
`liaison/*.ux-request.json` の作業指示を送り、本プラグインは
`*.ux-response.json` で応答します。firmware/FPGA sister はピンマップを、
simulation は connectivity を、wire はコネクタ envelope を、
mechanical/production-engineering/document sister はエクスポートと
レポートを消費します。

### AgentCanvas / OpenHands での始め方

プラグインをインストール(Agent Canvas → Customize → Plugins → Add plugin)
します: source は `github:VibeBB/electrical-circuit-agent`、path は
`plugins/circuit`、ref は最新リリースタグ。KiCad と Konnect は公開済み
ツールイメージ内で動くため Docker が必要です。最初のメッセージ例:
「RP2040 と JST バッテリーコネクタを使った USB-C 温度ロガー基板を
設計して」。

### 制限

- ツールは KiCad 11 nightly で、提供 Docker イメージ内で動作。
- 合否は KiCad 検査のみが決定 — 視覚レビューは助言。
- 自作ライブラリ部品は使用前に人間の承認が必要。
- 大規模・特殊な設計は複数ラウンドの会話を要することがある。

### 安全

API キー・トークン・秘密情報をプロンプトやプロジェクトファイルに
入力しないでください。製造ファイルは助言的な成果物です。商用電源・
大容量バッテリー・安全重視系に触れる製品は、製造前に専門家のレビューを
受けてください。

### リンク

- ドキュメント索引: [docs/README.md](docs/README.md)
- サイト: <https://vibebb.org/>
- sister プラグイン: [bard-agent](https://github.com/VibeBB/bard-agent) ·
  [mechanical-agent](https://github.com/VibeBB/mechanical-agent) ·
  [wire-agent](https://github.com/VibeBB/wire-agent)

### ライセンス

BSD-3-Clause、© VibeBB — [LICENSE](LICENSE) と
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) を参照。Konnect は
AGPL-3.0-only の無改変バイナリを別プロセスで実行します。
