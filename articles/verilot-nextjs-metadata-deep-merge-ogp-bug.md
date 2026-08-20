---
title: "og:imageは設定したのにリンクカードが出ない ― Next.jsのmetadataがネストしたオブジェクトをdeep mergeしない罠"
emoji: "🖼"
type: "tech"
topics: ["nextjs", "opengraph", "metadata", "typescript", "seo"]
published: true
---

og:imageとは、OGP（Open Graph protocol）でSNSやチャットツールがリンクカードに表示する画像を指定するメタタグのことです。Next.jsのMetadata APIでは、`app/opengraph-image.png`のようなファイル規約か、`metadata`オブジェクトの`openGraph.images`フィールドのどちらかで指定できます。今回はこの2つを併用したことが原因で、Verilotの全9ルートでog:imageが黙って消えるという不具合を踏みました。2026-07-25に調査して修正した記録です。

## 症状：ファイルは置いているのにカードが出ない

Verilotはオンチェーンの取得原価チェーンがどこで切れているかを診断するツールで、診断結果をXでシェアしてもらう導線を用意しています。ところが2026-07-25にXでシェアした自分のリンクを見て気づきました。カードにog:imageが出ていないのです。トップページだけでなく、診断フローのどのルートでも同じ状態でした。

まず疑ったのはファイル規約の設定漏れでした。`app/opengraph-image.png`と`app/twitter-image.png`は実際に配置済みで、`file`コマンドで確認してもちゃんとPNGとして読めます。ファイルが壊れているわけではありませんでした。

## layout.tsxの中身

`app/layout.tsx`にはこう書いてありました。

```tsx
openGraph: {
  title: "Verilot",
  description: SITE_DESCRIPTION,
  url: SITE_URL,
  type: "website",
  siteName: "Verilot",
},
twitter: {
  card: "summary_large_image",
  // 2026-07-29 external audit: attribute cards to the @verilot account
```

この`metadata`はルートレイアウトにあるので、原理上は配下の全ルートに継承されるはずです。加えて`app/opengraph-image.png`と`app/twitter-image.png`というファイル規約もルート直下にあるので、Next.jsが自動的にこれらを`openGraph.images`と`twitter.images`に注入してくれる仕組みになっています。ここまでは正しく動作していました。実際、curlでルートレイアウト直下のパスだけを見ると`og:image`は出ていました。

問題は9つある各ルート（`app/r/[id]/page.tsx`など）側にありました。それぞれが独自に`openGraph`と`twitter`オブジェクトを持つ`metadata`をエクスポートしていて、タイトルや説明文をルートごとに出し分けていたのです。ここに`images`の指定が入っていませんでした。

## 原因：Next.jsのmetadataマージはshallow

Next.jsのMetadata APIは親から子へmetadataをマージしますが、これはトップレベルのキー単位のマージであって、ネストしたオブジェクトの中身までは合成してくれません。つまり子ルートが`openGraph: { title: "..." }`のように`openGraph`キーを再定義すると、親の`openGraph`オブジェクトごと丸ごと置き換わってしまいます。親の`openGraph.images`（ファイル規約から自動注入されたもの）も、子の`openGraph`に含まれていなければそのまま消えます。`twitter`キーも同様です。

自分は最初「ファイル規約の画像はレイアウトの外側で常に効くグローバル設定」だと思い込んでいました。しかし実際にはファイル規約由来の画像も、内部的には対応する`metadata.openGraph.images`／`metadata.twitter.images`へのマージという扱いになっていて、子ルートが同じキーを明示的に上書きすると一緒に消えてしまう仕様でした。deep mergeされないのはNext.jsのApp Router全般の挙動で、ドキュメントにも「配列やオブジェクトはマージされずoverwriteされる」旨の記載はあります。ただ、ファイル規約の画像もこの対象になることまでは、実際に踏むまで理解していませんでした。

9ルート全部で同じ症状が出ていた理由も、これで説明がつきます。全ルートが同じパターン（ルートごとにタイトルと説明文を出し分けるため`openGraph`と`twitter`を独自定義する）を踏襲していたので、同じ理由で全滅していたわけです。開発中にSNSカードのプレビューを毎回確認していなかったこと、`<title>`や`<meta description>`自体は正しく出ていたので気づきにくかったことも、発見が遅れた要因でした。

## 修正：各ルートに明示的にimagesを指定

対処自体はシンプルで、各ルートの`metadata.openGraph`と`metadata.twitter`に`images`を明示的に持たせるだけです。ファイル規約への自動注入に頼らず、各ルートが自分の画像URLを自分で持つ形に変えました。あわせて`twitter.card`が一部ルートで未指定だったので、全ルート`summary_large_image`に統一しています。この修正はcommit `9637f87`で入れました。差分としては9ファイルすべての`metadata`エクスポートに`images`フィールドを1行足しただけですが、原因特定にかかった時間の方がよほど長い作業でした。

## curlでのUA別実測

修正が効いているかは、ブラウザのプレビューツールに頼らずcurlで直接HTMLを取得して確認するのが確実です。SNSやチャットツールのクローラーはUser-Agentで挙動を変えることがあるため、対象ごとにUAを偽装して叩きます。

```bash
curl -A "Twitterbot/1.0" https://verilot.app/r/xxxx | grep 'property="twitter'
curl -A "facebookexternalhit/1.1" https://verilot.app/r/xxxx | grep 'property="og:'
curl -A "Slackbot-LinkExpanding" https://verilot.app/r/xxxx | grep 'property="og:'
```

修正前はこれらのコマンドで`og:image`と`twitter:image`のタグ自体が出力に現れませんでした（`og:title`や`og:description`は出ていたので、`openGraph`オブジェクト自体は生きていて`images`だけが欠落しているのが分かります）。修正後は全9ルートで`og:image`と`twitter:image:src`が期待通りに返るようになり、UAを変えても差がないことを確認しました。

## まとめ

Next.jsのMetadata APIでファイル規約（`opengraph-image.png`／`twitter-image.png`）とルートごとの`metadata.openGraph`／`metadata.twitter`を併用する場合、後者を定義した瞬間に前者由来の`images`が上書きされて消えるという点は、公式ドキュメントを流し読みしただけでは見落としやすい仕様だと感じました。ルートレイアウトに画像を1枚置けば全ページに効くという思い込みは捨て、タイトルや説明文をルートごとに出し分けるなら`images`も含めて`openGraph`オブジェクトを毎回完結させる、という運用に倒すのが安全です。SNSシェアの導線があるプロダクトでは、リリース前にcurlでUAを変えて`og:image`の有無を機械的に確認する工程を入れておくと、今回のような「タイトルは出るのに画像だけ消える」不具合を早い段階で拾えるはずです。