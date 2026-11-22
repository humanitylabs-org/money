"""Finances without a bank (#29, on for everyone): add by hand, import a CSV / OFX file, same review and spending."""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from test_alerts import load

CHASE_CARD = """Transaction Date,Post Date,Description,Category,Type,Amount,Memo
{m}/03/{y},{m}/04/{y},WHOLE FOODS #123,Groceries,Sale,-86.63,
{m}/05/{y},{m}/06/{y},STARBUCKS 555,Food & Drink,Sale,-5.25,
{m}/05/{y},{m}/06/{y},STARBUCKS 555,Food & Drink,Sale,-5.25,
{m}/09/{y},{m}/10/{y},AUTOMATIC PAYMENT - THANK,,Payment,500.00,
"""

AMEX = """Date,Description,Amount
{m}/02/{y},UBER TRIP,23.10
{m}/07/{y},NETFLIX.COM,15.49
{m}/08/{y},DELTA AIR LINES,412.00
{m}/12/{y},PAYMENT RECEIVED - THANK YOU,-450.59
"""

CAPONE = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
{y}-{m}-04,{y}-{m}-05,1234,SHELL OIL 5544,Gas/Automotive,45.10,
{y}-{m}-11,{y}-{m}-12,1234,CAPITAL ONE AUTOPAY PYMT,Payment/Credit,,200.00
"""

EURO = """Kontoauszug;Girokonto
Zeitraum;letzter Monat

Buchungstag;Empfaenger;Verwendungszweck;Betrag
03.{m}.{y};REWE Markt;Einkauf;-1.234,56
04.{m}.{y};Arbeitgeber GmbH;Gehalt;2.500,00
"""

OFX = """OFXHEADER:100
DATA:OFXSGML
<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>Acme Bank</FI></SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKACCTFROM><ACCTID>000123456789<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>{y}{m}03120000[-5:EST]<TRNAMT>-42.17<FITID>A1<NAME>TRADER JOE S #55<MEMO>POS PURCHASE
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>{y}{m}15<TRNAMT>1800.00<FITID>A2<NAME>PAYROLL ACME
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>{y}{m}16<TRNAMT>-42.17<FITID>A3<NAME>TRADER JOE S #55
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)
        d = dt.date.today()
        self.month, self.y, self.mm = d.strftime('%Y-%m'), d.strftime('%Y'), d.strftime('%m')

    def tearDown(self):
        self.tmp.cleanup()

    def f(self, tpl):
        return tpl.format(m=self.mm, y=self.y)

    def test_on_for_everyone_with_no_switch(self):
        self.assertNotIn('offline-import', [s['id'] for s in self.m.switches_doc()['switches']])
        self.assertTrue(self.m.local_add({'amount': -5, 'payee': 'Cafe'})['ok'])
        self.assertIn('local_accounts', self.m.overview())
        tools = self.m.rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']
        self.assertIn('money_add_transaction', [t['name'] for t in tools])

    def test_stored_switch_value_is_cleared(self):
        f = Path(self.tmp.name) / 'switches.json'
        f.write_text(json.dumps({'offline-import': False, 'other': True}))
        self.m._sw_forget_retired()
        self.assertEqual(json.loads(f.read_text()), {'other': True})
        self.assertEqual(self.m.transactions({})['total'], 0)
        self.m.local_add({'amount': -7, 'payee': 'Cafe', 'category': 'Coffee'})
        self.assertEqual(self.m.transactions({})['total'], 1)

    def test_add_by_hand_shows_in_review_and_spending(self):
        t = self.m.local_add({'date': self.month + '-02', 'amount': -12.5, 'payee': 'Corner cafe', 'category': 'Coffee', 'note': 'with Ana'})['transaction']
        self.assertEqual((t['origin'], t['added'], t['origin_name'], t['category'], t['category_from'], t['reviewed']),
                         ('local', 'hand', 'Added by hand', 'Coffee', 'you', True))
        self.assertEqual(t['account_name'], 'Cash')
        self.m.local_add({'amount': -3, 'payee': 'Kiosk'})   # no category: waits in review
        self.assertEqual(self.m.overview()['to_review'], 1)
        sp = self.m.spending(self.month)
        self.assertEqual({c['category']: c['amount'] for c in sp['categories']}.get('Coffee'), 12.5)
        self.assertEqual(self.m.overview()['local_accounts'][0]['name'], 'Cash')
        self.m.local_delete({'id': t['id']})
        self.assertEqual(self.m.transactions({'origin': 'local'})['total'], 1)

    def test_csv_import_guesses_columns_remembers_them_and_skips_duplicates(self):
        p = self.m.local_preview({'filename': 'Chase1234_Activity.CSV', 'text': self.f(CHASE_CARD)})
        self.assertEqual((p['kind'], p['known'], p['total'], p['new'], p['duplicates']), ('csv', False, 4, 4, 0))
        self.assertEqual(p['mapping']['date'], 0)
        self.assertEqual(p['mapping']['amount'], 5)
        self.assertEqual(p['mapping']['description'], 2)
        self.assertEqual(p['mapping']['date_format'], '%m/%d/%Y')
        self.assertFalse(p['mapping']['negate'])
        r = self.m.local_import({'filename': 'Chase1234_Activity.CSV', 'text': self.f(CHASE_CARD), 'account': 'Chase Freedom'})
        self.assertEqual((r['imported'], r['duplicates']), (4, 0))   # the two same-day coffees are both real
        again = self.m.local_preview({'filename': 'Chase1234_Activity (1).CSV', 'text': self.f(CHASE_CARD)})
        self.assertEqual((again['known'], again['account'], again['new'], again['duplicates']), (True, 'Chase Freedom', 0, 4))
        r2 = self.m.local_import({'filename': 'x.csv', 'text': self.f(CHASE_CARD)})
        self.assertEqual((r2['imported'], r2['duplicates']), (0, 4))
        rows = self.m.transactions({'origin': 'local'})['transactions']
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(not t['reviewed'] and t['origin_name'] == 'Chase1234_Activity.CSV' for t in rows))
        groceries = next(t for t in rows if t['amount'] == -86.63)
        self.assertEqual((groceries['category'], groceries['category_from']), ('Groceries', 'bank'))
        # same review, rules and spending as a connected bank
        self.m.update({'source': 'local', 'id': groceries['id'], 'category': 'Shopping', 'reviewed': True})
        self.m.rules_change({'match': 'starbucks', 'category': 'Coffee'})
        sp = {c['category']: c['amount'] for c in self.m.spending(self.month)['categories']}
        self.assertEqual(sp.get('Shopping'), 86.63)
        self.assertEqual(sp.get('Coffee'), 10.5)

    def test_card_file_with_positive_charges_is_flipped(self):
        p = self.m.local_preview({'filename': 'activity.csv', 'text': self.f(AMEX)})
        self.assertTrue(p['mapping']['negate'])
        self.assertEqual(p['money_out'], -450.59)
        self.assertEqual(p['money_in'], 450.59)

    def test_debit_and_credit_columns(self):
        p = self.m.local_preview({'filename': 'capone.csv', 'text': self.f(CAPONE)})
        self.assertEqual((p['mapping']['debit'], p['mapping']['credit'], p['mapping']['amount']), (5, 6, None))
        self.assertEqual([r['amount'] for r in p['preview']], [-45.1, 200.0])

    def test_preamble_semicolons_and_decimal_commas(self):
        p = self.m.local_preview({'filename': 'konto.csv', 'text': self.f(EURO)})
        self.assertEqual(p['headers'], ['Buchungstag', 'Empfaenger', 'Verwendungszweck', 'Betrag'])
        self.assertEqual(p['mapping']['date_format'], '%d.%m.%Y')
        self.assertEqual([r['amount'] for r in p['preview']], [-1234.56, 2500.0])

    def test_changed_mapping_is_used_and_remembered(self):
        text = self.f(CHASE_CARD)
        p = self.m.local_preview({'filename': 'c.csv', 'text': text})
        mp = dict(p['mapping'], description=6, negate=True)
        self.m.local_import({'filename': 'c.csv', 'text': text, 'account': 'Card', 'mapping': mp})
        again = self.m.local_preview({'filename': 'c.csv', 'text': text})
        self.assertEqual((again['mapping']['description'], again['mapping']['negate']), (6, True))

    def test_ofx_import_uses_fitid(self):
        text = self.f(OFX)
        p = self.m.local_preview({'filename': 'acme.qfx', 'text': text})
        self.assertEqual((p['kind'], p['total'], p['last_four']), ('ofx', 3, '6789'))
        self.assertIn('6789', p['account'])
        r = self.m.local_import({'filename': 'acme.qfx', 'text': text})
        self.assertEqual(r['imported'], 3)   # same payee and amount on two days: two FITIDs, both kept
        self.assertEqual(self.m.local_import({'filename': 'acme.qfx', 'text': text})['imported'], 0)
        rows = self.m.transactions({'origin': 'local', 'sort': 'oldest'})['transactions']
        self.assertEqual(rows[0]['description'], 'TRADER JOE S #55 POS PURCHASE')
        self.assertEqual(rows[0]['last_four'], '6789')

    def test_amount_parsing(self):
        a = self.m._amount
        for raw, want in (('-12.50', -12.5), ('(12.00)', -12.0), ('$1,234.56', 1234.56), ('1.234,56', 1234.56), ('12,5', 12.5),
                          ('12.00 CR', 12.0), ('12.00 DR', -12.0), ('-$9.99', -9.99), ('USD 4', 4.0), ('', None), ('abc', None), ('12-', -12.0)):
            self.assertEqual(a(raw), want, raw)

    def test_no_bank_at_all_is_not_an_error_when_used_without_a_bank(self):
        c = self.m.db()
        with c:
            self.m._set(c, 'last_error', 'Could not reach that bank connection app.')
        c.close()
        self.m._SRC.update(at=self.m.time.time(), list=[{'id': 'wizard-simplefin', 'name': 'SimpleFIN', 'installed': False, 'connected': False, 'banks': 0, 'last_sync': None}])
        self.assertEqual(self.m.overview()['attention'], [])

    def test_agent_tool_adds_and_lists(self):
        tools = self.m.rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']
        tool = next(t for t in tools if t['name'] == 'money_add_transaction')
        self.assertFalse(tool['annotations']['readOnlyHint'])
        r = self.m.rpc({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'money_add_transaction', 'arguments': {'amount': -20, 'payee': 'Market'}}})
        self.assertFalse(r['result']['isError'])
        r = self.m.rpc({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'money_transactions', 'arguments': {'origin': 'local'}}})
        self.assertEqual(r['result']['structuredContent']['total'], 1)


if __name__ == '__main__':
    unittest.main()
