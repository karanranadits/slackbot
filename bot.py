import os
import re
import logging
import requests as http_requests
from dotenv import load_dotenv
from slack_bolt import App

from portal_service import PortalService
from ai_agent import DSRAgent
from db import UserDB

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── 1. Initialise SQLite DB ──────────────────────────────────────────────────
db = UserDB()

# ── 2. Initialise Gemini agent ───────────────────────────────────────────────
agent = DSRAgent(db=db)

# ── 3. Initialise Slack Bolt ─────────────────────────────────────────────────
SLACK_BOT_TOKEN    = os.getenv("SLACK_BOT_TOKEN")
SLACK_DSR_CHANNEL  = os.getenv("SLACK_DSR_CHANNEL_ID", "")
app = App(token=SLACK_BOT_TOKEN)

# Mime types that are audio/voice notes from Slack
AUDIO_MIME_TYPES = {
    "audio/webm", "audio/mp4", "audio/mpeg",
    "audio/ogg", "audio/wav", "audio/x-m4a", "video/mp4"
}
AUDIO_FILE_TYPES = {"webm", "mp4", "m4a", "mp3", "ogg", "wav"}

# ── 4. Per-user PortalService pool ───────────────────────────────────────────
# Maps slack_user_id → PortalService (already logged in)
_portals: dict[str, PortalService] = {}

# Maps slack_user_id → list of project dicts
_user_projects: dict[str, list] = {}

# Globally cached categories (fetched once)
_cached_categories: list = []


def get_portal(slack_user_id: str) -> PortalService | None:
    """Return (or create and login) a PortalService for a registered user."""
    if slack_user_id in _portals:
        return _portals[slack_user_id]

    user = db.get_user(slack_user_id)
    if not user:
        return None

    ps = PortalService(email=user["portal_email"], password=user["portal_password"])
    if not ps.login():
        log.error(f"Portal login failed for user {slack_user_id} ({user['portal_email']})")
        return None

    _portals[slack_user_id] = ps
    return ps


# ── 7. Onboarding state machine ──────────────────────────────────────────────
# States per user while they are in the DM onboarding flow
# { slack_user_id: { "state": "...", "name": "...", "email": "..." } }
_onboarding: dict[str, dict] = {}

ONBOARDING_STATES = {"PENDING_NAME", "PENDING_EMAIL", "PENDING_PASSWORD"}


def _clean_slack_text(text: str) -> str:
    """
    Strip Slack's auto-formatting from user input.

    Slack converts emails and URLs in messages to hyperlink markup:
      email → <mailto:user@domain.com|user@domain.com>
      url   → <https://example.com|example.com>

    This function extracts the plain value from such markup so we
    can use it as a raw string (e.g. for portal login).
    """
    text = text.strip()
    # <mailto:email|label>  → email
    text = re.sub(r'<mailto:([^|>]+)\|[^>]*>', r'\1', text)
    # <url|label> or <url>  → url (catch-all for any remaining angle brackets)
    text = re.sub(r'<([^|>]+)\|[^>]*>', r'\1', text)
    text = re.sub(r'<([^>]+)>', r'\1', text)
    return text.strip()


def start_onboarding(client, slack_user_id: str, group_channel: str):
    """Announce in group, then DM the new user to collect credentials."""
    # Announce in the group channel
    try:
        client.chat_postMessage(
            channel=group_channel,
            text=(
                f"👋 Hey <@{slack_user_id}>! I don't recognise you yet. "
                "I've sent you a DM to get you set up — just takes a minute! 🔐"
            )
        )
    except Exception as e:
        log.error(f"Failed to post group onboarding notice: {e}")

    # Kick off the DM flow
    _onboarding[slack_user_id] = {"state": "PENDING_NAME"}
    try:
        client.chat_postMessage(
            channel=slack_user_id,  # Slack opens a DM when you post to a user ID
            text=(
                "Hi there! 👋 I'm the DSR bot. Before you can use me in the group, "
                "I need to set you up.\n\n"
                "*What's your name?* (This is how I'll refer to you in reports)"
            )
        )
    except Exception as e:
        log.error(f"Failed to send onboarding DM to {slack_user_id}: {e}")


def handle_onboarding_dm(client, slack_user_id: str, text: str) -> bool:
    """
    Handle a DM message that is part of the onboarding flow.
    Returns True if the message was consumed by onboarding, False otherwise.
    """
    state_data = _onboarding.get(slack_user_id)
    if not state_data:
        return False

    state = state_data["state"]

    if state == "PENDING_NAME":
        name = text.strip()
        if not name:
            client.chat_postMessage(channel=slack_user_id, text="Please enter your name:")
            return True
        state_data["name"] = name
        state_data["state"] = "PENDING_EMAIL"
        client.chat_postMessage(
            channel=slack_user_id,
            text=f"Nice to meet you, *{name}*! 🙌\n\nNow, please enter your *portal email address*:"
        )
        return True

    if state == "PENDING_EMAIL":
        # Strip Slack's auto-link formatting: <mailto:email|email> → email
        email = _clean_slack_text(text)
        if "@" not in email or "." not in email.split("@")[-1]:
            client.chat_postMessage(
                channel=slack_user_id,
                text="That doesn't look like a valid email address. Please enter it in the format: *you@company.com*"
            )
            return True
        log.info(f"Onboarding email captured for {slack_user_id}: {email}")
        state_data["email"] = email
        state_data["state"] = "PENDING_PASSWORD"
        state_data["retry_count"] = 0
        client.chat_postMessage(
            channel=slack_user_id,
            text=f"Got it! Now please enter your *portal password*: 🔑"
        )
        return True

    if state == "PENDING_PASSWORD":
        password = text.strip()
        if not password:
            client.chat_postMessage(channel=slack_user_id, text="Password can't be empty. Please try again:")
            return True

        name  = state_data["name"]
        email = state_data["email"]
        retry_count = state_data.get("retry_count", 0)

        # Verify credentials immediately
        client.chat_postMessage(
            channel=slack_user_id,
            text="⏳ Verifying your credentials with the portal..."
        )
        ps = PortalService(email=email, password=password)
        if not ps.login():
            retry_count += 1
            state_data["retry_count"] = retry_count
            # Keep the email — only ask for password again
            # After 3 failed attempts, also let them re-enter the email
            if retry_count >= 3:
                state_data["state"] = "PENDING_EMAIL"
                state_data.pop("email", None)
                state_data["retry_count"] = 0
                client.chat_postMessage(
                    channel=slack_user_id,
                    text=(
                        "❌ That password didn't work again. Let's start fresh.\n\n"
                        "Please re-enter your *portal email address*:"
                    )
                )
            else:
                client.chat_postMessage(
                    channel=slack_user_id,
                    text=(
                        f"❌ That password didn't work for *{email}*. Please check it and try again:\n\n"
                        "Enter your *portal password*: 🔑"
                    )
                )
            return True

        # Success — register and cache portal
        db.register_user(
            slack_user_id=slack_user_id,
            display_name=name,
            portal_email=email,
            portal_password=password,
        )
        _portals[slack_user_id] = ps
        _onboarding.pop(slack_user_id, None)

        client.chat_postMessage(
            channel=slack_user_id,
            text=(
                f"✅ You're all set, *{name}*! Your credentials are verified and saved.\n\n"
                "You can now head back to the group chat and start using the DSR bot! 🎉"
            )
        )

        # Notify the group
        if SLACK_DSR_CHANNEL:
            try:
                client.chat_postMessage(
                    channel=SLACK_DSR_CHANNEL,
                    text=f"✅ <@{slack_user_id}> (*{name}*) has been registered and is ready to log DSRs!"
                )
            except Exception as e:
                log.error(f"Failed to post group registration notice: {e}")

        return True

    return False


# ── 8. Helper utilities ──────────────────────────────────────────────────────

def post(client, channel_id: str, text: str):
    """Post a message using chat_postMessage directly."""
    try:
        client.chat_postMessage(channel=channel_id, text=text)
    except Exception as e:
        log.error(f"Failed to post message: {e}")


def download_slack_file(url: str) -> bytes | None:
    """Download a Slack file using bot token for auth."""
    try:
        resp = http_requests.get(
            url,
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            timeout=30
        )
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        log.error(f"Failed to download Slack file: {e}")
        return None


def get_display_name(user_record: dict | None, slack_user_id: str) -> str:
    """Return a user's display name, falling back to their Slack mention."""
    if user_record and user_record.get("display_name"):
        return user_record["display_name"]
    return f"<@{slack_user_id}>"


# ── 9. Core DSR processing ───────────────────────────────────────────────────

def process_and_reply(user_id: str, text: str, client, channel_id: str):
    """Run the Gemini DSR flow for a given text message and send reply."""
    portal = get_portal(user_id)
    if not portal:
        post(client, channel_id, "❌ I couldn't connect to the portal with your credentials. Please DM me to re-register.")
        return

    is_new_session = user_id not in agent._session_context_injected
    logged_minutes = 0
    if is_new_session:
        logged_minutes = portal.get_todays_logged_minutes()
        _user_projects[user_id] = portal.get_projects()
        log.info(f"User {user_id} has {logged_minutes} minutes already logged today.")

    global _cached_categories
    if not _cached_categories:
        _cached_categories = portal.get_categories()
        log.info(f"Loaded {len(_cached_categories)} activity categories")

    user_projects = _user_projects.get(user_id, [])
    user_record = db.get_user(user_id)
    display_name = get_display_name(user_record, user_id)

    try:
        reply, dsr_data = agent.chat(
            user_id=user_id,
            message=text,
            categories=_cached_categories,
            already_logged_minutes=logged_minutes,
            projects=user_projects
        )
    except Exception as e:
        log.error(f"AI agent error: {e}")
        post(client, channel_id, f"Sorry, I hit an error: {e}. Please try again.")
        return

    # If DSR is complete → submit all entries to portal
    if dsr_data:
        tasks = dsr_data.get("tasks", [])

        if not tasks:
            if reply:
                post(client, channel_id, reply)
            post(client, channel_id, f"✅ {display_name}, you have already met your 8-hour goal for today. No new entries were submitted.")
            agent.reset_session(user_id)
            return

        total_minutes = dsr_data.get("total_minutes", 0)

        summary_lines = [f"*Here's your final DSR summary, {display_name}:*\n"]
        for i, t in enumerate(tasks, 1):
            hours = t["duration_minutes"] // 60
            mins  = t["duration_minutes"] % 60
            duration_str = f"{hours}h {mins}m" if mins else f"{hours}h"
            proj = f" | Project: {t.get('project_name', 'No project')}" if t.get("project_name") else ""
            summary_lines.append(
                f"*{i}. {t['title']}*\n"
                f"   Category: {t['category_display']} | Time: {duration_str}{proj}\n"
                f"   Notes: {t.get('notes', '')} "
            )
        total_h = total_minutes // 60
        total_m = total_minutes % 60
        total_str = f"{total_h}h {total_m}m" if total_m else f"{total_h}h"
        summary_lines.append(f"\n*Total: {total_str}*")
        summary_lines.append("\nSubmitting to the portal now...")

        if reply:
            post(client, channel_id, reply)
        post(client, channel_id, "\n".join(summary_lines))

        log.info(f"Submitting {len(tasks)} entries to portal for user {user_id} ({display_name})")
        success_count, fail_count = portal.submit_all_entries(tasks)
        log.info(f"Submission result: {success_count} success, {fail_count} failed")

        if fail_count == 0:
            post(client, channel_id,
                f"DSR submitted successfully! All {success_count} "
                f"entr{'y' if success_count == 1 else 'ies'} logged on the portal. "
                f"Your timesheet is complete for today, {display_name}! Have a great evening! 🎉"
            )
        else:
            post(client, channel_id,
                f"Partial submission: {success_count} submitted, "
                f"{fail_count} failed. Please check the portal manually for the failed ones."
            )

        agent.reset_session(user_id)
        return

    # Normal conversational reply
    if reply:
        post(client, channel_id, reply)


# ── 10. Slack event handlers ─────────────────────────────────────────────────

@app.event("message")
def handle_message(event, client):
    """Handle text messages AND voice notes (file uploads)."""
    # Ignore bot messages
    if event.get("bot_id"):
        return
    subtype = event.get("subtype", "")
    if subtype and subtype not in ("file_share",):
        return

    user_id    = event.get("user")
    channel_id = event.get("channel")
    if not user_id or not channel_id:
        return

    # Determine if this is a DM (channel type "im") or a group channel
    channel_type = event.get("channel_type", "")
    is_dm = channel_type == "im"

    # ── DM messages: could be onboarding or normal conversation ──────────
    if is_dm:
        # Check if this user is mid-onboarding
        if user_id in _onboarding:
            text = (event.get("text") or "").strip()
            handle_onboarding_dm(client, user_id, text)
            return

        # Registered user in DM — route to DSR flow
        if db.is_registered(user_id):
            files = event.get("files", [])
            for file_info in files:
                if _handle_voice_note(file_info, user_id, channel_id, client):
                    return
            text = (event.get("text") or "").strip()
            if text:
                log.info(f"DM from {user_id}: {text}")
                process_and_reply(user_id=user_id, text=text, client=client, channel_id=channel_id)
        return

    # ── Group channel messages ────────────────────────────────────────────
    # If user is in onboarding, remind them to finish via DM
    if user_id in _onboarding:
        post(client, channel_id,
             f"<@{user_id}> Please finish setting up your account in our DM first! 📬")
        return

    # If user is not registered, start onboarding
    if not db.is_registered(user_id):
        start_onboarding(client, user_id, channel_id)
        return

    # Registered user in group channel — process normally
    files = event.get("files", [])
    for file_info in files:
        if _handle_voice_note(file_info, user_id, channel_id, client):
            return

    text = (event.get("text") or "").strip()
    if not text:
        return

    log.info(f"Group message from {user_id}: {text}")
    process_and_reply(user_id=user_id, text=text, client=client, channel_id=channel_id)


def _handle_voice_note(file_info: dict, user_id: str, channel_id: str, client) -> bool:
    """
    Process a voice/audio file if applicable.
    Returns True if the file was handled (and the caller should stop), False otherwise.
    """
    filetype = file_info.get("filetype", "").lower()
    mimetype = file_info.get("mimetype", "").lower()

    is_audio = (mimetype in AUDIO_MIME_TYPES) or (filetype in AUDIO_FILE_TYPES)
    is_voice = (
        file_info.get("subtype") == "slack_audio"
        or "voice" in file_info.get("title", "").lower()
        or "voice" in file_info.get("name", "").lower()
        or is_audio
    )

    if not (is_voice or is_audio):
        return False

    post(client, channel_id, "Got your voice note! Transcribing it now... 🎙️")
    log.info(f"Voice note from {user_id}: {file_info.get('name')} ({mimetype})")

    download_url = file_info.get("url_private_download") or file_info.get("url_private")
    if not download_url:
        post(client, channel_id, "I couldn't access the audio file. Please try sending a text message.")
        return True

    audio_bytes = download_slack_file(download_url)
    if not audio_bytes:
        post(client, channel_id, "Failed to download the voice note. Please try again.")
        return True

    transcript = agent.transcribe_audio(audio_bytes, mime_type=mimetype or "audio/webm")
    if not transcript:
        post(client, channel_id, "I couldn't transcribe the voice note. Could you type it instead?")
        return True

    post(client, channel_id, f"I heard: \"{transcript}\"\n")
    log.info(f"Transcript for {user_id}: {transcript}")

    process_and_reply(user_id=user_id, text=transcript, client=client, channel_id=channel_id)
    return True


@app.error
def custom_error_handler(error, body, logger):
    logger.exception(f"Error: {error}")
    logger.info(f"Request body: {body}")


# ── 11. Automated Reminders ──────────────────────────────────────────────────

def check_and_remind_dsr():
    """
    Runs at 8 PM IST on weekdays.
    Fetches every registered user's logged minutes and posts a single group
    message @mentioning everyone who still needs to log time.
    """
    if not SLACK_DSR_CHANNEL:
        log.error("SLACK_DSR_CHANNEL_ID not set. Cannot send DSR reminder.")
        return

    all_users = db.get_all_users()
    if not all_users:
        log.info("No registered users found. Skipping DSR reminder.")
        return

    needs_reminder = []   # (slack_user_id, display_name, logged_minutes)
    already_done   = []   # (display_name,)

    for user in all_users:
        uid   = user["slack_user_id"]
        name  = user["display_name"]
        portal = get_portal(uid)
        if not portal:
            log.warning(f"Could not get portal for {uid} ({name}). Skipping reminder check.")
            continue

        logged = portal.get_todays_logged_minutes()
        log.info(f"Reminder check: {name} ({uid}) has {logged} min logged today.")

        if logged >= 480:
            already_done.append(name)
        else:
            needs_reminder.append((uid, name, logged))

    if not needs_reminder:
        log.info("All users have completed their DSR. No reminder needed.")
        return

    # Build a single group message
    lines = ["👋 *DSR Reminder — 8 PM Check-in!*\n"]
    lines.append("The following team members still need to complete their DSR today:\n")

    for uid, name, logged in needs_reminder:
        hours   = logged // 60
        mins    = logged % 60
        remaining = 480 - logged
        r_hours = remaining // 60
        r_mins  = remaining % 60

        logged_str    = f"{hours}h {mins}m" if mins else f"{hours}h" if hours else "nothing yet"
        remaining_str = f"{r_hours}h {r_mins}m" if r_mins else f"{r_hours}h"

        lines.append(
            f"• <@{uid}> (*{name}*) — logged *{logged_str}*, still needs *{remaining_str}* more"
        )

    lines.append("\nPlease reply here to log your remaining time! 🕐")

    message = "\n".join(lines)
    log.info(f"Sending group DSR reminder to {SLACK_DSR_CHANNEL} for {len(needs_reminder)} user(s)...")
    try:
        app.client.chat_postMessage(channel=SLACK_DSR_CHANNEL, text=message)
    except Exception as e:
        log.error(f"Failed to send group reminder: {e}")


# ── 12. Exports for main.py ────────────────────────────────────────────
# main.py imports these to wire up FastAPI lifespan and the /slack/events route.
__all__ = ["app", "db", "agent", "check_and_remind_dsr"]
