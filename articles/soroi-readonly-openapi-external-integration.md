---
title: "会計ソフト連携のための読み取り専用OpenAPI、公開範囲をどう線引きしたか"
emoji: "📖"
type: "tech"
topics: ["openapi", "typescript", "supabase", "api", "accounting"]
published: true
---

OpenAPIとは、REST APIのエンドポイント・リクエスト・レスポンス・エラー形式を機械可読な形式で記述する仕様書規格です。バージョン3.1ではJSON Schemaとの互換性が強化され、AIエージェントや外部クライアントがドキュメントを読み込んでAPI呼び出しを組み立てることが現実的になりました。ステーブルコイン入金消込ツールSoroiでは、会計ソフトやBIツールからバッチ・定期ポーリングで読ませる前提のREST APIを設計する際に、OpenAPI 3.1形式の仕様書を自前で組み立てています。今回はその公開範囲をどう線引きしたかを書きます。

## 読み取り専用に絞った理由

SoroiのAPIは `app/api/v1/*` 配下にあり、外部会計ソフトが仕訳データを取り込むための窓口です。最初に決めたのは、書き込み系エンドポイントを一切含めないことでした。会計ソフト側から見るとSoroiは「入金消込済みのデータを持ってくる先」であって、外部から状態を変更される必要がない対象です。書き込みAPIを用意すると、認証が漏れた場合の被害範囲が消込データの改ざんにまで広がります。読み取り専用に限定すれば、最悪でもデータの閲覧に留まるという設計上の防御ラインを引けます。

この方針はAPIの実装だけでなく、OpenAPI仕様書のビルダー自体にも表れています。`lib/openapi.ts` は純関数でJSON互換の値を組み立てるだけで、DBやネットワークに一切依存しません。

```typescript
// OpenAPIドキュメントはネスト構造が深く形も多様なため、厳密な型定義は
// 持たず JSON 互換の値として組み立てる（配信するだけで内部から参照しない）。
type Json = { [key: string]: JsonValue };
type JsonValue = string | number | boolean | null | JsonValue[] | Json;
```

配信専用の値であって内部ロジックからは参照されない、という制約を型コメントで明示しているのは、仕様書ビルダーが肥大化して実装に影響を与えないようにするための線引きです。

## 数値は仕様書に手書きしない

もう一つの判断は、レート制限やページングの上限値を仕様書側に直接書かないことです。実装側の定数を唯一の情報源（SSOT）として、仕様書はそこから組み立てる形にしています。

```typescript
import {
  DEFAULT_PAGE_LIMIT,
  MAX_PAGE_LIMIT,
  MAX_PAGE_OFFSET,
  API_RATE_LIMIT_PER_MINUTE,
  API_RATE_LIMIT_WINDOW_SECONDS,
  API_ERROR_DETAILS,
} from "./apiRequest.ts";
import { FORMAT_META, type AccountingFormat } from "./exportJournal.ts";
import { API_KEY_PREFIX } from "./apiKeys.ts";
```

これをやらないと、実装側でレート制限の値を変更したときに仕様書だけ古い数字が残るという事故が起きます。定期ポーリングを前提にしたAPIでは、クライアント側がこの数字を見てポーリング間隔を決めるため、乖離はそのままクライアントの実装ミスにつながります。実際にレスポンス本文にも数値を埋め込んでいます。

```typescript
"429": {
  description: `Rate limit exceeded (${API_RATE_LIMIT_PER_MINUTE} requests per ${API_RATE_LIMIT_WINDOW_SECONDS} seconds per API key, fixed window). The Retry-After response header tells how many seconds to wait before retrying.`,
```

fixed window方式である点まで文章化しているのは、BIツール側の実装者がリトライ戦略を組む際に「何分の何秒で区切られるウィンドウなのか」を推測させないためです。曖昧な記述にすると、クライアント側が必要以上に保守的なリトライ間隔を取ったり、逆に叩きすぎて429を連発したりします。

## スコープとエラーコードの最小化

APIキーの認証エラーについても、種類を絞った上でそれぞれ明確に区別しています。

```typescript
function commonErrorResponses(): Json {
  return {
    "401": {
      description:
        "Unauthorized. `missing_api_key`: no Bearer token in the Authorization header. `invalid_api_key`: the key is unknown or has been revoked.",
```

`missing_api_key` と `invalid_api_key` を分けているのは、クライアント側のデバッグを楽にするためです。トークンを送り忘れているのか、キーが失効しているのかを判別できないと、外部の実装者は「認証まわりで何かがおかしい」としか分からず、問い合わせが増えてしまいます。APIキー自体もプレフィックス付きで発行しており、そのプレフィックス定義も `lib/apiKeys.ts` からimportして仕様書に反映しています。スコープはAPIキー単位で読み取り専用に固定しており、書き込み権限を持つキーという概念自体を存在させていません。権限フラグで書き込みを禁止するのではなく、書き込みエンドポイントそのものを実装しないことで、設定ミスによる権限昇格のリスクを構造的に消しています。

## 仕様書と実装の乖離をテストで防ぐ

自前でOpenAPI仕様書を組み立てる方式には、仕様書だけ更新して実装を直し忘れる、あるいはその逆が起きるリスクがあります。これに対してSoroiでは `tests/unit/openapi.test.ts` で、ルート実装のインターフェース定義と仕様書のスキーマが一致することを機械検証しています。仕様書ビルダーがDB・ネットワーク非依存の純関数であるため、Supabaseの接続なしに `node --test` だけで検証が完結します。CI上で毎回この一致を確認できることが、読み取り専用APIを外部に公開し続ける上での安心材料になっています。

会計ソフト連携のAPIは、一度クライアントに組み込まれると仕様変更の影響範囲が読みにくくなります。だからこそ公開範囲は最初に絞り込み、絞った後の挙動はSSOTとテストで固定する、という順番で設計しました。読み取り専用・レート制限・スコープ最小化のどれも、書けば済む機能追加ではなく、外部に見せる情報をどこで止めるかという線引きの積み重ねだったと感じています。

---

ステーブルコインの入金消込を自動化するSoroiを、個人で開発しています。気になる方はこちらからどうぞ。

https://soroi-beryl.vercel.app/?utm_source=sen_zenn&utm_medium=cta&utm_campaign=soroi&utm_content=sen_zenn_a#waitlist

※本記事の内容は2026年8月5日時点の情報にもとづきます。

同じような場面で困った経験がある方がいれば、コメントかこのアカウントへの返信で、どんな入金・請求のパターンに一番時間を取られているか教えてください。次の設計判断の参考にします。
