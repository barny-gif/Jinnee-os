# Onboarding – general (every pack starts here)

At most 3 questions at a time, conversational, never a form. Output: `brain/company.md`, containing only the owner's own statements. The installer already asked the business name, language and tone of voice (`config/business.yaml`, in the prompt as "the business"): do not ask them again, build on them. If the owner wants one of them changed, tell them to edit `config/business.yaml` (or run `python3 core/business.py setup --force yes`); the team never edits that file. At the end the lead reads it back in 10 lines and saves only after approval.

## 1. Who you are
- Business name, what you do in one sentence, for whom.
- How should I address you, and what name should I run under?

## 2. Tone
- Formal or informal with customers? Three words that describe the brand. Three that definitely don't.

## 3. Tools
- Invoicing software? Email? (→ connector setup)
- How often do you invoice, and what tends to slip?

## 4. Competitors
- 3–5 businesses or pages to keep an eye on (URL / Instagram / Facebook).
- What matters about them: prices, promotions, new services, posting?
- Save the list as `brain/watch/competitors.md` (one name with its page per line): Scout's Thursday round starts once it is there.

## 5. Limits and rhythm
- What should I never spend on, and never do without asking? (money, prices, refunds, customer emails, invoices and posts are already locked to "ask first" or stricter)
- When and where should the daily brief arrive? (default: 08:00, Telegram, 5 lines). Times are set in `config/business.yaml` under `schedule` (the owner edits it; `/schedule` shows what runs when): tell the owner the line to change, do not edit it.

If a vertical pack (ecom, photographer) is installed, its onboarding follows and asks only for the extras.
