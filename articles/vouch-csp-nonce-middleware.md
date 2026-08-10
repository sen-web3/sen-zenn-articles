---
title: "script-srcのunsafe-inlineを消すために、Next.js 16のmiddlewareでリクエスト毎のnonceを配った話"
emoji: "🔐"
type: "tech"
topics: ["nextjs", "csp", "security", "middleware", "webperf"]
published: true
---

CSPのnonceとは、HTTPレスポンスヘッダのContent-Security-Policyに埋め込む使い捨てのランダム値のことで、その値と一致するnonce属性を持つscriptタグだけを実行許可する仕組みです。strict-dynamicディレクティブと組み合わせると、nonce付きで信頼されたスクリプトが動的に生成した子スクリプトまで連鎖的に許可されるため、unsafe-inlineを外した状態でも安全に動的スクリプト読み込みができます。

Vouchはオンチェーンの取得原価やトランザクション履歴を扱うツールなので、フロントエンドのXSS対策は後回しにしたくない領域です。今回はscript-srcからunsafe-inlineを消す作業の中で、Next.js 16のmiddleware（正式には`proxy.ts`という名前で呼ぶ層です）を使ってリクエストごとに新しいnonceを発行する実装をしました。ここでは実際に詰まった箇所と、その結果として下した設計判断を書きます。

## なぜunsafe-inlineを消す必要があったか

Vouchでは検証結果の要約をJSON-LDでページに埋め込んでいて、構造化データ用のinline scriptがどうしても発生します。CSPをざっくり`script-src 'self'`だけにすると、このinline scriptとNext.js自身のhydrationブートストラップscriptがどちらも実行できなくなります。よくある回避策がunsafe-inlineを許可することですが、これはXSSが発生した際に攻撃者の注入したscriptタグもそのまま実行されてしまうため、CSPを入れる意味の大部分が消えます。nonceベースに切り替えれば、攻撃者が注入したscriptにはnonce属性がないので実行されず、正規のscriptだけが動きます。

## middlewareでnonceを生成する

Next.jsのmiddlewareは全リクエストの前段で動くので、ここでnonceを生成してレスポンスヘッダに載せるのが自然な位置です。実装は`crypto.getRandomValues`で16バイトのランダム値を作り、base64にエンコードするだけです。

```ts
function generateNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
```

このnonceをCSPヘッダの`script-src`に埋め込み、同時にリクエストヘッダにも`x-nonce`として流し込んでいます。レスポンスだけでなくリクエストヘッダにも積む理由は、後段のServer Componentで`headers()`からこの値を読み出して、手動のscriptタグにnonce属性をつけるためです。

```ts
const requestHeaders = new Headers(request.headers);
requestHeaders.set("x-nonce", nonce);
requestHeaders.set("Content-Security-Policy", contentSecurityPolicy);

const response = NextResponse.next({
  request: { headers: requestHeaders },
});
response.headers.set("Content-Security-Policy", contentSecurityPolicy);
```

script-src自体はこうなっています。

```ts
`script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${isDev ? " 'unsafe-eval'" : ""}`,
```

strict-dynamicを入れているのは、Next.jsがhydration後にコード分割されたチャンクを動的にscriptタグとして挿入するからです。nonceだけだとその動的挿入分がブロックされるので、strict-dynamicで「信頼されたスクリプトが生成した子スクリプトは許可する」という挙動にしています。開発環境だけunsafe-evalを足しているのは、Fast Refreshがevalベースの機構を使うためで、本番ビルドでは外しています。

## Next.jsのhydrationブートストラップは自動で拾ってくれる

ここが最初に驚いた点です。middlewareでCSPヘッダにnonceを載せておくと、Next.js側のRSCハイドレーション用ブートストラップscriptは、こちらが何もしなくても自動的に同じnonceを自分のscriptタグに付与してくれます。App Routerのレンダラーが送出中のレスポンスヘッダを見て、nonceの値を拾ってきているためです。これは公式ドキュメントにも記載がある挙動で、React本体のレンダリング時にnonce propとして内部的に配線されています。

一方でJSON-LDのように自分で書いているinline scriptは、そんな自動配線の対象にはなりません。`src/app/faq`や`src/app/blog/[slug]`ではServer Componentの中で`headers()`を呼び、middlewareが積んだ`x-nonce`を取り出してscriptタグに手動でセットする必要があります。

## ビルド時に焼き込んだnonceが動かない理由

最初にやってしまった失敗が、nonceをビルド時に決めた固定値だと思い込んでいたことです。Next.jsのデフォルトではページは静的にプリレンダリングされる範囲があり、その場合HTMLはビルド時またはリクエスト外のタイミングで一度だけ生成され、その後は同じHTMLがキャッシュから配信されます。もしそのHTML内のnonceがビルド時点の値のまま固定されてしまうと、実行時にmiddlewareが新しく生成するCSPヘッダ側のnonceとは毎回ズレることになります。nonceはCSPの定義上、値が一致しない限りscriptは実行拒否されるので、静的生成のページでnonce付きscriptを使うと本番で確実にブロックされます。

これに気づいたのは、開発環境では動いていたのに本番相当のビルドで検証したときに、JSON-LDのscriptだけがConsoleでCSP違反として弾かれたのを見たときでした。開発サーバーは基本的に毎リクエストレンダリングされるので不整合が起きず、ビルド後の静的化が原因だと分かるまで少し時間がかかりました。

## 対象ルートを動的レンダリングに切り替える

対処として、inline scriptを含むルート（JSON-LDを埋め込んでいるFAQ・ブログ詳細と、hydrationブートストラップを持つ全ページのlayout）を動的レンダリングに切り替えました。`proxy.ts`のコメントにもその判断根拠を残しています。

```ts
// Nonces must be unique per request, so any route that renders an inline
// <script> (including the framework's own bootstrap script) must be
// dynamically rendered — see src/app/layout.tsx.
```

nonceはリクエストごとに一意である必要があるという性質そのものが、静的生成と根本的に相性が悪いということです。Next.jsではルートの`dynamic`エクスポートやレンダリング中に`headers()`を呼ぶことで動的レンダリングに切り替えられますが、いずれにしても静的最適化の恩恵は失われます。Vouchの場合、FAQやブログ詳細はもとからそこまでトラフィックが多いページではなく、CDNキャッシュ抜きになっても許容範囲だと判断しました。パフォーマンスとセキュリティのどちらを取るかの選択を、ページ単位で意識的に分けたということになります。

## connect-srcは別ディレクティブとして考える必要があった

もう一点、意外と抜け漏れやすかったのがアナリティクス計測タグでした。strict-dynamicの下ではscriptタグの挿入自体はNextのnonce付きランタイム経由なので許可されますが、そのscriptが内部で発行するfetchやXHRのビーコン送信は`connect-src`が別途制御しています。script-srcを締めた勢いでconnect-srcまで`'self'`だけにしてしまうと、計測用のビーコンだけが黙って失敗するという地味な不具合になります。

```ts
`connect-src 'self' https://plausible.io${isDev ? " ws: wss:" : ""}`,
```

devだけ`ws: wss:`を足しているのはHMRのWebSocket接続を通すためで、これも本番では外れます。

## まとめ

nonceベースのCSPは、middlewareでの生成自体は数行で終わる一方、hydrationブートストラップと手動scriptの両方に同じ値を通す配線と、静的生成との相性という2つの罠があります。特に後者は動いているように見えて本番ビルドで初めて壊れるパターンなので、CSPを締める作業をするときは必ず本番相当のビルドでConsoleを確認するようにしています。

---

気になる方はこちらからどうぞ。

https://agent-trust-tawny.vercel.app?utm_source=sen_zenn&utm_medium=cta&utm_campaign=vouch&utm_content=sen_zenn_a

※本記事の内容は2026年8月10日時点の情報にもとづきます。
