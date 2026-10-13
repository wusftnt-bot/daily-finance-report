import copy
import datetime as dt
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from telegram_receipts import GitHub, SafeRedirect, confirmed_receipt, validate_receipt
from telegram_morning_orchestrator import TW, coordinate


def receipt():
    rows = []
    for label in ("daily_finance", "technology_news", "jt_dashboard"):
        for recipient in (1, 2):
            for chunk in (1, 2):
                rows.append(dict(label=label, recipient_index=recipient, recipient_count=2,
                                 chunk_index=chunk, chunk_count=2, ok=True, message_id=len(rows) + 1))
    return dict(date="2026-10-13", recipient_count=2, receipt_count=len(rows),
                expected_messages=["daily_finance", "technology_news", "jt_dashboard"], receipts=rows)


class API:
    def __init__(self, data=None, active=False):
        self.calls = []
        self.data = data
        self.active = active

    def read(self, path, payload=None, binary=False):
        self.calls.append((path, payload))
        if payload is not None:
            return {}
        if path.startswith('/actions/artifacts?'):
            return {"artifacts": [] if self.data is None else [{"id": 1, "name": "daily-telegram-2026-10-13", "workflow_run": {"id": 1}}]}
        if path == '/actions/runs/1':
            return dict(path=".github/workflows/daily-telegram.yml", head_branch="main", conclusion="success", status="completed")
        if path.endswith('/zip'):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w') as bundle:
                bundle.writestr('daily-telegram-2026-10-13.json', json.dumps(self.data))
            return buf.getvalue()
        if '/runs?' in path:
            return {"workflow_runs": [dict(created_at="2026-10-12T22:00:00Z", status="in_progress")] if self.active else []}
        raise AssertionError(path)


class ReceiptTests(unittest.TestCase):
    def test_all_messages_all_recipients_all_chunks(self):
        self.assertTrue(validate_receipt(receipt(), '2026-10-13'))
        self.assertTrue(confirmed_receipt(API(receipt()), '2026-10-13'))

    def test_partial_duplicate_wrong_date_and_false_confirmation_rejected(self):
        cases = []
        data = receipt(); data['receipts'].pop(); data['receipt_count'] -= 1; cases.append(data)
        data = receipt(); data['receipts'][0]['ok'] = False; cases.append(data)
        data = receipt(); data['date'] = '2026-10-12'; cases.append(data)
        data = receipt(); data['receipts'][1] = copy.deepcopy(data['receipts'][0]); cases.append(data)
        data = receipt(); data['recipient_count'] = 3; cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    validate_receipt(data, '2026-10-13')
                self.assertFalse(confirmed_receipt(API(data), '2026-10-13'))

    def test_artifact_name_alone_is_not_a_receipt(self):
        data = receipt(); data['receipts'] = []; data['receipt_count'] = 0
        self.assertFalse(confirmed_receipt(API(data), '2026-10-13'))

    def test_download_redirect_does_not_forward_bearer(self):
        req = urllib.request.Request('https://api.github.com/example', headers={'Authorization': 'Bearer TEST'})
        redirected = SafeRedirect().redirect_request(req, None, 302, '', {}, 'https://storage.example.test/download')
        self.assertIsNone(redirected.get_header('Authorization'))

    def test_valid_receipt_survives_failure_in_later_cache_steps(self):
        class FailedPostStep(API):
            def read(self, path, payload=None, binary=False):
                data = super().read(path, payload, binary)
                if path == '/actions/runs/1':
                    data['conclusion'] = 'failure'
                return data
        self.assertTrue(confirmed_receipt(FailedPostStep(receipt()), '2026-10-13'))

    def test_diagnostic_exception_cannot_print_credential_context(self):
        api = GitHub('TEST_CREDENTIAL', 'owner/repo')
        with patch('telegram_receipts.urllib.request.build_opener', side_effect=ValueError('TEST_CREDENTIAL')):
            with self.assertRaises(RuntimeError) as error:
                api.read('/actions/runs')
        self.assertNotIn('TEST_CREDENTIAL', str(error.exception))


class CoordinationTests(unittest.TestCase):
    def now(self, hour=6, minute=30):
        return dt.datetime(2026, 10, 13, hour, minute, tzinfo=TW)

    def mutations(self, api):
        return [call for call in api.calls if call[1] is not None]

    def test_stale_site_dispatches_only_guarded_generation(self):
        site, telegram = API(), API()
        self.assertEqual(coordinate(site, telegram, self.now(), False), 'site_dispatched')
        self.assertEqual(self.mutations(site)[0][1]['inputs'], {'external_trigger': 'true'})
        self.assertFalse(self.mutations(telegram))

    def test_active_site_or_delivery_is_not_duplicated(self):
        site, telegram = API(active=True), API()
        self.assertEqual(coordinate(site, telegram, self.now(), False), 'site_pending')
        self.assertFalse(self.mutations(site))
        site, telegram = API(), API(active=True)
        self.assertEqual(coordinate(site, telegram, self.now(), True), 'telegram_pending')
        self.assertFalse(self.mutations(telegram))

    def test_complete_receipt_prevents_duplicate_dispatch(self):
        site, telegram = API(), API(receipt())
        self.assertEqual(coordinate(site, telegram, self.now(), True), 'complete')
        self.assertFalse(self.mutations(telegram))

    def test_ready_page_dispatches_only_watchdog(self):
        site, telegram = API(), API()
        self.assertEqual(coordinate(site, telegram, self.now(), True), 'telegram_dispatched')
        self.assertIn('telegram-delivery-watchdog.yml', self.mutations(telegram)[0][0])
        self.assertFalse(self.mutations(site))

    def test_final_missing_receipt_fails_even_if_run_is_active(self):
        site, telegram = API(), API(active=True)
        with self.assertRaises(RuntimeError):
            coordinate(site, telegram, self.now(7, 55), True, final=True)
        self.assertFalse(self.mutations(telegram))

    def test_final_complete_and_delayed_runs(self):
        self.assertEqual(coordinate(API(), API(receipt()), self.now(7, 55), True, final=True), 'complete')
        with self.assertRaises(RuntimeError):
            coordinate(API(), API(), self.now(11, 30), True, final=True)
        self.assertEqual(coordinate(API(), API(), self.now(11, 30), True), 'outside_window')

    def test_dry_run_never_mutates_at_any_time(self):
        site, telegram = API(), API()
        self.assertEqual(coordinate(site, telegram, self.now(12, 0), True, dry_run=True), 'checked')
        self.assertFalse(self.mutations(site)); self.assertFalse(self.mutations(telegram))


if __name__ == '__main__':
    unittest.main()
