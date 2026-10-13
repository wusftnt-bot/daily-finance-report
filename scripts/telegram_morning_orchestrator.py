"""External-cron entry point; Telegram/LINE credentials never enter this repo."""

import datetime as dt
import os
import re
import urllib.request

from telegram_receipts import GitHub, confirmed_receipt

TW = dt.timezone(dt.timedelta(hours=8))


def active_run(api, workflow, today):
    runs = api.read(f"/actions/workflows/{workflow}/runs?per_page=30").get("workflow_runs", [])
    for run in runs:
        started = dt.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")).astimezone(TW)
        if started.date().isoformat() == today and run.get("status") in {"queued", "in_progress", "waiting", "pending"}:
            return True
    return False


def coordinate(site, telegram, now, page_current, final=False, dry_run=False):
    today = now.date().isoformat()
    minute = now.hour * 60 + now.minute
    if dry_run:
        print(f"DRY RUN: website current={page_current}; complete Telegram receipt={confirmed_receipt(telegram, today)}.")
        return "checked"
    if final:
        if not 7 * 60 + 50 <= minute <= 8 * 60 + 15:
            raise RuntimeError("Final receipt check must run during Taipei 07:50-08:15.")
        complete = confirmed_receipt(telegram, today)
        if not page_current or not complete:
            raise RuntimeError("Final check failed: today's website or complete Telegram receipt is missing.")
        print("PASS: today's website and all recipients/chunks of all three messages are confirmed.")
        return "complete"
    if not 5 * 60 + 20 <= minute <= 7 * 60 + 35:
        print("Morning coordination skipped outside Taipei 05:20-07:35.")
        return "outside_window"
    if page_current and confirmed_receipt(telegram, today):
        print("Complete receipt verified; no duplicate Telegram dispatch.")
        return "complete"
    if not page_current:
        if minute > 7 * 60 + 25:
            raise RuntimeError("Today's website is missing and the safe generation window has passed.")
        if active_run(site, "publish-daily-finance-report.yml", today):
            print("Report generation is active; next cron tick will check again.")
            return "site_pending"
        site.read("/actions/workflows/publish-daily-finance-report.yml/dispatches", {
            "ref": "main", "inputs": {"external_trigger": "true"},
        })
        print("Dispatched guarded website generation; Telegram waits for today's published data.")
        return "site_dispatched"
    if active_run(telegram, "daily-telegram.yml", today):
        print("Telegram delivery is active; no duplicate dispatch.")
        return "telegram_pending"
    telegram.read("/actions/workflows/telegram-delivery-watchdog.yml/dispatches", {
        "ref": "main", "inputs": {"final_check": "false"},
    })
    print("Dispatched receipt-verifying Telegram watchdog.")
    return "telegram_dispatched"


def main():
    now = dt.datetime.now(TW)
    request = urllib.request.Request("https://wusftnt-bot.github.io/daily-finance-report/?cron=" + now.date().isoformat(),
                                     headers={"Cache-Control": "no-cache", "User-Agent": "morning-cron-check"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            html = response.read().decode("utf-8")
        match = re.search(r'<meta name="report-date" content="([^"]+)">', html)
        page_current = bool(match and match.group(1) == now.date().isoformat())
    except Exception:
        page_current = False
    site = GitHub(os.environ["GITHUB_TOKEN"], "wusftnt-bot/daily-finance-report")
    telegram = GitHub(os.environ["JT_PM_ACTIONS_TOKEN"], "wusftnt-bot/JT-PM")
    coordinate(site, telegram, now, page_current,
               final=os.environ.get("FINAL_CHECK") == "true" or now.hour * 60 + now.minute >= 7 * 60 + 50,
               dry_run=os.environ.get("DRY_RUN") == "true")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError) as exc:
        raise SystemExit(str(exc)) from None
