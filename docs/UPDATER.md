# Repository-owned Updater release 1.2.1

このリポジトリがソース・ビルド・Release・配布物・インストールカタログを管理します。Updaterは配布物を集め直さず、カタログに記載したこのリポジトリのReleaseから、選択したアプリとCPU向けのファイルだけを取得します。

公開成功後のカタログURL：
`https://github.com/flamoris-jp/flamoris-gpu-node-manager/releases/download/v1.2.1/catalog.json`

`.github/workflows/publish-updater-release.yml` はこのアプリだけをamd64/arm64でビルドし、固定image IDまたは完全なoffline wheelhouse、ファイルのSHA-256、設定と起動方法をカタログへ記録します。`release/install-recipe.json` がアプリ所有のインストール定義です。公開前にUpdater自身の型と実際のprofile展開処理で検証します。検証用のUpdater依存はCIツール環境だけに入れ、アプリのランタイムへは追加しません。

Release/tagが存在する場合は上書きしません。以前の集中配布と別のパッチ版で、URL・バイト列・カタログ内容の衝突を避けます。`compatible_from` は空で、新規導入向けです。DB/データの移行を確認していない旧版への更新は宣言しません。

Native方式です。初回インストールはWebのみを起動し、GPUランタイムの起動は行いません。実際のsystemdサービスを指定するruntimeプロファイルは運用側で設定します。

Updater 1.0.4へ更新後、このURLをカタログ画面へ登録して個別に導入します。アプリ自身の接続トークン・DBパスワード等は必要な設定であり、Updaterのログイン認証とは別です。初回導入後の設定、実際のプロキシ/トンネル、GPU・モデル動作は運用時に確認します。CI・公開成功を実機受入として報告しません。設定・DB・データは実行ファイルの更新対象から分離します。

従来の高度なOwner/移行用SDKは内部統合用としてソースに残りますが、通常のこの新規インストールにOwnerプロファイル・mTLS・DBバックアップ・旧配置の取り込みは要求しません。
