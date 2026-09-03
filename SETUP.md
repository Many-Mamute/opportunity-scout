# Opportunity Scout — Setup

You have never used GitHub Actions before; that is fine. Every step below explains the concept in one plain sentence before using it. Do them in order. Total time: about 30 minutes.

## 1. Create the repository (and why it must be private)

A **repository** ("repo") is a folder of files that GitHub stores online and keeps a history of. This repo will contain your dedup history and feedback preferences in `data/`, plus a workflow that can send email as you — so nobody else should see it.

1. Go to https://github.com and sign in (create an account if needed — the Free plan is enough).
2. Click **+** (top right) → **New repository**.
3. Name it `opportunity-scout`, choose **Private**, do **not** tick any "initialize" boxes, click **Create repository**.
4. Upload the project: on the empty repo page click **uploading an existing file**, drag the entire contents of this folder in (including the hidden `.github` folder — if drag-and-drop skips it, use **Add file → Create new file**, type `.github/workflows/daily.yml` as the name, and paste that file's contents), then click **Commit changes**. (If you know git: `git init && git add -A && git commit -m init && git remote add origin <url> && git push -u origin main`.)

## 2. Turn on 2-Step Verification for your Gmail account

Google only issues app passwords (step 3) to accounts with **2-Step Verification** — a second check, like a phone code, on top of your password.

1. Go to https://myaccount.google.com/security.
2. Under "How you sign in to Google", click **2-Step Verification** and follow the prompts (phone number or authenticator app).

## 3. Generate a Gmail app password

An **app password** is a separate 16-character password Google creates so a script can send and read email without knowing your real password.

1. Go to https://myaccount.google.com/apppasswords (2-Step Verification must already be on, or this page refuses to load).
2. Type a name like `opportunity-scout`, click **Create**.
3. Copy the 16 characters shown. **The spaces Google displays are not part of the password.** You cannot view it again later — if lost, just create a new one.

## 4. Get a Tavily key (search)

Tavily is a search service with a free tier of 1,000 credits per month; one basic search costs one credit, and the scout hard-caps itself at 60 per day (and 900 per month).

1. Go to https://app.tavily.com, sign up (no credit card).
2. Copy the API key from the dashboard — it starts with `tvly-`.

## 5. Get a Gemini key from AI Studio (extraction)

Gemini Flash-Lite is Google's free-tier language model; the scout uses it only for pages without structured data, in batches, well under the free daily allowance.

1. Go to https://aistudio.google.com, sign in with any Google account.
2. Click **Get API key** → **Create API key**. Copy it (starts with `AIza`). No credit card is asked for.
3. Note: on the free tier Google may use prompts to improve its products; the scout only sends public event-page text, never your email.

## 6. Add the five GitHub Secrets

A **Secret** is a value GitHub stores encrypted and hands to your workflow at run time, so keys never appear in code.

1. In your repo: **Settings → Secrets and variables → Actions → New repository secret**.
2. Create these five, names exactly as written:

| Name | Value |
|---|---|
| `TAVILY_API_KEY` | from step 4 |
| `GEMINI_API_KEY` | from step 5 |
| `GMAIL_ADDRESS` | your full Gmail address |
| `GMAIL_APP_PASSWORD` | the 16 characters from step 3, no spaces |
| `RECIPIENT_EMAIL` | `mabcec2019@gmail.com` |

## 7. Understand the workflow and its schedule

A **workflow** is a recipe (the file `.github/workflows/daily.yml`) that GitHub runs for you on its own computers; a **cron schedule** is the line inside it saying *when*, written as `minute hour * * *` in UTC time.

The run happens in **two phases**, which is how the digest arrives at 06:30 exactly without wasting Actions minutes:

| Time (Lisbon) | Phase | What it does | Duration |
|---|---|---|---|
| 05:05 | `prepare` | scrapes, filters, ranks, cross-checks, renders, and parks the digest in `data/outbox.json` | ~6 min |
| 05:50 | `deliver` | reads the parked digest, sleeps until exactly **06:00**, sends | ~10 min (mostly sleeping) |

The delivery job fires ten minutes early on purpose. GitHub starts scheduled runs late — 3 to 8 minutes on this repo, which is why a 06:30 cron produced 06:33 and 06:38 deliveries. The job absorbs that delay by sleeping off whatever time is left before `send_at`. If GitHub is so late that 06:00 has already passed, it sends immediately rather than waiting a day. Change the time with `send_at` in `config/filters.yaml`, and move the four crons to match.

GitHub bills by wall-clock time, so holding one job open from 06:10 to 06:30 would cost 20 idle minutes a day (~600 min/month). Two separate jobs cost about 7 minutes a day instead, and the mail still lands at 06:30.

Portugal switches between UTC+0 and UTC+1, so each phase has **two** crons. A cheap shell step checks the Lisbon clock *before* installing anything, so a wrong-DST wake-up costs seconds rather than a billed minute. Nothing to configure.

**If the 06:10 job fails**, the 06:30 job finds no digest, exits non-zero and GitHub emails you. It will never send a stale digest from a previous day either — it checks the date on the outbox first.

The workflow also has `workflow_dispatch`, which means you can press a button to run it manually at any time; manual runs skip the clock check.

## 8. First run

1. In your repo click the **Actions** tab. If GitHub asks you to enable workflows, click enable.
2. Click **opportunity-scout** in the left list → **Run workflow**. A **mode** dropdown appears:
   - **full** (default) — build and email immediately. Use this for testing; you never have to wait until 06:30.
   - **prepare** — build now and queue it for the 06:30 delivery job.
   - **deliver** — send whatever is currently queued.

**The daily schedule is already automatic.** Once `daily.yml` is on your default branch, the four crons fire every morning on their own — there is nothing else to switch on. Two caveats worth knowing: GitHub disables scheduled workflows in a repository with no activity for 60 days (it emails you first, and one click re-enables them), and scheduled runs can start a few minutes late when GitHub is busy, which is part of why the prepare phase has a 20-minute head start on the 06:30 delivery.
3. Wait a few minutes; a digest should arrive at the recipient address. The very first run backfills every currently-open opportunity into the database but still emails only the top 20 — the backlog drains over the following days and Sunday roundups. (Optionally, for a bigger first sweep, run locally once with `--backfill`, step 10, and push the resulting `data/` files.)

## 9. Reading the Actions log when a run fails

1. **Actions** tab → click the failed run (red ✗) → click the **scout** job.
2. Each step expands; the red one contains the error. The most common by far: `535 Username and Password not accepted` means the app password secret is wrong — regenerate (step 3) and re-save the secret.
3. GitHub emails you automatically when a scheduled workflow fails, so **a missing digest always means the job broke** — check the log, never assume "nothing today".

## 10. Running locally in dry-run mode

Dry-run prints the digest to your terminal instead of emailing it, and writes no state.

```
pip install -r requirements.txt
cp .env.example .env          # then edit .env with your real values
export $(grep -v '^#' .env | xargs)      # loads the values (Windows: set them in PowerShell)
python main.py --dry-run
```

Other flags: `--limit 5` (shorter digest), `--source feup` (one source only), `--backfill` (raise per-source caps for a first sweep), `--prepare` (park the digest instead of sending), `--deliver` (send a parked digest).

Run the tests any time with `python -m pytest tests` — they use fixture HTML and never touch the network.

## 11. Changing the send time

Edit `target_hour` in `config/filters.yaml` (local Lisbon hour), then move the four crons in `.github/workflows/daily.yml`, keeping the 20-minute gap between the prepare pair and the deliver pair, and keeping each pair one hour apart for DST. The `case` statement in the "Decide what this run should do" step lists the cron strings — update those to match, or the workflow will not know which phase to run.

For example, to deliver at 07:00 instead: prepare crons `40 5` and `40 6`, deliver crons `0 6` and `0 7`, `target_hour: 7`.

## 12. Adding a source

Open `config/sources.yaml` and append one entry — under 10 lines, no code changes:

```yaml
  - name: my-new-source
    tier: 1
    type: page
    urls: ["https://example.org/events"]
    link_pattern: "/event/"      # optional: only follow links matching this
```

If it errors, the diagnostics section of the next email says so plainly.

## 13. Replying with feedback

Every card has three buttons — **Interested**, **Meh**, **Not for me**. Tapping one opens a pre-filled email; just hit send. You can also type the lines yourself:

```
interested: E-0004        ← more of this kind; boosts this type, organiser and field
meh: E-0011               ← hides this one, but keeps hunting in the same area
uninterested: E-0002      ← wrong direction; hides it and downweights the area
```

The difference between **Meh** and **Not for me** is the whole point of having both. *Meh* says the area is right but this instance isn't — the field keeps its weight (or its boost) and only the event disappears. *Not for me* says stop looking here at all — the type, the organiser and the field all lose weight.

Anything you write after the ID, or on a following line, is kept as a justification and read into the weights. Several IDs per line work: `interested: E-0004, E-0011`. Older `not-interested:` / `cant-attend:` replies still work.

## 14. Free-tier limits, and what happens when one is hit

| Service | Free allowance | Scout's own cap | When exhausted |
|---|---|---|---|
| GitHub Actions | 2,000 min/month (private repos, Free plan) | ~6 min prepare + ~10 min deliver (mostly the wait to 06:00) + two ~15-sec guard exits ≈ 500 min/mo | Runs stop until the month resets; GitHub emails you. |
| Tavily | 1,000 credits/month | 60/day and 900/month, both persisted in `data/state.json`. One run uses ~23: 15 for the search sweep, up to 8 for cross-checking events with missing facts (~690/month). | Search sweep is skipped; diagnostics say so; Tier 1 sources still run. |
| Gemini (Flash-Lite, AI Studio) | ~1,000 requests/day free | ≤30 batched calls/run | Leftover pages are listed as "dropped at the LLM cap" in diagnostics — never invented. |
| Gmail SMTP/IMAP | ~2,000 sends/day | 1 email/day | Not reachable at this volume. |
| Nominatim | 1 request/second policy | Throttled + cached forever | Unresolvable in-person locations are rejected as "location unverifiable". |

If a provider changes its free tier, the symptom is auth/quota errors in diagnostics or the Actions log — the fix is a decision for you, never a silent paid substitution by the scout.


## 15. Dead sources, and turning them back on

Sixteen of the 41 sources returned 404/405/503 on the first live run — company careers pages move constantly. Each is still in `config/sources.yaml` with `enabled: false` and a comment recording exactly what it returned:

```yaml
  - name: efacec
    enabled: false          # dead on 2026-08-13 run: HTTP 404
```

To revive one: find the page's real current URL in your browser, paste it into `urls:`, delete the `enabled: false` line, commit. If it fails again the diagnostics will say so by name — you never have to guess which source is broken.

Twenty-five sources are live, including all of Meetup, Luma, Devpost, MLH, Unstop, Kaggle, FEUP, U.Porto, UPTEC, JuniFEUP, AEFEUP, Formula Student Portugal, and the Bosch/Preh/Kirchhoff/Sonae/Siemens/Vestas careers pages.

## 16. How the 1-5 scores are produced

They are lookup tables in `config/filters.yaml`, not judgements about the specific event. Each card states its own basis on the **Why** row.

- **Prestige** is organiser-name matching only. `prestige_boost` maps substrings (feup, bosch, siemens, kaggle, mlh…) to a number; anything on `override.prestigious_organisers` (CERN, ESA, EPFL…) scores 5; everything unrecognised gets `base_prestige_default: 2`. A brilliant event by an organiser not on the list scores 2 — that is the tool's blind spot, not a verdict.
- **CV value** starts from `base_cv` for the event type (internship / competition / hackathon / summer school = 4; workshop / masterclass / conference / other = 2) and gains +1 if prestige reached 4 or more.
- **Networking** is `base_networking` for the type alone, with no organiser input.
- **Effort** is `hours_per_day` for the type multiplied by the number of days (internships are counted in weeks).

To change them, edit the tables. To teach the ranking instead, reply — `interested` / `meh` / `uninterested` adjust the weight of the type, organiser and field, which is the part that actually learns.

## 17. Why there is no expand/collapse toggle

There was one, briefly. `<details>`/`<summary>` is the only disclosure widget email clients accept without JavaScript, and Gmail strips the tag while keeping its contents — so every row rendered fully expanded, the Sunday recap printed the entire digest a second time, and a stray `＋` appeared on some titles and not others.

The bottom rows are now compact instead: title, organiser, when, where, cost, deadline, a one-line description and the three feedback buttons. Everything you need to decide, without a link and without a toggle that only works in half the world's mail clients. Anything that already has a full card higher up is listed as a single back-reference line rather than repeated.

## 18. The bottom list, and the Sunday recap

On weekdays, the bottom section is **Also still open**: every open opportunity that did not make the top ten, so nothing good is invisible just because it ranked eleventh.

On **Sunday** it becomes **Sunday recap — everything still open**: a complete inventory of every opportunity you have not rejected, including the ones shown as full cards above and the unverified snippets, sorted by soonest deadline. Nothing is filtered out of that list. Each row is a `<details>` toggle: tap it to expand the full card, including the feedback buttons, without leaving the email.

Apple Mail, iOS Mail and Thunderbird collapse these properly. Gmail strips the `<details>` tag but keeps its contents, so in Gmail the rows render already expanded — longer, but nothing is hidden behind a link. There is no way to build a reliable collapsible section that Gmail honours without JavaScript, which email cannot run.

## 19. How feedback actually reaches the scout

Nothing you send is read by a person or by an AI in a chat window. The workflow polls the Gmail inbox over IMAP at the **start of every run**, before it scrapes anything. So:

- A reply sent at 14:00 is applied during the next morning's 06:10 prepare run, and its effect shows in that morning's digest.
- To apply it immediately, trigger a manual run (**Actions → Run workflow → mode: full**).
- The poller tracks the highest message UID it has processed, so nothing is read twice and nothing is missed if Gmail marks a message read.
- The digest itself lands in the same inbox. It carries an `X-Opportunity-Scout` header and is fingerprinted by its own text, so it is skipped rather than parsed as feedback.
- If IMAP fails, the Diagnostics section says `Could not read replies: ...`. Silence there means the poll worked.

## 20. If a digest looks wrong

The three failure shapes and what they mean:

- **Too much noise** (social events, irrelevant posts) → add a pattern to `social_noise` in `config/filters.yaml`. Anything matching that list is dropped before scoring, no matter how it ranked.
- **Almost nothing, and Diagnostics shows Gemini errors** → the Gemini key is the problem. The scout auto-discovers a working model if the configured one 404s, but it cannot fix an invalid key. Regenerate it (step 5).
- **An online event starts in the middle of the night** → that is deliberate. The scout converts the time to Lisbon, flags it (`starts 02:00 Lisbon — you would be up at night`) and leaves the decision to you. Set `reject_night_online: true` in `config/filters.yaml` if you would rather it drop them.
- **A score or its explanation looks out of date** → it isn't any more. Estimates and their wording are recomputed from `config/filters.yaml` on every run, so editing a table changes every stored event on the next digest. (They used to be computed once and frozen into `seen.jsonl`.)
- **Junk you thought was filtered keeps reappearing** → it was stored before the rule existed. Each run now re-tests every stored event against the current filters and retires the failures, listing them in Diagnostics. One run after a config change is enough.
- **Nothing at all, and no email** → the job crashed. Check the Actions log (step 9). The scout never sends a clean-looking email built on a failed run.


## 21. Where your feedback goes

Replies are read at the start of every run and then **moved out of your inbox** into a Gmail label called `Opportunity Scout` (created automatically on first run). The poller checks both the inbox and that label, so if you also add a Gmail filter to skip the inbox entirely, feedback is still found.

To add that filter — it removes the reply from your inbox the moment you send it, rather than the next morning:
**Gmail → Settings → Filters → Create a new filter → Subject: `Opportunity Scout feedback` → Create filter → tick "Skip the Inbox" and "Apply the label: Opportunity Scout".**

## 22. Cross-checking against independent sources

Events arrive with gaps — no dates, no cost, an organiser the prestige table has never heard of. Each run picks up to `enrichment_max_events` (default 6) of the highest-ranked gap-ridden events and runs one search each.

A result only counts as a second opinion if it passes three tests:

1. **Different site.** Same registrable domain as the event's own URL is rejected, and only one result per domain is kept — two pages on `publico.pt` are one source.
2. **Not an aggregator.** Eventbrite, Luma, Meetup, LinkedIn, Facebook and friends republish rather than report.
3. **Not a copy.** If the text is more than 72% similar to the original description, it is a syndicated press release, not corroboration.

What survives is quoted to the model under the usual anti-fabrication instruction. Filled fields appear on the card under **Cross-checked**, naming the domains. A prestige score raised this way says so explicitly instead of citing the lookup table. Anything with no independent support stays "not stated" — the scout does not guess.

## 23. Foreign events with a Portuguese twin

If an event's title, stripped of country names and years, matches an open Portuguese event, the foreign one is flagged: *"a Portuguese edition exists: E-0001"*. It is not rejected — the German round of a competition may well be the better one — but you are told, rather than having to spot it yourself across two cards.


## 24. Eligibility: what you can actually apply to

The CERN Summer Student Programme wants six completed semesters. That sentence
was being extracted and printed on the card — but never compared against where
you are, so the scout recommended something you could not apply to.

`config/filters.yaml` now records your position in the degree:

```yaml
level:
  academic:
    first_semester_end: 2027-02-05
    second_semester_end: 2027-07-13
    degree_semesters: 10          # MIEM: 5 years
    assume_within_days: 365       # for events that state no dates
```

From that the scout works out how many semesters you will have finished by the
event's start date, and rejects anything asking for more. It reads the bar out
of prose in either language: "at least six semesters", "two years of university
study", "third-year students and above", "final year", "terceiro ano",
"pelo menos quatro semestres". Events with no such requirement are unaffected,
and one you will have grown into by the time it runs is kept.

The rejection is explicit in Diagnostics: *"requires 6 completed semesters
(\"completed at least six semesters\"); by 2027-06-21 you will have 1"*.

**Keep those two dates current.** They are the anchor for the whole
calculation; when the 2027/28 FEUP calendar is published, update them.


## 25. Shortlist, and the "did you apply?" follow-up

Two more reply commands:

```
shortlist: E-0049      (or: pin:)    keeps it at the top of every digest
unshortlist: E-0049    (or: unpin:)  releases it
applied: E-0049                      you sent an application
skipped: E-0049        (or: didn't apply:)   you decided not to
```

A pinned event appears in **Your shortlist** above everything else until its
deadline passes. Pins survive reloads — they are stored on the event, not as a
flag, so the nightly flag reset cannot wipe them.

When something you marked *interested* or pinned is about to close, the next
digest asks once whether you applied. Once, not every morning: the question is
recorded on the event so it cannot nag.

## 26. Seasonal memory

Most of these run annually. Each time an event's application window is seen,
the month and day are recorded against a family key (the title with years and
country names stripped). When that anniversary comes round and nothing matching
is currently open, the digest says so under **Expected to open soon**, naming
the year it is extrapolating from:

> CERN Summer Student Programme — opened on 2025-11-01 last time, so expect it
> around 2026-11-01 (56 days away). Not found open yet; this is a prediction
> from last year, not a listing.

It is labelled a prediction every time, because that is what it is.

## 27. Cost policy

- **At or under €50**, price is never a reason to hide something.
- **€50 to €500**, the event is shown with the price stated plainly. If the page
  names no prize, certificate or selection process, the note says so directly
  rather than the event being dropped.
- **Above €500**, still rejected.
- A **certificate mill** — paid, self-paced, no cohort, no named institution —
  is rejected at any price, because that is a quality judgement, not a price one.

## 28. The academic calendar beyond 2026/27

`config/filters.yaml` holds the published FEUP 2026/27 dates. Until the real
2027/28 calendar exists, later years are **projected** by shifting those dates
forward whole years (`project_years: 4`). Without this the calendar simply
stops protecting you on 14 July 2027 — a heavy event in January 2028 would see
no exam period at all.

Anything judged against a projected date is flagged *"term dates projected, not
published"* on the card. **Replace `exam_periods`, `class_periods` and `breaks`
with the real dates when FEUP publishes them**, and update
`level.academic.first_semester_end` / `second_semester_end`, which anchor the
eligibility calculation.


## 29. "Not yet" — events you are too early for

Something rejected purely on eligibility is no longer discarded. It is parked
with the date you would qualify, and it comes back on its own:

- It stays out of the digest entirely while the date is far off.
- Once you are within `eligibility_horizon_days` (default 240) it appears under
  **Not yet — but worth knowing about**, one line, with the date.
- On that date it re-enters the digest normally, flagged *"you are now eligible
  for this"*.

CERN is the worked example: parked in 2026, first mentioned in early 2029,
released on 13 July 2029.

An explicit `uninterested:` still beats this — saying no is permanent, and a
future eligibility date will not resurrect something you rejected.

## 30. Quiet weeks

When fewer than `thin_digest_threshold` events clear the filters, the scout
spends leftover search budget on a **broader sweep** instead of sending you a
thin digest. Those queries live in `sources.yaml` marked `fallback: true`; they
are deliberately vaguer than the daily ones, trading precision for coverage,
which is only the right trade when the precise queries came back empty. They
never run on a normal week, and the Diagnostics line says when they did.

Worst case — a quiet week with full enrichment — is 29 Tavily credits, still
inside both the daily cap and the free tier over a month.
