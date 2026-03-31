"""
add_labels.py

Reads results_2026-03-27.csv and applies the Gmail label
'results_2026-03-27' to the top 50 matching emails.
"""

import csv
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Reuse auth helpers from match_jobs.py
from match_jobs import authenticate, load_env

load_env()

CSV_FILE = Path(__file__).parent / "results_2026-03-27.csv"
LABEL_NAME = "results_2026-03-27"


# ---------------------------------------------------------------------------
# Helper: extract bare email address from a From header value
# Handles both:
#   'Name' <email@domain.com>
#   email@domain.com
# ---------------------------------------------------------------------------

def extract_email(sender: str) -> str:
    """Extract bare email address from a From header value.

    Handles:
      'Name' <email@domain.com>     -> email@domain.com
      'Name' <truncated@partial     -> truncated@partial  (truncated CSV field)
      email@domain.com              -> email@domain.com
    """
    if not sender:
        return ""
    # Full angle-bracket form  <email>
    m = re.search(r'<([^>]+@[^>]+)>', sender)
    if m:
        return m.group(1).strip().lower()
    # Truncated angle-bracket form  <partial  (no closing >)
    m = re.search(r'<([^\s>]+@[^\s>]+)', sender)
    if m:
        return m.group(1).strip().lower()
    return sender.strip().lower()


# ---------------------------------------------------------------------------
# Gmail label helpers
# ---------------------------------------------------------------------------

def get_or_create_label(service, label_name: str) -> str:
    """Return label id, creating the label if it does not exist."""
    result = service.users().labels().list(userId="me").execute()
    for lbl in result.get("labels", []):
        if lbl["name"].lower() == label_name.lower():
            print(f"既存ラベル使用: {label_name} (id={lbl['id']})")
            return lbl["id"]

    # Create new label
    body = {
        "name": label_name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    created = service.users().labels().create(userId="me", body=body).execute()
    print(f"ラベル作成: {label_name} (id={created['id']})")
    return created["id"]


def apply_label(service, message_id: str, label_id: str):
    """Add label to a single message."""
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": [label_id]},
    ).execute()


# ---------------------------------------------------------------------------
# Gmail search helpers
# ---------------------------------------------------------------------------

def parse_date(date_str: str):
    """Parse a partial RFC 2822 date string into a datetime (best effort)."""
    # Normalise timezone: +090 -> +0900, +000 -> +0000
    date_str = re.sub(r'(\+0{2,3})$', r'\g<1>0', date_str.strip())
    date_str = re.sub(r'(-0{2,3})$', r'\g<1>0', date_str.strip())

    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%d %b %Y %H:%M:%S %z",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def is_truncated_email(email: str) -> bool:
    """Return True if the email address looks like it was cut mid-domain."""
    if not email or '@' not in email:
        return False
    domain = email.split('@', 1)[1]
    # Truncated if domain has no dot, or ends with a letter that looks cut off
    # (real TLDs have dots, e.g. .jp .com .co.jp)
    return '.' not in domain


def build_query(email: str, title: str, date_str: str, use_prefix: bool = False) -> str:
    """Build a Gmail search query for a specific email.

    When use_prefix=True (truncated sender), search with just the username@ prefix
    so Gmail can match any domain completion.
    """
    if use_prefix and '@' in email:
        username = email.split('@')[0]
        from_term = f"from:{username}"
    else:
        from_term = f"from:{email}"
    query_parts = [from_term]

    # Add subject fragment (first 20 chars, stripped of special chars)
    title_fragment = re.sub(r'["\'\[\]{}()<>]', '', title)[:20].strip()
    if title_fragment:
        query_parts.append(f'subject:"{title_fragment}"')

    # Add date window: ±1 day around the email date
    dt = parse_date(date_str)
    if dt:
        after = (dt - timedelta(days=1)).strftime("%Y/%m/%d")
        before = (dt + timedelta(days=2)).strftime("%Y/%m/%d")
        query_parts.append(f"after:{after}")
        query_parts.append(f"before:{before}")

    return " ".join(query_parts)


def find_message(service, email: str, title: str, date_str: str):
    """
    Search Gmail for the message. Returns message id or None.
    If multiple results, pick the one whose internal date is closest.
    Handles truncated email addresses by falling back to username-only search.
    """
    truncated = is_truncated_email(email)

    query = build_query(email, title, date_str, use_prefix=truncated)
    print(f"  検索: {query}" + (" [truncated-email]" if truncated else ""))

    results = service.users().messages().list(
        userId="me", q=query, maxResults=10
    ).execute()
    messages = results.get("messages", [])

    if not messages:
        # Fallback 1: search only by sender (without subject)
        if truncated:
            username = email.split('@')[0]
            fallback_query = f"from:{username}"
        else:
            fallback_query = f"from:{email}"
        dt = parse_date(date_str)
        if dt:
            after = (dt - timedelta(days=1)).strftime("%Y/%m/%d")
            before = (dt + timedelta(days=2)).strftime("%Y/%m/%d")
            fallback_query += f" after:{after} before:{before}"
        print(f"  フォールバック検索1: {fallback_query}")
        results = service.users().messages().list(
            userId="me", q=fallback_query, maxResults=10
        ).execute()
        messages = results.get("messages", [])

    if not messages and truncated:
        # Fallback 2 for truncated: try the partial email string as-is (Gmail is flexible)
        partial_fallback = f"from:{email}"
        dt = parse_date(date_str)
        if dt:
            after = (dt - timedelta(days=1)).strftime("%Y/%m/%d")
            before = (dt + timedelta(days=2)).strftime("%Y/%m/%d")
            partial_fallback += f" after:{after} before:{before}"
        print(f"  フォールバック検索2 (partial): {partial_fallback}")
        results = service.users().messages().list(
            userId="me", q=partial_fallback, maxResults=10
        ).execute()
        messages = results.get("messages", [])

    if not messages:
        return None

    if len(messages) == 1:
        return messages[0]["id"]

    # Multiple results: pick closest in date
    target_dt = parse_date(date_str)
    if not target_dt:
        return messages[0]["id"]

    best_id = None
    best_delta = None
    for msg in messages:
        meta = service.users().messages().get(
            userId="me", id=msg["id"], format="metadata",
            metadataHeaders=["Date"]
        ).execute()
        headers = {h["name"]: h["value"]
                   for h in meta["payload"].get("headers", [])}
        msg_date_str = headers.get("Date", "")
        msg_dt = parse_date(msg_date_str)
        if msg_dt:
            delta = abs((msg_dt - target_dt).total_seconds())
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_id = msg["id"]

    return best_id or messages[0]["id"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Gmail API に認証中...")
    service = authenticate()

    # Ensure label exists
    label_id = get_or_create_label(service, LABEL_NAME)

    # Read CSV
    rows = []
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    print(f"CSV行数: {len(rows)}")

    # Split rows into: known message IDs (new CSV format) vs. search-needed (old format)
    message_ids: set = set()
    search_needed = []

    for row in rows[:50]:
        msg_id = (row.get("message_id") or "").strip()
        if msg_id:
            message_ids.add(msg_id)
        else:
            search_needed.append(row)

    found = 0
    not_found = 0

    # --- Apply labels to known message IDs directly (no search required) ---
    if message_ids:
        print(f"\nメッセージID直接指定: {len(message_ids)}件")
        for msg_id in message_ids:
            try:
                apply_label(service, msg_id, label_id)
                print(f"  ラベル付与完了 (message_id={msg_id})")
                found += 1
            except Exception as e:
                print(f"  ラベル付与エラー (message_id={msg_id}): {e}")
                not_found += 1

    # --- Search-based fallback for old CSV format rows ---
    if search_needed:
        print(f"\n検索による処理 (旧CSVフォーマット): {len(search_needed)}件")
        for i, row in enumerate(search_needed, 1):
            sender   = (row.get("sender") or "").strip()
            title    = (row.get("title")  or "").strip()
            date_str = (row.get("date")   or "").strip()
            rank     = row.get("rank", str(i))

            email_addr = extract_email(sender)
            print(f"\n[{rank}] {title[:40]} | {email_addr}")

            if not email_addr:
                print("  送信元メールアドレスが不明、スキップ")
                not_found += 1
                continue

            msg_id = find_message(service, email_addr, title, date_str)
            if msg_id:
                apply_label(service, msg_id, label_id)
                print(f"  ラベル付与完了 (message_id={msg_id})")
                found += 1
            else:
                print("  メッセージが見つかりませんでした")
                not_found += 1

    print(f"\n完了: {found}件にラベル付与 / {not_found}件は未発見")


if __name__ == "__main__":
    main()
