---
title: "ERC-20の取得原価をオンチェーンデータだけから復元する ― 1099-DA文脈の検証エンジン実装"
emoji: "🧮"
type: "tech"
topics: ["ethereum", "web3", "typescript", "blockchain", "tax"]
published: true
---

取得原価（cost basis）とは、ある資産をいくらで取得したかを示す金額です。売却時にこの金額と売却額の差分を取ることで、実現損益が計算できます。ERC-20トークンの場合、取引所を経由した売買であれば取引所がこの原価を記録してくれますが、DEXスワップやブリッジ、エアドロップなど取引所を介さないオンチェーン移転が絡むと、原価情報がどこにも残らず「幻の含み益」が発生しがちです。私はこの問題を解くためにVerilotというオンチェーン取得原価検証ツールを作っています。本稿ではその内部実装を、ログ取得→正規化→分類→原価計算という処理順に沿って解説します。

## なぜオンチェーンだけで完結させる必要があるか

米国では2025年分の暗号資産取引から1099-DAという新しいフォームが適用され、ブローカー（取引所）は顧客の取引を報告する義務を負います。しかし1099-DAが機能するのは取引所の内部で完結した取引だけです。ウォレット間送金、DEXでのスワップ、L2ブリッジ、そしてエアドロップは取引所のシステムの外で起きるため、原価が引き継がれるかどうかは利用者自身が検証しなければなりません。Verilotが目指しているのは「取引所のレポートと、オンチェーンの実態が一致しているか」を第三者的に検算できる状態を作ることです。そのために、取引所APIには一切頼らず、公開されているブロックエクスプローラのデータだけからロット単位の原価を復元する設計にしています。

## ステージ1: ログ取得（Blockscout API）

最初のステージはBlockscoutのEtherscan互換エンドポイントから、ネイティブ送金・内部トランザクション・ERC-20 Transferログを取得する処理です。ここで一度、痛い目に遭いました。ページサイズを明示しないままリクエストしたところ、あるインスタンスがアドレスの全履歴を一括で返してくることが分かり、vitalik.ethのtokentxで実測376MBのレスポンスが返ってきてサーバーレス関数がOOMで落ちました。

```ts
// 2026-07-29 — the page size used to be implicit. The code assumed the
// etherscan-compat API caps a page at 10,000 rows; it does not. Without an
// `offset` parameter this instance returns the address's ENTIRE history in
// one body (measured: 376 MB of `tokentx` for vitalik.eth), which killed the
// serverless function with an out-of-memory abort before the maxRows
// guardrail could run. Page size is now explicit and every read is byte- and
// time-bounded in ingest/http.ts.
```

対策として`offset`を明示的に指定し、`startblock`を進めながらページングする方式に変更し、さらにHTTP層でバイト数と時間の両方に上限を設けるガードレールを追加しました。取得したデータは重複ページをまたぐことがあるため、`(hash, layer, from, to, value, index)`の組でダウンストリームにて重複排除しています。ネイティブ送金だけでなく内部トランザクション（`internal`）とトレース由来の行も同じ`SlimTx`型に正規化し、トレース由来の行には`traceAddress`を持たせています。これはクラスタ内の複数ウォレットから同じフレームを二重に見ても、正規化時に一件に潰すための識別子です。

## ステージ2: 正規化と分類

取得した生ログは`CanonicalEvent`という共通形式に正規化した後、分類パイプラインにかけます。分類はルールベースを最優先にし、ルールが判定できなかった曖昧な事象だけをLLMフォールバックに回す構成です。

```ts
export async function classifyEvent(ev: CanonicalEvent, llm?: LlmClassifier): Promise<Classification> {
  const rule = applyRules(ev);
  if (rule) {
    return {
      eventId: ev.id,
      category: rule.category,
      reasonCode: rule.reasonCode,
      confidence: rule.confidence,
      evidence: rule.evidence,
      taxable: rule.taxable,
      isIncome: rule.isIncome,
      needsReview: rule.confidence < REVIEW_THRESHOLD,
      source: "rule",
    };
  }
```

LLMをいきなり全件に投げるのではなく、まずルールベースの決定的な判定を通し、確信度が`REVIEW_THRESHOLD`（0.85）を下回るものだけを人間のレビューキューに送る設計です。LLMの出力もJSONスキーマで制約する前提にしており、幻覚が原価計算に混入する余地を極力小さくしています。ルールもLLMも判定できなかった場合は`category: "unknown"`として必ずレビューに回すため、誤分類に気づかないまま計算が進むことはありません。

## ステージ3: 原価計算エンジン

分類が終わった事象はウォレット・資産ごとにロット追跡され、FIFOで消費されます。ここが最もバグを踏みやすい部分で、2026年7月15日のPhase 0で7件のバグ（P1〜P7）と、独立検証で見つかった2件（V1、V2）を修正しています。

```ts
export function isLongTerm(acquiredAt: string, disposedAt: string): boolean {
  const cutoff = new Date(acquiredAt);
  cutoff.setUTCFullYear(cutoff.getUTCFullYear() + 1);
  return new Date(disposedAt).getTime() > cutoff.getTime();
}
```

修正の中でも特に印象的だったのはP4とV2です。米国の長期・短期区分は「取得日から1年を超えて保有」した場合に長期扱いになりますが、365日という日数ベースの判定では閏年を含む期間で1日ずれることがあります。カレンダーベースの`setUTCFullYear`で判定するようにした上で、閏日（2月29日）に取得したロットは処分時に必ずレビューフラグを立てるようにしました。カットオフが翌年3月1日にロールする仕様上の挙動をそのまま信用させず、会計担当者に確認させるためです。

その他のバグも実務上は見落としやすいものばかりでした。ネガティブリベース（トークンの残高が自動的に減るタイプの資産）は新規ロットを積むのではなく既存ロットを取り崩す挙動にし（P1）、`fmvUsd`（時価）が取得できなかった場合は例外を投げず「unpriced」としてレビューに回し（P3）、イベントの並び順は文字列比較ではなく`Date.parse`の数値比較に統一し、パースできないタイムスタンプは除外してレビューに回すようにしています（V1）。

## 1099-DAで検証可能にしたいこと

これらの積み重ねで最終的に検証したいのは、取引所が発行する1099-DAの数字と、オンチェーンの実態から独立に再計算した数字が一致するかどうかです。`linkTransfers`フラグをオフにすると、ウォレット間送金で原価が引き継がれない「ナイーブな計算」を再現でき、オンにした正しい計算との差分から幻の含み益がどれだけ発生していたかを定量化できます。取引所の外で起きたDEXスワップやブリッジ、エアドロップの原価まで含めて第三者的に再現できることが、Verilotがオンチェーンログだけにこだわる理由です。

---

オンチェーン取得原価の検証ツールVerilotを、個人で開発しています。気になる方はこちらからどうぞ。

https://verilot.app?utm_source=sen_zenn&utm_medium=cta&utm_campaign=verilot&utm_content=sen_zenn_a

※本記事の内容は2026年8月3日時点の情報にもとづきます。
