"""Money out/in leave out transfers so nothing counts twice; transfers are their own total, so the tiles add up to the rows."""
import datetime as dt
import tempfile
import unittest

from test_alerts import load


class SummaryTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)
        c = self.m.db()
        today = dt.date.today().isoformat()
        with c:
            self.m._set(c, 'source', 'wizard-simplefin')
            c.execute("INSERT INTO accounts VALUES ('wizard-simplefin','a1','Chase','Sapphire','1234','credit','USD',-100,?)", (today,))
            for tid, amt, merchant, cat in (('t1', -500.00, 'Rent Co', 'Rent'), ('t2', -86.63, 'Grocer', 'Groceries'),
                                            ('t3', -601.02, 'Wise', 'Transfer'), ('t4', 250.00, 'Card autopay', 'Card Payment'),
                                            ('t5', 1200.00, 'Employer', 'Paychecks')):
                c.execute('INSERT INTO tx(source,id,account,date,amount,description,merchant,source_category,status) VALUES (?,?,?,?,?,?,?,?,?)',
                          ('wizard-simplefin', tid, 'a1', today, amt, merchant, merchant, cat, 'posted'))
        c.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_tiles_leave_out_transfers_and_say_how_much(self):
        sm = self.m.transactions({})['summary']
        self.assertEqual(sm['out'], -586.63)
        self.assertEqual(sm['transfers'], 2)
        self.assertEqual(sm['transfers_out'], -601.02)
        self.assertEqual(sm['transfers_in'], 250.0)
        self.assertEqual(round(sm['out'] + sm['transfers_out'], 2), -1187.65)   # adds up to the rows

    def test_kind_transfer_lists_just_the_transfer_rows(self):
        r = self.m.transactions({'kind': 'transfer', 'direction': 'out'})
        self.assertEqual([t['id'] for t in r['transactions']], ['t3'])
        sm = r['summary']
        self.assertEqual((sm['out'], sm['in']), (0, 0))            # money out/in keep excluding transfers
        self.assertEqual((sm['transfers'], sm['transfers_out']), (1, -601.02))   # the Transfers tile stays

    def test_tiles_reconcile_with_rows_and_rows_say_transfer(self):
        r = self.m.transactions({})
        sm = r['summary']
        total = round(sum(t['amount'] for t in r['transactions']), 2)
        self.assertEqual(round(sm['out'] + sm['in'] + sm['transfers_out'] + sm['transfers_in'], 2), total)
        self.assertEqual(sorted(t['id'] for t in r['transactions'] if t['transfer']), ['t3', 't4'])

    def test_nothing_extra_when_no_transfers(self):
        sm = self.m.transactions({'category': 'Groceries'})['summary']
        self.assertEqual((sm['transfers'], sm['transfers_out'], sm['transfers_in']), (0, 0, 0))


if __name__ == '__main__':
    unittest.main()
