#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_articles.py — Zenn記事がpush前に「公開される形」になっているかを機械で確かめる。

## なぜ作るか（2026-08-21）

`verilot-nextjs-metadata-deep-merge-ogp-bug` が **4日間公開されないまま放置されていた**。
front matterは `published: true`、gitはpush済み（ahead 0）、つまり手元のあらゆる指標は
「完了」を示していた。しかし Zenn 側は記事ページもAPIも **HTTP 404** だった。

真因は `emoji: "🖼️"` が**2コードポイント**だったこと（U+1F5BC + U+FE0F 異体字セレクタ）。
Zennのemojiは1文字が要件で、弾かれると**エラーはどこにも出ず、ただ同期されない**。
公開済み11本のうち、この1本だけが2コードポイントだった。

pushの成功は公開の証拠ではない。だから push する前にここで止める。

## 使い方

    python3 check_articles.py            # 全記事を検査（異常があれば終了コード1）
    python3 check_articles.py --published # 公開済みのはずの記事が本当に公開されているかZennに問い合わせる

`--published` は外部通信あり。それ以外はローカル完結。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

ARTICLES = pathlib.Path(__file__).resolve().parent / "articles"
USERNAME = "sen_web3"

# Zenn公式のfront matter仕様（zenn.dev/zenn/articles/zenn-cli-guide）
VALID_TYPES = {"tech", "idea"}
SLUG_RE = re.compile(r"^[0-9a-z\-_]{12,50}$")


def front_matter(text: str) -> dict | None:
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    fm = {}
    for line in m.group(1).split("\n"):
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip().strip('"')
    return fm


def check_file(path: pathlib.Path) -> list[str]:
    problems = []
    text = path.read_text()
    fm = front_matter(text)
    if fm is None:
        return ["front matterが無い（記事が同期されない）"]

    slug = path.stem
    if not SLUG_RE.match(slug):
        problems.append(
            f"slugが規約外: {slug!r}（半角英数字・ハイフン・アンダースコアの12〜50文字）")

    emoji = fm.get("emoji", "")
    if len(emoji) != 1:
        cps = " ".join(f"U+{ord(c):04X}" for c in emoji)
        problems.append(
            f"**emojiが1文字でない**: {emoji!r} は {len(emoji)}コードポイント [{cps}]。"
            "Zennに弾かれ、エラーも出ないまま記事が公開されない"
            "（2026-08-21に4日間の未公開を実際に起こした欠陥）")

    t = fm.get("type", "")
    if t not in VALID_TYPES:
        problems.append(f"typeが{VALID_TYPES}のいずれかでない: {t!r}")

    topics_raw = fm.get("topics", "")
    topics = [x.strip().strip('"').strip("'")
              for x in topics_raw.strip("[]").split(",") if x.strip()]
    if not (1 <= len(topics) <= 5):
        problems.append(f"topicsは1〜5個である必要がある（現在 {len(topics)}個）")

    if fm.get("title", "").strip() == "":
        problems.append("titleが空")

    return problems


def check_published(slugs: list[str]) -> list[str]:
    """`published: true` の記事が、Zenn側に実在するかを問い合わせる。

    pushの成功で判定しない。**相手側に何が残っているか**で判定する。
    """
    problems = []
    for slug in slugs:
        url = f"https://zenn.dev/api/articles/{slug}"
        req = urllib.request.Request(url, headers={"User-Agent": "zenn-check/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                problems.append(
                    f"**{slug} は published: true なのにZennに存在しない（404）**。"
                    "同期に失敗している。front matterの検査結果を先に見ること")
            else:
                problems.append(f"{slug}: 問い合わせ失敗 HTTP {e.code}")
        except Exception as e:
            problems.append(f"{slug}: 問い合わせ失敗 {e!r}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--published", action="store_true",
                    help="公開済みのはずの記事がZennに実在するかを問い合わせる（外部通信あり）")
    a = ap.parse_args()

    files = sorted(ARTICLES.glob("*.md"))
    if not files:
        print("articles/ に記事が無い")
        return 1

    total, published_slugs = 0, []
    for f in files:
        problems = check_file(f)
        fm = front_matter(f.read_text()) or {}
        if fm.get("published", "").lower() == "true":
            published_slugs.append(f.stem)
        if problems:
            total += len(problems)
            print(f"\n[NG] {f.name}")
            for p in problems:
                print("   -", p)

    if a.published and published_slugs:
        print(f"\nZennへ実在確認中… ({len(published_slugs)}本)")
        for p in check_published(published_slugs):
            total += 1
            print("   -", p)

    print()
    if total:
        print(f"問題 {total} 件。**pushしても公開されない記事がある。** 直してから push すること")
        return 1
    print(f"問題なし（{len(files)}本を検査"
          f"{'・Zenn実在確認込み' if a.published else ''}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
