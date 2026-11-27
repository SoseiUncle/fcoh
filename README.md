# FCOH — Feature-Centric Operation History

**v0.1.0 / Experimental preview / MIT License **

FCOH（Feature-Centric Operation History）形式0.3の最小試作です。
設計意図つきYAMLから、新しい部品をFreeCADまたはFusionで作ります。
ソフトウェアの版番号は0.1.0、実装している形式の版番号は0.3です。
汎用CADファイル変換ソフトではなく、CADに依存しない設計意図・操作履歴の記録と再現を試すOSSです。

FCOH is an experimental, human/LLM-readable YAML representation of CAD design intent
and operation history, replayed through native FreeCAD and Autodesk Fusion APIs.
It is not a general CAD file converter or a complete round-trip interchange format.

2026-09-13: 両CADへのMCP経由の部品生成と、ネイティブ形式保存・再読込が成功しました。
サンプルのSTEP同士の形状差分も0 mm³でした。[検証概要](docs/validation.md)を参照してください。

## ライセンス・サポート

本プロジェクトのソースコードは[MIT License](LICENSE)で無償公開しています。
現状有姿で提供し、保証、個別サポート、返信、修正、継続保守の約束はありません。
製造や実務で使う前に、生成形状・寸法・履歴を利用者自身で検証してください。
FreeCAD、Autodesk Fusion、Python依存パッケージなどの第三者製品には、それぞれのライセンスが適用されます。
Fusion本体は同梱せず、その利用権や無償利用を提供するものではありません。

## 初回セットアップ（Windows x64）

PowerShell 7、Git、Python 3.11以上、uv、7-Zipを用意してください。
7-Zipは `C:\Program Files\7-Zip\7z.exe` にあることを前提とします。
Fusion連携は別途Autodesk Fusionをインストールし、利用可能なアカウントで起動しておきます。

```powershell
git clone https://github.com/SoseiUncle/fcoh.git
cd fcoh
pwsh -File scripts/setup.ps1
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

セットアップはFreeCAD 1.1.3公式ポータブル版をダウンロードし、SHA-256を照合して
`vendor/` へ展開します。Python依存は `uv.lock` に従って `.venv/` へ導入します。
このリリースはソースチェックアウトから使用してください。単体wheel/PyPI配布には対応していません。
フォルダを移動した場合はFusionアドインとMCPの登録をやり直してください。

## 使い方

このフォルダをVS CodeやCodexのプロジェクトとして開いてください。
コマンドはPowerShell 7で、このフォルダから実行します。
サンプルの生成だけなら `Generate-FreeCAD.cmd` または `Generate-Fusion.cmd` を
ダブルクリックしても実行できます。Fusion用はFusionを開いた状態で使います。

```powershell
.\.venv\Scripts\python.exe -m fcoh.cli validate examples/mounting-bracket.fcoh.yaml
.\.venv\Scripts\python.exe -m fcoh.cli build examples/mounting-bracket.fcoh.yaml --cad freecad
```

生成先は毎回別の `outputs/<job_id>/` です。FreeCADでは `part.FCStd` と `part.step`、
Fusionでは `part.f3d` と `part.step` を保存します。`request.json` に実行内容、
`source.fcoh.yaml` に入力の写し、`result.json` に寸法・体積・履歴・ハッシュを残します。

サンプルは80×40×30 mm、底板厚6 mm、直径6 mmの穴4個を持つブラケットです。
`examples/mounting-bracket.fcoh.yaml` の `params` の数値を変更し、再生成してください。
寸法の正本はFCOHです。CAD内で変更した寸法を自動的にFCOHへ戻す機能はありません。

## Fusion

初回は次のコマンドでFcohBridgeをユーザー用AddInsフォルダへ導入します。

```powershell
pwsh -File scripts/install-fusion.ps1
```

その後Fusionを再起動し、「ユーティリティ → スクリプトとアドイン → アドイン」で
`FcohBridge` を選び、「実行」「起動時に実行」を有効にします。

Fusionのモデリング画面で、実行中の編集コマンドを完了またはキャンセルしてから:

```powershell
.\.venv\Scripts\python.exe -m fcoh.cli capabilities
.\.venv\Scripts\python.exe -m fcoh.cli build examples/mounting-bracket.fcoh.yaml --cad fusion --wait 180
```

アドインはローカルの `.runtime/queue/` を読み、FusionのCustomEventでメインスレッドへ処理を渡します。
通信ポートやクラウド保存は使用しません。Fusion自体の通常のログイン・ライセンス通信は必要です。
生成文書はFusion上に開いたまま残り、アーカイブはローカルに保存されます。
ジョブは10分で期限切れになり、期限後には生成しません。

生成ロジックはジョブごとにこのプロジェクトの固定パスから読み込みます。
ブリッジ本体を更新したときは `scripts/install-fusion.ps1` を実行し、Fusion本体を再起動してください。
この環境ではアドインの停止・実行だけだと古いPythonコードが残ることを実機確認しています。
導入済みアドインは更新前に `.runtime/backups/` へ保存します。

## Codex / MCP

Codex CLIを利用する場合、次でユーザー設定に `fcoh` として登録できます。

```powershell
pwsh -File scripts/register-mcp.ps1
```

新しく開くセッションで利用してください。既存の会話へツールが自動追加されるとは限りません。
通し検証スクリプトは同じ起動コマンドへstdioで接続します。

| ツール | 用途 |
|---|---|
| `fcoh_capabilities` | 対応範囲、FreeCADの配置、Fusionアドインの起動応答を確認 |
| `fcoh_validate(file)` | YAMLの型・単位・参照・操作順を検証 |
| `fcoh_build(file, cad)` | 新しい部品を生成。FusionはジョブIDを返す |
| `fcoh_status(job_id)` | 結果・生成ファイル・計測値を取得 |

依頼例: 「examples/mounting-bracket.fcoh.yamlを検証して、FreeCADで新規部品を生成してください。」

別のMCPクライアントでも、次のコマンドをstdioサーバーとして登録できます。

```text
command: <absolute-path-to-fcoh>\.venv\Scripts\python.exe
args: ["-m", "fcoh.server"]
```

再登録は `scripts/register-mcp.ps1`、Codex登録の解除は `codex mcp remove fcoh` です。

## 検証

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts/verify_mcp.py --cad freecad
.\.venv\Scripts\python.exe scripts/verify_geometry.py
.\.venv\Scripts\python.exe scripts/verify_mcp.py --cad fusion
.\.venv\Scripts\python.exe scripts/verify_cross_cad.py
```

`evidence/` に通し検証の結果を保存します（PC固有パスを含むためGit管理対象外）。
Fusion用の検証はアドインが動作している必要があります。
MCP検証では、部品体積を幾何学的な計算値と比較し、外形寸法、ソリッド数、操作順、
出力ファイルの実在とハッシュ、元YAMLが変更されないことを確認します。
FreeCADでは保存後のFCStdとSTEP再読込、FCOHメタデータ保持も検証します。
FusionでもF3Dを再読込して形状・履歴・FCOH属性を確認します。
両CADの成功後、STEP同士の差分ソリッド体積を比較します。

## 現在の対応範囲

- 単位mm、単一部品、XY基準面とZオフセット。
- 矩形・円の閉じたスケッチ、+Z押し出し、-Zポケット。
- `meaning` / `why` とFCOH IDをCAD内のプロパティ／属性へ保存。
- FreeCADではSpreadsheet、Fusionではユーザーパラメータにも寸法を保存。
- 押し出し長さはCADの名前付きパラメータに結び付ける。輪郭全体の寸法変更はYAMLから再生成する。

未対応の指定はエラーにします。STEP/STL入力、フィレット、面取り、スケッチ拘束の一般化、
アセンブリ、既存CAD履歴の抽出、完全な往復変換、CAD内の編集結果のFCOHへの書き戻しは未対応です。
このスキーマはFCOH全仕様を確定したものではなく、実行できる最初の部分集合です。

## 構成と再現

`fcoh/` は共通処理、`schemas/` はスキーマ、`adapters/` はCADごとの処理です。
セットアップ後、`vendor/` にFreeCADポータブル版、`.venv/` に専用Python環境が作られます。
依存関係は `uv.lock` へ固定し、OS全体のMCP SDKは変更していません。

再構築はPowerShell 7、uv、7-Zipを用意して `scripts/setup.ps1` を実行します。
FreeCAD公式アーカイブのSHA-256を照合してから展開します。

参照: [FreeCAD公式配布](https://github.com/FreeCAD/FreeCAD/releases/tag/1.1.3)、
[Fusion CustomEvent公式例](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/CustomEventSample_Sample.htm)、
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)、
[Codex MCP設定](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。
