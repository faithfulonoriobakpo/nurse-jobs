# Nursing Job Finder

A daily search for UK nursing jobs that fit an NMC-registered adult nurse at Band 5 level
who needs **Skilled Worker visa sponsorship (a Certificate of Sponsorship, COS)**.
Results are published as a dashboard on GitHub Pages.

## The dashboard

Each job gets one sponsorship label:

- **Welcomes COS**: the advert itself says applicants needing sponsorship are welcome. On NHS Jobs this is the "Certificate of Sponsorship" section.
- **Licensed sponsor**: the advert doesn't say, but the employer is on the [Home Office register of licensed sponsors](https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers).
- **Not confirmed**: neither of the above. This includes employers whose name on the register doesn't match the advert.

Adverts that say sponsorship is *not* available are left out.

On the dashboard she can:
- filter by region, distance from Gateshead, contract type (Bank / Permanent / Fixed-Term) or site, or show care homes only;
- sort by best match, newest, closing soon or nearest;
- track each job as Saved, Applied, Interview, Offer, Rejected or Hidden, and see them all under **My applications** (closed jobs stay there).

Signed in, statuses are stored in Supabase and shared between devices and between the two logins. Signed out, they're saved in that browser only, then uploaded on first sign-in. Logins are created in the Supabase dashboard (Authentication → Users); public sign-ups are turned off.

## How it runs

The GitHub Action `.github/workflows/find-jobs.yml` runs every morning (and on demand from the Actions tab → "Run workflow"). It:

1. Searches NHS Jobs across the UK. Adzuna and Reed are added too if their keys are set as repo secrets `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` and `REED_API_KEY`.
2. Keeps only roles matching the CV (`profile.json`).
3. Checks each new NHS advert for its sponsorship section, and each employer against today's sponsor register.
4. Saves every job to Supabase with its first and last seen dates. A job stays listed until it closes, even if a day's search misses it (NHS Jobs: 1 day's grace; Adzuna/Reed: 7 days, since their searches are capped).
5. Publishes `output/dashboard/` to GitHub Pages.

## Supabase

- `supabase/schema.sql` holds the tables (`jobs`, `geo_cache`, `job_status`) and access rules. Row-level security is on everywhere. Only the Action's secret key can touch `jobs`/`geo_cache`; only signed-in users can read or change `job_status`.
- `setup_supabase.py` applies the schema and sign-in settings, and stores the keys as GitHub secrets (`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`). It needs `SUPABASE_ACCESS_TOKEN` and `SUPABASE_PROJECT_REF` in `.env.local` (git-ignored). Re-run it after changing the schema.
- Free Supabase projects pause after about a week without use. The daily run should keep it awake; if it is ever paused, restore it from the Supabase dashboard.

The first run checks about 1,000 adverts, which takes around 5 minutes. After that, only new adverts are checked.

## Run locally

```
python find_jobs.py --open
```

This writes `output/dashboard/index.html` and `output/jobs_latest.csv`. Only the Python standard library is needed. With the Supabase keys in `.env.local` it reads and writes the same database as the Action; without them it keeps its state in `output/` instead.

## Tuning (`profile.json`)

- `searches`: the keywords sent to each site.
- `title_exclude`: roles that are dropped outright (senior bands, specialist posts, midwifery, children's nursing, HCA roles, internal-only posts...).
- `boost` / `penalty`: words that raise or lower the match score (0-100).
- `max_annual_salary_floor`: drops posts whose starting salary is above Band 5.
- `needs_cos`: drops adverts that rule out sponsorship.
- `location` + `radius_miles`: limit the search to an area. Leave empty for the whole UK.
- `home_postcode`: where dashboard distances are measured from. This repo is public, so don't put a home address here.

Sponsorship labels are a guide. Always confirm on the advert, because Skilled Worker salary and other rules still apply.
