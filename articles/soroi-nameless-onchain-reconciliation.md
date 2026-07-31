---
title: "振込人名義が無いブロックチェーン入金を、請求書とどう自動照合するか"
emoji: "🧩"
type: "tech"
topics: ["typescript", "supabase", "web3", "blockchain", "accounting"]
published: true
---

オンチェーン入金消込とは、ブロックチェーン上で受け取ったステーブルコイン等の送金記録を、発行済みの請求書データと突き合わせて「どの入金がどの請求に対応するか」を確定させる作業のことです。銀行振込であれば振込人名義という強力な手がかりがありますが、ウォレットアドレスからの送金にはそれに相当する情報が存在しません。この違いをどう埋めるかが、Soroiのマッチングロジックを設計するうえで最初にぶつかった壁でした。

## 名義が無いという前提から設計をやり直す

銀行の入金消込ツールを作ったことがある方であれば、振込人名義の文字列マッチングを起点にロジックを組むはずです。しかしオンチェーン送金には名義欄がありません。あるのは送金元アドレス・金額・トークン種別・ブロックタイムスタンプだけです。

Soroiではこの制約を逆手に取り、最初から「名義に頼らない」設計にしました。`lib/matching.ts` は請求書（`InvoiceInput`）とトランザクション（`TransactionInput`）を受け取る純関数群として実装しています。DOMにもSupabaseクライアントにも依存しない設計にしたのは、`node --test` でマッチングロジックだけを単体検証できるようにするためです。金額のずれやタイミングのしきい値をいじるたびにDBやテストDBを立てて検証するのは非効率だと判断しました。

```ts
export interface InvoiceInput {
  id: string;
  counterpartyName: string;
  amount: number;
  currency: Currency;
  dueDate: string; // ISO date (YYYY-MM-DD)
}

export interface TransactionInput {
  id: string;
  tokenSymbol: Currency;
  amount: number;
  direction: "in" | "out";
  counterpartyAddress: string;
  blockTimestamp: string; // ISO8601
}
```

## 通貨不一致という一番地味だが一番危険なバグ

マッチングロジックを組む前に決めたのが、通貨（トークン種別）が一致しない候補は無条件で除外するというゲート条件です。USDCの請求書にUSDTの入金を紐付けてしまうと、金額がたまたま近いだけで誤マッチが成立してしまいます。

```ts
export function currenciesMatch(
  invoiceCurrency: Currency,
  transactionTokenSymbol: Currency
): boolean {
  return invoiceCurrency === transactionTokenSymbol;
}
```

このガードは自動照合（`matchInvoices` 内で `tx.tokenSymbol !== inv.currency` により除外）だけでなく、手動照合の `createManualMatch` からも呼ぶ設計にしています。実際、監査の2回目のラウンドで手動照合側にこのガードが欠落していて、異なる通貨同士を人力で強制的に紐付けられてしまう穴が見つかりました。自動照合と手動照合でロジックを別々に書いていたことが原因だったので、以降は判定関数を1つに集約し、両方の入口から同じ関数を呼ぶ形に修正しています。ロジックの二重実装は、片方だけ直して安心してしまうという典型的な事故を生みます。

## confidenceは会計上の正しさではない

マッチングの確からしさは `confidence`（0〜1）という数値で表現しています。ここで重要な設計判断は、confidenceは「突合の確からしさ」であって「会計上の正しさ」ではないという線引きを、コード上のコメントとUI文言の両方で明示していることです。税理士法52条に抵触しないよう、Soroiはあくまで機械的な一致候補を提示するだけで、税務判断や仕訳の正否には踏み込みません。

confidenceの配点は金額一致に0.6、時期の近さに0.4という基本配分にしています。ここに加えて、取引先アドレス帳（`counterparty_addresses` テーブル）に登録済みのアドレスからの入金であれば、独立した証拠として加点する仕組みを入れました。

```ts
// 既知アドレス一致時の confidence 加点。金額(0.6)＋時期(0.4)の既存配点を壊さず、
// 「同じ相手からの入金」という独立した証拠を上乗せする（上限1にクランプ）。
export const KNOWN_ADDRESS_BONUS = 0.15;
```

この加点方式にしたのは、ゲート条件（通貨一致・金額しきい値）を変えずに証拠を積み増せるからです。既存の配点ロジックをいじると過去の照合結果の意味合いが変わってしまうリスクがありますが、加点方式なら既存の結果はそのまま残り、複数候補が競合したときに既知アドレス側が貪欲割り当てで優先されるだけで済みます。しきい値調整は既存ロジックへの影響範囲を最小限に抑える方向で設計するのが安全だと感じました。

## 未知アドレスは自動確定させない

金額と時期だけで自動確定してしまうと、たまたま金額が一致した見知らぬアドレスからの入金を誤って紐付けるリスクが残ります。そこで `needsReview` というフラグを用意し、未知または類似アドレスからの入金は自動確定させず「要確認」に留める設計にしました。

```ts
export interface MatchResult {
  invoiceId: string;
  transactionId: string;
  matchRule: MatchRule;
  confidence: number;
  needsReview?: boolean;
  reviewReason?: MatchReviewReason;
}
```

このフラグは脅威モデルの検討時に洗い出した項目の1つです。金額と時期だけが根拠の場合、確度が高くても人間の目でひと目確認できるようにしておくことで、誤マッチが会計処理まで流れ込むのを防いでいます。

## サービス層で二重照合と再提案を防ぐ

`lib/matchService.ts` は純関数側のロジックを実データに適用してDBへ反映する層です。ここでは既にアクティブな照合がある請求書・トランザクションを候補から除外する `excludeActivelyMatched` と、一度却下された組み合わせを再提案しない `excludeRejectedPairs` を通してから `matchInvoices` を呼びます。

```ts
export async function autoMatchForUser(
  supabase: SupabaseServerClient,
  userId: string
): Promise<AutoMatchResult> {
  const [
    { rows: invoiceRows, error: invErr },
    { rows: txRows, error: txErr },
    { rows: linkRows, error: linkErr },
    { rows: counterpartyRows, error: cpErr },
    { rows: cpAddressRows, error: cpAddrErr },
  ] = await Promise.all([
    getInvoices(),
    getTransactions(),
    getMatchLinks(),
    getCounterparties(),
    getCounterpartyAddresses(),
  ]);
```

さらにDB側にも0009マイグレーションで「1請求書／1入金につきアクティブ照合1件」という部分UNIQUEインデックスを張っています。アプリケーション側の除外ロジックだけに頼ると、同時実行や二重呼び出しでレースコンディションが起きたときに重複INSERTが発生しうるため、DB制約とアプリロジックの二重防御にしました。衝突が起きた場合は1行ずつのupsertで検出し、その候補だけをスキップする実装にしています。

## まとめ

振込人名義が無いという制約は、一見するとマッチング精度を下げる要因に思えますが、実際には「何を根拠に自動確定してよいか」を明確に言語化するきっかけになりました。金額と時期という弱い根拠を土台にしつつ、取引先アドレス帳という強い根拠を加点方式で積み増し、未知アドレスは人間の確認に委ねる。この三段構えが、オンチェーン入金消込を実用レベルまで持っていく上での現状の設計解だと考えています。

---

ステーブルコインの入金消込を自動化するSoroiを、個人で開発しています。実装の続きはこのアカウントで書いていきます。

https://soroi-beryl.vercel.app/?utm_source=sen_zenn&utm_medium=cta&utm_campaign=soroi&utm_content=sen_zenn_b#waitlist

※本記事の内容は2026年7月31日時点の情報にもとづきます。

同じような場面で困った経験がある方がいれば、コメントかこのアカウントへの返信で、どんな入金・請求のパターンに一番時間を取られているか教えてください。次の設計判断の参考にします。
