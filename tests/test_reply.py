"""Reply: one transaction as {id,title,message} for an unsent Chats draft. The app never sends anything."""
import datetime as dt
import re
import tempfile
import unittest

from test_alerts import load


class ReplyContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)
        c = self.m.db()
        today = dt.date.today()
        with c:
            self.m._set(c, 'source', 'wizard-simplefin')
            c.execute("INSERT INTO accounts VALUES ('wizard-simplefin','a1','Chase','Sapphire','1234','credit','USD',-100,?)", (today.isoformat(),))
            for tid, amt, days in (('TRN-1', -42.5, 0), ('TRN-0', -40, 9), ('weird id/with:chars', -5, 1)):
                c.execute('INSERT INTO tx(source,id,account,date,amount,description,merchant,source_category,status) VALUES (?,?,?,?,?,?,?,?,?)',
                          ('wizard-simplefin', tid, 'a1', (today - dt.timedelta(days=days)).isoformat(), amt,
                           'SQ *BLUE BOTTLE 123', 'Blue Bottle', 'Coffee', 'pending' if tid == 'TRN-1' else 'posted'))
        c.close()
        self.m.update({'source': 'wizard-simplefin', 'id': 'TRN-1', 'note': 'meeting with Paul'})

    def tearDown(self):
        self.tmp.cleanup()

    def test_payload_carries_the_facts_and_fits_the_draft_limits(self):
        r = self.m.reply_context({'source': 'wizard-simplefin', 'id': 'TRN-1'})
        self.assertEqual(r['id'], 'tx-wizard-simplefin-TRN-1')
        self.assertEqual(r['title'], 'Money: Blue Bottle')
        msg = r['message']
        for want in ('Blue Bottle', '\u2212$42.50', 'Chase Sapphire …1234', 'pending', 'Coffee', 'meeting with Paul',
                     'waiting for review', 'SQ *BLUE BOTTLE', 'money_update', 'TRN-1'):
            self.assertIn(want, msg)
        self.assertIn('\u2212$40.00', msg)   # an earlier charge at the same merchant
        self.assertLessEqual(len(msg), 32000)
        self.assertLessEqual(len(r['title']), 1000)

    def test_odd_bank_ids_get_a_safe_stable_draft_id(self):
        a = self.m.reply_context({'source': 'wizard-simplefin', 'id': 'weird id/with:chars'})
        b = self.m.reply_context({'source': 'wizard-simplefin', 'id': 'weird id/with:chars'})
        self.assertRegex(a['id'], r'^[A-Za-z0-9_-]{1,128}$')
        self.assertEqual(a['id'], b['id'])

    def test_unknown_or_missing_transaction_is_an_error(self):
        with self.assertRaises(self.m.Problem):
            self.m.reply_context({'source': 'wizard-simplefin', 'id': 'nope'})
        with self.assertRaises(self.m.Problem):
            self.m.reply_context({})


if __name__ == '__main__':
    unittest.main()
