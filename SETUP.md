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

Tavily is a search service with a free tier of 1,000 credits per month; one basic search costs one credit, and the scout hard-caps itself at 30 per day.

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
| 06:10 | `prepare` | scrapes, filters, ranks, renders, and parks the finished digest in `data/outbox.json` | ~6 min |
| 06:30 | `deliver` | reads the parked digest and sends it | ~40 sec |

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
| GitHub Actions | 2,000 min/month (private repos, Free plan) | ~6 min prepare + ~1 min deliver + two ~15-sec guard exits ≈ 230 min/mo | Runs stop until the month resets; GitHub emails you. |
| Tavily | 1,000 credits/month | 12 searches/day (8 used per run), counter persisted in `data/state.json` | Search sweep is skipped; the email's diagnostics say so; Tier 1 sources still run. |
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

## 17. The bottom list, and the Sunday recap

On weekdays, the bottom section is **Also still open**: every open opportunity that did not make the top ten, so nothing good is invisible just because it ranked eleventh.

On **Sunday** it becomes **Sunday recap — everything still open**: a complete inventory of every opportunity you have not rejected, including the ones shown as full cards above and the unverified snippets, sorted by soonest deadline. Nothing is filtered out of that list. Each row is a `<details>` toggle: tap it to expand the full card, including the feedback buttons, without leaving the email.

Apple Mail, iOS Mail and Thunderbird collapse these properly. Gmail strips the `<details>` tag but keeps its contents, so in Gmail the rows render already expanded — longer, but nothing is hidden behind a link. There is no way to build a reliable collapsible section that Gmail honours without JavaScript, which email cannot run.

## 18. How feedback actually reaches the scout

Nothing you send is read by a person or by an AI in a chat window. The workflow polls the Gmail inbox over IMAP at the **start of every run**, before it scrapes anything. So:

- A reply sent at 14:00 is applied during the next morning's 06:10 prepare run, and its effect shows in that morning's digest.
- To apply it immediately, trigger a manual run (**Actions → Run workflow → mode: full**).
- The poller tracks the highest message UID it has processed, so nothing is read twice and nothing is missed if Gmail marks a message read.
- The digest itself lands in the same inbox. It carries an `X-Opportunity-Scout` header and is fingerprinted by its own text, so it is skipped rather than parsed as feedback.
- If IMAP fails, the Diagnostics section says `Could not read replies: ...`. Silence there means the poll worked.

## 19. If a digest looks wrong

The three failure shapes and what they mean:

- **Too much noise** (social events, irrelevant posts) → add a pattern to `social_noise` in `config/filters.yaml`. Anything matching that list is dropped before scoring, no matter how it ranked.
- **Almost nothing, and Diagnostics shows Gemini errors** → the Gemini key is the problem. The scout auto-discovers a working model if the configured one 404s, but it cannot fix an invalid key. Regenerate it (step 5).
- **An online event starts in the middle of the night** → that is deliberate. The scout converts the time to Lisbon, flags it (`starts 02:00 Lisbon — you would be up at night`) and leaves the decision to you. Set `reject_night_online: true` in `config/filters.yaml` if you would rather it drop them.
- **Junk you thought was filtered keeps reappearing** → it was stored before the rule existed. Each run now re-tests every stored event against the current filters and retires the failures, listing them in Diagnostics. One run after a config change is enough.
- **Nothing at all, and no email** → the job crashed. Check the Actions log (step 9). The scout never sends a clean-looking email built on a failed run.
