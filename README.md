# sns-archive

　100％Claudeさん家Fableさん産による、sns-archiveです。  
　自分の各種SNSの投稿データ、いいねデータをローカルで保存することができ、また検索を行うことができます。Twitter（現X）、Fediverse（Mastodon・Misskey）、Blueskyに対応しています。  
  本アプリの投稿者はコードの読み方がわかりません。一切の責任は存在しません。  
<br>

**※初回のデータ取り込み時に投稿（ポスト・ノート）数が１万以上とかある場合には、投稿データのエクスポートを行いjson等から取り込むことを推奨します。なお、すごく時間がかかります。**

## FAQ

### Tailscaleを通じてスマホ等でも開けるようにしたい場合
  1. `start.bat`ではなく、`start-tailscale.bat`をメモ帳で編集し、`あなたのTailscaleコンソールに表示されているデバイス名`に、当該名を記入して保存。  
  2. `start-tailscale.bat`を起動。

### sync時に毎回twitterとか普段使わないアカウントまで読み取ろうとすることを阻止したい場合  
　`config.json`の`"twitter"`とか`misskey`の`"host"`とかを雑に`_host`とかアンダーバーをつければいいです（読み込まれません）。

 ### 何がローカルに保存されるの？
 　自身の投稿（動画画像含む）、twitterの場合はリツイート（画像動画などのメディア含む）といいね欄（メディアは含まない）、FediverseやBlueskyは投稿といいね（リアクション）全部（メディア含む）。
  

## データベースビューアのイメージ
 
 <img width="50%" src="https://github.com/user-attachments/assets/92091f64-e759-41e1-9c97-c1244417691f" />  

 <img width="50%"  src="https://github.com/user-attachments/assets/80b2f9f1-f808-426b-aa3e-a1b430e4dfb4" />



