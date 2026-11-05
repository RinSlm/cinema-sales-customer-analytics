"""Business invariants and failure cases for the reproducible CSV build."""
from pathlib import Path
from tempfile import TemporaryDirectory
import shutil
import sys
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from build_from_raw import build


class CinemaPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generated = TemporaryDirectory(prefix='.test-build-', dir=ROOT)
        cls.output = Path(cls.generated.name)
        cls.report = build(ROOT / 'data/raw', cls.output, 'Europe/Moscow', 'exact')

    @classmethod
    def tearDownClass(cls):
        cls.generated.cleanup()

    def test_published_business_totals_reconcile(self):
        self.assertEqual(self.report['schedule_sessions'], 1963)
        self.assertEqual(self.report['ticket_rows'], 9643)
        self.assertEqual(self.report['ticket_customers'], 9573)
        self.assertAlmostEqual(self.report['ticket_revenue'], 2876035.0, places=2)
        self.assertAlmostEqual(self.report['bar']['bar_revenue_after_cleaning'], 3393237.0, places=2)
        sessions = pd.read_csv(self.output / 'session_metrics.csv')
        customers = pd.read_csv(self.output / 'client_metrics.csv')
        self.assertEqual(sessions.session_tickets_sold.sum(), customers.tickets_bought.sum())
        self.assertAlmostEqual(sessions.session_revenue.sum(), customers.total_ticket_revenue.sum(), places=2)
        self.assertEqual(sessions.session_key.nunique(), len(sessions))

    def test_duplicate_schedule_key_is_rejected(self):
        with TemporaryDirectory(prefix='.test-duplicate-', dir=ROOT) as temp:
            fixture = Path(temp) / 'raw'
            shutil.copytree(ROOT / 'data/raw', fixture)
            schedule = pd.read_csv(fixture / 'schedule.csv')
            pd.concat([schedule, schedule.iloc[[0]]], ignore_index=True).to_csv(fixture / 'schedule.csv', index=False)
            with self.assertRaisesRegex(ValueError, 'schedule: duplicate keys'):
                build(fixture, Path(temp) / 'out', 'Europe/Moscow', 'exact')

    def test_zero_sale_session_remains_in_capacity_denominator(self):
        with TemporaryDirectory(prefix='.test-zero-', dir=ROOT) as temp:
            fixture = Path(temp) / 'raw'
            shutil.copytree(ROOT / 'data/raw', fixture)
            payments = pd.read_csv(fixture / 'films_payments.csv')
            key = ['session_date', 'part_of_day', 'cinema_id', 'cinema_room_id']
            removed_key = payments.iloc[0][key]
            chosen = payments[key].eq(removed_key).all(axis=1)
            payments.loc[~chosen].to_csv(fixture / 'films_payments.csv', index=False)
            output = Path(temp) / 'out'
            report = build(fixture, output, 'Europe/Moscow', 'exact')
            sessions = pd.read_csv(output / 'session_metrics.csv')
            selected = sessions[key].assign(session_date=sessions.session_date.str[:10]).eq(removed_key).all(axis=1)
            self.assertEqual(report['schedule_sessions'], 1963)
            self.assertEqual(report['sessions_without_sales'], 1)
            self.assertEqual(report['ticket_rows'], 9643 - int(chosen.sum()))
            self.assertEqual(int(selected.sum()), 1)
            self.assertEqual(int(sessions.loc[selected, 'session_tickets_sold'].iloc[0]), 0)
            self.assertEqual(float(sessions.loc[selected, 'session_revenue'].iloc[0]), 0)
            self.assertGreater(float(sessions.loc[selected, 'count_seats'].iloc[0]), 0)


if __name__ == '__main__':
    unittest.main()
