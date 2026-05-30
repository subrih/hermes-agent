# iOS Plugin — Capability Roadmap & Tracking

Living doc for the Kaveri iOS ↔ Hermes platform adapter
(`plugins/platforms/ios/`). Tracks what the old app did, what Hermes
gives us, and what's new. Branch: `kaveri`.

Status legend: ✅ done · 🔨 building · 📋 planned · 🤔 decide · ❄️ deferred

---

## A. Foundation (done)

- ✅ Plugin scaffold (`__init__.py`, `plugin.yaml`, `adapter.py`) — mirrors the bundled `ntfy` platform plugin
- ✅ Inbound: Firestore `on_snapshot` on `users/{uid}/channels/{channel}/messages` (role=user, intent=interact) → `handle_message()`
- ✅ Outbound: `send()` writes role=assistant docs back to Firestore
- ✅ RTDB `channel_state/{channel}` typing toggle (best-effort) — `IOS_FIREBASE_RTDB_URL` set
- ✅ `firebase-admin` 7.4.0 installed in venv (NOT yet in pyproject.toml — TODO)
- ✅ Discovered + enabled (`hermes plugins enable platforms/ios`)
- ✅ Explicit `ios:` block in `platform_toolsets` (broadest = mirrors cli, 19 toolsets) + `known_plugin_toolsets.ios:[spotify]`
- ✅ Gateway runs 2 platforms (discord + ios), watching `cockpit`, rtdb=True
- 📋 **End-to-end text round-trip not yet user-tested** (app → Firestore → Kaveri → reply → app)

Config (runtime/.env): `IOS_FIREBASE_CREDENTIALS`, `IOS_FIREBASE_RTDB_URL`,
`IOS_UID=subri`, `IOS_CHANNELS=cockpit`, `IOS_HOME_CHANNEL=cockpit`, `IOS_ALLOWED_USERS=subri`.
Decision: **roll out all channels** → expand `IOS_CHANNELS=cockpit,finance,logistics`.

---

## B. Port from old app — features the app UI already supports

Ranked by value. Hermes hook in parens (file:line in hermes src).

### Tier 1
1. ✅ **Approvals (interactive bubble)** — DONE & VERIFIED (2026-05-30). Implemented `send_exec_approval()` in adapter.py — the platform hook the gateway calls for dangerous-command approval (gateway/run.py:17365; Discord uses it for its ExecApprovalView buttons). iOS version writes the app's existing contract so NO app changes:
   - WRITE `channel_state/{channel}/pending_approval` = `{request_id, description(incl. command), agent_id=session_key}` → app's ApprovalBubble shows Allow/Decline.
   - LISTEN `approval_decisions/{request_id}` for the app's write `{agent_id, decision:"approve"|"decline", state_key}`.
   - MAP approve→`once`, decline→`deny`; call `resolve_gateway_approval(session_key, choice)` to unblock the agent (same resolver as the text `/approve`).
   - Returns success=False on RTDB failure → gateway falls back to text `/approve` prompt automatically.
   - Verified both branches live: decline→"Command was denied"; approve→command executed.
   - LIMITATION: app bubble is binary (Allow/Decline) → only `once`/`deny`. The `session`/`always` options remain available only via text `/approve session|always` (acceptable; binary is the better phone UX).

   (historical note — TWO mechanisms exist:)
   - `send_slash_confirm()` (base.py:2167) is ONLY for slash commands (`/reload-mcp`), not the general flow.
   - Dangerous-command approval = `tools/approval.py`, resolved by user typing `/approve`÷`/deny`. Since the app sends messages as Firestore writes, **text `/approve` ALREADY works** through the adapter's inbound path today.
   - The richer RTDB *bubble* UX (app writes decision to `approval_decisions/{id}`) is a real bridge to build on top — write `pending_approval` to RTDB in `send_slash_confirm`, listen `approval_decisions`, call `slash_confirm.resolve()` (pattern mirrors `_dispatch_inbound`). DEFERRED until basic round-trip is user-tested.
2. ✅ **Live thinking ripple** — DONE & VERIFIED LIVE (2026-05-30). Driven by SHELL HOOKS (adapter has no per-tool callback — same gateway mechanism Discord uses for its 👀/✅ feedback). `runtime/hooks/ios_ripple.py` fires on `pre_tool_call`/`post_tool_call`/`on_session_finalize`, writes `channel_state/{channel}` `thoughts[]` + `current_hook` to RTDB via cached service-account token.
   - KEY FIX: channel is resolved from env **`HERMES_SESSION_KEY`** (`agent:main:<platform>:<type>:<channel>`, e.g. `agent:main:ios:dm:cockpit`) — the tool-call payload does NOT carry the channel (session_id empty on pre_tool_call). Requires `ios` in the key → strict no-op for cli/discord (verified).
   - Config: plain `hooks:` entries (NO shell guard — the earlier `test $HERMES_SESSION_PLATFORM` guard was broken; that env var is never set). `hooks_auto_accept: true`.
   - Verified live through the real gateway: injected a tool-forcing msg → watched RTDB cockpit go `typing=True → thoughts=['Running command'] → cleared`. debug capture (`IOS_RIPPLE_DEBUG`) now OFF.
   - Note: MiniMax tools are very fast so `current_hook` flickers; `thoughts[]` is the durable signal. Heavier tools (web/code) linger.
   - DON'T-REINVENT principle: the hook imports Hermes' canonical `get_tool_emoji` from `gateway.tool_emojis` (single source of truth) and uses the SAME phrase format as the gateway progress driver (`{emoji} {tool}: "{preview}"`). Earlier I'd hand-rolled a `_TOOL_EMOJI` dict that had already drifted (web_search 🔍 vs canonical 🌐) — removed. The only hook-local logic is preview-arg extraction (the gateway computes `preview` upstream and the hook can't see it; it only gets tool_input). Why a hook at all (not the gateway's progress path): the gateway writes progress as CHAT MESSAGES via send()/edit_message(); user wants it in the RTDB ripple, not as Firestore bubbles — so we keep the edit_message gate CLOSED and drive the ripple from the pre_tool_call hook instead.

### ✅ Tool-progress — FINAL DESIGN (2026-05-30): exactly Discord, as chat bubbles
DECISION (user, after a round of over-engineering): do EXACTLY what Discord does — no reinvention.
- iOS overrides `edit_message()`/`delete_message()` → gateway gate opens (run.py:16513) → the gateway's
  OWN in-process tool-progress engine builds the phrase ('🔍 web_search: "…"') using the REAL tool
  registry emojis (agent.display.get_tool_emoji) and delivers the finished string to `send()`/`edit_message()`.
  iOS just writes it to Firestore (one bubble, edited in place). Verified live: '🔍 web_search: "…"'.
- REMOVED the earlier ios_ripple.py shell hook + hand-rolled _TOOL_EMOJI dict + _phrase() builder.
  Why they were wrong: the hook ran in a SUBPROCESS without the tool registry, so canonical
  get_tool_emoji returned ⚙️ for everything → forced a hand-maintained emoji list (reinvention + drift).
  The gateway path has the registry in-process, so emojis are correct for free.
- TRADEOFF accepted: progress is a CHAT BUBBLE (like Discord), NOT the RTDB ripple box. The ripple now
  only carries typing on/off (on_processing_start/complete). Putting rich progress in the ripple was
  mutually exclusive with Discord-parity (would require the subprocess hook). User chose parity.
- config.yaml hooks back to `{}`; runtime/hooks/ dir removed.
- NEWLINE FIX (2026-05-30): gateway joins multi-tool progress with single `\n`; the iOS app renders
  Markdown where a lone `\n` is a soft break (collapses to a space) → all tools showed on one line.
  Fix: `_md_hardbreaks()` in adapter.py upgrades lone `\n`→`\n\n` (paragraph break), applied ONLY in
  `edit_message()` (the progress-accumulation path; final answers go through `send()` untouched;
  streaming is off so edit_message == progress only). Code fences are preserved. Verified: progress
  doc stores `💻 …\n\n💻 …\n\n💻 …`. If spacing feels too loose, switch to two-trailing-space hard breaks.
- BONUS: enabled `display.platforms.ios.cleanup_progress: true` → progress bubble auto-deletes after the
  final answer lands (uses delete_message). Verified bubbles get removed post-answer.

### (superseded) earlier tool-progress note — exactly mirroring Discord
The gateway's built-in tool-progress driver (gateway/run.py `progress_callback` →
`send_progress_messages()`) only routes progress to adapters that OVERRIDE
`edit_message()` — the gate is `type(adapter).edit_message is BasePlatformAdapter.edit_message`
(run.py:16513). Discord passes it; iOS now does too. Implemented in adapter.py:
- `edit_message()` — merge-updates the Firestore message doc in place (sets text/edited/ts_edited).
- `delete_message()` — for optional `display.platforms.ios.cleanup_progress`.
- `on_processing_start/complete()` — RTDB ripple on/off (iOS analog of Discord 👀→✅).
- `send()` no longer clears the ripple (it also fires for interim narration); clearing moved to on_processing_complete.
Verified live: a web_search turn produced `🔍 web_search: "…"` then the final answer in Firestore,
and a terminal turn produced `💻 terminal: "date"` — i.e. same as Discord. `display.tool_progress: all`.

### 📋 Tier 2 — planned
3. **Multi-channel** — `IOS_CHANNELS=cockpit,finance,logistics`. Each channel its own persona via `channel_prompts` + auto-skills via `channel_skill_bindings` (config.py; resolve_channel_prompt base.py:1558).
4. **Inbound media** — populate `MessageEvent.media_urls` from app's Firebase Storage `attachment_urls` → Kaveri vision tool sees images/PDFs (base.py:614 cache helpers).
5. **Outbound media** — `send_image/send_voice/send_document/send_multiple_images` (base.py:2353+). Kaveri sends images, voice notes, files back.
6. **TTS voice replies** — `play_tts()` (base.py:2471). ElevenLabs already configured.
7. **Push notifications** — old Supabase APNs edge-fn fires on assistant-insert → likely ALREADY works with our writes. VERIFY.
8. **Processing reactions / status** — `on_processing_start/complete` (base.py:2908-2911).
9. **System-event pills** — session_start/end, compact markers (app SystemEventPill).
10. **Sessions** — multiple convos per channel; Hermes session mgmt + app's ChatSessionListSheet.

### 🤔 Tier 3 — decide / replace, not port
11. **Graph interrupts** — were LangGraph-specific. Hermes does agents differently → REPLACE concept (use `send_clarify`/approvals) rather than port.
12. **Persistent boards** — `users/{uid}/boards`. Could map to a Hermes skill that writes boards. Decide.
13. **Geofence / location** — old geofence_watcher + schedules.json routing. Rebuild as Hermes **cron + skill**, or adapter location intent. Big.
14. **Finance / home-automation domain tools** — become **Hermes skills / MCP servers**, not adapter features.

### ❄️ Deferred — big separate projects
15. **Voice (WebRTC / Pipecat / Daily / CallKit)** — the live voice-call feature. Large standalone effort.

---

## C. What Hermes NEWLY unlocks — capabilities the old app NEVER had

The old app was limited to what sangamam-v3 hand-built. Hermes ships these
for free; the iOS app can now expose them:

- **Skills system** — 90 bundled skills + [agentskills.io] Hub + **self-authoring** (Kaveri creates skills from experience, improves them in use). App could browse/trigger skills; Kaveri gains abilities over time with no backend work.
- **Cron / scheduled automations** — `IOS_HOME_CHANNEL` already makes iOS a cron deliver target. Daily briefings, reminders, nightly reports → pushed to the app in natural language. (Replaces the old geofence/schedules glue with a general scheduler.)
- **Delegation / subagents** — Kaveri spawns parallel subagents for big tasks (delegation toolset is in the ios set). Heavy multi-step work from the phone.
- **MCP servers** — connect ANY MCP (GitHub, Gmail, Calendar, Drive, etc.). Each instantly becomes an iOS capability. The app's old hand-built "home MCP" generalizes to all MCP.
- **Persistent memory + user modeling** — Hermes agent-curated memory, USER.md, Honcho dialectic modeling. Kaveri builds a deepening model of the user across sessions — app benefits automatically.
- **Session search (FTS5 + LLM summarize)** — semantic search of all past conversations from the app ("what did we decide about X last month").
- **Remote/sandboxed compute** — terminal backends: local, Docker, SSH, Modal, Daytona. From the phone, Kaveri can run heavy/risky work in an isolated sandbox.
- **Computer use (macOS)** — `computer_use` is in the ios toolset. Drive the Mac desktop from the phone (screenshots, click, type).
- **Built-in multimodal tools** — web search/extract (Tavily), vision, image generation, (video). No custom backend.
- **Cross-platform continuity** — same Kaveri reachable from CLI, Discord, iOS, email — one brain, many doors. Start on phone, continue on desktop.
- **send_message fan-out** — from an iOS request, Kaveri can post to Discord/Telegram/email/etc.
- **Any-model switching** — `/model` from the app; swap providers with no code change (ties to the privacy/cost model decision still open).
- **Kanban multi-agent board** — task decomposition + parallel workers, surfaced to the app.
- **Clarify** — native interactive disambiguation (buttons) when Kaveri needs a choice.

**Framing:** moving to Hermes flips the model — instead of building each
feature into a custom backend, we *enable* Hermes capabilities and *expose*
them through the adapter. The app becomes a thin, rich client over a
general agent runtime.

---

## D. Known issues / debt
- `firebase-admin` not in `pyproject.toml` (only venv) — add so clean reinstall keeps it.
- Pyright flags `gateway.*` / `firebase_admin` imports unresolved — false positives (resolve at runtime).
- iOS toolset is very broad (full local power incl. terminal/file/code). Intentional ("full control on iOS") — `ios:` block is the tweak point.
- RTDB ripple currently only toggles `typing`; thoughts/current_hook not yet written (Tier 1 #2).
