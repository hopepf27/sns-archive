# sns-archive

100％Claudeさん家Fableさん産による、sns-archiveです。  
自分の各種SNSの投稿データ、いいねデータをローカルで保存することができ、また検索を行うことができます。Twitter（現X）、Fediverse（Mastodon・Misskey）、Blueskyに対応しています。詳しくは`sns-archive/README.md`ならびに`sns-archive/linux/README_linux.md`を読んでください。  
本アプリの投稿者はコードの読み方がわかりません。**一切の責任は存在しません。**  
<br>
**※`linux/`にあるLinux版のsh等は未検証です！**

<br><br><br><br><br>

**※初回のデータ取り込み時に投稿（ポスト・ノート）数が１万以上とかある場合には、投稿データのエクスポートを行いjson等から取り込むことを推奨します。なお、すごく時間がかかります。**

<br><br><br><br><br>

## FAQ



### sync時に毎回Fediverse予備垢とか普段使わないアカウントまで読み取ろうとすることを阻止したい場合  
`config.json`の`misskey`の`"host"`とかを雑に`_host`とかアンダーバーをつければいいです（読み込まれません）。

### 何がローカルに保存されるの？
自身の投稿（動画画像含む）、twitterの場合はリツイート（画像動画などのメディア含む）といいね欄（メディアは含まない）、FediverseやBlueskyは投稿といいね（リアクション）全部（メディア含む）。


<br><br><br><br><br>

## データベースビューアのイメージ
 
メイン  
 <img width="30%" alt="aolip0j1zv png" src="https://github.com/user-attachments/assets/70bf8294-eaea-4002-a836-2e24e138af69" />  <br>

検索  
<img width="30%" alt="aoliow49zu png" src="https://github.com/user-attachments/assets/d3fb32fb-1eba-4ae5-a3cf-843927fea2ca" />  <br>

前後の文脈を探れます  
<img width="30%" alt="aoliotm2zt png" src="https://github.com/user-attachments/assets/a528f9d1-d32d-4512-a71a-88e8178dc24f" />  <br>

前後の文脈について、好きに設定できます  
<img width="30%" alt="aolios5lzs png" src="https://github.com/user-attachments/assets/cfc70fd3-dd8f-4dbc-b1d3-88a8096e996e" />  <br>



