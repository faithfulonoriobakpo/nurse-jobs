-- Nursing job finder schema. Safe to re-run.
--
-- jobs, geo_cache: written only by the GitHub Action with the service-role key (RLS on, no policies,
--                  so the public anon key can't read or change them).
-- job_status:      her tracking (applied / interview ...). Readable and writable by signed-in users only.
--                  Sign-ups are disabled, so "signed in" means an account created in the Supabase dashboard.

create table if not exists public.jobs (
  id          text primary key,              -- "nhs:C9346-26-1154", "adzuna:123", "reed:456"
  source      text not null,
  title       text not null,
  employer    text,
  location    text,
  region      text,
  miles       integer,                       -- distance from profile.home_postcode
  salary      text,
  contract    text,
  posted      date,
  closes      date,
  cos         text check (cos in ('welcome', 'licensed', 'unknown', 'no')),
  advert_cos  text,                          -- raw result of reading the NHS advert: welcome / not stated / no
  licensed    boolean,
  score       integer,
  matched     text,
  url         text,
  snippet     text,
  first_seen  date not null default current_date,
  last_seen   date not null default current_date,
  updated_at  timestamptz not null default now()
);
create index if not exists jobs_last_seen_idx on public.jobs (last_seen);
create index if not exists jobs_cos_idx on public.jobs (cos);

create table if not exists public.geo_cache (
  key     text primary key,                  -- a postcode ("NE9 6JE") or "town:gateshead"
  lat     double precision,
  lon     double precision,
  region  text
);

create table if not exists public.job_status (
  job_id      text primary key references public.jobs (id) on delete cascade,
  status      text not null check (status in ('saved', 'applied', 'interview', 'offer', 'rejected', 'hidden')),
  note        text,
  updated_at  timestamptz not null default now(),
  updated_by  uuid default auth.uid() references auth.users (id) on delete set null
);

alter table public.jobs enable row level security;
alter table public.geo_cache enable row level security;
alter table public.job_status enable row level security;

revoke all on public.jobs, public.geo_cache from anon, authenticated;
-- Revoke Supabase's default grants first: TRUNCATE in particular bypasses row-level security.
revoke all on public.job_status from anon, authenticated;
grant select, insert, update, delete on public.job_status to authenticated;
grant all on public.jobs, public.geo_cache, public.job_status to service_role;

drop policy if exists "signed-in users read statuses" on public.job_status;
drop policy if exists "signed-in users add statuses" on public.job_status;
drop policy if exists "signed-in users change statuses" on public.job_status;
drop policy if exists "signed-in users remove statuses" on public.job_status;
create policy "signed-in users read statuses" on public.job_status for select to authenticated using (true);
create policy "signed-in users add statuses" on public.job_status for insert to authenticated with check (true);
create policy "signed-in users change statuses" on public.job_status for update to authenticated using (true) with check (true);
create policy "signed-in users remove statuses" on public.job_status for delete to authenticated using (true);

-- Keep updated_at / updated_by current when a status changes.
create or replace function public.touch_job_status() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  new.updated_at := now();
  new.updated_by := coalesce(auth.uid(), new.updated_by);
  return new;
end $$;
drop trigger if exists job_status_touch on public.job_status;
create trigger job_status_touch before insert or update on public.job_status
  for each row execute function public.touch_job_status();
