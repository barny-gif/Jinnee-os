# DESIGN.md: the shop's design rules (TEMPLATE, not filled in yet)

<!-- status: template -->
This is the template. `core/pack_loader.py --init` copies it to `brain/brand/DESIGN.md` (brain/ never goes to git), and
the team fills that copy in with the owner during onboarding (ecom onboarding, "Brand"): from the brandbook, the logo, the
live shop and the owner's answers. Until the ```design-lint``` block at the bottom has values, `tools/design_lint.py`
runs its general checks only and says that the brand checks were skipped.

Every UI the team makes (a landing page, a shop section, an email template, a dashboard page) follows this file. When a
general rule (Impeccable, the taste skill, a pattern from awesome-design-md) disagrees with what is written here, this
file wins, and the clash goes to the owner as an "Owner decides" point (section 8); the team never settles it alone.

## 1. The brand in two sentences
_Who buys here and what the shop should feel like to them. Example: "Wooden toys for parents who dislike plastic. Calm,
warm, honest: a workshop, not a supermarket."_

## 2. Colours
One row per colour, with its job. A colour without a job is not used.

| Token | Hex | Job (where it is used) |
|---|---|---|
| background | | page background |
| surface | | cards, panels |
| text | | body text |
| text-muted | | secondary text |
| primary | | the one main action (buy, add to cart) |
| accent | | at most one highlight per view |
| success / warning / danger | | states only, never decoration |

Rules: the exact brand colour is used as given (no "slightly deeper" stand-in); when it is hard to see on a background,
it gets a thin darker border, not a different colour. Text must keep a 4.5:1 contrast (3:1 for large text).

## 3. Typography
| Role | Font | Size / weight |
|---|---|---|
| display (hero) | | |
| headings | | |
| body | | |
| numbers (prices, stock) | | tabular figures |

The fonts must be licensed for the shop's use (a free font, or one the shop owns). Another company's own typeface is
never copied.

## 4. Shape and spacing
- Corner radius scale (e.g. 4px for inputs, 8px for cards, 999px for pills): __
- Spacing scale (e.g. 4 / 8 / 12 / 16 / 24 / 32 / 48): __
- Shadows: at most three levels, one per layer.

## 5. Components
_Buttons (primary / secondary), product card, price, badge (sale, new, out of stock), form field. One line each on how
they look; link to the live shop page that shows them._

## 6. Photos and imagery
_Real product photos first. Style (light, background, crop). What is never used (stock photos of smiling models, 3D
blobs, AI-generated people)._

## 7. Anti-AI-slop rules
The look that makes a page feel template-made. `tools/design_lint.py` reports these ids; [review] items need a person.

| Id | Avoid | Instead |
|---|---|---|
| AS-01 | Purple-to-blue gradient wallpaper, purple glow behind the hero, gradient-filled buttons. | A flat background; the product photo is the hero. |
| AS-02 | Gradient or shimmer text. | Solid text colour. |
| AS-03 | Glass panels (`backdrop-filter: blur`) everywhere. | A border and a soft shadow; blur at most on a sticky header. |
| AS-04 | One radius on everything, or radii off the scale. | The scale in section 4. |
| AS-05 | Emoji as icons (headings, buttons, nav). | A real icon set, or no icon. |
| AS-06 | Sparkles, magic wands, the "AI star". | Nothing: the shop does not need to say it uses AI. |
| AS-07 | The centred row of three identical feature cards; "1-2-3 how it works". [review] | Real product content; different sizes for different importance. |
| AS-08 | Generic copy: "seamless", "unlock", "elevate your", "all-in-one", "next level". | Concrete words about this product. |
| AS-09 | Made-up numbers, testimonials or logo walls. [review] | Real reviews and real numbers only, with their source. |
| AS-10 | One font, one weight; Inter for everything. | The roles in section 3. |
| AS-11 | An endless grid of identical cards. [review] | A lead item, then a compact list. |
| AS-12 | Shadow + glow + blur stacked on every element. | Three shadow levels. |
| AS-13 | Neon, gradient mesh, 3D blobs. [review] | The brand colours on calm surfaces. |
| AS-15 | Endless decorative animation (pulse, float, shimmer). | Motion only for a real state (loading, added to cart). |
| AS-16 | Em-dash (—) in customer-facing copy. | Comma, full stop, colon. |
| AS-17 | Colours outside the tokens in section 2. | Tokens only; a new colour is added here first. |

## 8. Owner decides
_Clashes between this file and a general rule, written down with the date and the owner's decision, so they are not
asked again. Example: "2026-01-10: the sale badge stays red #D92D20 although it is outside the palette (owner)."_

## 9. Machine-readable tokens
Kept in sync with sections 2 to 4. `colors` and `fonts` take the current values; `*_legacy` the old ones that may still
be on the live shop (reported as LOW, not as off-token). Radius values with their unit, separated by spaces.

```design-lint
name: shop
colors:
colors_legacy:
fonts:
fonts_legacy:
radius:
radius_legacy:
```
