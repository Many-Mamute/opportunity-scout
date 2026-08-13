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

Portugal switches between UTC+0 and UTC+1, so the file has **two** crons — `30 5` and `30 6` UTC. Both fire daily; the script itself checks the clock in Lisbon and only the one landing inside 06:00–07:00 actually runs (the other exits in seconds). Nothing to configure.

The workflow also has `workflow_dispatch`, which means you can press a button to run it manually at any time; manual runs skip the clock check.

## 8. First run

1. In your repo click the **Actions** tab. If GitHub asks you to enable workflows, click enable.
2. Click **opportunity-scout** in the left list → **Run workflow** → **Run workflow** (green button).
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

Other flags: `--limit 5` (shorter top section), `--source feup` (one source only), `--backfill` (raise per-source caps for a first sweep).

Run the tests any time with `python -m pytest tests` — they use fixture HTML and never touch the network.

## 11. Changing the send time

Edit `target_hour` in `config/filters.yaml` (local Lisbon hour), and move both cron lines in `.github/workflows/daily.yml` to `30 (hour-1)` and `30 hour` UTC. Commit; done.

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

Reply to any digest (keep "Opportunity Scout" in the subject, which replying does automatically) with either command on its own line, using the IDs printed on each card:

```
not-interested: E-0247, E-0301     ← suppress forever AND downweight this type/organiser/field
cant-attend: E-0255                ← suppress this one only; the type keeps full weight
```

After a `not-interested`, the next digest asks for a one-line reason; reply with one of: too expensive, wrong field, too basic, too advanced, bad timing, low prestige, dislike this format, dislike this organiser. It is stored and fed into the ranking weights.

## 14. Free-tier limits, and what happens when one is hit

| Service | Free allowance | Scout's own cap | When exhausted |
|---|---|---|---|
| GitHub Actions | 2,000 min/month (private repos, Free plan) | ~5–7 min/run + two ~1-min guard exits ≈ 220 min/mo | Runs stop until the month resets; GitHub emails you. |
| Tavily | 1,000 credits/month | 30 searches/day, counter persisted in `data/state.json` | Search sweep is skipped; the email's diagnostics say so; Tier 1 sources still run. |
| Gemini (Flash-Lite, AI Studio) | ~1,000 requests/day free | ≤30 batched calls/run | Leftover pages are listed as "dropped at the LLM cap" in diagnostics — never invented. |
| Gmail SMTP/IMAP | ~2,000 sends/day | 1 email/day | Not reachable at this volume. |
| Nominatim | 1 request/second policy | Throttled + cached forever | Unresolvable in-person locations are rejected as "location unverifiable". |

If a provider changes its free tier, the symptom is auth/quota errors in diagnostics or the Actions log — the fix is a decision for you, never a silent paid substitution by the scout.
