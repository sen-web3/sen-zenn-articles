---
title: "招待制ベータのまま、マジックリンクにGitHub/Google OAuthを共存させた設計"
emoji: "🔐"
type: "tech"
topics: ["supabase", "oauth", "nextjs", "auth", "typescript"]
published: true
---

## OAuth共存とは何か

OAuth共存とは、既存の認証方式（本記事ではメールOTPによるマジックリンク）を維持したまま、GitHubやGoogleといった外部プロバイダによるOAuth2.0ログインを追加提供することです。ステーブルコイン入金消込ツールSoroiは招待制ベータとして運用しており、新規ユーザーの作成をSupabase Auth側の「Allow new users」オフで制御しています。今回、この制御を崩さずにOAuthボタンを増やす作業を行ったので、実装と設計判断をまとめます。

## 前提: マジックリンクだけだった理由

Soroiはベータ開始時、`signInWithOtp` によるメールOTPのみでログインを提供していました。招待制のため新規サインアップを許可したくなく、Supabase Authの管理画面で「Allow new users」をオフにし、招待済みメールアドレスにだけ手動でユーザーを作成する運用にしています。この方式であればメールOTPの1経路だけを考えればよく、実装もシンプルでした。

利用者からGitHub/Googleログインの要望が増えてきたため、OAuthを追加することにしましたが、条件は一つだけでした。招待されていないユーザーが、OAuth経由でも新規作成されないことです。

## 「Allow new users」がプロバイダを問わず効く理由

結論から言うと、追加実装は不要でした。「Allow new users」はSupabase Auth内部でユーザー作成をブロックするフラグで、メールOTPかOAuthかというプロバイダ種別の外側、つまりユーザーレコード作成処理そのものに効く設定です。そのためGitHubやGoogleでサインインしようとした未招待ユーザーも、Supabase側で `signInWithOAuth` の裏側にあるユーザー作成ステップで弾かれます。アプリケーションコード側で招待チェックのロジックを書き足す必要はありませんでした。

これはコードのコメントにも明示しています。

```tsx
// GitHub / Google OAuth（Supabase Auth の外部プロバイダ）。
// 招待制ベータの「Allow new users」オフはプロバイダ種別に関わらず効くため、
// 未招待者はここからも新規作成されない。
// Client ID/Secret はこのアプリの env ではなく Supabase ダッシュボード
// （Authentication > Providers）側に設定する。
export function OAuthButtons() {
```

Client IDやSecretはNext.js側の環境変数ではなく、Supabaseダッシュボードの Authentication > Providers に設定します。アプリケーションコードにシークレットを持たせずに済むのは、外部プロバイダ管理をSupabase Auth側に寄せている恩恵だと感じています。

## OAuthButtonsコンポーネントの実装

`signInWithOAuth` の呼び出し自体は `signInWithOtp` とほぼ対称です。プロバイダ名を渡し、リダイレクト先を `/auth/callback` に固定します。

```tsx
async function signInWithProvider(provider: "google" | "github") {
  setError(null);
  setLoadingProvider(provider);
  const supabase = createClient();
  const safeNext = sanitizeNextPath(new URLSearchParams(window.location.search).get("next"));
  const { error } = await supabase.auth.signInWithOAuth({
    provider,
    options: {
      redirectTo: `${window.location.origin}/auth/callback?next=${encodeURIComponent(safeNext)}`,
    },
  });
```

ここで使っている `sanitizeNextPath` は、middlewareが未ログインで保護パスにアクセスした際に付与する `?next=` パラメータを相対パスのみに限定するユーティリティで、マジックリンク側のログインフォームと完全に同じロジックを流用しています。open redirect対策として、既存の `login/page.tsx` にあった仕組みをそのままコピーしただけです。

```tsx
const next = sanitizeNextPath(new URLSearchParams(window.location.search).get("next"));
const { error } = await supabase.auth.signInWithOtp({
  email,
  options: {
    emailRedirectTo: `${window.location.origin}/auth/callback?next=${encodeURIComponent(next)}`,
  },
});
```

この対称性のおかげで、OAuthButtons側の実装で新しく考える必要があったのは「window参照はクリック時のみ」という点くらいでした。コンポーネントはクライアントコンポーネントですが、SSR中に `window` を参照するとエラーになるため、`signInWithProvider` 関数内、つまりボタンクリック時にのみ `window.location` を読む形にしています。

## `/auth/callback` を変更せずに済んだ理由

OAuth追加作業で一番身構えていたのが `/auth/callback` の対応でした。マジックリンクは実装時、Supabaseの既定verifyリダイレクトが `#access_token` というURLフラグメント方式でトークンを返すのに対し、`/dashboard` のようなmiddleware保護下のページはCookieしか見ないためフラグメントのトークンを認識できずログインに失敗する、という問題が2026年7月13日の実測で見つかっていました。そのため一度非保護の `/auth/callback` を経由してクライアント側でセッションを確立してから遷移する設計に、すでになっていました。

一方でOAuthのリダイレクトはPKCEフローの `?code=` パラメータ方式を使うことが多く、フラグメント方式とは処理が異なります。ここで両者を振り分ける処理を新しく書く必要があるだろうと予想していましたが、確認したところ既存の `/auth/callback` は `#access_token` 方式と `?code` 方式の両方をSupabaseクライアントのセッション確立処理の中で吸収しており、呼び出し側で分岐を書く必要はありませんでした。Supabase JS SDKの `getSession` まわりの実装が、URLの形式に応じて内部で処理を切り替えているためです。結果として `/auth/callback` は一切変更せず、OAuthButtonsから同じエンドポイントにリダイレクトするだけで動作しました。

## 招待制であることの案内をUIに常設した経緯

もう一つ設計判断として記録しておきたいのが、招待制であることの案内文です。当初はログインエラー時にのみ表示していましたが、ペルソナを想定したUXレビューで、招待されていないユーザーがガイド等から流入した場合、何も説明のないままログイン画面に飛ばされる状態になっている点が指摘されました。OAuthボタンを追加するとさらにログイン経路が増えるため、常設の案内文をページ側に置き、ウェイトリストへの導線を明示する形にしています。

```tsx
<p className={styles.inviteNote}>
  {t("inviteNote")}
  <a href="/#waitlist" className={styles.link}>
    {t("inviteWaitlistLink")}
  </a>
</p>
```

エラー文言についても、Supabaseが返す英語の内部メッセージをそのまま出さず、`error.code` から安定したi18nキーに変換する処理をOAuth側にも同じ考え方で適用しています。エラーの詳細はコンソールにのみ出し、画面には翻訳済みの文言だけを表示する方針です。

## まとめ

今回の作業で分かったのは、Supabase Authを土台にしている場合、認証方式を増やすこと自体は薄い差分で済むという点です。招待制の制御はプロバイダの外側にあるユーザー作成処理にかかっているため、GitHubやGoogleを増やしてもゲート自体は自動的に効きますし、コールバック処理もSupabase JS SDK側が方式差を吸収してくれていました。既存実装がどこまで抽象化を引き受けてくれているかを最初に確認したことで、無駄な分岐やコピーを増やさずに済んだのが今回の一番の収穫だったと思います。

---

ステーブルコインの入金消込を自動化するSoroiを、個人で開発しています。気になる方はこちらからどうぞ。

https://soroi-beryl.vercel.app/?utm_source=sen_zenn&utm_medium=cta&utm_campaign=soroi&utm_content=sen_zenn_a#waitlist

※本記事の内容は2026年7月27日時点の情報にもとづきます。

同じような場面で困った経験がある方がいれば、コメントかこのアカウントへの返信で、どんな入金・請求のパターンに一番時間を取られているか教えてください。次の設計判断の参考にします。
