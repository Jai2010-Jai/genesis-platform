"""Outbound SMS copy. Kept short and in plain ASCII so each message stays within 1-3 GSM-7 segments
(160 characters each); non-GSM characters would force UCS-2 at 70 characters per segment."""

from __future__ import annotations

import unicodedata

MAX_CHARS = 450  # three concatenated GSM-7 segments (153 chars each)
MAX_CHARS_CHAT = 1500  # WhatsApp and the web chat have no segment pricing

HELP = ("Just talk to me normally (text or voice note):\n"
        "- Describe an app to build it\n"
        "- Ask for changes: \"make the buttons bigger\"\n"
        "- \"how's it going?\" for progress\n"
        "- \"send the link\" to download\n"
        "- \"show my apps\" / \"switch to 2\"\n"
        "- \"cancel it\" or \"try again\"\n"
        "Say \"new app: <idea>\" to start a separate app.")

# Stage -> (progress key, text). Only "major" stages are sent with GENESIS_SMS_PROGRESS=major.
PROGRESS = {
    "generating": ("generating", True, "Understanding your idea and writing the app's code."),
    "validating": ("validating", False, "Checking the generated code for safety."),
    "building": ("building", True, "Compiling your app."),
    "repairing": ("repairing", True, "Found a build error. Fixing it automatically."),
    "verifying": ("verifying", False, "Testing the finished app package."),
}
WEB_PROGRESS = {
    "generating": ("generating", True, "Researching a design direction and writing your website."),
    "validating": ("validating", False, "Checking the generated website for safety."),
    "verifying": ("verifying", False, "Preparing your private preview link."),
}


def ascii_safe(text: str) -> str:
    """Replace characters outside GSM-7 (smart quotes, emoji) so messages don't switch to UCS-2."""
    text = (text.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
            .replace("–", "-").replace("—", "-").replace("…", "..."))
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if ch == "\n" or 32 <= ord(ch) < 127)


def fit(text: str, limit: int = MAX_CHARS) -> str:
    text = ascii_safe(text).strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def duration(hours: int) -> str:
    if hours % 24 == 0:
        days = hours // 24
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{hours} hours"


def name_of(project: dict | None) -> str:
    if not project:
        return "your app"
    name = ascii_safe(project.get("name") or "").strip().rstrip(".")
    if name:
        return name
    if project.get("kind") == "website":
        return "your website"
    return "your app"


def failure_reason(error: str | None) -> str:
    """A short, truthful summary of a Genesis build error for SMS (full details stay in the build log)."""
    e = (error or "").lower()
    if "cancelled" in e:
        return "It was cancelled."
    if "quota" in e or "resource_exhausted" in e or "429" in e:
        return "The AI service is out of quota right now."
    if "temporarily unavailable" in e:
        return "The AI service is temporarily unavailable."
    if "gemini_api_key" in e or "api key" in e:
        return "The AI service isn't configured on the Genesis server."
    if "prerequisites missing" in e:
        return "The build machine is missing part of its Android toolchain."
    if "failed validation" in e:
        return "The generated code didn't pass Genesis safety checks."
    if "repair attempt" in e or "automatic repair failed" in e:
        return "The app didn't compile, even after automatic fixes."
    if "timed out" in e:
        return "The build took too long and was stopped."
    if "outside generated code" in e:
        return "The Android toolchain failed (not your app's code)."
    if "verification failed" in e:
        return "The app package failed verification."
    if "interrupted" in e:
        return "The Genesis server restarted during the build."
    if "could not design the website" in e:
        return "The AI service is busy right now, so please wait a minute."
    if "website builder unavailable" in e:
        return "Website building isn't set up on the Genesis server."
    if "did not apply the change" in e:
        return "The AI didn't apply the change to the website."
    if "website generation failed" in e or "did not produce the website" in e:
        return "The AI couldn't generate the website."
    if "website generation timed out" in e:
        return "Generating the website took too long and was stopped."
    if "netlify" in e:
        return "Netlify didn't accept the publish."
    if "sources of the previous build" in e:
        return "The previous version's code is no longer available."
    return "The build failed."
