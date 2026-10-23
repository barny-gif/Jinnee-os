# Forge playbook – what we learned wiring this shop (read before building a connector)

Facts below were confirmed against the real systems on 2026-10-11. New lessons go into `brain/forge_notes.md` (one dated line each: what happened, what fixed it); that file is read on every run.

## How a connector gets built here
1. Read the provider's API docs first (WebFetch). Never invent endpoint or field names; when docs are unreachable, say so and use a dry run.
2. Write `connectors/<id>/ops.py` in the pattern of `connectors/shopify/ops.py`; test with fake HTTP in `tests/`.
3. First real call is read-only or a dry run. Anything that writes outside the owner's systems goes through `core/approvals.py`.
4. Customer personal data (name, address, e-mail, phone) stays inside the connector code. Output, logs, handoffs and Telegram show the order number only.
5. State that must survive a rebuild lives in `brain/` (mounted), never in the image.
6. Ship as a pull request. The owner merges, then on the VPS: `cd /opt/jinnee-os && git pull --ff-only && docker compose build && docker compose up -d` (`up --build` is not accepted on this VPS).

## Shopify (the store in SHOPIFY_STORE, .env)
- Login is a Dev Dashboard app (Jinnee-ecom): SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET are exchanged for a 24 h token. `app_not_installed` = the app is released but not installed on the store. The Installs counter in the dashboard lags behind the real state.
- Required scopes (2026-10-15): read_orders, read_products, read_inventory, read_all_orders, read_reports, read_merchant_managed_fulfillment_orders, write_merchant_managed_fulfillment_orders. write_products was dropped on purpose and is not needed. Check with `python3 connectors/shopify/ops.py scopes` (never trust a scope by word of mouth); a new scope only shows after a Released app version that the store accepted. Name/address fields can come back empty without protected customer data access.
- Sessions / visitors: `python3 connectors/shopify/ops.py sessions --days 7` (shopifyqlQuery, read_reports). Fulfillment: `fulfill` (REST fulfillment_orders + fulfillments.json, notify_customer false); the site sender can do it automatically once GLS picked the parcel up, once per order (`brain/shopify_fulfilled.json`). OFF by default: only SHOPIFY_AUTO_FULFILL=yes turns it on, and then only parcels picked up after the switch-on moment (`brain/shopify_fulfill_start.json`) are fulfilled; SHOPIFY_FULFILL_SINCE=<date> back-fills older ones on purpose.
- GLS status codes (MyGLS GetParcelStatuses, HU texts, seen live): 51 Csomag rögzítve, 52 Utánvét adat rögzítve, 86 Lehívás - felvett csomag (GLS picked it up from us), 53 Depó tranzit, 01 GLS központban regisztrálva, 03 Csomag a kiszállító depóban, 10 Rollkarte check, 09 Meghat. időpontra történő kisz, 04 Csomag kiszállítás alatt, 05 Kiszállítva (delivered), 07 Depó tárolás (held at the depot, not delivered). Only 05 counts as delivered; a COD invoice is never due before it.

## Billingo
- API v3, header X-API-KEY. GET calls work. `POST /documents` (draft and proforma) answered 402 "You do not have subscription for this operation" although the plan is Pro: an account-level switch, to be asked from Billingo support. Do not work around it.
- Payment method names: `wire_transfer`, `bankcard`, `cash`, `cash_on_delivery` are valid; `transfer` is not (422). Partner creation works.

## MyGLS
- PrepareLabels needs `WebshopEngine` (error 56 otherwise). Password goes as the SHA-512 byte list. Street and house number are separate fields.
- Uploads are recorded in `brain/gls_uploaded.json`: one order, one parcel. Dry run is the default; `--send yes` uploads.

## Habits that paid off
- Make errors name the failing step (`[create draft] ...`), so one run is enough to diagnose.
- A value that "is in .env" is only seen by the container after `docker compose up -d` recreates it.

## Shopify through the claude.ai connector (read only, no personal data)
Besides `connectors/shopify`, the team can read the shop through the owner's Shopify connector (tools `mcp__claude_ai_Shopify__*`): shop info, products, collections, stock levels and ShopifyQL sales numbers (`run-analytics-query`). Use it for catalogue, stock and revenue questions.
- Orders and customers are not allowed through it: use `connectors/shopify` (order numbers only). In a ShopifyQL query never group or select by customer, name, e-mail or address.
- If the tool is missing on a run, say so; do not guess data.
