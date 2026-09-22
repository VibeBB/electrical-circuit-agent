# 第三者コンポーネント notices

本書は本プロジェクトに同梱または実行時に利用する第三者コンポーネントの
ライセンス、取得元、pin、再配布境界を記録する。本書は法的助言ではない。

## Konnect

- ライセンス: AGPL-3.0-only
- バージョン: v0.12.1
- release commit: `fa62e1ccb9eba359519bf8e3eab53a6cffeee33c`
- asset: `konnect-v0.12.1-x86_64-unknown-linux-gnu.tar.gz`
- SHA-256: `8a546fc949d11edbb55096a9b1f2c8f9147b26a9916b47a5c4990ee9e9441fb6`
- 取得元: <https://github.com/mixelpixx/Konnect/releases/tag/v0.12.1>
- LICENSE: <https://raw.githubusercontent.com/mixelpixx/Konnect/fa62e1ccb9eba359519bf8e3eab53a6cffeee33c/LICENSE>
- 再配布: release binaryを無改変で取得し、別プロセスのMCP stdio serverとして実行する。
- image内のLICENSE配置: `/usr/share/doc/konnect/LICENSE`

## KiCad と kicad-cli

- ライセンス: GPL-3.0-or-later
- 取得元: <https://ppa.launchpadcontent.net/kicad/kicad-dev-nightly/>
- package: `kicad-nightly`
- version: `202609210241+6e93fd642e~189~ubuntu26.04.1`
- package source: [Launchpad librarian package](https://launchpad.net/~kicad/+archive/ubuntu/kicad-dev-nightly/+files/kicad-nightly_202609210241+6e93fd642e~189~ubuntu26.04.1_amd64.deb)
- package SHA-256: `ca1c9e940092d606555e745c827c2b73e941249dbed9adb13a5afdc6c138bb19`
- 実行ファイル: `/usr/lib/kicad-nightly/bin/kicad-cli`

## KiCad公式 symbol / footprint libraries

- ライセンス: CC-BY-SA-4.0 with KiCad library exception
- ライセンスページ: <https://www.kicad.org/libraries/license/>
- 例外の要約: Licensed Materialを使う電子設計と生成ファイルがAdapted Materialに
  あたる範囲について、権利者はCC-BY-SA第3条を放棄する。
- package: `kicad-nightly-symbols`,
  `kicad-nightly-footprints`
- version:
  - symbols `202609211218+422f3fe0e~12~ubuntu26.04.1`
  - footprints `202609171319+1df46f29b~14~ubuntu26.04.1`

## CERN KiCad libraries

- ライセンス: CERN-OHL-P-2.0
- 取得元: <https://gitlab.com/ohwr/cern-kicad-libs>
- commit: `9dba1850616da7fb1a4834531a3a1f0fff7c8666`
- commit日: 2026-09-19 UTC
- 配置: `libraries/cern-kicad-libs`
- upstreamのLICENSEをsubmodule内で維持する。

## Ubuntu base image

- image: `ubuntu:26.04`
- 配布元: <https://hub.docker.com/_/ubuntu>
- Ubuntuの著作権表示とライセンスはbase imageに従う。

## OpenHands Software Agent SDK

- ライセンス: MIT
- package: `openhands-sdk==1.49.3`, `openhands-tools==1.49.3`
- 取得元: <https://pypi.org/project/openhands-sdk/1.49.3/>
- 本プロジェクトはSDKを依存関係として利用し、SDKコードをvendorしない。

## Python runtime dependencies

tools imageには`uv.lock`のruntime dependencyをインストールします。主な直接依存は
以下です。

- `mcp>=1.29,<2`: MIT、<https://pypi.org/project/mcp/>
- `pydantic>=2`: MIT、<https://pypi.org/project/pydantic/>
- `openhands-sdk==1.49.3`: MIT、<https://pypi.org/project/openhands-sdk/1.49.3/>
- `openhands-tools==1.49.3`: MIT、<https://pypi.org/project/openhands-tools/1.49.3/>

間接依存（anyio、httpx、starlette等）のpinとライセンスは`uv.lock`および各PyPI
distributionのmetadataに従います。本書は法的助言ではありません。
