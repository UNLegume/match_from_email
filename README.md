# match_from_email

Gmail で受信した案件紹介メールを自動解析し、候補者プロフィールとのマッチ度を Claude AI で採点するシステム。結果は CSV 出力と Gmail ラベル付与で整理されます。

## 処理フロー

```
match_jobs.py          案件メール取得 → フィルタリング → Claude API で採点 → CSV + ラベル出力
    ↓ results_*.csv
add_labels.py          CSV の上位案件に Gmail ラベルを一括付与
relabel.py             同一メールの重複を排除してラベル付与
retry_labels.py        自動処理で失敗したケースを段階的検索で補正
```

## セットアップ

### 前提条件

- Python 3.10+
- Google Cloud プロジェクト（Gmail API 有効化済み）
- Anthropic API キー

### インストール

```bash
pip install google-auth google-auth-oauthlib google-api-python-client anthropic beautifulsoup4 python-dotenv
```

### 設定ファイル

| ファイル | 説明 |
|---------|------|
| `credentials.json` | Google OAuth2 クライアントID（Google Cloud Console から取得） |
| `token.json` | Gmail API アクセストークン（初回実行時に自動生成） |
| `.env` | 環境変数（`ANTHROPIC_API_KEY` 等） |

## 使い方

### 1. 案件マッチング実行

```bash
python match_jobs.py
```

過去 N 営業日分のメールを取得し、技術キーワードで案件をフィルタリング後、Claude API でマッチ度（0-100）を採点します。結果は `results_YYYY-MM-DD.csv` に出力されます。

### 2. ラベル付与

```bash
python add_labels.py    # 基本的なラベル付与
python relabel.py       # 重複排除版
python retry_labels.py  # 失敗ケースの補正
```

## 技術スタック

- **Google Gmail API** — メール取得・検索・ラベル操作（バッチリクエスト対応）
- **Claude API** (`claude-sonnet-4-20250514`) — 案件マッチング採点
- **BeautifulSoup4** — HTML メール本文のテキスト抽出
