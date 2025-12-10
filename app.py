import streamlit as st
import sqlite3
import requests
import hashlib
from datetime import datetime
from contextlib import closing
from bs4 import BeautifulSoup

# =========================================================
# CONFIG FROM SECRETS
# =========================================================
# In cloud/local, set these in:
#   .streamlit/secrets.toml  (local)
#   Streamlit Cloud "App secrets" (cloud)
TELEGRAM_BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "")
CRON_KEY = st.secrets.get("CRON_KEY", "")  # shared secret for cron calls

DB_PATH = "monitor.db"


# =========================================================
# DB HELPERS
# =========================================================
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    with closing(get_conn()) as conn:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                description TEXT,
                last_hash TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER NOT NULL,
                checked_at TEXT NOT NULL,
                changed INTEGER NOT NULL,
                error TEXT,
                FOREIGN KEY(site_id) REFERENCES sites(id)
            )
            """
        )
        conn.commit()


def get_last_error_for_site(site_id: int):
    """Return last non-null error and its time for a site, or (None, None)."""
    with closing(get_conn()) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT error, checked_at
            FROM checks
            WHERE site_id = ? AND error IS NOT NULL
            ORDER BY checked_at DESC
            LIMIT 1
            """,
            (site_id,),
        )
        row = c.fetchone()
    if row:
        return row[0], row[1]
    return None, None


# =========================================================
# CORE MONITORING LOGIC
# =========================================================
def normalize_html(html: str) -> str:
    """Extract main text and normalize whitespace."""
    soup = BeautifulSoup(html, "html.parser")

    # Very simple: get visible text only.
    text = soup.get_text(separator=" ", strip=True)
    return " ".join(text.split())


def compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def send_telegram_message(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        # Telegram not configured; skip silently
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        # For cloud debugging, print to logs
        print("Error sending Telegram message:", e)


def fetch_page(url: str) -> str:
    """
    Fetch page HTML with a browser-like header.
    Some sites are more friendly to this than to default python-requests.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = requests.get(url, headers=headers, timeout=25)
    resp.raise_for_status()
    return resp.text


def check_site(site_row):
    """
    site_row = (id, url, description, last_hash, enabled, created_at)
    Returns: (changed: bool, error: str | None, new_hash: str | None)
    """
    site_id, url, description, last_hash, enabled, created_at = site_row

    try:
        html = fetch_page(url)
        text = normalize_html(html)
        new_hash = compute_hash(text)

        if last_hash is None:
            # First successful fetch: store hash, no "change" notification
            changed = False
        else:
            changed = (new_hash != last_hash)

        with closing(get_conn()) as conn:
            c = conn.cursor()
            c.execute("UPDATE sites SET last_hash = ? WHERE id = ?", (new_hash, site_id))
            c.execute(
                """
                INSERT INTO checks (site_id, checked_at, changed, error)
                VALUES (?, ?, ?, ?)
                """,
                (site_id, datetime.utcnow().isoformat(), int(changed), None),
            )
            conn.commit()

        return changed, None, new_hash

    except Exception as e:
        # Log the error for this run, but don't touch last_hash
        err_text = str(e)
        with closing(get_conn()) as conn:
            c = conn.cursor()
            c.execute(
                """
                INSERT INTO checks (site_id, checked_at, changed, error)
                VALUES (?, ?, ?, ?)
                """,
                (site_id, datetime.utcnow().isoformat(), 0, err_text),
            )
            conn.commit()

        print(f"Error checking site {url}: {err_text}")
        return False, err_text, None


def run_all_checks():
    """Run checks for all enabled sites and send Telegram for changes."""
    with closing(get_conn()) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, url, description, last_hash, enabled, created_at "
            "FROM sites WHERE enabled = 1"
        )
        sites = c.fetchall()

    results = []
    for site in sites:
        changed, error, _ = check_site(site)
        site_id, url, description, *_ = site

        if changed:
            msg = f"🔔 Website changed:\n{description or url}\n{url}"
            send_telegram_message(msg)

        results.append((site_id, url, changed, error))

    return results


# =========================================================
# STREAMLIT UI / ROUTING (CRON MODE vs DASHBOARD MODE)
# =========================================================
def _get_query_params():
    # Support both new and old Streamlit APIs
    if hasattr(st, "query_params"):
        return st.query_params
    else:
        return st.experimental_get_query_params()


def is_cron_request():
    params = _get_query_params()

    # Streamlit may give list-like or str-like values depending on version
    def _get_val(key):
        val = params.get(key)
        if isinstance(val, list):
            return val
        return [val] if val is not None else []

    cron_vals = _get_val("cron")
    key_vals = _get_val("key")

    is_cron = any(v in ("1", "true", "True") for v in cron_vals)
    # If CRON_KEY is empty, accept any key (useful for local testing)
    key_ok = (not CRON_KEY) or (CRON_KEY in key_vals)
    return is_cron and key_ok


def render_cron_page():
    st.write("Running scheduled checks...")
    results = run_all_checks()
    changed_count = sum(1 for _, _, changed, _ in results if changed)
    error_count = sum(1 for _, _, _, err in results if err)

    st.write(f"Done. Changed: {changed_count}, Errors: {error_count}")


def render_dashboard():
    st.set_page_config(page_title="Website Change Notifier", layout="wide")
    st.title("🔍 Website Change Notifier")

#     st.markdown(
#         """
# Monitor multiple public websites and get **Telegram notifications** whenever
# their content changes (any text change / new items, etc.).

# **Flow:**
# 1. Add URLs below.
# 2. Deploy to Streamlit Cloud.
# 3. Configure cron-job.org to call this app every 30 minutes.
# 4. Telegram pings you whenever something changes.
# """
#     )

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        st.warning(
            "Telegram is not fully configured. "
            "Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in secrets to receive alerts."
        )

    # ------------- Add new site -------------
    st.header("Add a website")
    with st.form("add_site_form", clear_on_submit=True):
        url = st.text_input("URL", placeholder="https://example.com")
        description = st.text_input(
            "Description (optional)", placeholder="e.g., Example homepage"
        )
        submitted = st.form_submit_button("Add website")

        if submitted:
            if not url:
                st.error("URL is required.")
            else:
                with closing(get_conn()) as conn:
                    c = conn.cursor()
                    try:
                        c.execute(
                            """
                            INSERT INTO sites (url, description, created_at)
                            VALUES (?, ?, ?)
                            """,
                            (
                                url.strip(),
                                (description.strip() or None),
                                datetime.utcnow().isoformat(),
                            ),
                        )
                        conn.commit()
                        st.success("Website added.")
                    except sqlite3.IntegrityError:
                        st.error("This URL is already being monitored.")

    # ------------- List & manage sites -------------
    st.header("Monitored websites")

    with closing(get_conn()) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, url, description, last_hash, enabled, created_at FROM sites"
        )
        sites = c.fetchall()

    if not sites:
        st.info("No sites added yet.")
    else:
        for site in sites:
            site_id, url, description, last_hash, enabled, created_at = site
            with st.container():
                cols = st.columns([4, 4, 1, 1])
                # Left: description + URL
                cols[0].markdown(f"**{description or 'No description'}**  \n`{url}`")

                # Middle: status + last error if any
                last_error, error_time = get_last_error_for_site(site_id)
                status_lines = []
                status_lines.append(f"First added: `{created_at}`")
                status_lines.append(
                    "First successful snapshot pending"
                    if last_hash is None
                    else "Has previous snapshot"
                )
                if last_error:
                    short_err = (last_error[:180] + "...") if len(last_error) > 180 else last_error
                    status_lines.append(
                        f"⚠️ Last error at `{error_time}`:\n`{short_err}`"
                    )
                cols[1].markdown("  \n".join(status_lines))

                # Enabled toggle
                enabled_box = cols[2].checkbox(
                    "Enabled", value=bool(enabled), key=f"enabled_{site_id}"
                )

                # Delete button
                delete_btn = cols[3].button("🗑️ Delete", key=f"delete_{site_id}")

                # Handle enabled toggle change
                if enabled_box != bool(enabled):
                    with closing(get_conn()) as conn:
                        c = conn.cursor()
                        c.execute(
                            "UPDATE sites SET enabled = ? WHERE id = ?",
                            (int(enabled_box), site_id),
                        )
                        conn.commit()
                    st.toast(f"Updated enabled state for {url}")

                # Handle delete
                if delete_btn:
                    with closing(get_conn()) as conn:
                        c = conn.cursor()
                        c.execute("DELETE FROM checks WHERE site_id = ?", (site_id,))
                        c.execute("DELETE FROM sites WHERE id = ?", (site_id,))
                        conn.commit()
                    st.toast(f"Deleted {url}")
                    st.rerun()

        st.divider()

    # ------------- Manual run -------------
    st.header("Manual check")

    if st.button("Run checks now"):
        results = run_all_checks()
        changed = [r for r in results if r[2]]
        errors = [r for r in results if r[3]]

        st.success(f"Completed checks for {len(results)} site(s).")
        if changed:
            st.info(f"{len(changed)} site(s) changed. Telegram notifications sent (if configured).")
        if errors:
            st.error(f"{len(errors)} site(s) had errors. See details under each site card / logs.")

#     # ------------- Cron URL info -------------
#     st.header("Scheduler setup (cron-job.org)")

#     st.markdown(
#         """
# To make checks run every 30 minutes even when you're offline:

# 1. Deploy this app to **Streamlit Community Cloud**.
# 2. Copy your app URL, e.g. `https://your-username-your-repo.streamlit.app/`
# 3. Set a secret `CRON_KEY` in Streamlit secrets (some long random string).
# 4. Go to **cron-job.org** and create a job:
#    - Target URL:  
#      `https://your-username-your-repo.streamlit.app/?cron=1&key=YOUR_CRON_KEY`
#    - Schedule: every 30 minutes
# 5. When cron runs, this app will:
#    - Fetch all enabled sites
#    - Detect changes
#    - Send Telegram alerts if anything changed
# """
#     )


# =========================================================
# MAIN ENTRY
# =========================================================
def main():
    init_db()

    if is_cron_request():
        # Cron mode: no full UI, just run checks and return summary
        render_cron_page()
    else:
        # Normal interactive dashboard
        render_dashboard()


if __name__ == "__main__":
    main()
