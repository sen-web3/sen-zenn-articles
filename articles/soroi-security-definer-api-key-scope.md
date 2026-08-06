---
title: "マルチテナントSaaSのAPIキー認証、RLSだけでは防げなかった話 ― SupabaseのSECURITY DEFINER関数で塞いだ穴"
emoji: "🔐"
type: "tech"
topics: ["supabase", "postgresql", "security", "typescript", "rls"]
published: true
---

Row Level Security（RLS）とは、PostgreSQLがテーブル単位ではなく行単位でアクセス制御を行う仕組みのことです。テナントごとに閲覧できる行をポリシーで絞り込めるため、マルチテナントSaaSのDB設計で広く使われています。私はステーブルコイン入金消込ツールSoroiを個人開発していて、先日「/api/v1/* 」という外部向け読み取り専用APIをBearerキー認証で実装しました。その過程で、RLSのUSING句だけではテナント分離を表現しきれない経路があることに気づき、SECURITY DEFINER関数と専用DBロールで塞いだので、判断の経緯を書きます。

## 何に困ったか

Soroiのダッシュボードはユーザーがブラウザからログインして使う画面で、こちらは`auth.uid()`をRLSポリシーのUSING句に埋め込めば済みます。Supabase Authのセッションが確立していれば、PostgREST経由のクエリにJWTのクレームが乗り、`auth.uid() = user_id`のようなポリシーがそのまま機能するからです。

問題は外部APIの方でした。会計ソフト連携やBIツールからのポーリングを想定した`app/api/v1/*`は、SupabaseのAuthセッションを経由しません。ヘッダーの`Authorization: Bearer soroi_live_...`を自分でパースし、自前の`api_keys`テーブルのハッシュと突き合わせて認証します。つまりPostgreSQL側のセッションには`auth.uid()`に相当する情報が一切乗らないわけです。ここで普通に考えると、DBアクセスは`service_role`キーで行うしかありません。

ところがSupabaseの`service_role`はデフォルトでRLSをバイパスします。バイパスすること自体は意図された仕様ですが、これは「RLSポリシーを書いてもテナント分離はDBが保証してくれない」ということでもあります。つまりAPIキー認証で解決した`userId`を使って、Route Handler側で`.eq("user_id", userId)`を書き忘れなく毎回付ける運用に依存することになります。1本のクエリを書き忘れれば、そのエンドポイントは全ユーザーの行を返してしまいます。RLSのUSING句という宣言的な最後の砦が、この経路では最初から効いていないわけです。

## SECURITY DEFINER関数でスコープを縛る

そこで採った方針は、Route Handlerから生テーブルへ直接SELECTを投げるのをやめ、必ずDB関数（RPC）経由にすることでした。SECURITY DEFINER関数とは、呼び出し元の権限ではなく関数定義者の権限で実行される関数のことです。関数内部で`p_key_hash`からキーの持ち主を解決し、以降のクエリはすべて関数内でその`user_id`に固定してしまいます。呼び出し側のRoute Handlerは`user_id`という変数自体を一切扱わなくなるので、WHERE句の書き忘れというヒューマンエラーの経路そのものが構造的になくなります。

`lib/apiAuth.ts`では、まず`api_verify_key`でキーの有効性を検証し、有効な場合のみ`api_check_rate_limit`でレート制限を判定します。

```ts
const { data: userId, error: verifyError } = await supabase.rpc("api_verify_key", {
  p_key_hash: keyHash,
});
if (verifyError) {
  console.error("[soroi:api:verify]", verifyError);
  return { ok: false, status: 503, errorCode: "internal_error" };
}
if (!userId) {
  return { ok: false, status: 401, errorCode: "invalid_api_key" };
}
```

無効なキーで即401にしているのは、レート制限テーブルへの行作成を有効キーのみに絞るためです。でたらめなトークンを連投されても、`api_key_rate_limits`のような集計テーブルが際限なく肥大化しません。

レート制限判定のエラーハンドリングでは、fail-openを避けています。

```ts
if (rateError) {
  console.error("[soroi:api:rate_limit]", rateError);
  // 判定自体が失敗した場合はfail-open（素通し）にせず、503として拒否する
```

判定処理がエラーで落ちたときに素通ししてしまうと、レート制限が実質機能しない状態でトラフィックを受け続けることになります。DB未接続時（`isApiServiceConfigured()`がfalse）も同じ理屈でfail-closedの503を返しています。読み取り専用APIとはいえ、判定不能な状態で「とりあえず通す」設計は選びませんでした。

## service_roleを直接触らせない

SECURITY DEFINER関数を用意しても、そのEXECUTE権限を`service_role`や`PUBLIC`に開けたままだと、結局「なんでも呼べる強い権限のキー」がRoute Handlerのコード内に存在することになります。これはRLSバイパス問題を関数の中に押し込めただけで、根本の攻撃面は変わっていません。

そこでマイグレーションを2段階に分けました。まず`0007`で`api_keys`テーブルと`api_verify_key`などの関数を作成し、続く`0008`でこの5関数のEXECUTE権限を`service_role`から剥がし、`api_service`という専用ロールにのみ許可する形に変更しています。アプリケーション側の接続も、`SUPABASE_API_SERVICE_KEY`という別クレデンシャルで`api_service`ロールとして繋ぎ直しました。`lib/apiAuth.ts`のコメントにも当時の判断がそのまま残っています。

```ts
// 0008マイグレーションでこの5関数のEXECUTEはapi_serviceロール専用になった
// ため、SUPABASE_API_SERVICE_KEY 未設定時も同様にfail-closedする。
if (!isApiServiceConfigured()) {
  return { ok: false, status: 503, errorCode: "supabase_not_configured" };
}
```

こうしておくと、万が一外部APIのRoute Handler側に脆弱性が見つかっても、そこから触れるDB権限は「決められた5つのRPCを叩く」ことに限定されます。`service_role`という万能キーがコードベース中に散らばっている状態と比べて、侵害時の被害範囲が明確に狭くなります。実際、ダッシュボード用のクライアント（`service_role`使用箇所）と外部API用のクライアント（`api_service`使用箇所）はファイルレベルで分離していて、`createApiServiceSupabaseClient`という専用の生成関数を用意しました。

## APIキー自体の設計も純関数側で固めた

RPC側の話とは別に、キー生成のハッシュ方式でも判断がありました。`lib/apiKeys.ts`ではbcryptのような意図的に低速なハッシュではなくSHA-256を採用しています。

```ts
export function generateApiKey(): GeneratedApiKey {
  const raw = `${API_KEY_PREFIX}${randomBytes(RANDOM_BYTES).toString("hex")}`;
  return { raw, hash: hashApiKey(raw) };
}
```

生キーは256bitのランダム生成のみで、人間が選ぶパスワードのようなエントロピー不足の心配がありません。GitHubのPersonal Access Tokenなど同種の高エントロピーAPIキーの多くも単純ハッシュを採用しており、レインボーテーブル耐性より毎リクエストの照合コストを優先する判断は妥当だと考えています。

生キーはDBに一切保存せず、発行直後の一度だけ画面に表示して以降は再表示不可にしています。この設計はDB層の話ではありませんが、`api_verify_key`が受け取るのは常にハッシュのみという前提を守るための土台になっていて、SECURITY DEFINER関数の設計とも噛み合っています。

## 詰まった箇所

RPC経由に統一したことで別種の不具合も見つかりました。`api_list_transactions`と`api_list_matches`は`p_offset`をPostgreSQLの`integer`型で受け取るのですが、ページネーションのオフセット値をそのまま渡すと`integer`の範囲を超えた場合にPostgRESTが「integer out of range」で500を返してしまいます。`limit`側は`MAX_PAGE_LIMIT`でクランプしていたのに、`offset`側の上限チェックが漏れていたのが原因で、監査の2回目のラウンドで見つかりました。純関数側（`lib/apiRequest.ts`）でクランプを追加し、DB非依存の`node --test`で境界値を検証しています。RPC呼び出し部分（`lib/apiAuth.ts`）はDB接続が前提のため実機検証、リクエスト解析部分は純関数としてユニットテストという住み分けは、他の非純粋処理（`lib/actions.ts`のレート制限処理）と同じ方針に揃えました。

## まとめ

RLSはセッションに認証情報が乗る経路では強力ですが、APIキーのような自前認証を`service_role`で処理する経路では最初から効いていません。この記事で書いたのは、そのギャップをSECURITY DEFINER関数でユーザー解決とクエリ条件をDB側に閉じ込め、さらにEXECUTE権限を専用ロールに絞ることで、コード側の書き忘れにテナント分離を依存させない構成にした話です。マルチテナントSaaSで外部APIを設計する際は、RLSポリシーの有無だけでなく、そもそもそのポリシーが機能するセッションを経由しているかを最初に確認する価値があると感じています。

---

ステーブルコインの入金消込を自動化するSoroiを、個人で開発しています。気になる方はこちらからどうぞ。

https://soroi-beryl.vercel.app/?utm_source=sen_zenn&utm_medium=cta&utm_campaign=soroi&utm_content=sen_zenn_a#waitlist

※本記事の内容は2026年7月30日時点の情報にもとづきます。

同じような場面で困った経験がある方がいれば、コメントかこのアカウントへの返信で、どんな入金・請求のパターンに一番時間を取られているか教えてください。次の設計判断の参考にします。
