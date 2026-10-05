# Connectors – shared rules

Every connector is a folder `connectors/<id>/` containing
- `manifest.json` – id, name, category (shop/invoicing/email/shipping/ads), version, auth method, operations
- `auth.md` – what the owner has to set up, step by step, screenshot-friendly
- `ops/` – the operations (Claude Code skills or MCP tools; pack agents call only these)

Within a category every connector exposes the **same operation set** (e.g. `invoicing.create_draft`, `invoicing.list`, `invoicing.get_pdf`). That's why Billingo can be swapped for Számlázz.hu in a few clicks: the agents don't know which one is underneath.

Base operation set per category – v0.1:
- shop: list_orders, get_order, list_products, get_product, update_product_draft, inventory_levels
- invoicing: create_draft, list, get_pdf, status
- email: search, read, draft_reply, send (per autonomy level)
- shipping: create_label_draft, track
- ads: read_spend, read_performance (read-only in v0.1)
