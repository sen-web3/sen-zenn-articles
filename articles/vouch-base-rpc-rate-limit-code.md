---
title: "無料の公開Base RPCが返す-32016を、429だと思い込んで見逃していた話"
emoji: "🔍"
type: "tech"
topics: ["typescript", "jsonrpc", "base", "blockchain", "web3"]
published: true
---

Vouchとは、オンチェーン上のイベントログを継続的に監視し、入金や所有権移転などのオンチェーンイベントを取りこぼさずに検知するための同期ツールです。私はこのVouchを個人開発しており、ステーブルコイン入金消込ツールSoroiやオンチェーン取得原価検証ツールVerilotの基盤としても利用しています。

今回はVouchのowner-indexer cronで起きていた、地味だけど厄介なバグの話です。無料の公開Base RPCエンドポイントであるmainnet.base.orgが返すJSON-RPCエラーコード-32016（"over rate limit"）を、レート制限エラーとして正しく検知できていなかったせいで、cronがブロック範囲を際限なく刻み続けたあげく、最終的に500エラーで落ちるという事象が起きていました。

## 何が起きていたか

Vouchのowner-indexerは`eth_getLogs`で過去ブロックのイベントを取得します。範囲が広いとRPCプロバイダ側の1リクエストあたりの上限に引っかかるため、`chunked-logs.ts`ではブロック範囲をチャンクに分割して並列取得する設計にしています。

```ts
export function getLogsChunkSize(): bigint {
  const raw = process.env.GET_LOGS_CHUNK_BLOCKS;
  if (!raw) return 2_000n;
  try {
    const size = BigInt(raw);
    return size > 0n ? size : 2_000n;
  } catch {
    return 2_000n;
  }
}
```

デフォルトは2,000ブロックずつ、`GET_LOGS_CHUNK_CONCURRENCY`で並列数を制御します（デフォルト4）。

```ts
export function getLogsChunkConcurrency(): number {
  const raw = process.env.GET_LOGS_CHUNK_CONCURRENCY;
  if (!raw) return 4;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed)) return 4;
  return Math.max(1, Math.min(8, Math.floor(parsed)));
}
```

コメントにも残していますが、250,000ブロックのキャッチアップを2,000ブロックずつ取ると約125回の往復が発生し、1回あたり150〜300msの往復でも20〜40秒の待ち時間がイベント処理ごとに積み上がります。並列fan-outはこれを緩和するための対策で、値を上げすぎるとBaseのブロック生成速度を追い越すどころかRPC側に負荷をかけるだけなので、上限は8に絞っています。

## bisectionパスの誤爆

各チャンクの取得はfetchRange内で、指数バックオフとbisection（範囲をさらに半分に刻んで再試行する処理）の2段構えになっています。bisectionは「レンジが広すぎてRPC側のレスポンスサイズ上限などに引っかかった」場合を想定したもので、レート制限とは本来まったく別の異常系として扱うべきものでした。

ここに問題がありました。レート制限かどうかを判定する`isRateLimitError`が、HTTPステータス429と`err.code === 429`しか見ていなかったのです。ところがmainnet.base.orgはHTTPレイヤーでは200を返しつつ、JSON-RPCのレスポンスボディに`code: -32016`、`message: "over rate limit"`を積んで返してきます。`err.code`に429が入ることは一度もなく、`isRateLimitError`は常にfalseを返していました。

結果として、レート制限で失敗したリクエストは「レンジが広すぎるエラー」として扱われ、bisectionパスに落ちて範囲を半分に刻んで再試行します。しかしレート制限は範囲を狭めても解消しません。単一ブロックまで刻んでもmainnet.base.orgは-32016を返し続け、最終的にバックオフとbisectionの再試行回数をすべて使い切ってエラーを投げ、owner-indexer cron全体が500で落ちていました。

## 発見の経緯

最初はレスポンスサイズか`GET_LOGS_CHUNK_CONCURRENCY`の設定を疑いました。並列数を1まで下げても事象が再現したため、並列アクセスによる輻輳ではないと分かりました。次にログを掘ってみると、bisectionで範囲がすでに1ブロックまで刻まれているのに、なお失敗し続けているケースが見つかりました。ここでようやく「これはレンジの問題ではなくレート制限そのものではないか」と疑い、失敗したエラーオブジェクトを生の形でダンプしてみました。すると、HTTPステータスは200、ボディの中に`code: -32016`が入っていることが確認できました。ステータスコードだけを見ていたら気づけなかっただろう場所にバグが潜んでいたことになります。

## 修正

`isRateLimitError`の判定条件に、JSON-RPCのエラーコード-32016と、メッセージ文字列に"rate limit"が含まれるケースを追加しました。あわせて、レート制限だと判定できた場合はbisectionではなくバックオフ（一定時間sleepしてから同じ範囲でリトライ）に倒すよう分岐を修正しています。sleepは`chunked-logs.ts`にすでに用意してあったユーティリティをそのまま流用しました。

```ts
function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
```

修正後は、mainnet.base.orgが-32016を返すバーストが来ても、範囲を刻み直すことなく一定時間待ってから同じチャンクで再試行するようになり、cronが500で落ちる事象は解消しました。あわせて`GET_LOGS_CHUNK_DELAY_MS`で各チャンク取得の間に固定の待機を挟めるようにもしてあるので、無料RPCの利用が増えて再びレート制限に当たりやすくなった場合でも、コードを触らず環境変数だけで様子見ができるようにしています。

## テストで再発を防ぐ

このバグの根本原因はエラー分類のロジックが甘かったことにあります。再試行の仕組み自体は指数バックオフもbisectionも正しく動いていて、入り口の分岐を間違えていただけでした。似た構造のバグは今後も起こり得るため、`isRateLimitError`に対しては、HTTPステータス429のケースだけでなく、-32016を含むJSON-RPCエラーオブジェクトを渡すケースのユニットテストを追加しました。実際のRPCレスポンスの形をそのままテストのフィクスチャに落とし込んでおくと、プロバイダ側の実装差異にも気づきやすくなります。

## 学んだこと

JSON-RPCのエラーはHTTPステータスコードと必ずしも一致しません。特に無料の公開RPCエンドポイントは、レート制限をHTTP 429ではなくJSON-RPCのアプリケーションエラー（-32000番台）として返すことがあります。「レート制限エラーの検知」を実装するときは、HTTPステータスだけでなくJSON-RPCのcodeフィールドとmessage文字列の両方を見ておくべきだと痛感しました。

また、bisectionとバックオフのような「似ているが原因が異なる失敗」に対する分岐処理は、判定ロジックが甘いとどちらか一方のパスにすべて吸い込まれてしまいます。今回のように再試行ロジック自体は精緻でも、エラー分類の入り口にバグがあれば意味をなさなくなるという点は、今後別のRPCプロバイダを追加するときにも忘れないようにしたいと思います。

---

気になる方はこちらからどうぞ。

https://agent-trust-tawny.vercel.app?utm_source=sen_zenn&utm_medium=cta&utm_campaign=vouch&utm_content=sen_zenn_a

※本記事の内容は2026年8月12日時点の情報にもとづきます。
