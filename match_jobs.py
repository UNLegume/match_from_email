import os, json, base64, re, sys
from datetime import datetime, timedelta, date
from pathlib import Path
import anthropic

def load_env():
    env_path = Path(__file__).parent / '.env'
    if env_path.exists():
        for line in env_path.read_text().strip().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, val = line.split('=', 1)
                os.environ[key.strip()] = val.strip().strip('"').strip("'")

load_env()

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from bs4 import BeautifulSoup

import warnings
warnings.filterwarnings("ignore")  # Suppress Python 3.9 deprecation warnings

import ssl
import httplib2

# =============================================================================
# Section 1: Constants and Resume Profile
# =============================================================================

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

RESUME = {
    "name": "杉原直城",
    "years_experience": 6,
    "core_skills": {
        "kotlin": 3.0, "java": 2.5, "android": 3.0, "swift": 1.5,
        "jetpack compose": 2.0, "room": 1.0, "retrofit": 1.0,
        "android sdk": 2.0, "spring boot": 1.5, "seasar2": 0.8,
    },
    "secondary_skills": {
        "karte": 0.5, "new relic": 0.5, "google analytics": 0.5,
        "microcms": 0.5, "github": 0.3, "jira": 0.3, "confluence": 0.3,
        "git": 0.3, "redmine": 0.3, "subversion": 0.3, "eclipse": 0.3,
        "sqlite": 0.3, "oracle": 0.5, "github copilot": 0.3,
    },
    "jp_domain_keywords": {
        "開発": 1.0, "設計": 0.8, "要件定義": 0.8, "テスト": 0.5,
        "アプリ": 1.2, "モバイル": 1.2, "リーダー": 0.5,
        "アジャイル": 0.5, "スクラム": 0.5, "オフショア": 0.5,
        "ブリッジ": 0.5, "D2C": 0.8, "決済": 0.8, "IoT": 0.8,
        "基本設計": 0.8, "詳細設計": 0.8, "結合テスト": 0.5,
    },
    "resume_text": "Androidアプリ開発エンジニア6年 Kotlin Java Swift Jetpack Compose Room Retrofit Android SDK Spring Boot 要件定義 設計 開発 運用 D2Cアプリ 決済代行システム 家電制御アプリ オフショアブリッジ アジャイル スクラム"
}

JOB_EMAIL_SUBJECTS = ["案件", "ご紹介", "ご案内", "ご提案", "募集", "求人"]

# =============================================================================
# Section 2: OAuth Authentication
# =============================================================================

CREDENTIALS_PATH = Path(__file__).parent / 'credentials.json'
TOKEN_PATH = Path(__file__).parent / 'token.json'


def authenticate():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

# =============================================================================
# Section 3: Business Day Calculation
# =============================================================================


def get_last_n_business_days(n=3, reference_date=None):
    """Return (start_date, end_date) for the last N business days before reference_date."""
    ref = reference_date or date.today()
    days = []
    current = ref - timedelta(days=1)
    while len(days) < n:
        if current.weekday() < 5:  # Mon-Fri
            days.append(current)
        current -= timedelta(days=1)
    start = min(days)
    end = max(days)
    # Gmail after/before are exclusive, so adjust
    return start, end

# =============================================================================
# Section 4: Gmail Fetch
# =============================================================================


def fetch_job_emails(service, start_date, end_date):
    date_after = (start_date - timedelta(days=1)).strftime('%Y/%m/%d')
    date_before = (end_date + timedelta(days=1)).strftime('%Y/%m/%d')
    # Narrow query: "案件" is the most specific keyword for SES/agency job emails
    query = f"subject:(案件 OR 募集 OR 求人) after:{date_after} before:{date_before}"

    print(f"Gmail検索クエリ: {query}", file=sys.stderr)

    # Step 1: Get all message IDs
    message_ids = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        results = service.users().messages().list(**kwargs).execute()
        message_ids.extend(results.get("messages", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    total = len(message_ids)
    print(f"メッセージID取得: {total}件", file=sys.stderr)

    # Step 2: Batch fetch metadata only (fast)
    print("メタデータ取得中...", file=sys.stderr)
    metadata_results = _batch_fetch(service, message_ids, format="metadata",
                                     metadata_headers=["Subject", "From", "Date"])

    # Step 3: Filter by subject content (strict filtering)
    filtered = []
    for msg in metadata_results:
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("Subject", "")
        sender = headers.get("From", "")
        if _is_job_email(subject, sender):
            filtered.append({
                "id": msg["id"],
                "subject": subject,
                "sender": sender,
                "date": headers.get("Date", ""),
            })

    print(f"案件メール候補: {len(filtered)}件 (フィルタ後)", file=sys.stderr)

    if not filtered:
        return []

    # Step 4: Batch fetch full body only for filtered emails
    print("本文取得中...", file=sys.stderr)
    full_ids = [{"id": f["id"]} for f in filtered]
    full_results = _batch_fetch(service, full_ids, format="full")

    # Build final email list
    emails = []
    full_map = {msg["id"]: msg for msg in full_results}
    for f in filtered:
        msg = full_map.get(f["id"])
        if not msg:
            continue
        body = decode_body(msg["payload"])
        if body:
            if len(body) > 5000:
                body = body[:5000]
            emails.append({
                "id": f["id"],
                "subject": f["subject"],
                "sender": f["sender"],
                "date": f["date"],
                "body": body,
            })

    print(f"取得メール数: {len(emails)}", file=sys.stderr)
    return emails


# Tech keywords that indicate a job posting (not generic marketing)
_TECH_KEYWORDS = [
    "android", "ios", "kotlin", "java", "swift", "python", "ruby", "php",
    "javascript", "typescript", "react", "vue", "angular", "node",
    "go", "rust", "c#", "c++", ".net", "aws", "azure", "gcp",
    "se", "pg", "pm", "pl", "devops", "sre", "infra",
    "spring", "django", "rails", "laravel", "flutter", "unity",
    "sql", "oracle", "mysql", "postgresql", "mongodb",
    "docker", "kubernetes", "terraform", "linux",
    "cobol", "sap", "salesforce", "servicenow",
]

_JP_JOB_KEYWORDS = [
    "案件", "募集", "エンジニア", "開発", "SE", "PG", "PM",
    "サーバーサイド", "フロントエンド", "バックエンド", "インフラ",
    "設計", "構築", "運用", "保守", "リモート", "常駐",
    "単価", "万円", "スキル", "工程", "フリーランス",
    "業務委託", "SES", "準委任", "請負",
]

# Domains known to be mass marketing / job board notifications (not direct agency emails)
_MARKETING_DOMAINS = [
    "green-japan.com", "wantedly.com", "indeed.com", "linkedin.com",
    "en-japan.com", "doda.jp", "mynavi.jp", "rikunabi.com",
    "type.jp", "bizreach.jp",
]


def _is_job_email(subject, sender):
    """Check if email is likely a direct job posting (not marketing blast)."""
    subject_lower = subject.lower()
    sender_lower = sender.lower()

    # Skip mass marketing emails from job boards
    for domain in _MARKETING_DOMAINS:
        if domain in sender_lower:
            return False

    # Must contain at least one tech keyword OR Japanese job keyword
    has_tech = any(kw in subject_lower for kw in _TECH_KEYWORDS)
    has_jp_job = any(kw in subject for kw in _JP_JOB_KEYWORDS)

    return has_tech or has_jp_job


def _batch_fetch(service, message_ids, format="metadata", metadata_headers=None, batch_size=10):
    """Fetch messages in batches using Gmail batch API."""
    from googleapiclient.http import BatchHttpRequest

    all_results = []

    for start in range(0, len(message_ids), batch_size):
        chunk = message_ids[start:start + batch_size]
        batch_results = []

        def callback(request_id, response, exception):
            if exception:
                print(f"  バッチエラー: {exception}", file=sys.stderr)
            elif response:
                batch_results.append(response)

        batch = service.new_batch_http_request(callback=callback)

        for msg_meta in chunk:
            kwargs = {"userId": "me", "id": msg_meta["id"], "format": format}
            if metadata_headers and format == "metadata":
                kwargs["metadataHeaders"] = metadata_headers
            batch.add(service.users().messages().get(**kwargs))

        try:
            batch.execute()
        except Exception as e:
            print(f"  バッチ実行エラー: {e}", file=sys.stderr)
            # Fallback: fetch one by one for this chunk
            for msg_meta in chunk:
                try:
                    kwargs = {"userId": "me", "id": msg_meta["id"], "format": format}
                    if metadata_headers and format == "metadata":
                        kwargs["metadataHeaders"] = metadata_headers
                    result = service.users().messages().get(**kwargs).execute()
                    batch_results.append(result)
                except Exception:
                    continue

        all_results.extend(batch_results)
        progress = min(start + batch_size, len(message_ids))
        print(f"  {progress}/{len(message_ids)}", file=sys.stderr)
        # Rate limit: wait between batches
        import time
        time.sleep(0.5)

    return all_results


def decode_body(payload):
    """Recursively extract text from Gmail message payload."""
    mime_type = payload.get("mimeType", "")

    if mime_type.startswith("multipart/"):
        parts = payload.get("parts", [])
        # Prefer text/plain
        plain_texts = []
        html_texts = []
        for part in parts:
            result = decode_body(part)
            if result:
                if part.get("mimeType") == "text/plain":
                    plain_texts.append(result)
                else:
                    html_texts.append(result)
        return "\n".join(plain_texts) if plain_texts else "\n".join(html_texts)

    data = payload.get("body", {}).get("data")
    if not data:
        return ""

    decoded = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    if "html" in mime_type:
        return BeautifulSoup(decoded, "html.parser").get_text(separator="\n", strip=True)

    return decoded

# =============================================================================
# Section 5: Job Posting Extraction
# =============================================================================


def extract_job_postings(email):
    """Extract individual job postings from a single email. Returns list of dicts."""
    body = email["body"]

    # Split multiple postings in one email
    sections = re.split(r'[━─]{3,}|={3,}|■案件|■\d|●案件|▼案件', body)
    sections = [s.strip() for s in sections if s.strip() and len(s.strip()) > 50]

    if not sections:
        sections = [body]

    postings = []
    for section in sections:
        posting = parse_posting_fields(section)
        posting["source_subject"] = email["subject"]
        posting["source_sender"] = email["sender"]
        posting["source_date"] = email["date"]
        posting["source_id"] = email["id"]
        posting["full_text"] = section
        postings.append(posting)

    return postings


def parse_posting_fields(text):
    """Parse structured fields from a posting section."""
    posting = {"title": "", "skills_required": "", "skills_preferred": "", "rate": "", "location": "", "period": ""}

    # Try 【field】value pattern
    brackets = re.findall(r'【(.+?)】\s*(.+?)(?=【|\Z)', text, re.DOTALL)
    if brackets:
        field_map = {
            "案件名": "title", "案件": "title", "プロジェクト": "title",
            "必須スキル": "skills_required", "必須": "skills_required", "スキル": "skills_required",
            "必要スキル": "skills_required", "経験": "skills_required", "必須条件": "skills_required",
            "歓迎スキル": "skills_preferred", "歓迎": "skills_preferred", "尚可": "skills_preferred",
            "単価": "rate", "金額": "rate", "報酬": "rate", "予算": "rate",
            "勤務地": "location", "場所": "location", "最寄り駅": "location", "就業場所": "location",
            "期間": "period", "開始": "period", "稼働": "period", "参画時期": "period",
        }
        for key, val in brackets:
            key = key.strip()
            for pattern, field in field_map.items():
                if pattern in key:
                    posting[field] = val.strip()[:200]
                    break

    # Try key：value pattern as fallback
    if not posting["title"]:
        colon_matches = re.findall(r'^(.{1,15})[：:]\s*(.+)$', text, re.MULTILINE)
        for key, val in colon_matches:
            key = key.strip()
            if any(k in key for k in ["案件", "プロジェクト", "タイトル"]):
                posting["title"] = val.strip()[:200]
            elif any(k in key for k in ["必須", "スキル", "経験"]):
                posting["skills_required"] = val.strip()[:200]
            elif any(k in key for k in ["単価", "金額", "報酬"]):
                posting["rate"] = val.strip()[:100]
            elif any(k in key for k in ["勤務地", "場所", "駅"]):
                posting["location"] = val.strip()[:100]
            elif any(k in key for k in ["期間", "開始", "稼働"]):
                posting["period"] = val.strip()[:100]

    # Use first non-empty line as title if still empty
    if not posting["title"]:
        for line in text.split("\n"):
            line = line.strip()
            if line and len(line) > 5 and len(line) < 100:
                posting["title"] = line
                break

    return posting

# =============================================================================
# Section 6: Claude API Matching
# =============================================================================

RESUME_SUMMARY = """## 候補者プロフィール: 杉原直城
- 経験年数: Android開発6年
- コアスキル: Kotlin, Java, Android, Jetpack Compose, Room, Retrofit, Android SDK, Spring Boot
- サブスキル: Swift, KARTE, New Relic, Google Analytics, microCMS, Oracle, SQLite
- ツール: GitHub, Jira, Confluence, Git, Redmine, SubVersion, GitHub Copilot
- 資格: 基本情報技術者, Oracle Java Silver
- 経験領域: D2Cアプリ, 決済代行システム, 家電制御IoTアプリ, 配送アプリ
- 役割経験: 開発メンバー, サブリーダー, OJT担当, オフショアブリッジ(ベトナム)
- 工程: 要件定義〜システムテスト・リリース, アジャイル/スクラム
- 最寄り: JR総武線 平井駅
"""


_RELEVANT_KEYWORDS = [
    "android", "kotlin", "java", "swift", "ios", "mobile", "モバイル",
    "アプリ", "jetpack", "spring", "boot", "決済", "iot",
    "d2c", "ec", "スマホ", "タブレット",
]


def _pre_filter_postings(postings):
    """Pre-filter postings by keyword relevance to reduce Claude API token usage."""
    relevant = []
    skipped = 0
    for p in postings:
        text_lower = p["full_text"].lower()
        if any(kw in text_lower for kw in _RELEVANT_KEYWORDS):
            relevant.append(p)
        else:
            skipped += 1
    print(f"事前フィルタ: {len(relevant)}件が関連 / {skipped}件スキップ", file=sys.stderr)
    return relevant


def score_with_claude(postings):
    """Score all postings against the resume using Claude API."""
    # Pre-filter: only send relevant postings to Claude
    postings = _pre_filter_postings(postings)

    if not postings:
        print("関連する案件が見つかりませんでした。", file=sys.stderr)
        return []

    client = anthropic.Anthropic()

    # Build compact posting summaries to minimize tokens
    posting_texts = []
    for i, p in enumerate(postings):
        text = p["full_text"]
        # Truncate each posting to 500 chars max for token efficiency
        if len(text) > 500:
            text = text[:500] + "..."
        posting_texts.append(f"--- 案件{i+1} ---\n{text}")

    all_postings_text = "\n\n".join(posting_texts)

    prompt = f"""以下の候補者プロフィールと案件一覧を比較し、各案件のマッチ度を評価してください。

{RESUME_SUMMARY}

## 案件一覧（{len(postings)}件）

{all_postings_text}

## 回答形式
JSON配列で返してください。各要素は以下の形式:
{{"index": 案件番号(1始まり), "score": マッチ度(0-100), "reason": "マッチ理由を30文字以内で", "matched_skills": ["マッチしたスキル"], "gaps": ["不足スキル(あれば)"]}}

評価基準:
- Android/Kotlin/Java案件は高スコア（80-100）
- モバイル関連案件は中スコア（50-79）
- 経験領域（決済、IoT、D2C）に関連する案件はボーナス
- 全く関係ない技術の案件は低スコア（0-30）
- スコア0の案件も含めて全件返してください

JSON配列のみ返してください。"""

    # Split into batches if too many postings (max ~100 per batch)
    BATCH_SIZE = 100
    if len(postings) > BATCH_SIZE:
        print(f"案件数が多いため {BATCH_SIZE}件ずつバッチ処理します ({len(postings)}件)", file=sys.stderr)
        all_results = []
        for batch_start in range(0, len(postings), BATCH_SIZE):
            batch_postings = postings[batch_start:batch_start + BATCH_SIZE]
            batch_results = _call_claude_batch(client, batch_postings, batch_start)
            all_results.extend(batch_results)
        all_results.sort(key=lambda x: x["total_score"], reverse=True)
        return all_results
    else:
        return _call_claude_batch(client, postings, 0)


def _call_claude_batch(client, postings, offset):
    """Call Claude API for a batch of postings."""
    posting_texts = []
    for i, p in enumerate(postings):
        text = p["full_text"]
        if len(text) > 500:
            text = text[:500] + "..."
        posting_texts.append(f"--- 案件{i+1} ---\n{text}")

    all_postings_text = "\n\n".join(posting_texts)

    prompt = f"""以下の候補者プロフィールと案件一覧を比較し、各案件のマッチ度を評価してください。

{RESUME_SUMMARY}

## 案件一覧（{len(postings)}件）

{all_postings_text}

## 回答形式
JSON配列で返してください。各要素は以下の形式:
{{"index": 案件番号(1始まり), "score": マッチ度(0-100), "reason": "マッチ理由を30文字以内で", "matched_skills": ["マッチしたスキル"], "gaps": ["不足スキル(あれば)"]}}

評価基準:
- Android/Kotlin/Java案件は高スコア（80-100）
- モバイル関連案件は中スコア（50-79）
- 経験領域（決済、IoT、D2C）に関連する案件はボーナス
- 全く関係ない技術の案件は低スコア（0-30）
- スコア0の案件も含めて全件返してください

JSON配列のみ返してください。"""

    batch_label = f"バッチ {offset//100+1}" if offset > 0 else ""
    print(f"Claude APIでマッチング分析中...{batch_label} ({len(postings)}件)", file=sys.stderr)

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = response.content[0].text.strip()
    if "```" in response_text:
        json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', response_text, re.DOTALL)
        if json_match:
            response_text = json_match.group(1).strip()

    scores = json.loads(response_text)

    results = []
    score_map = {s["index"]: s for s in scores}
    for i, posting in enumerate(postings):
        s = score_map.get(i + 1, {"score": 0, "reason": "評価なし", "matched_skills": [], "gaps": []})
        results.append({
            "total_score": s["score"],
            "reason": s.get("reason", ""),
            "matched_skills": s.get("matched_skills", []),
            "gaps": s.get("gaps", []),
            "posting": posting,
        })

    usage = response.usage
    print(f"トークン使用量: 入力={usage.input_tokens}, 出力={usage.output_tokens}", file=sys.stderr)

    return results

# =============================================================================
# Section 7: Output and Main
# =============================================================================


def format_results(results, start_date, end_date):
    """Print results."""
    matched = [r for r in results if r["total_score"] > 0]

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  マッチング結果 ({start_date} 〜 {end_date})", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"  抽出案件数: {len(results)} | マッチ案件: {len(matched)}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    for i, r in enumerate(matched[:20], 1):
        p = r["posting"]
        skills_str = ", ".join(r["matched_skills"][:8])
        print(f"  {i:2d}. [{r['total_score']:3d}点] {p['title'][:50]}", file=sys.stderr)
        print(f"      理由: {r['reason']}", file=sys.stderr)
        print(f"      スキル: {skills_str}", file=sys.stderr)
        if r["gaps"]:
            print(f"      不足: {', '.join(r['gaps'][:5])}", file=sys.stderr)
        if p["rate"]:
            print(f"      単価: {p['rate'][:30]}", file=sys.stderr)
        if p["location"]:
            print(f"      場所: {p['location'][:30]}", file=sys.stderr)
        if p["period"]:
            print(f"      期間: {p['period'][:30]}", file=sys.stderr)
        print(f"      送信元: {p['source_sender'][:40]}", file=sys.stderr)
        print(f"      日付: {p['source_date'][:30]}", file=sys.stderr)
        print(file=sys.stderr)

    # CSV to stdout and file
    csv_lines = ["rank,score,title,reason,matched_skills,gaps,rate,location,period,sender,date,message_id,email_subject"]
    for i, r in enumerate(matched[:50], 1):
        p = r["posting"]
        skills = ";".join(r["matched_skills"][:10])
        gaps = ";".join(r["gaps"][:5])
        def _clean(s, limit=None):
            """Replace quotes and newlines, then optionally truncate."""
            s = s.replace('"', "'").replace('\n', ' ').replace('\r', ' ')
            return s[:limit] if limit else s

        title = _clean(p["title"], 60)
        reason = _clean(r["reason"], 50)
        rate = _clean(p["rate"], 30)
        location = _clean(p["location"], 30)
        period = _clean(p["period"], 30)
        sender = _clean(p["source_sender"])  # no truncation — preserve full email
        date_str = _clean(p["source_date"], 30)
        message_id = p.get("source_id", "")
        email_subject = _clean(p.get("source_subject", ""), 80)
        csv_lines.append(f'{i},{r["total_score"]},"{title}","{reason}","{skills}","{gaps}","{rate}","{location}","{period}","{sender}","{date_str}","{message_id}","{email_subject}"')

    csv_text = "\n".join(csv_lines)
    print(csv_text)

    # Auto-save to file
    csv_name = f"results_{end_date}.csv"
    csv_path = Path(__file__).parent / csv_name
    csv_path.write_text(csv_text + "\n", encoding="utf-8")
    print(f"\nCSV保存: {csv_path}", file=sys.stderr)
    return csv_name


def _get_or_create_label(service, label_name):
    """Get existing label ID or create a new one."""
    results = service.users().labels().list(userId="me").execute()
    for label in results.get("labels", []):
        if label["name"] == label_name:
            print(f"ラベル既存: {label_name}", file=sys.stderr)
            return label["id"]

    body = {
        "name": label_name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    created = service.users().labels().create(userId="me", body=body).execute()
    print(f"ラベル作成: {label_name}", file=sys.stderr)
    return created["id"]


def _apply_label_to_matched(service, results, label_id):
    """Apply Gmail label to emails of matched postings (score > 0)."""
    matched_email_ids = set()
    for r in results:
        if r["total_score"] > 0:
            posting = r["posting"]
            if "id" in posting:
                matched_email_ids.add(posting["id"])

    if not matched_email_ids:
        return 0

    count = 0
    for email_id in matched_email_ids:
        try:
            service.users().messages().modify(
                userId="me", id=email_id,
                body={"addLabelIds": [label_id]}
            ).execute()
            count += 1
        except Exception as e:
            print(f"  ラベル付与エラー ({email_id}): {e}", file=sys.stderr)

    return count


def main():
    if not CREDENTIALS_PATH.exists():
        print("エラー: credentials.json が見つかりません。", file=sys.stderr)
        print("Google Cloud Console からOAuth クライアントIDをダウンロードして", file=sys.stderr)
        print(f"以下に配置してください: {CREDENTIALS_PATH}", file=sys.stderr)
        sys.exit(1)

    print("Gmail API に認証中...", file=sys.stderr)
    service = authenticate()

    start_date, end_date = get_last_n_business_days(1)
    print(f"検索期間: {start_date} 〜 {end_date}", file=sys.stderr)

    emails = fetch_job_emails(service, start_date, end_date)

    if not emails:
        print("該当期間に案件メールが見つかりませんでした。", file=sys.stderr)
        sys.exit(0)

    postings = []
    for email in emails:
        extracted = extract_job_postings(email)
        # Carry email ID to postings for label tagging
        for p in extracted:
            p["id"] = email["id"]
        postings.extend(extracted)

    print(f"抽出案件数: {len(postings)}", file=sys.stderr)

    if not postings:
        print("案件情報を抽出できませんでした。", file=sys.stderr)
        sys.exit(0)

    results = score_with_claude(postings)
    csv_name = format_results(results, start_date, end_date)

    # Apply Gmail label (same name as CSV file, without .csv)
    label_name = csv_name.replace(".csv", "")
    print(f"\nGmailラベル付与中: {label_name}", file=sys.stderr)
    label_id = _get_or_create_label(service, label_name)
    count = _apply_label_to_matched(service, results, label_id)
    print(f"ラベル付与完了: {count}件のメールに '{label_name}' を付与", file=sys.stderr)


if __name__ == "__main__":
    main()
