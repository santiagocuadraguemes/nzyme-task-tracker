# Our automation's web address — a small decision

*Plain-language summary for a quick decision. ~2 min read.*

## The situation

Our meeting-notes automation works like this: when a new meeting note is created in Notion, Notion sends a message to our system at a specific **web address** (think of it like a mailing address). Our system receives it and does its job.

That address currently lives in about **10 places inside Notion** (one per team member's setup).

## The problem

That web address is **auto-assigned by Amazon** — we don't choose it. And it **changes** whenever we move or upgrade the underlying system. Every time it changes, someone has to **manually update all ~10 places in Notion**. It's tedious and easy to get wrong (miss one, and that person's meetings silently stop working).

This just happened because we moved everything into our new company AWS account, and it will happen **again** when we make a planned improvement (splitting the system into smaller, cleaner pieces).

**Analogy:** it's like the office having a phone number that the phone company reassigns every time we change plans — and we have to reprint 10 business cards each time.

## The options

| Option | What it is | Trade-off |
|--------|-----------|-----------|
| **1. Custom domain** ⭐ *recommended* | We use our **own permanent address**, e.g. `hooks.nzyme.com`. We point Notion at it **once**, forever. Behind the scenes we can move/upgrade anything and the address never changes. | Needs a small one-time setup using a domain **we own** (e.g. `nzyme.com` or `kiboventures.com`) and access to change its DNS settings. |
| **2. No-domain workaround** | A technical arrangement that keeps the current Amazon address stable **as long as we never rebuild it from scratch.** | Free, no domain needed. But the address stays an ugly Amazon URL, and it's a "handle with care" promise rather than a guarantee. |
| **3. Do nothing** | Keep updating the ~10 URLs by hand each time it changes. | Free, but recurring manual work and risk of mistakes. |

## Cost

**Essentially zero either way.** There's no monthly fee — we pay only per message, at about **$1 per *million* messages**. At our volume (a handful of meetings a day) that's a fraction of a cent per year.

The "cost" of the custom domain is **not money** — it's just needing:
1. A domain we already own (`nzyme.com`, `kiboventures.com`, etc.), and
2. Permission/access to add one small setting to it (a subdomain like `hooks.nzyme.com`).

## What we're asking

- **Can we use a subdomain of a company domain** (e.g. `hooks.nzyme.com`) for this? If yes, who controls the domain's settings so we can set it up?
- If a custom domain isn't possible, we'll fall back to **Option 2** (the free no-domain workaround).

Either way, the goal is the same: **set the address once and never have to update those 10 Notion entries by hand again.**
