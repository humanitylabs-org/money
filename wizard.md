---
wizard: 0.2
id: money
name: Money
type: view
category: app
version: 0.16.3
summary: Review transactions, set categories and rules, and see spending and net worth over your bank connection.
tags: [finance]
license: GPL-3.0-only
source: https://github.com/oscarhenrycollins/money
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
api:
  port: 8809
  path: /api
  spec: openapi.json
mcp:
  path: /mcp
  port: 8809
  transport: streamable-http
skill: skill/SKILL.md
view:
  path: /
  port: 8809
  source:
    path: web
    build: python3 app/build_view.py
    dist: web
provides: [money]
system: false
uses: [wizard-simplefin, wizard-monarch, notifications]
needs: []
health: /healthz
---

# Money

A simple money app over your bank connection. Review new transactions with one tap, give them a category and a clearer name, add a note, and tick "always use this" to make a rule for next time. See spending by category for any month, and every account's balance.

Add manual accounts for things no bank sees, like a cold-storage Bitcoin wallet (enter the BTC amount; it's valued at the live price). Bring over past history from another money app once, and every row says where it came from.

Money reads from a bank-connection app on your box: SimpleFIN by default, or Monarch or Teller if you have those. It never changes the bank's data. Your reviews, categories, names, notes and rules live in Money's own database, so a fresh sync never overwrites them, and switching connections keeps each connection's edits.

Reply: open a transaction (or an alert at the top) and tap Reply to start a new chat with your agent about it. It opens an unsent Chats draft with the item attached; Money never sends anything.
