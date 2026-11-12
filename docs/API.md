# API reference

Base URL: your deployed Django API. JSON endpoints use
`Content-Type: application/json`.

## `GET /`

Returns a basic service health response. It does not check external AI-provider
availability.

## Sign-in and bearer token

Successful verified responses from `POST /login`, `/google-login`, and `/verify`
include `access_token`, `token_type`, and `expires_in` when
`AUTH_TOKEN_SECRET` (or the legacy `SECRET_KEY`) is configured. The signed
token identifies the account and expires after `AUTH_TOKEN_TTL_SECONDS`
(default 604800 seconds / seven days). Keep it private.

```json
{
  "access_token": "<signed-token>",
  "token_type": "Bearer",
  "expires_in": 604800
}
```

## `POST /recommend`

Requires a verified account's bearer token. A supplied email must match the
authenticated account; the API binds recommendation ownership to the signed-in
identity.

```http
Authorization: Bearer <access-token>
Content-Type: application/json
```

Example body:

```json
{
  "interest": "computer science and software development",
  "grades": {
    "Math": "A-",
    "English": "B",
    "Kiswahili": "B",
    "Biology": "B+",
    "Physics": "C",
    "Chemistry": "C+",
    "ComputerStudies": "A"
  }
}
```

### Three-attempt lifetime cap

Three admitted recommendation requests are allowed per authenticated account
over its lifetime. Each request consumes one attempt immediately after
authentication, including a request whose AI pipeline later fails. Attempts
persist in `recommendation_usage`. At the limit the AI pipeline is not called,
and a fourth request returns HTTP `402`:

```json
{
  "error": "You have used all 3 complimentary recommendation attempts. Please request funding to continue.",
  "code": "funding_required",
  "limit": 3,
  "funding_message": "Please contact the app administrator to request additional AI usage funding."
}
```

This is not a payment system. Funding approval and counter adjustments require
a secure administrator process; there is no public reset/funding endpoint. The
cap is on endpoint attempts, not internal LLM calls or retries.

### Errors

- `401 authentication_required` — bearer token absent.
- `401 invalid_token` — invalid/expired token or deleted account.
- `403 account_not_verified` — account is not verified.
- `403 account_mismatch` — request email differs from signed-in account.
- `402 funding_required` — three attempts used; request funding.
- `502 course_not_in_catalog` — the AI course suggestion was not in the KUCCPS catalog.
- `503 auth_not_configured` — signing secret is missing.
- `503 quota_unavailable` — persistent quota check failed; request is denied.

### Course and institution verification

The KUCCPS catalog in `utils/kuccps_courses.json` is used to validate course
names; it does not contain institution-to-course relationships. The verified
offering index is `data/course_urls.json`, with entries such as:

```json
{
  "schema_version": 1,
  "entries": [{
    "university": "Kenyatta University",
    "domain": "ku.ac.ke",
    "course": "Bachelor of Science (Nursing)",
    "url": "https://www.ku.ac.ke/official-course-page",
    "verified": true,
    "status": "verified"
  }]
}
```

Saved links are fetched again before use. The response must remain on the
institution's HTTPS domain from `data/school_domains.json`, return HTTP 200,
and identify the specific course in its title, headings, or a dedicated
course-title element. Mentions in navigation or unrelated page text do not
count. Generic degree names without a specific subject are not linked. If a
saved link fails those checks, the API tries other saved links for the same
course before searching for a course-specific page on the official domain. It
checks the search result and destination page. It
does not treat an AI-suggested URL, a homepage, or a search-results page as a
verified offering.
If no page can be confirmed, `universities` is empty and
`institution_match_status` is `not_verified`; the API does not invent a
university match. Entry requirements are not validated by this page check and
should be confirmed on the linked official page.

Recommendation course selection is constrained to exact titles from the saved
KUCCPS course registry. A student's entered grades do not remove a course from
the matching choices; they are assessed separately so a partial test can still
show the other listed subject requirements.

The response includes `admission_requirements`, derived from the structured
`data/course_requirements.json` extraction of
`utils/DEGREE_PROGRAMMES_2025 (1).pdf`. Each programme record includes the
institution, KUCCPS programme code, source PDF page, alternative subjects
(`subjects` joined with “or”), minimum grade, and the assessment status for
grades supplied by the student. The data retains unparsed source cells rather
than guessing their meaning. These subject minima do not establish overall
admission: mean grade, historical cutoffs, cluster points, current-year rules,
and institution-specific requirements may also apply. Rebuild the extracted
data with `python utils/kuccps_requirements.py`.

The response also includes `kuccps_cutoff` on each university candidate and
matching cutoff data on each `admission_requirements.records` item, derived
from the latest official KUCCPS cutoff PDF fetched online at
`https://statics.kuccps.net/uploads/globalFiles/DEGREE_CUTOFFS_14_07_2025.pdf`.
The backend refreshes this source at most once per day, validates the HTTPS
host and PDF contents, and falls back to `data/course_cutoffs.json` if the
online source is unavailable. The current public document contains cutoff
values through 2024; it does not provide current-cycle thresholds. Each cutoff
includes its retrieval status and source URL where available. Values are
historical reference points, not admission guarantees. Rebuild the offline
snapshot with `python -m utils.kuccps_cutoffs`.

Successful recommendations are saved once to the signed-in user's history.
They include `report_metadata` with the student's request, the provided grades,
grade-assessment completeness, aggregate points when seven valid grades are
available, data limitations, and source documents. An explicit chat request
for a report uses the student's existing interest/profile to generate and save
the standard recommendation report. If no interest has been shared yet, the AI
continues the conversation to gather that missing context first.

For example, one recorded subject group has this shape:

```json
{
  "subjects": ["English", "Kiswahili"],
  "minimum_grade": "C+",
  "attained": [{"subject": "English", "grade": "B"}],
  "status": "Met"
}
```

Subjects in one group are alternatives; separate groups are cumulative.

When `discover_program_catalogue.py` saves specifically verified course pages,
those links are also merged into `data/course_urls.json`; one official page can
remain linked to multiple distinct courses. Runtime lookup still revalidates
each page against the institution's official domain and course title.

## `GET|POST /data-consent`

Requires a verified account's bearer token. Existing accounts start with both
optional choices turned off; submitting `false` later revokes the choice. The
dashboard uses `personalization` to control local chat/profile persistence and
`improvement` to control whether a de-identified career category may be
included in aggregate career trends.

```json
{
  "personalization": true,
  "improvement": false
}
```

`GET` returns the saved choices, whether a choice has been recorded, its
timestamp and policy version, and `model_training_active: false`. CareerPath AI
does not currently train AI models on user data. A separate consent will be
required before any model-training use begins. The data a user submits to
generate a requested recommendation is still processed by the configured AI
provider, and the resulting report is saved to the user's account history;
these optional choices do not control that essential service processing.

`POST /recommend` additionally requires `improvement_consent: true` in its
request body, as well as a saved `improvement: true` choice, before it records
an aggregate contribution. Such a record contains only the generated career
category; it excludes name, email, grades, points, and conversation history.

## Other routes

All routes accept the methods listed below. JSON request bodies should use
`Content-Type: application/json`, except multipart chat uploads.

| Route | Method | Purpose |
| --- | --- | --- |
| `/` | `GET` | Basic service health response. |
| `/register` | `POST` | Create an account and send an email verification code. |
| `/verify` | `POST` | Verify an account; may return a bearer token. |
| `/login` | `POST` | Sign in with email or username and password. |
| `/google-login` | `POST` | Verify a Google ID token and sign in. |
| `/resend-code` | `POST` | Send a new verification code. |
| `/forgot-password` | `POST` | Email a temporary password to the account address. |
| `/chat` | `POST` | Generate a chat response; supports multipart attachments. |
| `/recommend` | `POST` | Generate recommendations; requires bearer authentication and consumes one attempt. |
| `/history` | `GET`, `POST` | Read saved reports by username/email (`GET`) or email (`POST`). |
| `/save-history` | `POST` | Append a report to account history. |
| `/data-consent` | `GET`, `POST` | Read or update optional data-use choices; requires bearer authentication. |
| `/change-username` | `POST` | Change the username for an email address. |
| `/change-password` | `POST` | Change a password after checking the current password. |
| `/delete-account` | `POST`, `DELETE` | Permanently delete the account identified by email. |
| `/scrape` | `POST` | Look up an institution course page. |
| `/send-report` | `POST` | Queue a base64-encoded PDF report for email delivery. |

### Chat request limits

Send a JSON object with `messages`, an array of 1–24 `{ "role": "user" |
"assistant", "content": "..." }` entries. Each content string may contain up
to 12,000 characters, with a combined maximum of 24,000 characters. The latest
message must be from the student. The optional `assistant_name` must be 1–32
letters, numbers, spaces, or simple punctuation.

For attachments, use `multipart/form-data` with a `messages` JSON field and
repeat the `attachments` file field. Up to three non-empty PDF, JPEG, PNG,
WebP, MP4, MOV, or WebM files are accepted, each up to 5 MB. File extension,
MIME type, and basic file signature are checked. Attachment analysis requires
the Gemini provider to be configured.

### Report email request

`POST /send-report` expects JSON containing `email` and `pdf_data`; the latter
is a base64 string or data URL for a PDF of no more than 12 MB. The response
uses HTTP `202` when delivery is queued; it does not mean the recipient has
received the email.

## Authorization boundary

Bearer authentication and the three-attempt quota currently protect only
`/recommend`; `/data-consent` also requires a bearer token. Other account, history, chat, scraping, and email endpoints do
not consistently require a bearer token. Do not treat this API reference or a
frontend login state as proof that those routes are authorized for the
requesting user. Review and enforce server-side authorization before exposing
sensitive routes to untrusted clients. See the [security policy](../SECURITY.md).

## Error responses

Error objects are not fully standardized: endpoints may return a `message`
field or an `error` field, and recommendation authentication errors also
include a stable `code`. Clients should check the HTTP status and handle
missing optional fields rather than assuming every response has the same
shape.
