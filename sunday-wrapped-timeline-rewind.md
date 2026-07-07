# Sunday Timeline + Wrapped — Replacing Rewind

## One-line brief

Replace the current Rewind screenshot scrubber with a local-first semantic activity timeline, plus weekly/monthly/yearly “Sunday Wrapped” summaries that explain what the user actually worked on, where attention went, and what patterns Sunday noticed.

## Product intent

The current Rewind feature is useful but too literal: it captures screenshots, OCRs them, and lets the user scrub through frames. That makes it feel like a private security camera for the Mac.

The desired feature is closer to Dayflow: a timeline of meaningful activity. Screenshots and OCR should remain as evidence, but the primary interface should be a human-readable timeline of work sessions, moments, projects, people, apps, websites, and summaries.

This should replace Rewind as the user-facing concept. Internally, screen capture can still exist, but the product should become “Timeline” or “Sunday Timeline,” with “Wrapped” as the recurring summary layer.

## Existing implementation context

Repository:

```txt
/Users/myles-gravity/Development/_github/sunday
```

Current Rewind implementation:

- UI entry point: `electron/renderer/rewind-view.js`
- UI markup: `electron/renderer/index.html`, section `#view-rewind`
- UI styles: `electron/renderer/styles.css`, classes around `.view-rewind`, `.rw-*`
- Daemon routes: `src/sunday/daemon.py`
  - `GET /v1/rewind/recent`
  - `GET /v1/rewind/state`
  - `POST /v1/rewind/toggle`
- Mac capture/storage: `src/sunday/devices/rewind_macos.py`
- Satellite handlers: `src/sunday/devices/satellite.py`
  - `rewind_start`
  - `rewind_stop`
  - `rewind_search`
  - `rewind_recent`
  - `rewind_stats`

Current storage:

```py
REWIND_DIR  = ~/.sunday/rewind
REWIND_DB   = ~/.sunday/rewind.db
REWIND_FLAG = ~/.sunday/rewind.enabled
```

Current capture behavior:

- Captures full-screen screenshots using macOS `screencapture`
- OCRs locally using Apple Vision via a small Swift binary
- Indexes OCR text into SQLite + FTS5
- Deduplicates repeated frames by hash
- Default capture interval is currently five minutes in `rewind_macos.py`
- UI enable button currently starts with `interval_seconds: 60`
- Current retention is short: `RETENTION_DAYS = 3`

Current frame schema:

```sql
CREATE TABLE IF NOT EXISTS frames (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    image_path   TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    ocr_text     TEXT,
    created_at   REAL NOT NULL
);
```

Current UI behavior:

- Loads recent frames from `/v1/rewind/recent?limit=1000`
- Sorts by timestamp
- Shows one screenshot at a time
- Has a slider, previous/next buttons, play button, timestamp, and OCR side panel
- Empty state says “Screen history is off”

This is the foundation, not the final product.

## Core product shift

### From

“Scroll back through screenshots of your screen.”

### To

“See a private timeline of what you were doing, why it mattered, and how your week/month/year unfolded.”

The screenshots become supporting evidence. The primary object becomes a timeline event.

## Naming

The user-facing feature should likely be called one of:

- Timeline
- Sunday Timeline
- Activity Timeline
- Rewind Timeline

Avoid making “Rewind” the main name long-term. Rewind implies raw playback. The desired product is more semantic and reflective.

“Sunday Wrapped” is the summary feature layered on top of Timeline.

## Main feature pieces

### 1. Local-first activity capture

Continue using the existing local screenshot + OCR pipeline, but enrich every captured frame with more metadata.

Capture should remain opt-in. Nothing should leave the Mac unless the user explicitly asks Sunday to reason over or summarize it. Even then, the product should prefer sending derived text/events instead of raw screenshots.

Recommended additions to frame metadata:

```sql
ALTER TABLE frames ADD COLUMN thumbnail_path TEXT;
ALTER TABLE frames ADD COLUMN active_app TEXT;
ALTER TABLE frames ADD COLUMN window_title TEXT;
ALTER TABLE frames ADD COLUMN browser_url TEXT;
ALTER TABLE frames ADD COLUMN browser_title TEXT;
ALTER TABLE frames ADD COLUMN display_name TEXT;
ALTER TABLE frames ADD COLUMN privacy_redacted INTEGER DEFAULT 0;
```

Possible metadata sources:

- macOS active application
- active window title
- browser URL/title from Cockpit or accessibility if available
- OCR text
- timestamp
- screenshot hash
- screen/display name

Do not block the feature on perfect metadata. Start with OCR + timestamp + screenshot. Add active app/window title next.

### 2. Timeline events

Add a semantic event layer over frames.

Frames are raw evidence. Timeline events are user-facing activity blocks.

Example event:

```json
{
  "id": 123,
  "start_ts": 1783271400,
  "end_ts": 1783275300,
  "type": "coding",
  "title": "Worked on Sunday Timeline planning",
  "summary": "Inspected the existing Rewind implementation, reviewed the screenshot/OCR storage path, and planned how to replace the scrubber with a semantic timeline and Wrapped summaries.",
  "apps": ["Terminal", "Cursor", "Chrome"],
  "urls": ["github.com/mylesndavid/sunday"],
  "project_guess": "Sunday",
  "people": [],
  "frame_ids": [1001, 1002, 1003],
  "confidence": 0.82,
  "importance": 0.68
}
```

Suggested table:

```sql
CREATE TABLE IF NOT EXISTS timeline_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts        REAL NOT NULL,
    end_ts          REAL NOT NULL,
    type            TEXT,
    title           TEXT NOT NULL,
    summary         TEXT,
    apps_json       TEXT,
    urls_json       TEXT,
    people_json     TEXT,
    projects_json   TEXT,
    frame_ids_json  TEXT,
    confidence      REAL DEFAULT 0,
    importance      REAL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_timeline_events_start ON timeline_events(start_ts);
CREATE INDEX IF NOT EXISTS idx_timeline_events_end ON timeline_events(end_ts);
CREATE INDEX IF NOT EXISTS idx_timeline_events_type ON timeline_events(type);
```

Optional FTS table:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS timeline_events_fts USING fts5(
    title,
    summary,
    content='timeline_events',
    content_rowid='id'
);
```

### 3. Segmenter

Build a background process that turns recent frames into timeline events.

This can run locally inside the satellite or daemon. It should not be a UI-only process.

Initial simple segmentation rules:

- Sort frames by timestamp.
- Start a new segment if there is a time gap above a threshold, e.g. fifteen or thirty minutes.
- Start a new segment if active app changes and stays changed for more than one or two frames.
- Start a new segment if browser domain changes and stays changed.
- Merge adjacent frames with similar OCR text or same app/window/project.
- Deduplicate low-information frames.
- Mark long gaps as idle/away if no frames exist.

Better segmentation later:

- Use embeddings over OCR text/title/window/app metadata.
- Cluster similar activity within a day.
- Detect project names from paths, repo names, browser URLs, issue titles, editor text, Terminal prompts.
- Detect people from Slack/iMessage/email/calendar/meeting contexts when available.
- Detect whether the user is coding, messaging, researching, designing, planning, meeting, admin/billing, entertainment, etc.

### 4. Summarizer

Add a summarization job that converts timeline segments into human-readable titles and summaries.

Important: summarize events from text/metadata first. Do not send screenshots to a remote model by default. Screenshots can be opened by the user locally as evidence.

The summarizer should produce:

- short title
- one-to-two sentence summary
- activity type
- project guesses
- people involved
- apps/sites involved
- importance score
- confidence score

Implementation options:

- Local heuristic titles first, no model required.
- Optional model summarization for richer summaries.
- Batch summarize once per event, then cache the result.
- Re-summarize only if an event changes meaningfully.

Example heuristic title:

- OCR mentions `rewind-view.js`, `/v1/rewind`, `sunday`, `daemon.py` → “Worked on Sunday Rewind”
- App is Slack/iMessage → “Messaging”
- Browser domain is Linear/GitHub + repo name → “Reviewed GitHub/Linear work”

### 5. Timeline API

Add new endpoints while keeping old Rewind endpoints during transition.

Recommended endpoints:

```txt
GET  /v1/timeline/events?from=&to=&limit=&type=&project=&q=
GET  /v1/timeline/day?date=YYYY-MM-DD
GET  /v1/timeline/search?q=&from=&to=
GET  /v1/timeline/state
POST /v1/timeline/toggle
POST /v1/timeline/segment
POST /v1/timeline/summarize
```

Compatibility endpoints can remain:

```txt
GET  /v1/rewind/recent
GET  /v1/rewind/state
POST /v1/rewind/toggle
```

But the UI should move to `/v1/timeline/*`.

### 6. Timeline UI

Replace the current slider-first UI.

The main UI should be a timeline, not a screenshot scrubber.

Suggested layout:

```txt
┌────────────────────────────────────────────────────────────┐
│ Timeline                         Search moments...          │
├──────────────┬───────────────────────────────┬─────────────┤
│ Day rail     │ Activity cards                │ Detail pane │
│              │                               │             │
│ Today        │ 9:10–10:25  Coding            │ Screenshot  │
│ Yesterday    │ Worked on Sunday Timeline     │ OCR         │
│ This Week    │                               │ Evidence    │
│ June         │ 10:30–11:05 Research          │ Apps/URLs   │
│              │ Looked into Dayflow-style UI  │             │
└──────────────┴───────────────────────────────┴─────────────┘
```

UI modes:

- Today
- Yesterday
- This Week
- This Month
- Custom range
- Search results
- Wrapped

Activity card fields:

- time range
- title
- summary
- app/site/project chips
- importance marker if notable
- small thumbnail strip or one representative thumbnail

Detail pane:

- selected event summary
- representative screenshot
- frame scrubber for that event only
- OCR text
- apps/sites
- related people/projects
- button to ask Sunday about this moment

The screenshot slider should survive only inside the detail pane for a selected event. It should no longer be the main interaction.

### 7. Search

Search should work across:

- timeline event titles
- summaries
- OCR text
- apps
- URLs/domains
- project guesses
- people

Example searches:

- “Dimitri billing”
- “Fricks app”
- “when was I working on Sunday Rewind?”
- “that thing about provisioning profiles”
- “Pipeline Data”
- “Stripe refund”

Search result should return timeline events, not raw frames first. Raw frames can be shown inside each result.

### 8. Sunday Wrapped

Sunday Wrapped is a generated summary over timeline events for a period.

Required periods:

- Weekly
- Monthly
- Yearly

Daily summaries are also useful, but the user specifically wants weekly/monthly/yearly, not just annual.

Suggested table:

```sql
CREATE TABLE IF NOT EXISTS timeline_summaries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    period_type       TEXT NOT NULL, -- day, week, month, year
    period_start      REAL NOT NULL,
    period_end        REAL NOT NULL,
    title             TEXT,
    summary           TEXT,
    highlights_json   TEXT,
    projects_json     TEXT,
    people_json       TEXT,
    apps_json         TEXT,
    websites_json     TEXT,
    stats_json        TEXT,
    observations_json TEXT,
    generated_at      REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_timeline_summaries_period
ON timeline_summaries(period_type, period_start, period_end);
```

Wrapped should include:

- Top projects worked on
- Where attention went
- Longest deep-work sessions
- Most active days
- People the user interacted with most
- Apps/sites by time or event count
- Recurring themes
- Shipped or nearly shipped work
- Abandoned or unresolved loops
- Surprising patterns
- Screenshots/moments as visual receipts
- Sunday’s opinionated read of the period

The important part is not Spotify-style vanity stats. It should be useful and personal.

Example weekly Wrapped:

```md
# Your Week

You spent most of this week circling infrastructure and agent coordination: Sunday, Orca, ClickMux, and build-server work kept showing up across sessions.

## Main threads

1. Sunday Timeline/Rewind
   You inspected the current Rewind implementation and shifted the concept toward a semantic timeline with weekly/monthly Wrapped summaries.

2. Build server setup
   You spent several sessions getting the Linux build environment into shape: Docker, Node, Python, pnpm, and GitHub Actions runner prep.

3. Agent collaboration systems
   Orca, AgentOS, ClickMux, and agent-to-agent workspace ideas kept overlapping. This is clearly one larger product direction, not separate random projects.

## Pattern Sunday noticed

You were not just building tools. You were trying to create a working environment where agents can remember context, coordinate over time, and not lose the plot. That showed up in almost every major work block.
```

Tone should feel like Sunday: direct, observant, useful. But do not hardcode Sunday’s chat personality into backend data. Store neutral structured data; render copy carefully in the UI.

### 9. Privacy and retention

Privacy matters more here than polish.

Rules:

- Feature is opt-in.
- Default raw screenshot retention should stay short.
- Derived timeline events can persist longer.
- User should be able to delete all timeline data.
- User should be able to delete a day/range/event.
- User should be able to pause capture.
- User should see what is stored.
- Sensitive screenshots should not be sent to cloud models by default.

Recommended retention policy:

- Raw full screenshots: three to seven days by default
- Thumbnails: thirty to ninety days by default
- OCR text: user-configurable; perhaps ninety days default
- Timeline events/summaries: long-term by default, because they are the actual memory layer

Add settings later:

- Capture interval
- Screenshot retention
- OCR retention
- Timeline retention
- Excluded apps/sites
- Pause timeline
- Delete all timeline data

Excluded apps/sites are important. Examples:

- password managers
- banking
- private browsing
- health portals
- payment/checkout screens

### 10. Implementation plan

#### Phase 1 — Rename/reframe UI

- Change the tab label from `Rewind` to `Timeline`.
- Keep existing capture toggle.
- Keep existing screenshot view working.
- Add a top-level explanation that this is becoming a semantic timeline.
- Do not break `/v1/rewind/*` yet.

Files likely touched:

- `electron/renderer/index.html`
- `electron/renderer/rewind-view.js`
- `electron/renderer/styles.css`
- maybe `electron/renderer/app.js`

#### Phase 2 — Add timeline event schema

- Extend `rewind_macos.py` or create a new module, e.g. `timeline_macos.py`.
- Keep the existing `frames` table.
- Add `timeline_events` and `timeline_summaries` tables.
- Add migration-safe schema creation.
- Add basic event generation from recent frames.

Recommendation: keep database path as `~/.sunday/rewind.db` at first to avoid migration mess, but abstract naming in code toward timeline. Later it can be migrated to `~/.sunday/timeline.db`.

#### Phase 3 — Segment frames into events

- Add local function like `build_timeline_events(from_ts, to_ts)`.
- Use simple rules first: time gaps, app/window/domain changes, OCR similarity.
- Create/update events idempotently.
- Store frame IDs as JSON.

#### Phase 4 — Timeline API

Add daemon handlers:

```py
_http_timeline_events
_http_timeline_search
_http_timeline_state
_http_timeline_toggle
_http_timeline_segment
_http_timeline_summarize
_http_timeline_wrapped
```

Route them in `daemon.py`.

Satellite methods can call local timeline functions similarly to current `rewind_recent` and `rewind_stats`.

#### Phase 5 — Timeline UI

Replace `rewind-view.js` behavior with a timeline renderer.

Possible file rename:

- `electron/renderer/rewind-view.js` → `electron/renderer/timeline-view.js`

Or keep the file initially to reduce diff size.

UI components:

- range selector: today/week/month
- search input
- event list
- event detail pane
- screenshot evidence viewer
- empty state
- capture toggle

#### Phase 6 — Wrapped generation

Add Wrapped page or mode inside Timeline.

UI sections:

- This Week
- This Month
- This Year
- custom previous periods

Backend:

```txt
GET /v1/timeline/wrapped?period=week
GET /v1/timeline/wrapped?period=month
GET /v1/timeline/wrapped?period=year
POST /v1/timeline/wrapped/generate
```

Generate from timeline events, not raw screenshots.

#### Phase 7 — Ask Sunday about a moment

Add a button on an event detail:

- “Ask Sunday about this”
- “Summarize this session”
- “What was I doing here?”

This should send the event summary, OCR snippets, timestamps, apps, and maybe selected screenshot only if the user chooses.

## Important product principles

### It should feel like memory, not surveillance

Do not center the UI around a wall of screenshots. The user should feel like Sunday remembers the shape of their work, not like they are being watched.

### The timeline should be opinionated

A weak version says:

> Chrome, Cursor, Terminal, Slack.

A good version says:

> You spent the afternoon turning Rewind from a screenshot scrubber into the beginning of Sunday’s long-term activity memory.

### Keep evidence accessible

The user should always be able to inspect the underlying screenshot/OCR for a timeline event. This builds trust and makes search useful.

### Preserve local-first behavior

This feature becomes deeply personal very quickly. Store locally. Summarize from local derived text. Do not casually upload screenshots.

### Weekly and monthly matter as much as yearly

The user specifically wants Wrapped for week and month, not just an annual novelty. Weekly Wrapped may be the most useful version because it can guide what to do next.

## Acceptance criteria for first usable version

A first real version is successful if:

1. The Rewind tab is replaced by Timeline in the UI.
2. Screen capture still works.
3. The UI shows activity cards grouped by time, not just a raw frame slider.
4. Selecting an activity shows screenshot/OCR evidence.
5. Search returns timeline events.
6. A weekly summary can be generated from timeline events.
7. Raw screenshots remain local and short-retention.
8. Existing Rewind endpoints/features are not broken during transition.

## Minimal first implementation

If time is limited, do this:

1. Keep `frames` as-is.
2. Add `timeline_events` table.
3. Add a simple local segmenter:
   - group frames into thirty-minute windows
   - title each event from top OCR terms/app/window if available
   - store frame IDs
4. Add `/v1/timeline/events` endpoint.
5. Change Rewind UI into a list of grouped sessions.
6. Keep screenshot viewer in detail pane.
7. Add `/v1/timeline/wrapped?period=week` that computes a basic summary from event titles and OCR.

That gets the product direction right without overbuilding.

## Non-goals for v1

- Perfect time tracking
- Perfect app detection
- Perfect project inference
- Beautiful animated Wrapped cards
- Cloud sync
- Multi-device merge
- Long-term screenshot hoarding
- Sending raw screenshots to models automatically

## Future ideas

- Calendar overlay
- Meeting transcript overlay
- Git commit/PR correlation
- Slack/iMessage/email correlation
- Project auto-detection from repo paths and browser URLs
- “What did I ship this week?” view
- “What did I keep avoiding?” view
- Attention leak detection
- Custom Wrapped exports
- Timeline memories that feed Sunday’s long-term memory selectively
- Local embeddings for private semantic search
- Per-app/site privacy exclusions
- Screenshots blurred by default until clicked

## Final direction

Replace Rewind with Sunday Timeline.

Rewind’s screenshot/OCR system remains the capture layer. Timeline becomes the product. Wrapped becomes the periodic reflection layer.

The goal is not to replay the user’s screen. The goal is to help the user understand their own work over time.
