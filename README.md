# Finances for Wizard

A simple money app for your Wizard. Review new transactions with one tap, set categories and clearer names, add notes, make rules for next time, and see spending by category each month.

Finances reads from a bank-connection app on your box: [SimpleFIN](https://github.com/oscarhenrycollins/wizard-simplefin) by default, or Monarch or Teller if you have them. It never changes the bank's data. Your reviews, categories, names, notes and rules stay in Finances' own database on your box.

Actual Budget (switch `finances:actual-budget`, off by default, money#38): Finances ships a stock [Actual Budget](https://actualbudget.org) server (official image, pinned, no fork, no port: only Finances reaches it on the app's own network). With the switch on, Finances copies its accounts, transactions, categories and rules into an Actual budget named "Finances" every minute and right after each change, and takes changes made in Actual back (a transaction's category, payee or note, renamed payees and categories, new categories; a transaction deleted in Actual is hidden in Finances). Pasting a SimpleFIN setup token (or an access URL) under Accounts hands SimpleFIN to Actual, and bank data then comes through Actual's SimpleFIN sync with the same transaction ids, so reviews, categories and notes stay put. Finances' own database stays a full copy, so switching off goes straight back to it and the SimpleFIN app. Not carried into Actual: reviewed state and hidden rows (Actual has neither), day-of-month and split parts of rules (Finances keeps applying them). The client is `app/actual.py` (Actual's sync protocol, stdlib only).

Install: `wam install finances` (and `wam install wizard-simplefin` to connect your banks)

Wizard inbox: `GET /finances/api/inbox` lists each transaction waiting for review (the same number as the app's badge), so Wizard can show them as a badge or as items in Chats. Opening an item jumps to that transaction; reviewing it removes it.

Reply: in a transaction's sheet (and on each alert) **Reply** opens a fresh, unsent Chats draft with the item attached (`WizardApp.nav.openContextDraft`; shown only on shells that have it). `GET /money/api/transactions/context?source=&id=` returns that draft's `{id,title,message}`. Finances never sends anything.
