---
title: "無料の公開Base RPCが返す-32016を、429だと思い込んで見逃していた話"
emoji: "🔍"
type: "tech"
topics: ["typescript", "jsonrpc", "base", "viem", "blockchain"]
published: true
---

## -32016とは何か

-32016は、JSON-RPC 2.0の仕様上は未定義のカスタムエラーコードで、Baseの公開RPCエンドポイント（mainnet.base.org）がレート制限超過時に返す独自コードです。メッセージには`over rate limit`という文字列が含まれます。HTTPステータスとしては200が返ってくることもあり、HTTP層だけを見ているとレート制限に気づけません。この記事では、私が開発しているオンチェーン検証ツールVouchのバックエンドで、このコードを長期間見逃していたバグの発見と修正について書きます。

## 背景：cronが定期的に500になる

Vouchはオンチェーン上の取得原価やトランザクション履歴を検証するツールで、内部的にBaseチェーンの`eth_getLogs`を大量に叩いています。無料の公開RPCを使っている都合上、レート制限は避けて通れません。そのため`chunked-logs.ts`には、範囲を分割して取得するロジックと、レート制限を検知したらリトライやバックオフをかけるロジックを実装していました。

問題が起きていたのはVercelのcronジョブです。特定のアドレスに対するログ取得だけ、ほぼ確実に500で落ちる状態が続いていました。ログを見るとエラーメッセージには`over rate limit`という文言が含まれているのに、なぜかリトライされずに例外がそのまま上まで伝播していました。

## bisectionが底を打っても直らない

このコードベースには、範囲が広すぎてRPCが一度に処理できない場合に、ブロック範囲を二分探索的に狭めていくパス（bisection）があります。正常なケースでは、範囲を半分にしていけばいずれ処理可能なサイズに収まってエラーが消えるはずです。

ところが今回のケースでは、単一ブロックまで刻んでも一向にエラーが消えませんでした。これは範囲の広さが原因ではなく、レート制限そのものが解除されていないことを意味します。つまりbisectionに落ちること自体が誤りで、本来はリトライ・バックオフのパスに入るべきエラーだったわけです。

原因を追うために、レート制限判定を担っている関数を確認しました。

```typescript
const JSON_RPC_RATE_LIMIT_CODE = -32016;

function isRateLimitError(error: unknown): boolean {
  const err = error as {
    status?: number;
    code?: number;
    details?: string;
    message?: string;
    cause?: { code?: number; details?: string };
  };
  if (err?.status === 429 || err?.code === 429 || err?.cause?.code === 429) return true;
```

この時点ではまだ-32016の定数は導入済みでしたが、実は最初に書いたバージョンではこの定数を宣言しただけで、後続の判定ロジックに組み込むのを忘れていました。つまり`isRateLimitError`は`status === 429`か`code === 429`しかチェックしておらず、Baseの公開RPCが実際に返してくる-32016は素通りしていたということです。

viemのエラーオブジェクトは、HTTPレイヤーのステータスコードとJSON-RPCレイヤーのエラーコードを別のプロパティに持ちます。`err.status`はfetchのレスポンスステータス、`err.code`（や`err.cause.code`）はJSON-RPCのレスポンスボディに含まれる`error.code`です。有料プロバイダの多くはHTTP 429を返してくれますが、mainnet.base.orgのような無料の公開ノードはHTTPステータスを200のまま、ボディの`error.code`に-32016を詰めて返してくることがあります。ここを混同していたのが今回の見逃しの本質でした。

## 修正：-32016を判定に加える

修正自体はシンプルで、既に定義していた定数を判定条件に組み込むだけです。

```typescript
if (err?.code === JSON_RPC_RATE_LIMIT_CODE || err?.cause?.code === JSON_RPC_RATE_LIMIT_CODE) {
  return true;
}
const text = `${err?.details ?? ""} ${err?.cause?.details ?? ""} ${err?.message ?? ""}`.toLowerCase();
return text.includes("rate limit");
```

最後の行で文字列の`rate limit`を含むかどうかもフォールバックとして見ているのですが、これも実は落とし穴でした。viemがラップするエラーは、`message`の中に元のHTTPリクエストURLを埋め込むことがあり、APIキーがURLのパスセグメントに含まれるプロバイダの場合、そのままログに出すとキーが漏れてしまいます。今回の修正と同時に、ログ出力前にURLをマスクする処理も入れました。

```typescript
function redactSecrets(message: string | undefined): string {
  if (!message) return "";
  return message.replace(/https?:\/\/\S+/g, "[redacted-url]");
}
```

レート制限のデバッグでエラーメッセージを生ログで出す機会が増えたタイミングだったので、これは合わせて直しておいてよかったと感じています。

## bisection側の呼び出し元も見直す

判定関数を直しただけでは根本解決になりません。呼び出し元で「レート制限エラーならリトライ、範囲エラーならbisection」という分岐が正しく機能しているかも確認しました。`fetchRange`関数側では、`isRateLimitError`がtrueを返した場合は`RATE_LIMIT_MAX_RETRIES`回まで、`RATE_LIMIT_BASE_DELAY_MS`を起点にした指数バックオフでリトライし、それ以外のエラー（レスポンスが大きすぎる系のエラーなど）だけをbisectionに回す設計になっていました。この分岐自体は元から用意されていたので、判定関数が正しく-32016を拾ってさえいれば、正常なリトライパスに入るはずだったのです。

修正後は、単一ブロックまで刻まれることなく、レート制限を検知した時点で待機とリトライに切り替わるようになり、cronの500エラーは解消しました。

## 振り返って学んだこと

今回の件で得た教訓は、レート制限の検知はHTTPステータスだけでは不十分だということです。JSON-RPCはトランスポート層とアプリケーション層でエラー表現が分離しているプロトコルなので、プロバイダによってどちらの層でエラーを返すかが異なります。有料プロバイダのAPIドキュメントだけを見て実装すると、無料の公開RPCが返す独自コードを見落としがちです。

また、範囲を狭めても解消しないエラーに遭遇したときは、それが本当に範囲起因なのかを一度疑ってみるべきだと感じました。bisectionは便利な仕組みですが、原因の異なるエラーに対して機械的に適用してしまうと、単一ブロックまで刻んでも解決しないという不自然な状態を長時間放置することになります。エラーの種類ごとに正しい対処パスへ振り分けることの重要性を、あらためて実感した一件でした。

---

気になる方はこちらからどうぞ。

https://agent-trust-tawny.vercel.app?utm_source=sen_zenn&utm_medium=cta&utm_campaign=vouch&utm_content=sen_zenn_a

※本記事の内容は2026年7月26日時点の情報にもとづきます。
