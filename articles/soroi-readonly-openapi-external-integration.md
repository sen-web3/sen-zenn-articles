---
title: "会計ソフト連携のための読み取り専用OpenAPI、公開範囲をどう線引きしたか"
emoji: "📖"
type: "tech"
topics: ["openapi", "typescript", "supabase", "api", "accounting"]
published: true
---

## OpenAPIとは

OpenAPIとは、REST APIの仕様をJSONやYAMLで機械可読な形式に記述するための業界標準規格です。エンドポイント・パラメータ・レスポンススキーマ・エラーコードなどを一つのドキュメントにまとめることで、人間だけでなくAIエージェントや外部システムからもAPIの挙動を機械的に把握できるようにします。Soroiでは、外部の会計ソフトやBIツールがバッチ処理や定期ポーリングでデータを取得するための読み取り専用API（`/api/v1/*`）を提供しており、その仕様書をOpenAPI 3.1形式で `/api/v1/openapi.json` から配信しています。

この記事では、なぜ書き込み系エンドポイントを一切含めない設計にしたのか、レート制限やスコープをどう線引きしたのかを、実装コードを見ながら書いていきます。

## 読み取り専用に絞った理由

会計ソフト連携と聞くと、仕訳の作成や更新まで含めたAPIを思い浮かべる方もいるかもしれません。ですが、外部の会計ソフトやBIツール側からSoroiのデータを「読みに来る」ユースケースに絞って考えると、書き込み系のエンドポイントは不要であるどころか、むしろリスクの方が大きくなります。

連携先はバッチジョブや定期ポーリングで動くことが多く、APIキーが漏洩した場合の被害範囲を考えると、書き込み権限を持たせない設計は攻撃面を最初から消しておけるという意味で費用対効果の高い判断だと考えています。加えて、読み取り専用であれば冪等性やリトライの設計もシンプルになり、連携する外部システム側の実装コストも下がります。Soroi側では `app/api/v1/*/route.ts` にGETエンドポイントだけを実装し、PUTやPOSTに相当する仕訳の書き込みは通常のWeb UI経由のフローに閉じています。

## 仕様書を実装から自動生成する

OpenAPI仕様書を手書きすると、実装との乖離がどこかで起きます。ページネーションのデフォルト値を実装側だけ変えて仕様書を直し忘れる、エラーコードの文言がずれる、といった事故は身に覚えのある方も多いのではないでしょうか。

Soroiでは `lib/openapi.ts` を純関数として実装し、数値やenumを手書きせず、実装側のSSOT（Single Source of Truth）から組み立てる方針にしました。

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

ページネーションの上限値もレート制限の数値もエラーメッセージも、すべて `lib/apiRequest.ts` や `lib/exportJournal.ts` からimportしています。仕様書側で定数を再定義することはありません。これにより、実装側の定数を変更すれば仕様書側も自動的に追随する形になっています。

さらに `tests/unit/openapi.test.ts` で、ルート実装のインターフェース定義と仕様書のスキーマが一致することを機械検証しています。仕様書だけ直して実装を直し忘れる、あるいはその逆の乖離を、テストで機械的に潰す構成です。DBやネットワークに依存しない純関数なので、`node --test` だけで検証が完結する点も地味に効いています。

CIで毎回このテストが走るので、仕様書のドリフトに気づかないままリリースしてしまう心配がありません。

## import拡張子の小さな詰まりどころ

このファイルだけ `lib` 内の値importに拡張子（`.ts`）を明示しています。理由は単体テストを素のNode ESM解決（`node --test`）から実行するためです。拡張子なしの相対importはNode ESMでは解決できないため、テストから直接importできるよう `.ts` を付けています。tsconfigの `allowImportingTsExtensions`（`noEmit` 時のみ許可されるオプション）を使うことでこれを成立させています。ビルドツール前提でimportを書いていると見落としがちな部分で、実は最初は他のファイルと同じ書き方をしていてテストが通らず、原因を追うのに少し時間がかかりました。

## レート制限の設計

読み取り専用とはいえ、バッチやポーリングを想定するとレート制限は必須です。連携先が想定より高頻度でポーリングしてくるケースや、実装ミスでループしてしまうケースを防ぐためです。

```typescript
"429": {
  description: `Rate limit exceeded (${API_RATE_LIMIT_PER_MINUTE} requests per ${API_RATE_LIMIT_WINDOW_SECONDS} seconds per API key, fixed window). The Retry-After response header tells how many seconds to wait before retrying.`,
  headers: {
    "Retry-After": {
      description: "Seconds to wait before retrying.",
      schema: { type: "integer" },
    },
  },
```

固定ウィンドウ方式でAPIキー単位のレート制限をかけ、429を返す際は `Retry-After` ヘッダーで待機秒数を明示しています。この値も `API_RATE_LIMIT_PER_MINUTE` と `API_RATE_LIMIT_WINDOW_SECONDS` という定数から仕様書の説明文に埋め込んでいるため、レート制限の閾値を変更した際に仕様書の記述だけ古いままになる事故を防げます。

会計ソフト側の実装者は、この仕様書を見るだけで「1分間に何回叩けるか」「429を受けたら何秒待てばいいか」が分かる状態になっています。バッチ処理を書く側にとって、この情報がドキュメントに機械可読な形で載っているかどうかは、実装のしやすさに直結する部分だと思います。

## エラーレスポンスとスコープの最小化

認証必須エンドポイントに共通するエラーレスポンスも、実装側の定数から組み立てています。

```typescript
"401": {
  description:
    "Unauthorized. `missing_api_key`: no Bearer token in the Authorization header. `invalid_api_key`: the key is unknown or has been revoked.",
  content: {
    "application/json": {
      schema: { $ref: "#/components/schemas/Error" },
      examples: {
        missing_api_key: {
          value: { error: "missing_api_key", detail: API_ERROR_DETAILS.missing_api_key },
        },
```

APIキーは `lib/apiKeys.ts` の `API_KEY_PREFIX` で識別可能な形式にし、`lib/apiAuth.ts` の `authenticateApiKey` で検証、失敗時は `apiAuthFailureResponse` で401を返す構成です。スコープの最小化という観点では、APIキー自体に書き込み権限を持たせる余地をそもそも作らないことが、一番シンプルな線引きになりました。権限フラグを持たせて制御するより、エンドポイント自体をGETのみに限定する方が、実装ミスによる権限昇格のリスクを構造的に排除できると考えています。

## まとめ

読み取り専用APIの設計は、機能を削るだけの後ろ向きな判断に見えるかもしれません。ですが会計データという性質上、外部からの書き込みを受け付けないことは信頼性の担保そのものです。加えてOpenAPI仕様書を実装側のSSOTから自動生成し、テストで一致を機械検証する構成にしたことで、仕様書と実装の乖離という別のリスクも同時に潰せています。Supabase上のデータをバッチで読ませたいという外部連携の要求に対して、書き込みを許さない、レートを制限する、仕様書を実装と一致させ続ける、という三点が、Soroiの読み取り専用APIを支える基本方針です。

---

ステーブルコインの入金消込を自動化するSoroiを、個人で開発しています。気になる方はこちらからどうぞ。

https://soroi-beryl.vercel.app/?utm_source=sen_zenn&utm_medium=cta&utm_campaign=soroi&utm_content=sen_zenn_a#waitlist

※本記事の内容は2026年7月24日時点の情報にもとづきます。

同じような場面で困った経験がある方がいれば、コメントかこのアカウントへの返信で、どんな入金・請求のパターンに一番時間を取られているか教えてください。次の設計判断の参考にします。
