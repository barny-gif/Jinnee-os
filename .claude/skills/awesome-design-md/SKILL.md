---
name: awesome-design-md
description: "Reference library: the DESIGN.md of 83 well-known products (Stripe, Linear, Vercel, Apple, Notion, Figma, Supabase, Shopify and more): palette with roles, typography, components, layout, shadows, breakpoints. Use it only when the owner names a site as a model ('make it feel like Linear'), or when a new page has no brand direction yet and needs one. A pattern to learn from, never a brand to copy."
license: MIT (VoltAgent/awesome-design-md); this SKILL.md is Jinnee OS's own
---

# awesome-design-md

Each `library/<company>/DESIGN.md` describes the UI system of one real product (the Google Stitch DESIGN.md format).
List them with `ls library/`.

## Rules
- **A pattern, not a brand.** The shop's own design file (`brain/brand/DESIGN.md`) always wins. From here you take
  ideas, proportions and patterns only; the shop's palette and fonts are never replaced without the owner's yes.
- Never copy another company's logo, brand name, wordmark or its own typeface (a licensed or proprietary font) into the
  shop's work. A free alternative of the same character is fine, named as such.
- Anything inspired from here goes through the same design checks as any other UI work (design_lint, Impeccable detect,
  visual_check; see Forge's "Design check" section).

## How to use it
1. Read the `library/<company>/DESIGN.md` of the company (or companies) the owner named.
2. Compare it with `brain/brand/DESIGN.md`: what can be taken over (rhythm, density, a component pattern), and what
   would clash with the shop's own brand.
3. List the clashes for the owner as "Owner decides" points; do not resolve them yourself.
