# Third-party components

Jinnee OS itself is MIT (see `LICENSE`). The skills below come from other projects, under their own licences. Each
folder keeps its upstream licence file (and, for Apache-2.0, its NOTICE) next to the files. They are pinned to one
upstream commit; an update is a deliberate change in a pull request, never an automatic pull.

| Skill (`.claude/skills/…`) | Upstream | Commit | Licence | In the repo | Changes made here |
|---|---|---|---|---|---|
| `impeccable` (skill 4.9.0, engine 0.1.23) | [pbakaus/impeccable](https://github.com/pbakaus/impeccable), path `.claude/skills/impeccable` | `c4cd1924c389f68d37c687d9c2bbf13f12547c41` | Apache-2.0 (`LICENSE`, `NOTICE.md`) | the skill files, unchanged | none. The engine binary (`scripts/bin/`) is **not** in the repo: the launcher downloads it on first use and checks it against `scripts/engine-checksums.txt` (sha256). Telemetry is switched off by the bridge (`DO_NOT_TRACK=1`, `IMPECCABLE_NO_TELEMETRY=1`). |
| `design-taste-frontend` (tasteskill 2.5.1) | [Leonxlnx/taste-skill](https://github.com/Leonxlnx/taste-skill), path `skills/taste-skill` | `f6b76575f949707b4e3c37cd86cb44b0b98643e8` | MIT (`LICENSE`); its photos and icons carry their own credits (`assets/**/credits.json`) | **nothing**: `tools/skills_fetch.py` fetches the pinned commit at install time (git checks the commit hash) | `disable-model-invocation: true` added to `SKILL.md`, so it runs only when the owner names it. It bans purple and deals a random palette, which would fight a shop's own brand if it started on every UI task. |
| `awesome-design-md` | [VoltAgent/awesome-design-md](https://github.com/VoltAgent/awesome-design-md), path `design-md` → `library/` | `fcc70284932276d085f01ef9f83edde90b1258cb` | MIT (`LICENSE`) | 83 of the 84 `DESIGN.md` / `README.md` folders | `SKILL.md` is Jinnee OS's own (upstream has none). The `1win` folder (a gambling brand) is left out. |
| `emails` (2.1.7) | [coreyhaines31/marketingskills](https://github.com/coreyhaines31/marketingskills), path `skills/emails` | `1f837447b20bb6f0e72d87b80ece1c221c362c7d` | MIT (`LICENSE`) | the skill files | an "In this product" section at the end of `SKILL.md`: drafts only, sending through the `send_customer_email` approval, quiet hours, nothing made up, the customer's language. |

Why the taste skill is fetched rather than copied: it carries about 27 MB of photographs from Wikimedia and similar
sources, each under its own licence and credit. Fetching the pinned upstream commit keeps those files with their
source instead of re-publishing them inside this MIT repository, and keeps every install's clone small.
