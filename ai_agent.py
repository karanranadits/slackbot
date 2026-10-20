"""
ai_agent.py — Modular AI provider for the DSR Slack Bot.

Switch providers by setting in .env:
    AI_PROVIDER=gemini   (default)
    AI_PROVIDER=openai
    AI_PROVIDER=claude

    AI_MODEL=gemini-2.5-flash   (or gpt-4o, claude-3-5-sonnet-20241022, etc.)
"""

import os
from pathlib import Path
import json
import pathlib
import tempfile
from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from db import UserDB

load_dotenv()

AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").lower()
AI_MODEL = os.getenv("AI_MODEL", "gemini-2.5-flash")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


# ── Provider factory ────────────────────────────────────────────────────────

def _build_chat_model():
    """Return a LangChain chat model based on AI_PROVIDER env var."""
    if AI_PROVIDER == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=AI_MODEL,
            google_api_key=GEMINI_API_KEY,
            temperature=0.7,
        )

    elif AI_PROVIDER == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=AI_MODEL or "gpt-4o",
            api_key=OPENAI_API_KEY,
            temperature=0.7,
        )

    elif AI_PROVIDER == "claude":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=AI_MODEL or "claude-3-5-sonnet-20241022",
            api_key=ANTHROPIC_API_KEY,
            temperature=0.7,
        )

    elif AI_PROVIDER == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(
            model=AI_MODEL or "llama-3.3-70b-versatile",
            api_key=GROQ_API_KEY,
            temperature=0.7,
        )

    else:
        raise ValueError(
            f"Unknown AI_PROVIDER '{AI_PROVIDER}'. "
            "Choose one of: gemini, openai, claude, groq"
        )


# ── System prompt template ──────────────────────────────────────────────────

SYSTEM_PROMPT_TEMPLATE = (
    Path(__file__).parent / "prompts" / "dsr_system_prompt.md"
).read_text(encoding="utf-8")


# ── Main DSR Agent class ────────────────────────────────────────────────────

class DSRAgent:
    """
    Provider-agnostic DSR conversation agent.
    Uses LangChain's unified BaseChatModel interface so the same code
    works with Gemini, OpenAI, or Claude — just change AI_PROVIDER in .env.

    Audio transcription always uses the raw Gemini Files API (best audio support).
    """

    def __init__(self, db: UserDB):
        self._system_prompt_template = SYSTEM_PROMPT_TEMPLATE

        self._model = _build_chat_model()
        self._db = db
        print(f"AI provider: {AI_PROVIDER.upper()} | model: {AI_MODEL}")

        # Tracks whether the system context injection has been done for the current
        # DSR session. Keyed by user_id; cleared when session resets.
        self._session_context_injected: set[str] = set()

    def _get_history(self, user_id: str) -> list:
        """Load history from SQLite and reconstruct LangChain message objects."""
        raw = self._db.get_history(user_id)
        messages = []
        for role, content in raw:
            if role == "human":
                messages.append(HumanMessage(content=content))
            elif role == "ai":
                messages.append(AIMessage(content=content))
            elif role == "system":
                messages.append(SystemMessage(content=content))
        return messages

    def reset_session(self, user_id: str) -> None:
        """Clear a user's conversation history after DSR is submitted."""
        self._db.clear_history(user_id)
        self._session_context_injected.discard(user_id)

    def chat(self, user_id: str, message: str, categories: list, already_logged_minutes: int = 0, projects: list = None) -> tuple[str, dict | None]:
        """
        Send a message and get a response.

        Returns:
            (reply_text, dsr_data)
            dsr_data is the parsed tasks dict once all 8h are confirmed, else None.
        """
        history = self._get_history(user_id)
        is_new_session = user_id not in self._session_context_injected

        cat_list = "\n".join([f"- {c['display_value']} (id: {c['id']})" for c in categories])
        proj_list = "\n".join([f"- {p['project_name']} (id: {p['project_id']})" for p in (projects or [])])
        system_prompt = (
            self._system_prompt_template
            .replace("CATEGORIES_PLACEHOLDER", cat_list or "No categories available")
            .replace("PROJECTS_PLACEHOLDER", proj_list or "No projects available")
        )

        # Build the full message list: system + history + new user message
        messages = [SystemMessage(content=system_prompt)]
        
        # Inject dynamic context about remaining hours at the start of a session
        if is_new_session:
            remaining_minutes = max(0, 480 - already_logged_minutes)
            context = (
                f"[SYSTEM CONTEXT: The user has already logged {already_logged_minutes} minutes today. "
                f"You ONLY need to collect {remaining_minutes} more minutes to reach the 480 minute (8 hour) goal. "
                f"If the remaining minutes is 0, just tell them they are done for the day and output DSR_READY with empty tasks. "
                f"Do not let the total of new tasks exceed {remaining_minutes} minutes.]"
            )
            # Persist the context message so it's available in future sessions from DB
            self._db.save_message(user_id, "system", context)
            self._session_context_injected.add(user_id)
            # Reload history with the context now included
            history = self._get_history(user_id)

        messages.extend(history)
        messages.append(HumanMessage(content=message))

        response = self._model.invoke(messages)
        reply = response.content

        # Persist to DB
        self._db.save_message(user_id, "human", message)
        self._db.save_message(user_id, "ai", reply)

        # Check if DSR is ready
        dsr_data = self._extract_dsr(reply)
        if dsr_data:
            clean_reply = reply[:reply.find("<DSR_READY>")].strip()
            return clean_reply, dsr_data

        return reply, None

    # ── Audio transcription ────────────────────────────────────────────────

    def transcribe_audio(self, audio_bytes: bytes, mime_type: str = "audio/webm") -> str:
        """
        Transcribe a Slack voice note.
        - openai / claude / groq provider -> Groq Whisper API (100% free)
        - gemini provider                 -> Gemini Files API
        """
        if AI_PROVIDER in ("openai", "claude", "groq"):
            return self._transcribe_groq(audio_bytes, mime_type)
        return self._transcribe_gemini(audio_bytes, mime_type)

    def _transcribe_groq(self, audio_bytes: bytes, mime_type: str) -> str:
        """Transcribe using Groq Whisper API (100% free fast inference)."""
        import io
        from groq import Groq

        ext_map = {
            "audio/webm": "audio.webm", "audio/mp4": "audio.mp4",
            "audio/mpeg": "audio.mp3", "audio/ogg": "audio.ogg",
            "audio/wav": "audio.wav", "audio/x-m4a": "audio.m4a",
            "video/mp4": "audio.mp4",
        }
        filename = ext_map.get(mime_type, "audio.webm")

        try:
            print(f"Transcribing audio ({len(audio_bytes)} bytes) via Groq API...")
            client = Groq(api_key=GROQ_API_KEY)
            result = client.audio.transcriptions.create(
                file=(filename, io.BytesIO(audio_bytes), mime_type),
                model="whisper-large-v3",
                response_format="text"
            )
            transcript = result.strip() if isinstance(result, str) else result.text.strip()
            print(f"Transcript: {transcript}")
            return transcript
        except Exception as e:
            print(f"Groq transcription error: {e}")
            return ""

    def _transcribe_gemini(self, audio_bytes: bytes, mime_type: str) -> str:
        """Transcribe using Gemini Files API."""
        from google import genai
        from google.genai import types as gtypes

        ext_map = {
            "audio/webm": ".webm", "audio/mp4": ".mp4", "audio/mpeg": ".mp3",
            "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/x-m4a": ".m4a",
            "video/mp4": ".mp4",
        }
        ext = ext_map.get(mime_type, ".webm")
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        try:
            print(f"Transcribing audio ({len(audio_bytes)} bytes) via Gemini...")
            g_client = genai.Client(api_key=GEMINI_API_KEY)
            uploaded = g_client.files.upload(
                file=pathlib.Path(tmp_path),
                config=gtypes.UploadFileConfig(mime_type=mime_type)
            )
            response = g_client.models.generate_content(
                model="gemini-2.5-flash",
                config=gtypes.GenerateContentConfig(temperature=0.0),
                contents=[gtypes.Content(parts=[
                    gtypes.Part(file_data=gtypes.FileData(file_uri=uploaded.uri, mime_type=mime_type)),
                    gtypes.Part(text="Please transcribe this audio exactly as spoken. Return only the transcript text, nothing else.")
                ])]
            )
            transcript = response.text.strip()
            print(f"Transcript: {transcript}")
            return transcript
        except Exception as e:
            print(f"Gemini transcription error: {e}")
            return ""
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    # ── Private helpers ────────────────────────────────────────────────────

    def _extract_dsr(self, text: str) -> dict | None:
        """Extract and parse the DSR_READY JSON block if present."""
        if "<DSR_READY>" not in text or "</DSR_READY>" not in text:
            return None
        try:
            start = text.index("<DSR_READY>") + len("<DSR_READY>")
            end = text.index("</DSR_READY>")
            return json.loads(text[start:end].strip())
        except Exception as e:
            print(f"Failed to parse DSR JSON: {e}")
            return None
