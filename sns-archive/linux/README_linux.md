# Linux での使い方

Ubuntu 等の Linux でサーバとして動かすためのスクリプト集です。
プログラム本体（py・データ・設定）は Windows 版と完全に共通で、
このフォルダには Linux 用の起動・同期・常駐化スクリプトだけが入っています。

## 1. セットアップ

```bash
cd sns-archive
./linux/setup.sh          # 仮想環境(venv-linux)の作成と依存のインストール
nano config.json          # トークン等を記入（Windowsで使っていたものをそのまま使えます）
```

- 必要なもの: Python 3.9 以降と venv（無ければ `sudo apt install python3 python3-venv`）
- 仮想環境は `venv-linux/` に作られ、Windows の `venv/` とは共存できます
- Windows で使っていたフォルダをそのままコピーして使えます
  （`archive.db`・`media/`・`config.json` は共通。両OSで**同時に**開かないでください）

## 2. ビューアの起動

手動起動（お試し用・Ctrl+C で終了）:

```bash
./linux/start.sh
```

常時起動（推奨・サーバ用）:

```bash
./linux/install_service.sh
```

systemd のユーザーサービスとして登録され、**クラッシュ時の自動再起動**と
**OS 再起動後の自動開始**が有効になります。

```bash
systemctl --user status sns-archive      # 状態確認
journalctl --user -u sns-archive -f      # ログを見る
./linux/uninstall.sh                     # 登録解除
```

## 3. 他の端末から見る（Tailscale）

このアーカイブには **DM 等の非公開投稿も含まれる**ため、インターネットや
LAN への公開はせず、Tailscale の閉じた網の中でだけ見る運用を推奨します。

### 方法A: tailscale serve（推奨）

アプリは `127.0.0.1` のまま一切触らず、Tailscale に中継してもらう方式です。
アプリ自体は他のネットワークから物理的に到達不能で、tailnet の端末だけが
HTTPS 付きでアクセスできます。

```bash
sudo tailscale serve --bg 5089
tailscale serve status        # 表示されたURL（https://<マシン名>.<tailnet名>.ts.net/）を開く
```

やめるとき: `sudo tailscale serve --bg=false 5089`

### 方法B: Tailscale の IP に直接バインド

`config.json` に、このマシンの Tailscale IP（100.x.x.x）を書く方式です。

```json
  "host": "100.64.xx.xx",
```

設定後にサービスを再起動: `systemctl --user restart sns-archive`
tailnet の端末から `http://100.64.xx.xx:5089/` で開けます。
（この方式は HTTP のままです。tailnet 内は暗号化されるので実用上は問題ありません）

### してはいけないこと

`"host": "0.0.0.0"` は同じ LAN の全端末に公開されます。DM を含む本アプリでは
使わないでください（起動時にも警告が表示されます）。

## 4. 毎日 03:45（日本時間）の自動同期

```bash
./linux/install_timer.sh                    # 4サービスすべて
./linux/install_timer.sh misskey mastodon   # 特定サービスだけにする例
```

systemd タイマーとして登録されます。`OnCalendar` に日本時間を明示しているので、
**サーバ本体のタイムゾーンが JST でなくても正しく 03:45 JST** に実行されます。
電源が入っていなかった日の分は、次回起動時に追いかけて実行されます（Persistent）。

```bash
systemctl --user list-timers sns-archive-sync.timer   # 次回の実行予定
systemctl --user start sns-archive-sync.service       # 今すぐ手動実行
journalctl --user -u sns-archive-sync                 # 同期ログ
```

対象を変えたいときは、もう一度 `install_timer.sh` を実行し直すだけです。

### cron 派の場合（代替）

systemd を使わない場合は crontab に次の 2 行を足しても同じことができます:

```
CRON_TZ=Asia/Tokyo
45 3 * * * /フルパス/sns-archive/linux/sync_all.sh
```

（`CRON_TZ` 非対応の cron ではサーバを JST に設定するか、時差を換算してください）

## 5. 手動での同期・全件取得

```bash
./linux/sync_all.sh                  # 4サービスすべて（差分同期）
./linux/sync_misskey.sh              # 個別に
./linux/sync_all.sh --full           # 全件を取得し直す（各サービスに --full が渡る）
./linux/sync_all.sh misskey --full   # 組み合わせも可
```

## 6. トラブルシューティング

- **`Failed to connect to bus` と出る** … SSH 接続によっては
  `export XDG_RUNTIME_DIR=/run/user/$(id -u)` が必要です
- **再起動後にサービスが起きない** … `sudo loginctl enable-linger $USER`
  を一度実行してください（install_service.sh が自動で試みますが、
  環境により sudo が必要です）
- **ポートを変えたい** … `config.json` の `"port"` を変更し、
  `systemctl --user restart sns-archive`。tailscale serve の番号も合わせてください
