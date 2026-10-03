"""One-off Supabase setup for the job finder. Safe to re-run.

Needs, in .env.local (git-ignored) or the environment:
  SUPABASE_ACCESS_TOKEN   personal access token from https://supabase.com/dashboard/account/tokens
  SUPABASE_PROJECT_REF    the project's reference ID (Project Settings -> General)

It then:
  1. creates the tables and access rules in supabase/schema.sql
  2. turns off public sign-ups and allows sign-in from the GitHub Pages dashboard
  3. saves the project URL and API keys to .env.local (for local runs)
  4. stores them as GitHub Actions secrets for the daily run (needs the gh CLI, signed in)

Keys are never printed.
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env.local"
API = "https://api.supabase.com/v1"
REPO = "faithfulonoriobakpo/nurse-jobs"
DASHBOARD_URL = "https://faithfulonoriobakpo.github.io/nurse-jobs/"


def read_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                env[k.strip()] = v.strip().strip('"')
    return {**env, **{k: v for k, v in os.environ.items() if k.startswith("SUPABASE_")}}


def write_env(updates):
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    lines = [l for l in lines if l.partition("=")[0].strip() not in updates]
    lines += [f"{k}={v}" for k, v in updates.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def api(method, path, token, body=None):
    req = urllib.request.Request(
        API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "nursing-job-finder-setup"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        sys.exit(f"Supabase API {method} {path} failed: {e.code} {e.read().decode('utf-8', 'replace')[:400]}")


def pick_key(keys, new_type, legacy_name):
    """Prefer the new publishable/secret keys; fall back to the legacy anon/service_role JWTs."""
    for k in keys:
        if k.get("type") == new_type and k.get("api_key"):
            return k["api_key"]
    for k in keys:
        if k.get("name") == legacy_name and k.get("api_key"):
            return k["api_key"]
    sys.exit(f"No {new_type} / {legacy_name} API key found on the project")


def main():
    env = read_env()
    token, ref = env.get("SUPABASE_ACCESS_TOKEN"), env.get("SUPABASE_PROJECT_REF")
    if not (token and ref):
        sys.exit("Set SUPABASE_ACCESS_TOKEN and SUPABASE_PROJECT_REF in .env.local first (see the top of this file).")

    project = api("GET", f"/projects/{ref}", token)
    print(f"Project: {project.get('name')} ({project.get('region')}), status {project.get('status')}")
    if project.get("status") != "ACTIVE_HEALTHY":
        sys.exit("The project isn't ready yet (or is paused). Wait for it to show as active in the dashboard, then re-run.")

    print("Creating tables and access rules...")
    api("POST", f"/projects/{ref}/database/query", token, {"query": (ROOT / "supabase" / "schema.sql").read_text(encoding="utf-8")})
    tables = api("POST", f"/projects/{ref}/database/query", token, {"query":
        "select table_name from information_schema.tables where table_schema = 'public' order by 1"})
    print("  tables:", ", ".join(t["table_name"] for t in tables))

    print("Configuring sign-in (sign-ups off, dashboard allowed as redirect)...")
    # The project is shared with dev-jobs, so add to the allowed addresses rather than replacing them.
    allowed = [u for u in (api("GET", f"/projects/{ref}/config/auth", token).get("uri_allow_list") or "").split(",") if u]
    api("PATCH", f"/projects/{ref}/config/auth", token,
        {"disable_signup": True, "site_url": DASHBOARD_URL,
         "uri_allow_list": ",".join(allowed + ([DASHBOARD_URL] if DASHBOARD_URL not in allowed else []))})

    keys = api("GET", f"/projects/{ref}/api-keys?reveal=true", token)
    values = {
        "SUPABASE_URL": f"https://{ref}.supabase.co",
        "SUPABASE_ANON_KEY": pick_key(keys, "publishable", "anon"),
        "SUPABASE_SERVICE_KEY": pick_key(keys, "secret", "service_role"),
    }
    write_env(values)
    print(f"Saved project URL and keys to {ENV_FILE.name}")

    gh = shutil.which("gh") or str(Path(os.environ.get("LOCALAPPDATA", "")) / "gh-cli" / "bin" / "gh.exe")
    for name, value in values.items():
        subprocess.run([gh, "secret", "set", name, "--repo", REPO], input=value.encode(), check=True,
                       stdout=subprocess.DEVNULL)
    print(f"Stored {', '.join(values)} as GitHub Actions secrets on {REPO}")


if __name__ == "__main__":
    main()
