"""Pattern / recipe library for complex formr runs.

These patterns **inform** the agent — they are not copy-paste templates. Real deployments
differ too much (survey names, study_ids, item lists, positions) for a fixed unit blueprint to
drop in cleanly. Instead each pattern describes the *approach*: the structure (which unit types,
in what order, with which key fields), the reusable R idioms that are the genuinely portable
part, the gotchas, and — where relevant — which repeating R to factor into the run's `custom_r`
store so the run stays DRY. The agent reads a pattern, then builds units adapted to the
specific run with the normal editing tools.

Patterns are mined from real working runs in ``improvement_materials/``. Two conventions recur:

* ``type_options`` is a **string**, never a raw int. On a ``submit`` item it means auto-submit:
  ``"1"`` (~1 ms, used to create a page boundary), ``"800"``/``"2000"`` (ms delay, polling),
  ``"auto"`` (submit as soon as a choice is made). On a ``number`` item it means
  ``"min,max,step"``. Same field, different meaning per item type.
* A join renames columns: ``formr_api_results()`` and ``formr_api_fetch_results(join = TRUE)``
  suffix every column but ``session`` as ``<item>_<survey>`` whenever more than one table comes
  back (more than one survey, which is all of the run's surveys if ``surveys`` is not given, or
  a run whose Shuffle has assigned anyone). Name the survey(s) you need and reference the
  columns accordingly; count only completed participants when balancing.
"""

from __future__ import annotations


# ── reusable R idioms (the portable part of each pattern) ────────────

_R_BALANCED_ASSIGNMENT = """library(dplyr)
formr_api_authenticate()  # no args inside a run — formr injects a run-scoped token

# join = TRUE across multiple surveys renames columns to <item>_<survey>, hence
# group_group_selector / question_questionnaire below.
df <- formr_api_fetch_results(
  run_name   = .formr$run_name,
  surveys    = c("group_selector", "questionnaire"),
  item_names = c("group", "question"),
  join       = TRUE
)

# Count only participants who COMPLETED the questionnaire (question == 1), so drop-outs
# don't count against the cell they were assigned to.
completed <- !is.na(df$question_questionnaire) & df$question_questionnaire == 1
n_exp  <- sum(completed & df$group_group_selector == "experimental", na.rm = TRUE)
n_ctrl <- sum(completed & df$group_group_selector == "control",      na.rm = TRUE)

ifelse(n_exp <= n_ctrl, "experimental", "control")  # smaller cell wins; tie -> experimental"""

_R_COVARIATE_BALANCING = """library(balancr)
formr_api_authenticate()

# Fixed population estimates (Mean, SD) — must stay constant across ALL participants.
est_age_mean <- 40; est_age_sd <- 15
est_weight_mean <- 75; est_weight_sd <- 20

# age / weight are items in THIS survey, so formr exposes them as bare variables.
scaled_age    <- (age - est_age_mean) / est_age_sd
scaled_weight <- (weight - est_weight_mean) / est_weight_sd

# Fetch all prior participants' covariates + the JSON state stored in bwd_result.
# Name the survey: without `surveys` every survey of the run comes back, and a join would
# rename the columns to age_<survey>, ...
past_data <- suppressMessages(formr_api_fetch_results(
  run_name = .formr$run_name, surveys = "intake",   # the survey holding these items
  item_names = c("bwd_result", "age", "weight")))
if (!is.data.frame(past_data)) past_data <- past_data$intake   # a run with a Shuffle gets a list
past_data$age    <- (past_data$age    - est_age_mean)    / est_age_sd     # SAME estimates!
past_data$weight <- (past_data$weight - est_weight_mean) / est_weight_sd

result <- balancr::bwd_assign_next(
  current_covariates     = c(scaled_weight, scaled_age),
  history                = past_data,
  history_covariate_cols = c("age", "weight"),
  bwd_settings           = list(N = 300, D = 2))

result$json_result  # return the JSON state; formr stores it back into this calculate item"""

_R_LIVE_COUNT = """library(dplyr)
formr_api_authenticate()

# Name the survey ("poll" here): with several surveys or a Shuffle, a join would
# rename `item` to item_<survey>.
votes <- formr_api_fetch_results(run_name = .formr$run_name, surveys = "poll", item_names = "item")
if (!is.data.frame(votes)) votes <- votes$poll   # a run with a Shuffle gets a list
tally <- votes |> count(item, name = "Count") |> rename(Answer = item)

dog <- sum(tally$Count[tally$Answer == "dog"], na.rm = TRUE)
cat <- sum(tally$Count[tally$Answer == "cat"], na.rm = TRUE)
ifelse(dog > cat, "dog", "cat")  # store as item_max; notes use showif current(item_max) == ..."""

_R_KNITR_IN_LABEL = """<!-- A knitr chunk placed directly in a NOTE label renders inline (kable/ggplot both work). -->
```{r warning=FALSE, message=FALSE, error=TRUE, echo=FALSE}
library(dplyr); library(ggplot2)
formr_api_authenticate()
votes <- formr_api_fetch_results(run_name = .formr$run_name, surveys = "poll", item_names = "item")
if (!is.data.frame(votes)) votes <- votes$poll   # a run with a Shuffle gets a list
df <- votes |> count(item, name = "Count") |> rename(Answer = item)
knitr::kable(df)                       # a live table, or:
ggplot(df, aes(as.factor(Answer), Count)) + geom_col(fill = "steelblue") + theme_minimal()
```"""

_R_SESSION_ADVANCE = """library(dplyr)
formr_api_authenticate()

waiting_users <- formr_api_sessions(.formr$run_name) |>   # all sessions + positions
  filter(position == 20) |> pull(session)                 # everyone parked in the waiting room

if (length(waiting_users) > 0) {
  formr_api_session_action(.formr$run_name, action = "move_to_position",
                           position = 10, session_codes = waiting_users)  # release the batch
}
TRUE"""

_R_CONVERGENCE = """# SkipForward condition: exit the loop once the estimate has converged.
# Replace adaptive_block / est_theta_se with your survey + SE column.
se  <- as.numeric(adaptive_block$est_theta_se)
n   <- length(se)
last_se   <- if (n >= 1) se[n] else NA_real_
se_change <- if (n >= 2) abs(se[n] - se[n - 1]) else 1   # 1 = "not converged" for early rounds
isTRUE(last_se <= 0.5 || se_change <= 0.003)"""

_R_EMAIL_GREETING = """`r ifelse(intake$gender == 2, "Dear Ms.", ifelse(intake$gender == 1, "Dear Mr.", "Hello"))` participant,

thanks for taking part. Your personal link to resume the study at any time:

**{{login_link}}**

This link always returns you to exactly where you last left off."""

# DRY: define this once in the run's custom_r store; call it from the External unit.
_R_SMS_HELPER_CUSTOM_R = """# --- put this in the run's custom_r (Settings -> R Functions) ---
# Helpers must take their data as ARGUMENTS — custom_r functions cannot see inline-R variables.
transform_phone_number <- function(number) {
  digits <- stringr::str_c(unlist(stringr::str_extract_all(number, "[0-9]")), collapse = "")
  loc <- stringr::str_locate(digits, "1[567]")[, 1]
  stringr::str_c("0049", stringr::str_sub(digits, start = loc))
}"""

_R_SMS_DISPATCH = """# --- in the External unit's address (R code) ---
# Credentials come from run SECRETS as the literal .formr$secret_<name> form.
to_number <- transform_phone_number(last(contact$phone))   # helper lives in custom_r
httr::GET("https://gate1.example.com/sms/sendsms.asp", query = list(
  receiver = to_number, sender = "MyStudy",
  msg = "Please complete today's short survey: https://example.org/run/today",
  id = .formr$secret_sms_id, pw = .formr$secret_sms_pw,
  msgtype = "t", test = "1"))   # "1" = dry-run; "0" actually sends"""

_R_CUSTOM_R_EXAMPLE = """# --- Settings -> R Functions (custom_r) — injected before EVERY R evaluation ---
# Functions take data as arguments; they cannot see variables from inline R code.
is_done <- function(ended_col) {
  !is.null(ended_col) && length(ended_col) > 0 && !all(is.na(ended_col))
}
safe_parse_json <- function(json_str, fallback) {
  tryCatch({
    p <- jsonlite::fromJSON(json_str)
    if (!is.data.frame(p)) p <- as.data.frame(p, stringsAsFactors = FALSE)
    if (!all(c("from", "to") %in% names(p))) fallback else p
  }, error = function(e) fallback)
}"""

_R_CUSTOM_R_CALLSITE = """# --- then anywhere in the run (condition / value / showif / body) ---
is_done(VIBE_SQ1_ready$ended)            # instead of re-writing the 6-line null/NA guard each time
safe_parse_json(network_1, empty_edges)  # pass the data + fallback in as arguments"""

# JSON-state store: tolerant parser. The fallback schema MUST list every column you read back
# (including `session` when you fetch with join = TRUE), or unnest/bind_rows breaks on one bad cell.
_R_JSON_SAFE_PARSE = """# Best kept in custom_r so every unit shares one tested parser (see dry-r-functions).
safe_parse_json <- function(json_str) {
  fallback <- data.frame(
    timestamp = NA_character_, session = NA_character_,
    from = NA_character_, to = NA_character_, remove = NA_character_,
    stringsAsFactors = FALSE)                 # columns must match what you read back
  tryCatch({
    parsed <- jsonlite::fromJSON(json_str)
    if (!is.data.frame(parsed)) parsed <- as.data.frame(parsed, stringsAsFactors = FALSE)
    if (!all(c("from", "to") %in% names(parsed))) fallback else parsed
  }, error = function(e) fallback)            # empty/garbled cell must not crash everyone
}"""

# WRITE PATH: a calculate value. formr APPENDS a new DB row each evaluation (per session,
# per loop iteration) — there is no in-place update, so writes never collide or get lost.
_R_JSON_WRITE = """# value of a calculate item (e.g. edge_1). Re-evaluations add history; they don't overwrite.
formr_api_authenticate()
record <- list(
  timestamp = format(Sys.time(), "%Y-%m-%d %H:%M:%OS6"),   # ordering key: reconstruction keeps latest
  session   = survey_run_sessions$session,
  from      = survey_run_sessions$session,
  to        = current(connect$target),
  remove    = "FALSE")                                       # "TRUE" = tombstone (a deletion)
jsonlite::toJSON(record, auto_unbox = TRUE)                  # formr stores this string as a new row"""

# READ PATH: reconstruct the global state from EVERY session's rows.
_R_JSON_RECONSTRUCT = """formr_api_authenticate()
raw <- formr_api_fetch_results(.formr$run_name,
         item_names = c("edge_1", "edge_2", "edge_3"), join = TRUE)  # all sessions, all rows

state <- raw |>
  tidyr::pivot_longer(tidyselect::starts_with("edge"),
                      values_to = "payload", values_drop_na = TRUE) |>
  dplyr::mutate(
    payload = stringr::str_trim(payload),
    payload = dplyr::if_else(stringr::str_starts(payload, stringr::fixed("[")),
                             payload, paste0("[", payload, "]")),     # wrap a lone object as an array
    parsed  = purrr::map(payload, safe_parse_json)) |>
  tidyr::unnest(parsed) |>
  dplyr::filter(!is.na(from), !is.na(to)) |>
  dplyr::group_by(from, to) |>
  dplyr::filter(timestamp == max(timestamp), remove != "TRUE") |>     # latest wins; drop tombstoned
  dplyr::slice(1) |> dplyr::ungroup()"""


# ── pattern registry ────────────────────────────────────────────────

_PATTERNS: list[dict] = [
    {
        "name": "loading-screen",
        "title": "Loading screen for heavy R computation",
        "problem": "A heavy calculate item hangs a page on its GET request, so participants stare at a frozen browser and drop out.",
        "when_to_use": "Any time a calculate runs slow R (API calls, model fitting, balancing) and you want a 'please wait' message shown DURING the work.",
        "structure": (
            "One Survey split into two pages by a hidden auto-submit. Page 1: a 'please wait' note + a "
            "submit with class 'hidden' and type_options \"1\" (~1 ms auto-submit). Page 2: the heavy "
            "`calculate` item, then a 'done' note + a normal submit."
        ),
        "how_it_works": (
            "A calculate runs server-side during the GET that renders ITS page; formr holds the response "
            "until it returns. If the calculate shares a page with the wait message, the page can't paint "
            "until the work is done. Splitting it onto page 2 behind a ~1 ms auto-submit means the browser "
            "keeps page 1 (the wait message) painted while page 2 — carrying the calculate — is computed."
        ),
        "key_r": [],
        "gotchas": [
            "The auto-submit submit needs class 'hidden' and a STRING type_options ('1'); any small value works.",
            "Put the wait note on the page BEFORE the calculate, never the same page.",
        ],
    },
    {
        "name": "balanced-assignment",
        "title": "Dynamic condition balancing (smaller cell wins)",
        "problem": "Assign each new participant to whichever experimental group currently has fewer COMPLETED sessions.",
        "when_to_use": "Between-subjects designs wanting roughly equal, completion-weighted cells without a fixed randomization list.",
        "structure": (
            "A `group_selector` Survey whose `calculate` (named e.g. `group`) does the assignment behind a "
            "loading screen, then your real questionnaire Survey that shows the group via "
            "`current(group_selector$group)` and records a completion flag the next person's balancing reads."
        ),
        "how_it_works": (
            "The calculate fetches everyone's data, counts completed participants per cell, and returns the "
            "smaller one. Because the fetch is slow, wrap it in the loading-screen pattern."
        ),
        "key_r": [{"label": "calculate `group`", "code": _R_BALANCED_ASSIGNMENT}],
        "gotchas": [
            "A join renames columns to <item>_<survey> whenever more than one table comes back (several surveys, or a run whose Shuffle has assigned anyone) — reference df$group_group_selector, not df$group. This trips up nearly everyone.",
            "Count only COMPLETED participants or drop-outs bias the cells.",
            "`current(survey$item)` reads from a DIFFERENT survey in the same run; a bare variable only works on the calculate's own page.",
        ],
    },
    {
        "name": "covariate-balancing",
        "title": "Confounder balancing on continuous covariates",
        "problem": "Balance conditions across continuous confounders (age, weight, ...), not just equal cell counts.",
        "when_to_use": "Designs where pre-treatment covariates must be balanced between conditions (online covariate balancing / minimization).",
        "structure": (
            "A Survey collecting the covariate items, a submit (execution boundary), then a `calculate` "
            "(`bwd_result`) calling balancr::bwd_assign_next(). The assignment is shown by parsing the "
            "returned JSON inline: `` `r jsonlite::fromJSON(bwd_result)$assignment` ``."
        ),
        "how_it_works": (
            "State is stored as a JSON string inside the calculate item itself — each participant deserializes "
            "the prior state, adds their data, and re-serializes (stateless, DB-backed). The submit before the "
            "calculate is an execution boundary: covariates aren't available until it's posted."
        ),
        "key_r": [{"label": "calculate `bwd_result`", "code": _R_COVARIATE_BALANCING}],
        "gotchas": [
            "Scale history with the EXACT SAME fixed estimates you scale the current participant with.",
            "Requires the `balancr` R package.",
            "The JSON state in bwd_result is a special case of json-state-store (one row per session) — see that pattern for the concurrency caveats.",
            "For a demo that lets people re-enter, add SkipBackward(TRUE, if_true=first position); omit it in a real study so participants aren't trapped in a loop.",
        ],
    },
    {
        "name": "waiting-room",
        "title": "Waiting room — synchronize participants",
        "problem": "Hold participants at a position until an admin (or a paired participant) releases a batch — e.g. dyadic/group studies, matchmaking.",
        "when_to_use": "Real-time coordination: park sessions at one position, then move them together with formr_api_session_action().",
        "structure": (
            "A menu Survey (mc_button: wait / advance) + a SkipForward routing 'advance' to an admin panel. "
            "A waiting Survey whose hidden submit has type_options \"2000\" (auto-refresh every 2 s), followed "
            "by SkipBackward(TRUE) back to itself — the polling loop. An admin Survey whose calculate calls "
            "formr_api_session_action(move_to_position) on everyone parked in the waiting room."
        ),
        "how_it_works": (
            "The 2 s auto-submit + SkipBackward(TRUE) refresh the waiting page on a timer. Moving a session to "
            "another position from the admin panel breaks it out of the loop. Hide the progress bar with custom "
            "CSS (`.progress { visibility: hidden !important; }`) so it doesn't look stuck."
        ),
        "key_r": [{"label": "admin advance calculate", "code": _R_SESSION_ADVANCE}],
        "gotchas": [
            "type_options is a string: '2000' (poll every 2 s), 'auto' (submit on choice).",
            "The releasing admin uses formr_api_session_action(action='move_to_position'); participants can't release themselves.",
        ],
    },
    {
        "name": "live-aggregate-feedback",
        "title": "Live aggregate feedback (real-time results + conditional content)",
        "problem": "Show participants live aggregate results from all respondents and adapt content to the current majority.",
        "when_to_use": "Polls, wisdom-of-crowd demos, content that depends on what everyone else has answered so far.",
        "structure": (
            "A Survey: the vote item + submit, a `calculate` (`item_max`) returning the current majority, a "
            "short auto-submit ('800') to the results page, notes carrying knitr chunks for a live table/plot, "
            "notes gated by `showif: current(item_max) == \"...\"`, then SkipBackward(TRUE) to loop voters."
        ),
        "how_it_works": (
            "item_max polls all participants and returns the running majority; notes use showif on it for "
            "conditional content. Live tables/plots come from a ```{r}``` knitr chunk placed directly in a "
            "note label. Items after a submit only run AFTER posting, so the tally includes the just-cast vote."
        ),
        "key_r": [
            {"label": "calculate `item_max`", "code": _R_LIVE_COUNT},
            {"label": "knitr chunk inside a note label", "code": _R_KNITR_IN_LABEL},
        ],
        "gotchas": [
            "Reach the Endpage only via an admin move_to_position — the SkipBackward(TRUE) otherwise loops forever.",
            "knitr chunks render in note labels AND in Page/Email bodies; inline `` `r ...` `` works too.",
        ],
    },
    {
        "name": "adaptive-loop",
        "title": "Adaptive loop with a convergence stopping rule",
        "problem": "Administer items repeatedly, updating an estimate each round, and stop once it converges (adaptive/IRT testing).",
        "when_to_use": "Computerized adaptive testing, staircase procedures, any iterate-until-converged measurement.",
        "structure": (
            "A Survey that administers an item/block and updates the estimate (in a calculate or External). "
            "Then TWO control units: a SkipForward whose condition is the convergence check (exit to the end), "
            "followed by SkipBackward(TRUE) that loops back to the survey. The exit check must come FIRST."
        ),
        "how_it_works": (
            "After the block, the SkipForward tests convergence and jumps to the end if met; otherwise control "
            "falls through to the SkipBackward, which loops. The heavy IRT math (catR/mirt) lives in a "
            "calculate or External inside the block — strong candidate for a custom_r helper (see dry-r-functions)."
        ),
        "key_r": [{"label": "SkipForward convergence condition", "code": _R_CONVERGENCE}],
        "gotchas": [
            "Order matters: exit-check (SkipForward) before the loop (SkipBackward).",
            "Guard the convergence math for early rounds (too few estimates to diff) — the length checks above do this without tryCatch.",
        ],
    },
    {
        "name": "personalized-email",
        "title": "Personalized email with inline-R greeting and resume link",
        "problem": "Email a participant with a greeting adapted to their data and a personal link back into the run.",
        "when_to_use": "Welcome / invitation / reminder emails in longitudinal or multi-session studies.",
        "structure": (
            "An intake Survey collecting the address (+ any variable the greeting depends on), an optional "
            "SkipForward that skips the Email when the address is missing, then an Email whose body uses inline "
            "R for the salutation and the {{login_link}} placeholder for the resume link."
        ),
        "how_it_works": (
            "recipient_field names the survey item holding the address (e.g. intake$start_email, or 'most recent "
            "reported address'). account_id must be a real configured email account."
        ),
        "key_r": [{"label": "Email body", "code": _R_EMAIL_GREETING}],
        "gotchas": [
            "Inline R (`` `r ...` ``) works in the SUBJECT too (knitted as plain text), but the subject gets no Markdown, no {{login_link}}-style placeholders and no API token: formr_api_*() calls belong in the BODY.",
            "Use the {{login_link}} placeholder; don't hand-build the link.",
            "A wrong account_id is a common silent failure.",
            "Don't use formr_connect()/formr_raw_results() — deprecated.",
        ],
    },
    {
        "name": "external-api-dispatch",
        "title": "Call an external API (e.g. SMS) from an External unit",
        "problem": "Trigger a third-party service — send an SMS, post to a webhook — as part of the run.",
        "when_to_use": "ESM/diary studies that text reminders, or any external HTTP integration.",
        "structure": (
            "A Survey collecting the input (e.g. a tel item), then an External unit whose `address` is R code "
            "(not an http URL) that calls the API with httr. api_end: 0 = fire-and-forget; 1 = wait for callback."
        ),
        "how_it_works": (
            "Factor the reusable transform (phone normalisation, payload building) into the run's custom_r so "
            "every reminder unit calls one tested helper. Read gateway credentials from run SECRETS via the "
            "literal .formr$secret_<name> form — never hardcode them. See custom-r-and-secrets."
        ),
        "key_r": [
            {"label": "helper in custom_r (DRY)", "code": _R_SMS_HELPER_CUSTOM_R},
            {"label": "External unit address", "code": _R_SMS_DISPATCH},
        ],
        "gotchas": [
            "External runs R only when `address` is NOT an http(s) URL.",
            "Keep test=\"1\" (dry-run) until verified; \"0\" actually sends.",
            "Secrets are injected only if the code literally contains .formr$secret_<name> (no dynamic get()).",
            "custom_r helpers can't see inline-R variables — pass the phone number in as an argument.",
        ],
    },
    {
        "name": "json-state-store",
        "title": "Stateless JSON state store (handle concurrency carefully)",
        "problem": "You need to accumulate structured shared state across participants (a social network, a booking grid, a running tally of objects) but formr has no mutable run-level store.",
        "when_to_use": "Any growing, structured, cross-participant state: edges in a network, claimed slots, multi-field records that outlive a single survey.",
        "structure": (
            "Each session stores its record(s) as a JSON string in a calculate item (one, or a few like "
            "edge_1/edge_2/edge_3). Because formr appends a new DB row on every evaluation, these accumulate a "
            "history automatically. To use the state, a `calculate` rebuilds the global picture: fetch every "
            "session's values with `join = TRUE`, parse each safely, row-bind, and resolve to the latest per key "
            "(dropping tombstones)."
        ),
        "how_it_works": (
            "formr storage is append-only by construction: every time a session evaluates a calculate item — "
            "including each pass through a SkipBackward loop — formr writes a NEW results row, keyed by session, "
            "iteration and timestamp. You cannot mutate a value in place or overwrite another session's data, so "
            "there are no lost writes. The care goes into READ and into races, not writes: (1) a session/item can "
            "have many rows, so resolve to the latest per key (group_by + filter timestamp == max, or last()); "
            "(2) reads are eventually consistent with no locking, so two participants acting at once each compute "
            "a snapshot that misses the other's just-written row — the hazard is duplicate DECISIONS (both claim "
            "the last slot, both get sent to the smaller cell), not lost data. Design so that's tolerable: "
            "over-allocate and reconcile, or make the decision idempotent; formr offers no atomic claim. "
            "covariate-balancing is a special case (one record per session, reconstructed by balancr)."
        ),
        "key_r": [
            {"label": "safe_parse_json (tolerant, schema-matched fallback)", "code": _R_JSON_SAFE_PARSE},
            {"label": "write — append this session's record", "code": _R_JSON_WRITE},
            {"label": "read — reconstruct global state", "code": _R_JSON_RECONSTRUCT},
        ],
        "gotchas": [
            "formr never overwrites: each calculate evaluation (and each loop iteration) appends a new row keyed by session + iteration + timestamp. Append-only is the data model, not something you implement — there are no lost writes to guard against.",
            "The real hazard is races, not lost data: with no locking, concurrent participants act on snapshots that miss each other's pending rows (double-booking, both-to-smaller-cell). Make the outcome tolerable/idempotent — there is no atomic claim.",
            "READ resolves history: a session/item can return several rows (one per iteration); collapse to the latest per key with group_by + filter(timestamp == max(...)) or last()/tail().",
            "Deletions: you can't remove a row, so append a tombstone (remove = \"TRUE\") and drop it during reconstruction.",
            "safe_parse_json's fallback schema must list EVERY column you read back — including `session` under join=TRUE — or unnest/bind_rows fails on a single bad cell.",
            "jsonlite::fromJSON returns a list (not a data.frame) for a single object — coerce with as.data.frame; wrap a lone object as an array before parsing.",
            "State grows unbounded across iterations/participants; keep payloads compact (result_log is MEDIUMTEXT but finite).",
            "Factor safe_parse_json + the reconstruction into custom_r so every unit shares one tested copy (see dry-r-functions).",
        ],
    },
    {
        "name": "dry-r-functions",
        "title": "Share repeating R via custom_r (keep runs DRY)",
        "problem": "The same R (null/NA guards, JSON parsing, phone formatting, scoring) is copy-pasted into many conditions/calculates, so a fix means editing N places and each copy drifts.",
        "when_to_use": "Any run where ≥2 units share non-trivial R, or where a condition keeps re-deriving the same defensive scaffolding.",
        "structure": (
            "Define named functions and globals once in the run's custom_r store (Settings -> R Functions; the "
            "MCP setting `custom_r`). formr injects them before EVERY R evaluation, so they're callable by name "
            "in showif, value, condition, label, Page/Email body, and External code."
        ),
        "how_it_works": (
            "custom_r is the run-level analogue of custom CSS/JS. Read it from settings.custom_r in the structure "
            "file, write it with update_run_settings(custom_r=...). The single hard rule: a custom_r function "
            "CANNOT see variables from inline R code — it only sees its own arguments and globals, so pass survey "
            "data in explicitly (e.g. is_done(VIBE_SQ1_ready$ended))."
        ),
        "key_r": [
            {"label": "define once in custom_r", "code": _R_CUSTOM_R_EXAMPLE},
            {"label": "call from anywhere in the run", "code": _R_CUSTOM_R_CALLSITE},
        ],
        "gotchas": [
            "Functions take data as ARGUMENTS — they can't see inline-R variables.",
            "custom_r round-trips in the structure file under settings.custom_r; update it with update_run_settings.",
            "Good candidates: the 6-level exists/is.null/length/is.na guards, safe_parse_json, transform_phone_number, scoring formulas.",
        ],
    },
]

_BY_NAME = {p["name"]: p for p in _PATTERNS}


# ── accessors ───────────────────────────────────────────────────────

def pattern_names() -> list[str]:
    """All registered pattern names."""
    return [p["name"] for p in _PATTERNS]


def list_patterns() -> list[dict]:
    """Short catalog of every pattern: name, title, and the problem it solves."""
    return [
        {"name": p["name"], "title": p["title"], "problem": p["problem"]}
        for p in _PATTERNS
    ]


def get_pattern(name: str) -> dict:
    """Full guidance for one pattern: the problem it solves, when to use it, the structure
    (which units/fields), how it works, the reusable R idioms (key_r), and gotchas.

    Informational only — adapt it to the specific run with the editing tools; there is no
    drop-in unit blueprint. Raises ValueError (listing available names) for an unknown pattern.
    """
    p = _BY_NAME.get(name)
    if p is None:
        available = ", ".join(pattern_names())
        raise ValueError(f"Unknown pattern '{name}'. Available: {available}")
    return {
        "name": p["name"],
        "title": p["title"],
        "problem": p["problem"],
        "when_to_use": p["when_to_use"],
        "structure": p["structure"],
        "how_it_works": p["how_it_works"],
        "key_r": p["key_r"],
        "gotchas": p["gotchas"],
    }
