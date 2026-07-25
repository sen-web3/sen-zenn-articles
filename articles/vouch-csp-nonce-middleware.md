---
title: "script-srcのunsafe-inlineを消すために、Next.js 16のmiddlewareでリクエスト毎のnonceを配った話"
emoji: "🔐"
type: "tech"
topics: ["nextjs", "csp", "middleware", "security", "typescript"]
published: true
---

CSP（Content Security Policy）とは、ブラウザに対してどのスクリプトやスタイルを実行してよいかをHTTPレスポンスヘッダで指示し、XSSなどのインジェクション攻撃の影響範囲を狭めるためのWeb標準の仕組みです。`script-src`ディレクティブに`'unsafe-inline'`を残していると、攻撃者が注入したインラインスクリプトも「許可された」ものとして実行されてしまうため、CSPを強めに設定していても実質的な防御効果は大きく削がれてしまいます。

私が開発しているオンチェーン向けの検証ツールVouchでも、当初は`script-src 'self' 'unsafe-inline'`という緩めのポリシーで運用していました。理由は単純で、Next.jsのハイドレーション用ブートストラップスクリプトと、SEO用に手動で埋め込んでいたJSON-LDのインラインscriptタグの両方を許可する必要があったからです。今回はこれを`nonce` + `strict-dynamic`に切り替えた際の実装と、途中で踏んだ落とし穴を書き残します。

## nonceをリクエスト毎に生成する

方針はシンプルで、Next.jsのmiddleware（Next.js 16では`proxy.ts`という名前で扱われます）でリクエストごとにランダムなnonceを生成し、CSPヘッダに埋め込みます。

```ts
function generateNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
```

`crypto.getRandomValues`はEdge Runtimeでも動くWeb Crypto APIなので、Node依存の`crypto.randomBytes`を使わずに済みます。16バイトのランダム値をBase64エンコードしてnonceとして使う、というのはCSP仕様でよく見る実装パターンです。

生成したnonceは2箇所に渡します。1つはレスポンスヘッダのCSP自体、もう1つはリクエストヘッダの`x-nonce`です。後者はServer Componentから`headers()`経由で読み出して、JSON-LDのscriptタグに手動でnonce属性をセットするために使います。

```ts
const requestHeaders = new Headers(request.headers);
requestHeaders.set("x-nonce", nonce);
requestHeaders.set("Content-Security-Policy", contentSecurityPolicy);

const response = NextResponse.next({
  request: { headers: requestHeaders },
});
response.headers.set("Content-Security-Policy", contentSecurityPolicy);
```

`NextResponse.next()`に`request.headers`を渡すのがポイントで、これをやらないと下流のServer Componentからは`x-nonce`が見えません。レスポンス側にもCSPヘッダをセットしているのは、実際にブラウザへ送るヘッダとリクエスト内部で使い回すヘッダを分けて管理したかったためです。

## strict-dynamicで縛る

`script-src`の中身は次のようにしています。

```ts
`script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${isDev ? " 'unsafe-eval'" : ""}`,
```

`strict-dynamic`を付けると、nonceが付与されたスクリプトから動的に挿入された子スクリプトは、ホワイトリストの個別指定なしに信頼されるようになります。Next.jsのランタイムがチャンクを動的にロードする挙動と相性がよく、`'self'`だけでドメイン管理する場合よりも柔軟です。開発環境では`'unsafe-eval'`をFast Refresh用に許可していますが、本番ビルドでは外しています。

計装用に入れているPlausibleのビーコン送信は`script-src`ではなく`connect-src`側の話なので、コメントで明示的に区別して書いています。ここを混同すると「スクリプトは動くのにfetchだけ弾かれる」という切り分けにくい不具合になりがちです。

```ts
// https://plausible.io: 全社日次反応レポート向け計装。script自体は
// strict-dynamic下でNextのnonce付きランタイムからの動的挿入として許可されるが、
// ビーコン送信(fetch/XHR)は connect-src が別途governsするため明示許可が必須。
`connect-src 'self' https://plausible.io${isDev ? " ws: wss:" : ""}`,
```

## JSON-LDへのnonce注入と、ビルド時焼き込み問題

VouchではFAQページとブログ記事詳細（`src/app/faq`と`src/app/blog/[slug]`）でJSON-LDを手動のscriptタグとして埋め込んでいます。ここに`x-nonce`ヘッダの値を渡すだけなら簡単に見えますが、最初は本番ビルドで動かしてもCSP違反がブラウザコンソールに出続けました。

原因はNext.jsの静的最適化です。対象のルートが静的にプリレンダリングされていると、ビルド時点で一度だけ生成されたnonceがHTMLに焼き込まれてしまい、実行時にmiddlewareが発行する新しいnonceとは一致しません。nonceは仕様上「リクエストごとに一意」でなければ意味がないため、ビルド時に固定された値は本質的にCSPのセキュリティ効果を持たなくなります。

対処として、インラインscriptを持つルート（ブートストラップスクリプトを含むルートすべて）を動的レンダリングに切り替えました。`layout.tsx`側で動的レンダリングを強制する設定を入れ、Next.jsのRSCハイドレーションスクリプトとJSON-LDの両方が、常にそのリクエストのCSPヘッダと同じnonceを参照する状態にしています。静的生成によるTTFB・キャッシュ効率のメリットは一部失いますが、CSPを`strict-dynamic`まで締める以上はトレードオフとして許容しています。

## まとめ

middlewareでのnonce生成自体はランダムなバイト列をBase64にするだけの数行ですが、実際に効かせるには「どこにnonceを配るか」と「Next.jsのレンダリング戦略がその値の一貫性を壊していないか」を両方確認する必要がありました。特に静的プリレンダリングとリクエスト毎nonceの相性の悪さは、CSPヘッダだけを見ていると気づきにくく、ブラウザ側の違反ログとNext.jsのレンダリング設定を突き合わせて初めて原因が特定できました。CSPを`unsafe-inline`から`strict-dynamic`へ移行する作業をしている方の参考になれば幸いです。

---

気になる方はこちらからどうぞ。

https://agent-trust-tawny.vercel.app?utm_source=sen_zenn&utm_medium=cta&utm_campaign=vouch&utm_content=sen_zenn_a

※本記事の内容は2026年7月25日時点の情報にもとづきます。
