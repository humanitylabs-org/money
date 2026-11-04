---
name: money
description: Use when the owner asks about his spending, transactions to review, categories or money rules.
version: 0.1.0
author: Hermes Agent
license: GPL-3.0-only
platforms: [linux, macos]
metadata:
  hermes:
    tags: [finance, budgeting, transactions, spending]
    related_skills: [wizard-simplefin, card-benefits]
---
# Money

The owner's money app at /money/. It reads one bank-connection app (SimpleFIN by default; Monarch or Teller if chosen under Accounts → Bank connection) and layers his own reviewed state, categories, merchant names, notes, hidden flags and rules on top.

Read: `money_overview` (current connection, `attention` = banks that stopped updating or need reconnecting and where to fix them (mention these first, and treat those banks' numbers as out of date), accounts, how many need review, categories, rules), `money_transactions` (his categories and names applied; filters from/to, account, search, category, reviewed, amounts, direction out/in; min_abs; sort newest, oldest, largest = biggest in or out first, smallest, merchant/account/category A–Z or *_desc; each answer has a summary of money out/in and uncategorized count), `money_spending` (one month by category; refunds net; categories whose kind is transfer, such as card payments, Biz to Personal and Loan Repay, are excluded and income kinds count as income). Kinds (expense/income/transfer) come from Monarch's category groups and are listed in `money_overview.category_kinds`. Transaction summaries' money out/in also leave out transfers unless filtered to one category.

Every transaction comes back with `category_from`: `you` (his pick), `rule`, `bank`, `payment` (card payment/autopay, excluded from spending) `memory` (what he called this merchant before) or `common` (a well-known merchant or plain word such as coffee, hotel, pharmacy, mapped onto his own categories; lowest priority). Treat `memory` as a good guess he may want to confirm.

`money_overview.free_check` pairs Free-category charges (card-benefit buys) with their statement credits; `waiting_for_credit` is still real spending until the credit posts. Imported history keeps the category he gave it in the old app over rules.

Money sends its own notifications (no agent needed) through the Notifications app: a bank that needs him (signed out, stopped updating, Money can't refresh) goes out as needs-you, and each new charge at or above his large size (default $500; transfers and card payments left out) once. Don't send these yourself. `money_alerts` reads (`get`) or changes (`set`: `on`, `large`) that, or sends one `test` notification; change it only when he asks.

Change only when he asks: `money_update` with `source` and `id` (or `ids`). Setting `make_rule` with `match` text also categorizes future matching transactions. `money_rules` adds rules with optional size (min_abs/max_abs) and day-of-month ('24-5') conditions or deletes rules; the most specific matching rule wins. `money_categories` adds, sets kind (expense/income/transfer), merges or deletes (unused only) categories. A transaction can be split across categories: `money_update` with `splits` [{category, amount}] adding up to the charge (rows then carry `splits`, and spending counts each part); a rule with split_category + split_amount sends that fixed amount to one category and the rest to its own (e.g. Apple: 14.99 to Subscription (Paul), rest Subscriptions). Categories sit under headings (`money_overview.category_groups`: group, kind, categories; Transfers = not spending); `money_spending` gives each category's group plus group totals; set a heading with `money_categories` action group. Pending charges show status pending and can be reviewed; if a reviewed pending charge settles at a different amount Money puts it back to review with a note, and holds that drop off without posting are listed for 30 days in `money_overview.released_holds` (mention them in review). All of this lives in Money only; Monarch is never changed. The app has an Undo for the last change; edits never alter the bank's data.

Negative amounts are money out. For card-benefit credits, search the transaction and report it as "looks posted"; he confirms in Card Benefits.
