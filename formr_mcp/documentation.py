import re

_TOPICS = {}


def _topic(name: str, title: str):
    def deco(fn):
        _TOPICS[name] = {"title": title, "get": fn}
        return fn
    return deco


def _slugify(heading: str) -> str:
    s = heading.replace("/", "-").lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    return re.sub(r"-+", "-", s)


def _parse_sections(markdown: str) -> list[dict]:
    lines = markdown.splitlines()
    sections: list[dict] = []
    for i, line in enumerate(lines):
        if line.startswith("## "):
            title = line[3:].strip()
            if sections:
                sections[-1]["end"] = i
            sections.append({"name": _slugify(title), "title": title, "start": i, "end": None})
    if sections:
        sections[-1]["end"] = len(lines)
    return sections


def get_topics() -> list[dict]:
    return [
        {"name": k, "title": v["title"]}
        for k, v in _TOPICS.items()
    ]


def get_sections(topic: str) -> list[dict]:
    """List available H2 sections within a topic."""
    entry = _TOPICS.get(topic)
    if not entry:
        available = ", ".join(_TOPICS)
        raise ValueError(f"Unknown topic '{topic}'. Available: {available}")
    sections = _parse_sections(entry["get"]())
    return [{"name": s["name"], "title": s["title"]} for s in sections]


def get_documentation(topic: str, section: str | None = None) -> str:
    entry = _TOPICS.get(topic)
    if not entry:
        available = ", ".join(_TOPICS)
        raise ValueError(f"Unknown topic '{topic}'. Available: {available}")
    markdown = entry["get"]()
    if section is None:
        return markdown
    sections = _parse_sections(markdown)
    lines = markdown.splitlines()
    for s in sections:
        if s["name"] == section:
            return "\n".join(lines[s["start"]:s["end"]])
    available = ", ".join(s["name"] for s in sections)
    raise ValueError(f"Unknown section '{section}' in topic '{topic}'. Available: {available}")


# ── Item types ──────────────────────────────────────────────────────

@_topic("item-types", "All 56 survey item types with fields")
def _item_types():
    return r"""# Survey Item Types

Items are the building blocks of a survey. In a run structure's `survey_data`,
each item is a JSON object in the `items` array.

**Choices quick reference:**
- **Required** — items that show a list of options (radio, dropdown, checkbox group, slider, weekday picker, heading)
- **Forbidden** — items that collect free-form input, binary checkboxes, dates, files, or computed values
- **Optional** — `select_or_add_multiple` (can work with inline choices or without), `sex` (auto-set to Male/Female if absent)

## JSON vs Spreadsheet

When authoring items as JSON (via `survey_data`), these differences apply:

| Aspect | Spreadsheet | JSON |
|--------|-------------|------|
| `type` + options | Inline: `number 1,100,1` | Separate: `"type":"number"`, `"type_options":"1,100,1"` |
| `optional` | `*` = optional, `!` = force-required, empty = required | `1` = optional, `0`/absent = required |
| Inline choices | `choice1..choice12` columns | `choices` object: `{"cat":"Cat","dog":"Dog"}` or array |
| Choices sheet | Separate "choices" sheet | Not needed — inline `choices` or `choice_list` reference |
| Item order | `order` column (auto-assigned) | `item_order` field (int, 1-indexed) |
| `class` | Column in spreadsheet | Same field, same CSS values |
| `label_parsed` | Not applicable (no equivalent) | Set equal to `label`, or `null` |

## Common fields (every item type)

| JSON Field | Required | Description |
|------------|----------|-------------|
| `name` | **Yes** | Variable name. Alphanumeric + underscore, must start with a letter. |
| `type` | **Yes** | The item type string (see tables below). |
| `label` | No | Question text / display text. Supports HTML. |
| `optional` | No | `1` = optional, `0` or absent = required. |
| `class` | No | Space-separated CSS class names for styling. |
| `showif` | No | R expression controlling visibility (e.g. `age >= 18`). |
| `value` | No | Pre-set value, R expression, or `sticky` (remembers last value). |
| `choice_list` | No | Reference to a named choice list defined by another item. |
| `choices` | No | Inline choices as object `{"key":"Label"}` or array `["a","b"]`. |
| `type_options` | No | Type-specific options (min, max, step, auto, maxlength, etc.). |
| `item_order` | No | Integer display order (1-indexed). |
| `block_order` | No | Block ordering field for randomization. |
| `label_parsed` | No | HTML-parsed label. Set equal to `label` or `null`. |

## Display / Layout

| Type | Stores | Choices | Description |
|------|--------|---------|-------------|
| `note` | no storage | Forbidden | Markdown info text, full width. Always optional. Never saved. |
| `note_iframe` | no storage | Forbidden | Like `note` but renders R Markdown via iframe (OpenCPU knits the label). |
| `block` | no storage | Forbidden | Blocks progress until condition met. Rendered as danger alert. |
| `submit` | no storage | Forbidden | Submit/pagination button. `type_options` = auto-submit timeout (ms) or `auto`. |

## Simple Input

| Type | MySQL type | Choices | type_options | Description |
|------|-----------|---------|--------------|-------------|
| `text` | TEXT | Forbidden | maxlength or regex pattern | Single-line text. |
| `textarea` | MEDIUMTEXT | Forbidden | maxlength or regex pattern | Multi-line text. |
| `number` | INT UNSIGNED | Forbidden | `min,max,step` (default `0,10000000,1`) | Numeric input. Step `any` = decimals. |
| `letters` | TEXT | Forbidden | maxlength | Letters only (A-Za-z + basic punctuation). |
| `email` | VARCHAR(255) | Forbidden | — | Email with server-side validation. |
| `url` | VARCHAR(255) | Forbidden | — | URL with validation. |
| `tel` | VARCHAR(100) | Forbidden | — | Telephone (no validation pattern). |
| `cc` | VARCHAR(255) | Forbidden | — | Credit card with Luhn validation. |
| `blank` | TEXT | Forbidden | — | Only label text, no input field. Can still store a value. |

## Date / Time

All accept `min,max` in type_options (e.g. `2013-01-01,2014-01-01`, `-2years,now`). Choices: **Forbidden**.

| Type | MySQL | Format | Description |
|------|-------|--------|-------------|
| `date` | DATE | Y-m-d | Date picker. |
| `datetime` | DATETIME | Y-m-d\\TH:i | Date + time. |
| `datetime-local` | DATETIME | Y-m-d\\TH:i | Local date + time (no timezone). |
| `time` | TIME | H:i | Time picker. |
| `month` | DATE | Y-m | Month + year. |
| `week` | VARCHAR(9) | Y-mW | Week + year. |
| `year` | YEAR | Y | Year. |
| `yearmonth` | DATE | Y-m-01 | Year + month (stored as 1st of month). |

## Choice / Multiple Choice

| Type | MySQL | Widget | Choices | Description |
|------|-------|--------|---------|-------------|
| `mc` | TINYINT UNSIGNED | Radio buttons | **Required** | Choose one. Values stored as index number. |
| `mc_button` | TINYINT UNSIGNED | Button group (single) | **Required** | Like `mc` but rendered as large buttons. |
| `mc_multiple` | VARCHAR(40) | Checkboxes | **Required** | Choose many. Stored comma-separated. Always optional by default. |
| `mc_multiple_button` | VARCHAR(40) | Button group (multi) | **Required** | Like `mc_multiple` with toggle buttons. |
| `mc_heading` | no storage | Column header | **Required** | Displays choices as column headings. Not stored. |
| `check` | TINYINT UNSIGNED | Single checkbox | Forbidden | Yes/no (0 or 1). Does NOT use choices. |
| `check_button` | TINYINT UNSIGNED | Single toggle button | Forbidden | Like `check` but large toggle. |
| `choose_two_weekdays` | VARCHAR(40) | Special checkboxes | **Required** | Select exactly two weekdays. Must provide 5 choices (Mon–Fri). |

## Dropdown / Select

| Type | MySQL | Choices | Description |
|------|-------|---------|-------------|
| `select_one` | TEXT | **Required** | Dropdown, choose one. |
| `select_multiple` | VARCHAR(40) | **Required** | Multi-select dropdown. Values comma-separated. |
| `select_or_add_one` | TEXT | **Required** | Dropdown + custom option (Select2). `type_options`: `maxType` (max input length). |
| `select_or_add_multiple` | TEXT | Optional | Multi-select + add custom. `type_options`: `maxType,maxChoose`. Newline-separated. |
| `timezone` | VARCHAR(255) | Forbidden | All IANA timezones. Auto-detects browser timezone. |

## Slider / Scale

| Type | MySQL | Choices | type_options | Description |
|------|-------|---------|--------------|-------------|
| `range` | INT UNSIGNED | **Required** | `min,max,step` (0,100,1) | Slider. `choice1` = left label, `choice2` = right label. Value hidden. |
| `range_ticks` | INT UNSIGNED | **Required** | `min,max,step` (0,100,1) | Slider with tick marks + tooltip. |
| `rating_button` | SMALLINT | **Required** | `min,max,step` (1,5,1) | Numbered buttons. `choice1` = left label, `choice2` = right label. Choices auto-generated from range. |
| `sex` | TINYINT UNSIGNED | Optional | — | Male/female buttons. 1 = male, 2 = female. Choices auto-set if absent. |

## Hidden / Automatic (no user input)

All choices: **Forbidden**.

| Type | MySQL | type_options | Description |
|------|-------|--------------|-------------|
| `calculate` | MEDIUMTEXT | — | Executes R expression from `value`, stores result. |
| `hidden` | MEDIUMTEXT | — | Hidden input with preset `value`. Always optional. |
| `get` | TEXT | Query param name | Captures URL query string value (default: `referred_by`). |
| `server` | TEXT | $_SERVER key | Captures server variable (default: `HTTP_USER_AGENT`). |
| `browser` | TEXT | — | Alias for `server`. |
| `ip` | VARCHAR(46) | — | Stores participant's IP address. |
| `referrer` | TEXT | — | Stores last outside referrer URL. |
| `random` | INT UNSIGNED | `min,max` (0,1) | Generates random integer via `mt_rand()`. |

## File Upload

All accept optional `type_options` = max file size in MB. Choices: **Forbidden**.

| Type | Default accept | Description |
|------|----------------|-------------|
| `file` | image, video, audio, text, PDF | General file upload. Embeds as img/audio/video or link. |
| `image` | images + `capture=camera` | Image upload. Triggers camera on mobile. |
| `audio` | audio + `capture=microphone` | Audio upload. Uses microphone. |
| `video` | video + `capture=camcorder` | Video upload. Triggers camcorder. |

## Other

Choices: **Forbidden**.

| Type | MySQL | Description |
|------|-------|-------------|
| `geopoint` | TEXT | GPS coordinates via browser geolocation API. Read-only. |
| `color` | CHAR(7) | Color picker. Validates `#RRGGBB`. |

## PWA Family

| Type | MySQL | Choices | Description |
|------|-------|---------|-------------|
| `request_phone` | VARCHAR(20) | Forbidden | Detects mobile vs desktop. Shows QR code for phone transition. |
| `add_to_home_screen` | VARCHAR(20) | **Required** | Button to add PWA to home screen. |
| `push_notification` | TEXT | **Required** | Button to request push permission. Stores subscription JSON. |
| `request_cookie` | VARCHAR(20) | **Required** | Button for functional cookie consent. |

## Type Options (spreadsheet → JSON mapping)

In JSON, `type_options` is always a separate string field. When reading data
exported from spreadsheets, the type string may include inline options:

| Spreadsheet type column | JSON equivalent |
|------------------------|-----------------|
| `number 1,100,1` | `"type": "number", "type_options": "1,100,1"` |
| `text 100` | `"type": "text", "type_options": "100"` |
| `mc agreement` | `"type": "mc", "choice_list": "agreement"` |
| `submit auto` | `"type": "submit", "type_options": "auto"` |

When authoring JSON directly, always use the separate `type_options` and
`choice_list` fields — never embed options in the `type` string.

## Choice Lists

In JSON, define choices inline via the `choices` field (object or array)
or reference another item's choices via `choice_list`. There is no separate
choices sheet — the `choices` field on any item serves as the definition.
"""


# ── Run concepts ────────────────────────────────────────────────────

@_topic("run-concepts", "How runs work — flow, positions, branching, timing")
def _run_concepts():
    return r"""# Run Concepts

## What is a run?

A **run** is an ordered composition of **units** that participants traverse.
Think of it as a study timeline. Units execute sequentially by `position`.
The participant moves from one position to the next unless a branch redirects them.

## Key rules

- **Units are ordered by `position`** — an integer field. Lower positions execute first.
- **Sequential flow by default** — when a unit completes, the participant moves to the next higher position.

> **⚠️ CRITICAL: Page and Endpage units permanently end the run session.**
> `Page` and `Endpage` set `end_session = true` and `end_run_session = true`.
> This means the participant **cannot advance past them** — there is no "continue"
> button. **Every unit after a Page/Endpage that isn't reached via a branch
> is unreachable.** If you need a Pause that participants can wait at and then
> continue, use `Pause` (not `Page`). If you need informational text followed by
> more units, put it in a `Survey` note item or a `Pause` body — never a `Page`.

> **Note on Page vs Endpage:** `Page` and `Endpage` are the same unit type in formr.
> The PHP class is `Page` but the stored type is `'Endpage'`. When you create a `Page`
> unit and upload it, formr normalizes the type to `Endpage`. Downloaded structures
> will always show `type='Endpage'`. Use `Endpage` in your run structures — it's the
> canonical name. `Page` is accepted as an alias.

- **Branches redirect** — `Branch`, `SkipForward`, and `SkipBackward` use an R `condition` expression.
  If `TRUE`, the participant jumps to the `if_true` position.
- **Surveys collect data** — a `Survey` unit links to a survey definition (either existing `study_id` or inline `survey_data`).
- **Emails are sent automatically** — `Email` units fire when reached (or on cron for `cron_only=1`).
- **Pauses wait** — `Pause` units stall progress until a time, date, or duration passes. No user interaction; the page displays the `body` content.
- **Waits are interactive pauses** — `Wait` units also stall progress, but participants can click through to advance early. The `body` field stores the **position number** to jump to on click.
- **External units execute R or redirect** — `External` units either redirect to a URL, or **execute full R code** (via OpenCPU) when the `address` field doesn't start with `http`. This enables SMS gateway calls, phone number transformation, and other dynamic logic.

## Position spacing

Use spaced positions (10, 20, 30...) so you can insert units between existing ones later.
Positions are integers. Duplicate positions are not allowed.

## Unit flow diagram

```
position 10: Survey "demographics"
    ↓ (auto-advance after submit)
position 20: SkipForward "age check"
    ├─ condition: age < 18 → if_true: 40 (skip to end)
    ↓ (condition FALSE)
position 30: Survey "main study"
    ↓
position 40: Endpage "thank you"
```

## Wait units — the key to ESM/diary designs

`Wait` extends `Pause` with **interactive advancement**: participants can click through
before the timer expires. This is essential for experience sampling and diary studies
where participants should be able to start a survey when they receive a notification.

### How Wait.body works

The `body` field on a `Wait` unit is **not** display content — it is the **integer position**
the participant jumps to when they click through. This enables the "advance-on-click" pattern:

```
Wait (body: survey_position, wait_minutes: 90)
  → Participant clicks within 90 min → jumps to survey_position
  → 90 min passes → timer expires → advance to next unit (e.g., reminder)
```

### ESM beep pattern

The standard ESM pattern uses SkipForward + Wait + Email + Survey for each beep:

```
position 10: SkipForward (if hour >= 12 → jump to beep 2)
position 20: Wait (body: 60, wait_minutes: 90) — advance to survey on click
position 30: Email (cron_only: 1) — reminder, only sent by cron
position 40: Survey "esm"
position 50: SkipBackward or continue to next beep
```

- **SkipForward** skips the beep if the time window has passed (e.g., `hour(now()) >= 12`)
- **Wait** lets participants click through to the survey within the time window
- **Email** (with `cron_only: 1`) sends the reminder only via cron, not on participant visit
- After the ESM survey, logic loops back or advances to the next beep

## Pause/Wait timing with relative_to

The `relative_to` field on Pause/Wait units is an **R expression** that determines
the base timestamp. The unit expires at `base_timestamp + wait_minutes`.

- If `relative_to` is empty and `wait_minutes` is set, the default is
  `tail(survey_unit_sessions$created, 1)` — the timestamp of the most recent unit session.
- If `relative_to` returns a **POSIXct timestamp**, the pause expires at
  `timestamp + wait_minutes * 60`.
- If `relative_to` returns **TRUE**, the pause expires immediately.
- If `relative_to` returns **FALSE**, the pause never expires (waits indefinitely).
- `wait_until_time` and `wait_until_date` are additional constraints — the pause
  won't expire until the specified time-of-day and/or date are also met.

### Common relative_to expressions

```r
# Wait until next day 9 AM:
time_passed(hours = 12) && hour(now()) >= 9

# Wait until 9 AM on the day after the baseline survey:
library(lubridate)
time_passed(hours = 1) && hour(now()) >= 9 &&
  date(now()) > as.Date(baseline$created)

# Pause for 3 hours from when SMS was sent:
tail(survey_unit_sessions$created, 1)

# Check if it's within a time window (for SkipForward conditions):
hour(now()) >= 9 && hour(now()) < 12
```

## System variables available in R expressions

### survey_unit_sessions

A data frame tracking every unit visit for the **current participant**. Key columns:

> **SCOPE**: Contains ONLY the current participant's rows. To access data across
> ALL participants, use the formr V1 API anywhere in run R code (see §10 below).

| Column | Type | Description |
|--------|------|-------------|
| `id` | int | Unit session ID |
| `unit_id` | int | Which unit definition |
| `run_session_id` | int | Which participant run session |
| `created` | datetime | When this unit session was created |
| `ended` | datetime | When ended (NULL = still active) |
| `result` | varchar | Human-readable result (e.g., 'survey_started', 'pause_ended') |

Common patterns:
```r
# When was the SMS last sent? (position of the External unit)
tail(survey_unit_sessions[survey_unit_sessions$position == 55, ]$created, 1)

# Has the participant visited any ESM survey yet?
nrow(esm) > 0

# How long since last ESM completion?
as.numeric(difftime(now(), tail(esm$created, 1), units = "hours")) > 24
```

### survey_run_sessions

| Column | Type | Description |
|--------|------|-------------|
| `session` | varchar(64) | Unique session code (the participant's login code) |
| `created` | datetime | When the run session was created |
| `ended` | datetime | When ended (NULL = still active) |
| `position` | smallint | Current position in the run |

Use `survey_run_sessions$session` to construct personalized links for other runs
(e.g., acquaintance surveys).

### .formr special variables

These are available in R code evaluated by OpenCPU:
- `.formr$login_code` — the participant's session code (= their login link code)
- `.formr$last_action_time` — timestamp of the participant's last action
- `.formr$nr_of_participants` — total number of participants in the run
- `.formr$secret_<name>` — run secrets (only injected if referenced in code)

## Common patterns

### Filter (SkipForward)
SkipForward at position 10 checks a condition. If TRUE, jump to position 30 (main study).
If FALSE, fall through to position 20 (end page).

### Diary loop (SkipBackward)
After the diary survey at position 40, a SkipBackward at 50 checks if enough entries exist.
If not enough, jump back to position 20 (pause until next reminder).

### Longitudinal study
Survey (wave 1) → Pause (1 year) → Email (invitation) → Survey (wave 2) → Endpage.

### Reminder system (Wait + Email)
```
Wait (body: 30, wait_minutes: 60)
  → participant clicks → jumps to position 30 (survey)
  → 60 min timer expires → advances to position 20 (email)
Email (cron_only: 1)  → sends reminder only via cron
```
The Wait's `body = 30` means "if the participant arrives, redirect them to position 30."
The Email's `cron_only = 1` means "only send this via cron, not when the participant visits."

### 24-hour dropout detection
```r
# In a SkipForward condition — skip to exclusion if inactive for 24+ hours
nrow(esm) > 0 && as.numeric(difftime(now(), tail(esm$created, 1), units = "hours")) > 24
```

### Reminder deduplication
```r
# In a SkipForward condition — skip reminder if one was already sent today
length(survey_unit_sessions[survey_unit_sessions$position == 60, ]$created) > 0 &&
  date(tail(survey_unit_sessions[survey_unit_sessions$position == 60, ]$created, 1)) == today()
```

## Settings

Use `update_run_settings(name, settings)` to change a run's settings.
Pass only the fields you want to change. Available settings:

| Field | Type | Description |
|-------|------|-------------|
| `title` | string | Study display title |
| `description` | string | Markdown study description |
| `footer_text` | string | Markdown footer text |
| `public_blurb` | string | Text shown on public run listing |
| `privacy` | string | Markdown privacy policy |
| `tos` | string | Markdown terms of service |
| `header_image_path` | string | Path to header image |
| `custom_css` | string | Custom CSS content (written to file) |
| `custom_js` | string | Custom JavaScript content (written to file) |
| `custom_r` | string | Custom R functions (written to file) |
| `cron_active` | 0/1 | Whether cron daemon processes this run |
| `use_material_design` | 0/1 | **Forbidden** — must be 0 or omitted. Legacy theme, use custom_css instead. |
| `expiresOn` | string | Run expiry date (YYYY-MM-DD, must be future) |
| `expire_cookie_value` | int | Cookie lifetime value (combined with unit) |
| `expire_cookie_unit` | string | Cookie lifetime unit: seconds, minutes, hours, days, months, years |
| `public` | int | Visibility: 0=admin/test-users only, 2=accessible with link; 1 and 3 are rarely used |
| `locked` | 0/1 | Lock run to prevent modifications |

### Clearing settings

When clearing a run setting field (e.g. `header_image_path`), passing `null` via
`update_run_settings` may be silently ignored by the formr API. Use an empty
string `""` instead:

```
# Does NOT clear the field:
update_run_settings(name, {"header_image_path": null})

# Correctly clears the field:
update_run_settings(name, {"header_image_path": ""})
```
"""


# ── R code ──────────────────────────────────────────────────────────

@_topic("r-code", "R code in formr — conditions, values, knitr, timing, secrets")
def _r_code():
    return r"""# R Code in formr

formr evaluates R code via **OpenCPU** for dynamic content. R is used in:

## 1. Branch conditions

The `condition` field on Branch/SkipForward/SkipBackward units is an R expression
that must evaluate to `TRUE` (jump) or `FALSE` (continue):

```r
nrow(diary) < 20
```

```r
age >= 18
```

## 2. Item values (`value` column)

The `value` column on items can be an R expression. The `calculate` item type
always executes its `value` as R:

| Value | Effect |
|-------|--------|
| `nrow(friend_rate)` | Count of previous entries in this survey |
| `friend_list[, paste0("name_friend", friend_nr)]` | Lookup from another survey |
| `sticky` | Preserves last entered value |
| `ifelse(sex, "his", "her")` | Conditional text |
| `now()` | Current timestamp |

## 3. Showif conditions

The `showif` column controls item visibility. It's an R expression that must
evaluate to `TRUE` (show) or `FALSE` (hide):

```r
age >= 18
```

```r
nrow(friend_rate) < friend_list$nr_friends
```

Use `//js_only` prefix for JavaScript-only conditions (no server round-trip):
```r
//js_only
nr_friends >= 1
```

## 4. Labels and body content (knitr)

Labels, Pause/Endpage bodies, and email bodies support R **knitr** inline code.
Wrap R code in:

- Inline: `` `r expr` `` — e.g. `` `r ifelse(sex == 1, "Lieber Teilnehmer", "Liebe Teilnehmerin")` ``
- Block: ```` ```{r} code ``` ```` — e.g. for plots, tables, datatables

formr renders knitr content by sending it to OpenCPU with a settings prefix:
```r
```{r settings, include=FALSE}
library(knitr); library(formr)
opts_chunk$set(warning=FALSE, message=FALSE, error=FALSE, echo=FALSE)
```
```

This means `library(formr)` is always available in knitr rendering.

### Knitr in email bodies

Email bodies also support knitr. formr renders them as self-contained HTML emails
with `fig.retina=2` for high-quality plots. Images are embedded as CID attachments.
When `cron_only=1`, the email is only sent by the cron daemon — not during a
participant's browser visit. This is essential for reminder emails in diary/ESM studies.

### Knitr in Pause/Wait bodies

Pause body content is rendered via knitr when it contains R code (`` `r ` `` or
`` ```{r} ``). For Pause units, `body` is display content. For Wait units, `body`
is the position number to jump to on click (not display content).

## 5. Pause/Wait timing with relative_to

The `relative_to` field on Pause/Wait units is an **R expression** that determines
when the pause expires:

- Returns a **POSIXct timestamp** → pause expires at `timestamp + wait_minutes * 60`
- Returns **TRUE** → pause expires immediately
- Returns **FALSE** → pause never expires (waits indefinitely)
- If empty with `wait_minutes` set → defaults to `tail(survey_unit_sessions$created, 1)`

Common timing patterns:

```r
# Wait until next day 9 AM:
time_passed(hours = 1) && hour(now()) >= 9

# Wait 3 hours from when the last unit was visited, only during 9am-10pm:
library(lubridate)
time_passed(minutes = 180) && hour(now()) > 9 && hour(now()) < 22

# Wait until a specific condition on survey data:
date(now()) > as.Date(baseline$created) && hour(now()) >= 9
```

## 6. Referencing survey data

Access previously collected data using `survey_name$variable_name`:

- `demographics$age` — the `age` variable from the `demographics` survey
- `nrow(diary)` — number of rows submitted for the `diary` survey
- `friend_list[, "name_friend1"]` — specific column from another survey
- `tail(diary$created, 1)` — timestamp of the most recent diary entry
- `diary$created` vector — all submission timestamps for the `diary` survey

System columns available in every survey: `created`, `ended`, `modified`.

## 7. survey_unit_sessions — tracking unit visits

`survey_unit_sessions` is a system data frame tracking **every unit visit** for the
**current participant only**. It does NOT contain rows for other participants.
To access data across all participants, use the formr V1 API anywhere in run R code (§8).

Key columns:

| Column | Description |
|--------|-------------|
| `created` | Datetime when this unit session was created |
| `ended` | When the session ended (NULL = still active) |
| `position` | Run position of the unit (not in base schema, accessible via joins) |
| `result` | Human-readable result string |

Common patterns:

```r
# When was the SMS at position 55 last sent?
tail(survey_unit_sessions[survey_unit_sessions$position == 55, ]$created, 1)

# Has a reminder (position 60) already been sent today?
date(tail(survey_unit_sessions[survey_unit_sessions$position == 60, ]$created, 1)) == today()

# What hour was the last SMS sent?
hour(last(survey_unit_sessions[survey_unit_sessions$position == 55, ]$created))
```

## 8. External unit R code

When an External unit's `address` field does **not** start with `http`, it is
evaluated as R code via OpenCPU. This enables:

- SMS gateway API calls via `httr::GET()`
- Phone number transformation
- Conditional redirects (return a URL string to redirect, or `FALSE` to move on)
- Cross-participant data queries via the formr V1 API (`formr_api_authenticate` + `formr_api_results`/`formr_api_sessions`)

The R expression has access to all the participant's run data, just like conditions.
The return value determines behavior:
- Returns **FALSE** → no redirect, advance to next unit
- Returns a **URL string** → participant is redirected to that URL
- Any other value → logged, advance to next unit

The `api_end` field controls what happens after execution:
- `api_end = 0` (default): formr immediately ends the unit session and moves on.
  Use this for fire-and-forget actions (e.g., sending an SMS).
- `api_end = 1`: formr does NOT auto-advance. It waits for the external service
  to call back via the formr API. Use this for integrations that need confirmation.

## 9. Run secrets

Never hardcode API credentials in External unit code. Use the run's `secrets`
setting to store them securely:

1. Add secret names (not values) to the run settings: `"secrets": ["sms_api_key", "sms_password"]`
2. Fill in the actual values in the formr admin interface (they're encrypted at rest)
3. Reference them in R code as `.formr$secret_<name>`:
```r
# In an External unit's R code:
sms_key <- .formr$secret_sms_api_key
sms_pw <- .formr$secret_sms_password
httr::GET(paste0("https://sms-gateway.example/send?user=", sms_key, "&pw=", sms_pw, "&to=", phone))
```

Secrets are conditionally injected — only those referenced in code are included
in the OpenCPU evaluation. Error messages are automatically redacted to prevent
leaking secret values.

## 10. Cross-participant data access (V1 API)

`survey_unit_sessions` and survey data frames contain **only the current participant's
data**. To query data across all participants, use the formr V1 API (`formr_api_*`).

**Auto-injection**: Whenever formr evaluates R code that contains `formr_api_authenticate(`,
it automatically mints a short-lived OAuth token (180 s, `data:read` scope) for the run
owner and injects it into `.formr$access_token`, `.formr$host`, and `.formr$run_name`
before the code runs. Call `formr_api_authenticate()` with **no arguments** — it picks
up the injected token automatically. No secrets to configure.

This works in **all R evaluation contexts within a run**: Branch/SkipForward/SkipBackward
conditions, survey item `value` and `showif` fields, item and choice `label` knitr
rendering, Page/Endpage `body` content, Email unit `body` content, and External unit R
code. The only exception is plaintext evaluation (email subject lines, push notification
titles), which does not go through the R evaluation pipeline.

The auto-injected token is **restricted to the current run**. For accessing a different
run, store credentials as run secrets (§9) and pass them explicitly.

```r
library(formr)

# ── Current run: token auto-injected, no secrets needed ──────────────────────
formr_api_authenticate()

all_sessions <- formr_api_sessions(.formr$run_name)
nrow(all_sessions)  # total session count

all_results <- formr_api_results(.formr$run_name)
nrow(all_results)   # participants with response data

# ── Different run: store credentials as run secrets (§9) ──────────────────────
formr_api_authenticate(
  client_id     = .formr$secret_api_client_id,
  client_secret = .formr$secret_api_client_secret,
  host          = .formr$host
)
other_data <- formr_api_results("other-run-name")
nrow(other_data)
```

Never use the deprecated `formr_connect()` / `formr_raw_results()` — use `formr_api_*` exclusively.

For R code that researchers run in **their own RStudio session** (not inside a formr
run), the same `formr_api_*` functions apply with explicit client_id, client_secret, and host.

## 11. Available R packages

formr's OpenCPU instance has many R packages pre-installed, including:
`lubridate`, `ggplot2`, `dplyr`, `tidyr`, `DT`, `knitr`, `formr`, `httr`,
`stringr`, `purrr`, `psych`, `data.table`, and others from CRAN.

## 12. Robust R condition patterns

Real studies need defensive R code that handles missing data, empty data frames,
and edge cases. Here are proven patterns:

### Safe data frame existence checks
```r
# Check if a survey has been completed at all
if (!exists("my_survey") || is.null(my_survey) || nrow(my_survey) == 0) {
    FALSE  # survey not yet completed
}
```

### Safe last-value access
```r
# Access the most recent value, handling NULL/empty cases
if (length(my_survey$variable) == 0 || all(is.na(my_survey$variable))) {
    FALSE  # no data available
} else {
    tail(my_survey$variable, 1) == 1
}
```

### 24-hour inactivity detection
```r
nrow(esm) > 0 &&
as.numeric(difftime(now(), tail(esm$created, 1), units = "hours")) > 24
```

### Day counting from baseline
```r
library(lubridate)
as.numeric(difftime(today(), as.Date(tail(baseline$created, 1)), units = "days")) >= 5
```

### Named R function in conditions
For complex logic, define a function and call it:
```r
is_eligible <- function() {
    if (is.null(screening)) return(FALSE)
    screening$age >= 14 && screening$age <= 18 &&
    screening$german == 1
}
is_eligible()
```

### recipient_field patterns in Email units
The `recipient_field` on Email units can be:
- `"most recent reported address"` (default) — uses the most recent email-type item
- `"survey_name$item_name"` — references a specific email item (e.g., `"baseline$email_address"`)
- An R expression that returns an email address string
"""


# ── Survey JSON ─────────────────────────────────────────────────────

@_topic("survey-json", "How to author surveys as JSON (inline survey_data)")
def _survey_json():
    return r"""# Survey JSON Authoring

Surveys can be embedded directly in a run structure via the `survey_data`
field on a Survey unit, instead of uploading a separate spreadsheet. The
`survey_data` field is a JSON object containing the survey name, items,
and settings.

## Structure

```json
{
    "type": "Survey",
    "position": 10,
    "description": "demographics survey",
    "survey_data": {
        "name": "demographics",
        "items": [ ... ],
        "settings": { ... }
    }
}
```

## Items Array

Each item in `items` is a JSON object with these fields:

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `type` | **Yes** | string | Item type (see the item-types topic) |
| `name` | **Yes** | string | Variable name. Alphanumeric + underscore, must start with a letter. |
| `label` | **Yes** | string | Question text / display text. Supports HTML. |
| `optional` | No | int | `0` = required, `1` = optional. Default `0`. |
| `class` | No | string | Space-separated CSS class names. |
| `showif` | No | string | R expression controlling visibility (e.g. `age >= 18`). |
| `value` | No | string | Pre-set value, R expression, or `sticky` (remembers last value). |
| `choice_list` | No | string/null | Reference to a named choice list (from `choices` on another item). |
| `type_options` | No | string/null | Type-specific options (min,max,step, auto, maxlength, etc.). |
| `choices` | No | object or array/null | Inline choices (see below). |
| `block_order` | No | string | Block ordering field for randomization. |
| `item_order` | No | int | Display order (1-indexed, sequential). |
| `label_parsed` | No | string/null | HTML-parsed version of label. Set equal to `label` when creating; can be `null`. |

## Choices

Choices can be specified in two formats:

**Object format** (preferred):
```json
{
    "type": "mc",
    "name": "favorite_color",
    "label": "What's your favorite?",
    "choices": {
        "red": "Red",
        "blue": "Blue",
        "green": "Green"
    }
}
```

**Array format** (legacy, values = labels):
```json
{
    "type": "mc",
    "name": "datenschutz",
    "label": "Do you consent?",
    "choices": ["nein", "ja"]
}
```

When using `choice_list` instead of inline `choices`, reference another item's
name or a shared list name:
```json
{
    "type": "mc",
    "name": "item1",
    "label": "First item",
    "choice_list": "likert5",
    "choices": {
        "1": "Strongly disagree",
        "2": "Disagree",
        "3": "Neutral",
        "4": "Agree",
        "5": "Strongly agree"
    }
},
{
    "type": "mc",
    "name": "item2",
    "label": "Second item",
    "choice_list": "likert5"
}
```

## Type Options

In JSON, `type_options` is always a **separate string field**. Unlike the
spreadsheet format, you do NOT append options to the type string:

| Spreadsheet | JSON equivalent |
|-------------|-----------------|
| `number 1,100,1` | `"type": "number", "type_options": "1,100,1"` |
| `text 100` | `"type": "text", "type_options": "100"` |
| `submit auto` | `"type": "submit", "type_options": "auto"` |

## Optional Flag

In JSON, `optional` is an integer:
- `0` or absent = required
- `1` = optional
- (The spreadsheet `*` and `!` conventions do not apply to JSON.)

## Page Breaks

Same as spreadsheets: a `submit` item creates a page break. Items before it
are rendered as one page. Multiple `submit` items = multi-page survey.

```json
{"type": "submit", "name": "page1", "label": "Continue"}
```

**Do not use Custom Paging.** The `use_paging` and `page_items` settings are
legacy features that are no longer supported. Always use `submit` items to
create page breaks.

## Settings Object

The `settings` object (optional) controls survey behavior:

```json
"settings": {
    "maximum_number_displayed": 0,
    "displayed_percentage_maximum": 100,
    "add_percentage_points": 0,
    "enable_instant_validation": 0,
    "expire_after": 0,
    "google_file_id": null,
    "unlinked": 0,
    "expire_invitation_after": 0,
    "expire_invitation_grace": 0,
    "hide_results": 0
}
```

Most settings default to `0`/`false`. The key ones:
- `enable_instant_validation` — validate on blur (1) or on submit (0)
- `add_percentage_points` — show progress bar (%)
- `displayed_percentage_maximum` — progress bar max
- `unlinked` — allow anonymous (unlinked) sessions
- `hide_results` — hide results from participants

**Forbidden settings:**
- `use_paging` — Do NOT set this to 1. Custom Paging is a legacy feature that is no longer supported. Use `submit` items in the item list to create page breaks instead.
- `page_items` — Do NOT use. This is part of the legacy Custom Paging feature. Use `submit` items to create page breaks.
- `use_material_design` — Do NOT set this to 1. Material Design is a legacy theme that is not supported. Use `custom_css` to style the survey instead.

## Full Example

```json
{
    "type": "Survey",
    "description": "welcome and consent",
    "position": 10,
    "special": "",
    "survey_data": {
        "name": "welcome",
        "items": [
            {
                "type": "note",
                "name": "note_welcome",
                "label": "<h1>Welcome!</h1>",
                "optional": 1,
                "class": "label_align_center",
                "showif": "",
                "value": "",
                "block_order": "",
                "item_order": 1
            },
            {
                "type": "mc_button",
                "name": "consent",
                "label": "Do you consent?",
                "optional": 0,
                "class": "",
                "showif": "",
                "value": "",
                "choice_list": null,
                "type_options": null,
                "block_order": "",
                "item_order": 2,
                "choices": {
                    "1": "Yes",
                    "2": "No"
                }
            },
            {
                "type": "number",
                "name": "age",
                "label": "How old are you?",
                "optional": 0,
                "class": "",
                "showif": "consent == 1",
                "value": "",
                "choice_list": null,
                "type_options": "0,120,1",
                "block_order": "",
                "item_order": 3
            },
            {
                "type": "submit",
                "name": "page1",
                "label": "Continue",
                "optional": 0,
                "class": "",
                "showif": "",
                "value": "",
                "choice_list": null,
                "type_options": null,
                "block_order": "",
                "item_order": 4
            }
        ],
        "settings": {
            "maximum_number_displayed": 0,
            "displayed_percentage_maximum": 100,
            "add_percentage_points": 1,
            "enable_instant_validation": 0,
            "expire_after": 0,
            "google_file_id": null,
            "unlinked": 0,
            "expire_invitation_after": 0,
            "expire_invitation_grace": 0,
            "hide_results": 0
        }
    }
}
```

## CSS Classes

Use the `class` field for layout control (same as spreadsheet):

| Class | Effect |
|-------|--------|
| `clear` | New line before this item |
| `right150` | Float right, 150px wide |
| `align_horizontally` | Display items side by side |
| `label_as_placeholder` | Use label as input placeholder text |
| `hide_label` | Hide the label, show only input |
| `label_align_center` | Center-align the label |
| `answer_align_center` | Center-align answer options |
| `mc_vertical` | Stack MC options vertically |
| `mc_width80` | Set MC item width to 80% |
| `right_offset100` | Right offset 100px |
| `space_bottom_30` | Bottom margin 30px |
| `hidden` | Visually hidden (useful for auto-submit) |

## Key Differences from Spreadsheet Authoring

1. No choices sheet — inline `choices` or `choice_list` references instead
2. No `choice1..choice12` columns — use `choices` object instead
3. `type_options` is a separate field, not appended to the type string
4. `optional` is `0`/`1` integer, not `*`/`!`
5. `label_parsed` should mirror `label` or be `null`
6. Items use `item_order` (int) instead of spreadsheet row `order`
7. No separate Google Sheets import — JSON is uploaded as part of the run structure
"""


# ── Examples ────────────────────────────────────────────────────────

@_topic("examples", "Example run structures you can learn from")
def _examples():
    return r"""# Example Run Structures

These are working examples from the formr documentation. Use them as reference
for designing your own runs.

---

## 1. Basic Diary

A daily diary study with reminder emails and a loop.

```json
{
    "name": "BasicDiary",
    "units": [
        {
            "type": "Survey",
            "description": "demographics and contact info",
            "position": 10,
            "special": ""
        },
        {
            "type": "Pause",
            "description": "diary beginning: a pause until the diary is accessible",
            "position": 20,
            "special": "",
            "wait_until_time": "17:00:00",
            "body": "## Thank you for your participation\r\n\r\nWe will invite you to participate again tomorrow around 5pm."
        },
        {
            "type": "Email",
            "description": "diary invitation: sent after the pause above expires",
            "position": 30,
            "special": "",
            "account_id": 2,
            "subject": "Diary invitation",
            "recipient_field": "most recent reported address",
            "body": "Dear participant,\r\n\r\nplease fill out your diary now.\r\n\r\n{{login_link}}"
        },
        {
            "type": "Survey",
            "description": "diary: main diary survey (this one is repeated)",
            "position": 40,
            "special": ""
        },
        {
            "type": "SkipBackward",
            "description": "end of diary loop",
            "position": 50,
            "special": "",
            "condition": "nrow(diary) < 20",
            "if_true": 20
        },
        {
            "type": "Endpage",
            "description": "end of study",
            "position": 60,
            "special": "",
            "body": "## It's over\r\n\r\nThanks for participating."
        }
    ]
}
```

**Pattern**: Survey → Pause → Email → Survey → SkipBackward(→Pause) → Endpage.

---

## 2. Experience Sampling (ESM)

Random-interval reminders throughout the day.

```json
{
    "name": "Experience_sampling",
    "units": [
        {
            "type": "Survey",
            "description": "get email, intro survey",
            "position": 10
        },
        {
            "type": "Pause",
            "description": "wait random time between 5-100min, only 9am-10pm",
            "position": 20,
            "relative_to": "library(lubridate)\r\nx_minutes = sample(5:100, 1)\r\nhour_now = hour(now())\r\ntime_passed(minutes = x_minutes) && hour_now > 9 && hour_now < 22",
            "body": "# Wait\r\nPlease wait for your first reminder."
        },
        {
            "type": "Email",
            "description": "reminder",
            "position": 30,
            "account_id": 1,
            "subject": "Time to tell us about your experiences!",
            "recipient_field": "most recent reported address",
            "body": "Time to tell us!\r\n\r\n{{login_link}}"
        },
        {
            "type": "Survey",
            "description": "experience sampling survey",
            "position": 40
        },
        {
            "type": "SkipBackward",
            "description": "loop the experience sampling",
            "position": 50,
            "condition": "nrow(experience_survey) < 100",
            "if_true": 30
        },
        {
            "type": "Endpage",
            "description": "end",
            "position": 60,
            "body": "kthxbye"
        }
    ]
}
```

**Key**: Uses `relative_to` with `lubridate` for random intervals, and `hour(now())` for time-of-day gating.

---

## 3. ESM with Wait Units and Reminder (Production Pattern)

This is the production-tested pattern for experience sampling studies.
It uses Wait units for click-through windows, Email for cron-sent reminders,
SkipForward for time-gating, and SkipBackward for daily looping.

```json
{
    "name": "ESM_with_reminders",
    "units": [
        {
            "type": "Survey",
            "description": "baseline: collect contact info",
            "position": 10
        },
        {
            "type": "Pause",
            "description": "wait until next day 9AM before ESM starts",
            "position": 20,
            "wait_minutes": 600,
            "relative_to": "library(lubridate)\ntime_passed(hours = 1) && hour(now()) >= 9 && date(now()) > as.Date(baseline$created)",
            "body": "## See you tomorrow!\n\nThe ESM phase starts tomorrow at 9:00 AM."
        },
        {
            "type": "SkipForward",
            "description": "ESM beep 1: skip if past 12:00",
            "position": 25,
            "condition": "hour(now()) >= 12",
            "if_true": 50,
            "automatically_jump": 1,
            "automatically_go_on": 1
        },
        {
            "type": "Wait",
            "description": "ESM beep 1: wait up to 90 min for participant to click",
            "position": 30,
            "wait_minutes": 90,
            "body": 45
        },
        {
            "type": "Email",
            "description": "ESM beep 1: reminder email (cron only)",
            "position": 40,
            "account_id": 1,
            "subject": "Time to tell us about your experiences!",
            "recipient_field": "most recent reported address",
            "body": "Your morning questionnaire is ready!\n\n{{login_link}}",
            "cron_only": 1
        },
        {
            "type": "Survey",
            "description": "ESM survey",
            "position": 45
        },
        {
            "type": "SkipForward",
            "description": "ESM beep 2: skip if past 15:00",
            "position": 50,
            "condition": "hour(now()) >= 15",
            "if_true": 70,
            "automatically_jump": 1,
            "automatically_go_on": 1
        },
        {
            "type": "Wait",
            "description": "ESM beep 2: wait up to 90 min",
            "position": 55,
            "wait_minutes": 90,
            "body": 80
        },
        {
            "type": "Email",
            "description": "ESM beep 2: reminder (cron only)",
            "position": 60,
            "account_id": 1,
            "subject": "Afternoon questionnaire",
            "recipient_field": "most recent reported address",
            "body": "Time for your afternoon check-in!\n\n{{login_link}}",
            "cron_only": 1
        },
        {
            "type": "Survey",
            "description": "ESM survey",
            "position": 65
        },
        {
            "type": "Pause",
            "description": "overnight pause until next 9AM",
            "position": 70,
            "wait_minutes": 600,
            "relative_to": "library(lubridate)\ntime_passed(hours = 1) && hour(now()) >= 9",
            "body": "See you tomorrow!"
        },
        {
            "type": "Survey",
            "description": "ESM survey (afternoon, second instance)",
            "position": 75
        },
        {
            "type": "Pause",
            "description": "overnight pause (alternate path)",
            "position": 80,
            "wait_minutes": 600,
            "relative_to": "library(lubridate)\ntime_passed(hours = 1) && hour(now()) >= 9",
            "body": "See you tomorrow!"
        },
        {
            "type": "SkipForward",
            "description": "24hr dropout: if inactive for 24+ hours, end study",
            "position": 85,
            "condition": "nrow(esm) > 0 && as.numeric(difftime(now(), tail(esm$created, 1), units='hours')) > 24",
            "if_true": 110,
            "automatically_jump": 1,
            "automatically_go_on": 1
        },
        {
            "type": "SkipBackward",
            "description": "loop back to beep 1 if < 10 days and < 50 completions",
            "position": 90,
            "condition": "nrow(esm) < 50 && as.numeric(difftime(now(), baseline$created, units='days')) < 10.5",
            "if_true": 25
        },
        {
            "type": "Endpage",
            "description": "dropout: 24hr inactivity",
            "position": 110,
            "body": "We haven't heard from you in over 24 hours. Thank you for your participation so far."
        }
    ]
}
```

**Key patterns demonstrated:**
- **Wait `body` positions**: `body: 45` means "if participant clicks within 90 min, jump to position 45 (survey)"
- **SkipForward time gates**: skip entire beeps if the time window has passed
- **`cron_only: 1` on Email**: reminder emails are only sent by the cron daemon, not during participant visits
- **24-hour dropout**: SkipForward checks `difftime()` against last ESM completion
- **Loop condition**: SkipBackward checks both completion count and days elapsed since baseline

---

## 4. Filter / Screening

Skip participants who don't meet criteria.

```json
{
    "name": "Filter",
    "units": [
        {
            "type": "SkipForward",
            "description": "Filter condition, those with true can go on",
            "position": 10,
            "condition": "survey1$variable == 1",
            "automatically_jump": 1,
            "if_true": 30,
            "automatically_go_on": 1
        },
        {
            "type": "Endpage",
            "description": "Filtered people end here.",
            "position": 20,
            "title": "Filtered",
            "body": "You can't take part."
        }
    ]
}
```

**Pattern**: SkipForward (check condition → jump to 30) → Endpage (if condition false).

---

## 4. Reminder System

Send a reminder if participant hasn't responded within 60 minutes.

```json
{
    "name": "Reminder",
    "units": [
        {
            "type": "Wait",
            "description": "send a reminder if participant doesn't react within 60 minutes",
            "position": 10,
            "wait_minutes": 60,
            "body": 30
        },
        {
            "type": "Email",
            "description": "Reminder",
            "position": 20,
            "account_id": 2,
            "body": "Hey,\r\n\r\ndidn't you forget about your favourite study?\r\n{{login_link}}",
            "cron_only": 1
        }
    ]
}
```

**Key**: Wait's `body` = position to redirect on manual advance (30). Email's `cron_only=1` = only cron sends it.

---

## 5. Longitudinal Study

Wave-based design with 1-year gap.

```json
{
    "name": "Longitudinal_study",
    "units": [
        {
            "type": "Survey",
            "description": "wave1: initial survey, have to collect contact info here",
            "position": 110
        },
        {
            "type": "Pause",
            "description": "wait 1 year for wave 2",
            "position": 120,
            "wait_minutes": 525600,
            "body": "## Thank you\r\n\r\nWe'll recontact you in a year."
        },
        {
            "type": "Email",
            "description": "wave2: invitation",
            "position": 200,
            "account_id": 1,
            "subject": "Longitudinal study: Wave 2",
            "recipient_field": "most recent reported address",
            "body": "Remember us?\r\n\r\n{{login_link}}"
        },
        {
            "type": "Survey",
            "description": "wave2: survey",
            "position": 210
        },
        {
            "type": "Endpage",
            "description": "end",
            "position": 300,
            "body": "## Thank you\r\n\r\nYou could repeat 120-210 for more waves."
        }
    ],
    "settings": {
        "cron_active": 1,
        "footer_text": "Contact [study admin](mailto:email@example.com)"
    }
}
```

**Pattern**: Uses wide position gaps (110, 120, 200, 210, 300) to insert future waves.

---

## 6. Social Network Study

Complex multi-survey design with R calculations and dynamic item display.

The run has these units:
- Survey "friend_list" — asks how many friends, collects names/sex/type/size for up to 8 friends using `showif` conditions and inline choices
- Survey "friend_rate" — one friend at a time using `calculate` items to look up previous answers, inline R in labels, and `showif` on submit buttons to control flow
- SkipBackward — loops back to friend_rate until all friends rated
- Endpage — shows a DT datatable summary and ggplot

Key techniques demonstrated:
- `showif` with JavaScript-only (`//js_only`) for immediate client-side hide/show
- `calculate` items calling `nrow()`, `paste0()` for cross-survey lookups
- Inline R in labels: `` `r name` ``, `` `r his` ``
- Conditional submit buttons with different `showif` conditions

---

## 7. Inline Survey Data (JSON)

A run with surveys embedded directly in the structure via `survey_data`.
Each Survey unit defines its items inline as JSON objects.

```json
{
    "name": "RunWithInlineSurveys",
    "units": [
        {
            "type": "Survey",
            "description": "Consent and demographics",
            "position": 10,
            "special": "",
            "survey_data": {
                "name": "demographics",
                "items": [
                    {
                        "type": "note",
                        "name": "note_welcome",
                        "label": "<h1>Study Information</h1>",
                        "optional": 1,
                        "class": "label_align_center",
                        "showif": "",
                        "value": "",
                        "block_order": "",
                        "item_order": 1
                    },
                    {
                        "type": "mc_button",
                        "name": "consent",
                        "label": "Do you agree to participate?",
                        "optional": 0,
                        "class": "",
                        "showif": "",
                        "value": "",
                        "choice_list": null,
                        "type_options": null,
                        "block_order": "",
                        "item_order": 2,
                        "choices": {
                            "1": "Yes",
                            "2": "No"
                        }
                    },
                    {
                        "type": "number",
                        "name": "age",
                        "label": "Age in years",
                        "optional": 0,
                        "class": "",
                        "showif": "consent == 1",
                        "value": "",
                        "choice_list": null,
                        "type_options": "0,120,1",
                        "block_order": "",
                        "item_order": 3
                    },
                    {
                        "type": "submit",
                        "name": "page1",
                        "label": "Continue",
                        "optional": 0,
                        "class": "",
                        "showif": "",
                        "value": "",
                        "choice_list": null,
                        "type_options": null,
                        "block_order": "",
                        "item_order": 4
                    }
                ],
                "settings": {
                    "maximum_number_displayed": 0,
                    "displayed_percentage_maximum": 100,
                    "add_percentage_points": 1,
                    "enable_instant_validation": 0,
                    "expire_after": 0,
                    "google_file_id": null,
                    "unlinked": 0,
                    "expire_invitation_after": 0,
                    "expire_invitation_grace": 0,
                    "hide_results": 0
                }
            }
        },
        {
            "type": "Survey",
            "description": "Personality items with shared choice list",
            "position": 20,
            "special": "",
            "survey_data": {
                "name": "personality",
                "items": [
                    {
                        "type": "mc_heading",
                        "name": "head_likert",
                        "label": "",
                        "optional": 0,
                        "class": "mc_width80",
                        "showif": "",
                        "value": "",
                        "choice_list": null,
                        "type_options": null,
                        "block_order": "",
                        "item_order": 1,
                        "choices": {
                            "1": "Strongly disagree",
                            "2": "Disagree",
                            "3": "Neutral",
                            "4": "Agree",
                            "5": "Strongly agree"
                        }
                    },
                    {
                        "type": "mc",
                        "name": "extraversion",
                        "label": "I enjoy social gatherings.",
                        "optional": 0,
                        "class": "hide_label mc_width80",
                        "showif": "",
                        "value": "",
                        "choice_list": "head_likert",
                        "type_options": null,
                        "block_order": "",
                        "item_order": 2
                    },
                    {
                        "type": "mc",
                        "name": "neuroticism",
                        "label": "I worry about things often.",
                        "optional": 0,
                        "class": "hide_label mc_width80",
                        "showif": "",
                        "value": "",
                        "choice_list": "head_likert",
                        "type_options": null,
                        "block_order": "",
                        "item_order": 3
                    },
                    {
                        "type": "submit",
                        "name": "page2",
                        "label": "Submit",
                        "optional": 0,
                        "class": "",
                        "showif": "",
                        "value": "",
                        "choice_list": null,
                        "type_options": "auto",
                        "block_order": "",
                        "item_order": 4
}
                ],
                "settings": {
                    "maximum_number_displayed": 0,
                    "displayed_percentage_maximum": 100,
                    "add_percentage_points": 1,
                    "enable_instant_validation": 0,
                    "expire_after": 0,
                    "google_file_id": null,
                    "unlinked": 0,
                    "expire_invitation_after": 0,
                    "expire_invitation_grace": 0,
                    "hide_results": 0
                }
            }
        },
        {
            "type": "Endpage",
            "description": "end",
            "position": 30,
            "special": "",
            "body": "## Thank you"
        }
    ]
}
```

**Key techniques demonstrated:**
- `survey_data` embedding items as JSON objects with all fields
- `choices` as key-value objects (string → string)
- Shared choice list via `choice_list` referencing a `mc_heading` item's choices
- `type_options` as separate field (e.g. `"0,120,1"`, `"auto"`)
- Survey settings object controlling progress bar and validation
- Multiple Survey units in one run, each with independent survey_data
"""


# ── Editing tools ─────────────────────────────────────────────────────

@_topic("editing-tools", "How to use the editing tools for run structure changes")
def _editing_tools():
    return r"""# Editing Tools for Run Structures

## Workspace Directory

The MCP stores all run structure files in a `.formr/` directory. By default this
is relative to the MCP server's working directory at startup, which varies by
client (Claude Code, Cursor, OpenCode each launch from a different CWD). To pin
it to a known absolute location, set `FORMR_WORKSPACE_DIR` in the `.env` file
next to `server.py`:

    FORMR_WORKSPACE_DIR=/absolute/path/to/your/project/.formr

All tool messages (fetch, save, error) include the absolute path so you always
know exactly where files are being read from and written to.

## Core Workflow: Fetch → Edit → Upload

Always use the file-based workflow. Do NOT pass large JSON structures through tool call arguments.

1. `get_run_structure_to_file("run-name")` — fetches to `.formr/run-name.json`
2. Edit `.formr/run-name.json` with Read/Edit tools, or use the programmatic editing tools below
3. `update_run_structure_from_file("run-name")` — validates and uploads

On success, the backup (`.formr/run-name.json.bak`) is auto-removed.
On validation error, fix the file and retry. If stuck, restore from `.bak`.

## Programmatic Editing Tools

For systematic changes, use the editing tools instead of manual JSON edits.
All editing tools operate on `.formr/<name>.json` directly. Call `get_run_structure_to_file` first to ensure the file exists.

### add_run_unit

```
add_run_unit(name, unit_type, position, **kwargs)
```

Add a unit to the run structure. If `insert_mode='shift'` (default) and the position is already occupied, all units at that position or higher are shifted up by 10. Position references (`if_true`, Wait `body`) in existing units are automatically updated.

If `insert_mode='overwrite'`, any existing unit at the target position is replaced.

Common kwargs by unit type:
- **SkipForward/SkipBackward**: `condition` (R expr), `if_true` (int position), `automatically_jump` (0/1), `automatically_go_on` (0/1)
- **Email**: `subject`, `body`, `account_id` (int), `recipient_field`, `cron_only` (0/1)
- **Pause/Wait**: `wait_minutes`, `wait_until_time` ("HH:MM:SS"), `wait_until_date` ("YYYY-MM-DD"), `relative_to` (R expr), `body`
- **Wait**: `body` is an integer position for click-through (NOT display content)
- **Survey**: `study_id` (int) or `survey_data` (dict with name, items, settings)
- **Endpage/Page**: `body` (markdown/knitr content)
- **External**: `address` (URL or R code), `api_end` (0/1)

### remove_run_unit

```
remove_run_unit(name, position, compact=False)
```

Remove a unit at the given position. If `compact=True`, shifts all units at higher positions down by **1** to fill the gap. Position references are automatically updated when compacting. Dangling references to the removed position are detected and reported as warnings.

**Note**: Compact shifts by 1, not by the original spacing. This means removing position 20 from `[10, 20, 30]` produces `[10, 29]`, not `[10, 20]`. Use `renormalize_positions` afterwards to clean up to clean multiples of 10.

### duplicate_run_units

```
duplicate_run_units(name, from_positions, to_start_position, shift_existing=True)
```

Copy units at `from_positions` to new positions starting at `to_start_position`, with gaps of 10 between copies. Copied units get a "copy: " prefix on their description (but the prefix won't stack on re-duplication).

If `shift_existing=True` (default), any existing units at conflicting positions are shifted up to make room.

Position references are remapped in three ways:
1. **Internal**: References within the copied block point to the new corresponding positions
2. **External shifted**: References in existing units that are shifted are updated
3. **External unchanged**: References pointing outside both blocks are left as-is

### shift_run_positions

```
shift_run_positions(name, from_position, delta)
```

Shift all units at positions >= `from_position` by `delta`. Positive delta shifts up (making room), negative shifts down (closing gaps). Position references (`if_true`, Wait `body`) are automatically updated.

### renormalize_positions

```
renormalize_positions(name, spacing=10)
```

Renumber all unit positions to clean multiples of `spacing` (default 10) while preserving order. Assigns positions 10, 20, 30, ... based on current sorted order. All position references (`if_true`, Wait `body`) are automatically updated.

This is essential after `remove_run_unit(compact=True)` which shifts by 1 and leaves messy positions like 10, 19, 29. Calling `renormalize_positions` cleans these up to 10, 20, 30.

Safe to call on already-clean structures — if positions are already at clean multiples in the right order, no changes are made.

## Authoring Survey Items

To create or modify items in a Survey unit's `survey_data`, use the JSON format
documented in `get_documentation("survey-json")` (structure, fields, choices, page
breaks, CSS classes) together with `get_documentation("item-types")` (all 56 item
types, choices rules, type_options). Edit the `.formr/<name>.json` file directly
with Read/Edit tools after fetching it with `get_run_structure_to_file`.

## Read-Only Inspection Tools

These tools read from the local `.formr/<name>.json` file (call `get_run_structure_to_file` first):

- **`summarize_run(name, detail)`** — human-readable overview. `detail="units"` for unit-level only, `detail="items"` (default) to include all survey items. Strips HTML from labels.
- **`find_run_items(name, query?, item_type?)`** — search items by name/label substring and/or item type (e.g. `"mc"`, `"text"`, `"calculate"`).
- **`analyze_run(name)`** — check for structural errors: R syntax validation, variable references, branch flow, Page/Endpage blocking, Wait body validation, item consistency, and common mistakes.
- **`open_flowchart(name)`** — upload the local run structure to the formr Flowchart Generator and open an interactive flowchart in the browser. Returns a shareable URL (expires after 24 hours). Use this to show the user a quick, intuitive overview of what was built — position flow, branches, and unit types at a glance.

## Position Management

Positions are integers, typically spaced by 10 to allow insertions (10, 20, 30, ...).

- **`add_run_unit`** auto-shifts by 10 to avoid collisions at the exact position, but only at that position. Planning positions with gaps is best practice.
- **`remove_run_unit(compact=True)`** shifts by **1**, not by the original spacing. After compacting, positions like 10/20/30 become 10/19/29. Call `renormalize_positions` to clean up.
- **`if_true`** (on Branch/SkipForward/SkipBackward) and **Wait `body`** contain position references. These are automatically updated by all editing tools:
  - `add_run_unit` (shift mode) remaps references in existing units shifted up
  - `remove_run_unit` (compact) remaps references and warns about dangling references
  - `shift_run_positions` remaps references for all shifted units
  - `renormalize_positions` remaps all position references to match new positions
  - `duplicate_run_units` remaps internal references within the copied block, updates references in shifted existing units, and updates references in copies pointing to shifted external positions
- **Wait `body`** is a position integer, not display content. Only Wait uses `body` as a position reference. Pause, Page, Endpage, and Email `body` fields are display content and are left unchanged by position remapping.
- When a reference points to a removed position (dangling reference), `remove_run_unit` warns but does not delete or redirect it. Use `analyze_run` after edits to verify structural validity.
"""


# ── Best practices ──────────────────────────────────────────────────

@_topic("best-practices", "Design patterns and recommendations")
def _best_practices():
    return """# Best Practices

## ⚠️ Critical: Page/Endpage Permanently End the Run

**This is the #1 most common mistake in formr run design.**

`Page` and `Endpage` units **permanently end the run session**. They set
`end_session = true` and `end_run_session = true`. The participant **cannot
advance past them** — there is no "Continue" button.

**Every unit that comes after a Page/Endpage in position order (without a branch
redirecting around it) is unreachable.** Entire study phases become invisible.

### What to use instead

| Scenario | ❌ Don't use | ✅ Use |
|----------|-------------|--------|
| Waiting between phases | Page | Pause (displays `body` content while waiting) |
| Click-through to continue | Page | Wait (lets participant click through via `body` position) |
| Informational text only | Page | Survey with `note` items |
| Thank-you / exclusion end | Endpage | Endpage (this IS the correct use) |
| Feedback at study end | Endpage | Endpage (correct) — but only at the true end |

### The Pause vs Wait difference

- **Pause**: Displays `body` content (markdown/knitr). No user interaction possible.
  Only cron advances past a Pause when the timer/condition expires.
- **Wait**: The `body` field is an **integer position** to jump to on click.
  If the participant arrives, they click through. If only cron arrives,
  it waits until the timer expires, then advances to the next position.

### Common ESM mistake pattern
```
❌  Survey → Page("instructions") → Pause → Survey → ...
   (Page ends the session, everything after is unreachable!)

✅  Survey → Pause(body="instructions...") → Survey → ...
   (Pause displays instructions and lets cron advance when ready)
```

## Run Design

**Position spacing**: Use gaps (10, 20, 30, ...) so you can insert units between
existing ones. Never use consecutive positions (1, 2, 3) unless the run is final.

**Branch always has a fallthrough**: Make sure there's a default path when all
branch conditions are FALSE. The fallthrough continues to the next position.

**Test with a small session**: Before launching, create a test session with
`testing=true` and walk through the entire run.

**Cron for automated actions**: Set `cron_active=1` in settings and use
`cron_only=1` on Email units that should only fire from cron, not on first visit.

**24-hour dropout detection**: For ESM studies, include a SkipForward that checks
whether the last survey completion was >24 hours ago and redirects to an
exclusion Endpage or a keep-in-loop Survey for staff intervention.

**Reminder deduplication**: Use `survey_unit_sessions` to check whether a reminder
has already been sent today, avoiding duplicate emails on the same day.

## Survey Design

**Inline survey_data**: Embed surveys directly in the run structure via
`survey_data` on Survey units. Each survey defines `name`, `items[]`,
and optional `settings{}`.

**Items as JSON objects**: Each item has fields: `type`, `name`, `label`,
`optional` (0/1), `class`, `showif`, `value`, `choice_list`, `type_options`,
`choices`, `block_order`, `item_order`. Set `label_parsed` equal to `label`.

**Inline choices**: Use the `choices` field instead of a separate choices
sheet. Object format gives explicit key-value pairs; array format uses
values as labels.

**Shared choice lists**: Define choices on one item (e.g. a `mc_heading`),
then reference them via `choice_list` on other items. No separate sheet needed.

**Page breaks with submit**: A `submit` item creates a page break. Items
before it are one page. Multiple submit items = multi-page survey.

**Showif for adaptive questioning**: Use `showif` to show/hide items based on
previous answers. Prefix with `//js_only` for instant client-side response.

**Calculate for computed values**: Use `calculate` items for on-the-fly
computations. The R expression in `value` runs when the page is submitted.

**Validation via type_options**: Set min/max on numbers, patterns on text,
auto-submit on submit buttons.

**Sticky values**: Use `value = "sticky"` on items to preserve the last entered
value (useful for diary studies).

## Email Units

**recipient_field patterns**:
- `"most recent reported address"` (default) — uses the most recent email-type survey item
- `"survey_name$item_name"` — references a specific email item from a specific survey
- Any R expression that returns an email address string

**cron_only on reminders**: Set `cron_only: 1` on Email units that should only be
sent by the cron daemon. When a participant visits a `cron_only` Email unit, no
email is sent and the unit is immediately completed. This prevents duplicate
reminder emails when a participant logs in.

**Knitr in email bodies**: Email bodies support `` `r expr` `` inline R and
`` ```{r} ``` `` code chunks. Use these for personalized greetings:
```r
`r ifelse(demographics$gender == 1, "Lieber Teilnehmer", "Liebe Teilnehmerin")`,
```

## External Units

**URL vs R code**: When `address` starts with `http`, it's a redirect URL.
Otherwise, it's R code executed via OpenCPU. This means you can put entire
R scripts in `address`:

```r
# In the address field of an External unit:
library(stringr)
number <- str_replace_all(baseline$phone, " ", "")
body <- paste0("Please complete the survey: ", "https://study.example.org/esm/")
httr::GET(paste0("https://sms-gateway.example/send?to=", number, "&msg=", body))
FALSE  # return FALSE to move on without redirecting
```

**api_end field**:
- `api_end = 0` (default): formr immediately ends the unit session after execution.
  Use for fire-and-forget actions like sending an SMS.
- `api_end = 1`: formr waits for an external service to call back. The unit session
  stays open until the callback is received or `expire_after` minutes pass.

**Secrets for credentials**: Never hardcode API keys in External unit code.
Store them in run secrets and access via `.formr$secret_<name>`.

## R Code

**Reference surveys by name**: The survey name becomes the data frame name.
`demographics$age` accesses the `age` variable from the `demographics` survey.

**nrow() for counting**: `nrow(diary)` counts how many times the `diary` survey
has been filled out. Useful for loop conditions.

**tail() for most recent**: `tail(diary$created, 1)` gives the most recent
submission timestamp. Essential for dropout detection.

**lubridate for timing**: Use `library(lubridate)` for date arithmetic in pause
conditions and showif expressions.

**survey_unit_sessions for deduplication**: This system data frame tracks every
unit visit. Use it to check when an SMS was last sent or whether a reminder
was already sent today.

**Package availability**: OpenCPU has many CRAN packages. If you need one that's
not installed, it needs to be added to the OpenCPU Dockerfile.

## Common Mistakes

1. **Page/Endpage blocking the flow** — `Page` and `Endpage` permanently end
   the run session. Every unit after them (in position order, without a branch
   redirecting around them) is unreachable. Use `Pause` for informational text
   and `Wait` for click-through points instead.
2. **Survey unit without study_id or survey_data** — a Survey unit must either
   reference an existing survey (`study_id`) or include inline `survey_data`.
   Without one of these, the survey won't work. When embedding `survey_data`,
   also include the original `study_id` if the survey already exists on the
   server (the API preserves the link).
3. **Wrong account_id on Email units** — the email account must be owned by the
   run owner. Check ownership before importing.
4. **Branch if_true points to itself** — creates an infinite loop. Always point
   to a different position.
5. **Missing cron_active** — Pause/Wait/Email units won't process without
   `cron_active=1` in settings.
6. **Overlapping position numbers** — duplicates cause undefined behavior.
   Always use unique positions.
7. **Using `null` to clear settings** — when clearing a run setting field (e.g.
   `header_image_path`), passing `null` may be silently ignored by the formr
   API. Use an empty string `""` instead: `update_run_settings(name,
   {"header_image_path": ""})`.
8. **Wait body as display content** — in `Wait` units, `body` is a position
   number for click-through redirect, NOT display content like Pause. If you
   put HTML/markdown in a Wait body, it will try to jump to that position number
   and fail.
9. **Hardcoded credentials in External units** — never put API keys or passwords
   directly in R code. Use run secrets (`.formr$secret_<name>`) instead.
10. **Ignoring survey_unit_sessions** — this table is essential for ESM studies.
    Use it to track when notifications were sent and implement reminder
    deduplication.
11. **Using use_paging or page_items in survey settings** — these are legacy
    "Custom Paging" controls. They are no longer supported. Use `submit` items
    in the item list to create page breaks instead. A `submit` item creates a
    page break: all items before it render as one page.
12. **Setting use_material_design to 1 in run settings** — Material Design is a
    legacy theme that is not supported. Use `custom_css` to style the survey
    appearance instead.
"""


# ── Advanced unit types ──────────────────────────────────────────────

@_topic("unit-types-advanced", "Wait, External, secrets, and ESM patterns")
def _unit_types_advanced():
    return r"""# Advanced Unit Types: Wait, External, Secrets, and ESM

These unit types and patterns are essential for diary and experience sampling
studies.

## Wait Unit

The `Wait` unit extends `Pause` with **interactive click-through**. It is the
key building block for ESM/diary study designs.

### How it works

When a participant arrives at a Wait unit:
1. If they **click through** (interact with the page), they jump to the position
   stored in `body`
2. If the **timer expires** (cron processes the unit), they advance to the next
   sequential position

This creates two paths:
- **Participant arrives** → Wait → clicks → jumps to `body` position (usually a survey)
- **Timer expires** (cron) → Wait → advance to next position (usually a reminder Email)

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"Wait"` |
| `position` | int | Position in the run |
| `description` | string | Description |
| `body` | **int** | The position number to jump to on click. **NOT display content.** |
| `wait_minutes` | decimal | Minutes to wait before advancing via cron |
| `wait_until_time` | string | Time of day HH:MM:SS for additional time gate |
| `wait_until_date` | string | Date for additional date gate |
| `relative_to` | string | R expression for dynamic timing (same as Pause) |

### Example: ESM beep with 90-min window

```json
{
    "type": "Wait",
    "position": 30,
    "description": "ESM beep: wait up to 90 min for participant",
    "wait_minutes": 90,
    "body": 45,
    "relative_to": ""
}
```

- Participant clicks within 90 min → jumps to position 45 (ESM survey)
- 90 min passes (cron) → advances to position 31 (reminder Email)

### Critical: Wait body is a position, NOT content

The `body` field on a Wait unit is the **integer position** to redirect to on
click. Do NOT put HTML or markdown in it — that's what Pause `body` is for.
If you write `"body": "<p>Click here</p>"`, formr will try to parse it as a
position number and fail.

## External Unit — R Code Execution

When the `address` (JSON field name: `address`, originally `external_link`)
doesn't start with `http`, formr treats it as **R code** and evaluates it
via OpenCPU with the participant's full run data available.

### What External R code can do

- Send SMS messages via API (`httr::GET()` / `httr::POST()`)
- Transform data (e.g., phone number formatting)
- Return a URL string → participant is redirected to that URL
- Return `FALSE` → no redirect, advance to next unit
- Access all survey data for the current participant

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"External"` |
| `position` | int | Position in the run |
| `description` | string | Description |
| `address` | string | URL for redirect, or R code for execution |
| `api_end` | 0 or 1 | `0` = auto-advance after execution (default). `1` = wait for external callback |
| `expire_after` | int | Minutes until unit auto-expires (with `api_end=1`) |

### Example: Send SMS via API

```json
{
    "type": "External",
    "position": 80,
    "description": "Send SMS notification",
    "address": "library(stringr)\nphone <- str_replace_all(baseline$phone, \" \", \"\")\nhttr::GET(paste0(\"https://sms.example.com/send?to=\", phone, \"&msg=Hello\"))\nFALSE",
    "api_end": 0,
    "expire_after": 0
}
```

### Secrets for API credentials

Never hardcode API keys. Store them in run secrets and reference via
`.formr$secret_<name>`:

```r
# In External unit address:
sms_key <- .formr$secret_sms_key
httr::GET(paste0("https://sms.example.com/send?key=", sms_key, "&to=", phone))
```

To add secrets, include them in the run settings:
```json
{"secrets": ["sms_key", "sms_password"]}
```
Then fill in actual values via the formr admin interface.

## ESM Study Design Pattern

The production-tested ESM pattern uses these units for each daily beep:

```
SkipForward (time gate) → Wait (click window) → Email (reminder) → Survey
```

### Complete beep structure

```json
{
    "type": "SkipForward",
    "position": 25,
    "condition": "hour(now()) >= 12",
    "if_true": 50,
    "description": "Skip beep 1 if past 12:00"
}
{
    "type": "Wait",
    "position": 30,
    "wait_minutes": 90,
    "body": 45,
    "description": "90-min click window for beep 1"
}
{
    "type": "Email",
    "position": 40,
    "cron_only": 1,
    "subject": "Time for your questionnaire!",
    "body": "{{login_link}}"
}
{
    "type": "Survey",
    "position": 45,
    "description": "ESM survey"
}
```

### 24-hour dropout detection

```json
{
    "type": "SkipForward",
    "position": 85,
    "condition": "nrow(esm) > 0 && as.numeric(difftime(now(), tail(esm$created, 1), units='hours')) > 24",
    "if_true": 110,
    "description": "24hr inactivity → end study"
}
```

### Reminder deduplication

```json
{
    "type": "SkipForward",
    "position": 59,
    "condition": "length(survey_unit_sessions[survey_unit_sessions$position == 60, ]$created) > 0 && date(tail(survey_unit_sessions[survey_unit_sessions$position == 60, ]$created, 1)) == today()",
    "if_true": 61,
    "description": "Skip reminder if already sent today"
}
```

### Day counting for 10-day ESM

```json
{
    "type": "SkipBackward",
    "position": 90,
    "condition": "nrow(esm) < 50 && as.numeric(difftime(now(), baseline$created, units='days')) < 10.5",
    "if_true": 25,
    "description": "Continue if <10.5 days and <50 completions"
}
```

## Cross-Participant Data Access (V1 API)

`survey_unit_sessions` and survey data frames contain only the current participant's
data. The V1 API is available in all R evaluation contexts within a run (conditions,
item values/showif, labels, page/email bodies, External units) to query all participants.

**Auto-injection**: formr injects a token when it detects `formr_api_authenticate(`.
Call it with **no arguments** for the current run — no secrets needed.
The auto-token is **run-restricted**; for a different run, pass credentials explicitly.

```r
library(formr)

# ── Current run: token auto-injected ─────────────────────────────────────────
formr_api_authenticate()
sessions <- formr_api_sessions(.formr$run_name)
nrow(sessions)

# ── Different run: credentials from run secrets ───────────────────────────────
formr_api_authenticate(
  client_id     = .formr$secret_api_client_id,
  client_secret = .formr$secret_api_client_secret,
  host          = .formr$host
)
acq <- formr_api_results("acq-run")
nrow(acq)
```

Never use the deprecated `formr_connect()` / `formr_raw_results()` — use `formr_api_*` exclusively.

## Personalized Links Between Runs

To create a link from one run to another that carries the participant's session:

```r
# In an Email body or Pause body:
paste0('https://formr.example.org/acq-run/?ps=1&anchor=',
       stringr::str_sub(survey_run_sessions$session, 1, 20))
```

The `anchor` parameter pre-fills the acquaintance run with the participant's
truncated session code, enabling data linkage.
"""


@_topic("data-access", "Accessing participant data — V1 API for External units and RStudio")
def _data_access():
    return r"""# Accessing Participant Data in formr

## The boundary: system frames vs. V1 API

| Data source | Scope | When to use |
|-------------|-------|-------------|
| `survey_unit_sessions` | Current participant only | Timing, deduplication, dropout detection |
| Survey data frames (`diary`, `baseline`, …) | Current participant only | Personalising content, conditional logic |
| formr V1 API (`formr_api_*`) | ALL participants, ALL runs | Counts, aggregate checks, cross-run lookups |

If you need a number, percentage, or list that covers **more than one participant**,
you need the V1 API.

## Reading data through the MCP (read tools + the GDPR gate)

The MCP can read a live run directly — useful while designing or debugging:

| Tool | Returns | Scope |
|------|---------|-------|
| `list_sessions` / `get_session` | who is in the run, position, ended, testing flag | session:read |
| `list_unit_sessions` | per-unit history: progress, dropout, trajectories | session:read |
| `get_run_results` | raw survey responses (filter by survey/session/item) | data:read |
| `list_run_files` | uploaded-file metadata | file:read |
| `get_survey_structure` | item tables + choice lists (definitions, not data) | survey:read |

**Token limits are surfaced, not guessed.** Call `whoami` first: it reports the
token's granted scopes, whether it is read-only, its run allowlist (e.g. "limited
to run X"), and a per-tool availability map. Don't infer limits from 403s — read
them from `whoami`.

**The data gate (`FORMR_DATA_ACCESS`).** This server-level env var is a hard ceiling
on what the MCP may read:

- `test_only` (default) — only **test** sessions (`testing=1`) are ever readable.
  `list_sessions`/`list_unit_sessions`/`get_run_results` silently restrict to test
  data; `get_session` on a real participant and any explicit `testing=false` are
  refused. This is the GDPR-safe default.
- `all` — real participant data is readable; you may still filter test/real per call.

Note `get_run_results` enforces the ceiling by first resolving the matching session
codes (via the sessions endpoint) and passing them as the `sessions` filter, because
the results endpoint itself has no testing filter. It returns `{}` when nothing matches
(e.g. no test sessions yet). For analysing a full real dataset, still prefer emitting
R code for the researcher's RStudio session (below) over pulling everything into context.

## When to reach for the V1 API

Reach for `formr_api_*` whenever you need to:

- Count how many participants completed a run or survey
- Check whether a participant's acquaintance / partner has already submitted data
- Join data across two separate runs (e.g., target run + nominator run)
- Detect global study state (e.g., "has wave 2 begun?")
- Query session statuses or current positions across all participants

## Two usage contexts

### A — Inside any R code within a formr run

R code is evaluated by formr's OpenCPU server in many contexts: Branch conditions,
survey item `value`/`showif` fields, item/choice `label` knitr, Page/Endpage bodies,
Email bodies, and External unit code. The V1 API is available in all of them.

**formr auto-injects an OAuth token** when it detects `formr_api_authenticate(` in
the code. It mints a 180 s token (scopes: `data:read session:read session:write run:read`)
for the run owner and injects it into `.formr$access_token`, `.formr$host`, and
`.formr$run_name`. Call `formr_api_authenticate()` with **no arguments** — no secrets,
no setup required.

The auto-token is **run-restricted** (current run only). For a different run, store
OAuth credentials as run secrets (see §B.2 below) and pass them explicitly.

**Exception**: Email subject lines and push notification titles are rendered as
plaintext — they do not go through R evaluation and cannot use the V1 API.

```r
library(formr)

# ── Same run: no arguments, token auto-injected ──────────────────────────────
formr_api_authenticate()

# Count all participants in this run
all_sessions <- formr_api_sessions(.formr$run_name)
nrow(all_sessions) >= 10   # TRUE/FALSE → branch condition

# ── Different run: credentials from run secrets ───────────────────────────────
formr_api_authenticate(
  client_id     = .formr$secret_api_client_id,
  client_secret = .formr$secret_api_client_secret,
  host          = .formr$host
)
acq <- formr_api_results("acq-run")
n_completed <- nrow(acq[!is.na(acq$acq_name_1), ])
n_completed >= 3   # TRUE/FALSE → branch condition
```

Return value semantics for External units:
- `FALSE` → advance to next unit
- A URL string → redirect participant there
- Any other value → logged, advance to next unit

### B — In the researcher's own RStudio session

The same `formr_api_*` functions work identically outside formr. Researchers run
this locally to download and analyse data.

```r
library(formr)

# Interactive authentication (stores token in OS keyring)
formr_api_authenticate(
  client_id     = "your-client-id",
  client_secret = "your-client-secret",
  host          = "https://formr.example.org"
)

# Download and process all participant data for a run
results <- formr_api_results("my-diary-run")
head(results)

# List all sessions with current positions
sessions <- formr_api_sessions("my-diary-run")
table(sessions$position)
```

> For full-scale analysis (downloading and processing an entire dataset),
> produce R code as an artifact for the researcher to run in RStudio. The MCP
> read tools (below) are for inspecting and debugging a run during design —
> not a substitute for a proper analysis session.

## Key V1 API functions

| Function | Returns | Notes |
|----------|---------|-------|
| `formr_api_authenticate()` | token (invisible) | No-args inside External units — token auto-injected |
| `formr_api_authenticate(client_id, client_secret, host)` | token (invisible) | Explicit creds for RStudio or cross-run access |
| `formr_api_results("run")` | data.frame | All participants, processed (types recognised, scales aggregated) |
| `formr_api_fetch_results("run", join=FALSE)` | list of data.frames | Raw unprocessed, one per survey |
| `formr_api_sessions("run")` | data.frame | All sessions: session code, created, ended, position |
| `formr_api_session_action("run", session_codes, action)` | invisible | Move/end/toggle sessions; actions: `"end_external"`, `"move_to_position"`, `"toggle_testing"` |

## Setting up credentials

### For External units accessing the current run

No setup required. formr auto-injects a run-scoped token whenever
`formr_api_authenticate(` appears in the code. Call `formr_api_authenticate()`
with no arguments.

### For External units accessing a different run (run secrets)

1. Add secret names (not values) to run settings:
   ```json
   "secrets": ["api_client_id", "api_client_secret"]
   ```
2. Fill in actual values in the formr admin interface (encrypted at rest).
3. Reference in R code as `.formr$secret_api_client_id`, `.formr$secret_api_client_secret`.
4. The credential needs `data:read` scope and the target run in its allowlist.

### For RStudio (researcher's own session)

Pass explicit credentials to `formr_api_authenticate()`. These can be stored in
the OS keyring via `formr_store_keys()` to avoid hardcoding.

## Common patterns

### Count completions in the current run (External unit)
```r
library(formr)
formr_api_authenticate()   # auto-injected token, no args needed
sessions <- formr_api_sessions(.formr$run_name)
sum(!is.na(sessions$ended))  # participants who finished
```

### Check if a linked run has a matching entry (cross-run, needs secrets)
```r
library(formr)
formr_api_authenticate(
  client_id     = .formr$secret_api_client_id,
  client_secret = .formr$secret_api_client_secret,
  host          = .formr$host
)
acq <- formr_api_results("acq-run")
any(acq$target_code == .formr$login_code, na.rm = TRUE)
```

### Download data in RStudio
```r
library(formr)
formr_api_authenticate(
  client_id     = "your-client-id",
  client_secret = "your-client-secret",
  host          = "https://formr.example.org"
)
results <- formr_api_results("my-diary-run")
head(results)
```

### Fetch raw results for two surveys separately
```r
raw_list <- formr_api_fetch_results("my-run", join = FALSE)
baseline <- raw_list[["baseline"]]
diary    <- raw_list[["diary_survey"]]
nrow(diary)
```
"""


@_topic("patterns", "Recipe library for complex runs — use list_patterns / get_pattern")
def _patterns():
    return r"""# Pattern / recipe library

Complex runs are compositions of a few recurring idioms. Rather than re-deriving them from
scratch, consult a pattern mined from a real working run.

**These patterns inform — they are not copy-paste templates.** Real deployments differ too much
(survey names, study_ids, item lists, positions) for a fixed unit blueprint to drop in. Each
pattern gives you the *approach*; you build the units adapted to the run with the editing tools.

**Use the tools:**

- `list_patterns()` — short catalog (name, title, problem it solves).
- `get_pattern(name)` — the approach: problem, when_to_use, `structure` (which units/fields),
  `how_it_works`, `key_r` (the portable R idioms), and `gotchas`.

## Workflow

1. `list_patterns()` to find a match for the request.
2. `get_pattern("<name>")` to read the structure, the R idioms, and the gotchas.
3. Build the units yourself (add_run_unit / Edit), reusing the `key_r` snippets and renaming to
   the run's real survey/item names.
4. `analyze_run` then `update_run_structure_from_file`.

## Available patterns

| Name | Solves |
|------|--------|
| `loading-screen` | Hide a slow calculate behind a "please wait" page so participants don't drop out |
| `balanced-assignment` | Assign each participant to the smaller (completion-weighted) condition cell |
| `covariate-balancing` | Balance conditions on continuous confounders via `balancr` + JSON state |
| `waiting-room` | Hold participants at a position until an admin releases a batch (`session_action`) |
| `live-aggregate-feedback` | Show live results from all respondents + majority-dependent content |
| `adaptive-loop` | Iterate items until an estimate converges (adaptive/IRT testing) |
| `personalized-email` | Email with inline-R greeting and a `{{login_link}}` resume link |
| `external-api-dispatch` | Call a third-party API (e.g. SMS) from an External unit using run secrets |
| `json-state-store` | Accumulate structured cross-participant state as JSON (append-only rows; resolve latest, races not lost writes) |
| `dry-r-functions` | Factor repeating R into the run's `custom_r` store so units stay DRY |

## Two conventions every pattern relies on

- **`type_options` is a string**, never a raw int. On a `submit` item it means auto-submit:
  `"1"` (~1 ms, used to create a page boundary), `"800"`/`"2000"` (ms delay, used for
  polling), `"auto"` (submit as soon as a choice is made). On a `number` item it means
  `"min,max,step"`. Same field, different meaning per item type.
- **`formr_api_fetch_results(surveys = c(...), join = TRUE)` renames columns** to
  `<item>_<survey>`. Reference the renamed columns in your R, and count only completed
  participants when balancing.

See also `custom-r-and-secrets` for the run-level R function store and encrypted secrets.
"""


@_topic("custom-r-and-secrets", "Run-level custom R functions (DRY) and encrypted secrets")
def _custom_r_and_secrets():
    return r"""# Custom R functions and secrets (run level)

formr lets a run define shared R and encrypted secrets at the run level, both injected into
every R evaluation context. They are the analogue of custom CSS/JS, and they let runs stay DRY
and keep credentials out of the structure.

Both live in the run **settings** (so they round-trip in the structure file under
`settings.custom_r` and `settings.secrets`), and both are exposed by the v1 API.

## Custom R functions (`custom_r`) — keep runs DRY

Define named functions and global variables once. formr injects them **before every R
evaluation**, so they are callable by name in `showif`, item `value`, branch `condition`,
note labels, Page/Email bodies, and External code.

- **The one hard rule:** a custom_r function **cannot see variables defined in inline R code**.
  It only sees its own arguments and globals. So pass survey data in explicitly.
- Edit it in the admin UI (Settings → R Functions) or via the MCP:
  `update_run_settings(name, {"custom_r": "..."})`. It appears in the structure file as
  `settings.custom_r`.

```r
# --- custom_r (defined once) ---
is_done <- function(ended_col) {
  !is.null(ended_col) && length(ended_col) > 0 && !all(is.na(ended_col))
}

# --- anywhere in the run (condition / value / showif / body) ---
is_done(VIBE_SQ1_ready$ended)   # pass the column in; the function can't see it otherwise
```

**`formr_api_authenticate()` does NOT work in `custom_r`.** The
auto-injected OAuth token (see `data-access`) is only available inside
**unit-level** R evaluation contexts: branch conditions, item `value`/`showif`,
labels, bodies, and External unit code. `custom_r` is sourced separately at
run boot and does not go through the token-minting pipeline. If you need
cross-participant data access, put `formr_api_authenticate()` in a
`calculate` item's `value` instead — formr sees the call there and injects
the token.

Good candidates to factor out: the long `exists()/is.null()/length()/is.na()` guards that
appear in many conditions, `safe_parse_json()` for JSON-state stores, phone-number
normalisation for SMS, and scoring formulas reused across surveys. See the `dry-r-functions`
pattern.

## Run secrets — encrypted credentials

Store API keys / gateway passwords as **encrypted, write-only** run secrets instead of
hardcoding them in External units.

- **Naming:** letters, digits, underscore (`[A-Za-z0-9_]+`), e.g. `api_key`.
- **Access in R:** the literal form `.formr$secret_<name>` — a secret named `api_key` is
  available as `.formr$secret_api_key`. Works in showif, value, label, body, condition, etc.
- **Selective injection:** a secret is only sent to the R environment when your code
  **literally contains** `.formr$secret_<name>`. Dynamic access like
  `get(paste0(".formr$secret_", var))` will NOT trigger injection — always use the literal form.
- **Write-only:** once saved the value is never shown again, and it is auto-redacted from
  result logs (including escaped forms). Set/replace values in the admin UI (Settings →
  Secrets).
- **Round-trip:** the structure export lists secret **names** (not values) under
  `settings.secrets`. Importing a structure with `secrets` names creates placeholder entries so
  the admin knows which values to fill in; values themselves are never carried in the structure.

```r
# Read a gateway credential from a run secret (never hardcode it):
httr::GET("https://gate.example.com/send", query = list(
  id = .formr$secret_sms_id, pw = .formr$secret_sms_pw, msg = body))
```

**Not for the formr v1 API secret.** Cross-participant data access uses
`formr_api_authenticate()` with no arguments — formr auto-injects a run-scoped token. Don't
store the API secret here. See `data-access`.
"""
