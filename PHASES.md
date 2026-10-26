# Transcode — what we are building

A person logs in. The browser keeps a session cookie the page cannot read. The API then gives the browser a short-lived URL, and the video bytes go straight to storage. The API records the job and an outbox row in one database transaction. A worker, woken by Pub/Sub, reads that object, cuts it into playable chunks at two sizes, and writes the playlist. The page polls and plays it.

Three programs are ours. The others store the session, the job, the event, or the bytes.

```text
browser
   |  cookie on every /api call
   |  PUT video bytes to a 15-minute signed URL
   v
 web            api                         publisher          worker
(screen) ----> (session, job, outbox) --> (Pub/Sub) -------> (ffmpeg)
                  |                            ^                  |
                  +----------- Postgres -------+                  |
                  |            sessions, jobs, outbox             |
                Storage inbox  <--- browser PUT                   |
                  ^                                                |
                  +------------- source object ------ worker      |
                  ^                                                |
                  +-------- chunks and playlist -------------------+
```

| Piece | In one sentence | Who starts it |
|---|---|---|
| `web` | Login, upload, poll, player | The user |
| `api` | The only program the browser calls | `web` |
| publisher | Reads unpublished outbox rows and publishes them | A loop in `api`, and a sweeper if that loop died |
| `worker` | Reads the source, writes HLS, acks the message | Pub/Sub |
| Storage | Inbox for the original, output for the playlist and `.ts` chunks | The browser writes the source with a signed URL. The worker writes the output |
| Postgres | Users, sessions, jobs, outbox | `api` and `worker` |
| Pub/Sub | The event, kept until the worker acks | The publisher writes, the worker reads |

`web` never talks to `worker`, Postgres, or Pub/Sub. It talks to `api`, and it PUTs the file to storage. Upload and job routes require a live session. Health does not. The worker never sees the cookie, the password, or the signed upload URL. It sees the job id on the queue.

On your laptop the same steps run against Postgres, the Pub/Sub emulator, and a Cloud Storage emulator that can sign URLs. Those are the real protocols with local processes, not a second design. GCP replaces the processes with Cloud SQL, Pub/Sub, and Cloud Storage. The code path does not change.

---

## What is stored, and where

| Data | Where | What a refresh does |
|---|---|---|
| Email and password while typing | React state | Gone. The form is empty |
| Password after signup | A hash on `users` | Stays |
| Session | Random id in an `HttpOnly` cookie, row in `sessions` | Stays. The next `GET /api/session` returns the user |
| CSRF token | Returned by `GET /api/session`, held in React state, sent as `X-CSRF-Token` on POST | Fetched again with the session. Not in `localStorage` |
| Chosen file before the PUT finishes | The browser's file input | Gone. The browser will not restore a file input. The job row remains |
| Original video | Inbox object | Stays once the PUT succeeded |
| Job, outbox, playlist | Postgres and the output bucket | Stay |

The cookie is `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`. JavaScript cannot read it. `Secure` works on `localhost`. Logout sets `revoked_at` and clears the cookie, so a copied cookie stops working. A password change revokes every session for that user. An expired or revoked session is `401`.

There is no token in `localStorage` or `sessionStorage`. The Firebase ID token, when Firebase is the login, is sent once to create this session and is not kept.

---

## What one job looks like

| Field | Meaning | Who sets it |
|---|---|---|
| `id` | Job id | Postgres |
| `user_id` | Who created it | The server, from the session |
| `filename` | Original name | The client, as a label only |
| `content_type` | Allowed video type | The client, checked again against the stored object |
| `size_bytes` | Declared size | The client, checked again against the stored object |
| `idempotency_key` | Client retry key, unique per user | The client header |
| `status` | `awaiting_upload`, `uploaded`, `transcoding`, `ready`, `failed`, `expired` | The server. A new job is `awaiting_upload` |
| `error` | Why it failed or expired | Empty until then |
| `attempts` | Encode tries | The worker |
| `lock_expires_at` | Lease while ffmpeg runs | The worker |
| `source_path` | Inbox object name | The API, before it signs the URL |
| `playlist_url` | Player URL | Null until the worker has uploaded `master.m3u8` |
| `created_at` | When the row was committed | The server |

`playlist_url` null means not playable yet. It is not an empty string.

An outbox row is written in the same transaction as the change to `uploaded`: `event`, `job_id`, `published_at` null. A unique key on `(job_id, event)` means a second complete cannot enqueue a second encode.

---

## The request path

### Login survives a refresh

| Method and path | What it does | Success | Failure |
|---|---|---|---|
| `GET /api/health` | Process is up, and a database query works. No session | `200` | `500` if the database is down |
| `POST /api/signup` | Email and password, at least 8 characters. Stores a hash | `201` user, no hash | `422` bad email or short password. `409` email exists |
| `POST /api/login` | Checks the hash. Inserts a session. Sets the cookie | `200` and the user | `401` wrong email or password. `429` after too many failures for that account |
| `GET /api/session` | Reads the cookie, loads the row, returns the user and a CSRF token | `200` | `401` if the cookie is missing, expired, or revoked |
| `POST /api/logout` | Sets `revoked_at`, clears the cookie | `204` | `401` if there was no session |
| `POST /api/videos` | JSON only: filename, content type, size, plus `Idempotency-Key` and `X-CSRF-Token`. Inserts `awaiting_upload` and returns a signed PUT URL that expires in 15 minutes | `201` and the job plus `upload_url` | `401`, `403` if the CSRF header does not match, `422` if the type is not allowed or the size is 0 or over 2 GiB, `409` if that idempotency key was already used with a different body, `429` if this user is creating jobs too fast |
| `POST /api/videos/{id}/upload-url` | A new 15-minute URL for the same object, only while the job is `awaiting_upload` | `200` | `404` if the job is not this user's. `409` if the job has already moved on |
| `POST /api/videos/{id}/complete` | Heads the object. Size, type, and non-zero must match. Same transaction: `uploaded` and the outbox row | `202` | `422` if the object is missing, empty, or the wrong size or type. The job stays `awaiting_upload` so the person can PUT again. `202` again if it was already `uploaded` or later, with no second outbox row |
| `GET /api/videos` | This user's jobs, newest first | `200` | `401` |
| `GET /api/videos/{id}` | This user's job | `200` | `401`, or `404` if the id is missing or belongs to someone else |
| `POST /api/videos/{id}/playback` | After `ready`, authorizes the player for one hour | `200` | `409` if the job is not `ready`. `404` if it is not this user's |

Allowed types are `video/mp4`, `video/quicktime`, and `video/webm`. The filename is not the check.

A file over 32 MiB uses a resumable upload session, still bound to that object and that expiry, so a dropped connection continues from the last byte. A smaller file is one signed PUT. Either way the bytes do not pass through the API.

On GCP, login can be Firebase. The page sends the Firebase ID token once to `POST /api/session`. The API checks the signature, expiry, and audience, then inserts the same `sessions` row and sets the same cookie. After that the browser uses the cookie. Locally, `POST /api/login` is that door without Firebase. Logout, refresh, and revocation work the same in both.

The page and the API are one host, through the Vite proxy or nginx, so the cookie is first-party and `SameSite=Lax` is sent on the API calls. A split host would allow that one origin and send credentials. It would not use `*`.

### From the button to the player

1. On load the page calls `GET /api/session`. `200` shows the app. `401` shows the login form.
2. The person picks a file. The page sends `POST /api/videos` with the cookie, the CSRF header, and an idempotency key. The button stays disabled until the PUT and the complete call finish.
3. The browser PUTs the bytes to `upload_url`.
4. The page calls `complete`. The API checks the object, commits `uploaded` and the outbox row, and returns `202`.
5. The publisher publishes `{"event":"video.uploaded","job_id":12}` and sets `published_at`. The message is the job id. It is not the file, the cookie, or the signed URL.
6. The worker locks the job, sets `transcoding`, closes the database session, probes the file, runs ffmpeg, uploads the playlist last, sets `ready` and `playlist_url`, then acks.
7. The page polls `GET /api/videos/{id}` while the status is `awaiting_upload`, `uploaded`, or `transcoding`. `ready` calls `playback`, then starts the player. `failed` and `expired` show `error`.

ffmpeg is not inside any HTTP request. A transcode can take minutes. The API requests stay short.

### What the worker writes

```text
outputs/12/master.m3u8
outputs/12/480p/index.m3u8
outputs/12/480p/seg_000.ts
outputs/12/720p/index.m3u8
outputs/12/720p/seg_000.ts
```

It uploads the `.ts` segments and the rendition playlists first, and `master.m3u8` last. `playlist_url` is set only after that object exists. A retry deletes an incomplete `outputs/12/` prefix before writing again, so a half-written tree is not what the player opens.

Playback on GCP is a Cloud CDN signed cookie for `outputs/{id}/`, lifetime one hour, minted by the playback route after the session and the owner check. HLS requests the playlist and every segment, so one signed URL on `master.m3u8` would not authorize the segments. The buckets stay private. Locally the player uses `/api/videos/{id}/media/...`, and that route checks the session on every segment. Same rule, no public files. The laptop does not pretend to be a CDN.

---

## What breaks

| What happens | What the system does |
|---|---|
| Bad email, short password, size 0, disallowed type | `422`. No session and no job, except a bad complete, which leaves the existing `awaiting_upload` job so they can PUT again |
| Email already registered | `409` |
| Wrong password | `401`. Failures for that account are counted in Postgres. Too many in a short window is `429`. Cloud Armor rate-limits login and `POST /api/videos` by IP in front of that |
| Missing, expired, or revoked cookie | `401`. The page shows login |
| POST without the CSRF header | `403` |
| Another user's job id | `404` |
| Same idempotency key and same body retried | The same job comes back, with a fresh upload URL if it is still `awaiting_upload` |
| Same idempotency key and a different body | `409` |
| Double click | The button is disabled, and the idempotency key makes the retry the same job |
| Person uploads a different file on purpose | A new key, a new job |
| Refresh after login | `GET /api/session` succeeds. They stay logged in |
| Refresh during the PUT | The cookie remains. The file input is empty. The job is still `awaiting_upload`. They pick the file again. If the object is already complete, the page calls `complete` and does not PUT twice |
| Signed URL expires | The PUT is rejected by storage. `POST /api/videos/{id}/upload-url` mints another 15-minute URL while the job is `awaiting_upload` |
| PUT succeeded, browser died before `complete` | The object is in the inbox. A storage notification runs the same complete function. A sweeper does it too if the job is still `awaiting_upload` and the object checks out. Both are safe to run twice |
| Object missing, empty, or the wrong size at complete | `422`. Status stays `awaiting_upload`. Nothing is published |
| Crash after the job insert and before the URL is returned | The row exists. The client retries the same idempotency key and gets the URL |
| Crash after commit of `uploaded`, before publish | The outbox row is unpublished. The publisher loops. A sweeper publishes rows older than 30 seconds. The worker cannot see a job that was not committed |
| Publisher publishes, then crashes before `published_at` | Pub/Sub may get a duplicate. The worker treats `ready` as done |
| Worker is down | The subscription holds the messages. Jobs stay `uploaded`. When it returns, it drains them |
| Worker dies during ffmpeg | The ack never happens. The lease expires. Redelivery encodes again and replaces the incomplete prefix |
| Two deliveries overlap | The second sees a live `lock_expires_at` and waits. It does not start a second ffmpeg |
| Ack deadline is shorter than the encode | The worker extends the deadline on a timer until it finishes, then acks |
| Already `ready` | Ack. Do not encode. Do not overwrite the playlist |
| ffprobe says it is not a video | `failed`, the error is stored, the message is acked. Retrying it will not help |
| ffmpeg, disk, or storage fails transiently | The attempt count goes up, the message is nacked. After 5 deliveries it is `failed`, acked, and copied to the dead-letter subscription |
| `awaiting_upload` for 24 hours with no good object | Sweeper sets `expired`. A lifecycle rule deletes the orphan object |
| `uploaded` for 15 minutes and no live lease | Sweeper publishes again from the outbox. The worker is safe to run twice |
| Kubernetes stops the worker | `SIGTERM` stops the lease extension and nacks, then exits. `SIGKILL` skips that. The lease expiry and the missing ack are the backup |
| Database is down | Health returns `500`. The load balancer stops sending traffic |
| Description model is down | The video can already be `ready`. The description is a later write. A model failure does not change the playlist |

Login failures are stored per account so two API instances share the count. Cloud Armor is the IP limit at the edge. The API is the account limit. Neither one decides who owns a job.

Logs are JSON with `job_id`, status, and severity. They do not contain the cookie, the signed URL, the password, or the database URL.

Schema changes are Alembic revisions, run once as a release step before new API replicas serve traffic. `create_all` is for the test database only. Tests use in-memory SQLite and do not start Postgres, Pub/Sub, or storage. They call the routes and assert the status codes. The emulators are for the manual run.

---

## The services

### 1. `web`

Folder: `services/web`

A login form, then one file input, one button, a status line, and a player once playback is authorized. On load it calls `GET /api/session`. While a job is unfinished it polls every few seconds. It never calls the worker.

| Tool | Why it is here |
|---|---|
| React | The page |
| TypeScript | The job fields and the status union |
| Vite | The page on port `5173`. `/api` is proxied to port `8000`, so the cookie stays on one origin |

### 2. `api`

Folder: `services/api`

| Tool | Why it is here |
|---|---|
| Python | This service, the publisher loop, and the worker |
| FastAPI | Routes and status codes |
| Pydantic | Signup, login, and the upload JSON. The video bytes are not a JSON body |
| Uvicorn | HTTP. The worker has no routes |
| SQLAlchemy | Users, sessions, jobs, outbox |
| Alembic | Schema changes |
| Postgres | The database the app runs against |
| pytest | Session, authz, validation, idempotency, complete-without-object |
| Firebase Auth | Optional issuer of the one-time ID token on GCP. The session row is still ours |

### 3. `worker`

Folder: `services/worker`

A pull loop. No public URL. It extends the ack deadline while ffmpeg runs, and it acks only after `ready` or a permanent `failed`.

| Tool | Why it is here |
|---|---|
| Python | Same language as `api` |
| ffprobe | Rejects a non-video before ffmpeg |
| ffmpeg | HLS at 480p and 720p |
| Pub/Sub | Pull, modify the ack deadline, ack, dead letter |
| SQLAlchemy | Short transactions around the lease and the final status. The connection is not held during ffmpeg |

### 4. What runs where

| Piece | Laptop | GCP |
|---|---|---|
| Storage | Storage emulator that can sign PUT URLs | Inbox bucket and output bucket |
| Database | Postgres in Compose | Cloud SQL, private IP |
| Queue | Pub/Sub emulator | Pub/Sub, with a dead-letter topic |
| Playback bytes | API media route, session checked per request | Cloud CDN, signed cookie for that job prefix |
| Secrets | Env file that is not committed | Secret Manager |
| Page and API | nginx or the Vite proxy, one host | One host name. TLS on the load balancer |

The database password, the storage signer, and the CDN signing key come from the environment. They are not in source and not in a Vite variable.

Redis is not on this path. A list pop removes the message before the work is finished, so a crash loses it. Pub/Sub keeps it until ack.

---

## What the posting asked for, and where it sits

| They listed | Where you show it |
|---|---|
| TypeScript / JavaScript | `services/web` |
| Python | `services/api` and `services/worker` |
| React | The upload page |
| Cloud Run | The `api`. Each request is short: check the session, sign a URL or commit a row, return. The video bytes go to storage |
| GKE | A pull worker that must stay up. A Cloud Run Job that encodes one video and exits is the other honest worker |
| Cloud SQL | Users, sessions, jobs, outbox |
| Firebase | The login issuer on GCP. The API verifies one ID token and then uses its own session cookie |
| GitHub Actions | pytest, then images, then deploy. A failed test stops the pipeline |
| Cloud Build | The same stages on GCP |
| Jenkins | The same stages in a Jenkinsfile |
| AI / ML on Google Cloud | After `ready`, the worker may write a description. The upload and the playlist do not wait for it |
| Design system | One button, one file input, one status pill |
| Cost | Two renditions. Workers scale with the backlog. The API scales to zero when nobody is calling it |

---

## Phase 1 — Name the flow

- The browser uploads to storage with a URL that dies in 15 minutes. The API does not receive the bytes.
- The session is a cookie. A refresh stays logged in. Logout revokes the row.
- `uploaded` and the outbox row commit together. Publish happens after that, and it retries.
- The worker acks only when the job is `ready` or permanently `failed`.
- A second delivery does nothing once the job is `ready`.
- Cloud Run fits `api`. The worker is a GKE pull loop or a Cloud Run Job.

### Move on when

You can redraw the boxes: browser, `web`, `api`, storage, Postgres, outbox, Pub/Sub, `worker`.

---

## Phase 2 — `api`

Folder: `services/api`

Postgres is the database. Tests use SQLite in memory. The storage emulator signs the PUT URL.

- [ ] FastAPI, Uvicorn, SQLAlchemy, Alembic, pytest
- [ ] `GET /api/health` with no session
- [ ] Signup, login, logout, `GET /api/session`. Passwords are hashed. The cookie is `HttpOnly`, `Secure`, `SameSite=Lax`
- [ ] A second request with the cookie returns the user. A request after logout is `401`
- [ ] `POST /api/videos` without a session is `401`. With a session it stores `awaiting_upload` and returns a signed URL. It does not read the file bytes
- [ ] Size 0 or a disallowed type is `422` and leaves no job
- [ ] The same idempotency key returns the same job. A different body with that key is `409`
- [ ] `complete` against a missing or empty object is `422` and writes no outbox row
- [ ] `complete` against a real object commits `uploaded` and one outbox row. A second complete does not write a second row
- [ ] List and get return only the caller's jobs. Someone else's id is `404`
- [ ] pytest covers those cases through `TestClient`, including the cookie on the second request

### Move on when

`pytest` passes, and a signed URL from curl accepts a short mp4 in the emulator.

---

## Phase 3 — Publish and `worker`

- [ ] Pub/Sub emulator. The publisher sends unpublished outbox rows and sets `published_at`
- [ ] `worker` has no port. It pulls, sets a lease, runs ffprobe and ffmpeg, uploads `master.m3u8` last, sets `ready`, then acks
- [ ] A non-video becomes `failed` and is acked
- [ ] Kill the worker during ffmpeg. Start it again. The job reaches `ready` without calling a worker URL
- [ ] A second delivery after `ready` does not change the playlist
- [ ] Five failed deliveries end on the dead-letter subscription with `failed` on the row

### Move on when

You can point at the publisher and the worker and say which one owns the message, and you can open the playlist.

---

## Phase 4 — `web`

- [ ] Vite, React, TypeScript
- [ ] On load, `GET /api/session`. Login only when that returns `401`
- [ ] File input. Button disabled from the create call through `complete`
- [ ] PUT to `upload_url`, then `complete`
- [ ] Poll until `ready`, `failed`, or `expired`
- [ ] `ready` calls playback, then shows the player. `failed` and `expired` show `error`
- [ ] Refresh once logged in. The session is still there. Refresh during a PUT leaves `awaiting_upload` and an empty file input
- [ ] The page never calls `worker`

### Move on when

A video chosen in the browser plays after the worker finishes, and a refresh does not send you back to login.

---

## Phase 5 — One command starts it

Folder: `deploy/compose`

- [ ] Images for `web`, `api`, and `worker`
- [ ] Compose starts Postgres, the Pub/Sub emulator, and the storage emulator
- [ ] `api` and `worker` wait until Postgres accepts connections
- [ ] Alembic runs once before the API serves
- [ ] Upload still works, and the chunks are still there after a restart

---

## Phase 6 — The platform, after a video plays

Kubernetes, when you go there: namespace `transcode`, a Deployment and Service for `api` and `web`, a Deployment and no public Service for `worker`, a ConfigMap for hosts and bucket names, a Secret for the database password and the signing keys.

A scheduler every minute expires abandoned uploads, completes a job whose object is already valid, publishes outbox rows older than 30 seconds, and republishes a job stuck in `uploaded` with an expired lease.

Pipeline order: pytest, then build images, then deploy. GitHub Actions is the file you can show. Cloud Build and Jenkins are the same order.

### Say these in the meeting

- What happens from the button until the player starts
- Where the session lives, and why a refresh stays logged in
- Why the video bytes never enter the API
- What is in the queue if the worker is down
- Why a second copy of the same event is safe
- Why ffmpeg is not inside an HTTP request
- What replaces the emulators on GCP: Cloud Storage, Pub/Sub, Cloud SQL
- Why the API fits Cloud Run, and why the worker is a Cloud Run Job or a GKE pull worker
- Where the database password lives, and why the worker never sees the cookie
- Why a missing session is `401` and another user's job is `404`
- What you would check in the docs before changing a customer project

This folder is the project you are building for the conversation. If a previous job really ran a transcode pipeline, describe that system with its real format, volume, and failure. Do not tell them this folder was that company's production system.
