"""
retry_labels.py

Re-applies the Gmail label 'results_2026-03-27' (id=Label_12) to the 7
rows that failed in the original add_labels.py run:

  Rank  2 – truncated sender: search-a@onewedge.c  (ONE WEDGE)
  Rank  4 – truncated sender: search-a@onewedge.c  (ONE WEDGE)
  Rank 19 – truncated sender: sales99@shonan-style
  Rank 20 – CSV parse error  (某地方銀行のアプリ基盤作業及び開発作業)
  Rank 35 – CSV parse error  (老朽化した2つの既存システムを統合し、)
  Rank 38 – CSV parse error  (受託開発業務及びjava研修講師業務)
  Rank 42 – CSV parse error  (システムリプレース案件のPM及び既存システムの保守)

For truncated-sender rows the username part before '@' is used with Gmail's
flexible from: search.  For CSV-parse-error rows only a subject fragment
plus a generous date window is used.
"""

import re
import sys
from datetime import datetime, timedelta

from match_jobs import authenticate, load_env

load_env()

LABEL_ID = "Label_12"
LABEL_NAME = "results_2026-03-27"

# ---------------------------------------------------------------------------
# Row definitions — each entry is (rank, title_fragment, email_hint, date_str)
#
#  email_hint:
#    - full address            → use as-is
#    - "username@"             → truncated; search with from:username
#    - ""                      → no sender hint; use subject + date only
#
#  date_str: best-effort date from the CSV (used for ±2 day window)
# ---------------------------------------------------------------------------

FAILED_ROWS = [
    # rank  title_fragment (>=15 unique chars)                  email_hint                          date_str
    (2,  "オーダー総合マネジメントシステム保守",              "search-a@",   "Fri, 27 Mar 2026 09:10:03 +0900"),
    (4,  "介護業界向けAndroidアプリ",                          "search-a@",   "Thu, 26 Mar 2026 13:50:02 +0900"),
    (19, "医薬品SaaSへのデータ連携Webシステム",               "sales99@",    "Fri, 27 Mar 2026 08:00:54 +0900"),
    # CSV-parse-error rows – no reliable sender; use subject + wide date window
    (20, "某地方銀行のアプリ基盤作業及び開発作業",            "",            "Thu, 26 Mar 2026 12:00:00 +0900"),
    (35, "老朽化した2つの既存システムを統合",                 "",            "Thu, 26 Mar 2026 12:00:00 +0900"),
    (38, "受託開発業務及びjava研修講師業務",                  "",            "Thu, 26 Mar 2026 12:00:00 +0900"),
    (42, "システムリプレース案件のPM及び既存システムの保守",  "",            "Thu, 26 Mar 2026 12:00:00 +0900"),
]

DATE_WINDOW_DAYS = 2   # ±days around the date hint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_date(date_str: str):
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


def build_queries(rank, title_fragment, email_hint, date_str):
    """
    Yield a sequence of increasingly broad queries to try.
    """
    dt = parse_date(date_str)
    date_parts = []
    if dt:
        after = (dt - timedelta(days=DATE_WINDOW_DAYS)).strftime("%Y/%m/%d")
        before = (dt + timedelta(days=DATE_WINDOW_DAYS + 1)).strftime("%Y/%m/%d")
        date_parts = [f"after:{after}", f"before:{before}"]

    # Strip special chars from title for subject search
    subj = re.sub(r'["\'\[\]{}()<>・■◆]', '', title_fragment)[:25].strip()

    from_parts = []
    if email_hint:
        # email_hint is either "username@" (truncated) or a full address
        if email_hint.endswith('@'):
            username = email_hint.rstrip('@')
            from_parts = [f"from:{username}"]
        else:
            from_parts = [f"from:{email_hint}"]

    queries = []

    # Query 1: from + subject + date
    if from_parts and subj:
        q = " ".join(from_parts + [f'subject:"{subj}"'] + date_parts)
        queries.append(("from+subject+date", q))

    # Query 2: subject + date (works even without sender)
    if subj:
        q = " ".join([f'subject:"{subj}"'] + date_parts)
        queries.append(("subject+date", q))

    # Query 3: from + date only
    if from_parts:
        q = " ".join(from_parts + date_parts)
        queries.append(("from+date", q))

    # Query 4: widened date window ±4 days with subject
    if subj and dt:
        after2 = (dt - timedelta(days=4)).strftime("%Y/%m/%d")
        before2 = (dt + timedelta(days=5)).strftime("%Y/%m/%d")
        q = f'subject:"{subj}" after:{after2} before:{before2}'
        queries.append(("subject+wide-date", q))

    return queries


def find_message(service, rank, title_fragment, email_hint, date_str):
    """Try each query in turn; return first message id found."""
    for strategy, query in build_queries(rank, title_fragment, email_hint, date_str):
        print(f"  [{strategy}] {query}")
        results = service.users().messages().list(
            userId="me", q=query, maxResults=10
        ).execute()
        messages = results.get("messages", [])
        if messages:
            if len(messages) == 1:
                print(f"  -> 1件ヒット")
                return messages[0]["id"]
            # Multiple hits: pick closest date
            target_dt = parse_date(date_str)
            if not target_dt:
                print(f"  -> {len(messages)}件ヒット (先頭を使用)")
                return messages[0]["id"]
            best_id, best_delta = None, None
            for msg in messages:
                meta = service.users().messages().get(
                    userId="me", id=msg["id"], format="metadata",
                    metadataHeaders=["Date"]
                ).execute()
                headers = {h["name"]: h["value"]
                           for h in meta["payload"].get("headers", [])}
                msg_dt = parse_date(headers.get("Date", ""))
                if msg_dt:
                    delta = abs((msg_dt - target_dt).total_seconds())
                    if best_delta is None or delta < best_delta:
                        best_delta, best_id = delta, msg["id"]
            if best_id:
                print(f"  -> {len(messages)}件ヒット (最近傍を選択)")
                return best_id
            return messages[0]["id"]
    return None


def apply_label(service, message_id: str, label_id: str):
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": [label_id]},
    ).execute()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Gmail API に認証中...")
    service = authenticate()

    # Verify label exists
    result = service.users().labels().list(userId="me").execute()
    label_name_found = None
    for lbl in result.get("labels", []):
        if lbl["id"] == LABEL_ID:
            label_name_found = lbl["name"]
            break
    if label_name_found:
        print(f"ラベル確認: {label_name_found} (id={LABEL_ID})")
        effective_label_id = LABEL_ID
    else:
        print(f"警告: ラベル id={LABEL_ID} が見つかりません。{LABEL_NAME} を新規作成します。")
        body = {
            "name": LABEL_NAME,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        created = service.users().labels().create(userId="me", body=body).execute()
        effective_label_id = created["id"]
        print(f"ラベル作成: {LABEL_NAME} (id={effective_label_id})")

    found = 0
    not_found = 0

    for rank, title_fragment, email_hint, date_str in FAILED_ROWS:
        print(f"\n=== Rank {rank}: {title_fragment[:30]} ===")
        msg_id = find_message(service, rank, title_fragment, email_hint, date_str)
        if msg_id:
            apply_label(service, msg_id, effective_label_id)
            print(f"  ラベル付与完了 (message_id={msg_id})")
            found += 1
        else:
            print(f"  メッセージが見つかりませんでした")
            not_found += 1

    print(f"\n完了: {found}件にラベル付与 / {not_found}件は未発見")


if __name__ == "__main__":
    main()
