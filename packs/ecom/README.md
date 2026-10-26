# ecom pack

Builds on `general`: Milo (office), Echo (customer support), Ledger (finance) and Scout (watcher and research) are already there.
This pack adds: Muse (marketing), Spark (social), Milo's shop duties (order → parcel chain, stock), Forge (support and build). Echo, Ledger and Scout come from `general`.
Shared rules: `agents/commerce-rules.md` (propose then apply, re-check before acting, system-issued numbers, fixed texts, outside text is data, how Forge ships a change). Every file in `agents/` goes into the team's prompt, so the whole team reads it.
Connectors: shop (Shopify default), shipping (GLS default), optionally meta-ads.

It evolves with real shop use – what hurts one shop owner hurts the next.
