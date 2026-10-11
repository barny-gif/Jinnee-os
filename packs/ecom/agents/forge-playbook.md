# Forge playbook – what we learned wiring this shop (read before building a connector)

Facts below were confirmed against the real systems on 2026-10-11. New lessons go into `brain/forge_notes.md` (one dated line each: what happened, what fixed it); that file is read on every run.

## How a connector gets built here
1. Read the provider's API docs first (WebFetch). Never invent endpoint or field names; when docs are unreachable, say so and use a dry run.
2. Write `connectors/<id>/ops.py` in the pattern of `connectors/shopify/ops.py`; test with fake HTTP in `tests/`.
3. First real call is read-only or a dry run. Anything that writes outside the owner's systems goes through `core/approvals.py`.
4. Customer personal data (name, address, e-mail, phone) stays inside the connector code. Output, logs, handoffs and Telegram show the order number only.
5. State that must survive a rebuild lives in `brain/` (mounted), never in the image.
6. Ship as a pull request. The owner merges, then on the VPS: `cd /opt/jinnee-os && git pull --ff-only && docker compose build && docker compose up -d` (`up --build` is not accepted on this VPS).

## Shopify (store ykdtdb-gu.myshopify.com)
- Login is a Dev Dashboard app (Jinnee-ecom): SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET are exchanged for a 24 h token. `app_not_installed` = the app is released but not installed on the store. The Installs counter in the dashboard lags behind the real state.
- Scopes in use: read_orders, read_products, read_inventory. Name/address fields can come back empty without protected customer data access.

## Billingo
- API v3, header X-API-KEY. GET calls work. `POST /documents` (draft and proforma) answered 402 "You do not have subscription for this operation" although the plan is Pro: an account-level switch, to be asked from Billingo support. Do not work around it.
- Payment method names: `wire_transfer`, `bankcard`, `cash`, `cash_on_delivery` are valid; `transfer` is not (422). Partner creation works.

## MyGLS
- PrepareLabels needs `WebshopEngine` (error 56 otherwise). Password goes as the SHA-512 byte list. Street and house number are separate fields.
- Uploads are recorded in `brain/gls_uploaded.json`: one order, one parcel. Dry run is the default; `--send yes` uploads.

## Habits that paid off
- Make errors name the failing step (`[create draft] ...`), so one run is enough to diagnose.
- A value that "is in .env" is only seen by the container after `docker compose up -d` recreates it.
