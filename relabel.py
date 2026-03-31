"""
relabel.py — Apply Gmail label 'results_2026-03-27' (Label_12) to emails
that correspond to the 50 rows in results_2026-03-27.csv.

Strategy:
1. Parse CSV to extract unique (sender_email, date) pairs.
2. For each unique pair, search Gmail with from:{email} + date window.
3. Match by comparing the Date header (within 120 seconds tolerance).
4. Apply Label_12 to verified matches only.
"""

import csv
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from match_jobs import authenticate, load_env

import warnings
warnings.filterwarnings("ignore")

LABEL_ID = "Label_12"
CSV_PATH = Path(__file__).parent / "results_2026-03-27.csv"
DATE_TOLERANCE_SECS = 120


def extract_email_address(sender_field: str) -> str:
    if not sender_field:
        return ""
    m = re.search(r'<([^>]+)>', sender_field)
    if m:
        return m.group(1).strip()
    m = re.search(r'<([^\s>]+@[^\s>]+)', sender_field)
    if m:
        return m.group(1).strip()
    if "@" in sender_field:
        return sender_field.strip().strip("'\"")
    return ""


def is_truncated_email(email: str) -> bool:
    if "@" not in email:
        return True
    domain = email.split("@", 1)[1]
    if "." not in domain:
        return True
    # TLD that is too short (e.g., "onewedge.c" instead of "onewedge.co.jp")
    tld = domain.split(".")[-1]
    return len(tld) < 2


def gmail_search_from(email: str) -> str:
    if is_truncated_email(email):
        username = email.split("@")[0]
        return f"from:{username}"
    return f"from:{email}"


def parse_date(date_str: str):
    if not date_str:
        return None
    # Remove trailing timezone name like "(UTC)", "(JST)"
    normalised = re.sub(r'\s*\([A-Z]+\)\s*$', '', date_str.strip())
    # Fix truncated timezone: +090 → +0900
    normalised = re.sub(r'([+-])(\d{3})$', r'\1\g<2>0', normalised)
    for fmt in ["%a, %d %b %Y %H:%M:%S %z", "%d %b %Y %H:%M:%S %z"]:
        try:
            return datetime.strptime(normalised, fmt)
        except ValueError:
            continue
    return None


def main():
    load_env()
    print("Gmail API に認証中...", file=sys.stderr)
    service = authenticate()

    # CSV読み込み — (sender_email, date_parsed) ペアでユニーク化
    rows = []
    skipped = 0
    text = CSV_PATH.read_text(encoding="utf-8")
    reader = csv.DictReader(text.splitlines())
    for i, row in enumerate(reader, start=2):
        sender = (row.get("sender") or "").strip()
        date_str = (row.get("date") or "").strip()
        rank = row.get("rank", "?")
        email_addr = extract_email_address(sender)
        dt = parse_date(date_str)
        if not email_addr or not dt:
            print(f"  [SKIP row {i}] sender={sender!r} date={date_str!r}", file=sys.stderr)
            skipped += 1
            continue
        rows.append({"rank": rank, "email": email_addr, "dt": dt})

    print(f"CSV: {len(rows)} valid, {skipped} skipped", file=sys.stderr)

    # (sender_email, date) ペアでグループ化（同一メール内の複数案件を統合）
    unique_pairs = {}
    for r in rows:
        key = (r["email"], r["dt"])
        if key not in unique_pairs:
            unique_pairs[key] = []
        unique_pairs[key].append(r["rank"])

    print(f"ユニークな (sender, date) ペア: {len(unique_pairs)}", file=sys.stderr)

    labeled_ids = set()
    not_found = []

    for (email_addr, csv_dt), ranks in unique_pairs.items():
        ranks_str = ",".join(ranks)
        from_clause = gmail_search_from(email_addr)
        after = (csv_dt - timedelta(days=1)).strftime("%Y/%m/%d")
        before = (csv_dt + timedelta(days=2)).strftime("%Y/%m/%d")
        query = f"{from_clause} after:{after} before:{before}"

        print(f"\n[Rank {ranks_str}] {email_addr} {csv_dt.strftime('%m/%d %H:%M')}", file=sys.stderr)
        print(f"  検索: {query}", file=sys.stderr)

        results = service.users().messages().list(
            userId="me", q=query, maxResults=100
        ).execute()
        messages = results.get("messages", [])

        if not messages:
            print(f"  メッセージなし", file=sys.stderr)
            not_found.append((ranks_str, email_addr))
            time.sleep(0.3)
            continue

        # 各候補のDateヘッダーを取得し、CSVの日付と比較
        best_id = None
        best_delta = None
        for msg in messages:
            meta = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["Date"]
            ).execute()
            headers = {h["name"]: h["value"] for h in meta["payload"].get("headers", [])}
            msg_dt = parse_date(headers.get("Date", ""))
            if msg_dt:
                delta = abs((msg_dt - csv_dt).total_seconds())
                if delta <= DATE_TOLERANCE_SECS:
                    if best_delta is None or delta < best_delta:
                        best_delta = delta
                        best_id = msg["id"]

        if best_id and best_id not in labeled_ids:
            service.users().messages().modify(
                userId="me", id=best_id,
                body={"addLabelIds": [LABEL_ID]}
            ).execute()
            labeled_ids.add(best_id)
            print(f"  ラベル付与: {best_id} (差分{best_delta:.0f}秒)", file=sys.stderr)
        elif best_id:
            print(f"  既にラベル済み: {best_id}", file=sys.stderr)
        else:
            print(f"  日付一致なし ({len(messages)}件中)", file=sys.stderr)
            not_found.append((ranks_str, email_addr))

        time.sleep(0.3)

    print(f"\n{'='*50}", file=sys.stderr)
    print(f"完了: {len(labeled_ids)}件のユニークメールにラベル付与", file=sys.stderr)
    if not_found:
        print(f"未発見: {len(not_found)}件", file=sys.stderr)
        for r, e in not_found:
            print(f"  Rank {r}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
