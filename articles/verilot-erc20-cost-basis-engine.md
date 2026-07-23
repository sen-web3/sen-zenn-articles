---
title: "ERC-20の取得原価をオンチェーンデータだけから復元する ― 1099-DA文脈の検証エンジン実装"
emoji: "🧾"
type: "tech"
topics: ["blockchain", "typescript", "erc20", "tax", "web3"]
published: true
---

取得原価（コストベース）とは、暗号資産を含む資産の売却時に課税上の損益を計算するための基準となる、取得時の価格情報のことです。取引所内で完結する売買であれば取引所の履歴から復元できますが、DEXスワップ・ブリッジ・エアドロップのようにオンチェーンで完結する移転が絡むと、取引所のCSVだけでは原価が追えなくなります。この記事では、私が個人開発しているオンチェーン取得原価検証ツールVerilotの中核である`costbasis/engine.ts`を軸に、ログ取得から原価計算までの実装をHOW寄りで書いていきます。

## 背景：なぜ「取引所を介さない移転」が問題になるのか

米国では2025年分の取引から暗号資産にも1099-DA（Digital Asset）様式が適用され始めており、ブローカー（主に取引所）が総所得を報告する一方で、取得原価の証明責任は依然として納税者側に残ります。ウォレット間送金、DEXでのスワップ、L2ブリッジ、エアドロップ受領はいずれも取引所を経由しないため、素朴なツールはこれらを「原価ゼロの新規取得」として扱ってしまいがちです。これは実際の含み益がないのに巨大な譲渡益（phantom gain）を生む典型的なバグで、Verilotではこれを定量化すること自体を検証項目の一つに据えています。

## パイプラインの全体像

Verilotの処理は次の4段階に分かれます。

1. Blockscout APIでの生ログ取得（native tx / internal tx / ERC-20 Transfer）
2. 正規化（重複排除・順序保証・タイムスタンプ検証）
3. ルールベース→LLMフォールバックの分類
4. FIFOロット管理による原価計算・実現損益・監査証跡の出力

### 1. ログ取得：Blockscout APIとページネーション

エクスプローラAPIはEtherscan互換のため、Blockscout公開インスタンス（例: `eth.blockscout.com`）に対してAPIキー無しで呼び出せます。ただし1ページあたり10,000行の上限があるため、`startblock`をずらしながら取得し、重複区間はダウンストリームで排除する設計にしています。

```ts
export interface SlimTokenTx {
  hash: string;
  block: number;
  ts: number;
  txIndex: number;
  index: number;           // synthetic within-block occurrence ordinal
  from: string;
  to: string;
  value: string;
  contract: string;        // lowercase ERC-20 contract = canonical asset id
  decimals: number;
  symbol: string;           // display only — attacker-controlled, sanitized
}
```

ここで詰まったのが`tokentx`エンドポイントにはログインデックスが含まれない点です。ブロック内で同一の送金が複数回発生するケース（同じfrom/to/valueのTransferが1ブロックに2回あるなど）を正しく区別しつつ、ページ重複時の再取得は決定的に重複除去したかったので、ブロック内の出現順を合成インデックスとして自前で振っています。`truncated`フラグも用意していて、行数上限で途中打ち切りになったレジャーは原価計算に使わせない安全弁にしています。

### 2. 正規化：時刻の扱いとべき等性

分類より前段で、イベントの並び順とID重複の扱いを固めています。エンジン側のコメントに残しているP2/V1がこれで、`Date.parse(ts)`を数値比較し、`logIndex`とパース元順序でタイブレークします。パース不能なタイムスタンプは黙って誤順序にせず、レビュー対象として除外する扱いにしています。P7では同一イベントIDの再投入を拒否する冪等性ガードを入れていて、リトライで同じログを二重取り込みしても原価が二重計上されないようにしています。

### 3. 分類：ルール優先・LLMは尻尾だけ

分類は決定論的ルールを最優先し、ルールが判定できなかった曖昧なテールだけをLLMに回す構成にしています。

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
  ...
}
```

`REVIEW_THRESHOLD`は0.85に設定していて、ここを下回るものはルールでもLLMでも一律にアカウンタントのレビューキューに送ります。LLM実装は本番では大手LLMプロバイダのAPIに厳格なJSONスキーマ（enum制約付き構造化出力）で問い合わせる想定ですが、MVPではオフラインで再現可能なスタブに差し替えています。ハルシネーション面を最小化するため、LLMが判断できる余地自体をなるべく狭くしているのが設計上の判断です。

### 4. 原価計算：FIFOロットと`linkTransfers`フラグ

コアはウォレット単位でのロット追跡とFIFO消費です。特に効いているのが`linkTransfers`というオプションで、`true`なら送金元での取得原価を送金先に引き継ぎ、`false`ならナイーブツール同様に転送先を原価ゼロの新規取得として扱います。同じデータセットに対して両方走らせることで、ナイーブ実装がどれだけの幻の譲渡益を生んでいるかを差分として提示できるようにしています。

```ts
export function isLongTerm(acquiredAt: string, disposedAt: string): boolean {
  const cutoff = new Date(acquiredAt);
  cutoff.setUTCFullYear(cutoff.getUTCFullYear() + 1);
  return new Date(disposedAt).getTime() > cutoff.getTime();
}
```

長期/短期の判定（P4）は「1年を超えて保有」という米国ルールに合わせ、365日固定ではなくカレンダー年で判定しています。うるう年の2月29日取得（V2）は`setUTCFullYear`のロールで3月1日カットオフになる副作用があるため、この日を含む処分は自動計算はそのまま通しつつレビューフラグを立てる、という妥協点にしています。負のリベース（P1）はゼロ原価ロットから優先消費、ブリッジでの数量減耗（P5）は警告してレビューに回す、価格が取れない場合（P3）は例外を投げず「未価格付け」として処理を止めないなど、実運用で出た不整合を例外扱いせずレビュー可能な状態で通す方針を一貫させています。

## 1099-DA文脈で検証したいこと

1099-DAが求めるのは総所得の報告であって取得原価の証明ではないため、ブローカー間の資産移動や自己管理ウォレットへの出庫では原価情報が引き継がれません。Verilotが目指しているのは、この欠落を埋めるための監査証跡（`LotConsumption`）をオンチェーンログだけから再構成し、どのロットがどの取得時点・どの取引ハッシュに紐づくかを機械的に検証可能にすることです。会計士が最終判断する前段階として、根拠付きの原価候補と、レビューが必要な項目を明示的に分離して渡す、という位置づけで実装を進めています。

## まとめ

ログ取得・正規化・分類・原価計算という4段の分離は地味な作業ですが、それぞれの層で「黙って間違えない」ための小さなガード（順序保証、冪等性、レビューフラグ）を積み重ねたことで、結果的に説明可能な原価計算エンジンになったと感じています。特に`linkTransfers`のON/OFF比較は、オンチェーン移転を無視した原価計算がどれだけ危険かを具体的な数字で示せる点で、実装していて手応えを感じた部分でした。

---

オンチェーン取得原価の検証ツールVerilotを、個人で開発しています。気になる方はこちらからどうぞ。

https://verilot.vercel.app?utm_source=sen_zenn&utm_medium=cta&utm_campaign=verilot&utm_content=sen_zenn_a

※本記事の内容は2026年7月23日時点の情報にもとづきます。
