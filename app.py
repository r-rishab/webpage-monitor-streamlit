import streamlit as st
import sqlite3
import requests
import hashlib
from datetime import datetime
from contextlib import closing
from bs4 import BeautifulSoup

# =========================================================
# CONFIG FROM SECRETS
# Set these in Streamlit Cloud "Secrets"
# =========================================================
TELEGRAM_BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "")
CRON_KEY = st.secrets.get("CRON_KEY", "")  # shared secret for cron-job.org calls

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


# =========================================================
# CORE MONITORING LOGIC
# =========================================================
def normalize_html(html: str) -> str:
    """Extract main text and normalize whitespace."""
    soup = BeautifulSoup(html, "html.parser")

    # You can tweak this if you want to ignore headers/footers or ads.
    text = soup.get_text(separator=" ", strip=True)
    # Normalize spaces
    return " ".join(text.split())


def compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def send_telegram_message(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        # Telegram not configured; just skip
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        # Optional: log somewhere; for now just print to server log
        print("Error sending Telegram message:", e)


def fetch_page(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=20)
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
            # First time: just store hash, don't notify
            changed = False
        else:
            changed = (new_hash != last_hash)

        # Update DB with new hash and check record
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
        # Log the error in checks table
        with closing(get_conn()) as conn:
            c = conn.cursor()
            c.execute(
                """
                INSERT INTO checks (site_id, checked_at, changed, error)
                VALUES (?, ?, ?, ?)
                """,
                (site_id, datetime.utcnow().isoformat(), 0, str(e)),
            )
            conn.commit()

        return False, str(e), None


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
def is_cron_request():
    # For newer Streamlit:
    params = getattr(st, "query_params", None)
    if params is None:
        # Backward compatibility
        params = st.experimental_get_query_params()
    # Streamlit returns lists for query params
    cron_vals = params.get("cron", [])
    key_vals = params.get("key", [])

    is_cron = "1" in cron_vals or "true" in cron_vals
    key_ok = (not CRON_KEY) or (CRON_KEY in key_vals)
    return is_cron and key_ok


def render_cron_page():
    st.write("Running scheduled checks...")

    results = run_all_checks()
    changed_count = sum(1 for _, _, changed, _ in results if changed)
    error_count = sum(1 for _, _, _, err in results if err is not None)

    st.write(f"Done. Changed: {changed_count}, Errors: {error_count}")


def render_dashboard():
    st.set_page_config(page_title="Website Change Notifier", layout="wide")
    st.title("🔍 Website Change Notifier")

    st.markdown(
        """
This app monitors multiple websites and sends you Telegram notifications when **any change** is detected.

**How it works:**
1. Add URLs below.
2. Deploy this app to Streamlit Cloud.
3. Configure cron-job.org to hit this app every 30 minutes.
4. When a page changes, you'll get a Telegram message.
"""
    )

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        st.warning(
            "Telegram is not configured yet. "
            "Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in Streamlit secrets."
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
                            (url.strip(), description.strip() or None, datetime.utcnow().isoformat()),
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
            with st.container(border=True):
                cols = st.columns([4, 3, 1, 1])
                cols[0].markdown(f"**{description or 'No description'}**  \n`{url}`")
                cols[1].markdown(
                    f"First added: `{created_at}`  \n"
                    + ("First check pending" if last_hash is None else "Has previous snapshot")
                )

                # Toggle enabled
                enabled_box = cols[2].checkbox("Enabled", value=bool(enabled), key=f"enabled_{site_id}")
                # Delete button
                delete_btn = cols[3].button("🗑️ Delete", key=f"delete_{site_id}")

                if enabled_box != bool(enabled):
                    with closing(get_conn()) as conn:
                        c = conn.cursor()
                        c.execute(
                            "UPDATE sites SET enabled = ? WHERE id = ?",
                            (int(enabled_box), site_id),
                        )
                        conn.commit()
                    st.toast(f"Updated enabled state for {url}")

                if delete_btn:
                    with closing(get_conn()) as conn:
                        c = conn.cursor()
                        c.execute("DELETE FROM checks WHERE site_id = ?", (site_id,))
                        c.execute("DELETE FROM sites WHERE id = ?", (site_id,))
                        conn.commit()
                    st.toast(f"Deleted {url}")
                    st.experimental_rerun()

        st.divider()

    # ------------- Manual run -------------
    st.header("Manual check")

    if st.button("Run checks now"):
        results = run_all_checks()
        changed = [r for r in results if r[2]]
        errors = [r for r in results if r[3] is not None]

        st.success(f"Completed checks for {len(results)} site(s).")
        if changed:
            st.info(f"{len(changed)} site(s) changed. Telegram notifications sent (if configured).")
        if errors:
            st.error(f"{len(errors)} site(s) had errors. See server logs for details.")

    # ------------- Cron URL info -------------
    st.header("Scheduler setup (cron-job.org)")

    # For docs, we show a placeholder URL; user should replace with their real one.
    st.markdown(
        """
1. Deploy this app to **Streamlit Community Cloud**.
2. Copy your app URL, e.g. `https://your-username-your-repo-name.streamlit.app/`
3. In cron-job.org, create a new job:
   - Target URL: `https://your-username-your-repo-name.streamlit.app/?cron=1&key=YOUR_CRON_KEY`
   - Schedule: every 30 minutes
4. Set `CRON_KEY` in Streamlit secrets to the same value (`YOUR_CRON_KEY`) for security.
"""
    )


# =========================================================
# MAIN ENTRY
# =========================================================
def main():
    init_db()

    if is_cron_request():
        render_cron_page()
    else:
        render_dashboard()


if __name__ == "__main__":
    main()
