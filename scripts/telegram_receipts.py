"""Credential-free receipt contract shared by the website and Telegram workflows."""

import io
import json
import urllib.error
import urllib.request
import zipfile

LABELS = {"daily_finance", "technology_news", "jt_dashboard"}


def positive_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate_receipt(data, today):
    if not isinstance(data, dict):
        raise ValueError("Receipt must be a JSON object.")
    if data.get("date") != today:
        raise ValueError("Receipt is not for the current Taipei date.")
    recipients = data.get("recipient_count")
    if not positive_integer(recipients) or recipients > 1000:
        raise ValueError("Invalid receipt recipient count.")
    rows = data.get("receipts")
    if not isinstance(rows, list) or data.get("receipt_count") != len(rows):
        raise ValueError("Receipt count does not match receipt rows.")
    if set(data.get("expected_messages", [])) != LABELS:
        raise ValueError("Receipt does not declare all three daily messages.")
    groups = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Receipt rows must be JSON objects.")
        label, recipient = row.get("label"), row.get("recipient_index")
        chunk, count = row.get("chunk_index"), row.get("chunk_count")
        if label not in LABELS or not positive_integer(recipient) or recipient > recipients:
            raise ValueError("Invalid receipt message or recipient index.")
        if row.get("recipient_count") != recipients or row.get("ok") is not True:
            raise ValueError("Receipt contains an unconfirmed delivery.")
        if not positive_integer(chunk) or not positive_integer(count) or chunk > count or count > 1000:
            raise ValueError("Invalid receipt chunk index.")
        if not positive_integer(row.get("message_id")):
            raise ValueError("Receipt lacks a Telegram message confirmation.")
        group = groups.setdefault((label, recipient), {"count": count, "chunks": set()})
        if group["count"] != count or chunk in group["chunks"]:
            raise ValueError("Duplicate or inconsistent receipt chunks.")
        group["chunks"].add(chunk)
    expected = {(label, recipient) for label in LABELS for recipient in range(1, recipients + 1)}
    if set(groups) != expected:
        raise ValueError("Receipt is missing a message for one or more recipients.")
    if any(group["chunks"] != set(range(1, group["count"] + 1)) for group in groups.values()):
        raise ValueError("Receipt is missing a Telegram message chunk.")
    for label in LABELS:
        if len({groups[(label, recipient)]["count"] for recipient in range(1, recipients + 1)}) != 1:
            raise ValueError("Recipients did not receive the same complete message plan.")
    return True


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")
        return redirected


class GitHub:
    def __init__(self, token, repo):
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError("GitHub integration token is missing.")
        token = "".join(token.split())
        self.base = "https://api.github.com/repos/" + repo
        self.headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                        "Content-Type": "application/json", "User-Agent": "telegram-receipt-check",
                        "X-GitHub-Api-Version": "2022-11-28"}

    def read(self, path, payload=None, binary=False):
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=body, headers=self.headers,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.build_opener(SafeRedirect()).open(req, timeout=30) as response:
                result = response.read(2_000_001 if binary else 5_000_001)
                if len(result) > (2_000_000 if binary else 5_000_000):
                    raise ValueError("GitHub response exceeded verification size limit.")
                return result if binary else (json.loads(result) if result else {})
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"GitHub request failed: HTTP {exc.code}.") from None
        except urllib.error.URLError:
            raise RuntimeError("GitHub request failed: network unavailable.") from None
        except Exception as exc:
            raise RuntimeError(f"GitHub verification request failed: {type(exc).__name__}.") from None


def confirmed_receipt(api, today):
    artifacts = api.read(f"/actions/artifacts?name=daily-telegram-{today}&per_page=30").get("artifacts", [])
    expected_name = f"daily-telegram-{today}.json"
    for artifact in artifacts:
        if artifact.get("expired") or artifact.get("name") != f"daily-telegram-{today}":
            continue
        run_id = artifact.get("workflow_run", {}).get("id")
        if not run_id:
            continue
        run = api.read(f"/actions/runs/{run_id}")
        if run.get("path") != ".github/workflows/daily-telegram.yml" or run.get("head_branch") != "main":
            continue
        # Confirmed Telegram chunks remain delivered even if later cache/post steps fail.
        try:
            archive = api.read(f'/actions/artifacts/{artifact["id"]}/zip', binary=True)
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                files = [info for info in bundle.infolist() if info.filename == expected_name]
                if len(files) != 1 or files[0].file_size > 1_000_000:
                    continue
                data = json.loads(bundle.read(files[0]))
            validate_receipt(data, today)
            return True
        except (ValueError, KeyError, zipfile.BadZipFile):
            print("Rejected an incomplete or invalid Telegram receipt artifact.")
    return False
