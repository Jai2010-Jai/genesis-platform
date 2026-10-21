# Genesis — describe an Android app or a website, get the real thing

Genesis is a local AI software builder with one conversation (WhatsApp, SMS, web chat, voice notes) and two
pipelines: **Android apps** (below) and **websites** (weave's generator, see [docs/WEBSITES.md](docs/WEBSITES.md)).
It works out which one you mean, asks when that's unclear, and returns an APK download link or a private
website preview. Architecture and audit: [INTEGRATION_PLAN.md](INTEGRATION_PLAN.md).

```
WhatsApp / SMS (Twilio) ─┐                          ┌─ Android: Gemini ─► validator ─► Gradle ─► APK verify ─► /download/<token>
Web chat / voice note ───┼─► conversation + router ─┤
                         │   (one SQLite store)     └─ Website: weave engine (Node bridge) ─► validator ─► /preview/<token>/
                         └──────────── progress and results through the same outbox ─── publish: owner-approved, Netlify
```

### Android pipeline

Genesis is a local AI app factory. You describe an app in plain language. Gemini writes Kotlin + Jetpack
Compose into a locked, verified Android template, Gradle compiles it on your machine, compile errors are
sent back to Gemini for up to two bounded repairs, and you download a real, verified `.apk`.

```
prompt ─► Gemini (structured manifest + Kotlin) ─► validator ─► isolated workspace (template copy)
       ─► ./gradlew assembleDebug ─► [compile errors ─► Gemini patch ─► validator ─► rebuild] ×2 max
       ─► APK verification (zip, aapt2, apksigner, permissions) ─► download
```

Phase 1 scope: calculators, habit trackers, flashcards, simple workout trackers, to-do lists.

## Requirements

| Component | Version used | Notes |
|---|---|---|
| Python | 3.10+ (3.12 recommended) | macOS system Python 3.9 is too old for the venv here |
| JDK | 17 | Eclipse Temurin 17 (`~/Library/Java/JavaVirtualMachines/jdk-17*`) |
| Android SDK | cmdline-tools, `platforms;android-35`, `build-tools;35.0.0`/`34.0.0` | `~/Library/Android/sdk` |
| Gradle | 8.9, via the checked-in wrapper | distribution pinned by SHA-256 |
| Android Gradle Plugin / Kotlin / Compose BOM | 8.7.3 / 2.0.21 / 2024.12.01 | fixed in the template |
| Gemini API key | — | see "Gemini API access and billing" |

On this machine all of the above were installed as described below and verified by `python -m genesis doctor`.

## Installation

```bash
# 1. Python environment (uv shown; python -m venv works too)
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt

# 2. JDK 17 (no sudo): download Temurin 17 for your CPU from https://adoptium.net,
#    verify its SHA-256, and extract into ~/Library/Java/JavaVirtualMachines/
#    (or: brew install openjdk@17)

# 3. Android SDK command-line tools (no Android Studio needed)
#    Download commandlinetools-mac-*_latest.zip from https://developer.android.com/studio#command-line-tools-only,
#    verify the SHA-256 shown on that page, then:
mkdir -p ~/Library/Android/sdk/cmdline-tools
unzip commandlinetools-mac-*_latest.zip -d ~/Library/Android/sdk/cmdline-tools
mv ~/Library/Android/sdk/cmdline-tools/cmdline-tools ~/Library/Android/sdk/cmdline-tools/latest
export JAVA_HOME=~/Library/Java/JavaVirtualMachines/jdk-17*/Contents/Home
~/Library/Android/sdk/cmdline-tools/latest/bin/sdkmanager --licenses      # read & accept
~/Library/Android/sdk/cmdline-tools/latest/bin/sdkmanager "platforms;android-35" "build-tools;35.0.0" "build-tools;34.0.0" "platform-tools"
```

## Configuration

```bash
cp .env.example .env
# edit .env: set GEMINI_API_KEY. JAVA_HOME / ANDROID_HOME are auto-detected if left empty.
.venv/bin/python -m genesis doctor
```

`.env` is git-ignored. The key is read only by the Python backend. It is never sent to the browser, never
written into generated apps or logs, and it is stripped from the environment that Gradle runs in.

### Gemini API access and billing

- A **Google AI Pro / Gemini app subscription does not include Gemini API credits.** The API is billed
  separately through Google AI Studio / Google Cloud.
- Keys from https://aistudio.google.com/apikey work on the **free tier** with rate limits. Free-tier prompts
  may be used by Google to improve its products. Enabling billing on the key's Cloud project raises
  limits and changes data terms; Genesis never enables paid services for you.
- `GEMINI_MODEL=auto` (default) lists the models your key can use and picks the first available of
  `gemini-flash-latest`, `gemini-pro-latest`, `gemini-2.5-pro`, `gemini-2.5-flash`. Set an explicit
  model id to override it (e.g. `GEMINI_MODEL=gemini-pro-latest` for higher-quality code, if your quota allows).
- Transient `503 UNAVAILABLE` overload errors are retried with backoff, then the next available model is tried.
- One app is typically 1 generation call plus 0–2 repair calls (~10–40k tokens in total).

### Legacy: the Antigravity bridge

> The Antigravity `agentapi` bridge only works when Genesis is started from a terminal inside the Antigravity app,
> and this setup no longer has that access. Use `GENESIS_AI_BACKEND=api` with `GEMINI_API_KEY` (the website builder
> requires it). Genesis never switches between providers by itself: a missing or failing key is reported, not
> worked around.

`GENESIS_AI_BACKEND=antigravity` sends each generation/change/repair request to the **Antigravity** desktop
app's agent (through Antigravity's own `agentapi` command) instead of the Gemini API, so it runs on the model
and quota of the Google account signed in to Antigravity. Genesis reads the agent's reply from Antigravity's
transcript (`~/.gemini/antigravity/brain/<conversation>/`), validates it against the same schema, and builds
the APK as usual. Each request appears as a "Genesis: …" conversation in Antigravity.

- Start Genesis **from a terminal inside Antigravity** (Antigravity sets `ANTIGRAVITY_LS_ADDRESS` and
  `ANTIGRAVITY_CSRF_TOKEN` there; Genesis never looks them up anywhere else). `python -m genesis doctor`
  shows an "Antigravity bridge" check.
- `GENESIS_ANTIGRAVITY_MODEL=app` (default) uses the model selected in Antigravity's model picker;
  `flash_lite` / `flash` / `pro` request a tier instead.
- Conversations run outside your Antigravity projects (`outside-of-project`) and the prompt tells the
  agent not to use tools. There is no JSON-schema enforcement, so an invalid reply is retried once.

## Running

```bash
.venv/bin/python -m genesis            # dashboard at http://127.0.0.1:8765
.venv/bin/python -m genesis build "A flashcards app with decks and a study mode" --name "CardCraft"   # CLI
```

In the dashboard: describe the app (or click an example chip), optionally name it, and click **Generate APK**.
You'll see live stages (Generate → Validate → Compile → Repair → Verify), build attempts with compiler
errors and repair notes, and the streaming Gradle log. **Download APK** is enabled only after a build
succeeded and the APK passed verification. History lists every build, including failures.

The first build downloads Gradle dependencies (~4 minutes). Later builds take about 1–2 minutes, depending
mostly on Gemini latency.

### Installing an APK on a phone

APKs are **debug-signed** test builds. Copy the file to the device and open it (allow "Install unknown apps"
for the app you open it with), or run `adb install path/to/app.apk`. Genesis never installs or runs APKs itself.

## Genesis on your phone

`mobile/` is **Genesis Studio**, a native Android app (Kotlin + Compose, ~1 MB) for creating apps from a phone.
The phone is only a remote control: your computer still does the AI generation and the Gradle builds, and the
Gemini key never leaves it.

```bash
scripts/build_phone_app.sh                 # builds dist/Genesis.apk
.venv/bin/python -m genesis serve --lan    # accept phones on your Wi-Fi
```

1. On the dashboard click **Connect phone**. Scan QR 1 with the phone camera to download the app (`/get`), or send
   people `dist/Genesis.apk` directly.
2. In the app, scan QR 2 (a `genesis://pair` link) or type the studio address and the 16-character access code.
3. Describe an app, watch it build live, then tap **Install** (Android asks once to allow installs from Genesis)
   or **Share APK**.

Remote access rules: every non-local request needs `Authorization: Bearer <access code>`. The code is stored in
`data/access_code` (mode 600) or set with `GENESIS_ACCESS_CODE`, compared in constant time, and 10 wrong codes
block that IP for 10 minutes. Only `/get` (the phone app APK, which holds no secrets) is public. Pairing details
are shown only on the computer's own dashboard. Traffic is plain HTTP on your LAN, so use it only on networks
you trust. For access from outside your home, put it behind a VPN such as Tailscale rather than exposing the port.

## Genesis by text message

Text an app idea to a Twilio number (or WhatsApp sender) and get progress texts, a secure download link for
the verified APK, and follow-up changes ("make the theme dark") applied to the same app. SMS builds use the
same pipeline and appear in the dashboard History. Setup, security model, Twilio Console steps and India
limitations are in [docs/SMS.md](docs/SMS.md).

```bash
.venv/bin/python -m genesis sms-check                                   # config + Twilio number check (sends nothing)
.venv/bin/python -m genesis sms-simulate --from +91XXXXXXXXXX "Build me a habit tracker"   # local, no SMS sent
```

## Design

Both the dashboard and the phone app use the **Journal Ink** design: warm paper, ruled notebook lines with a red
margin, serif headings, handwritten fountain-pen blue accents with highlighter, typewriter labels, rubber-stamp
statuses, watercolor app icons in each generated app's own color, and ink buttons with an offset pen shadow.

### How generated apps are designed

The template ships a protected **design kit** (`android_template/app/src/main/java/com/genesis/app/ui/kit/Kit.kt`) and theme:
- a full light/dark color scheme derived from each app's brand color, plus tuned typography and shapes
- components such as `GenesisScreen` (large collapsing title), `HeroCard`, `StatTile`, `ItemCard` with emoji/icon
  badges, `EmptyState`, `BarChart`, `LineChart`, `ProgressRing`, `ChoiceChips`, `BottomTabs`, styled inputs and buttons

Gemini must build every screen from the kit (the validator rejects apps that don't import it) and follow the design
rules in `genesis/core/prompts.py`. The kit is compiled in every build and can't be edited by the model.

## Testing

```bash
.venv/bin/python -m pytest -q
```

Tests run offline. They cover path traversal and file-type rejection, forbidden APIs, size limits, the
manifest schema, repair patch rules, compiler-diagnostic parsing, the full orchestration pipeline with a
fake Gemini and fake Gradle (success, repair success, bounded repair failure, toolchain failure not sent to AI,
unsafe output rejected), secret scrubbing from the Gradle environment, and API validation, rate limits,
host/origin checks and download gating.

Website and unified-routing tests (`tests/test_website.py`, `tests/test_unified.py`) cover: the real `bridge.mjs`
run under Node against a fake weave engine (detecting canned fallbacks, blocking `agy`, redacting keys), website
validation, versioning and rollback, Android-vs-website routing and clarification, greetings and status questions
never building, project selection across both kinds, ownership and token separation (an APK token can't open a
preview and vice versa), duplicate webhooks, one job at a time, provider failures, sandboxed preview headers,
publish approval (owner only, explicit `PUBLISH`, never anonymous), the chat brain's guard rails, and the additive
database migration. Node.js is required for the bridge tests (they are skipped without it).

SMS tests (`tests/test_sms.py`) fake Twilio and Gemini and cover signature validation, duplicate deliveries,
linking/authorization, conversations and follow-up changes, queue retries and crash recovery, cancellation, send
failures, delivery callbacks, download tokens and rate limits. `GENESIS_RUN_INTEGRATION=1` additionally runs the
SMS flow through a real Gradle build (`tests/test_sms_integration.py`).

To verify the Android template itself:

```bash
cd android_template && JAVA_HOME=... GRADLE_USER_HOME=../data/gradle-home ./gradlew assembleDebug
```

## Project layout

```
genesis/
  __main__.py        CLI: serve | doctor | build
  config.py          settings from env/.env, JDK/SDK detection, `doctor` checks
  server.py          FastAPI app: dashboard + JSON API, localhost/origin guard, rate limit, CSP
  core/schemas.py    Pydantic contracts (AppManifest, RepairPatch, BuildRequest)
  core/prompts.py    Gemini system/generation/repair prompts (the template contract)
  core/gemini.py     google-genai client, model auto-selection, structured JSON output, retries
  core/validation.py path/type/size/package/forbidden-API checks for generated code and patches
  core/builder.py    workspace creation, Gradle runner (timeout, scrubbed env), APK verification
  core/diagnostics.py Kotlin compiler error extraction
  core/jobs.py       job store (data/builds/<id>/), queue, generate→build→repair pipeline
  sms/               shared conversation engine for WhatsApp/SMS/web chat: store, intents, chat brain, worker, routes
  web/bridge.mjs     runs weave's website generator in a sandboxed Node process (no deploys, no fallbacks)
  web/builder.py     website builds (data/sites/<id>/): generate/modify -> validate -> preview
  web/validate.py    checks for generated HTML
  web/deploy.py      owner-approved Netlify publishing (token only)
  static/            dashboard (vanilla HTML/CSS/JS, no external requests)
android_template/    protected Android project (Gradle, manifest, theme, MainActivity)
tests/               pytest suite
data/                runtime: builds, logs, APKs, Gradle cache (git-ignored)
```

## Security model

What's enforced now:

- **The model writes Kotlin only.** Each file must be a `*.kt` path under `app/src/main/java/com/genesis/app/`,
  with at most 4 levels of nesting and no `..`, absolute paths, control characters or protected files
  (`MainActivity.kt`, theme files). Limits are 20 files, 60 KB per file, and 250 KB in total. Each file's
  `package` must match its directory.
- **Build configuration is server-controlled.** Gradle scripts, the wrapper, dependencies, repositories,
  `AndroidManifest.xml` (no permissions), and resources come from the template. Per-build values
  (applicationId, color, app name) are validated by regex and escaped before they're written. `app/build.gradle.kts`
  re-validates the applicationId.
- **Forbidden APIs** in generated code: process execution, dynamic code loading, reflection, networking,
  native code, WebView, and imports outside an allowlist. Repairs go through the same validator, and a
  repair can add at most 3 new files.
- **Generated code is compiled, never executed, on the host.** There are no annotation processors or compiler
  plugins that the model controls. The only Gradle task allowed is `assembleDebug`, and it runs from the
  pinned wrapper.
- **Gradle runs with a minimal environment** (no `GEMINI_API_KEY`), in its own process group, with a
  timeout (default 900 s) that kills the whole group.
- **Each build gets its own directory** (`data/builds/<uuid>/`), which keeps concurrent and past builds from
  interfering. Bulky intermediates are deleted afterwards; sources, logs and the APK are kept.
- **Success is claimed only** when Gradle exits 0 and the APK passes these checks: valid zip, has a manifest,
  dex and resources, the aapt2 package name matches, no unexpected permissions, and `apksigner verify` passes.
- **The API** binds to 127.0.0.1 only (the CLI refuses other hosts). It rejects non-localhost `Host` headers
  and cross-origin POSTs, validates all input, caps body size, and rate-limits builds to 6 per 10 minutes
  (5 queued at most). It serves a strict CSP and returns no filesystem paths.

**Needed before exposing Genesis to untrusted users (not done in Phase 1):**

- Authentication, per-user quotas, and CSRF tokens.
- Run every Gradle build in a disposable sandbox: a container or VM with no host filesystem, a read-only
  dependency cache, egress limited to a vetted Maven mirror (or no network with a pre-warmed cache),
  CPU/memory/disk quotas, and a non-root user. The Kotlin compiler and AGP are complex parsers of
  attacker-influenced input.
- Release signing with managed keys (outside the build sandbox), and malware/static scanning of outputs.
- Persistent job queue and artifact storage with retention limits; abuse monitoring of prompts.

## Status: implemented vs. not yet

Implemented and verified on this machine:

- [x] Dark dashboard with prompt, optional name, example chips, live stage stepper and progress, streaming
      log, attempts/errors, history, and gated APK download with install instructions
- [x] Gemini structured generation (Pydantic schema to JSON schema) with model auto-discovery and retries
- [x] Verified Compose template. Built directly, and via Genesis, into installable debug APKs
- [x] Workspace isolation, Gradle wrapper builds, timeouts, cleanup
- [x] Compiler-diagnostic extraction and bounded AI repair (≤2). Seen working on a real build
      (FitPulse: failed attempt 1, repaired, succeeded on attempt 2)
- [x] APK verification (zip, aapt2, permissions, apksigner)
- [x] Automated tests (offline), `.env.example`, `.gitignore`, `doctor` command

Not yet / limitations:

- [ ] Generated apps aren't run or UI-tested. A successful compile doesn't prove the app behaves well;
      install it and check the `validation_rules` shown for each build.
- [ ] Only debug-signed APKs (no release signing or AAB/Play upload).
- [ ] Kotlin sources only: no custom resources, images, or extra dependencies (Room, navigation,
      DataStore, networking). Persistence uses SharedPreferences + org.json.
- [x] Phone app (Genesis Studio) with pairing, live build tracking, install and share. Tested on an Android 14 emulator
- [ ] Single-machine, in-process queue. A server restart marks in-flight builds as failed.
- [ ] Access-code auth only (no user accounts) over LAN HTTP; see the security section before exposing it beyond your network.
- [x] Iterative editing of an existing app by text/chat ("make the theme dark"): change builds modify the last good version.

Unified platform (Android + websites), verified on this machine on 2026-10-21:

- [x] One conversation engine for WhatsApp, SMS and web chat routes to the Android or website builder, asks "app
      or website?" when unclear, and never builds from greetings or status questions
- [x] Real end-to-end Android build through the conversation (Gemini API -> Gradle -> verified APK -> token link)
- [x] Real end-to-end website generation and change with weave's generator (Gemini API), validated and served as a
      sandboxed private preview
- [x] Gemini API replaces the unavailable Antigravity bridge (`GENESIS_AI_BACKEND=api`)
- [ ] Netlify publishing: implemented and tested against a fake API only (no token configured)
- [ ] Live Twilio delivery of the unified flow: not re-tested (needs the tunnel running, the Twilio webhook URL
      updated, and real messages sent)
- [ ] Website builds are not shown in the dashboard History (logs: `data/sites/<id>/build.log`)
- [ ] Gemini latency on the current key is high (2-6 minutes per large call), so builds take several minutes
