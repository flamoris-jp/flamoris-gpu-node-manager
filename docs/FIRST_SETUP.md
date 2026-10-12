# GPU Node Manager：Updaterの入力値と導入後の設定

対象は公開済み1.2.2のインストール定義です。別の版では選定Releaseのカタログと[インストール定義](../release/install-recipe.json)を照合します。
Updaterの画面には短い項目名が表示されます。本書で用途・取得元・空欄時の動作を確認してください。
既定値は入力欄の初期値で、generate=true以外の欄を空にしても自動生成されません。
共通のDB・秘密値の扱い、配置の調べ方、作業の順番は[共通設定ガイド](https://github.com/flamoris-jp/flamoris-updater/blob/main/docs/APPLICATION_SETUP.md)にあります。

## 導入前に決めるもの

このhostで管理する既存runtime、systemd service、health確認方法とGPU解放の観測方法を確認します。
Updaterの初回入力はポートだけです。既存runtimeやモデルをインストールするアプリではありません。

## Updaterに入力する値

| 項目（キー） | 用途・値の用意 | 初期表示／空欄の動作 |
| --- | --- | --- |
| 待受ポート（PORT） | ManagerのWeb入口。ホスト上で未使用のポートを選ぶ | 8090 |

このpublic Native recipeにtoken入力・自動生成はありません。
privateな統合版／deployment overlayを選ぶ場合は、その配布元の設定を確認し、本書のservice名・profile配置を無条件に流用しません。

## 導入後にユーザー／AIが行うこと

1. UpdaterでWebを起動する。初期の空のruntime一覧は有効で、runtimeがゼロでもWebは起動する。
2. Updaterの配置情報でアプリrootを確認する。公開recipeはroot/runtimesをprofile directory、root/config/transition.lockを共通lockとして使う。
3. 実際の既存systemd service・process matcher・health契約を確認し、runtime YAMLを作成する。例は[config/examples](../config/examples)と[deployment契約](DEPLOYMENT.md)を参照する。例の名前を実機serviceと決めつけない。
4. すべてのCLI／HTTP／MCPで同じprofile directory・lock・実行identityを使うようにする。profileを反映するためManagerを再起動する。
5. 外部Web／MCP接続を使う場合は、proxy／トンネルの到達性とHost・アクセス境界をdeployment側で設定する。
6. 選定したruntimeだけで起動・停止・healthとGPU解放を確認する。

公開recipeのWeb service名はflamoris-gpu-node-manager.web.serviceです。
Updaterでの導入・Web起動はGPU runtimeの自動起動や全profileの検証を行いません。
