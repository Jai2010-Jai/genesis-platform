"""Configuration and toolchain detection. Secrets are read from the environment only."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def _find_java_home() -> Path | None:
    """Locate a JDK 17+ (AGP 8.x requires 17)."""
    candidates: list[Path] = []
    env = os.environ.get("JAVA_HOME", "").strip()
    if env:
        candidates.append(Path(env))
    for base in (Path.home() / "Library/Java/JavaVirtualMachines", Path("/Library/Java/JavaVirtualMachines")):
        if base.is_dir():
            candidates.extend(sorted((p / "Contents/Home" for p in base.iterdir()), reverse=True))
    candidates += [
        Path("/usr/local/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"),
        Path("/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"),
        Path("/Applications/Android Studio.app/Contents/jbr/Contents/Home"),
    ]
    for home in candidates:
        major = java_major_version(home)
        if major is not None and major >= 17:
            return home
    return None


def java_major_version(home: Path) -> int | None:
    java = home / "bin" / "java"
    if not java.is_file():
        return None
    try:
        out = subprocess.run([str(java), "-version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'version "(\d+)(?:\.(\d+))?', out.stderr + out.stdout)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2)) if major == 1 and m.group(2) else major


def _find_android_home() -> Path | None:
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        v = os.environ.get(name, "").strip()
        if v and Path(v).is_dir():
            return Path(v)
    default = Path.home() / "Library/Android/sdk"
    return default if default.is_dir() else None


def _str_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _choice_env(name: str, default: str, choices: set[str]) -> str:
    value = _str_env(name, default).lower()
    return value if value in choices else default


def _list_env(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


@dataclass
class Settings:
    root: Path = ROOT
    template_dir: Path = ROOT / "android_template"
    data_dir: Path = ROOT / "data"
    gemini_api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", "").strip(), repr=False)
    gemini_model: str = field(default_factory=lambda: os.environ.get("GEMINI_MODEL", "auto").strip() or "auto")
    # api = Gemini API with GEMINI_API_KEY; antigravity = the Antigravity desktop app's agent (no API key).
    ai_backend: str = field(default_factory=lambda: _choice_env("GENESIS_AI_BACKEND", "api", {"api", "antigravity"}))
    antigravity_model: str = field(default_factory=lambda: _choice_env(
        "GENESIS_ANTIGRAVITY_MODEL", "app", {"app", "flash_lite", "flash", "pro"}))
    antigravity_project: str = field(default_factory=lambda: _str_env("GENESIS_ANTIGRAVITY_PROJECT", "outside-of-project")
                                     or "outside-of-project")
    antigravity_timeout: int = field(default_factory=lambda: _int_env("GENESIS_ANTIGRAVITY_TIMEOUT_SECONDS", 600, 60, 1800))
    java_home: Path | None = field(default_factory=_find_java_home)
    android_home: Path | None = field(default_factory=_find_android_home)
    build_timeout: int = field(default_factory=lambda: _int_env("GENESIS_BUILD_TIMEOUT_SECONDS", 900, 60, 3600))
    max_repairs: int = field(default_factory=lambda: _int_env("GENESIS_MAX_REPAIR_ATTEMPTS", 2, 0, 2))
    max_concurrent_builds: int = field(default_factory=lambda: _int_env("GENESIS_MAX_CONCURRENT_BUILDS", 1, 1, 4))
    max_queued_builds: int = field(default_factory=lambda: _int_env("GENESIS_MAX_QUEUED_BUILDS", 5, 1, 50))
    host: str = field(default_factory=lambda: os.environ.get("GENESIS_HOST", "127.0.0.1").strip() or "127.0.0.1")
    port: int = field(default_factory=lambda: _int_env("GENESIS_PORT", 8765, 1024, 65535))

    # ---- SMS (Twilio). Secrets are server-side only and never logged or returned by the API. ----
    sms_provider: str = field(default_factory=lambda: _choice_env("GENESIS_SMS_PROVIDER", "twilio", {"twilio", "log", "off"}))
    twilio_account_sid: str = field(default_factory=lambda: _str_env("TWILIO_ACCOUNT_SID"))
    twilio_auth_token: str = field(default_factory=lambda: _str_env("TWILIO_AUTH_TOKEN"), repr=False)
    twilio_phone_number: str = field(default_factory=lambda: _str_env("TWILIO_PHONE_NUMBER"))
    twilio_messaging_service_sid: str = field(default_factory=lambda: _str_env("TWILIO_MESSAGING_SERVICE_SID"))
    # sms, or whatsapp (Twilio API for WhatsApp; same webhooks, addresses prefixed with 'whatsapp:').
    twilio_channel: str = field(default_factory=lambda: _choice_env("TWILIO_CHANNEL", "sms", {"sms", "whatsapp"}))
    # WhatsApp sender in E.164 (the Sandbox is +14155238886). Replies go back on the channel the user wrote in.
    twilio_whatsapp_number: str = field(default_factory=lambda: _str_env("TWILIO_WHATSAPP_NUMBER"))
    # Optional Content template (HX...) whose whole body is {{1}}: WhatsApp replies are sent through it, for senders
    # that refuse free-form Body (error 21654). Create it with `python -m genesis sms-whatsapp-template`.
    twilio_whatsapp_content_sid: str = field(default_factory=lambda: _str_env("TWILIO_WHATSAPP_CONTENT_SID"))
    twilio_region: str = field(default_factory=lambda: _str_env("TWILIO_REGION"))
    twilio_edge: str = field(default_factory=lambda: _str_env("TWILIO_EDGE"))
    # Externally visible https origin that Twilio calls (used for signature validation and download links).
    public_base_url: str = field(default_factory=lambda: _str_env("PUBLIC_BASE_URL").rstrip("/"))
    # closed: only GENESIS_SMS_OWNER_NUMBERS; link: others must text LINK <one-time code>; open: anyone gets an isolated guest account.
    sms_signup: str = field(default_factory=lambda: _choice_env("GENESIS_SMS_SIGNUP", "link", {"closed", "link", "open"}))
    sms_owner_numbers: list[str] = field(default_factory=lambda: _list_env("GENESIS_SMS_OWNER_NUMBERS"))
    sms_default_country_code: str = field(default_factory=lambda: _str_env("GENESIS_SMS_DEFAULT_COUNTRY_CODE", "+1"))
    sms_worker: str = field(default_factory=lambda: _choice_env("GENESIS_SMS_WORKER", "embedded", {"embedded", "external"}))
    sms_daily_builds_member: int = field(default_factory=lambda: _int_env("GENESIS_SMS_DAILY_BUILDS", 20, 1, 1000))
    sms_daily_builds_guest: int = field(default_factory=lambda: _int_env("GENESIS_SMS_GUEST_DAILY_BUILDS", 3, 0, 100))
    sms_inbound_per_hour: int = field(default_factory=lambda: _int_env("GENESIS_SMS_INBOUND_PER_HOUR", 30, 5, 1000))
    sms_max_queued: int = field(default_factory=lambda: _int_env("GENESIS_SMS_MAX_QUEUED", 20, 1, 500))
    sms_job_max_attempts: int = field(default_factory=lambda: _int_env("GENESIS_SMS_JOB_MAX_ATTEMPTS", 3, 1, 10))
    sms_progress: str = field(default_factory=lambda: _choice_env("GENESIS_SMS_PROGRESS", "major", {"all", "major", "off"}))
    sms_progress_min_interval: int = field(default_factory=lambda: _int_env("GENESIS_SMS_PROGRESS_MIN_INTERVAL_SECONDS", 60, 0, 3600))
    sms_link_ttl_hours: int = field(default_factory=lambda: _int_env("GENESIS_SMS_LINK_TTL_HOURS", 72, 1, 720))
    # Chat brain: plain-language conversation through the AI backend (Antigravity or the API). off = rules only.
    chat_brain: bool = field(default_factory=lambda: _choice_env("GENESIS_CHAT_BRAIN", "on", {"on", "off"}) == "on")
    chat_timeout: int = field(default_factory=lambda: _int_env("GENESIS_CHAT_TIMEOUT_SECONDS", 90, 15, 600))
    # Voice notes (WhatsApp/MMS audio) are transcribed locally with Whisper. Models: tiny | base | small | medium.
    voice_enabled: bool = field(default_factory=lambda: _choice_env("GENESIS_VOICE", "on", {"on", "off"}) == "on")
    voice_model: str = field(default_factory=lambda: _choice_env(
        "GENESIS_VOICE_MODEL", "base", {"tiny", "base", "small", "medium", "tiny.en", "base.en", "small.en"}))
    voice_language: str = field(default_factory=lambda: _str_env("GENESIS_VOICE_LANGUAGE", ""))  # "" = auto-detect

    # ---- Website builder: weave's generator (adityapokuri229/weave, src/portfolio/) run through genesis/web/bridge.mjs ----
    # Path to an unmodified weave checkout. Empty = the website builder is unavailable.
    website_engine_dir: str = field(default_factory=lambda: _str_env("GENESIS_WEBSITE_ENGINE_DIR"))
    website_builder: bool = field(default_factory=lambda: _choice_env("GENESIS_WEBSITE_BUILDER", "on", {"on", "off"}) == "on")
    node_bin: str = field(default_factory=lambda: _str_env("GENESIS_NODE_BIN") or shutil.which("node") or "")
    # Design options generated per new website (each is one more large Gemini call).
    website_variants: int = field(default_factory=lambda: _int_env("GENESIS_WEBSITE_VARIANTS", 1, 1, 3))
    website_timeout: int = field(default_factory=lambda: _int_env("GENESIS_WEBSITE_TIMEOUT_SECONDS", 1200, 60, 3600))
    # Publishing is off unless set to netlify AND a token is configured; every publish still needs the owner's PUBLISH.
    website_deploy: str = field(default_factory=lambda: _choice_env("GENESIS_WEBSITE_DEPLOY", "off", {"off", "netlify"}))
    netlify_auth_token: str = field(default_factory=lambda: _str_env("NETLIFY_AUTH_TOKEN"), repr=False)

    @property
    def builds_dir(self) -> Path:
        return self.data_dir / "builds"

    @property
    def sites_dir(self) -> Path:
        return self.data_dir / "sites"

    @property
    def gradle_user_home(self) -> Path:
        return self.data_dir / "gradle-home"

    @property
    def sms_db_path(self) -> Path:
        return self.data_dir / "sms.sqlite3"

    @property
    def whatsapp_sender(self) -> str:
        if self.twilio_whatsapp_number:
            return self.twilio_whatsapp_number
        return self.twilio_phone_number if self.twilio_channel == "whatsapp" else ""

    @property
    def sms_sender(self) -> str:
        return "" if self.twilio_channel == "whatsapp" and not self.twilio_whatsapp_number else self.twilio_phone_number

    @property
    def sms_enabled(self) -> bool:
        """An external messaging channel (Twilio SMS/WhatsApp, or the log provider) is configured."""
        if self.sms_provider == "log":
            return True
        return (self.sms_provider == "twilio" and bool(self.twilio_account_sid and self.twilio_auth_token)
                and bool(self.twilio_phone_number or self.twilio_whatsapp_number or self.twilio_messaging_service_sid))

    def sms_issues(self) -> list[str]:
        """Configuration problems that stop the SMS channel from working (names only, never values)."""
        if self.sms_provider == "off":
            return ["GENESIS_SMS_PROVIDER=off"]
        issues = []
        if self.sms_provider == "twilio":
            if not self.twilio_account_sid.startswith("AC"):
                issues.append("TWILIO_ACCOUNT_SID is missing or does not start with AC")
            if not self.twilio_auth_token:
                issues.append("TWILIO_AUTH_TOKEN is missing")
            if not (self.twilio_phone_number or self.twilio_whatsapp_number or self.twilio_messaging_service_sid):
                issues.append("set TWILIO_WHATSAPP_NUMBER (WhatsApp), TWILIO_PHONE_NUMBER (SMS) or TWILIO_MESSAGING_SERVICE_SID")
        if not self.public_base_url.startswith("https://"):
            issues.append("PUBLIC_BASE_URL must be the public https:// URL that forwards to Genesis")
        return issues

    def build_tools_dir(self) -> Path | None:
        if not self.android_home:
            return None
        bt = self.android_home / "build-tools"
        if not bt.is_dir():
            return None
        versions = sorted((p for p in bt.iterdir() if p.is_dir()), key=lambda p: [int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", p.name)])
        return versions[-1] if versions else None

    def doctor(self) -> dict:
        """Report prerequisites without revealing secrets."""
        checks = []

        def add(name: str, ok: bool, detail: str, fix: str = "") -> None:
            checks.append({"name": name, "ok": ok, "detail": detail, "fix": "" if ok else fix})

        if self.ai_backend == "antigravity":
            from .core.antigravity import AntigravityClient
            problem = AntigravityClient(self).problem()
            add("Antigravity bridge", not problem, problem or "running inside an Antigravity terminal",
                "Start Genesis from a terminal inside the Antigravity app")
        else:
            add("Gemini API key", bool(self.gemini_api_key),
                "GEMINI_API_KEY is set" if self.gemini_api_key else "GEMINI_API_KEY is missing",
                "Create a key at https://aistudio.google.com/apikey and put GEMINI_API_KEY=... in .env")
        major = java_major_version(self.java_home) if self.java_home else None
        add("JDK 17+", major is not None and major >= 17,
            f"Java {major} at {self.java_home}" if major else "No JDK 17+ found",
            "Install a JDK 17 (e.g. Eclipse Temurin 17) and set JAVA_HOME in .env")
        add("Android SDK", self.android_home is not None,
            str(self.android_home) if self.android_home else "Android SDK not found",
            "Install Android cmdline-tools and set ANDROID_HOME in .env")
        platform_ok = bool(self.android_home and (self.android_home / "platforms/android-35").is_dir())
        add("Android platform 35", platform_ok, "platforms;android-35 installed" if platform_ok else "platforms;android-35 missing",
            'Run: sdkmanager "platforms;android-35" "build-tools;35.0.0"')
        licenses_ok = bool(self.android_home and (self.android_home / "licenses/android-sdk-license").is_file())
        add("Android SDK license", licenses_ok, "accepted" if licenses_ok else "not accepted",
            "Run: sdkmanager --licenses")
        wrapper_ok = (self.template_dir / "gradlew").is_file() and (self.template_dir / "gradle/wrapper/gradle-wrapper.jar").is_file()
        add("Gradle wrapper", wrapper_ok, "template wrapper present" if wrapper_ok else "android_template/gradlew missing",
            "Restore android_template/gradlew and gradle/wrapper from git")
        return {"ready": all(c["ok"] for c in checks), "checks": checks}

    def website_problem(self) -> str:
        """Why the website builder cannot run (empty when it can). Kept out of doctor(), which gates Android builds."""
        if not self.website_builder:
            return "the website builder is switched off (GENESIS_WEBSITE_BUILDER=off)"
        if not self.website_engine_dir:
            return "no website engine is configured (GENESIS_WEBSITE_ENGINE_DIR)"
        engine = Path(self.website_engine_dir).expanduser()
        if not (engine / "src/portfolio/bespokeHtmlGenerator.js").is_file():
            return f"the website engine was not found at {engine} (expected a weave checkout with src/portfolio/)"
        if not self.node_bin or not Path(self.node_bin).is_file():
            return "Node.js was not found (install Node 20+ or set GENESIS_NODE_BIN)"
        if self.ai_backend != "api":
            return "the website engine needs the Gemini API backend (GENESIS_AI_BACKEND=api)"
        if not self.gemini_api_key:
            return "GEMINI_API_KEY is missing"
        return ""

    def website_doctor(self) -> list[dict]:
        problem = self.website_problem()
        checks = [{"name": "Website builder", "ok": not problem,
                   "detail": problem or f"weave engine at {Path(self.website_engine_dir).expanduser()}",
                   "fix": "" if not problem else "See README: Website builder"}]
        publish = self.website_deploy == "netlify" and bool(self.netlify_auth_token)
        checks.append({"name": "Website publishing", "ok": True,
                       "detail": "Netlify (owner approval per publish)" if publish else
                                 "off: websites get private previews only (set GENESIS_WEBSITE_DEPLOY=netlify and NETLIFY_AUTH_TOKEN)",
                       "fix": ""})
        return checks


settings = Settings()

