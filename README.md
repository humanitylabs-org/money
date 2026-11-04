# Money for Wizard

A simple money app for your Wizard. Review new transactions with one tap, set categories and clearer names, add notes, make rules for next time, and see spending by category each month.

Money reads from a bank-connection app on your box: [SimpleFIN](https://github.com/oscarhenrycollins/wizard-simplefin) by default, or Monarch or Teller if you have them. It never changes the bank's data. Your reviews, categories, names, notes and rules stay in Money's own database on your box.

Install: `wam install money` (and `wam install wizard-simplefin` to connect your banks)

Wizard inbox: `GET /money/api/inbox` lists each transaction waiting for review (the same number as the app's badge), so Wizard can show them as a badge or as items in Chats. Opening an item jumps to that transaction; reviewing it removes it.

Reply: in a transaction's sheet (and on each alert) **Reply** opens a fresh, unsent Chats draft with the item attached (`WizardApp.nav.openContextDraft`; shown only on shells that have it). `GET /money/api/transactions/context?source=&id=` returns that draft's `{id,title,message}`. Money never sends anything.
