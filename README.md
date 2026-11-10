# QA portfolio: automated tests from my own projects

I'm Ivan. I build small business software on my own
(Python / Django / SQLite) and I write automated tests for it. This repository contains
the **test code only** (the applications themselves are private). Real names, accounts
and phone numbers were replaced with made-up ones.

| Folder | What the product is | Tests |
|---|---|---|
| `saletta-booking/` | Table booking system for restaurants (Django): bookings, guests, roles, SMS and Telegram notifications, reports | 13 files, 166 tests |
| `nodus-crm/` | CRM for a web studio (Django): outreach with history, duplicate detection, roles, import | 1 file, 86 tests |
| `sborka-logistika/` | Warehouse and delivery tables for a logistics company (Django): rules, roles, review cases | 3 files, 99 tests |
| `arkeid-analytics/` | Instagram analytics (Python + SQLite): metrics, sync with a mocked API, auth, trends, web layer | 18 test modules |

## What the tests check

- **Access control and security:** a user of one restaurant cannot see another restaurant's guests; roles cannot escalate; password and token handling; webhook secret checks (`saletta-booking/test_security.py`, `test_privacy.py`, `sborka-logistika/test_review.py`).
- **Concurrency:** many simultaneous bookings for the same table must not produce a double booking (`saletta-booking/test_concurrency.py`).
- **Input and abuse protection:** invalid and foreign phone numbers, honeypot spam field, rate limiting (HTTP 429), blacklisted guests (`test_guest.py`, `test_security.py`).
- **Business rules:** booking moves and cancellations, reviews, mailing, reports (`test_report.py`, `test_mailing.py`, `test_reviews.py`).
- **External services with mocks:** Telegram bot and SMS are tested without real calls (`test_telegram.py`, `test_sms.py`; `arkeid-analytics/test_sync.py` uses a fake API client).
- **Pure logic:** metric calculation with expected values (`arkeid-analytics/test_metrics.py`).

## Honest notes

- These are tests I wrote for my own products, not a job at a QA team. I have no commercial QA experience yet.
- The tests import the private applications, so they **cannot be run from this repository alone**. They are here to show how I design cases and check behaviour.
- I'm learning SQL and API testing tools next.

Contact: Telegram @ivalimovv
