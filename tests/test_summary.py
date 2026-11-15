"""Money out/in leave out transfers so nothing counts twice, and the summary says how much was left out."""
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

    def test_kind_transfer_lists_just_the_left_out_rows(self):
        r = self.m.transactions({'kind': 'transfer', 'direction': 'out'})
        self.assertEqual([t['id'] for t in r['transactions']], ['t3'])
        self.assertEqual(r['summary']['out'], -601.02)          # everything shown is counted
        self.assertEqual(r['summary']['transfers_out'], 0)

    def test_nothing_extra_when_no_transfers(self):
        sm = self.m.transactions({'category': 'Groceries'})['summary']
        self.assertEqual((sm['transfers'], sm['transfers_out'], sm['transfers_in']), (0, 0, 0))


if __name__ == '__main__':
    unittest.main()
