# Release Notes — TokenLean

Newest date first. All changes that shipped on the same day are grouped under **one**
`## YYYY-MM-DD` header. Enterprise-only items are labelled **[Enterprise]** and link to
<https://tokenlean.cbeyond.cloud/>.

<!--
Format (newest date at the TOP; ONE date header per day):

## YYYY-MM-DD
### <one-line summary> — <Type>
<what changed and why — keep to ~5-7 lines where possible; don't force-fit a genuinely
large change. For Enterprise items, state it explicitly and link the URL below.>
- **OSS:** <what ships in every tier>            (omit both bullets for a pure bug fix)
- **[Enterprise]:** <the managed depth> — <https://tokenlean.cbeyond.cloud/>

Type = Bug fix | Bug fix [Enterprise] | Enhancement (OSS) | Enhancement (OSS + Enterprise) |
Enhancement [Enterprise].  Use the [Enterprise] bug-fix type when the fix lands entirely in the
managed product — self-hosters have nothing to upgrade, and the entry should say so.
Add a new `###` item under today's date header; only start a new `## YYYY-MM-DD` when the
date changes.
-->

## 2026-10-07

### The key-sync job can read its keys file on any host — Bug fix

The key-sync image copies in the operator's keys file, keeping its file mode, and runs as an
unprivileged user. Because the copy belonged to root, a keys file only its owner may read
(mode 600, common on a Linux host or in Cloud Shell) would have left the job unable to open
it. The copy now belongs to the user that reads it.

## 2026-10-06

### Any command run in the proxy image now finds its metrics directory — Bug fix

The proxy images put every process into multiprocess metrics (`PROMETHEUS_MULTIPROC_DIR`), but
only the server's start command created that directory. Anything started with its own command,
such as a `docker exec` or the commercial deploy's schema job, failed at its first metric, so
the next commercial deploy on the runtime database role would have stopped at that job. The
image now creates the directory, as the user it runs as; the server still clears it at start.

### The proxy and the Java client template take four dependency security fixes — Bug fix

The proxy now pins LiteLLM 1.95.1, the fix for an advisory about request-body routing
parameters (GHSA-3cv6-jpf6-8222), and `src/proxy/requirements.in` makes 1.95.1 the floor.
urllib3 moves to 2.8.0 for three advisories (an infinite loop in chunked deflate streaming, an
unbounded chunk-size line, HTTPS-proxy TLS settings that could be ignored), multidict to 6.9.1
for a reference leak in its items views (GHSA-54p9-h82j-f925), and the Java client template's
jackson-databind to 2.18.11 for five. Nothing else in the lockfile moves. A new test fails when
a lockfile pins a version its `requirements.in` rules out.

### A key pasted into an agent's `api_key_env` no longer reaches the log — Bug fix

`api_key_env` names a server environment variable. When a tenant's agent names one the
operator's config does not define, the proxy dispatches without a key and logs a warning that
names the variable. If the tenant had pasted the key itself into that field, the warning logged
its first 128 characters. It now names the value only when it looks like a variable name
(capitals and digits joined by underscores, such as `LLM_KEY_OPENAI`); anything else, a whole
`.env` line included, shows as withheld.

## 2026-10-05

### The proxy image no longer installs four unused packages — Enhancement (OSS)

`langgraph`, `zep-python`, `pgvector` (the Python client; the Postgres extension stays) and
`google-cloud-tasks` were declared in `src/proxy/requirements.in` but imported nowhere, and
`zep-python` 2.0.2 ships with no license. They are gone, and the recompiled lockfile drops 14
more packages only they pulled in (the langgraph/langchain family, langsmith, orjson, ormsgpack,
zstandard and others): 171 pins become 153, with no version changes. The other lockfiles keep
their pins. The license audit now passes with no pending removals.
- **OSS:** a smaller proxy image; nothing it runs is affected.

### The proxy no longer asks for a Redis extra that does not exist — Bug fix

`src/proxy/requirements.in` asked for `redis[asyncio]`, but redis-py has no `asyncio` extra: its
async client, `redis.asyncio`, is part of the package. Every lockfile compile and image build
warned about it. The line is now plain `redis>=5.0.0`; the pin (8.1.0) and what the image
installs are unchanged.

## 2026-10-04

### Every dependency checked against one license rule, in CI — Enhancement (OSS)

TokenLean now has one written license rule: everything it uses must be free to use and to host,
so permissively licensed (MIT, Apache-2.0, BSD, ISC, PSF, PostgreSQL, BSL-1.0, CNRI-Python, Zlib,
CC0) or MPL-2.0 for an unmodified transitive dependency, with Grafana (AGPL-3.0, run unmodified)
the one exception. `scripts/audit_licenses.py` now enforces it on every CI run: each pin of all six
lockfiles, judged by that release's own license metadata (it used to fail only on GPL/SSPL, and
read the latest release). `THIRD_PARTY_LICENSES.md` gains the rule and a Models table;
`docs/oss-licenses.md` lists every `requirements.in` dependency, and a test keeps it in step.
`pgvector` and `google-cloud-tasks`, imported nowhere, are marked for the next recompile, and
Terraform no longer enables the unused Cloud Tasks API.
- **OSS:** the rule, the CI audit, and both license documents.

### Redis pinned to 7.2, the last BSD-licensed release — Bug fix

`redis:7-alpine` had moved to Redis 7.4, which is licensed RSALv2/SSPLv1 rather than BSD. Docker
Compose and the GCP Redis VM now run `redis:7.2-alpine` (BSD-3-Clause, still patched); nothing in
TokenLean needs a newer Redis (managed GCP already runs Memorystore 7.0). On GCP, reset the Redis
VM once after `terraform apply` so it starts the 7.2 image; Redis then starts empty (cache,
sessions and rate-limit counters).

### G06 RouteLLM defaults to the licensed `bert` router — Bug fix

With `classifier: routellm`, G06 defaulted to the `mf` router, whose published checkpoint carries
no license, and without an OpenAI key it fell back to `causal_llm`, a 16 GB Meta Llama 3
derivative that cannot load in the 2 GB sidecar. The default is now `bert` (Apache-2.0
checkpoint, no OpenAI key needed) at its own calibrated threshold, 0.4066: about half the requests
go to the strong model, as with `mf`'s 0.11593. `mf` and `sw_ranking` still run when configured
with an OpenAI key; without one G06 logs the switch and uses `bert`. An unset threshold now takes
the configured router's own calibration.

### Scripts that run git name it when it is missing — Bug fix

The open-core path check (`scripts/ci/commercial_paths.py`) and the PR template token budget
(`scripts/ci/pr-diff-token-check.py`) ran git by bare name, so a machine without it got a bare
"No such file or directory". They now look git up on your PATH, run it by the path found, and
say "git is not on PATH" when it is not there. Nothing else changes.

### G10's Zep memory backend is removed — Bug fix

G10 could also keep conversation memory in Zep, but that client was written against a
zep-python API it never matched, so it never worked. It is removed rather than rewritten
without a server to test against. A config that still sets `zep_enabled` gets one warning that
nothing reads it. G10's memory is the session window with summaries, plus Mem0
(`mem0_enabled`) for long-term memory. The `zep-python` package leaves the image at the next
dependency refresh.

## 2026-10-03

### The managed proxy and portal images use pinned bases and a pinned torch — Bug fix [Enterprise]

The managed proxy image now runs as an unprivileged user like the open-source one, its base
image is pinned by digest, and it installs the same pinned CPU build of torch (2.14.0) instead of
whatever was newest at build time. The portal image's two bases are pinned by digest too —
<https://tokenlean.cbeyond.cloud/>

### The images run as an unprivileged user, on pinned bases — Bug fix

Every image ran as root, so code that broke into one ran as root inside it; their base images
were floating tags, so a rebuild could take a different OS or Python patch level; and CI
tested Python 3.12 while the images run 3.11. The proxy, both sidecars and the pipelines now
run as uid 1000 (the Tika sidecar as its own user, the migration job as `postgres`), every
base is pinned by digest, and CI runs 3.11. Dependabot now proposes base-image digest bumps
(OS patches), never a new major or minor tag. On a Linux host whose user is not uid 1000, keep
`config/` readable by others (the default `644` is).

### Each response names its trace in an X-Trace-ID header — Bug fix

`tracing.propagate_trace_id_header` (on in the template) was documented but read by nothing,
so no response said which trace it was. While the proxy exports OpenTelemetry spans, every
response to a model request now carries `X-Trace-ID`, the request's trace id (32 hex
characters), on streamed answers and refusals too, so a slow or failed call can be found in
Jaeger or your collector. Set `propagate_trace_id_header: false` to leave it out.

### The portal and admin console say when a change was not fully applied — Bug fix [Enterprise]

When a portal user was deleted or had their password reset and their live sessions could not
be revoked, those sessions stayed valid until they expired, with no sign of it. The same held
when a portal save could not refresh the tenant's cached settings (the old ones applied until
the cache expired), when the login rate limiter could not count a failed attempt, and when a
failed signup could not give back its company code. Each now logs a warning —
<https://tokenlean.cbeyond.cloud/>

### Errors the proxy used to ignore are now logged — Bug fix

Dozens of places in the proxy and its document and fine-tune pipelines caught an error and
carried on without a word: a metric that did not record, a stream chunk or queue message that
could not be read, a setting that could not be read and fell back to its default, a provider
outcome the circuit breaker never saw. Each now logs the error at DEBUG, so debug logging shows
what was skipped and why. Nothing else changes.

### A revoked or suspended key stops working on every proxy instance within seconds — Bug fix

With the default key stores (a local key file or Secret Manager), each proxy instance trusted
its key cache for up to five minutes, so a key revoked or suspended through another instance,
or with `scripts/issue-key.sh`, kept working there that long. Each instance now checks whether
the store changed, the key file every second and Secret Manager's latest version every 5
seconds, and reloads at once when it did. Run `terraform apply` before deploying: the proxy
needs to read the key secret's version names (`roles/secretmanager.viewer` on that one secret).
Until then it logs a warning and a revoke still waits up to five minutes.
`KEY_FILE_CHECK_SECONDS` and `KEY_SECRET_CHECK_SECONDS` set the intervals.

### The proxy image no longer carries 1.8 GB of unused GPU libraries — Bug fix

The proxy installs PyTorch's CPU build, but its pinned requirements also pinned the CUDA runtime
that recent torch releases depend on (`cuda-toolkit`, `cuda-bindings`, `cuda-pathfinder`), so
every proxy image carried about 1.8 GB of GPU libraries it never loads: slower builds and
pushes, more registry storage per revision, and more packages for image scanners to flag.
Those pins are gone, `scripts/compile-requirements.sh` now drops them along with `nvidia-*`, and
the proxy's torch is pinned to 2.14.0. The next image build is about 1.8 GB smaller.

### The LLMLingua, RouteLLM, document and fine-tune images install exact dependency versions — Bug fix

These four images installed open version ranges, so each rebuild took whatever was newest that
day and no past image could be rebuilt. Each now installs a fully pinned `requirements.txt`,
compiled from its `requirements.in` by `scripts/compile-requirements.sh` as the proxy's is. The
LLMLingua and RouteLLM images pin the CPU build of torch they install (2.14.0), the version
their pins are resolved against. Refresh the pins with that script.

### The local Docker stack requires its own Redis, Qdrant and admin passwords — Bug fix

The local `docker compose` stack ran Redis without a password and Qdrant without an API key,
created Langfuse's admin with a fixed default password, left Grafana's admin password empty,
and accepted cross-origin browser calls to the proxy. Redis, Qdrant and the Langfuse and
Grafana admins now each require a credential from `.env` (`REDIS_PASSWORD`, `QDRANT_API_KEY`,
`LANGFUSE_INIT_USER_PASSWORD`, `GRAFANA_PASSWORD`). `docker compose` refuses to start without
them, and `scripts/local/deploy-local.sh` and `start-local.sh` generate any that `.env` lacks.
Cross-origin calls are off unless `CORS_ORIGINS` (or `CORS_ALLOW_ALL=true`) is set. On an
existing stack, Langfuse and Grafana keep the admin passwords they already have: change them in
their UIs if they are still the old defaults.

### The docs assistant keeps its documents across a Qdrant restart — Bug fix [Enterprise]

On GCP, a new Qdrant revision or a restart restores each tenant's documents from their
snapshots, but the docs assistant's collection had none: the managed deploy seeded it without
writing one, so after a restart the assistant answered "not found" until the next deploy. The
seed job now writes the collection's snapshot to the same bucket after every sync, and Qdrant
restores it with the others. A deploy on a Terraform state from before the bucket warns and
seeds without one. Self-hosters have nothing to upgrade —
<https://tokenlean.cbeyond.cloud/>

### Ingested documents survive a restart of Qdrant on GCP — Bug fix

On GCP, Qdrant keeps its collections in its Cloud Run container, so a new revision of the
Qdrant service or a restart of its instance lost every tenant's ingested documents, and RAG
answers and fine-tuning silently found nothing. Each ingest now writes the changed
collection's snapshot to a private bucket (`<project>-qdrant-snapshots`), and Qdrant restores
the newest snapshot of each collection before it serves again. Only an ingest that is running
at the moment of the restart has to run again. Deleting a tenant's data deletes its snapshots
too, and the bucket keeps no soft-deleted copies, so erased documents cannot come back.

## 2026-10-02

### Redis requires its password and encrypts its traffic by default — Bug fix

On GCP, Redis accepted connections without a password by default and carried every tenant's
cached prompts and answers, sessions and counters, and the password itself, in clear across
the VPC. Both Redis backends now require the password and serve only TLS by default: the
proxy and the fine-tune job connect with a `rediss://` URL and trust only the deployment's
own CA (`REDIS_CA_CERT`). Memorystore uses its built-in TLS; for the Redis VM, Terraform
creates a CA and a certificate, and the VM reads its key at boot from Secret Manager. When the
VM is not yet running the configured settings, the deploy restarts it just before updating the
proxy. Switching TLS on Memorystore recreates the instance. Either way Redis starts empty
once. `REDIS_AUTH_ENFORCE=false` and `REDIS_TLS=false` in `.env.gcp` opt out.

### A GDPR erase also deletes the tenant's Langfuse traces — Bug fix [Enterprise]

An erase or offboard left the tenant's request traces in Langfuse, with the user's identifier
and, where content capture was on, the prompts and responses; the erase summary listed them as
retained. The version of Langfuse the stack runs has no way to delete traces through its API,
so the erase now deletes the tenant's traces, and everything attached to them, in Langfuse's
own database, including traces written before this change. On the managed service the proxy
does this through a database role allowed only to read and delete those records. If the
traces cannot be deleted, the erase reports that step as failed and can be retried —
<https://tokenlean.cbeyond.cloud/>

### The managed service protects the audit log at the database level by default — Bug fix [Enterprise]

The restricted database role that can only read and insert audit rows was built but not
used by default, so the managed proxy still connected as the tables' owner and could rewrite
or delete the audit trail. Every managed deploy now moves the proxy onto that role, after
the deploy's schema step has created the tables, the role and the semantic cache's vector
index; if that step fails, the deploy stops before the proxy moves. Setting
`DB_RUNTIME_ROLE=false` keeps the old behaviour, and the evidence pack then reports the audit
log as not protected —
<https://tokenlean.cbeyond.cloud/>

### Each tenant's batched requests queue on their own stream — Bug fix

G13 batching queued every tenant's deferred requests on one Redis stream per topic, so one
tenant that filled `max_backlog` made every other tenant's batchable requests skip the batch
discount, and a large backlog from one tenant delayed everyone's results. Each tenant now has
its own stream per topic, `max_backlog` applies per tenant, and one consumer reads them all.
The default tenant keeps the old stream name, so requests already queued before an upgrade
are still answered.

### The semantic cache looks up through a vector index — Bug fix

Every G05 L2 (semantic cache) lookup scanned all of the tenant's stored questions, on the
request path, because nothing indexed the embeddings and the query was not written in a
shape an index could serve. The proxy now builds an HNSW index on the embeddings in the
background the first time the cache is used (without blocking writes), and the lookup takes
the nearest stored question through it, serving it only within `l2_similarity_threshold`.
With pgvector 0.8 or later the index search also keeps going past other tenants' rows, so a
tenant with few cached answers among many still finds its own; on an older pgvector, run
`ALTER EXTENSION vector UPDATE`.

### The Tika sidecar moves to Apache Tika 3.3.1, past the PDF-parser XXE — Bug fix

The document-parsing sidecar was built on Apache Tika 2.9.1, inside the range of
CVE-2025-54988 and CVE-2025-66516, an XML external entity flaw in Tika's PDF parsing (1.13 to
3.2.1). The ingestion pipeline sends uploaded documents that its main parser cannot read to
this sidecar, so a crafted upload could reach the flaw. The sidecar is now built on Apache
Tika 3.3.1, and a test refuses any base image older than 3.2.2. The pipeline's call is
unchanged, and a local check read text, PDF, Word and Excel files through it. Rebuild and
redeploy the Tika sidecar to pick it up.

### Pipeline traces: one trace per request, and the tracing settings apply — Bug fix

The proxy's OpenTelemetry pipeline tracing ignored the `tracing` settings and started each
middleware stage as a trace of its own, so a request's stages could not be seen together
and the trace id stored with each usage event led to a trace with none of them. It also
exported to `jaeger:4317` by default even where no collector runs, and never used TLS. Now
each stage is a child of the request's pipeline span. `tracing.enabled`, `otlp_endpoint`,
`sample_rate` and `service_name` are read at startup (`OTEL_EXPORTER_OTLP_ENDPOINT` still
wins for the endpoint). With no `tracing.enabled` set, spans are exported only when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set. TLS is used unless the endpoint is `http://`.

### A GDPR purge or trial change that could not refresh a cache now says so — Bug fix [Enterprise]

After a GDPR purge, if the in-process key cache could not be cleared, a purged key could
still serve from memory until the cache expired, and the purge reported nothing. After a
trial change, if the cached tenant config could not be refreshed, requests kept the old
limits for a while, also without a word. Both now log a warning —
<https://tokenlean.cbeyond.cloud/>

### A billed call the proxy could not record, or a skipped retention pass, is now logged — Bug fix

Two failures were swallowed without a word. When a provider call's usage could not be
recorded, the request was still served, but its cost silently left that call out. When the
retention job could not read its config, that pass deleted nothing, so data outlived its
retention period with no sign of why. Both now log a warning that says what was lost.

### Evidence packs count retrieved-context and tool-call enforcement — Bug fix [Enterprise]

The trust & safety section of the signed audit evidence pack counted only guardrail (G30)
and PII (G29) events. Blocks, strips and flags of poisoned retrieved documents (G31), its
record-only managed-rule matches, and tool calls the policy flagged or denied or the proxy
refused to execute (G32) were all left out, so the record handed to an auditor understated
what was enforced. Each now has its own count, by attack category, rule id or tool name, and
the pack's attested controls describe G31 and G32 too —
<https://tokenlean.cbeyond.cloud/>

### G8 now finds the registry tools the model has stopped calling — Enhancement (OSS)

G8's pruning of unused tools never ran, and as written it could not have run safely: it
treated a tool with no usage record as unused, nothing ever lifted a pruned mark, and it
counted the tools a request offered rather than the tools the model called. It now runs
once a day (`pruning.schedule`, UTC), for each tenant and against that tenant's own tool
registry. It looks for registry tools that every request carried for the whole
`inactivity_threshold_days` window and that the model never called. A tool with no history
is never one. By default (`dry_run_first: true`) it only reports them, in a log line and the
`token_opt_tool_pruning_candidates` gauge. Set `dry_run_first: false` and it drops them from
that tenant's requests. A dropped tool returns after the same window, or at once when a
request names it. The model's calls are recorded from streamed and non-streamed answers
alike, and only for tools from your registry.

### Mem0 long-term memory works, and keeps each user's memory to that user — Bug fix

G10's Mem0 long-term memory could not run: it imported a class the pinned mem0ai 2.x does not
have, so its client never loaded, and its calls did not match the library's API. Had it
loaded, it would have keyed memory on a user id taken from the request body, which any
caller can set, and on `anonymous` when there was none. It now uses mem0ai's
`AsyncMemoryClient` and keys memory on the authenticated user within the tenant: a legacy
key's user, or an `X-User-ID` the tenant's allowlist accepted. A tenant key with no
accepted `X-User-ID` gets no long-term memory, and the first such request logs a warning,
because one memory for the whole tenant would mix what its users said. A request waits at
most 2 seconds for its memories; the exchange is stored in the background; and the client is
built off the request path, retried after a failed start. The mem0ai library's own usage
analytics are off unless you set `MEM0_TELEMETRY=true`. A read-only home directory no
longer stops the proxy from loading when mem0ai is installed. A missing `MEM0_API_KEY` is
now reported at the first request, as a missing `MEM0_API_URL` already was.

### Ship newer managed guardrail rules without a release, signed — Enhancement [Enterprise]

The managed injection ruleset that Enterprise adds to G30 (and to G31, record-only by default)
was fixed at build time: the setting meant to fetch newer rules did nothing. The proxy now loads a newer
ruleset bundle from a `gs://` or `https://` location you choose, and only one that carries a
valid Ed25519 signature from your own key. A bundle that is unsigned, tampered with, older than
the rules in force, or that holds any invalid rule (including a pattern that would backtrack
badly) is refused whole, and the rules in force stay. A rule a newer bundle leaves out is
removed, so a bad rule can be withdrawn; your own extra rules are kept. The fetch runs off the
request path, and the refusal log never shows a token carried in the bundle's URL. A command-line
tool makes the signing key, signs a bundle after checking it exactly as the proxy will, and
verifies a signed pair.
- **[Enterprise]:** signed managed-ruleset updates for G30 and G31 — <https://tokenlean.cbeyond.cloud/>

### Managed guardrail rules reach retrieved content, record-only until you enforce them — Enhancement (OSS + Enterprise)

G31 scans the documents and memories that retrieval adds to a prompt for indirect prompt
injection. It can now run an extra set of managed rules in record-only mode: a match is counted
and audited, and the request is left alone, so you can see the rules' false positives before
they change anything.
- **OSS:** G31 settings `managed_rules: record | enforce` (operator) and `managed_rules_enforce`
  (a tenant's own opt-in, which can only add enforcement); a new metric,
  `token_opt_context_trust_managed_recorded_total`; a `context_trust.managed_recorded` audit
  row; and a G31 row on the Trust & Safety dashboard, which had none.
- **[Enterprise]:** the managed guardrail ruleset now feeds G31 as well as G30, and tenants can
  turn enforcement on for themselves in the portal's G31 settings —
  <https://tokenlean.cbeyond.cloud/>

### The admin console guide shows how to restrict the admin key to your network — Bug fix [Enterprise]

The admin API accepts the admin key on its own, so a leaked key could be used from anywhere.
The admin console guide now shows how to limit the admin key to your operators' and scripts'
addresses with the existing per-tenant IP allowlist, how to lift that limit, and that on GCP it
holds only once the client-IP check is switched to enforce —
<https://tokenlean.cbeyond.cloud/>

### Managed deploys use Qdrant for RAG by default — Bug fix [Enterprise]

The managed deploy scripts defaulted to pgvector for document retrieval, but the
document-ingestion job writes to Qdrant only and nothing fills the pgvector tables. On a
default deploy, tenants' uploads were never searchable and both RAG and the docs assistant
returned nothing. Both the GCP and the local managed deploy now default to Qdrant, and choosing
`--vector pgvector` prints a warning that RAG will find nothing. Qdrant on Cloud Run runs one
always-on instance, which adds to the hosting cost, and its data is still lost when the Qdrant
service gets a new revision (durable storage is a separate open item) —
<https://tokenlean.cbeyond.cloud/>

### Ingestion reads Excel and PowerPoint through Tika and refuses unreadable files — Bug fix

The document-ingestion job read files with Unstructured, whose Excel and PowerPoint parsers are
not in the job image. When Unstructured could not read a file, the job decoded the file's raw
bytes as text and stored that as RAG context, so such an upload filled the tenant's knowledge
base with noise. The job now reads every file with Unstructured first, so PDF and Word results
are unchanged. It then asks the Tika sidecar, which the GCP deploy now connects to the job, for
what Unstructured cannot read. A file neither can read is refused and nothing is stored, unless
it is a plain-text file that really is UTF-8.

### Oversized document chunks are cut, not summarised — Bug fix

The document-ingestion job was meant to condense any chunk over `MAX_CHUNK_TOKENS` (4,000 by
default) with a cheap model, but the libraries it needed were never installed, so such chunks
were stored whole. Had it run, it would have sent tenants' document text to a model on the
platform's own key and stored a lossy summary instead of the text. The job now cuts an
oversized chunk into pieces within the limit, preferring a line break or a space near it. No
model is called and nothing leaves the job. Such chunks are rare: only token-dense text (some
non-Latin scripts) or a raised `CHUNK_SIZE_TOKENS` produces them.

### The GCP deploy summary no longer prints the Grafana admin password — Bug fix

At the end of a GCP deploy, the summary read the Grafana admin password from Secret Manager and
printed it, so it ended up in terminal scrollback and in any CI log of the deploy. The summary
now shows the command that fetches the password instead.

### The guardrail ruleset feed is described as it works — Bug fix [Enterprise]

The managed guardrail ruleset was described as a feed kept current against new jailbreak
techniques and as covering context-trust scanning (G31). In this release it is a bundled set of
extra rules added to the prompt guardrail (G30) only; it does not fetch newer rules, and G31 does
not receive them. The code comments and the configuration reference now say so. Making the feed
update itself, and applying it to G31, are planned separately —
<https://tokenlean.cbeyond.cloud/>

### Background work in the managed service is kept until it finishes — Bug fix [Enterprise]

Several pieces of background work in the managed service were started without the service
keeping hold of them: the message that tells other instances a tenant's provider key changed,
the audit row recorded when a stored provider key is used, and three long-running loops (the
key-change listener, the guardrail ruleset feed and the learning loop). Python keeps only a weak
reference to such work, so in rare cases it could be discarded before it finished. The service
now keeps each one until it completes, and a test fails if new code starts work this way —
<https://tokenlean.cbeyond.cloud/>

### Removed an unused pgvector retrieval helper — Bug fix

The proxy carried a second pgvector retrieval helper that nothing called. It built its SQL by
inserting a table name into the query text without checking it, and it kept every tenant's
documents in one shared table, so wiring it in later would have been unsafe. It is deleted.
Retrieval is unchanged: G07's own pgvector search, which validates the collection name, is
the one that runs. A test now also fails if any middleware module is left that nothing
imports.

## 2026-10-01

### The ingestion docs no longer promise chunk summarisation — Bug fix

The document-ingestion job is meant to condense any chunk longer than 4,000 tokens with a cheap
model before storing it. The job image does not include the libraries this needs, so such a
chunk is stored at full size and a warning is logged. This is rare, because the splitter aims
for chunks of about 400 tokens. The architecture notes said every oversized chunk was condensed;
they now describe what happens.

### The docs no longer say G8 prunes unused tools on a schedule — Bug fix

G8 includes a job that would remove tools no request has used for 30 days, with
`G8_tools.pruning` settings for its schedule. Nothing runs that job, so the settings have no
effect. The configuration reference said so, but the README, the architecture notes and the
settings template still described scheduled pruning as working. They now say it is not run in
this release.

### Unused Tika settings removed; the docs say tika-svc is not called yet — Bug fix

The GCP deploy runs a Tika document-extraction service, but nothing calls it: the
document-ingestion job uses Tika only when it runs with `USE_TIKA=true`, which the deploy does
not set. The proxy also carried a Tika client that nothing used. The template's
`G3_doc_pipeline.tika_sidecar` settings and a deploy step that patched a Tika URL into the
configuration had no effect either. The client, the settings and the deploy step are removed,
and the deployment guides now say that `tika-svc` is deployed but not called. Whether to have
the ingestion job use it or stop deploying it is still open.

### Removed an unused Tika wrapper from the Tika sidecar — Bug fix

The Tika sidecar directory held a Python wrapper service that was never built into the image:
the image runs the standard Apache Tika server directly. The wrapper also expected a different
upload format from the one the document pipeline sends, so it could only mislead anyone reading
or changing it. It is deleted, and a test now fails if a sidecar directory holds Python code
that its Dockerfile does not build.

### Mem0 memory says when it cannot run — Bug fix

Turning on `mem0_enabled` with a Mem0 URL appeared to work but did nothing: the integration
expects a client class that the bundled mem0ai release does not provide, so the client was
never created and no memory was stored or recalled, without any message. The proxy now logs a
warning on the first request whenever Mem0 or Zep memory is switched on but its client library
could not be loaded, and the configuration reference says Mem0 does not work in this release.
The README and deployment guides no longer describe Mem0 as how G10 works: G10 keeps a sliding
window with a per-session summary and adds relevant agent skills.

### Memory lookup settings take effect per tenant — Bug fix

Two G10 memory settings were ignored. `memory_query_max_chars`, which the portal offers as a
per-tenant setting, had no effect: lookups always used the `MEMORY_QUERY_MAX_CHARS` environment
variable, and one lookup path used a fixed 400 characters. `skills_similarity_threshold` was read
by only one of the two skill lookups; the other used the `SKILLS_SIMILARITY_THRESHOLD` environment
variable. Both settings now apply to every lookup, and the environment variables are the defaults
when a setting is not given.

The documentation also described `skills_qdrant_enabled: false` as a non-Qdrant fallback. Both
choices search the tenant's skills collection in Qdrant: `false` uses the retrieval stage's hybrid
search and reranker instead of a plain vector search. The description is corrected.

### Cache TTL adjustment follows recent traffic and honours its settings — Bug fix

The cache adapts how long it keeps new entries to each tenant's hit rate: above 80% they are
kept 25% longer, below 20% 25% shorter. Three things were wrong with it. The hit rate was
counted over the tenant's whole history, so early traffic decided the TTLs for good; it is now
counted over the last one to two hours. Setting `auto_ttl_enabled: false` had no effect, and
now it keeps the configured TTLs. The `auto_ttl_min_multiplier` and `auto_ttl_max_multiplier`
settings were not read either; they now bound the adapted TTL. Each lookup also made four Redis
reads whose results were thrown away; those reads are gone.

### Removed unwired modules, including a memory adapter that mixed tenants — Bug fix

Several modules were present but reachable from nowhere. A Mem0 memory adapter looked a
user's memories up by bare user ID in one collection shared by every tenant, so two tenants
with a user of the same name would have read each other's memories had it been switched on.
An agent runtime for G16 priced runs from a hardcoded table and estimated cost at a flat
rate per token. A TOON legend module for G13 was never called (G13's own TOON step, which
rewrites arrays of uniform objects as a compact table, is unchanged), and neither were two
skill-writing functions in G10. All are deleted.

G18 also carried branches that billed each request through a usage meter and wrote an audit
row per request. The pipeline never gave G18 a meter or an audit logger, so neither branch
ran: billing is the usage row written for each request outcome, and the audit log records
configuration changes and security events. The branches are removed, and so the `enabled`
switches in the billing and audit settings files are no longer read; their comments now say
so. A new test fails if a middleware module is added that nothing imports.

### Removed three unwired modules that mixed tenants' data — Bug fix

Three pieces of code were present but reachable from nowhere: a database answer cache for
G04 with no tenant column, so one tenant's stored answer would have served another tenant's
question; an MCP manifest loader for G8 whose cache key was not scoped to a tenant; and an
older G03 middleware class, with an "out-of-distribution" fallback that searched a collection
shared by every tenant. None was in the request pipeline, and nothing else used them. Had
any been switched on, it would have leaked data between tenants, so all three are deleted
along with their tests.

The configuration reference listed their settings as pending wiring and documented two
environment variables for the fallback. Those entries, and the architecture notes that named
the modules, are removed.

### The docs no longer tell clients to send `x-token-opt-state` back — Bug fix [Enterprise]

The portal docs said to send the `x-token-opt-state` response header back on your next call,
to carry the token budget across agents. The proxy never read it, so echoing it changed
nothing. The docs now describe it as a response header for your information. G17 counts a
conversation's turns itself, by its `workflow_id` —
<https://tokenlean.cbeyond.cloud/>

### `.env.template` no longer points the GCP scripts at the maintainer's project — Bug fix

The public `.env.template` set `GCP_PROJECT_ID` to the maintainer's own GCP project. The key,
backup and deploy scripts use that value before gcloud's current project, so a user who copied
the template sent commands to someone else's project. The value is now empty, so the scripts
use your gcloud project. The deploy-host setup script also defaulted to that project and set
gcloud to it; it now keeps your current gcloud project. `issue-key.sh`, `gcp-deploy.sh` and
the host setup also refuse the `your-gcp-project-id` placeholder from `.env.gcp.template`
instead of passing it to gcloud.

### The docs no longer call G26 a reserved slot — Bug fix

G26 (Context Budget Compaction) has shipped, but the deployment guides, the onboarding guide
and the API description still listed it as reserved. The reserved slot is G27 (multimodal),
which ships no image transform yet. The pipeline's own description of its response order
also left out the trust and safety stages; it now matches the code, and a test keeps it so.
- **OSS:** the four guides, `docs/config-reference.md`, `docs/request-flow-diagram.md`, and
  the API description.
- **[Enterprise]:** the portal's knob reference (and the Docs assistant's copy) now documents
  G26 and its settings, and no longer offers G27 settings that were removed. Reseed the Docs
  assistant after deploying —
  <https://tokenlean.cbeyond.cloud/>

### Group settings in `config/params` files now take effect — Bug fix

`config/params/*.yaml` files are merged into the configuration so that a group's settings can
live in their own file. A group's settings written there never reached the group, because
the files placed them at the top level while every group reads `groups.<name>`. The five
group templates shipped in `config/params` had the same problem, and some of their key
names were wrong too.

Put group settings under `groups:` and name only the keys you change, for example
`groups: {g22_deduplication: {enabled: false}}`. The group's other settings keep their values
from `config.yaml`. If a file sets a group at the top level, the proxy now logs a warning.
The five broken templates have been removed, since `config.yaml.template` documents every
group setting.

### A rotated BYOK key stops being used on every instance — Bug fix [Enterprise]

When a tenant rotates or deletes a provider key, each proxy instance is told to drop its
cached copy. The listener waited in a way that reconnected every 5 seconds while nothing
arrived, and a notice sent during a reconnect was lost. That instance then kept using the
old key until its cache expired, about a minute later by default. The listener now polls
without dropping its connection. Whenever it subscribes again, it clears its cached keys,
since any notice sent in the meantime is lost —
<https://tokenlean.cbeyond.cloud/>

### Streamed Gemini and Anthropic responses report their token usage — Bug fix

In a streamed response through the Gemini endpoint, the final frame's `usageMetadata` always
showed 0 tokens. The proxy sent the final frame as soon as the model stopped, but the token
counts arrive just after that. The final frame now waits for them.

On the Anthropic endpoint, `message_start` still reports 0 input tokens, because it is sent
before the counts are known. The closing `message_delta` now reports `input_tokens` as well
as `output_tokens`.

### One slow check no longer switches off GCP sign-in until a restart — Bug fix

To call services that require Google sign-in (Qdrant, the sidecars), the proxy and its
document jobs first check whether they run on GCP by asking the metadata server. If that
first check was slow or failed, the answer "not GCP" was kept for the life of the process,
and every such call was refused until a restart. On Cloud Run the check now reads Cloud Run's
own environment, so no metadata call is needed. Elsewhere, a "not GCP" answer is checked
again after 5 minutes.

### A bad or huge `X-Rag-Top-K` header no longer breaks or loads retrieval — Bug fix

G07 read the caller's `X-Rag-Top-K` header without checking it. A value that is not a number
made the request fail with a 500, and nothing limited a large one, so `X-Rag-Top-K: 1000000`
sized every vector-store search for that request to a million. The header is now used only
when it is a whole number of at least 1, and is capped at the new `max_top_k` setting
(default 50). Otherwise the configured `top_k` applies.

### A partly written config no longer switches off controls on reload — Bug fix

The proxy rereads `config.yaml` every 60 seconds. If it read a partly written file, it
applied whatever parsed, so a config cut short lost its later sections. Rate limits and spend
caps switched off and providers disappeared, with no error. A reload that lacks a top-level
section the running config has is now refused, and the running config stays. The proxy logs
an error and counts it in the new `token_opt_config_reload_failures_total` metric, as it does
for any reload that fails. To remove a whole section, restart the proxy.

### A cancelled request no longer goes on to the next provider — Bug fix

When a request was cancelled while the proxy was trying a fallback provider (the client
disconnected, or the proxy was shutting down), the cancellation was treated as that
provider's failure. The proxy then tried the next provider, and the request ended in a 502
that nobody received. A cancellation now stops the request at once.

### G06 `least_latency` stops picking a model that keeps failing — Bug fix

With `strategy: least_latency`, G06 picks the tier model with the lowest measured latency.
Only successful calls were measured, so a model whose calls always failed was never measured.
It counted as the fastest and was picked first on every request, and each request paid for a
failed call before failing over. A model whose call fails on the provider's side (a server
error, timeout or lost connection) is now passed over for 5 minutes, then tried again. A
rate limit or a rejected key does not count.

### G06 can reach the RouteLLM sidecar on GCP — Bug fix

On GCP the RouteLLM sidecar accepts only authenticated calls from inside its network. G06
called it without credentials, from outside that network, so every RouteLLM routing call
was refused, and routing fell back to the built-in heuristic. G06 now sends the proxy's
identity token. The deploy keeps the sidecar private through access control and lets the
proxy reach it, and the post-deploy check now fails if the sidecar answers anyone without
credentials. The change takes effect on your next GCP deploy.

### Deploy scripts keep scratch files private on a shared host — Bug fix

Several deploy and maintenance scripts wrote working files to fixed paths under `/tmp`.
Another user on the same machine could create those files first. The GCP deploy uploaded
whatever was at `/tmp/config.yaml` as the live configuration, and reused a copy an earlier run
had left there. The DSPy step ran a script from `/tmp`. The deploy-host setup installed
`terraform` and `cloud-sql-proxy` with `sudo` from files it had downloaded to guessable paths.
Each script now works in a private temporary directory that it removes when it exits.
- **OSS:** `gcp-deploy.sh`, `issue-key.sh`, `ci/dspy-optimize.sh` and
  `prepare-gcp-deploy-host.sh`.
- **[Enterprise]:** the managed deploy also keeps its config copies private, and no longer
  patches and uploads a leftover copy when a download fails. It no longer passes the database
  password as a command argument, which other users could see in the process list. Secret
  files restored from backup are now readable only by their owner —
  <https://tokenlean.cbeyond.cloud/>

### Prometheus on GCP now checks the proxy's certificate before sending its token — Bug fix

The Prometheus that Terraform sets up on GCP scraped the proxy over HTTPS with certificate
checks turned off, so anyone able to intercept the connection could have collected its
`/metrics` token. It now verifies the proxy's certificate like any other client. After you
apply Terraform, restart Prometheus so it loads the new configuration.

### Webhooks refuse addresses that resolve inside your network — Bug fix [Enterprise]

A webhook's address was checked only as written. A name that pointed (or was later
re-pointed) at a private address or the cloud metadata server passed both registration and
delivery, so the proxy sent requests into its own network.

- Registering a webhook now looks up its name and refuses it if the name points at a private
  or reserved address.
- Every delivery attempt looks the name up again. An unsafe answer stops the delivery and
  records it in the dead-letter list. A name that does not resolve is retried like any
  network error —
<https://tokenlean.cbeyond.cloud/>

### Ingest PII masking now stops the job instead of storing unmasked text — Bug fix

With `INGEST_PII_MODE=mask`, the document pipeline masks personal data before a document is
stored for retrieval. If its masking engine could not be loaded, it logged a warning and
stored the document unmasked. It now fails the job instead, and stores nothing.

A value other than `off`, `flag` or `mask` (for example a typo of `mask`) used to mean off.
It now fails the job too. `flag` mode is unchanged: it never alters the text, so it still
warns and continues.

### G06 cascade failures no longer write provider error text to the log — Bug fix

When a tier of the G06 cascade failed, the proxy logged the provider's error message twice:
once in G06 and again before falling back to a normal call. Those messages can include an
API key or the provider's base URL. The log now names the error class and HTTP status only.

### /ingest-doc checks its caller on every multi-tenant deploy — Bug fix

The document-ingestion webhook verified the Pub/Sub token only when `INGEST_REQUIRE_OIDC`
was `true`. A multi-tenant deployment that left it unset let anyone make it re-ingest any
object in a tenant's bucket, at the cost of extraction and embedding. With the flag on but
the push account's email or the audience unset, it accepted any token Google had signed.

- The check is now on whenever `DATABASE_URL` is set, unless `INGEST_REQUIRE_OIDC=false`.
- When it is on, it needs both `INGEST_PUSH_SA_EMAIL` and `INGEST_OIDC_AUDIENCE`. Without
  them the webhook answers 503, and the proxy logs why at startup.

The GCP deployment script already sets all three.

### /metrics now needs a scrape token, or an explicit opt-out — Bug fix

`/metrics` lists every tenant's id with its token and cost figures. With no
`METRICS_SCRAPE_TOKEN` set it was open to anyone who could reach the proxy. It now refuses
every scrape until you set the token, which your Prometheus presents as a Bearer token.

- To keep it open on a machine nobody else can reach, set `METRICS_ALLOW_UNAUTHENTICATED=true`.
- The local docker-compose stack sets it, so its Prometheus keeps working. Before anyone else
  can reach port 4000, set a token, or set `METRICS_ALLOW_UNAUTHENTICATED=false`.
- On GCP, Terraform now generates the token when `metrics_scrape_token` is empty and gives
  the same one to Prometheus. Apply Terraform before you next deploy the proxy.

### G07 and G22 settings now take effect per tenant and on reload — Bug fix

G07's chunk limits (`max_chunk_tokens`, `max_total_context_tokens`) and G22's embedding
settings (`use_embeddings`, `embedding_model`) were read once, from the first request after
start-up. A tenant's own values, or a config reload, then changed nothing until a restart.
Both are now read on every request.

### G28's statistics tool no longer counts other tenants' blocks — Bug fix

For a caller on the default tenant (a legacy key), the `headroom_stats` tool counted every
tenant's stored blocks, which showed how busy other tenants were. It now counts only the
caller's own. G28 is off by default.

### A workflow id cannot write into another tenant's usage logs — Bug fix

With G18's JSONL export on the local storage backend, a caller could send a workflow id such as
`../other-tenant/x` and have its usage record written into another tenant's folder: the path
stayed inside the export root, which was all the backend checked. The workflow id is now reduced
to a single safe path segment under the caller's own tenant.

### A malformed key without a tenant is refused — Bug fix

A proxy key whose stored record named an empty or null tenant (only possible by editing the key
store by hand) was accepted, and the usage export then skipped its tenant filter and returned
every tenant's usage. Such a key is now refused, and the export refuses any non-admin caller
without a tenant.

### A deactivated operator is signed out of the admin console at once — Bug fix [Enterprise]

Deactivating an operator stopped new logins but left their console session working until it
expired. The session is now re-checked on every use and ends as soon as the operator is
deactivated. The console guide also no longer calls its sign-in a two-factor gate: the operator
login protects the console screens, while the admin API accepts the admin key alone —
<https://tokenlean.cbeyond.cloud/>

### Admin audit rows name the authenticated admin — Bug fix [Enterprise]

The audit rows for re-encrypting stored provider keys and for generating an evidence pack took
their actor from a request header, so whoever held the admin key could record any name; every
other admin action already recorded the authenticated caller. These two now do too —
<https://tokenlean.cbeyond.cloud/>

### A password-reset request no longer reveals whether an account exists — Bug fix [Enterprise]

The forgot-password answer was the same for every address, but for a registered one the portal
sent the reset email before answering, which took noticeably longer: timing a few requests told
anyone which addresses had accounts. The email is now sent after the answer, so both cases
answer equally fast —
<https://tokenlean.cbeyond.cloud/>

### A portal session is not trusted when its checks cannot run — Bug fix [Enterprise]

Every portal request re-checks that the user is still active and unsuspended and that the
tenant's contract is active. When that check failed, for example during a database outage, the
session was accepted as if it had passed, so a suspended user kept the portal for as long as the
outage lasted. The portal now answers 503 ("try again shortly") until the check can run, and
keeps the session for when it can —
<https://tokenlean.cbeyond.cloud/>

### The portal image installs the versions it was tested with — Bug fix [Enterprise]

The portal image was built without its npm lockfile, running `npm install` with a flag npm does
not have, so every build took whatever versions the ranges allowed that day; its Python
server installed the newest `fastapi`, `uvicorn` and `httpx`; and it ran as root. It now builds
with `npm ci` from the lockfile, installs the Python packages at the versions the proxy image
pins, and runs as an unprivileged user —
<https://tokenlean.cbeyond.cloud/>

### The LLMLingua sidecar answers while it compresses — Bug fix

The compression sidecar ran the model on its only event loop, and loaded it there on the first
request after a start: a long prompt, or that first request, held up every other tenant's
compression and the health check for seconds, or tens of seconds after a start, until the proxy
gave up. The model now loads before the sidecar serves, and compression runs on worker threads
(`LLMLINGUA_CONCURRENCY` at a time, default 2).

### Local backups go only to a bucket in your own project — Bug fix

`scripts/local/docker-backup.sh` (run by `stop-local.sh --backup`) fell back to the bucket name
`token-opt-config` when none was configured. Bucket names are global, so the full database and
Redis dumps went to whoever owned that name, and they were left in `/tmp`. The backup now needs
`--bucket` or `CONFIG_GCS_BUCKET`, checks that the bucket is in your project (creating it there
if missing, refusing it otherwise), writes the dumps to a private temporary folder that is
deleted afterwards, and uploads them to `backups/<timestamp>/`.

### Starting a paused GCP deployment no longer cuts the proxy off from Qdrant — Bug fix

`scripts/gcp/start-gcp.sh` checked the Qdrant collection by opening Qdrant to everyone, probed it
without its API key (so it always looked empty and offered a reseed), then set Qdrant to
internal-only access, which the proxy and the pipeline jobs cannot use: retrieval and docs chat
returned nothing until the next Terraform apply. It now reads the collection as you, with the
key, and leaves Qdrant's access as deployed; only a reseed opens Qdrant to everyone, briefly,
and closes it again however the script ends, a Ctrl-C included. `gcp-deploy.sh`'s seeding
window closes the same way.

### The GCP deploy completes without Qdrant — Bug fix

With `enable_qdrant=false` (the pgvector set-up, and the default of the managed deploy),
Terraform reports no Qdrant address and `scripts/gcp/gcp-deploy.sh` stopped with "QDRANT_URL is
empty" after the sidecars and Langfuse but before the proxy. It now requires the address only
when `ENABLE_QDRANT` is on, and the proxy and the pipeline jobs get no `QDRANT_URL` without it
(an update in place removes one an earlier Qdrant deploy left).

### Langfuse's database password is no longer in its plain environment — Bug fix

`scripts/gcp/gcp-deploy.sh` put the Langfuse database URL, password included, in the service's
environment variables, readable by anyone with view access to Cloud Run, and did not URL-encode
the password, so a generated password containing `#`, `?`, `%`, `@` or `/` stopped Langfuse from
starting. The password is now encoded and the URL kept in Secret Manager
(`langfuse-database-url`), which Langfuse reads at start. If your deployment ran the old script,
consider rotating the database password.

### Alertmanager's alerts reach the proxy — Bug fix

The Terraform-built Alertmanager posted alerts to the proxy's `/admin/alert-webhook` with no
credentials, and the endpoint accepts only an admin key, so every alert was refused; with no
proxy address it posted to `localhost` inside its own container. Terraform now generates a
token that Alertmanager presents and the proxy reads as `ALERT_WEBHOOK_TOKEN` (`gcp-deploy.sh`
mounts it); it opens that endpoint only, and is not sent to a webhook URL of your own
(`alert_webhook_url`). With nowhere to send alerts, `terraform plan` now warns.

### Cloud SQL is backed up, with point-in-time recovery — Bug fix

The Terraform module created the Cloud SQL instance with neither automated backups nor
point-in-time recovery, though it holds `usage_events` (the billing record), the audit log,
tenant configuration and keys: a bad migration or a mistaken `DELETE` could not be undone. Daily
backups (7 kept) and point-in-time recovery over 7 days of logs are now on by default
(`db_backups`, `db_point_in_time_recovery`, `db_backup_retained_count`,
`db_transaction_log_days`, `db_backup_start_time`). Applying it may restart the instance once,
and backup storage is billed.

### Prometheus alert rules use only labels the metrics carry — Bug fix

Three recording rules in `infra/prometheus-alerts.yml` grouped requests and cost by `user_id`,
a label no metric has, so each recorded one series with no breakdown; they are removed
(per-user figures are in `usage_events`). The Terraform module now loads every `*rules.yaml`
file beside the alert rules into the one rules file Prometheus reads, and a test checks that
every metric and label a rule uses is one the proxy exports.
- **OSS:** the rule fix, the loading of extra rule files, and the test.
- **[Enterprise]:** the per-tenant SLA alerts (p99 latency, error rate) had never been loaded
  by Prometheus, and the error-rate alert filtered on a status label its metric lacks, so
  neither could fire. Both are now loaded and the error rate counts responses by status —
  <https://tokenlean.cbeyond.cloud/>

### The local Docker stack no longer exposes its databases and admin tools to the network — Bug fix

`docker-compose.yml` published Redis and Qdrant (neither with authentication), Postgres, the
sidecars and the admin tools (Langfuse with a default admin password, Grafana, Prometheus,
Jaeger) on every network interface, and ports Docker publishes bypass the host firewall: on a
machine with a public address all of them were reachable. They now listen on 127.0.0.1 only;
set `TOKEN_OPT_BIND` in `.env` to widen them deliberately, after setting real passwords. The
proxy itself still listens on all interfaces.

### A tenant that upgraded this month sees its enterprise quota — Bug fix [Enterprise]

The portal's usage page and the admin console's tenant view took the tenant's tier from the
alphabetically last tier on this month's usage, and "free" sorts after "enterprise": a tenant
that upgraded during the month was shown as free with an unlimited quota. Both now use the
tier of the most recent request, as the spend-limit and SLA views already did —
<https://tokenlean.cbeyond.cloud/>

### SOC2 evidence packs are signed, and large months no longer load into memory — Bug fix [Enterprise]

An evidence pack's only proof that it was unaltered was a SHA-256 over its own events, which
anyone editing the pack could recompute, and the hash left out each event's details (the
guardrail categories and PII entity types the trust & safety evidence rests on). Packs are now
signed with an Ed25519 key held by the service, over everything in them, and the hash covers
the details. Auditors check a pack against the public key the admin console publishes; a pack
from a deployment with no signing key says it is unsigned. A month with many audit rows was
read whole into the proxy's memory to build its pack; the rows are now read in batches and the
pack streamed to the download. A row with no request id no longer counts as a request in the
summary —
<https://tokenlean.cbeyond.cloud/>

### Portal emails' one-time links are no longer written to the logs — Bug fix [Enterprise]

With no email provider configured, the portal fell back to a stand-in sender that wrote every
verification, password-reset and invite email, one-time link included, to the proxy log: anyone
able to read the logs could request a reset for any customer and take the account. The stand-in
now logs only recipient and subject (the link only on a local development stack that asks for
it). On a managed deploy with no provider, no email is sent and each one is logged as an error
rather than passing for delivered, and an admin invite now reports `invited: false` when the
email did not go out —
<https://tokenlean.cbeyond.cloud/>

### A tenant's IP allowlist is enforced whether or not the global allowlist is on — Bug fix

A tenant's own source-IP allowlist was checked only while `ip_allowlist.enabled` was on, and that
switch is off by default, so a list saved for a tenant could restrict nothing. A tenant's list is
now enforced whenever it has one; `enabled` switches on only `global_cidrs`. A key issued to the
tenant after its list was set, or by rotation, now carries the list too (before, a new key was an
unrestricted way in). Before upgrading, review the tenants that have a list: if the global switch
was off, they become restricted.
- **OSS:** enforcement, and the list on new and rotated keys.
- **[Enterprise]:** the admin console shows the list each stack's keys enforce. It was kept per
  company, so a sibling stack appeared restricted when it was not, and creating another stack
  blanked the list shown for the first while its keys stayed restricted. A stack whose keys carry
  different lists is flagged —
  <https://tokenlean.cbeyond.cloud/>

### Checking a proxy key no longer stalls the proxy on Secret Manager — Bug fix

With proxy keys kept in Secret Manager (the default for a self-hosted deploy on GCP), a key the
proxy had not cached, or a cache past its five-minute lifetime, made the request read the secret
with a blocking call on the server's event loop, stalling every request on that worker for the
call, for its full timeout during a Secret Manager outage. A stream of random keys forced one
every five seconds. That read now happens off the event loop, one at a time, and while Secret
Manager is failing the proxy keeps answering from the keys it has instead of retrying on every
request.

### Cost routing works for tenants that bring their own provider keys — Bug fix

G06 only routes a request to a cheaper tier model whose provider it can reach, and it judged
that from platform keys alone. On a deployment that holds no platform keys and serves every
tenant on its own key, every routed model looked unreachable, so each request was served on the
model it asked for and G06's cost routing, including a tenant's own tier choices, did nothing.
A tenant's own key for the provider now counts.

### Code blocks keep their import lines as written — Bug fix

G19's import "compression", on by default, merged nothing: it only removed the leading
whitespace of every import line, so an import inside a function reached the model at the top
level, an indentation error the user never wrote. Import lines are now left as they are, and the
`compression_strategies.code.compress_imports` setting is no longer read.

### The injection threshold cannot switch detection off — Bug fix

A tenant cannot turn G30 or G31 off, but could set their `threshold` to 1.0, above every rule's
severity, so nothing was ever flagged. The scanner now caps any threshold at its strongest rule,
and a tenant's own threshold is capped at the weakest built-in rule (0.8), including a value
saved earlier.
- **OSS:** the scanner cap and the cap on a tenant's override.
- **[Enterprise]:** the portal's threshold setting now ranges 0.0–0.8 —
  <https://tokenlean.cbeyond.cloud/>

### Masked personal data is restored inside tool calls too — Bug fix

In G29 `mask` mode, personal data in an earlier tool call was replaced by a placeholder on the
way to the model, but the placeholders the model then wrote into a new tool call came back to
the client unchanged: an agent's tool would send mail to `[PII:EMAIL:1]`. Tool-call arguments in
responses are now restored like message text, and personal data the model writes into them is
masked (or, in `flag` mode, counted).

### A Presidio-backed PII check no longer reloads its model per request — Bug fix

With `use_presidio` on, every PII detector built its own Presidio engine, loading the spaCy
model (seconds, hundreds of MB) on the request path, and G29/G31 rebuild their detector whenever
the entity set changes, so tenants with different `phi` settings made it rebuild on alternate
requests. One engine is now built per process and shared.

### A recovering provider gets one probe, not all the traffic — Bug fix

When a provider's circuit breaker finished its cooldown, it let every request through while the
first one tested the provider, so all of them hit a provider that might still be failing, each
paying retry time before failing over. And if that first request ended with a 4xx, the breaker
stayed in its testing state and let everything through for good. Now one request tests the
provider while the rest fail over, and a test that never reports an outcome makes way for a new
one after the cooldown.

### Anthropic and Gemini batch jobs use the resolved key — Bug fix

The provider-native batch lane for Anthropic and Gemini never passed the key it was given to
the provider calls, so litellm fell back to the provider's own environment variable. Where that
variable is kept out of the proxy, as recommended, batch submission always failed and the
discounted lane never ran; where it is present, jobs ran on a credential nobody resolved. Every
batch call now uses the resolved key.

### Anthropic and Gemini tool and format controls are no longer lost — Bug fix

An Anthropic `tool_choice` of `{"type": "none"}` reached the model as `auto`, so it could call
tools the client had forbidden, and `disable_parallel_tool_use` was ignored. On the Gemini route,
`toolConfig` (function-calling mode and allowed functions), JSON mode (`responseMimeType` and
`responseSchema`), `topK`, `candidateCount`, `seed`, the penalties and `thinkingConfig` were
silently dropped. All are now carried. A Gemini `generationConfig` field the proxy cannot carry,
or an Anthropic `tool_choice` it does not know, is refused with a 400 that names the field.

### `/admin/budget-status` is removed — Bug fix

The endpoint summed Redis keys that nothing in the proxy writes, so it reported zero
consumption for every team and feature however much had been spent. It is no longer served.
Per-tenant usage and cost are in `/admin/usage-export`, and spend is enforced by the spend cap.

### Billing rows and counters of the last requests survive a shutdown — Bug fix

A served request's billing row, security audit row, quota, trial and spend counter updates and
webhook event deliveries ran as background tasks that nothing kept: their failures were never
logged, and on a scale-in or deploy the proxy closed its Redis and database pools under them,
so the last moments' invoice rows and counter updates could be lost. They are now held until
they finish, failures are logged, and shutdown waits up to five seconds for them before the
pools close.

### Langfuse traces no longer store request text by default — Bug fix

`capture_trace_content` defaulted to on, so with Langfuse tracing enabled every prompt and
answer was written to a Langfuse project that all tenants share, including personal data that
G29 had detected but, in its default `flag` mode, left in place. It now defaults to off. When
it is on, a request whose personal data G29 or G31 found and did not mask is still traced
without its text. Spans keep their token counts either way. If your config sets
`capture_trace_content: true`, review that choice.

### Agent URLs that point inside the network are refused — Bug fix

An F2 agent URL was checked only against three host names and literal private addresses,
so `http://metadata.google.internal./` (trailing dot), `http://2852039166/` (the metadata
address written as a number) or, for an agent a tenant saved, `http://langfuse:3000` were
accepted, and a matching prompt made the proxy send the conversation there.
- **OSS:** every agent URL is refused if its host is the metadata service or loopback (with
  or without a trailing dot), an IPv4 address spelled as a number, or any address that is
  not public. Agents defined in your own config file may still name hosts on your network,
  as the configuration example does.
- **[Enterprise]:** an agent saved from the portal must also name a public host (no
  single-label, `.internal`, `.local` or `.svc` name), and its name is resolved before each
  call and refused if it points inside; webhook URLs are held to the same name and address
  rules when saved and before delivery — <https://tokenlean.cbeyond.cloud/>

### Streamed tool calls are checked per answer when a request asks for several — Bug fix

With `n` above 1, each streamed answer numbers its tool calls from 0, and G32's stream gate
remembered its verdict by that number alone. A tool call your policy denies could then reach
the client in the second answer because an allowed call with the same number came first in the
first answer, and an allowed call could be withheld the other way round. Each answer's calls
are now judged on their own.

### G23 no longer reports savings it never made — Bug fix

G23 collapsed repeated phrases in each answer into a second, shorter copy that it added to the
response as `x_compressed_content`, and booked the difference as savings. The client always
received the full answer and nothing ever read the shorter copy, so those savings (in group
savings and the per-group USD figure, priced at the input rate) were not real, and every
response carrying repetition grew by a second copy, which the cache stored too. G23 now only
measures: the response is returned unchanged and the repetition is counted in
`token_opt_g23_compressible_output_tokens_total`. Dashboards that showed G23 savings will show
none.

### G18's JSONL export no longer blocks other requests — Enhancement

With `jsonl_gcs_bucket` set, G18 built a new Cloud Storage client and uploaded each request's
record on the event loop, stalling every request on that worker for the length of the upload.
The upload now runs off the event loop, and one client is reused for the process.

### The `feature` metric label is bounded by default — Bug fix

Any caller could send a new `X-Feature` value on every request, and each one created new
Prometheus series on G18's counters that were never removed, growing proxy memory and the
`/metrics` scrape without limit. With no `label_values.feature` list configured, every value
other than `default` is now counted as `other`; list the features you want to see, or set
`"*"` to keep every value as before.

### Switching G18 off no longer zeroes a tenant's costs — Bug fix

A tenant can switch G18 (observability) off in the portal. That also skipped pricing every
non-streamed answer, so its cost was recorded as $0, its spend counter never grew and a spend
cap could never trip, while streamed answers were still priced. Pricing now runs whether G18 is
on or off, for batched answers too; the switch covers G18's metrics, export and tracing.

### Batched results are served only to the tenant that sent the request — Bug fix

`GET /v1/batch/results/{id}` checked the owner only when one was on record, and the owner
record expired an hour after the request was queued, before a provider batch (24-hour window)
or a long backlog finished. Any key that knew the request id could then read the answer. The
owner is now recorded before the request is queued (a request whose owner cannot be recorded
is answered at once), lasts longer than any batch can wait and as long as its result, and an
id with no owner on record is not found for every caller but an admin key.

### TOON leaves data it cannot write faithfully as JSON — Bug fix

G13's TOON notation turned on whenever a system prompt mentioned "schema" and contained a "|",
as any markdown table does, and wrote values unescaped: a "|" or a line break inside a value
shifted the columns or split the row, and null, an empty string and a missing key all came out
as an empty cell. TOON now needs the marker itself (a system line such as `schema:name|age`)
unless `toon_auto_detect` is on, a block with a "|" or line break in any value or key stays
JSON, and null, empty and missing are written differently.

### G11 caps reach the model's configured limit — Bug fix

G11 looked the model's output limit up under a request parameter that is never set, so every
`max_tokens` it applied, from `fallback_max_tokens` or from completion history, was clamped to
4096 and the `model_max_tokens` table was never read: a configured 8000-token fallback on a
16k-output model cut answers at 4096. The limit now comes from the model the request is routed
to, and a model missing from the table gets `default_model_max_tokens`.

### Tool descriptions keep 'make sure', 'might' and 'not just' — Bug fix

G08 compresses tool descriptions by default, and the compressor removed words that carry the
instruction: "Make sure the date is ISO-8601. This might return an empty list." reached the model
as "Make date is ISO-8601. This return empty list.", "just-in-time" lost its "just", and "not just
the IDs but the records" became "not the IDs but the records". Those words are now kept, as are
hyphenated compounds, no surviving word changes case (a lowercase parameter name opening a
sentence stays lowercase), and a description whose compression would drop a negation or a bound
is sent as written, the same check G01 applies to prompts.

### Adaptive-bypass rules learned on benchmarks stay with benchmark traffic — Bug fix

A G24 rule that named benchmark `datasets` also matched requests that carried no dataset tag,
which is all production traffic, so a skip learned on one benchmark applied to every tenant. It
now matches only requests tagged with one of its datasets. `scripts/review_bypass_candidates.py`
now requires `--tenants <ids>` or an explicit `--global`, and writes that scope into each approved
rule. G24 also stopped re-reading an empty rules source on every request (on GCP, two blocking
downloads per request) and loads its rules off the event loop.

### G20 matches a template to the whole system prompt — Bug fix

G20 looked up an optimised prompt by a fingerprint of the system prompt's first 512 characters,
so two prompts sharing a long common start, such as a policy followed by per-user details, got the
same template: the second user's prompt was replaced by the first user's. The fingerprint now
covers the whole prompt, a system message with image or other non-text parts is left alone
instead of failing the request, and the saving is counted in tokens rather than words. Templates
stored under the old fingerprint have to be stored again (G20 is off by default).

### Tool pruning no longer removes a tool the request names — Bug fix

G08 could prune the very tool a request forced with `tool_choice` (or the legacy
`function_call`), or one an earlier assistant turn had already called, whenever the operator's
tool registry gave it intents the latest message did not mention. The provider then rejected the
request. Those tools are now always kept.

### A malformed `tenants:` block no longer switches the response cache off — Bug fix

An operator `config.yaml` whose `tenants:` key was left empty, or held a tenant entry that was
not a mapping, made G05 fail on every cache lookup and store for every tenant: nothing was
cached or served from the cache. G05 now reads its per-tenant scope through the same
type-checked lookup the other groups use, and falls back to the global setting.

### Bypass rules from the database now apply for their whole cache window — Bug fix

G04 used a rule loaded from the `bypass_rules` table for one request, then switched back to the
config rules until the cache window expired, so a database rule applied to about one request a
minute per worker. Without such a table (no migration creates one) it opened a new Postgres
connection on every request to find that out. The database's answer, rules or none, is now kept
for `db_cache_ttl_seconds`, the query uses the shared connection pool, and the per-rule
statistics go to Redis in one round trip.

### The RAG fallback no longer blocks the server — Bug fix

When a tenant's primary document search found nothing, G03's fallback chain embedded the query
and searched Qdrant synchronously for each of up to four strategies, holding up every other
request on the worker, and with Qdrant unreachable each attempt waited out its full timeout. The
chain now embeds once, off the event loop, uses one asynchronous Qdrant client that is always
closed, and skips a collection that does not exist. Its `G3_doc_pipeline.rag_fallback` settings
(`enabled`, `strategies`, `top_k`, `similarity_threshold`) were documented but never read;
they are now. G07's primary search also closes its client when it fails.

### A rate-limit override that sets one window no longer lifts the limit — Bug fix

A `rate_limit.per_user` or `per_team` entry that set only `requests_per_minute` (or only
`requests_per_hour`) made the limiter fail on the missing window and let every request from
that user or team through. Overrides are now merged over the default limits, as `per_tenant`
and `tiers` already were, and an entry that is not a mapping falls back to them.

### Judge, summary and repair calls now count toward a request's cost — Bug fix

A request's `cost_actual_usd` priced its answer (and, since an earlier fix, every cascade tier
it tried) but not the other paid calls made on its behalf: the G06 judge, G09's schema
extraction, the G10/G26 summariser and G11's repair re-ask. The figure, the spend cap that accrues
it and the savings percentage all flattered the proxy. Each such call is now recorded and added
at list price, wherever it happens. The G06 judge also uses the served tenant's provider key,
as the request's own call does.

### Only TokenLean's own X-* headers are kept with a request — Bug fix

Every `X-*` request header was copied into the request's parameters, which the batch lane stores
in Redis, so infrastructure headers such as `X-Cloud-Trace-Context` were kept with queued requests
(client-address headers were already removed by an earlier fix). Only the headers TokenLean reads,
such as `X-Template-ID` and `X-Session-ID`, are kept now. G06 routing rules still match any
`X-*` header, through a copy that is never stored.

### LLMLingua compression on GCP is now an explicit choice — Bug fix

The GCP deploy pointed G01 at the LLMLingua sidecar without its `/compress` route, so every call
failed and G01 quietly fell back to its other compressors: GCP tenants never got LLMLingua and paid
a failing call per message. Switching LLMLingua on changes what compressed prompts look like, so
on GCP it is now opt-in: set `LLMLINGUA_ON_GCP=true` in `.env.gcp` and the deploy points G01 at
the right route. Otherwise the deploy turns it off explicitly, which is what tenants were already
getting. An empty `sidecar_url` now skips the call entirely.

### The RouteLLM sidecar decides without making a billed completion — Bug fix

With `classifier: routellm`, every routing decision made a real one-token completion on the
sidecar's OpenAI key, sending the tenant's conversation to the strong model. The name it answered
with never matched the model G06 expected either, so G06 fell back to its heuristic anyway. The
sidecar now asks RouteLLM only to decide, which calls no model, and answers with the weak or
strong model name G06 sent. Each router loads on first use, so the large `causal_llm` model
loads only when asked for. The config reference now names the `routellm.url` key G06 reads.

### The prefix-cache floor no longer keeps a span whole when that costs more — Bug fix

With `preserve_cacheable_prefix` on, G08 and G19 refused to shrink the cacheable span whenever
compressing it down to the provider's minimum would have been cheaper. They cannot do that:
they can only shrink or not, so a refusal kept the span whole, and with a small cache discount
that costs more than the shrink it refused (1.66x on one measured provider). G01 had the same gap
when its rate-dial compressor was unavailable. Each now compares compressing with what it can
actually do instead. The setting is off by default.

### CI now scans tracked files for credentials — Enhancement (OSS)

Path rules catch a secret file, but never a real key pasted into a tracked one: a template, a
test fixture or a doc. A new `scripts/ci/leak_scan.py` looks for high-confidence credential
formats (private keys, cloud and model-provider API keys, TokenLean proxy keys) and CI runs it
over every tracked file on each push and pull request. It prints the file, line and kind of
credential, never the value. A fixture line that must hold such a value can carry
`leak-scan: allow`.
- **OSS:** `python scripts/ci/leak_scan.py --tracked`, or `--staged` before a commit.

### The proxy image leaves out every commercial path, and CI now checks them all — Bug fix

The open-core checks each kept their own copy of the commercial file list, and the copies had
drifted: `src/proxy/.dockerignore` missed five commercial paths, so a proxy image built from a
tree that has the commercial files (the maintainers' own builds) included them. Images built
from this public repository were never affected, since it doesn't contain those files. The
paths are now excluded, and CI reads the commercial paths from the commercial sections of
`.gitignore` (new `scripts/ci/commercial_paths.py`) instead of a list of seven, so a new
commercial path is checked as soon as its `.gitignore` line exists. CI also fails if a
commercial path under `src/proxy` is missing from `.dockerignore`.

## 2026-09-30

### Batched requests are no longer marked failed while another instance works on them — Bug fix

With more than one proxy instance, a batch that took over 30 seconds had its unfinished
requests taken by another instance and marked failed, though the first was still working on
them; a client polling in between saw "failed", resubmitted and paid twice. An instance now
keeps its claim on what it is processing, so only work left by a stopped instance is taken
over, and that work is retried (`max_attempts`, default 3) before being marked failed. A
failure never replaces an answer already stored, and each process uses its own consumer name.

### Claude Code and other Anthropic SDK clients keep their prompt caching — Bug fix

The Anthropic endpoint (`/v1/messages`) dropped the `cache_control` markers a client places
on its system prompt, messages, tools and tool results, so Anthropic cached nothing and every
turn paid full input price for a prefix it would otherwise bill at about a tenth. The markers
now reach Anthropic where the client put them, and the proxy adds none of its own to such a
request (Anthropic rejects more than four). Markers go only to providers that cache by marker
(Anthropic, Bedrock): a request routed, cascaded or failed over to another provider is sent
without them, on either endpoint, instead of becoming a separately billed Gemini cache.

### Reported savings no longer count a provider's own prompt-cache discount — Bug fix

OpenAI, Gemini and other providers cache repeated prompts on their own, with or without the
proxy, but the savings figure priced the "without the proxy" cost at full price, so their
cache discount showed as the proxy's saving on the portal and on invoices. That cost now gets
the same discount whenever the request would have been cached without the proxy: its provider
caches on its own, or the caller marked the prompt for caching itself. A discount the proxy
made possible (its Anthropic cache marker, or routing to a provider that caches) still counts.
Figures already recorded are unchanged.

### The proxy and the fine-tune job reach their private Redis on every GCP deploy — Bug fix

The Redis Terraform creates on GCP (VM or Memorystore) has only a private address, but the
deploy gave Cloud Run access to the private network only when Cloud SQL was private. On the
open-source default the proxy could therefore not reach Redis, so rate limits, quotas, spend
caps, trial counters, the cache and sessions quietly stopped working, and the fine-tune job
could not reach it on any deploy. Both now get Direct VPC egress on the project's default
network whenever the Redis is Terraform's, for private addresses only.

### Qdrant on GCP no longer loses its documents when it sits idle — Bug fix

On GCP, Qdrant keeps its collections in its Cloud Run instance's own storage, and it scaled to
zero whenever it sat idle, which erased every tenant's ingested documents and the docs-chat
collection. Under load, Cloud Run could also start a second instance holding different
documents. Qdrant now runs as exactly one instance that is always on, billed at Cloud Run's idle
rate, including while a project is paused. A change to the Qdrant service or a Cloud Run restart
still starts it empty: ingest the documents again after one. Durable storage is a separate
change.

### Retrieved documents no longer stop the provider caching the system prompt — Bug fix

G07 (on by default) put retrieved documents in front of the caller's own system prompt, so
every prompt started with text that changes with each query. The provider's prompt cache
never matched the stable system prompt, and G21 hashed the documents into OpenAI's
`prompt_cache_key`, spreading requests across cache shards. The documents now sit just
before the latest user turn and stay out of the cached prefix and its key. They remain a
system message, so G31 still scans them, and G11's and G12's appended instructions still
go to the caller's system prompt rather than into them.

### A session id no longer costs an extra model call on every request — Bug fix

G10 is on by default. For every request carrying a session id it asked a model to summarise the
whole conversation, adding cost and latency, and put that summary into the next request,
although a chat client resends the whole conversation anyway. For a client that sends only its
newest turn, the summary covered just that one request, so it remembered one request back. Now
a request that resends the conversation costs no summary call and gets nothing added, while a
request with no more turns than last time gets the stored summary, which builds on the previous
one. Each session's first request is summarised once.

### Claude requests no longer get extended thinking they did not ask for — Bug fix

When G25 was off for a tenant, or a G24 rule skipped it, G12 gave every request that named no
reasoning its configured default, `medium`. On Anthropic that switched extended thinking on, so
the customer paid for reasoning nobody had asked for. G12 now caps the platform's default at
the provider's own default, as G25 already does: an Anthropic request that asks for nothing gets
no thinking, OpenAI's o-series and Gemini keep reasoning at medium, and a platform default of
`high` becomes medium on the o-series. A request's own setting, and a default the tenant set in
the portal, are used as they are. An operator can raise a provider's default with
`providers[].default_reasoning_effort`.

### Grafana, Langfuse, Qdrant, Prometheus and Alertmanager no longer run as the proxy's service account — Bug fix

On GCP these services ran as the proxy's service account, so whoever compromised one (Grafana is
public; all five are third-party images) could read the proxy's provider keys and database
password, decrypt tenants' stored provider keys, reach Cloud SQL and call any Cloud Run service.
On the open-source default that also meant every secret and bucket in the project, including the
backups of the operator's own secrets. Each now runs as an account of its own holding only what
it uses, and the proxy's account reads only its own secrets by default (`least_privilege_secret_iam`
now defaults to `true`). Two grants nothing used, on the RouteLLM account, are gone. After
upgrading, run one deploy without `--skip-infra`. If you point a bucket of your own at
`/ingest-doc`, grant `token-opt-proxy-sa` `roles/storage.objectViewer` on it.

## 2026-09-29

### Batched requests count against the quota, trial and spend cap — Bug fix

A request sent with a `batch_topic` was billed when it was queued, but it never counted
against the tenant's monthly quota or free trial, and its cost never reached the spend cap, so
a tenant could send everything as batched requests and never reach a limit. It now counts
against the quota and trial when it is queued. When its answer arrives, its cost, priced from
the usage the provider reported (at `batch_discount_multiplier` on the native batch lane), is
added to the tenant's spend counter. A request an admin key sends as the tenant counts against
none of them.

### An admin key acting as a tenant is no longer billed to that tenant — Bug fix

An admin key that sends `X-Tenant-ID` acts as that tenant, for support or benchmarks. Each of
its answers wrote a billable usage row for the tenant and counted against the tenant's monthly
quota, spend cap and trial, and nothing recorded who had sent it. Such a row now names the
admin key's own tenant in a new `impersonated_by` column and is not billable, so it stays off
invoices and usage counts, and the counters are left alone. The provider call itself is
unchanged: under strict BYOK it uses the tenant's own provider key.

### Redis can now require a password — Bug fix

Redis ran with no password, and its network rules let any private address reach it: a
compromised host in the VPC could read every tenant's cached prompts and answers, sessions and
memory, and reset rate-limit and spend-cap counters. Terraform now keeps a Redis password in
Secret Manager (`redis-auth`), and the GCP deploy mounts it into the proxy and the fine-tune job,
which log in with it. `REDIS_AUTH_ENFORCE=true` then makes Redis require it: on the VM backend
with no outage, in a later deploy followed by one restart of the Redis VM; on Memorystore by
turning AUTH on. See "Make Redis require its password" in docs/deployment-gcp.md. Traffic to
Redis is still unencrypted.

### A tenant's own provider key no longer appears on a fine-tune job's execution — Bug fix

A fine-tune for a tenant with its own provider key passed that key to the Cloud Run job as a plain
environment override, and an execution keeps its overrides: anyone who could view the job's
executions could read the key. The key is now stored as a new version of a Secret Manager secret,
`finetune-tenant-key`, and the job is given only the version's name. It destroys the version once
the run has ended cleanly. Terraform creates the secret and gives the proxy's service account
version rights on that secret alone: run `terraform apply` before deploying, since such
fine-tunes do not start until the secret exists. Executions from before this change still hold
the key in plain text: delete them, and consider rotating the keys concerned.

### Provider calls now time out, and retries no longer stack — Bug fix

No provider call set a timeout. A provider that accepted the connection and then stalled held the
request for 10 minutes, the client library's own limit, long after the client had given up, so
failover never happened. On a 429 or 5xx the client library also retried twice under the proxy's
own retries: up to six calls to one provider, each of which could be billed. Every provider call
now waits at most `resilience.request_timeout_seconds` (default 300; for a stream, each wait
between chunks), and a call that runs out is retried and failed over like any provider error.
While the resilience layer is on, only it retries the main call and its failover.

### A failing response stage no longer turns a paid answer into an unrecorded error — Bug fix

After the provider had answered and billed, the response stages ran with no error handling: one
that raised ended the request in a 500, and no usage row, quota or spend count, or security audit
row was written. A failing optimisation or observability stage is now skipped, logged and counted
on `token_opt_response_stage_errors_total`, and the answer is served. A failing safety stage
(response PII masking, response guardrails, tool eligibility) still withholds the answer with a
500, but the request is recorded: a non-billable usage row priced at what the provider billed, and
its security audit row. G16 also accepts `"tools": null`, which used to fail the request.

### A managed proxy instance no longer serves before its API keys have loaded — Bug fix [Enterprise]

On the managed service, proxy API keys are checked against a Postgres key store. If setting up
that store failed while an instance was starting, the instance checked keys against the key list
kept from before the move to Postgres, which is no longer updated: keys revoked since still worked
there, and keys created since were refused. If only the first read of the keys failed, the
instance refused every key until it was replaced. An instance now retries until the keys have
loaded, and serves only after that.

Self-hosters have nothing to upgrade — <https://tokenlean.cbeyond.cloud/>

## 2026-09-28

### Strict BYOK no longer lets a stored "Bedrock key" bill the platform — Bug fix [Enterprise]

Under strict bring-your-own-key, a tenant could store any string of eight or more characters as its
Bedrock key and have it accepted. Calls to Bedrock are signed with the platform's AWS credentials
and ignore that key, so the platform paid for the tenant's usage. Under strict BYOK, a provider
called with platform-held credentials (Bedrock, or any provider configured with
`requires_api_key: false`) now returns 402 for tenants whatever key they stored, and the portal
refuses to store a key for it. Exempt tenants and deployments without strict BYOK are unchanged.

Self-hosters have nothing to upgrade — <https://tokenlean.cbeyond.cloud/>

### The managed prompt-injection rules no longer drop out between config reloads — Bug fix [Enterprise]

On the managed service, a feed adds the managed prompt-injection rules to G30's configuration
every five minutes, but the configuration reloads every minute and each reload replaced it whole.
For about four minutes in every five, G30 ran with the open-source rules only. The rules are now
applied to every reloaded configuration before it takes effect. The open-source core gains the
hook this uses (`config_loader.register_post_load`); self-hosted behaviour does not change.

Self-hosters have nothing to upgrade — <https://tokenlean.cbeyond.cloud/>

### Document ingestion and fine-tuning reach Qdrant on GCP — Bug fix

On GCP, Qdrant runs on Cloud Run behind IAM and its own API key. The doc-pipeline and finetune
jobs connected with neither, and on port 6333, which Cloud Run does not serve, so uploaded
documents never reached the vector store and fine-tuning found no training data. Both jobs now
connect the way the proxy does: with the Qdrant API key (the GCP deploy mounts the
`qdrant-api-key` secret on both jobs), a Cloud Run identity token, and port 443. The
doc-pipeline image also installs `tiktoken`, which it imported without installing.

### A withheld answer is now recorded at what the provider billed — Bug fix

When G30 checks responses in block mode, or G11 blocks an answer that does not match the
requested schema, the answer the provider already produced and billed is replaced with a refusal
that reports zero usage. G18 priced that refusal, so the request was recorded as costing $0 with a
100% token saving, the spend cap was not charged, and cost reports under-counted. The call is now
recorded at the provider's usage; the caller still receives the refusal unchanged.

### A tenant's model choice for a routing tier now takes effect — Bug fix

In the portal's model preferences a tenant picks the model for each routing tier (simple,
medium, complex), and the portal shows that pick as in effect. With the per-provider ladders the
shipped config enables (`tiers_by_provider`), G06 used the ladder for the requested model's
provider and ignored the pick. Now a tier the tenant set wins for all of that tenant's requests,
including a request for another provider's model, and tiers it did not set keep the ladder. The
same holds for tiers an operator sets for one tenant under `tenants.<id>`. Picks already saved
take effect with this release.

The configuration reference now states the precedence the proxy applies: an operator's
`tenants.<id>` block is merged after the portal's settings and wins where both set a key.

### Blank lines in chat history no longer stall G01 — Bug fix

G01, on by default, checks assistant history for log output whenever LLMLingua does not
shorten a message. One of its patterns, for Java stack frames, let a frame's indentation include
newlines, so on a long run of blank lines its time grew with the square of the length. It ran in
the event loop, so one crafted request could hold up every other request on that worker for
seconds or longer. A stack frame's indentation is now spaces and tabs only, which is linear and
still matches real stack traces; a test times every log pattern on crafted inputs.

## 2026-09-27

### G01 no longer hands the model a corrupted copy of its own code — Bug fix

G01, on by default, sent every assistant message in the history over 100 characters to the
LLMLingua-2 sidecar, which keeps about half the word tokens. Log and error text went to Kompress,
which rewrites it with a seq2seq model. Neither can tell a function name or a URL path from
filler, and the only check looked for dropped negations. In a coding conversation the model could
get its own earlier code back with names or paths missing, and edit that version.
- A message with a fenced code block is no longer sent to LLMLingua, Kompress or Selective Context.
- A compression that changes or moves inline code, a URL, a file path, an identifier or a version
  number is refused, and the original is sent. The check covers every compressor, including the
  cache-floor re-compression.
- The patterns that find those parts now run in linear time. Two of them took quadratic time on
  crafted input, and G08 already runs them by default on client-supplied tool descriptions.

### The GCP deploy script builds the doc-pipeline image again — Bug fix

Since the doc-pipeline image started bundling the proxy's guardrails engine (for opt-in
`INGEST_PII_MODE` masking at ingest), its Dockerfile copies a folder that only
`ci/cloudbuild.yaml` created. `scripts/gcp/gcp-deploy.sh` and `ci/cloudbuild-images-only.yaml`
never did, so from a fresh clone the doc-pipeline build failed and the deploy stopped, with the
proxy and LLMLingua images already pushed.
- The new `scripts/ci/stage-doc-pipeline-guardrails.sh` stages exactly the three files the image
  needs, starting from an empty folder each time. Every build path runs it first.
- To build the image by hand, run that script before `docker build src/doc-pipeline`.

### A database that is down when the proxy starts no longer leaves billing, the audit log and tenant settings off — Bug fix

With `DATABASE_URL` set, the proxy uses the database to record usage, write the security audit
log (G29–G32 events) and apply tenant settings made in the portal. It connected once, at
startup. If the database was briefly unreachable then (a failover, a connection limit), the
proxy logged a warning and served every request unbilled, unaudited and without those tenant
settings until it was restarted.
- The proxy now keeps retrying, 1 s apart at first and doubling to 30 s, until all three are
  wired. Each is wired on its own, so one failing schema step no longer holds back the others.
- Meanwhile `/health` answers `"status": "degraded"`, still with HTTP 200, and lists what is
  missing under `not_wired`. The error itself goes only to the log.
- With `MANAGED_DEPLOY=true`, startup waits for the database instead, so the proxy accepts no
  request until all three are wired.

### Deploying the managed service no longer takes the proxy off its managed settings — Bug fix [Enterprise]

A managed-service deploy first ran the base GCP deploy, which redeployed the proxy on the
open-source image and replaced all of its environment and secrets. The managed step that put
them back came near the end, after the image and portal builds. Until then portal-issued keys
were refused and requests were not metered, and if a step in between failed the proxy stayed
that way.
- The base deploy (`scripts/gcp/gcp-deploy.sh`) now takes `SKIP_PROXY_DEPLOY=true` from a
  wrapper that deploys the proxy itself. A running proxy then keeps its image and settings, and
  only the base deploy's own environment variables and secrets are merged in.
- The Cloud Build deploy step refuses to replace a proxy running the managed image.

Self-hosters have nothing to upgrade — <https://tokenlean.cbeyond.cloud/>

### G08 no longer adds a Redis entry for every tool on every request — Bug fix

G08, on by default, added a sorted-set entry for every tool on every request. The entries were
kept 90 days and read by nothing, and each tool cost about six sequential Redis round trips. Its
per-tool record never expired, and tool names chosen by the caller became Redis keys. Under
agent traffic Redis filled up. Depending on its eviction policy it then either evicted other
keys, including the spend-cap, quota and rate-limit counters, or refused writes.
- G08 now keeps only a last-used time and a call count, and only for tools the registry or an
  MCP manifest names.
- They are written in one pipelined call per request and expire after `tool_usage_ttl_days`.
- The pruned-status check is one call per request.

The sorted sets written before this release (`*tok_opt:tool:usage:<tool>`, without `:meta`)
expire within 90 days, or can be deleted now.

### Chats that send a workflow id no longer switch to terse JSON answers — Bug fix

G17 loop control, on by default, kept a 10,000-token budget per `workflow_id` per hour. Each turn
it subtracted the whole prompt, history included, so an ordinary chat ran the budget out within
a few turns. Every conversation sending the same workflow id drew on the same total. From then
on the system prompt began "[BUDGET] … Respond ONLY with required JSON fields", which turned prose
answers into terse JSON; changing the start of the prompt also broke the provider's prompt
caching.
- The budget is now measured against each conversation's own prompt size.
- The brief-answer instruction is opt-in (`G17_loop.compact_output_enabled`, default off).
- It now asks only for a brief answer, not JSON, and is added at the end of the system prompt.

### A repeated question is no longer deleted from the conversation — Bug fix

G22 deduplication, on by default, replaced two near-identical consecutive turns with the text
`[summarised: 2 similar turns]`. When a user double-sent "Can you check order 1234?", the model
received no question and no order number, and a saving was recorded. G22 now keeps the last
turn of such a run word for word and drops only the earlier repeats. It records the saving in
tokens.

### Pasted code keeps its `#include` lines, and JSON keeps its null values — Bug fix

G19, on by default, deleted every line starting with `#` in code sent in a request: C
`#include` and `#define`, Rust `#[derive]`, shebangs. It also cut lines at ` #` or ` //`,
turning CSS `color: #fff;` into `color:` and Python `a // b` into `a`. The model then answered
about code the user never wrote. Comments are now stripped only from a fenced block that
names its language, using that language's comment marker. Unlabelled code keeps its comments.
G19's JSON cleanup also dropped `null` and `""` fields, which do not mean the same as absent
ones. It now keeps them and drops only empty lists and objects.

## 2026-09-26

### A system prompt that mentions template syntax is no longer replaced — Bug fix

G01's layered composition, on by default, replaced any system prompt containing `{{` and `}}`
with the generic layers from the config ("You are a helpful AI assistant…"), so a prompt that
mentions Handlebars or Jinja fields lost all of the developer's instructions, and nothing
recorded it. Composition is now off by default and applies only to a system message that asks
for it with a `layer_context` object. G01 removes that field before the provider call and
records each composition as a savings step. The layers also follow the current config, rather
than the first request's.

### A system message sent as a list of parts no longer fails the request — Bug fix

A system message whose content is a list of parts (a valid OpenAI shape) made G12's reasoning
suppression prompt (on by default) and G11's verbosity steering fail with a 500. Both append
their text as an extra text part now, which leaves any `cache_control` marker on the earlier
parts where it was.

### Streaming OpenAI clients get a stream on a cache hit or a guardrail block — Bug fix

An OpenAI-SDK client that asked for a stream (`stream=true`) got a plain JSON body whenever the
proxy answered without calling the model: a cache hit, a G04 bypass, a G29/G30/G31 block or an
F2 agent answer. The SDK found no stream events, so the user saw an empty reply (a block's
refusal text was lost too), and the request was still billed as served. These answers are now
sent as a stream: one chunk with the answer, then the usage chunk, then `[DONE]`. As on a live
stream, the usage chunk is left out when the client's `stream_options` did not ask for it. The
Anthropic and Gemini routes already did this.

### The response cache no longer serves one request's answer to a different request — Bug fix

The response cache (G05) could serve a stored answer that did not fit the request:
- **Settings.** Its key ignored the settings that change the answer (`tools`, `tool_choice`,
  `response_format`, `n`, `max_tokens`, the sampling settings, `stop` and others) and the
  arguments of earlier tool calls. A request could get JSON in another schema, calls to tools
  it never offered, or the wrong number of choices. Both tiers now key on them, and on
  TokenLean's own fields that change the prompt later: retrieval (G07), session context (G10)
  and output format (G11).
- **Images.** The semantic tier matched two requests about different images as the same
  question. It now skips requests whose user turns carry images, audio or files.
- **Expiry.** Semantic-tier entries never expired. A row past `l2_ttl_seconds` is now never
  served, and the store deletes expired rows, whether or not `retention` is enabled.

After upgrading, requests that set any of these parameters get new cache keys, so their entries
are rebuilt once.

### A batched request is queued only when it will be answered, and keeps its output checks — Bug fix

A request with any `batch_topic` got a 202 and was billed even when no consumer read that topic
(the default config runs none) or the queue write had failed, so its result stayed "pending"
forever. It is now batched only when the topic is listed in `G13_batch.batch_topics` and the
write succeeded; otherwise it is answered at once. A topic's queue stops taking requests at
`max_backlog` (default 10000), and processed entries are deleted. Batched results also skipped
the response-side checks: G29's masking of PII in the model's output (and restoring the
caller's own masked values) and G30's response scan. Requests from tenants with either on are
no longer batched.

### The Observability tab and the FinOps anomaly panel load again — Bug fix [Enterprise]

With the proxy's keys stored in Postgres (the managed setup), the operator console's
Observability tab failed on every load: it read the key store on the server's event loop,
which that store refuses because the read would block it. It now reads it in a worker thread,
like every other admin page. The portal's FinOps cost-anomaly panel also failed on every
call, from a missing import. Self-hosters have nothing to upgrade —
<https://tokenlean.cbeyond.cloud/>

### The database can stop the application from changing audit rows — Enhancement (OSS + Enterprise)

The audit log was protected only by the application's own code: its database role owned
`audit_events` and could rewrite or delete any row. `python -m audit.enforcement`, run as the
tables' owner, now creates a restricted role for the proxy: it can read and insert audit rows,
changes them only through two database functions (right-to-erasure pseudonymisation, and
retention that never deletes a row younger than 90 days), and cannot regain the owner's rights.
Startup schema steps skip tables the role does not own. See `retention` in
docs/config-reference.md.

- **[Enterprise]:** every managed deploy runs a schema job that creates the role, and the SOC2
  evidence pack reports, checked live, whether the protection holds; the proxy moves onto the
  role once verified — <https://tokenlean.cbeyond.cloud/>

### The SOC2 evidence pack attests only the audit controls that are in place — Bug fix [Enterprise]

The pack's audit statements said `audit_events` was append-only at the database level and
that every served request wrote an audit row. Neither holds: the application's database
role can change and delete audit rows, and ordinary requests write none. The statements now
say what the audit log records, name the only two code paths that change rows
(right-to-erasure pseudonymisation and the optional retention job), and list exactly which
fields the pack's integrity hash covers; a test ties each statement to the code. The
open-source audit engine's comments now say the same. Self-hosters have nothing to
upgrade — <https://tokenlean.cbeyond.cloud/>

### The managed service's image no longer downgrades its encryption library — Bug fix [Enterprise]

The commercial proxy image installed the core requirements, which pin the patched
`cryptography` 50.0.0, and then ran a second install capped at `<46`, so pip replaced 50.0.0
with 45.0.7, a release with seven published advisories. That library encrypts tenants' own
provider keys. The image now keeps the core pin, and a test fails any image install whose
version range would replace a pinned package. Self-hosters have nothing to upgrade: the
open-source image was never affected — <https://tokenlean.cbeyond.cloud/>

### The compression and document-extraction services on GCP accept only the proxy — Bug fix

`gcp-deploy.sh` deployed `llmlingua-svc` (G01 compression) and `tika-svc` (document
extraction) as public services that took calls from anyone, running as the proxy's service
account. Anyone could flood them (one instance each) or send Tika crafted files, under an
identity that can read the project's secrets. Both now require Cloud Run IAM and run as their
own service accounts with no roles; the proxy and the doc-pipeline job send an identity token,
and the deploy stops if either service is still public. The compression service also refuses
text over 200,000 characters (`LLMLINGUA_MAX_TEXT_CHARS`), and `post-deploy-check.sh` fails a
sidecar that answers an anonymous call. Redeploy to apply. Under Docker Compose only the text
limit changes.

### Erasing or offboarding a tenant no longer leaves its webhooks and documents behind — Bug fix [Enterprise]

The GDPR erase and the admin offboard left a tenant's webhook endpoints (URLs and signing
secrets) and its uploaded documents in place, and passed over any step that failed, such as
an unreachable Qdrant or a denied delete. Offboarding then released the company code, so a
new company given that code got the same tenant id and inherited what remained, including
the old webhook URLs, which were sent the new tenant's events. Both now also delete webhook
endpoints and every version of the tenant's uploaded documents (offboard also removes the
bucket and the contract record), and report a failed step: the erase answers 503 and the
offboard keeps the code reserved until a retry completes. The summary also states what is
kept: invoices (legal retention), request traces for now, and deleted document versions for
the bucket's soft-delete period. Self-hosters have nothing to upgrade —
<https://tokenlean.cbeyond.cloud/>

### One request can no longer stall the proxy through a regular expression — Bug fix

Two regular expressions could hold the event loop, which every tenant on a worker shares,
for minutes. G29's email pattern was quadratic: one request of `a.` repeated to a few
hundred KB stalled a worker. G06 routing-rule patterns, which a tenant writes, ran on
Python's backtracking engine, so a rule such as `^(\w+\s?)+$` could freeze a worker. The
email pattern now caps an address's parts at their RFC maximums, which makes the scan linear
(400 KB of hostile input: under 0.1 s), and skips text without an `@`. Rule patterns run on
the `regex` engine within one 50 ms budget per request; a pattern that runs out counts as no
match, and is logged and counted (`token_opt_g06_rule_pattern_timeouts_total`).
- **OSS:** both fixes, in the core engines.
- **[Enterprise]:** the portal's Routing tab also refuses to save a pattern that repeats a
  repeated group, such as `(a+)+` — <https://tokenlean.cbeyond.cloud/>

### Streamed calls are always priced, and priced like any other call — Bug fix

A streamed call was priced from the provider's final usage chunk by a separate copy of the
cost code. A client that sent `stream_options: {"include_usage": false}`, or disconnected
before that chunk, left the call at $0: outside the spend cap, with no tokens on its usage
row. Streams also skipped paid side calls (such as G06's judge) and the reasoning surcharge,
recorded the prompt on a different basis, and moved none of the token or cost metrics, so
budget alerts missed streaming traffic. The usage chunk is now always requested (a client
that opted out still does not receive it); a stream without one is priced from an estimate
and recorded like any unreported usage (`provider_prompt_tokens` empty); and streams go
through the same pricing and metrics as other calls. New counters:
`token_opt_stream_usage_estimated_total`, `token_opt_stream_accounting_errors_total`.

### A caller can no longer choose the address the IP allowlist and login limits see — Bug fix

The source-IP allowlist and the portal's login, signup and password-reset limits took the
caller's address from the first `X-Forwarded-For` entry, which the caller writes. A leaked
key plus `X-Forwarded-For: <an allowlisted address>` got past the tenant's allowlist, and a
new value on each request escaped the per-IP login limit. The address is now the entry
`network.trusted_proxy_hops` places from the right (`auto`: 1 on Cloud Run, else 0). If a
proxy other than Cloud Run fronts TokenLean, set it (`docs/config-reference.md`);
`ip_allowlist.trust_x_forwarded_for` is deprecated.
- **OSS:** the right-counted parser, the `network` settings, forwarding headers dropped
  before they reach request parameters, and optional vouching by a trusted forwarder with a
  Google-signed ID token.
- **[Enterprise]:** the portal vouches for the browser's address with its own service
  account's ID token; a successful login no longer resets the per-IP count; IPv6 callers
  count per /64. Managed deploys start in observe mode (log only) until a live Cloud Run
  check confirms the header shape — <https://tokenlean.cbeyond.cloud/>

### A suspended or revoked key stops working on every instance, not just one — Bug fix

With several proxy instances, a key change reached only the instance that made it. The
others kept accepting a suspended or revoked key until their next cache reload: up to 30
seconds on the Postgres key store, and up to `KEY_CACHE_TTL_SECONDS` (default 300) on the
Secret Manager or file store.
- **OSS:** every key change is now reported to a new `on_change` hook on
  `install_key_store_backend`, and the Postgres store's new `CacheRefresher` re-reads one
  tenant the moment it is told to. The Secret Manager and file stores are unchanged;
  `DEPLOYMENT.md` now states their delay and how to shorten it.
- **[Enterprise]:** the managed service announces each change over Redis, and the other
  instances apply it within milliseconds — <https://tokenlean.cbeyond.cloud/>

## 2026-09-25

### The OpenAI-compatible endpoint accepts only documented request fields — Bug fix

`/v1/chat/completions` used to copy every field of the request body into the request it
builds for the provider. It now keeps only the documented Chat Completions parameters,
TokenLean's own client parameters (every `x_*` field, plus `workflow_id`, `template_id`,
`rag_query`, `session_id` and the rest), and any names listed in the new
`ingress.extra_allowed_params` setting. Other fields are dropped and their names logged,
never their values. Fields starting with `_` are never accepted, and litellm call arguments
such as `api_base`, `extra_headers` and `extra_body` are also stripped before every provider
call. A client that sent one of those in the JSON body loses it; list any other dropped name
in `ingress.extra_allowed_params` to keep it.

### An agent's API key can only come from operator config — Bug fix

An F2 agent's `api_key_env` names the server environment variable that holds its API key.
It is now honoured only when the operator's config defines an agent with the same `id`,
`url` and `api_key_env`, either in `orchestration.agents` or under a static
`tenants.<id>.orchestration`. Agents that arrive through tenant overrides are called
without a key, and a refused name is logged, never its value. The Enterprise agent console
no longer stores `api_key_env`: saving an agent that carries one succeeds, drops the field
and returns a warning.

### The GCP config bucket no longer deletes the live config — Bug fix

The Terraform config bucket (`infra/main.tf`) had one lifecycle rule: delete every object
90 days after upload. That included the live `config/config.yaml` and
`config/local-keys.json`, which are uploaded only on deploy — so a deployment left alone for
90 days lost them, and the next cold start served with an empty config. The rule now
deletes only `backups/` objects older than 90 days, and a second rule removes superseded
object versions 30 days after they stop being current; a live object is never matched.
Run `terraform apply` (or redeploy) to update an existing bucket.

### The proxy refuses to start without a readable config — Bug fix

When the config could not be read at startup — a missing GCS object, a storage error, an
empty or unparsable file — the proxy logged "using last known good" and served on an empty
config: no rate card (so every invoice came out $0), no spend cap, no provider tiers. Startup
now retries twice, then stops with an error naming the source it tried, so the failure is
visible (a failed instance) instead of silent. Hot reload is unchanged: a failed reload keeps
the last good config. The Enterprise invoicing job likewise refuses to run without a config.
A deployment that was starting without a readable config will now fail at startup and say
where it looked.

### Key scripts no longer report a change they did not make, or wipe the key store — Bug fix

`scripts/issue-key.sh` writes only the Secret Manager key blob. It now checks which store the
deployed proxy validates keys against, and refuses to issue or revoke when that is Postgres
(the managed deploy) — where it used to report success while changing nothing the proxy
reads. Pass `--backend blob` if the service cannot be read. A failed secret read now stops
the script instead of counting as an empty store, which made the next write keep only the
new key. `scripts/generate_proxy_key.py` likewise refuses to overwrite a `local-keys.json`
it cannot read, tolerates a UTF-8 byte-order mark, and writes atomically.

### A deploy no longer brings back revoked keys or resets console key settings — Bug fix

With the Postgres key store, every deploy's key sync wrote each key in
`config/local-keys.json` back into `proxy_keys`: it reset a tenant's suspension, IP allowlist
and contract flags, and re-inserted keys that had been revoked (a revoke deletes the row and
left no record). The sync is now insert-only, and every removed key is recorded in a new
`revoked_proxy_keys` table. No writer — the sync, the one-time blob import, or a store write
from an out-of-date snapshot — can put a recorded key back. Keys revoked before this change
have no record: remove them from `local-keys.json` before your next deploy.

### One instance's key change can no longer undo another's — Bug fix

With the Postgres key store, each key change (signup, rotate, suspend, contract flag, IP
allowlist, offboard) read the whole store, then rewrote the whole table, with only a lock
inside one process between the two. When two instances wrote at about the same time, the
later write could undo the earlier one: a suspension lifted, an allowlist dropped, a new key
lost. A write that timed out was reported as failed but could still commit afterwards. Each
change is now one transaction: it takes a database lock, reads the current store inside it,
and writes only the rows that change; a write that times out is cancelled and rolls back. If
you install the Postgres store yourself, pass `transact_fn=pg_key_store.make_transact(pool,
loop)` to `install_key_store_backend`.

## 2026-09-18

### Request-header values could weaken rate limiting and inflate metrics — Bug fix

Rate limiting and the usage metrics trusted team and user values that arrive as request
headers, which any caller sets. Because each distinct value was treated as a separate
identity, a caller could stay under the per-minute and per-hour limits by varying a header
per request, and could grow the number of stored metric series without bound. Rate limits
now apply per authenticated key (tenant + key-bound principal + team) and the token bucket
is updated atomically, so limits also hold under bursts of simultaneous requests. A team
value is honoured only from a key issued as an API-gateway key (a trusted gateway that
stamps the team per request); for every other key the team is fixed. Team and feature
metric labels can be bounded to an operator allowlist, with unlisted values grouped as
`other`. Separately, the per-conversation "workflow turns" metric became a per-tenant
distribution, and the local usage-log export can no longer be steered outside its folder.

### A backup of the operator's configuration could still be committed to the public repository — Bug fix

Yesterday's fix excluded the benchmark launcher's config-backup path from version control, but
the same change renamed that backup to a per-process name the exclusion does not match — so a
leftover backup of an operator's live configuration was again one routine commit away from the
public repository. Backups now live inside the excluded folder, and a test checks each launcher's
real backup path against the ignore rules. `--restore`, which only ever looked for its own
process's backup and so never found a stranded one, now recovers any.

### The benchmark reported success on runs where requests had failed — Bug fix

The benchmark only treated a run as failed when every request failed; with one success it printed
a savings figure from the survivors and exited 0. Its quality gate left unanswered requests out of
the count and counted five tool-call records with nothing to check as passes ("36/36" was 31 real
checks), the Windows launcher exited 0 whatever the run returned, and the A/B harness did the same
after a failed request pair. A failed request now makes the run INCOMPLETE — exit code 3, no
headline figure, and the result file says so (the A/B harness exits 5).

### A cold benchmark stack was started twice, and timed before it was warm — Bug fix

The launcher started the stack, then restarted it to load the benchmark configuration, throwing
away the first warm-up. Its own warm-up could fail without stopping the run, and was too short to
reach the prompt-compression service, whose ~9 s first load then landed on a timed request. The
configuration is now applied before the stack starts, nothing is timed until a warm-up succeeds,
the compression model is loaded up front, and the proxy image is rebuilt from the checkout each
run so an out-of-date image is never measured silently.

### The Windows benchmark launcher measured whatever configuration happened to be loaded — Bug fix

`run.ps1`, the README's Windows quick start, had no configuration pin. On a fresh clone it
measured the shipped template, whose compression-service address does not resolve in the local
stack, so a Windows run could not reproduce the published figure. Both launchers now share one pin
and restore the operator's configuration on exit or Ctrl+C.

### The benchmark's quality check passed or failed at random — Bug fix

The benchmark sent no sampling temperature, so answers were sampled at the provider's default and
a few borderline records flipped between runs. Measured against the model called directly, the
direct model missed those same facts about as often as the proxy did, and at temperature 0
neither missed once in 40 tries. The prompts the proxy sent still contained every checked fact. The
benchmark now runs at temperature 0, like the project's other quality gates, and a miss on an
answer cut off by its length limit is labelled as such.

### Benchmark runs switched Langfuse tracing off — Bug fix

The benchmark's pinned configuration was built from the shipped template, which has tracing off,
so every benchmark run disabled tracing even where the operator had it on — leaving the
trace-backed dashboards empty for exactly that traffic. The operator's tracing setting now carries
over (it changes no measured number). A startup warning about a missing `traceloop` package, which
appeared on every start for a feature that is off by default, now appears only if it is enabled.

### Local deployments stalled for about 3 seconds on tool-bearing requests — Bug fix

The shipped tool-registry location points at a cloud storage bucket named by an environment
variable. With the variable unset, as on every local deployment, the proxy still searched for
cloud credentials — about 3 s, on the request path, once per worker every five minutes. A registry
location with no bucket now reads the local registry directly.

### Grafana showed healthy values in red — Bug fix

35 single-value dashboard panels declared no colour thresholds, so Grafana applied its default
(red at 80 and above): "Uptime 100%" rendered red, as did every count and total over 80. Uptime now
uses the same bands as the error-rate panel (green at 99.5% and above, red below 99%), and totals
get a neutral colour. A test fails any future panel that colours by value without thresholds.

## 2026-09-17

### A stray local file could carry an operator's live configuration into the public repository — Bug fix

The benchmark launcher backs up an operator's configuration file to a fixed, predictable
path before temporarily pinning a benchmark-specific config, and restores it afterward.
That backup path was never added to the list of files excluded from version control, so
if a run were interrupted before cleanup — or simply left the file behind — a routine
`git add` of the whole tree would stage it, carrying the operator's actual configuration
into the next push to the public repository. The path is now excluded.

### Two benchmark runs started at the same time could corrupt each other's configuration backup — Bug fix

The same launcher's config backup used one fixed filename regardless of which process
created it, so two overlapping runs — say, one kicked off before a prior run had finished
cleaning up — would each try to back up to and restore from the same file, corrupting
whichever run restored second. The backup path is now unique per process, and recovery
after an interrupted run now looks for any leftover backup rather than one exact name.

### A cascade routing error could leave a disclosed budget increase describing a tier that was never called — Bug fix

Cascade routing can escalate a request through progressively larger models, and calling a
tier's model can itself set the flag that discloses a raised output budget as a side
effect of preparing that tier's call. If that call then failed and the proxy fell back to
its normal, non-cascade path, the flag from the failed tier attempt was never cleared —
so a request that cascade never actually served could still carry a disclosure describing
a budget raised for a model it was never sent to. The fallback path now clears it before
the normal call re-sets it correctly for the model that actually answers.

### The new Windows run-lock liveness check could misread a 64-bit process handle — Bug fix

The Windows-specific process-liveness check added earlier today calls three low-level OS
functions directly, and left their expected argument and return types unspecified. Without
that declaration, the interface used to make the call can default to assumptions that
don't hold for every value it passes or receives — including the handle these functions
use to identify the process being checked — on some platform configurations. The three
function signatures are now declared explicitly so the call behaves the same everywhere
this harness runs.

### An internal quality check could grade an answer's facts and its tool calls by different rules — Bug fix

When a request has no recorded ground truth to compare against, the check that verifies
facts survived an optimisation correctly treats the missing baseline as nothing to check —
but the equivalent check for tool calls did not, and would fail a request for missing
tools even when there was no ground truth for tools to be measured against either. Both
now apply the same rule: no recorded baseline means nothing is checked, consistently,
for both facts and tool calls on the same request.

### The backlog tracker's rebuild script could be silently run unguarded — Bug fix

The script that regenerates the deferred-work tracker file protects several invariants —
no duplicate item numbers, no lost content, every item left in a valid state — entirely
through `assert` statements. Assertions are a language feature that can be turned off
wholesale for an entire run, which would silently remove every one of those protections
from a script whose whole job is to rewrite a shared tracking file safely. They are now
ordinary checks that stop the script with a clear error regardless of how it's invoked.

### An offline re-run of the ablation harness could apply a dataset's config override to the wrong arm — Bug fix

A per-dataset config override is meant to reshape only that dataset's own measurement
arms. On the harness's offline re-aggregation path (not the one behind any published
number), it was applied one step too late, after each optimisation's arm had already
been isolated — so an override touching a DIFFERENT optimisation than the one being
measured could leak into every other optimisation's arm for that dataset. Reordered to
match the live path, which was already correct.

### The internal quality gate could call an agentic answer empty when it was not — Bug fix

The check that excuses a tool-call answer from being classified as an empty response
looked only at one signal (the reported finish reason), when a provider can report a
tool call under a different signal (an ordinary "stop") while still attaching the
tool call itself. Such an answer is now recognised either way, matching how the
proxy's own routing logic already reads the same signal pair.

### A per-optimisation quality check skipped tool-using datasets entirely — Bug fix

Ablation runs grade each individual optimisation's own answers so a technique cannot
be credited with savings it bought by giving a wrong answer — but a dataset whose
correctness criteria live entirely in tool calls (rather than prose) was skipped by
that check altogether, so its per-optimisation figures were never quality-verified.
Fixed to check both kinds of ground truth, at no added cost (still zero model calls).

### A quality check that verified nothing could print as if it had passed — Bug fix

Where an agentic answer carries no text to check, the internal quality report could
read as a clean pass even though nothing was actually verified for that request. Such
requests are now counted and named separately, so a real pass and "nothing to check"
can never be printed identically.

### A reporting script could silently overwrite the quality-gated results file — Bug fix

Run the documented way, the harness's investor-report script wrote its own summary to
the exact same filename the quality gate uses for its pass/fail verdict and evidence —
replacing the one file that says whether a number is publishable with one that cannot
answer that question. The two reports now use distinct filenames and can never
collide.

### A Windows-only defect made the harness run-lock unrecoverable after a crash — Bug fix

The lock that stops two live ablation runs from corrupting each other's config checks
whether its recorded owner process is still alive before treating an old lock as stale.
That check is a single `os.kill(pid, 0)`, whose POSIX contract — raise a specific,
distinguishable error for a pid that no longer exists — does not hold on Windows: a dead
pid there raises a plain, generic OS error, indistinguishable by type from "something went
wrong asking." The liveness check landed in the generic branch, so on the one platform this
harness ships on, a crashed owner (the exact case this exists to catch) was reported alive.
After a host crash the lock was stuck until a human deleted the file by hand, and the
launcher's per-dataset loop was silently reporting every subsequent dataset as errored
rather than surfacing the real cause. Fixed with a Windows-specific liveness check that asks
the OS directly rather than relying on an exception-type mapping that doesn't hold here.

### A cold-start timeout during local validation reported an unrelated, unexplained failure — Bug fix

The local deploy's health check sends a proxy request with a 10-second timeout; under the
script's fail-fast mode, a timeout on that one call killed the whole script before the
branch that would have named the failure, so a freshly restarted proxy that was merely slow
on its first request printed a generic failure over four other checks that had already
passed. It now captures a timeout explicitly, retries once, and only reports failure with
the real reason if the retry also times out.

### A readiness deploy-gate check sent a real vision request for a feature that ships off — Bug fix

The per-group readiness sweep learns which groups are disabled from the deployment's own
config, but it fetched that map only after every group's probe request had already been
sent — including a real vision-model call for the multimodal optimizer, which ships
disabled by default. Every readiness run was paying for one evidence-free request. The
enabled/disabled map is now fetched before any request goes out, and a disabled group's
probe is skipped rather than sent and then ignored.

### An operator narrowing which models may reason could keep leaking a reasoning parameter — Bug fix

A provider can name the specific models it allows to reason. One of the two places that
question gets asked — the one that decides whether the output budget needs extra headroom
for hidden thinking — read that list; the other, which decides whether to forward a
reasoning parameter to the provider at all, did not. An operator who narrowed the list to
protect against one thing (a model getting no output-budget protection) kept doing the
other (still telling a now-excluded model to reason) on that exact model. Both checks now
read the same list.

### A private, cross-module function name papered over a coupling that could silently break — Bug fix

The check that decides whether an answer is empty is deliberately shared between where a
response is cached and where a cached response is served back, so the two can never drift
apart — but the shared function crossed a module boundary under a private, underscore-prefixed
name, so a routine internal rename would have broken the read side at import time with
nothing pointing at why. Promoted to a public name and pinned by a new test that fails
loudly, at the coupling, if this ever happens again.

### A zero-choices response body could be cached and replayed forever — Bug fix

The guard that stops an empty answer from being cached checked for no content and no
tool calls, but treated a response with NO CHOICES AT ALL as "not empty" — the opposite
of intent. The same function gates both the cache write and the cache read, so a
malformed provider reply could be written once and served to every look-alike question
for a full TTL (up to 24 hours) with the provider never called again. It now treats a
missing or empty choices list as empty, on both the write and read side.

### The same empty-answer check could crash on a malformed response — Bug fix

Found while fixing the item above: the check indexed into the response body without
checking its shape first, so a response whose `choices` field was present but not a
list — or whose first entry was not the expected shape — raised an unhandled error that
propagated into the response pipeline, turning a safety check into a 500 on an otherwise
successful request. It now treats anything it cannot parse as empty rather than raising.

### A disclosed budget increase could describe a call that failed, not the one served — Bug fix

When a request escalates through multiple models before one of them answers, the proxy
discloses when it grew the caller's output budget so a reasoning model has room to think.
That disclosure could end up describing a LATER model that was only evaluated and then
failed, rather than the model whose answer was actually served — telling a caller their
budget was raised for reasoning that never happened. It is now committed per attempt and
restored for whichever model actually answers.

### Routing to a different provider left downstream stages reasoning about the wrong one — Bug fix

On the opt-in routing options that can send a request to a different AI provider than
the one requested, the internal provider handle used for prompt-caching alignment and
cache-cost decisions was not updated to match — it kept pointing at the originally
requested provider. Deployments not using cross-provider routing are unaffected.

## 2026-09-16

### G06 refused reasoning-model routes the provider seam already fixed for free — Bug fix

A routing guard was meant to stop the proxy sending a request to a reasoning model whose
thinking cannot fit inside the caller's output budget, which used to bill customers in full
for an empty reply. It compared the caller's CURRENT budget to what the effort needs — but a
separate mechanism raises that exact budget on the very next step, for exactly this reason.
So the guard fired on precisely the cases already handled downstream, and refused legitimate
cost-saving routes for no reason. It now tests the operator's own ceiling instead — the one
case the downstream fix genuinely cannot reach — so a caller's merely-low budget is no longer
mistaken for starvation. No behavior change for any deployment without an explicit output-
token ceiling configured below what reasoning needs, which is the default.

### The public benchmark can now measure the provider's prompt cache — Enhancement (OSS)

`ab_results.json` reported provider cache read/write as `0` on every run, and that was
structural rather than a reporting bug: on a warm repeat neither arm reaches the provider, and
the distinct items share only a ~15-token prefix — far under the ~1,024 tokens a provider needs
before it caches anything. So the benchmark could say nothing about cache cost, which is the half
of the bill token counts do not show. A new `--workload provider-cache` runs many *distinct*
questions over one long shared prefix with the proxy cache bypassed, so the calls genuinely reach
the provider. The corpus is re-assembled offline from HotpotQA paragraphs already in the repo — no
download — and each question keeps its own gold answer, so the existing facts gate applies
unchanged. Read and write are always reported together as absolute tokens per arm; the slice
carries **no savings percentage** and is excluded from the illustrative blend, because its prefix
size is a parameter we chose. A provider that discloses no cache counters is recorded as
*unreported*, never as zero.
- **OSS:** `--workload provider-cache` in `examples/benchmark/run_ab.py`, plus
  `build_provider_cache_dataset.py` and the checked-in `provider_cache_dataset.jsonl`.

**First run (2026-09-16, `gpt-4o-mini`, $0.046):** OpenAI served **91.0%** of the direct arm's
prompt from its cache and **90.6%** of the proxy arm's — so the proxy does not break the
provider's prefix cache, while also sending 2.5% fewer tokens. The write half is a different
story: OpenAI publishes a read counter and **no write counter**, so that column now reports
`n/r` rather than `0` — a confident zero about a provider's billing is a claim we cannot
support. Anthropic does publish it, so the write half becomes measurable when this workload is
run against that provider.

## 2026-09-11

### The internal harness scored a rephrasing as a dropped fact, like the public one did — Bug fix

The quality gate that decides which datasets enter the published savings figure matched each
expected fact as a literal substring, so an answer that reordered the same words failed it. The
gate is relative to an unoptimised baseline, which is where this does the most damage: the
baseline writes the phrase one way, an optimisation changes the wording rather than the content,
and a fact plainly present is recorded as lost. The public benchmark measured the same defect in
its own copy of the gate yesterday, where it produced every one of that run's ten recorded
regressions. A multi-word fact now gets one narrow second chance, requiring every content word
inside a single sentence, contiguous apart from filler, with no negation. Single-word facts and
file paths still require an exact match, and forbidden terms are untouched. A new test runs the
same cases through both harnesses and fails if their verdicts ever disagree. **The published
figure was minted before this fix and is therefore a floor; it is marked pending re-measurement
rather than restated.**

### A killed benchmark run could silently eat your proxy configuration — Bug fix

The benchmark launcher backs up the configuration, pins its own, and restores on exit. A run that
is killed never reaches the restore, so the pinned configuration stayed in place — and because the
backup went to a temporary file, the next run then backed that pinned configuration up as though
it were the original and faithfully restored it. One interrupted run was enough to lose a local
setting permanently, with nothing reporting it. The backup now goes to a fixed path, a leftover
backup is recovered before anything else happens, interrupt and terminate signals are handled as
well as normal exit, and `--restore` puts a stranded configuration back without needing the stack
to be running. The Windows launcher has no pin step and was never affected.

### A drift guard was comparing against a local file instead of what ships — Bug fix

The check that stops the measurement harness from being tuned differently to the product read its
reference from an untracked configuration file that every machine and continuous integration
runner creates differently. It could fail for reasons unconnected to the harness, which is how it
behaved when the bug above clobbered a local value, and it would equally have passed in silence on
any machine whose local file happened to agree. It now reads the tracked template an operator
actually deploys from. The one genuine difference this surfaced, tracing being on for the harness
stack and off by default, is recorded as a declared exception with its reason.

## 2026-09-10

### The A/B benchmark's facts gate scored a rephrasing as a dropped fact — Bug fix

The gate matched an expected fact as a literal substring, so an answer that reordered the same
words failed it. On the checked-in calibrated run this produced ten recorded quality regressions,
every one of them the same item: the expected fact was "Donald J. Trump's private jet" and the
proxy arm wrote "the private jet of Donald J. Trump". It fires hardest on the arm whose phrasing
an optimisation changed, which is the arm being measured. A multi-word fact now gets one narrow
second chance: every content word must appear, inside a single sentence, within a bounded window,
with no negation in that sentence. Single-word facts, including every numeric answer, still
require an exact match, and forbidden terms are unchanged. Re-grading the shipped artifact turns
10 regressions into 0. **This raises measured quality, and therefore may raise published savings,
so the affected figures are marked awaiting re-calibration rather than left in place.**

### One quality verdict counted nine extra times in the warm-cache burst — Bug fix

A warm repeat replays a request byte-identically: the direct arm is memoised and the proxy arm is a
cache hit, so both arms grade the very same pair of strings. Each replay was counted as an
independent verdict, multiplying a single item's result by the burst multiplicity — which is how
one item produced a tally of ten. Verdicts are now counted once per item; paraphrases, which change
the prompt, are still counted independently, and the per-call evidence list is unchanged so nothing
becomes harder to diagnose. Token and cost accounting still covers every call.

### The agentic benchmark's tool results were too small to optimise — Enhancement (OSS)

Each mocked tool result was a twelve-token status stub, so the request-side pruning lever had
nothing to act on and the agentic slice measured tool-catalogue pruning alone. Real agents get back
API responses and query results, and those re-enter the prompt on every later turn. Results are now
generated deterministically from each tool's own schema and capped to the size band the internal
agentic dataset uses for the same job. The tool catalogue, the questions and the turn structure are
byte-identical, so the catalogue-pruning figure cannot move for an unrelated reason. The upstream
benchmark ships no result payloads, so these are ours either way; that is stated in the data
licences file, and the cap is what stops realism becoming inflation.
- **OSS:** `build_agentic_dataset.py --results-only` regenerates them offline, with no download.

### Prompt compression could not fire anywhere in the public A/B benchmark — Enhancement (OSS)

Compression only touches assistant messages unless the caller opts in, and every benchmark item is
system plus user, so the lever was inert across all 125 items while the launcher enabled it and the
documentation counted it among the techniques the benchmark proves. Because the opt-in carries a
quality risk in production, the fix is not to switch it on: the prose figure is now reported
two-sided. A default run measures what a stock install gets; `--compress-user` measures what the
documented opt-in gets, on the prose profiles only. Neither side may be published alone, the result
file records which side it holds, and the run exits non-zero if compression was requested but never
fired, so a sidecar outage cannot be mistaken for a measurement.
- **OSS:** measured live on 2026-09-10, the opt-in moved the combined prose lever from 8% to 9%
  and **dropped two facts the default side kept** (a retry count and an out-of-memory cause). It
  also made the structured-payload profile slightly worse, 44.0% to 42.8%. On this corpus the
  opt-in is a bad trade, which is consistent with it shipping off — and is exactly why both sides
  are published rather than the flattering one.

### Two benchmark profiles were silently missing from the published blend — Bug fix

The illustrative blend drew its prose lever from three profiles and its reasoning lever from one,
which left the code and repository-issue profiles — 35 of 110 items — run, billed, graded and then
omitted, with nothing in either document saying so. The lever membership and the exclusions now sit
in one place with a stated reason for each, and a test fails if any profile belongs to neither.

## 2026-09-09

### G02's system-prompt truncation removed — it rewrote answers; G26 owns budget compaction — Bug fix

The template registry had an opt-in `budget.truncate_enabled` path that cut the tail off the caller's
system prompt until the request fit a registered template's token budget, with no faithfulness check.
Measured on a real enterprise-support workload it deleted 691 characters of policy text per request and
changed billed answers: a refund reply stopped naming the disputed amount, an SLA reply stopped naming
the breach. The path is deleted, not guarded: G02 looks the template up, tracks token history, blocks
sunset templates and WARNS when a request exceeds its budget, then forwards the request unchanged.
Runtime protection for a prompt that outgrows the window is G26's job; template budgets are enforced at
build time. Default installs were unaffected (`truncate_enabled` shipped `false`); the knobs are removed.
Direction: this LOWERS the published headline twice — G02's ablation dataset now saves ~0% and, no longer
failing its quality gate, enters the PASS-only blend at ~0%. G02 is no longer a scoreable savings group.

### The savings harness sampled the cache datasets down to almost no repeats — Bug fix

The 2026-09-07 fix that made every ablation arm gradeable reserved every request carrying curated facts.
On the cache datasets those are the requests that are NOT repeated, so reserving them all crowded the
repeats out: one dataset's sampled repeat rate fell from 83% to 33%, and the measured cache workload
fell from 55.9% to 11.9% with the proxy byte-identical. The reservation is now capped at what the
dataset's own repeat rate leaves, repeated requests are reserved together with their repeats, and the
sample is enlarged when both cannot fit, so every arm is still graded (never fewer than three checked
answers) and the sample still looks like the dataset it came from. The quality gate now fills its graded
window with fact-covered answers first. Direction: this RAISES the published cache figure back to its
workload's shape and grades more answers, which can only lower a verdict; served behaviour is unchanged.
The cache figure is the hit share of a repeated-question workload, not a general traffic figure.

### G25 no longer raises reasoning effort above the provider default — Bug fix
- Adaptive Reasoning shipped with `effort_ceiling: high`, so a request its keyword classifier
  judged complex was sent to a reasoning model at **high** effort — above the effort the caller
  would otherwise have been served. Measured on a reasoning benchmark: identical prompts,
  **2.7x the reasoning tokens** and **+37.8% output tokens**, while the arm's own answer-quality
  gate passed 30/30. The escalation cost money and bought no checked fact.
- The ceiling now defaults to `medium`, the provider's own default, making G25 non-increasing:
  it can lower reasoning effort but never raise it. Operators who want escalation set
  `effort_ceiling: high` deliberately. Changed in the shipped config **and** the code default,
  so a deployment with an older config file does not keep escalating.
- The ceiling is now enforced **per provider**, not just per config: G25 asks the routed provider what
  effort it serves by default and never selects above it. Where extended thinking is opt-in, the previous
  default did not lower a bill - it turned thinking on and raised one. Those customers pay less; OpenAI
  behaviour is unchanged. Escalation is now an explicit `escalate_above_provider_default: true`.
- A malformed `effort_ceiling` (a typo, a null, or a bare `off`, which YAML reads as false) used to fail
  OPEN to `high`, the most expensive setting the group can select. It now falls back to `medium` and warns.
- No change to the published savings headline FROM THE LAST MINT, where the group had no effect
  in any `all-on` arm because the reasoning models were unreachable (see the `o4` prefix item
  above). Once they are reachable the ceiling is what stops effort being escalated. Customers on
  defaults pay less on reasoning-model traffic.

### G12's budget prompt no longer tells the model to skip steps — Bug fix
- The reasoning-budget instruction injected at `medium` said "Keep reasoning minimal. One brief
  step max, then final answer." On a graded reasoning benchmark it dropped the named subject of
  the question in **4 of 30** checks — one request in all three repeats — while saving 49% of
  reasoning tokens. A budget that costs a required fact is not a saving.
- The `low` and `medium` texts now bound how much the model NARRATES, never what the answer
  must contain ("Do not skip any step a correct answer requires").
- Direction: this **lowers** G12's measured reasoning saving. That is the intended trade.

### Reasoning models no longer silently downgraded when their prefix is unlisted — Bug fix
- A model name matching no `providers[].model_prefixes` is treated as unknown, and G06's
  disabled/no-ladder path replaces it with `default_model`. The `o4` prefix was missing, so an
  `o4-mini` request was served by `gpt-4o-mini` with the reasoning parameters stripped — a
  different, cheaper model than the caller asked for, with no error anywhere.
- `o4` added to the OpenAI provider's `model_prefixes` and `tiktoken_prefixes`; a new unit test
  fails if any config advertises or routes to a model its own prefixes cannot match.
- Direction: this makes reasoning models REACHABLE where they were being swapped away, so a
  deployment that routes complex traffic to one now pays that model's reasoning cost and gets
  that model's answer. Reasoning-group measurements that had silently never run will run.

### G11's automatic max_tokens tightening cut answers mid-sentence - Bug fix
G11 capped each answer's length from the observed sizes of past answers, but every workload a
tenant runs shared one bucket of evidence (`workflow_id`/`template_id` both default to
`default`, and ordinary traffic sets neither), read as a 10-entry sliding window with only 20%
headroom - so a long-form answer was capped from short-form ones. Measured on billed 200s: 4 of
54 answers cut on the DS1 ablation and 6 of 27 probes cut on a live readiness sweep that still
reported READY. The escalation meant to recover from a bad cap aged out of that window, so the
same request was cut, raised, and cut again. Tightening now ships OFF; when enabled it caps only
from a bucket the caller identified, reads the whole retained history, allows headroom sized to
observed variance, and keeps a floor any truncation raises that no later estimate may undercut.
Readiness now blocks on any probe served a cut answer it did not ask for.
- Token savings are unchanged: the loop reduced input tokens by 0.00%; its only measurable
  effect was the missing end of the answer.

### Reasoning models could return an empty answer and bill for it in full - Bug fix
On OpenAI's o-series the model's hidden reasoning is paid for out of the SAME allowance as the
answer, so a caller who sized `max_completion_tokens` for the answer alone could have the whole
budget spent on thinking and receive an empty reply - HTTP 200, billed in full, no error anywhere.
Measured on a reasoning benchmark: 18 of 54 requests came back empty, and it was the real cause of
that dataset failing its answer-quality gate. The proxy now reserves room for the model's reasoning
at the one seam every call passes through (primary, failover and each cascade tier), refuses to
route a request onto a reasoning model whose thinking cannot fit the budget the caller set, never
stores an empty answer in the cache, and discloses when it raised the budget. An empty completion
is now counted, written to the audit log, and blocks a deployment-readiness verdict instead of
passing silently.
- Direction: this RAISES the output allowance on reasoning-model traffic, so a request that was
  returning nothing now costs slightly more and returns an answer. It lowers no published savings
  figure in our favour.
- The asynchronous batch lane was reaching the provider without going through that seam at all,
  so a batched request to a reasoning model had no headroom reserved and nothing downstream could
  detect the empty answer either. It now uses the same seam as every other call.
- **This narrows the window; it does not close it.** Where a model reasons intrinsically, asking for
  no reasoning cannot switch it off, so the smallest allowance we will provision is still finite and
  a very small budget can still be consumed before the answer starts. That is why the empty-completion
  counter, the audit row and the readiness gate ship with the fix: the remaining cases are now visible
  and blocked at deploy time instead of being billed silently. Expect the counter to be non-zero.

### An answer given as a tool call was scored as no answer at all - Bug fix
Fixing the gate that passed a dataset where neither side answered introduced the opposite error: it
treated the absence of prose as the absence of an answer, and an agentic turn answers with a tool
call, which carries no prose by design. On the re-measurement two datasets produced 36 such rows,
every one of them a tool call and none a truncation, and a dataset that had passed before failed
every check while its own tool gates passed at 3 of 3. The classification now reads the reason the
model stopped: a tool call is not an empty answer, a spent output budget still is, and an unknown
reason with no text still counts as empty so the original defect cannot return.
- Direction: this RAISES the measured figure by returning a wrongly-failed dataset to the blend. It
  does not excuse a tool-call answer from the facts check - a fact the baseline gave and the
  optimised arm dropped still fails, and that case is pinned by its own test.

### G06 credited savings to a route it did not take - Bug fix
The routing group recorded its savings step from the model it PLANNED to use, so a cascade that
escalated, or a route reverted by the cost floor, left a step crediting a model that never answered
the request. A cascade also priced only its final call, so a request that paid two or three providers
reported the cost of one. The step is now written from the model that actually served, every provider
call the proxy made is priced, and a request served by a different model than the caller asked for says
so in the response and a header instead of being substituted silently.
- Direction: reported cost savings on cascade deployments go DOWN, because they were understated by
  counting one call out of several. Nothing a customer receives changes.

### The quality gate passed a dataset in which neither answer existed - Bug fix
The answer-quality gate compares an optimised answer against an unoptimised one. When the optimised
answer was empty it was scored as having dropped no facts, provided the comparison answer was empty
too - so a request that neither side answered counted as a pass and entered the published savings
average. An empty answer is now treated as a failure to answer whatever the comparison did, and the
case where both are empty is reported as un-measurable rather than credited.
- Direction: this can only LOWER a published figure, by removing from it requests that measured nothing.

## 2026-09-07

### Quality-gate console print now agrees with the gate's own verdict — Bug fix

The harness console summary read a judge sub-report's raw `passed` field, so a prose judge that
graded 0 pairs (an agentic dataset answers in tool calls) printed `— FAIL` while the gate itself
correctly treats "nothing to assess" as not-applicable; the layer that actually decided the dataset,
`tool_judge`, was never printed. The `results.md` FAIL-evidence line had the identical latent defect.
Both now render through one shared three-state rule: a 0-pair / 0-checked layer prints `n/a` with its
reason, `tool_facts`/`tool_judge` rows print whenever present, and a FAIL names the deciding layer.
Gate logic (`passed`/`verdict`/`invalid_for_roi`) is byte-identical — reporting only, no number moves.

### Dropped the bundled image-compression library; G27 is an honest reserved slot — Bug fix

- G27 Multimodal shipped `enabled: true` and handed inline images to a bundled third-party re-encoder on
  every vision request. It could not save a billable token (this proxy counts only text parts; nothing prices
  an image), and on any byte reduction it would have recorded `bytes // 4` as a token saving, a unit the
  ledger never carries, straight into `usage_events.group_savings`. The lever and its `quality`, `min_bytes`
  and `provider` knobs are removed; G27 is a reserved slot that ships off and records nothing.
- The same library backed the G05 "L3" tier, which never executed, and a G11 hook that probed for an
  attribute the library never had; both removed, `l3_enabled`/`l3_similarity_threshold` retired. L1 and L2
  caching are unchanged.
- Three packages and roughly 38 MB leave the image with no other pin changed; docs no longer describe
  headroom APIs, an L3 tier, or AST-aware pruning that the code does not have.

### The harness never once ran adaptive reasoning — Bug fix

The ablation harness's base config had no block for the adaptive-reasoning group, so its flag fell to
the code default of off and the group ran in no arm of any run — including the dataset registered to
measure it. A shipped, on-by-default, customer-reachable group was therefore unmeasured and its answer
quality ungraded. It now mirrors the shipped defaults exactly and sits in the ablation registry,
scored on reasoning tokens (it cannot reduce input tokens by construction), registered against the two
reasoning-model datasets only. This can move the published figure in either direction.

### Ablation arms could be scored on answers nothing had checked — Bug fix

The sampler kept the requests a dataset declared essential but not the requests carrying curated
facts, the only ones the deterministic gate can grade; on the default profile one dataset graded zero
answers in every per-group arm and still reported PASS. The sampler now reserves fact-covered requests
too, and the size coercion accounts for duplicate-pair headroom, which had made the two protections
mutually exclusive. Grading more can only reveal degradation: this moves the figure down or not at all.
The extra volume is paid for out of the affected profile's own sizing: the $5 profile swaps its most
expensive dataset for a cheaper reasoning one and now estimates well under its cap rather than the cap
being raised; the $25 and $100 profiles measured under their caps unchanged.

### Ask the proxy for the prompt it actually sent — Enhancement (OSS)

The prompt is the only place an optimisation's defects are visible, and nothing kept it. A caller can now
receive the exact messages and parameters the provider got, alongside the usual savings metadata. Off by default and double-gated: an operator must allow it
and the caller must ask per request, because the sent prompt can include retrieved documents and
memories the caller never sent. Provider credentials are never included, real parameters such as the
output budget are never censored, oversized prompts are marked clipped, a cache-served request says so
instead of implying a prompt was sent, and every turn of a tool round trip is recorded, including the one
carrying tool results back to the model. Not available on streamed responses, and on the OpenAI-compatible
route only (the native Anthropic and Gemini endpoints drop it). The deploy gate now probes
it: the fail-closed default is verified on every deploy, and an opted-in deployment must echo a sanitised
request.
- **OSS:** `observability.echo_sent_prompt` (default `false`) + `max_echo_chars`; per-request
  `x_echo_prompt`. The ablation harness stores the prompt behind every graded answer.

### Compression no longer silently forfeits the provider's prefix-cache discount — Enhancement (OSS)

Providers only cache a prompt prefix above a minimum size, and they decline in silence: no read, no
write, no error. A compression that crosses that line can send far fewer tokens and still cost more,
because a large discount on a repeated prefix is lost. The guard that shipped on 2026-09-04 was
whole-prompt, all-or-nothing, G01-only and cost-blind; it is replaced. The prefix guard now measures
the span the provider actually caches, compresses it *down to* the minimum instead of abandoning the
whole compression, leaves the never-cached remainder fully compressed, and holds tokens back only when
the provider's own published cache rates and the observed reuse make that cheaper. Off by default.
It needs the compression sidecar to compress to the floor (without it the span is preserved whole), and a
provider whose prefix-cache marker is off reports no cacheable span at all, so nothing is ever held back for
a discount that cannot arrive. The reservation survives being handed from one optimisation to the next,
and if it ever becomes unmeasurable the guard holds the prompt whole rather than compressing blind. The
deploy gate probes the guard in both tiers, and the prefix-cache probe pair now carries a genuinely
cacheable prefix so a missing cache read is a real signal, not a note.
- **OSS:** `groups.G1_compression.preserve_cacheable_prefix` (semantics changed), `cacheable_prefix_margin`,
  `assumed_prefix_reuse`, `prefix_reuse_window_seconds`; `providers.<name>.min_cacheable_tokens[_by_model]`;
  the shared floor is honoured by compression, structured pruning and tool-description trimming alike.

### Deployments did not verify the Anthropic and Gemini endpoints they serve — Bug fix

Every deploy runs a readiness gate, and a NOT-READY verdict blocks it. That gate skipped the
protocol matrix, so the native Anthropic `/v1/messages` and Gemini `generateContent` endpoints —
the ones a Claude or Gemini SDK talks to — were never exercised on an ordinary deploy; they were
checked only in the deeper pre-release tier. The verdict already knew how to fail on a protocol
error, but in the quick tier it was handed a placeholder that always reported success. The two
checks now run on every deploy. They cost two 32-token calls against the roughly two dozen the
gate already makes. Note what they establish: request/response translation for each endpoint,
not the Anthropic or Gemini services themselves.

## 2026-09-06

### Prompt compression could invert the meaning of a policy it shortened — Bug fix

G1 compresses long messages in the conversation. On an HR policy answer it turned *"employees
may carry over up to 5 unused PTO days... Any PTO **exceeding this limit** is forfeited"* into
*"5 PTO days. **PTO forfeited** January 1st."* Deleting the qualifier does not lose a detail —
it turns an allowance into a blanket denial, and the model then told the user they could not
carry over any PTO at all. Digit preservation had protected the number; nothing protected the
words that bounded it. Compression is now checked before it is accepted: if a negation or a
scope-limiting qualifier present in the source is missing from the result, the compression is
declined and the original text is sent. The check covers every compression path, so a future
compressor inherits it. Measured across our benchmark corpus it declines about a quarter of
compressions and keeps roughly three quarters of the token savings — the declined ones include
*"None of these has fully resolved"* compressed to a list of things that had **not** worked.


### The published savings figure was measured with a default-off group switched on — Bug fix

Our ablation harness built its `all-on` arm — the one that produces the published savings
number — by force-enabling every optimisation in its registry, including the five that ship
**off**. G28 (context compression) was the clearest cost: it advertises two extra tool
definitions into every tool-carrying request, and on our agentic dataset that added **131
tokens to all 54 requests while the model called those tools zero times**, which is most of why
that dataset's optimised arm sent 13% *more* than its baseline. Customers were never affected —
G28 ships disabled — but the number was being measured on a configuration nobody deploys. The
arm now enables exactly what ships. Each default-off group is still measured on its own
dedicated dataset, so nothing became unmeasurable.


### Tool pruning did nothing unless you had registered your own tools — Enhancement (OSS)

G8's intent-based tool pruning only ever acted on tools listed in your `registry_path`. A tool
with no registry entry is treated as intent `default` and always kept — the safe behaviour, since
dropping a tool the caller sent would break their agent, but it means the pruning was inert on a
fresh install and nothing said so. Measured across our own benchmark datasets, two example tools
we had registered ourselves accounted for **all** of the pruning; two datasets pruned nothing at
all. Tool-description compression, which works on anyone's tools and never drops one, is now **on
by default**, so G8 contributes out of the box. The docs and the G8 row now state the registry
requirement plainly instead of implying the pruning works unconfigured.
- **OSS:** `compress_descriptions` defaults to `true`; `registry_path` documented as a
  prerequisite for intent pruning.

Code review of that change caught the code's own fallback still reading `False` — so a config
that omitted the key would have silently reverted to the old behaviour despite the docs now
saying `true`. Fixed to a named default matching the shipped value. Also fixed: a new unit test
read the gitignored, developer-local `config/config.yaml` unconditionally, which would have
failed on any clean checkout, in CI, and in the OSS gate's `git archive HEAD` tree — it now
checks that file only when present and asserts the shipped contract against the tracked
template, which is what the OSS gate actually ships.


### The deploy check could not tell whether the agent-limits optimisation worked — Bug fix

Every deployment runs a readiness gate that verifies each optimisation actually fires, and a
NOT-READY verdict blocks the deploy. The probe for the agent-architecture limits sent 12 tools
against a shipped cap of 20 and a 14-token system prompt against a 4,096-token cap — neither
threshold could trip, so the group could not act on its own probe, and it passed on the fact
that its pipeline stage had executed. That is the same "the stage ran, therefore it works"
inference removed elsewhere in this release. The probe now carries 24 tools, so pruning is a
real observable: the live gate reports a measured saving instead of a bare tick. If an operator
raises the cap past 24 the check reports "ran, no effect" with the reason rather than failing —
the cap is their setting to choose.


### The public benchmark measured a config nobody runs — its agentic figure is corrected down — Bug fix

`examples/benchmark/run.sh` pins a known-good config so results do not depend on local toggles.
One pinned value had drifted: G16's system-prompt cap was pinned to **800** tokens while the
shipped default is **4096**, and every BFCL agentic episode carries a ~1,046-token system prompt.
So the cap fired on all 15 episodes here and on none for anyone running defaults — in a harness
whose entire claim is that a skeptic can reproduce it. The pin is gone; the benchmark now runs the
shipped config. Re-measured over 7 runs, the **agentic lever is ~12% (7–22%), not ~20% (19–25%)**,
and the illustrative blend is **~32% (31–34%)** rather than ~34%. Removing the pin also widened the
spread, because the cap was a deterministic per-turn saving that damped it. A new test compares
every knob the launcher pins against `config.yaml.template`, so this class of drift cannot return
silently.


### An oversized system prompt had its END deleted to fit a token cap — Bug fix

G16 enforces a `max_system_prompt_tokens` cap. It did so with a straight tail cut, so what was
removed was the *end* of the customer's instructions — which is where closure rules, escalation
rules and "never include raw credentials in a summary" tend to live. On a real 1,510-token SRE
playbook the cut took the entire closing "Safety Constraints & Guardrails" section; the only
signal was a warning string in a response field. The cap now defaults to `system_prompt_overflow:
warn` — the prompt is passed through byte-identical and reported, never edited. Operators who
want the cap enforced set `compact`, which drops the **middle** on paragraph boundaries, keeps
the opening role and the closing policy, and marks the elision so the model is not handed a
truncated policy that looks complete. Compaction is still lossy in the middle, which is why it
is opt-in, and it now spends the budget it is given — the first cut kept whole paragraphs only, so a single large paragraph was dropped entire and left most of the budget unused, discarding far more of the prompt than the cap required. Also fixed: with the prompt split across several system messages the cap enforced
nothing at all (each message was budgeted the full cap) while the warning still claimed it had
truncated — 6,003 tokens passed a 4,096 cap untouched.


### A per-tenant setting in the operator config was ignored by most optimisations — Bug fix

Operators can tune or disable an optimisation for one tenant under `tenants.<id>.groups.*` in
`config.yaml`. That overlay was applied at read time by a helper only 9 of the 32 pipeline
stages call, so for the rest — including prompt compression and structured pruning — a
per-tenant `enabled: false` was silently ignored while the group kept running. The overlay is
now merged once at pipeline entry, so every stage sees the same effective config. The new
`GET /v1/groups` endpoint reflects that same merge (it had been reporting groups as disabled
that were still running), defaults rate limiting to off when no `enabled` key is present
(matching the rate limiter itself), and resolves the tenant exactly as traffic does — the
key is authoritative and `X-Tenant-ID` is honoured only for admin keys.

### Prompt optimisation shipped under a config key the proxy never read — Bug fix

The template carried the G20 block as `G20_prompt_optimization`; the middleware reads
`g20_prompt_optimizer`. So `enabled: true` there did nothing and every template-based
deployment ran with G20 off. The block now ships under the key that is read, with
`enabled: false` — the behaviour deployments already had. Turning it on by default is a
savings-affecting change that needs a quality proof first; set
`groups.g20_prompt_optimizer.enabled: true` to enable it now.

### Deployment readiness was checking most optimisations against the wrong request — Bug fix

Every deploy runs a readiness sweep and a failing verdict blocks it. That sweep sends one
purpose-built request per optimisation — but on a warm cache most of them were answered from
the cache, which returns before the optimisation being tested ever runs. Each one was then
passed on a counter that other requests in the same sweep had moved, so a green verdict was
not evidence about the optimisation it named. The sweep's requests now opt out of the cache
(the two that exist to prove caching still use it), and a request that short-circuits is
reported as **unverified** and blocks the deploy instead of quietly passing.

### A group turned off in config was reported as working — Bug fix

A disabled optimisation still enters its pipeline stage and returns immediately, so the
timing signal readiness watched moved whether or not the group did anything. Readiness could
only tell disabled from firing by asking the Enterprise portal, which self-hosted deployments
do not run — so on those it assumed everything was on, and a group shipped off by default
collected a clean tick on every deploy. New OSS endpoint `GET /v1/groups` reports the calling
tenant's effective on/off state for each group, resolved exactly as a live request resolves
it (operator overlay and per-tenant overrides included). It returns booleans only — no
thresholds, models or service URLs — so it is safe on the same tenant-key auth as the rest of
`/v1`. A group that is on but whose input nothing on the deployment produces is now reported
as **unreachable** rather than scored, since a failure there is not something an operator can
act on.
- **OSS:** the `/v1/groups` endpoint and the corrected readiness verdicts ship in every tier.
- **[Enterprise]:** the portal's group console remains the place to change these settings —
  <https://tokenlean.cbeyond.cloud/>

### Long text fields in a tool result were replaced by an unusable placeholder — Bug fix

When the proxy compacted a tool result, any text field of roughly 300 characters or more was
swapped for a short internal reference that nothing could resolve — so an agent asking for a
runbook, an incident summary or a log excerpt received a placeholder instead of the content,
on a request that had already been billed as successful. The result was still valid JSON and
was genuinely shorter, so neither the size check nor the validity check added earlier could
see it. Compaction now runs entirely on the proxy's own compactors, which cannot produce a
reference, and every compacted value is checked for one before it is accepted. Measured on 70
real tool payloads first: the removed library saved 55.3% of tokens against the built-in
compactor's 54.5%, so this costs about eight tenths of a point and returns the answer.

### Deployment readiness now distinguishes a group that worked from one that merely ran — Bug fix

The readiness report gave a group a clean tick when its stage executed, which is the right
answer to "did it run" and the wrong answer to the question an operator is actually asking.
The image optimiser showed a tick on a build where it ran on every request and shrank nothing.
Groups whose documented result is a token saving are now reported in three states rather than
two: worked, ran without producing that saving, and never ran. The middle state still passes,
because a no-op on a single smoke request is often legitimate, and it is never a reason to
block a deploy — it is now simply visible instead of hidden behind a tick.

The scope is deliberately narrow. A first version also treated a flat savings counter as proof
of no effect, and against a live deployment that flagged eighteen of twenty-six groups, because
only one group emits that counter there. It was reporting missing telemetry as a missing result,
which is the same mistake in a new place. It now answers only where the evidence is direct.

### Removed two unreachable modules and the docs that advertised them — Bug fix

A Kafka batch backend and a Temporal agent runtime were both present as modules but reachable
from nowhere: neither was registered in the request pipeline, neither had a flag in any shipped
config, and neither was referenced by any other code or test. The Kafka one could not have run
at all, because its client library was never a dependency. Both are deleted, and the Temporal
library goes with them — 58 MB of image for code nothing called.

The documentation was the more visible half. The configuration reference described a Kafka
batch backend with four environment variables that nothing implemented, and the README listed
Temporal in the stack, as the agent runtime, and in the orchestration table. Those are now
corrected, along with five references in the architecture diagram and a source comment pointing
at starter templates that only ever contained the LangGraph pattern. Batching runs on Redis
Streams, which is not a fallback — it is the implementation.

### Removed a second unwired module that could execute a tool without checking policy — Bug fix

A dormant tool-batching module looked up a handler by the tool name the model asked for and
ran it, without consulting the tool policy that governs every other place the proxy executes
something on a model's say-so. It was never wired into the request pipeline, its configuration
key existed in no shipped config file, and the config reference already listed it as pending
wiring — but had it ever been switched on it would have reopened a hole closed in September.
It is deleted, with its documentation references. A new test now pins the complete set of
places that dispatch a handler, so the next one cannot appear unnoticed rather than being
found by hand.

### A readiness check that could never pass on a local run now explains itself — Bug fix

The check proving one tenant's contract change cannot affect a sibling tenant creates two
probe tenants, which mints API keys into cloud secret storage. On a local run with no cloud
project, minting fails and the check reported a flat FAIL — every time, forever. It is
advisory so it never blocked a deployment, but a check that always fails is indistinguishable
from one that has just started failing for a real reason. It now separates the two: if the
probe tenants could not be created it reports as skipped and quotes the reason, while a
genuine isolation failure is still reported red, including when an error occurred alongside it.

### A dataset could be silently dropped from a savings run instead of measured — Bug fix

A dataset can mark specific requests as essential to what it measures. If the run's sample
size was smaller than that list, the run refused the dataset — and the runner then excluded
it and carried on, so a dataset marked essential-to-measure was the one thing not measured.
The sample is now enlarged to fit instead, announced with the arithmetic behind it. The first
version of this fix enlarged it to exactly the required list, which removed the duplicate
requests another guard needs to measure caching; the sample now reserves room for those too.

### Removed a documented but unwired tool-dispatch module — Bug fix

A low-level dispatch module built a request URL by pasting a model-supplied tool name straight
into the path, with no validation. It was never wired into the request pipeline, but the
README documented it as an extension point complete with a worked example, so it was code we
were inviting people to use. It is deleted, along with its README section and doc references.
The tool-dispatch path that actually runs is unaffected and keeps its existing authorisation
check. No configuration key for the removed module existed in any shipped config file.

### The temperature-0 methodology note now says which measurements it covers — Bug fix

Published savings figures are described as measured at temperature 0. That holds for the
OpenAI measurements every published number comes from, but not for Anthropic runs with
extended thinking enabled: the provider rejects any temperature but 1 once thinking is on, and
because it is the proxy that enables thinking, the adapter drops the caller's temperature
rather than fail the request. No published figure is affected, but the claim was broader than
the code. Both READMEs now scope it, and a test ties the wording to the adapter's behaviour.

### The harness test suite was covered by no gate at all — Bug fix

In September a release gate was widened after roughly 590 tests turned out to be executed by
nothing. That fix stopped one directory short: the measurement harness's own 473 tests, which
cover request sampling, cache reconciliation and the usage extractors, ran in no gate and no
CI job. It was found the same way as last time — a test had been failing for days with nothing
reporting it. The release gate now runs that suite too, and skips cleanly on a checkout that
does not include it.

## 2026-09-05

### Turning the reasoning-budget group off broke every Claude request — Bug fix

Adaptive reasoning and the reasoning-budget group are separate toggles, and the budget group
is the only thing that translates the chosen effort level into what a provider actually
understands. With adaptive reasoning on and the budget group off, the raw effort string was
forwarded to Claude, where the client library silently expanded it into a thinking budget —
so Claude then rejected the caller's own `temperature: 0` with "temperature may only be set
to 1 when thinking is enabled", and **every Claude request failed with a 502**. Observed on a
live run, not in theory. Claude and Gemini now reject that parameter outright, because both
express reasoning in their own vocabulary, so no combination of group toggles can leak it.
The caller's temperature is left untouched, and an explicitly requested thinking budget still
works.


### An internal reasoning tier name could reach the provider — Bug fix

Adaptive reasoning writes the chosen effort level for the reasoning-budget group to
consume, and the two groups can be switched on and off independently. With adaptive
reasoning enabled and the budget group disabled, nothing cleared the internal `off` value
and it was forwarded to the provider — which either rejects it or expands it back into a
thinking budget, i.e. turns reasoning ON for a request that had asked for none. The
outgoing-parameter build now strips it, so no combination of the two groups can leak an
internal tier name. Found by reasoning about an untested configuration before running it,
rather than from a failure.


### The ROI harness reported zero reasoning tokens on every run — Bug fix

The per-run aggregate summed a `reasoning_tokens` field that no request record ever
carried, so every arm of every measurement reported zero — including a run whose full-stack
Anthropic block had actually billed 4,869 of them, 60% of its output. The reasoning-budget
group's measured-savings percentage is defined as the before/after difference over that
figure, so it was being computed from zeros and could never have shown a change in either
direction. The harness now reads the number from the provider's own usage block, using the
same fields the proxy does so the two cannot drift, and sums it across the turns of a
multi-turn conversation because every turn bills its own. Validated by re-deriving the
4,869 from the stored responses of the run that first exposed it.


### Cascade routing computed a complexity tier and discarded it — Bug fix

Adaptive reasoning reuses the complexity tier the router already decided, rather than
re-deciding it with a second, differently tuned classifier. One routing path broke that: the
cascade-execution branch classifies the request for its own escalation cap, then never
published the answer — so a deployment with cascade execution enabled silently fell back to
the duplicate classifier, with nothing reporting the behaviour had gone. The shipped default
has cascade execution off, so the default path was unaffected. Found while checking the
running configuration before a measurement run, not by a failing test.


### Compressed tool output is now checked for validity, not just size — Bug fix

When the optional compaction library shortened a tool result, the only check applied was
that the output was smaller than the input. Nothing verified it was still valid JSON — and
an agent consuming a tool result normally parses it, so a shorter-but-unparseable payload
would have broken the caller silently on a successful, billed request. Compaction is now
rejected if it turns parseable JSON into something that is not, falling back to the
built-in compactor. Measured against the shipped library this changes no output today; it
exists so a future upgrade cannot reintroduce the failure quietly, which this component has
done twice before.

Two related cleanups found by measuring rather than reading. The CSV branch of tool-output
compression was calling the library on every CSV result and then discarding the answer,
because it came back larger every time at 10, 200 and 2,000 rows — that call is gone. And
tool-output compaction now uses the library's lossless entry point rather than one that can
drop rows and leave behind a retrieval marker this proxy has no way to resolve; that path
does not trigger on the pinned version, but calling the safe entry point means an upgrade
cannot switch it on underneath us.

### A content-type detection call had never once succeeded — Bug fix

Response compression called `detect_type()` on the optional compaction library to classify
content, wrapped in a catch-all. That function does not exist in the pinned version, so the
call raised on every request and fell through to the built-in classifier — which has
therefore always been the only one running. Removed, so the code says what it does.

## 2026-09-04

### Four G19 tests depended on whether an optional package happened to be installed — Bug fix

Immediately after the test gate was widened, CI went red on four G19 compression tests that
pass on a typical developer machine. `headroom-ai` is a pinned production dependency, so the
container and CI take its compaction path while a bare dev checkout falls back to the
built-in one — and the two produce different output for arrays of records. The tests asserted
the fallback's shape without pinning which path they were on, so they had been passing
locally and failing under the real dependency for as long as both paths existed; nothing
noticed, because no gate ever ran them. They now pin the built-in path explicitly, matching
the discipline the other G19 test file already used, and the shipped path gained its own
coverage — including that a compressed tool result must remain valid JSON, which is the
contract an agent parsing that result depends on.

### CI and the OSS gate now run the whole test suite — Bug fix

Both the public CI job and the open-core release gate ran `pytest tests/unit`, so roughly
590 tests at `tests/` root and under `tests/integration` were **never executed by anything**
— CI imported them in its collection step and stopped there. Two tests were sitting broken
on `main` as a direct result, one of them introduced by the previous day's commit. Both
gates now run `pytest tests/`; commercial-only tests already self-skip on the open-source
tree, which is what makes the full run safe. Also fixed the second of those two tests: it
asserted that an embedding model gets loaded, but the embedding cache had since been added
in front of it, so on any machine with Redis running the first run populated the cache and
**every run afterwards returned the cached vector and never loaded the model** — it passed
exactly once and failed forever after, while also writing into a live Redis.

### Reasoning can now be turned OFF per request, across providers — Enhancement (OSS)

Every effort tier previously emitted an *enabling* parameter, including `low` — so once a
reasoning-capable model was routed, extended thinking was on for every request and the tier
only set a ceiling the model never approached. Measured on a support workload: reasoning was
**61% of the output bill** for answers no longer than the non-reasoning arm's. There is now
an `off` tier, realised per provider: Claude omits `thinking` (its own default), Gemini sends
budget `0`, OpenAI omits `reasoning_effort`. **The o-series still reasons intrinsically**, so
`off` there is recorded as `off_unsupported` and never counted as a saving. G25 now reuses the
complexity tier G06 already decided for routing — when that says `simple`, effort is `off`.
Also fixed: an unrecognised tier used to fall through to a 1024-token Claude thinking budget,
so a config typo turned reasoning **on**. Savings are **not yet measured**; the published
G12/G25 "10-30%" figures, which never were, are corrected to "not measured".
**Upgrade note:** `off` is a new rung *below* `low`, so an existing `G25_adaptive_reasoning.
effort_floor: low` keeps reasoning at `low` or above. Set it to `'off'` (quoted — bare `off`
is YAML `false`) to adopt the new default.
- **OSS:** `off` tier, `use_routing_complexity`, config-driven `reasoning_models`, and a new
  `token_opt_reasoning_mode_total{mode}` metric pairing the request with what was billed.
- **[Enterprise]:** the effort selectors are portal knobs — <https://tokenlean.cbeyond.cloud/>

### Portal offered a reasoning effort level that did nothing — Bug fix

The tenant portal listed `minimal` as a selectable reasoning effort for three settings, and
no part of the system implemented it. Choosing it sent an invalid parameter to OpenAI
o-series models, gave Claude a 1024-token thinking budget (i.e. it *enabled* thinking), and
silently widened the adaptive-reasoning band instead of narrowing it. Replaced with `off`,
which every layer now implements. The setting that governs most traffic — the effort used
when a request matches no complexity keyword — was also not exposed at all, while two
rarely-binding knobs were; it is now selectable.

### Tool policy is now enforced on streaming responses — Bug fix

Tool-call policy was applied only to non-streaming responses. On a streamed response the
policy silently did nothing: a DENY rule did not apply, the call was relayed, and the
caller's own agent loop ran it — on the path agentic clients actually use. The gate now
runs per chunk, keyed off the tool name (which arrives in the first fragment of each call),
so a denied call and its trailing argument fragments never reach the client. `flag` still
records without altering the response, matching non-streaming exactly, and an installation
with no policy configured does no per-chunk work at all. Note the proxy never
server-side-executed a streamed tool call — server-side execution is not on the streaming
path — so this closes a policy-enforcement gap, not an execution one.

### Removed a dormant cache module with un-namespaced keys — Bug fix

`g05_temporal_activity.py` built activity-replay cache keys without a tenant prefix, so two
tenants running the same workflow step could in principle have collided. Nothing imported
it, so it was never reachable, and the activity replay that actually ships
(`G05Cache.temporal_activity_replay`) has always been tenant-scoped and test-covered. The
module was deleted rather than patched — fixing keys in code that never runs is the
appearance of a fix — and the documentation that listed it has been corrected. A new test
asserts every G05 cache-key builder takes a tenant prefix.

### Batching savings figure corrected to what is measured — Bug fix

The G13 row advertised **25-60%**, a range covering three mechanisms: TOON compaction,
Kafka batching, and the opt-in provider-native batch lane. Only TOON is exercised by the
ablation, at **36%**; the other two are not measured at all. The row now states the
measured figure and names the unmeasured mechanisms as unmeasured. This **narrows the
published claim** — the 60% upper bound had nothing behind it. Same correction as the
"~84% on cached prefix" figure withdrawn earlier today.

### Adaptive reasoning now classifies the request, not the system prompt — Bug fix

Reasoning effort was chosen by scanning the user turn **and** the system prompt together. A
system prompt is fixed across a workload, so a single keyword in it pinned every request to
the same effort level — the opposite of adaptive. A one-line billing question inherited
"medium" from the word "explain" inside an 8,150-character policy prompt. Complexity is now
scored on the request itself; `scan_roles` restores the old behaviour if your system prompts
genuinely vary per request, and the effort used when nothing matches is now configurable as
`default_effort`.

**What this does not do:** it does not reduce reasoning cost on workloads like the one that
exposed it. Measured before and after on the same dataset, reasoning tokens were unchanged
(4,615 → 4,703). The cost there does not come from the effort tier at all — the model used
~157 reasoning tokens per request against a 5,000-token budget, so the tier is a ceiling it
never approaches. It comes from extended thinking being enabled at all, which currently
happens for any effort level on a reasoning-capable model. That is recorded as open work,
not fixed here.

### Optional guard: stop compressing a prompt out of the provider's cache — Enhancement (OSS)

Providers only cache a prompt prefix above a minimum size (~1024 tokens), and below it they
decline silently — no cache read, no cache write, no error. Measured on a shared-prefix workload:
the full optimisation stack compressed a 2,044-token prefix to ~736, so caching stopped entirely
and the input bill came to **2.5x the cost of prefix caching alone, while sending 63% fewer
tokens**. A new `preserve_cacheable_prefix` setting makes compression stand down when it would
push a prompt under the routed provider's minimum, with that minimum read from provider config so
it can be corrected without a redeploy.

**Ships off.** Which lever wins is workload-shaped: the conflict only bites when a prefix actually
repeats, and on a workload without repetition compression is strictly better. Turn it on with your
own measurement, not on ours. With it off, behaviour is byte-identical.

- **OSS:** the `groups.G1_compression.preserve_cacheable_prefix` toggle, per-provider
  `min_cacheable_tokens`, and per-arm cache reporting in the ablation harness so the trade is
  visible per configuration rather than as one blended number.

### Cache reads and writes are now reconciled against the provider bill — Enhancement (OSS)

The proxy already reported provider cache reads, writes and their costs per call. There was no
evidence that those figures were *right*. Each request now records the provider's own numbers
alongside the proxy's and marks whether they agree, so every call is its own reconciliation test —
first measured result: **54/54 calls reconciled, zero mismatches**, with costs independently
recomputed from list prices and matching to the cent. A provider that reports nothing (OpenAI has
no cache-write charge and no write field) is recorded as such, never as a disagreement. This is an
accuracy proof, not a savings claim: the read discount is the provider's, and TokenLean measures
and reconciles it rather than creating it.

Measuring it surfaced two things worth stating plainly. Prompt compression can push a prompt below
the ~1024-token minimum both providers require to cache a prefix at all, at which point caching
silently stops happening — no read, no write, no error; the two features do not always compose, and
which one wins is workload-shaped. And the README's "up to ~84% on cached prefix" has been
**withdrawn**: it was modelled, never measured.

- **OSS:** per-call cache reconciliation in the ablation harness, reported per arm; a
  `required_request_ids` guarantee so a dataset's signal cannot be sampled away; and an explicit
  warning when an arm's percentage was graded against no curated facts.

### Proxy metrics are now aggregated across worker processes — Bug fix

Each proxy container runs two uvicorn workers, and Prometheus keeps its counters in
per-process memory. Without a shared directory each worker held a private registry, so a
`/metrics` scrape answered from whichever worker the OS happened to route to: six
consecutive scrapes returned 69, 69, 23, 69, 23, 69. Every metrics-derived dashboard and
alert was computed from that oscillating series, and `rate()` reads each flip as a counter
reset. The images now set `PROMETHEUS_MULTIPROC_DIR` (it has to be in the environment —
metric objects choose their storage at import time) and `/metrics` aggregates across all
workers, purging stale files at container start. Gauges report the highest value any worker
saw, which keeps series names and labels identical, so no dashboard changes are needed.
**Billing is unaffected** — invoices are computed from the Postgres ledger, never metrics.
Under `max_instances > 1` each container is still scraped separately, as Prometheus expects.

### Deployment readiness no longer blames routing for a cached probe — Bug fix

The readiness check that proves per-tenant routing rules take effect sends a fixed prompt,
so its own earlier run left that answer in the cache. On any re-run against the same live
deployment the probe was served from cache, the routing stage never ran, and the check
reported "config propagation lag, or G6 routing disabled" — neither of which was true.
The result was a deployment that passed the first time and reported NOT READY on every
subsequent check, with a message pointing at the wrong subsystem. The probe now opts out
of the cache explicitly, and if a probe is still cache-served the check says so and reports
itself inconclusive rather than failing routing. Verified by polling for 145 seconds: the
rule never appeared, which is what ruled out a propagation delay and identified the cache.

### Reference-substitution trust now propagates across workers and instances — Bug fix

Context Compression & Reuse only replaces content with a reference for a client that has
proven it can fetch the content back, and revokes that trust the moment a client answers
without fetching. Both records lived in per-process memory, so neither travelled between
uvicorn workers or Cloud Run instances. Missing trust was harmless (the proxy simply sent
full content), but a missing **revocation** was not: a client that had demonstrably stopped
resolving kept receiving references from every other worker, answering from a summary on a
request that still billed 200. The record now lives in Redis under a per-tenant key, so
trust and its withdrawal are shared instantly. A lookup failure reads as "not proven" (the
safe direction); a failed revocation logs a warning. New operator setting
`groups.G28_ccr.resolver_proof_ttl_seconds` (default 3600) replaces the hardcoded lifetime.
Also corrects the configuration reference, which still described this feature as unavailable
and its store as in-process — both untrue since 2026-09-03.

## 2026-09-03

<!-- Marketing one-liners (benefit-led, no G-codes, honest about opt-in):
  * "See exactly how much of your LLM bill is cache - reads and writes, per call, per team,
     per day - so your cost line reconciles against the provider invoice."
  * "One changing value in your prompt can make you re-buy your whole cached prefix every
     turn. TokenLean can move it out of the way, without hiding it from the model."
  * "Several apps sharing a prompt can share one cached copy instead of each paying to
     build their own." (configurable)
  * "Park a big document once and refer to it, instead of re-sending it every turn -
     measured at 63% fewer input tokens when it is rarely read back, and a 30% penalty when
     it always is. We publish both numbers." (configurable; agent clients only)
  * "Index the same document twice and pay for it once."
-->


### Deployment readiness no longer reports a healthy proxy as broken — Bug fix

A full readiness run against a live, healthy proxy reported 16 optimisation stages as "never
executed" and returned NOT READY. Nothing was wrong with the proxy. The check compares a metrics
snapshot from before and after the run, and the proxy serves metrics from two worker processes
that each count separately — so the two snapshots came from different workers and the second
looked *smaller* than the first. A counter cannot go backwards, so that difference was never
evidence of anything.

The check now recognises that signature and reports "metrics unusable, cannot confirm from
metrics" instead of asserting a stage did not run, and continues to judge each stage on the
per-request evidence in the response, which is unaffected. This mattered because the failure was
invisible in normal use: a readiness run immediately after a deploy reads near-empty counters, so
it always passed there and only misfired against a proxy that had already served traffic.

Follow-on, tracked separately: the same worker split means Prometheus-derived dashboards and
alerts are computed from an oscillating series. Billing is not affected — invoices are computed
from the request ledger in Postgres, not from these counters.

### Feature-proof datasets no longer enter the blended savings headline — Bug fix

Our published savings figure is blended over the datasets that pass the quality gate. Two
datasets exist to prove one optional feature works, measuring deliberately opposite conditions —
its best case and its worst case. Because the blend counts only passing datasets, the best case
would have been folded into the headline (worth about +1.25 points) while the worst case, which
fails by construction, was silently dropped. A benchmark that keeps a feature's wins and discards
its losses is not measuring anything.

Both are now excluded together, by name and with the reason recorded, and the two "run everything"
code paths that had drifted apart now share one definition. That feature's figure is reported as a
range across both regimes rather than an average, because the mean of a best and a worst case
describes no workload that exists.

No effect on any published number today — the headline datasets are unchanged. This closes the
route by which a future re-baseline could have quietly risen.

### Context-budget compaction is lossy from rung 3, not just rung 4 — Bug fix

The G26 documentation marked only `rungs.drop` (rung 4, opt-in, default off) as lossy, which
reads as "leave drop off and nothing is lost". That is not true: rung 3 (`summarize`, **on by
default**) replaces the old span with a cheap-model summary, and a summary keeps the gist rather
than every detail. Measured 2026-09-03 — with `rungs.drop` off, `compress+summarize` cut a
compacted policy document from 2,588 to 259 tokens and the model could then no longer answer a
question about a value that existed only in that span, answering confidently on a normal 200.

No behaviour change: compaction always worked this way and the trade is the point of a budget
backstop. What was wrong was the disclosure, and an operator could reasonably have enabled G26
believing the defaults were lossless. README and the config reference now say so, and point at
`keep_recent_turns` / `target_pct` — or G28, whose reference comes back verbatim — when details
in older turns must survive.

### Ablation arms are now graded for answer quality, not just the all-on arm — Bug fix

The measurement harness gated `all-on` against `all-off` and nothing else, so a per-technique
savings figure could come from a run whose answers were wrong. Found with a live instance: one
technique was credited with 84% savings on an arm that had lost the fact it was asked about,
invisible because the combined arm recovered it. Every arm's own answers are now checked against
its own baseline with the existing deterministic fact check (no extra model calls, no extra run
cost); a figure from an arm that dropped a fact is recorded and reported, never averaged into a
published number. No customer-facing behaviour changes — this is the harness that produces our
published figures holding itself to the standard it already applied to the headline.

### Context Compression & Reuse now has a measured, two-sided savings figure — Enhancement (OSS)

G28 (Context Compression & Reuse) shipped with its savings honestly marked "not measured". It now
has a number, and the number has two sides, because CCR's value is entirely a function of how
often a parked document is actually read back (the expansion rate):

- **17% expansion** (a document parked once, referenced across many requests that rarely open it):
  **−63% input tokens, −20% cost**, quality gate passing with the retrieval request graded.
- **100% expansion** (every request needs the document): **+30% tokens** — the round trip re-sends
  the document, so CCR can only lose. Break-even sits near 75-80% expansion.

Measured on a new ablation dataset that also settles what CCR is *for*: budget-aware compaction
(G26) saved more on the same traffic but silently lost a detail that existed only in the parked
document, while CCR fetched it back intact. CCR is lossless recall of rarely-needed context, not
compression. Both remain default-off and require a tool-capable client.

- **OSS:** the measurement, the dataset, and the two-sided figure — no behaviour change.

### A document inside a tool result is no longer destroyed by structured pruning — Bug fix

Structured pruning treats a JSON payload as data to compact. But a tool result carrying a
document — `{"text": "<the whole runbook>"}`, one of the most common tool-output shapes — is
prose in an envelope, and the JSON compactor reduced an 11,492-character document to **45
characters** in the live deployment. The failure chain was invisible end to end: the model
asked for the document, the proxy fetched it, pruning destroyed it in transit, and the model
answered from memory on a request that returned 200. Payloads dominated by a single long
string are now compressed as the prose they are (11,492 → 1,534 characters with every checked
fact intact, verified against the same live compactor), genuinely structured JSON keeps the
strong compaction path, and a payload that resists safe compression is kept whole — content
beats tokens. Found by a quality-gated ablation whose planted facts vanished only when this
group was in the chain.

### Claude requests no longer fail when the proxy itself enables extended thinking — Bug fix

When the reasoning-budget optimisation turns on Claude's extended thinking, Anthropic rejects
any temperature but 1 — so a caller's perfectly valid `temperature: 0` came back as a 502
Bad Gateway naming neither the parameter nor the model. The caller's request was valid when
they sent it; the proxy made it invalid, so the proxy now owns the fix: when it enables
thinking it drops the incompatible sampling settings (`temperature`, `top_p`, `top_k`) —
dropped, not rewritten, since Anthropic's own default under thinking is the only accepted
value anyway. A request where thinking is not enabled keeps the caller's settings untouched.

### A reference the model re-formats is still a valid reference — Bug fix

Models copying a `[CCR:...]` handle out of a prompt often re-emit it without the brackets —
they read the delimiters as markup. The lookup demanded the exact wrapper, so a byte-perfect
64-character hash was refused for formatting alone; the model retried, failed again, and
improvised an answer on a request that returned 200. References are now parsed tolerantly
(`[CCR:<hash>]`, `CCR:<hash>`, or the bare hash), while the actual security property is
unchanged: exactly 64 hex characters and an exact keyed lookup, so truncated or forged
handles are refused exactly as before.

### A shortened reference is never sent to a caller that cannot expand it — Bug fix

Context Compression & Reuse offers its lookup tools only to callers that already send tools.
But it would still shorten a document for a caller that sent none — handing over a reference
and no way to expand it. Nothing could resolve that reference, so the model answered from the
short summary instead of the document, on a request that returned 200 and recorded a saving.

Shortening now requires that the lookup tools are actually being offered on that request, and
unlike the trust handshake this condition cannot be switched off: choosing to trust a client
is a judgement an operator may reasonably make, but sending a reference nothing can expand is
never correct. The document is still stored, so a later agentic turn is still cheap.

Found by the first live measurement runs, which recorded 45% "savings" for answers that had
lost the facts they were graded on.

### A reference the model never reads no longer counts as a saving — Bug fix

Context Compression & Reuse only pays off if the client actually fetches back the document
it parked. It earns that trust by resolving a reference once — but that trust was permanent:
after a single successful fetch the workspace kept receiving short references for an hour,
whatever happened afterwards. A client that stopped fetching — a different model, a changed
agent loop, or simply a turn where the model could not be bothered — kept answering from the
one-line summary instead of the document, on requests that returned 200 and recorded a
saving. The first live measurement run caught exactly that: with the tools offered, the model
answered anyway and invented the details that lived in the parked document.

Trust now decays on evidence. If a reference goes out and the model returns a final answer
without ever reading it, the workspace goes back to full content until it fetches one again,
the event is counted (`token_opt_ccr_reference_ignored_total`) and logged. A mid-conversation
turn is never judged this way, since the model may fetch on a later turn. Worst case is one
full-price turn; the alternative was a wrong answer billed as a success.

### A/B benchmark mis-reported cache tokens on two paths — Bug fix

The cache read/write columns added earlier today were only correct for single-call slices. The
multi-turn agentic episode never summed them, so the workload those columns exist for reported
zero on both arms; and the memoised direct arm re-counted its cold call's numbers on every
replay, reporting a cache burst as N writes and no reads — the inverse of what a warm cache
actually does. Episodes now sum both halves, and a replay counts nothing (`a_cache_calls` is the
denominator for the direct arm, since only calls that really happened have known cache numbers).
Savings percentages were never affected: they are computed from prompt/completion tokens only.

### Embed a document once, not once per app — Enhancement (OSS)

Apps inside one tenant already shared a vector collection, but the sharing stopped at
storage: every ingest re-encoded identical content from scratch, and nothing anywhere cached
a vector. Re-indexing an unchanged corpus paid the full encode again, and a second app
indexing the same document paid it a third time.

Two changes, both invisible to results:
- **Unchanged content is skipped.** Each chunk stores a hash of its text, so re-ingesting a
  document that has not changed does no work at all instead of re-encoding and re-writing it.
- **Vectors are cached per tenant, keyed by content.** Identical text encodes once, however
  many chunks, queries or apps ask for it. The cache is tenant-scoped like every other key,
  and a cache outage simply falls back to computing.

Embeddings are deterministic for a given model and text, so a cache hit returns a
byte-identical vector — this can only skip work, never change what retrieval returns, and
that is asserted rather than assumed.

### One stored copy instead of a copy per app — Enhancement (OSS + Enterprise)

Context Compression & Reuse is available again. It parks a large recurring block once and
sends a short reference in its place, so an agent that keeps returning to the same runbook,
contract or spec pays for it once rather than on every turn.

It was switched off because its store lived in a single process's memory: a reference died
with the instance that made it, and a later turn failed silently on a request that still
billed as a success. The store is now Redis-backed and **content-addressed** — the key is a
hash of the content itself. That gives three things at once: references survive restarts and
scale to any number of instances; two apps in the same tenant sending the same document
resolve to **one** stored copy instead of each keeping their own; and concurrent writers of
identical content are idempotent by construction, which is the hard part of any shared cache.

Answer quality is protected by refusing to be clever:
- a reference is **never** substituted for a client that has not demonstrated it can fetch
  one back — until then the full content is sent, and stored anyway so later turns are cheap;
- if the durable store is unreachable, nothing is substituted at all;
- the CCR tools are offered only to callers that already send tools, so ordinary
  request/response traffic is untouched;
- system prompts are still left alone unless explicitly opted in.

Also fixed along the way: references carried only 8 characters of the hash and were resolved
by scanning, so a collision could return a **different** document with no error anywhere; the
default tenant's scan matched every tenant's keys; the auto-execution path did not check
whether the feature was available at all; an unresolvable reference was a debug log rather
than a warning; and the in-memory store honoured neither its expiry nor any size limit.

- **OSS:** the durable content-addressed store, the resolve handshake, exact-key retrieval,
  and a new ablation dataset (DS22) that fails its quality gate if a reference does not resolve.
- **[Enterprise]:** the portal toggle and its knobs — <https://tokenlean.cbeyond.cloud/>

### Stop re-paying to build the same prompt cache every turn — Enhancement (OSS + Enterprise)

Provider prompt caches match from the first token and stop at the first byte that differs.
One changing value early in a system prompt — a timestamp, a session id, a rotating build
number — therefore invalidates the entire cached prefix behind it: the turn is billed as a
full cache **write** instead of a discounted read, with an identical token count. Nothing in
the product addressed this; G21 reordered a prefix that then failed to match anyway.

Two configurable additions, both default-off and byte-identical until enabled:
- **Prefix stabilisation** relocates operator-nominated volatile spans out of the cached
  prefix and re-attaches them immediately after it. Nothing is deleted or reworded — the
  model still sees every value; only its position changes. Patterns stay operator-owned
  because a wrong one silently moves the wrong text.
- **Shared prefix profile** lets several internal apps declare they share a prompt, so they
  converge on one provider cache shard instead of each paying to build a private copy.
  Set per tenant, or per request with an `X-Prefix-Profile` header.

Verified the honest way: two turns differing only in a timestamp now produce a byte-identical
prefix *and* an identical cache-shard key, with a control proving they genuinely diverge when
the feature is off.

- **OSS:** the stabilisation and profile engines, config, and a readiness probe.
- **[Enterprise]:** a portal switch for stabilisation, and the cache read/write split that
  shows it worked — <https://tokenlean.cbeyond.cloud/>

### Cache reads *and* writes are now measured, priced and reported — Bug fix

Cost reporting credited the provider cache half that is **discounted** (reads) and tracked
the half that is **not** (writes) nowhere at all — `cache_creation_input_tokens` appeared
nowhere in the product. A tenant whose prompt prefix changes each turn re-pays to build the
cache on every call, sees identical token counts, and had no line anywhere that explained
the invoice. Both halves are now captured from the provider response, priced at published
per-provider rates, disclosed per call, and persisted per tenant and per day.

Also fixed: **streamed** requests recorded no cached tokens and a cost of zero, because the
response pipeline is skipped for streams — so the traffic most likely to use prompt caching
(agentic clients) was the least visible. And G21 published a cache-discount percentage taken
from config that was never checked against the response; on Anthropic it claimed a 90%
discount even with the cache marker off, i.e. when nothing had been cached. It now reports
only what was measured.

**Reported cost changes for Anthropic tenants using cache markers** — writes were being
priced at 1.0x and are really ~1.25x (5-minute) / ~2x (1-hour). This corrects a disclosed,
never-billed estimate; it does not change what anyone is charged.

- **OSS:** cache read/write tokens + their cost split in `_token_opt`, new
  `x-tokenlean-cache-*` response headers, two Prometheus counters, four nullable
  `usage_events` columns, published per-provider write rates in the config template, and a
  read-vs-write **token** row on the billing dashboard.
- **[Enterprise]:** the cost split and cache-share-of-bill percentage in the usage rollup,
  chargeback export and billing dashboard — so a finance question ("how much of this
  invoice is cache?") is answerable per tenant and per day —
  <https://tokenlean.cbeyond.cloud/>

## 2026-09-01

### Context Compression & Reuse is now refused rather than quietly unreliable — Bug fix
This optimisation swaps a large block of text in your prompt for a short reference the
model fetches back on demand. The text was only ever held in the memory of the single
process that stored it — so the reference stopped resolving as soon as that process was
replaced, which happens on any restart, on a second instance, and on the idle shutdown
that the default deployment relies on to scale to zero. The failure was silent: the model
got a short "not found" back, no error reached your dashboards, the request was billed as
a success, and the model would typically carry on from its own earlier summary rather
than say it had lost the text — a confident answer reconstructed from a paraphrase
instead of the source. The proxy now refuses to run it, says so once in the logs, and the
setting can no longer be switched on from the portal, which until now recommended it. The
README's savings figure for it has been corrected to "not measured", because it never ran.
For long conversations, Budget-Aware Context Management does the same job and is measured.
This is a gate, not a removal — the feature comes back when its storage is durable.
- **OSS:** the runtime refusal and the corrected documentation.
- **[Enterprise]:** the portal explains why the toggle is unavailable instead of
  accepting a change that would not take effect — <https://tokenlean.cbeyond.cloud/>

### The proxy no longer runs a tool it was told not to run — Bug fix
The proxy can carry out a handful of tool calls itself, server-side, rather than handing
them back to your application. It decided whether to do so by matching the tool's name,
and nothing else. The tool policy was checked earlier, when the response was assembled —
but its default setting is "record what you would have blocked, and change nothing", so a
tool call the policy had already flagged as not permitted was recorded as such and then
carried out anyway. That is worse than not checking: the audit entry proved we knew.
Server-side execution now checks the policy at the moment of acting, and refuses in every
mode. It also refuses to run any tool the proxy did not itself offer to the model — so a
tool of your own that happens to share a name with one of ours is passed back to your
application untouched instead of being intercepted, and a name a model invents or is
tricked into producing is not run at all. A refused call is still returned to you, just
not acted on. Refusals are counted and audited separately from policy decisions, so
"we declined to act" and "you were denied a result" stay distinguishable.
- **OSS:** the check, the refusal reasons and the metric ship in the core proxy.
- **[Enterprise]:** refusals appear in the portal's Trust & Safety tab and the operator
  console under a new filter — <https://tokenlean.cbeyond.cloud/>

### One workspace could infer another's traffic volume from a usage-stats tool — Bug fix
The server-side `headroom_stats` tool reported how many stored text blocks the proxy held
and how many lookups had hit or missed. Both numbers covered every workspace sharing the
process, not the one asking. No content was exposed, but polling the tool across turns
revealed other workspaces' request volume, the size profile of what they were sending,
and when they were active — enough to read a competitor's working hours or batch
schedule off a shared deployment. The numbers are now scoped to the workspace asking.
Anyone who was reading the larger figure will see it drop; it was never theirs to see.

### Server-side compute settings can now be set per workspace — Bug fix
The `G15_server_compute` block was documented as configurable per workspace in
config.yaml, like every other group, but read only the global block — so an operator had
no way to turn server-side tool execution off for a single workspace short of editing the
database. It now resolves the per-workspace override like its siblings, and tolerates a
mis-indented config section instead of failing every request for that workspace.

## 2026-08-31

### One workspace could read another's server-side compressed text — Bug fix
The context-compression feature can store a block of text server-side and hand back a
short reference the model uses to fetch it later. Storage is scoped per workspace, and
the retrieval scan is written to stay inside that scope — but the server-side compute
path called it without saying which workspace was asking. With no workspace named, the
scan matched every stored block, so a reference from one workspace resolved to another
workspace's text. The compression group's own copy of this call passed the workspace
correctly; the server-side compute copy, which is the one enabled by default, did not.
Both now pass it, and stored keys are namespaced per workspace so two workspaces
compressing identical text no longer share a single entry. Found while reviewing which
code paths can act on a tool call without checking it first.

### One workspace's tool policy could be applied to another's traffic — Bug fix
The tool-eligibility gate cached each workspace's compiled policy in a single shared slot
rather than one per workspace. If a policy failed to compile — a mistyped wildcard, say —
the gate fell back to "the last policy that worked", which could be a *different*
workspace's. The result was a workspace being judged by rules it never wrote, with the
outcome depending on which requests happened to run first. The cache is now keyed per
workspace, so a fallback can only ever reach that workspace's own last-good policy; if it
has none, the gate reports the misconfiguration loudly and stops enforcing rather than
guessing. This also removes the cache contention that made busy multi-tenant deployments
recompile policies on nearly every request.

### Turning PII redaction or the tool-eligibility gate "off" in config.yaml did nothing — Bug fix
YAML treats an unquoted `off` as the value `false`, not as the word "off". Both controls
compared it against their list of valid settings, found no match, and quietly fell back to
their default. Nothing unsafe happened — the fallback is a detect-and-record setting that
changes no traffic — but an operator who had switched a control off still saw its audit
entries and metrics accumulate, with nothing anywhere explaining why. Both spellings now
work, for these controls and for context trust's PII setting. The config template and
reference call the gotcha out.

### A malformed tenant block in config.yaml took a whole workspace offline — Bug fix
config.yaml is edited by hand, so a mis-indented `tenants:` block can leave a section
holding text where the proxy expects settings. Eight groups then failed while reading it
and returned an error for every request from that workspace — a full outage from a typo.
Configuration reading is now type-checked at every level: a malformed section costs that
workspace its custom settings for that group and is logged, while traffic keeps flowing on
the defaults. The four groups that carried their own near-duplicate copy of this logic now
share the single hardened one, which also fixes batching quietly discarding sibling
settings when a workspace overrode one value inside a nested block.

### Batched requests skipped the tool-eligibility gate — Bug fix
Batching answers a request out of band and delivers the result through a separate endpoint,
neither of which runs the response checks. A batched request that came back asking to call
a tool therefore reached the caller without being checked against the workspace's tool
policy — a silent hole in a control whose entire guarantee is that it runs before anything
can act. Requests that declare tools are no longer batched: they run normally, through the
full set of checks. Bulk prose batching, which is what the feature exists for, is
unaffected.

### Context-trust decisions left no audit trail — Bug fix
When the context-trust control found an injection attempt inside retrieved documents and
flagged, stripped or blocked it, no audit entry was written — the decision was missing from
both the audit writer and the code that schedules it, so it was the one trust & safety
verdict with no compliance record. It now writes an entry like every sibling control, kept
distinct from the user-prompt guardrail because the two mean different things: one says a
user attacked you, the other says your own knowledge base is carrying an attack. Blocked,
stripped and flagged stay separate outcomes, since a stripped request was still answered.
- **[Enterprise]:** the events appear in the portal's Trust & Safety tab and the operator
  console under a new *context trust* filter — <https://tokenlean.cbeyond.cloud/>

### A broken tool-eligibility gate on cached responses looked identical to a working one — Bug fix
Cached and bypassed responses are checked by a separate call to the tool-eligibility gate.
If that call failed it was logged as a warning and the response served unchecked — the
right trade-off, since failing a cache hit closed would be an outage, but indistinguishable
on any dashboard from the gate passing cleanly. Failures are now logged as errors and
counted on a dedicated metric, so a permanently broken gate is visible instead of silent.

### Per-tenant configuration now works for all four trust & safety controls — Bug fix
Every group is meant to be configurable per tenant through two routes: the settings a
tenant edits in the portal, and a `tenants.<id>.groups.<group>` block an operator sets in
config.yaml. The second route was documented for PII redaction, injection guardrails,
context trust and tool eligibility but implemented for none of them — those four read the
global block only, silently ignoring a per-tenant operator override. That gap bit hardest
exactly where it mattered: a tenant is deliberately refused permission to switch a safety
control off, so with the operator route inert there was nowhere to configure one tenant
differently short of editing the database directly. All four now resolve the overlay
through one shared helper, merging key-by-key so overriding one setting never drops its
siblings, and never mutating the shared config other tenants are reading. Deployments
with no `tenants:` block behave exactly as before.


### Tool-call events now shown correctly in the portal and operator console — Bug fix [Enterprise]
Tool-eligibility events were being recorded but mis-presented. Both the tenant Security
tab and the cross-tenant operator summary bucketed trust & safety events into exactly two
kinds, so every tool-call event was counted and labelled as a guardrail event — the data
was right, the reporting was not. Bucketing is now three-way, and the incident log gained
a "Tool call" label, a matching filter, and a readable summary line naming the tools that
were blocked or flagged. The operator console gained a "Tool calls stopped" tile. Also
fixes a nearby gap: the deployment readiness check verified the tool-eligibility gate but
never counted that result toward the READY verdict, so a broken gate could have been
reported alongside a passing deploy. Self-hosters are unaffected — the engine, its metric
and its audit rows were always correct; only the managed presentation layer was wrong.
- **[Enterprise]:** Security tab, operator console and readiness verdict —
  <https://tokenlean.cbeyond.cloud/>


### Tool-call eligibility added to the never-auto-skip safety list — Bug fix
The self-tuning learning loop keeps a denylist of groups it may never emit a bypass rule
for (rate limiting, cache, routing, observability, and the trust & safety groups). G32
shipped earlier the same day without being added to it. No live exposure — the response
chain has no `skip_groups` guard, so G32 was unreachable by adaptive bypass regardless —
but the denylist is the registry that keeps the invariant true if that ever changes, and
a safety control that could be switched off by a learned rule is not a safety control.
The replacement test asserts the property by class (every trust & safety group is
denylisted) rather than by re-listing today's groups, so the next one to land fails until
it is covered. Also completes the G32 documentation pass: the group table, response-chain
order, RequestContext fields, Free-vs-Enterprise matrix, and the remaining `G0–G31` spans.


### Tool-call eligibility — decide which tools a model is allowed to ask for — Enhancement (OSS + Enterprise)
Server-side tool execution previously had **no authorization**: G15 dispatched handlers by
bare name match against a hardcoded set, so a prompt-injected model could make the proxy
*act*, not merely answer. The new gate checks every requested `tool_calls` entry against a
per-tenant allow/deny policy and runs **ahead of every auto-executing stage**, so an
ineligible call is stopped before anything can dispatch it. `flag` records and serves
unchanged; `block` strips the call and repairs the message (`finish_reason` corrected,
`content` never null, no dangling `tool_calls`). Shipped enabled in `flag` with an empty
policy — byte-identical until you write one. Also non-bypassable on the cache/bypass
short-circuit, which previously returned without running any response-side group.
Malformed glob patterns are rejected at write and load time: `fnmatch` silently matches
nothing, so an unvalidated typo in a **deny** rule would quietly stop denying.
**Known limitation, stated plainly:** streamed responses bypass the response pipeline and
are **not** gated — same limitation the G29/G30 response scans carry.
Built ahead of its recorded trigger (untrusted-tenant server-side execution) deliberately.
- **OSS:** policy engine + gate + `token_opt_tool_eligibility_denied_total{mode}` +
  PII-free `tool_eligibility.*` audit rows + config block + Grafana panels.
- **[Enterprise]:** Tool Policy console — per-tenant policy CRUD with `tenant|base|none`
  inheritance, a dry-run tester, and a change-audit trail — <https://tokenlean.cbeyond.cloud/>

## 2026-08-09

### Fresh deployments no longer truncate long-form answers — Bug fix
The output-length control derived its `max_tokens` cap from INPUT size (≈30% × 2,
ceiling 1024) whenever no usage history existed, so a short question needing a long
answer — a proof, an algorithm design — was cut off mid-sentence on any cold start
(fresh deploy, or expired 7-day history). Worse, the truncated completions were then
recorded as history and re-taught the low cap permanently. Caps now come only from
evidence: the p95 of observed **completed** answers; an answer cut off by our own cap
re-enters the evidence escalated (`truncation_backoff_multiplier`) so caps climb out of
a bad guess; with no evidence, no cap is applied (opt-in static `fallback_max_tokens`
for operators who want one), and history is tenant-scoped so workloads never cross.

## 2026-08-08

### Tool token estimates are now provider-aware — Bug fix
Measured against the providers' own token-counting endpoints, the same 11 tool
definitions bill ~285 tokens on OpenAI, 625 on Gemini and 1,307 on Anthropic (which
injects a tool-use system prompt server-side) — up to 4.4x apart, so no single
serialisation can estimate all three. Each provider adapter now reports its own billing
shape (calibrated against measured actuals and pinned by tests that fail on ±15% drift);
providers without a specific shape keep the packed OpenAI form unchanged. This corrects
disclosed savings on tool-heavy Claude/Gemini traffic and, more importantly, context-budget
window math: Anthropic tool overhead was under-counted ~3.7x, which could have delayed
compaction until a request actually overflowed. Estimates only — billing is request-count
and never affected.

### Deferred cascade hardened after code review — Bug fix
Same-day review of the cascade deferral found nine defects, all fixed before any deploy.
The ones that mattered: tier calls skipped the provider param-hygiene the normal call site
applies (mixed-provider ladders could silently fail to escalate); the escalation cap was
re-derived from the *compressed* prompt, so a long request could get locked to the cheap
tier — every plan-time decision (tier pick, cap, routing label) is now carried in the plan
and never re-derived; a failed tier-1 was retried instead of re-routing to the caller's own
model; stateful tier rotation was consulted twice per request; routing metadata claimed a
cascade before it had actually run; an unreachable tier left a doomed plan re-attempting on
every request; and streaming requests are now excluded up front (the confidence probe cannot
read a stream). Separately, malformed tool schemas no longer crash the token estimator, and
tool-catalogue pruning now uses the same packed tool counting as everything else.

### Cascade routing now applies every optimisation before calling the model — Bug fix
With cascade execution enabled, the tier-1 model call was made at the routing stage —
*before* prompt compression, tool pruning, output-format control and the other
optimisations had run. Those stages still executed and recorded savings, but their work
never reached the wire: the provider received the unoptimised prompt, and the recorded
savings were phantom. The cascade call now happens at the normal call site, after the
full pipeline, so cascaded requests get exactly the same optimisations as everything
else. On any cascade error the request falls back to a normal call on the cheap tier —
never a failure, never a duplicate provider round-trip.

### Tool definitions are no longer over-counted in savings estimates — Bug fix
Token estimates for tool/function definitions counted the raw JSON schema, but providers
send the model a much more compact packed form — so requests carrying many tools
over-stated their baseline by ~2-3x, inflating both the disclosed savings on tool-heavy
traffic and the per-step savings recorded by architecture enforcement. The estimator now
renders tools the way the provider actually packs them and counts that, validated against
provider-billed usage. Savings figures on tool-bearing requests become more conservative
and more honest; no served traffic changes.

## 2026-08-07

### Context-budget compaction hardened after code review — Bug fix
Nine defects found reviewing the budget-aware compaction that shipped earlier the same day,
all fixed before it can be enabled in anger (the feature is off by default, so no deployment
was affected). The ones that could have changed answers: the prose compressor was rewriting
**tool results**, so a payload value like `"the north"` came back as `"north"`; repeated short
turns ("ok", "continue") were being deleted as duplicates, stranding the replies that answered
them; and a conversation summary could be larger than the history it replaced, growing the
prompt invisibly. The ones that could have broken requests: the output reservation ignored the
`max_tokens` the proxy itself adds later, `keep_recent_turns` protected half the exchanges it
promised, an over-large history could exceed the summariser's own context window, and a
negative setting disabled compaction permanently instead of failing loudly. Also: summaries are
now reused as a conversation grows (previously the cache could never hit on a live thread), and
the trust-and-safety context scan no longer strips the proxy's own summary — which, since the
summary replaces the earlier turns, would have discarded the whole conversation.

### Long conversations now compact themselves before they overflow the model's context window — Enhancement (OSS + Enterprise)
Multi-turn agents and long-running support threads grow until they hit the model's context
limit, at which point the request either fails or the caller has to throw history away by hand.
The proxy now watches that budget for you: when an assembled prompt passes a configurable share
of the *usable* window (the model's window minus the space reserved for its answer), it compacts
the older part of the conversation back down using the cheapest step that works — dropping
repeated turns and trimming stale tool output, then compressing wording, then replacing the older
span with a short cached summary, with an opt-in last-resort step that drops the oldest turns
outright. Recent turns and system prompts are never touched, and every cut is made at a
tool-call boundary so a tool result is never separated from the call that produced it. Off by
default; when off, requests are byte-identical.
- **OSS:** the full engine, all thresholds and per-step switches, the per-model context-window
  map, the `token_opt_context_budget_compactions_total` metric, and a new benchmark dataset
  (DS21) that measures it end to end under the standard quality gate.
- **[Enterprise]:** tune every threshold and step per tenant from the portal's Groups tab
  without touching config files — <https://tokenlean.cbeyond.cloud/>

## 2026-08-06

### Reproducible installs: proxy + test dependencies are now pinned lockfiles — Enhancement (OSS)
Every dependency was an open `>=` floor, so each CI run and image build silently installed
whatever PyPI had that day — contributor PRs could go red from an overnight upstream release,
and dependabot's floor-bump PRs changed nothing about what actually shipped. `src/proxy/requirements.txt`
and `tests/requirements-test.txt` are now full pinned resolves compiled from human-edited
`requirements*.in` files by `scripts/compile-requirements.sh` (runs pip-compile inside the same
python:3.11 image the proxy ships on; torch stays unpinned so the image keeps its CPU build).
CI and the Dockerfile are unchanged — they install the same filenames, now deterministic.
Dependabot is scoped to match: version PRs stay on for the tests lockfile (where a bump is a real,
CI-tested change) and GitHub Actions, and are disabled for the proxy lockfile (Dependabot's
regenerator re-pins the excluded CUDA stack — proven by its first live PR going red on the new
guard; refresh via the script instead), the sidecar/pipeline floors, Docker base images and the
Java sample — security updates still flow everywhere.
`tests/unit/test_requirements_pinned.py` guards the lockfiles' completeness and exclusions.

### Qdrant client and server versions no longer drift apart — Bug fix
`qdrant-client` refuses a client/server gap of more than one minor version, and five independent
pins had drifted: the proxy was capped `>=1.12,<1.13` while the doc/finetune pipelines and the
pitch-test-plan harness were uncapped (resolving to 1.18.x), and the server was v1.12.6 locally but
v1.9.0 on GCP. The pipelines were therefore seeding, with a 1.18 client, the very collections a 1.12
proxy reads back for retrieval, and every test run logged an explicit incompatibility warning. All
client pins are now `>=1.12,<1.13`, both server declarations are `v1.12.6`, and Dependabot holds
qdrant-client at that minor (patches still flow). A new `tests/unit/test_qdrant_version_alignment.py`
fails if any one of them moves without the others.

### Pick models and providers from a dropdown, backed by a refreshed model catalog — Enhancement (OSS + Enterprise)
Model and provider fields in the portal were free text with a loose autocomplete, and the model
suggestions came from the `pricing:` keys — matching *fragments* like `claude-opus`, not real model
ids. Both are now proper dropdowns: providers come from the configured `providers:` list (a closed
set — the proxy can only route to those), and models come from each provider's `models:` list in
`config/config.yaml`, grouped by provider. That list is the operator-maintained catalog: a plain
static file, hot-reloaded like the rest of the config, so a newly-released model appears in the
picker within ~60s with no deploy and no code change. A **Custom…** option keeps any model usable
before it is added. The shipped catalog and `pricing:` table were refreshed against the providers'
current line-ups (verified 2026-08-06) across OpenAI, Anthropic, Gemini, Mistral, DeepSeek, xAI,
Cohere, Groq and Bedrock; legacy ids are kept where the provider still serves them.
- **OSS:** the refreshed catalog + pricing rows in `config.yaml.template`, and the same list already
  governs which requested models the proxy accepts.
- **[Enterprise]:** the grouped dropdowns in the portal's Models & Keys tab — <https://tokenlean.cbeyond.cloud/>

### A tenant's contract is now scoped to that tenant, not to its whole company — Bug fix [Enterprise]
Affects the managed product only (customer portal + operator console — <https://tokenlean.cbeyond.cloud/>);
self-hosted deployments are unchanged and have nothing to upgrade.
Contract state lived only on the `companies` row (one per 4-letter company code), so every stack
of a company shared it: deactivating `ACME-PRD-01` immediately blocked portal login for
`ACME-PRD-02` (and the console showed both as inactive), while key-level request blocking was
already per-tenant — half company-wide, half per-tenant. Creating a second stack also silently
re-activated a deliberately deactivated sibling. Contracts now live in a new per-tenant
`tenant_contracts` table (status + `paid_until`); the company row remains a read-only **fallback**
for tenants provisioned before it, so no migration runs and no live customer is locked out. The
same fix closes a self-serve signup lockout: `companies.contract_status` defaults to `pending`, so
a brand-new self-serve owner was 403'd out of the portal on their very next request — signup now
writes an explicit `active` contract row for the tenant it provisions. "Resend invite" is also
per-tenant now instead of flagging every stack sharing the code.

## 2026-08-05

### G19 no longer rewrites the model's answer — Bug fix
G19's content detector used a whole-message `.search()`, so a **single ``` fence** — or one line
opening `from `/`class ` — reclassified an entire prose answer as a *code payload*. `_compress_code`
then deleted every `#`-leading line, i.e. the answer's **Markdown headings**, in text that goes
straight back to the caller. Detection now requires code (or logs) to **dominate** the payload
(configurable `detect_dominance_ratio`, default ≥50% of non-blank lines), and `_compress_code`
only compresses **inside** ``` fences — prose, headings and bullets around a code block are
emitted verbatim. Separately, the response side no longer rewrites **answer content at all** by
default (`response_side_compress_answers`, default `false` — covers prose sentence-dedup, code
comment-stripping, log dedup and JSON field-dropping alike): the answer is what the caller reads,
and rewriting it saves nothing on that call since the provider has already generated and billed
those output tokens. Request-side compression and response-side **tool-result** compression are
unchanged, so payload savings are unaffected — verified offline: the internal calibration datasets
(DS7/DS14) have **zero** classification changes under the new detector, pinned by a regression
test. The stale docstring claiming "prose is excluded by default" was false against the shipped
template — and the test fixture omitted `text`, so the whole suite validated a config that never
ran; a guard test now asserts the fixture equals `config.yaml.template` (values, not just keys).
Both knobs are settable from the portal's Optimisations tab.

### Quality gate scored Markdown formatting as a dropped fact — Bug fix
Both facts gates matched required facts as raw case-insensitive substrings, so a fact the model
**emphasised** was scored as missing: `"The St Andrews Agreement"` is not a substring of
`"The **St Andrews Agreement**"`. Because the gate is *relative*, this fired precisely when an
optimisation changed the answer's **formatting** rather than its content — manufacturing quality
regressions where nothing was lost, and hitting Markdown-heavy models hardest. Facts, OR-groups and
forbidden strings are now normalised (emphasis stripped, whitespace collapsed) on **both** sides of
the comparison; underscores are deliberately preserved so identifiers like `_affinity_propagation.py`
still match. A genuinely absent fact still fails — covered by regression tests in both harnesses.

### A/B harness: right-sized output budget and diagnosable quality failures — Bug fix
`swe` items capped output at 256 tokens, which truncated **both** arms mid-answer
(`finish_reason='length'`), so the facts gate scored whichever arm happened to reach the filename
first rather than answer fidelity; the per-profile budget is now 768 for `swe` (others unchanged).
The harness also reported only a *count* of dropped facts, leaving a failing gate impossible to
investigate — it now records each failure's label, the dropped fact, both arms' answers, the cache
flag and `finish_reason`, printing `[proxy TRUNCATED at max_tokens]` when the answer simply ran out
of budget. New `--profiles rag,swe` re-runs just the profiles that regressed instead of the whole
corpus (a typo'd profile name exits 1 before any spend rather than silently running nothing).
The fact matcher also normalises Markdown emphasis to **spaces** (never deletions), so stripping
can never merge adjacent characters into a false match (`2*4` can no longer satisfy an expected
`24`). Re-measured on OpenAI after the G19 fix: the combined prose lever holds at **~8%** with a
clean 40/40 facts gate; the disclosed `ops` figure is restated **~44% → ~43%**, the cost of no
longer running a pasted config through the *code* compressor (which stripped its `#` comments).

## 2026-08-04

### Per-provider model routing (G06) — non-OpenAI providers now route within their own family out of the box — Enhancement (OSS)
G06's tiers were a single OpenAI-only ladder (`simple→gpt-4o-mini`, …), so with routing enabled a
Claude/Gemini/Mistral/… request was silently rerouted to `gpt-4o-mini` — the wrong provider and
model. Added `tiers_by_provider`: G06 now picks the ladder for the **requested model's own provider
family** (a Claude request cascades `claude-haiku-4-5 → sonnet → opus`, a Gemini request
`flash → pro`, …), and a provider with **no** ladder passes through untouched — G06 never
cross-provider misroutes. The template ships ladders for all 10 native providers (delete the ones
you don't use). The `openai` ladder mirrors the previous flat tiers, so OpenAI routing — and the
published savings baseline — is byte-identical (verified by a flat-tiers-identity regression test).
The **cross-provider** cost cascade (route by complexity *across* providers — `simple→openai`,
`medium→gemini`, `complex→anthropic`) is still supported and is documented as the opt-in alternative
(the flat `tiers` map with a mixed-provider ladder; delete `tiers_by_provider` to use it).
Also fixes the public A/B benchmark so `--providers <anything>` measures that provider on both arms
instead of unknowingly comparing it against `gpt-4o-mini`.
- **OSS:** `_resolve_tiers` in `g06_routing.py` (family-aware, pass-through on miss, legacy flat-`tiers` fallback); `tiers_by_provider` in `config.yaml.template` (10 providers); provider-aware benchmark pin (`run.sh` + `run_ab.py`); 15 new unit tests (flat-tiers identity + template parity).

## 2026-07-28

### Public A/B benchmark: OpenAI-compatible model gateways (opencode/zen) as an A/B provider — Enhancement (OSS)
The A/B harness assumed every provider was a *native* litellm provider that reads its key from an
env var, so a model **gateway** like OpenCode Zen (an OpenAI-compatible endpoint fronting many
models) couldn't be A/B-tested. Added a generic gateway path: `call_direct` now accepts an explicit
`api_base`+`api_key` (the direct arm calls it as `openai/<model>` so litellm never falls back to the
real `OPENAI_API_KEY`/base), and a new `opencode` entry in `PROVIDER_MODELS` (11th provider) carries
`api_base: https://opencode.ai/zen/v1` + a distinct `OPENCODE_API_KEY` key var. Priced `ling-3.0-flash-free`
in `prices.json` at a genuine $0 (free model → the **cost** lever is $0-vs-$0 by construction; only the
**token** lever is meaningful — disclosed in `_opencode_note`) and fixed the stale `config.yaml.template`
opencode model ids (`mimo-v2.5` etc. 404; real ids are `-free`-suffixed) by adding the runnable
`opencode/ling-3.0-flash-free`. Validated live: both arms route to opencode, correct content. Generalizes
the harness to any OpenAI-compatible gateway (point the map at any `api_base`).
- **OSS:** `call_direct` api_base/api_key; `opencode` provider entry; `prices.json` + `config.yaml.template` rows; new `test_opencode_is_openai_compatible_gateway` guard + provider-count 10→11; 52 A/B tests green.

### Admin console Trial tab now labels the day/request units and explains how the limits work — Enhancement [Enterprise]
The per-tenant **Trial** tab in the Enterprise admin console showed two bare number boxes per row
whose meaning lived only in placeholder text that vanished once a value was typed — so a filled form
read as `14 / 5000` with no indication of units. Added always-visible **days** / **requests** labels
next to each input (and **more days** / **more requests** on the Extend row to signal those are
increments, not replacements), plus per-row captions and a collapsible *"How these numbers work"*
explainer: a trial ends when **either** limit is reached, `0` leaves a dimension unlimited, Start/Set
set absolute limits while Extend adds to the running trial. Inputs gained `aria-label`s for screen
readers. No behavioural change — copy/labels only. Covered by an extended `TenantDrawer` vitest.
- **[Enterprise]:** admin-console UX clarity — <https://tokenlean.cbeyond.cloud/>

### `generate_proxy_key.py` runs on Python 3.7/3.8 again — Bug fix
The local key-mint helper (`scripts/generate_proxy_key.py`) used PEP 585 builtin-generic annotations
(`tuple[str, str, dict]`) that are evaluated at import time, so it crashed with
`TypeError: 'type' object is not subscriptable` on Python 3.7/3.8 (common on stock WSL/Ubuntu) — the
documented admin-key mint command failed there. Added `from __future__ import annotations` (PEP 563)
so the annotations are lazy strings and the script runs unchanged on any Python 3.7+.

### Admin console self-lockout guard: an operator can no longer disable its own tenant — Bug fix
The Enterprise admin console let an admin key run destructive lifecycle actions against **its own**
tenant. Because deactivating/suspending/deleting a tenant flags **every** key of that tenant —
including the key making the call — an operator could set the `admin` tenant's contract to `inactive`
and instantly lock out all admin keys (a request-time 403 that then needs out-of-band recovery). The
admin authenticator now stamps the acting tenant, and `/tenants/{id}/contract` (non-active), `/suspend`,
`DELETE /tenants/{id}`, and `/offboard` refuse when the target is the caller's own tenant (403). Since
all admin keys share the bootstrap `admin` tenant, this also hard-protects that root tenant from console
self-lockout; managing every other (customer) tenant is unchanged. Covered by 4 new admin-router tests.

### Public A/B benchmark: Gemini provider now uses floating `-latest` aliases so new projects can run it — Enhancement (OSS)
The A/B harness pointed its Gemini arm at pinned ids (`gemini-2.5-flash-lite` / `gemini-2.5-pro`),
but Google now 404s those with *"not available to new users"* on **newly-created** API projects —
steering new projects onto the floating `-latest` aliases. A verifier with a fresh Gemini key
therefore couldn't run `--providers gemini` at all. Switched the provider map to
`gemini-flash-latest` (+ `gemini-pro-latest` for the routed tier), added priced rows for both to
`prices.json` (with deprecation notes explaining the new-project restriction on the 2.5 ids, kept
priced for existing projects), and added the aliases to the `config.yaml.template` Gemini provider
model list + pricing so the proxy routes them. Validated live end-to-end on a paid key: cold floor
5.4%, and the structural `ops` lever fires at **45.2% — matching OpenAI's 44.2%**, confirming the
levers are model-agnostic. Reasoning-model note: `gemini-flash-latest` has thinking on by default,
so small stateless profiles (chat/reason) can go net-negative through the proxy — disclosed, not hidden.
- **OSS:** provider-map + `prices.json` + `config.yaml.template` updates; new `test_gemini_map_uses_latest_aliases` guard; 51 A/B tests green.

### Public A/B benchmark: disclosed production-shaped `ops` profile so the structured-pruning levers are reproducible — Enhancement (OSS)
The public A/B harness's cold "prose" floor read only ~2–4% because the recognized Q&A datasets
(HotpotQA/MT-Bench/etc.) are too small and stateless to exercise the **structured-pruning (G19)** and
**dedup (G22)** levers on a first-ask — the levers only bite on bulky, repetitive real-world payloads.
Added one **disclosed, production-shaped** `ops` profile (10 verbose DevOps/support items — pasted
JSON, logs, config — in `ops_seed.jsonl`, adapted from the single-arm harness and flagged in
`DATA_LICENSES.md` + `public_dataset.meta.json` as **not** a recognized benchmark). It is relative
facts-gated, reads ~44%, and lifts the combined cold prose lever to ~8%, so the illustrative full-mix
blend now prints **~34%** (was ~33%). In the same pass, an experimental agentic tool-catalogue
*enrichment* was **reverted** after calibration proved it was dominated by the un-enriched baseline
(it only raised the agentic number by over-pruning tools the model needed) — the agentic lever stays
on verbatim BFCL catalogues at its honest live-reproducible **~20%** (run-variable 19–25%). Nothing is
tuned to hit a target: the harness reports whatever it prints. 50 A/B unit tests green.
- **OSS:** new `ops` dataset profile + builder; `run.sh` agentic pin restored to `max_tools_per_agent: 20`; README/benchmark-README/`run_ab.md`/`DATA_LICENSES.md` refreshed to the calibrated ~34% blend.

### A/B benchmark: refresh retired provider model ids so --providers all runs clean — Bug fix
Three providers in the A/B harness pointed at model ids retired from their first-party API, so a
`--providers all` run would have failed on them. Refreshed the `run_ab.py` provider map to current GA
ids (grounded against official pricing pages): **anthropic** `claude-3-5-haiku/sonnet-20241022` →
`claude-haiku-4-5` ($1/$5) + `claude-sonnet-5` ($3/$15); **gemini** `gemini-1.5-flash/pro` →
`gemini-2.5-flash-lite` ($0.10/$0.40) + `gemini-2.5-pro` ($1.25/$10); **xai** `grok-2-latest`/
`grok-3-mini` → a single `grok-4.3` ($1.25/$2.50, EU-safe; no cheap "mini" successor exists). Added
priced rows for the new ids, kept the retired rows as historical reference under a new `retired` list,
and added a guard test asserting the provider map never targets a retired id. The six working providers
(openai, azure, bedrock, mistral, groq, cohere) and the deprecated-but-servable ids (o4-mini,
deepseek-chat) are unchanged. Cost-estimate table only — token savings unaffected.

### A/B benchmark: re-grounded prices.json against live vendor pricing — Bug fix
Reconfirmed every row in `examples/benchmark/prices.json` against the official vendor pricing pages
(the file's cost estimate prices both A/B arms identically, so drift skews the reported cost saving).
Three rows had drifted and are corrected: **Mistral Large** `$2.00/$6.00 → $0.50/$1.50` (Large 3
repricing), **Mistral Small** input `$0.20 → $0.15`, **DeepSeek chat** `$0.27/$1.10 → $0.14/$0.28`
(the slug now maps to `deepseek-v4-flash`). The other 15 rows verified unchanged. Added a
`deprecations` block flagging eight 2024-era ids that are now retired/deprecated on their first-party
API (Claude 3.5 Haiku/Sonnet, Gemini 1.5 Flash/Pro, grok-2/grok-3-mini, o4-mini sunsetting, deepseek-chat)
— their prices are kept as last-published historical constants so an unknown-model lookup never
hard-errors mid-run, but they're explicitly marked not-currently-servable. Bumped `as_of` to 2026-07-27
and refreshed the moved OpenAI/Anthropic/Mistral source URLs. Token savings are unaffected (this is the
cost-estimate table only).

## 2026-07-26

### A/B benchmark: production-realistic RAG corpus + honest two-number headline — Enhancement (OSS)
Closes the public A/B harness's honesty loop. The `rag` profile now draws from **HotpotQA (distractor)**
verbatim (CC BY-SA 4.0) — 10-paragraph multi-document contexts (~1–2k tokens) that look like real RAG,
replacing the too-small SQuAD snippets — and the whole shipped `public_dataset.jsonl` is now a **real
Hugging Face build** (`build_source: "huggingface"`), not a fixture placeholder. Fixed a local-only
G01 miss where the LLMLingua sidecar URL used the deployed name (`llmlingua-svc`) that has no DNS in
the compose stack, plus raised G00 burst headroom + a `call_proxy` 429 retry so the cache burst isn't
throttled. **Calibration finding (disclosed):** the cold/prose floor is genuinely ~2–4% — this is
*parity* with the 54.1% methodology (which also runs `compress_user_messages: false`), not a defect —
so the README now leads with a **per-workload reproducibility map** (cache ~90% · agentic ~25% · prose
~2–4% · reasoning ~0%, each independently runnable) plus a **disclosed illustrative-mix blend** (~33%
at balanced weights, `--weights` tunable), shown alongside — and explaining the honest gap to — the
internal 54.1%. No weight is tuned to hit a target. Docs (`run_ab.md`, benchmark README, DATA_LICENSES)
reconciled from the retired `--mode cold/replay` model to `--workload standard/cache/agentic/full`.
- **OSS:** `build_public_dataset.py` (HotpotQA normaliser + yes/no filter), real HF dataset artifacts,
  run.sh G01 sidecar + G00 headroom pin, `run_ab.py` 429 retry, root+benchmark READMEs + run_ab.md +
  DATA_LICENSES, unit tests (46). Marketing: *"Don't take our headline on faith — run the benchmark and
  reproduce each savings lever yourself (caching, agentic tool-use, prose) on recognized public data,
  measured against the provider's own token bill."*

### A/B benchmark: agentic workload — multi-turn tool-loop A/B on recognized BFCL tasks — Enhancement (OSS)
Adds a real **multi-turn agentic** lever to the public A/B harness. `run_ab.py` gained an N-turn tool
loop (`run_episode`) that round-trips `tool_calls` on **both** arms — direct-to-provider and through the
proxy — executing tools locally and summing **provider-billed tokens across the whole episode** (the
honest agentic unit). New `--workload agentic` runs `agentic_dataset.jsonl`: 15 tasks built from
**BFCL v3 multi_turn** (Berkeley Function Calling Leaderboard, Apache-2.0) — real tool schemas (18–39
tools/task) + the first user turn verbatim — reproducible via `build_agentic_dataset.py`. The launcher
pins G16 tool-catalogue pruning + system-prompt cap; a **relative tool-trajectory gate** flags any tool
the proxy dropped that the direct arm called. Smoke at `max_tools=20`: **~29% token savings, trajectory
5/5 preserved**. Honest scope: this reproduces the **tool-pruning** lever (G08/G16) only — G14/G15
tool-*output* projection is response-side (fires on pre-baked embedded results) and **cannot** be
reproduced by a live loop, so it is not claimed here (disclosed in `DATA_LICENSES.md` + item provenance).
- **OSS:** `run_ab.py` (`run_episode`, `--workload agentic`, `relative_tool_gate`, tools on both arm
  calls), `build_agentic_dataset.py` + `agentic_dataset.jsonl`, run.sh G16 agentic pin, BFCL license
  entry, unit tests. Marketing: *"See the agentic savings for yourself — the harness runs real
  multi-turn tool-using tasks through the proxy and measures the provider's own token bill, then checks
  the proxy never dropped a tool the task needed."*

### A/B benchmark: reproduce the cache lever + per-workload transparency — Enhancement (OSS)
The public A/B harness now lets a verifier **reproduce the cache-savings lever themselves** instead of
only seeing a low single-shot blend. New `--workload cache` runs a **disclosed** warm-cache burst
(each cacheable item once cold, then N verbatim repeats — default 9, i.e. 90% warm, tunable via
`--cache-multiplicity`) with the exact-cache lever isolated (`x_cache_semantic:false`), so it lands
~90% token savings **with 0 quality loss** (verbatim repeats hit their own answer, no semantic
collisions). The console now also prints a **per-profile breakdown** under every slice (rag/chat/code/
reason/swe), so results show *where* savings come from. A new checked-in `cache_schedule.json` +
`meta.cache_burst` disclose the repeat multiplicity; `replay_schedule.json` and the dataset sha are
byte-identical (unchanged). Aggregation is now slice-driven so future workloads plug in uniformly.
- **OSS:** `build_public_dataset.py` (`build_cache_burst_schedule`, `--cache-multiplicity`), `run_ab.py`
  (`--workload standard|cache`, slice-driven `aggregate`/`render`, per-profile rows), `cache_schedule.json`,
  unit tests. Marketing: *"Run the cache-savings lever yourself — a disclosed high-repeat traffic burst
  shows ~90% token savings with zero quality loss, and every run breaks the number down by workload so
  you see exactly where the savings come from."*

## 2026-07-25

### A/B benchmark: trustworthy cold floor — flush the key's real tenant + bypass cache on the cold pass — Bug fix
Two fixes so the A/B **cold floor** is a true stateless-optimisation number:
1. **Flush the tenant the KEY actually runs under.** `run.sh`/`run.ps1 --ab` always flushed the label
   tenant (`bench`), but an admin key honours our `X-Tenant-ID` while a **non-admin** key (e.g. a real
   business tenant's `tok-…` set as `PROXY_API_KEY`) ignores it and runs under the key's OWN tenant —
   so the flush missed the real namespace and cold mode read stale cache hits. The launchers now
   resolve the effective tenant from the key hash against whichever store is live (OSS
   `config/local-keys.json` blob or commercial Postgres `proxy_keys`) and flush that namespace — no
   manual `BENCHMARK_TENANT` override.
2. **Bypass G05 on the cold pass.** Cold now runs each item with `x_no_cache` (G05 fully bypassed), so
   same-context near-duplicates (e.g. several SQuAD questions on one passage) can't collide in the L2
   semantic cache — which was both *inflating* cold savings and *serving a neighbour's answer* (the
   spurious cold "fact drops"). `run_ab.py` now runs **two clean passes** (cold = caching off/no
   residue, replay = caching on) with the direct arm memoised (temp 0 → pass-independent, never billed
   twice); replay stays flush-free so the same design still works against a live remote proxy.

### Publicly-verifiable A/B benchmark + tenant self-verify (proxy vs direct, 10 providers, recognized datasets) — Enhancement (OSS)
The public `examples/benchmark/` measured the proxy's *own* `_token_opt` counterfactual — easy to
dismiss as "the proxy grades its own homework." Added `run_ab.py`, a **true A/B**: every request is
fired once **direct to the provider** (via litellm) and once **through the proxy**, compared on the
**provider's own billed usage**, priced identically from a checked-in dated `prices.json`. Dataset is
**recognized public standards used verbatim** (SQuAD v2 / MT-Bench / SWE-bench Lite / HumanEval /
GSM8K; `build_public_dataset.py`, licenses in `DATA_LICENSES.md`), reported as **two numbers** — a
cold standard-order **floor** and a realistic-replay **ceiling** — across **all 10 first-class
providers** (auto-detected by configured keys, per-provider spend caps, OpenAI-only default under $1).
Onboarded tenants can preview savings against their **live** proxy with one command via `verify.sh`
(remote, no Docker, auto-venv; always a true A/B so it needs the tenant's own provider key; only the
bundled public dataset is sent). Non-savings measurement tooling → the pitch-test-plan harness and the
calibrated single-arm 57.1% path are untouched. Marketing: *"Don't take our word for the savings —
run a true A/B against real provider bills over standard public datasets, across 10 providers, for
under a dollar; onboarded tenants can preview it against their own live proxy in one command."*
- **OSS:** `run_ab.py` + `build_public_dataset.py` + `verify.sh`/`verify.ps1` + `--ab` launcher mode + checked-in dataset/prices + unit tests; root README "Verify it yourself" + `docs/client-onboarding.md` "Verify your savings before going live". The checked-in dataset is now the **real** Hugging Face build (100 items, `build_source: huggingface`) — the `--hf` loader was fixed to use canonical dataset ids (`openai/openai_humaneval`, `openai/gsm8k`, `HuggingFaceH4/mt_bench_prompts`) under `datasets` 5.x. `run.sh`/`run.ps1 --ab` auto-export every `LLM_KEY_*` (+ azure/bedrock extras) from `.env` so `--providers all` fans out across a multi-key `.env`, and read a fixed `PROXY_API_KEY=tok-…` from `.env` (nothing passed at runtime); full CLI/keys/local-vs-GCP reference in `examples/benchmark/run_ab.md`.

## 2026-07-24

### Operator Console redesign — tabbed layout, per-tenant drawer, invoices & trust-safety surfaced — Enhancement [Enterprise]
The operator console (`/adminconsole`) was one long vertical page: clicking a tenant's Users /
Trial / Inspect opened a panel appended far below the fold, so an action looked like it "did
nothing," and any failure surfaced only in a top-of-page banner the operator had scrolled past.
Redesigned into three top-level tabs (**Tenants · Observability · Billing**); each tenant row is
now a single **Manage** button that opens a right-side **drawer** with sub-tabs (Overview / Users
/ Trial / Security / Audit / **Danger zone**), keeping the table in view and showing errors next
to the action. Destructive actions are disambiguated — **Revoke keys** (keeps data) vs
**Offboard** (irreversible GDPR erase) — and the two independent holds (contract vs key
suspension) are grouped with plain-language help. Three operator capabilities that previously had
no console surface are now exposed so customers aren't impacted: the **all-tenant invoice run**
(Billing), the **cross-tenant trust-&-safety summary** (Observability), and **BYOK key
re-encryption** after a master-key rotation (Billing). No backend/API changes — all endpoints
already existed. Marketing: *"A faster operator console: manage any tenant from one focused panel,
run invoices and trust-&-safety reports in a click, and rotate encryption keys without a customer
outage."*
- **[Enterprise]:** operator-console UX + newly surfaced invoice / trust-safety / BYOK-rotation controls — <https://tokenlean.cbeyond.cloud/>

### Declarative per-tenant routing rules for G06 — Enhancement (OSS + Enterprise)
G06 now supports **declarative routing rules**: deterministic, in-proxy policy that pins a
matched traffic segment to a tier or specific model — evaluated below a caller's per-request
`x_complexity` override and above the complexity classifier, first-match-wins by `priority`.
Rules match on keywords/regex, prompt-token size, requested model, tool presence, header tags
(`X-Team` → `x_team`), or user id, and can pin a tier/model and/or override strategy knobs for
just that traffic. A rule-selected model still passes the existing cost-floor (never routes
above the caller's model) unless the rule sets `allow_escalation`. Default is empty (`rules: []`)
— a no-op that leaves the classifier and the published savings baseline byte-identical.
Marketing: *"Route by policy, not just heuristics — pin any traffic segment to a tier or model
with deterministic, per-tenant routing rules, and dry-run them before they go live."*
- **OSS:** the rules engine + config authoring (`groups.G6_routing.rules`, per-tenant via config), evaluated in-proxy with cost-floor protection by default.
- **[Enterprise]:** a portal **Routing** tab (structured editor, server-side validation, atomic per-tenant saves, a rerouted-traffic audit, and a no-LLM dry-run tester) — <https://tokenlean.cbeyond.cloud/>

## 2026-07-23

### Cost-routing cascade could serve a truncated answer — cap externalised, truncation now retried — Bug fix
The G06 execution cascade's cheap-tier probe injected a hardcoded 512-token output cap on
requests that carried no `max_tokens`, and three compounding paths could then serve that
truncated answer as final: a length-stopped probe scored low confidence but a cost-blocked
tier-2 hop aborted the whole cascade (never considering tier-3 — often the caller's own
model), so the mid-sentence answer shipped. Fixed: the cap is now configurable
(`cascade_tier1_max_tokens`, `0` = don't inject; caller-supplied `max_tokens` always wins),
a blocked tier-2 hop falls through to evaluate tier-3 against the same cost guards, and a
tier-1 answer truncated by the injected cap is retried once uncapped before serving
(`cascade_retry_uncapped_on_truncation`). Cost estimates also externalised
(`expected_output_tokens_estimate`).

### G19 log compression could silently drop a recurring error — dedup is now severity-aware — Bug fix
G19's log compressor stripped timestamps before comparing lines for duplication, so a genuine
*second occurrence* of the same error (identical text, different timestamp — e.g. an alert firing
twice 61 seconds apart) landed in the same bucket as repeated INFO/DEBUG heartbeat noise and was
silently collapsed behind an opaque "[N duplicate log patterns suppressed]" footer that named no
pattern. For log-heavy incident-investigation workloads, that erased exactly the signal an SRE
cares about (is this a one-off or is it flapping?). Fixed: lines matching a configurable severity
list (`always_keep_severities`, default `ERROR, FATAL, CRITICAL, PANIC`) are now never folded into
the dedup count — every occurrence survives verbatim with its own timestamp; only lower-severity
boilerplate still collapses. Found via the pitch-test-plan quality gate's stronger-judge escalation
(2026-07-23 mode-100 pre-flight) on a DevOps incident-response dataset.

## 2026-07-22

### Docs-chat corpus refresh is now a 3-step publish loop — generate, review, apply — Enhancement [Enterprise]
Keeping the portal chatbot's knowledge base current after a feature push is now one command per step. `--generate` drafts doc updates from the feature diff (review-gated, as before, and now records new-doc titles for the publish step). After the operator reviews the drafts — editing accepted ones in place and deleting rejected ones — a new **`--apply`** mode publishes everything that survived review in one shot: copies drafts over the live docs, registers new docs in the manifest with the recorded title (H1 fallback), bumps `docs_version` so every tenant's cached chat answer invalidates atomically, cleans up the draft directory, and immediately runs the delta-sync ingest into the vector store. Replaces the previous manual step (hand-copying files, editing the manifest, bumping the version, running `--sync` separately). Marketing: *"Refresh your support chatbot's knowledge base after every release with a generate → review → publish loop — drafts stay human-gated, publishing is one command."*
- **[Enterprise]:** the docs-chat corpus tooling ships with the managed portal — <https://tokenlean.cbeyond.cloud/>

## 2026-07-21

### A3 output-holdout cohort stayed stable across measurement arms — Bug fix
The G11 output-shaping A/B holdout assigns each workflow a sticky cohort (treatment vs. control) keyed on `workflow_id`. The ablation harness scopes that id per measurement arm (`<id>::<arm>::<token>`), which would have let one workflow drift between cohorts across arms and corrupt the treatment-vs-control comparison. `_assign_cohort` now keys on the original id by stripping the harness suffix — a no-op for production traffic, where `workflow_id` never contains `::`. Latent until now (the holdout is off by default); fixed ahead of enabling it.

## 2026-07-20

### Semantic cache could serve an answer produced under a different system prompt — Bug fix
The G05 L2 semantic cache embeds **user turns only** — deliberately, because a long system prompt would dominate and truncate the embedding window and collapse distinct questions onto one point. The side effect was that the cache key was blind to the system prompt: the same user question asked under a **restrictive** system prompt could be served an answer cached under a **laxer** one, silently bypassing the scope, persona, or output-format constraint that prompt encodes. Caught by the ablation harness (DS8), where the baseline correctly declined off-topic questions while the cached arm answered them at 0.95–0.96 similarity without calling the model. Isolation was never broken across tenants (`tenant_id` has always been in the L2 filter) — this bit a single tenant running several personas, apps, or agents through one key. Fixed by folding a **system-prompt fingerprint into the cache scope** rather than into the embedding, so the vector still keys on query intent with no truncation regression: set `groups.G5_cache.cache_scope` to `tenant+system` (or `tenant+model+system`), globally or per tenant. The default `tenant` scope is unchanged and keys stay byte-identical, so upgrading invalidates nothing. Marketing: *"Cache scoping can now include the system prompt, so an assistant with a restricted scope is never served an answer generated under a different one."*

### Per-tenant free trials — days and requests, whichever first — Enhancement (OSS + Enterprise)
Enterprise prospects can now be put on a real production free trial limited by **N days AND M served requests, whichever is hit first** (either dimension optional). The counting basis is exactly the billable unit — every served 2xx, cache hits and bypasses included — so trial usage previews precisely what a paid invoice would count, and trial-period requests are flagged and **excluded from invoices** ($0 for a trial-only period). On expiry the proxy returns a clean **HTTP 402 `trial_expired`** (not billed, doesn't consume allowance) until an operator converts or extends. Operators start / set / extend / convert / cancel a trial per tenant from the admin console at runtime (no redeploy; effective within ~60 s), with an audited operator actor and a fleet view of trials that are active / expiring / awaiting action. Tenants see their own trial burn-down and 80/90% banners in the portal, and both thresholds plus expiry emit optional `trial.threshold` / `trial.expired` webhooks. Enforcement is OSS-core and default-off, so the self-host tier and the reproducible savings baseline are byte-identical. Marketing: *"Give prospects a real production trial — a configurable number of days or requests on the full optimisation pipeline, with automatic 80/90% warnings, webhook alerts, a clean cut-over to paid, and trial traffic never billed."*
- **OSS:** the free-trial gate (G00 `_check_trial` + `trial.threshold`/`trial.expired` event types), the `usage_events.trial` billing exclusion, and the tenant-facing `/portal/trial` status all ship in every tier (default-off).
- **[Enterprise]:** the admin-console trial lifecycle (start/extend/convert/cancel + audit), the fleet trials tile + per-tenant badge, the portal trial card/banner, and webhook delivery — <https://tokenlean.cbeyond.cloud/>

### Webhook/G06 code-review fixes — SSRF, notification misattribution, cross-tenant routing state — Bug fix
A multi-angle code review of the OmniRoute enterprise-roadmap work (response headers, G31 PII pass, per-model lockout, outbound webhooks, G06 routing strategies — shipped 2026-07-19) surfaced ten confirmed defects, all fixed: (1) **SSRF** — the webhook `_validate_url` only checked the `https://` scheme, so a tenant could register an endpoint pointed at the cloud metadata service or an internal address, and the `/test` button served as an on-demand probe. Now reuses the existing `intent_orchestration.validate_outbound_url` host check at both registration and delivery time. (2) The outbound `guardrail.block`/`pii.detected` webhook payloads picked their `categories`/`action` fields by "whichever is non-empty" rather than by which guardrail actually fired — a non-blocking G30 flag could mask a real G31 block and misreport severity to a SIEM. Now attributed to whichever guardrail(s) actually triggered, with block > mask > flag severity precedence. (3) G06's `least_latency` strategy fed its EWMA from every LLM call outcome including failures — a model failing fast looked "fast" and got preferentially routed to, undermining the sibling per-model-lockout feature. Now gated on a genuine success. (4) G06's round-robin state was a single process-global counter keyed only by tier name — two tenants configured differently for the same tier perturbed each other's rotation. Now tenant-scoped. (5) A G29/G31 PII **block** was mislabeled `redaction.applied` in the audit trail and SOC2 attestation, implying content was masked and served when the request was actually refused — added a distinct `redaction.blocked` action, threaded through the portal security-events taxonomy and the attestation evidence pack's new `pii_blocks` counter. Plus five lower-severity fixes: no new HTTP client per webhook delivery retry; a Redis outage no longer silently disables the webhook dead-letter queue (now logged); the duplicated hash-bucket formula (G06 canary/weighted vs. G11's A3 holdout) is now one shared `stable_bucket` helper; and the `least_latency` EWMA smoothing factor is a hot-reloadable config knob instead of a hardcoded constant. 90+ new/updated unit tests.

### F1/F2/F3 code-review fixes — SSRF, savings misattribution, cache/routing correctness — Bug fix
A multi-angle code review of the F1 learning loop / F2 intent orchestration / F3 agent registry console (shipped 2026-07-19) plus the savings-header fix surfaced ten confirmed defects, all fixed: (1) **SSRF** — a registered agent `url` reaching the cloud metadata service or an internal address is now rejected (literal-IP check, both at registration and dispatch); (2) an F2-dispatched request with no `usage` block in the agent's response could misreport ~100% savings — `final_tokens_sent`/`proxy_optimised_tokens` are now set on dispatch, mirroring the pipeline's own accounting step; (3) `routed_model` now reflects the agent that actually served the request, not G06's now-skipped pick, fixing both billing pricing and the `x-tokenlean-routed-model` header; (4) G05 no longer caches an agent-dispatched answer (it was being replayed on later matching prompts, bypassing intent classification entirely); (5) a learned F1 rule scoped to the post-routing model could never match under G06 tiered/cascade routing — G24 now runs a second, narrower pass right after G06; (6) an agent's `timeout_seconds` is now capped (300s) so a hanging agent can't tie up a request indefinitely; (7) the batch-results poller (`GET /v1/batch/results`) now attaches best-effort `x-tokenlean-*` headers too, closing the last gap the header fix didn't cover; (8) the Agents-tab save now writes via an atomic single-key `jsonb_set` instead of a read-modify-write, closing a lost-update race with the Groups tab; (9) the portal now detects and surfaces a static config.yaml tenant override that would silently make an Agents-tab Save have no effect on live routing; (10) the F1 miner's GCS mirror now uploads to the configured `rules_file` path instead of a hardcoded literal, plus an atomic (write-then-rename) local write. 60+ new/updated unit tests.

### Tune every optimisation from the portal — full toggle + savings-vs-quality knob coverage — Enhancement [Enterprise]
The portal **Optimisation Settings** tab now exposes an enable toggle and the key savings-vs-quality dials for **every** implemented step, closing two gaps: **G00 Rate Limiting** (enable + requests/minute, requests/hour, monthly-quota) and **G31 Context Trust** (RAG/indirect-injection + retrieved-PII policy) were previously invisible in the UI. Existing groups gained their primary missing dials — G01 user/system-prompt compression toggles, G05 semantic-cache TTL + scope, G06 routing strategy, G07 retrieved-context budget, G11 output validation, G13 TOON + native-batch, G16 tool-selection, G29 PHI, G30 response scanning. Trust & safety groups (G29/G30/G31) are now **operator-safe**: tenants tune the policy mode/threshold but the hard on/off — including the specific `mode` values (`off`/`allow`) that are functionally equivalent to disabling the group — is operator-only, so security can't be silently switched off by any route, and a rejected field in the save response tells the caller exactly what didn't apply. A self-healing migration clears any legacy per-tenant override that had disabled a safety group before this lock existed, so no tenant is left stuck. Every knob stays whitelisted + clamped server-side and hot-reloads within ~60 s. Marketing: *"Tune every optimisation for savings vs. quality from one dashboard — with safety controls locked to your operators."*
- **[Enterprise]:** the portal catalog + operator-locked safety toggles (mode-value bypass closed) + legacy-override self-heal + readiness coverage probe — <https://tokenlean.cbeyond.cloud/>

### Portal toggle correctness — off-by-default groups, inert G27/G20 knobs, dead G20 key — Bug fix
Three portal fixes surfaced during the coverage audit: (1) the settings UI showed a group's toggle as **ON** whenever the base config omitted `enabled`, even though most stages default OFF — the toggle now mirrors each stage's real default (ON only for G24/G29/G30/G31). (2) G27's image `quality`/`min_bytes` knobs were displayed but never passed to the compressor — they are now forwarded when the installed optimiser accepts them (signature-checked, so legacy builds are unaffected). (3) G20's catalog key was `G20_prompt_optimization`, but the middleware reads `g20_prompt_optimizer` — so the G20 toggle never took effect; the key is corrected and G20's offline-only knobs (which never applied per-request) were removed from the tenant UI.

### Deterministic prose compression + terse-output steering — three new savings levers — Enhancement (OSS)
Three opt-in, default-off savings features built on a new zero-LLM, zero-latency prose compressor (`prose_compress.py`) that strips filler/hedging/pleasantries while preserving code, URLs, paths, identifiers and version numbers **byte-for-byte** (regex engine ported from caveman-shrink, MIT — attribution in `docs/oss-licenses.md`). (1) **G08 tool-description compression** trims the prose in tool/function `description`s, which ride *every* agentic request and were previously passed verbatim (`G8_tools.compress_descriptions`). (2) **G01 deterministic fallback** engages only when the LLMLingua sidecar (and Kompress) reduced nothing — so a compression outage degrades to *some* savings instead of pass-through (`G1_compression.deterministic_fallback`). (3) **G11 terse-output steering** ships bundled `lite`/`full`/`ultra` presets that steer the model toward shorter answers — the biggest uncovered savings axis, since the 54.1% headline is input-only and output tokens cost far more per token — with safety carve-outs keeping security/destructive-action text in normal prose, and the active level folded into the G05 cache key so terse and verbose answers never mix (`G11_output.verbosity_steering.level`). Plus an offline `scripts/compress_prompts.py` to shrink prompt/memory files at rest. All default-off → the reproducible savings baseline stays byte-identical; each is a SAVINGS feature to be proven with a pitch-test-plan quality-gate run before enabling by default. 40+ unit tests (protection invariants, resolver priority, cache-scope, deep-copy isolation, fallback gating).
- **OSS:** all three levers + the shared compressor + the offline script ship in every tier — one-word marketing: *"cut output tokens with a terseness dial, and shrink tool manifests for free."*

## 2026-07-19

### Savings headers now emitted on cache-hit / bypass responses — Bug fix
The per-call `x-tokenlean-*` attribution headers (and the `x-savings-usd` alias) were only attached on the full-LLM / cascade / agent responses — the cache-hit, bypass, and content-filter short-circuits returned a header-less response. That silently broke the advertised always-on FinOps attribution exactly where it matters most (`x-tokenlean-cache` was absent on the highest-volume cache-hit traffic) and failed the deployment-readiness header gate. The header builder is now shared and applied to every served 2xx path, so a cache hit returns `x-tokenlean-cache: hit:<level>`. Streamed responses remain a documented exception.

### Agent Registry Console — declare & govern your orchestration agents from the portal — Enhancement [Enterprise]
A portal **Agents** tab to manage intent-orchestration (F2) without editing config: declare downstream agents (id, OpenAI-compatible URL, intent keywords, optional model / key / output budget), toggle orchestration on/off, and set the match threshold — all self-serve, per-tenant, validated server-side, effective within ~60 s. Plus a **routing-decisions** view — which agent handled each request, joined to model, cost, and latency — for audit and change-control. Persisted in the existing per-tenant config store (no new table); routing decisions are backed by a new `agent_id` column on the usage ledger.
- **OSS:** `usage_events.agent_id` (observability — which agent served a request; never billed) ships in every tier.
- **[Enterprise]:** the portal registry console + routing-decisions audit view — <https://tokenlean.cbeyond.cloud/>

### Intent-based multi-agent orchestration — one endpoint, every agent — Enhancement (OSS + Enterprise)
Point one proxy endpoint at TokenLean and it routes each request to the right **downstream agent** by intent — "refund my invoice" → your billing agent, "the server is down" → your SRE agent — with no routing code in your app. An agent is any OpenAI-compatible chat endpoint you run; register it per tenant with intent keywords and TokenLean forwards matching requests there (its answer still runs response-side groups + billing), falling back to the normal LLM on no match. Opt-in / default-off (no agents registered → byte-identical path), per-tenant isolated (a tenant's agent list never leaks to another), with an optional per-agent output budget. First increment is single-agent routing; multi-intent fan-out follows.
- **OSS:** the orchestration engine — config-driven agent registry, heuristic intent classifier, dispatch + short-circuit — ships in every tier (`orchestration.*`).
- **[Enterprise]:** the managed registry console (declare/govern agents in the portal), routing-decision audit, and a managed ML intent classifier — <https://tokenlean.cbeyond.cloud/>

### Agentic learning loop — the proxy self-tunes per tenant — Enhancement [Enterprise]
A managed background job mines your own `usage_events` ledger and, for each `(tenant, routed_model)`, finds savings-optimisation groups that keep running but realise ≈no tokens — then writes **per-tenant** adaptive-bypass rules into the very artifact G24 already hot-reloads. Within one reload cycle (~60 s) the proxy stops paying for that group for that cohort, with zero engineer effort; bills keep falling as more rules accrue. Conservative by design: opt-in / default-off, a minimum-sample floor, a hard **never-skip denylist** (cache, routing, rate-limit, observability, trust & safety), and any operator-authored rules are always preserved.
- **OSS:** the G24 adaptive-bypass engine that consumes the rules ships in every tier.
- **[Enterprise]:** the managed miner that generates them per tenant, and the portal to review/override — <https://tokenlean.cbeyond.cloud/>

### G06 routing strategies — canary, weighted, round-robin, least-latency — Enhancement (OSS + Enterprise)
G06 gains a `strategy` layer that picks **which model of a chosen tier's list** to use (the complexity classifier still picks the tier; the strategy picks within it). Options: `priority` (**default — the tier's first model, byte-identical to today, so the 54.1%% savings baseline is unchanged**), `round_robin`, `weighted` (`strategy_weights`), `least_latency` (routes to the tier model with the lowest observed served-latency EWMA, fed from real calls), and `canary` (`canary_pct`% to the tier's second model — ramp a new model 5→50→100% and compare cost/quality via the `x-tokenlean-routed-model` header). All strategies are **deterministic** (request-id hash / per-worker counter / EWMA, never random) so the ablation stays reproducible. Per-tenant, opt-in, default off. 14 tests.

- **OSS:** the strategy engine + all five modes ship in every tier (`groups.G6_routing.strategy`).
- **[Enterprise]:** portal strategy config + canary A/B comparison dashboards — <https://tokenlean.cbeyond.cloud/>.

### Outbound event webhooks — push budget/security events to your Slack, PagerDuty, SIEM — Enhancement [Enterprise]
Tenants can register HTTPS endpoints (portal `/portal/webhooks`) to receive **PII-free** TokenLean events in real time: `spend_cap.reached`, `budget.threshold` (a one-shot warning when monthly spend first crosses a configurable `warn_pct` of the cap), `guardrail.block` (G30/G31 injection), and `pii.detected` (G29/G31). Each delivery is **HMAC-SHA256 signed** (`X-TokenLean-Signature`) with a per-endpoint secret shown once at registration and stored Fernet-encrypted; delivery uses bounded exponential-backoff retry with a Redis dead-letter on final failure. The emit seam is OSS core (`events.py`, a no-op without a dispatcher) so the barricade holds; the delivery product + portal CRUD are commercial. Payloads carry event metadata only (counts / entity types / categories) — never content. 24 tests (8 core seam + 6 spend-emit + 10 delivery/CRUD).

- **[Enterprise]:** endpoint registration, signed delivery, retry/dead-letter, and the portal Webhooks surface — <https://tokenlean.cbeyond.cloud/>.

### Per-model lockout — quarantine one degraded model without blacking out the provider — Enhancement (OSS + Enterprise)
The resilience layer gains a third, finer gate alongside the per-provider circuit breaker and per-tenant cooldown: a **per-(provider,model) lockout**. When a single model racks up `model_failure_threshold` model-scoped 5xx/timeout failures, it's skipped on subsequent requests for `model_lockout_seconds` (then one probe re-tests) — so a deprecated or degraded model (e.g. `gpt-4o` flaking while `gpt-4o-mini` is fine) is quarantined and failover routes around **just that model**, not the whole provider. The threshold is deliberately lower than the provider breaker's, so a fallback model's success resets the provider breaker and the provider stays live. Opt-in via `resilience.model_lockout` (default off → provider-breaker behaviour byte-identical); gauge `token_opt_model_lockout_state{provider,model}`. 8 unit + 1 integration test.

- **OSS:** the lockout primitive + config + metric ship in every tier.
- **[Enterprise]:** the SLA-dashboard model-lockout panel + managed alerting on quarantined models — <https://tokenlean.cbeyond.cloud/>.

### G31 now scans retrieved context for PII, not just injection — Enhancement (OSS + Enterprise)
G31 Context-Trust already re-scanned RAG/memory-injected `system`/`tool` context for indirect prompt-injection; it now optionally runs the **same G29 PII engine** over that assembled context too. This closes the gap where a poisoned or PII-laden retrieved document (an SSN in a support ticket, an email in a KB doc) reached the model or cache — G29 runs *before* retrieval, so it never saw it. Opt-in via `groups.G31_context_trust.pii_mode`: `off` (default) / `flag` / `mask` / `block`. Masking here is **irreversible** by design (`[EMAIL]`, no vault) — retrieved PII is never the caller's data to restore, and restoring it would let the model echo it back. Recorded on dedicated `context_trust_pii_*` fields + `token_opt_context_trust_events_total` (category `pii:<ENTITY>`) + a `source:"retrieved"` audit row, kept separate from G29's request-side redaction. DS20 gains a `ctxpii` block-proof; 8 tests.

- **OSS:** the retrieved-context PII pass + `flag`/`mask`/`block` modes ship in every tier.
- **[Enterprise]:** managed medical-NER / Presidio recognisers + the context-quality/trust-safety dashboards over retrieved-corpus PII — <https://tokenlean.cbeyond.cloud/>.

### Per-call savings exposed as `x-tokenlean-*` response headers — Enhancement (OSS + Enterprise)
Every served 2xx response now carries a machine-readable header family so a customer's FinOps/observability pipeline can attribute cost per request **without parsing the body**: `x-tokenlean-routed-model`, `x-tokenlean-cache` (`miss`/`hit`/`hit:<level>`), `x-tokenlean-tokens-saved`, `x-tokenlean-pct-saved`, `x-tokenlean-cost-saved-usd`, `x-tokenlean-latency-ms`, and `x-tokenlean-request-id`. Emitted on the normal and G06 cascade short-circuit paths alike, and carried through unchanged to Anthropic/Gemini clients by the protocol egress passthru. The existing `x-savings-usd` is retained as a back-compat alias of the cost header. Streamed responses are unaffected (documented limitation). Always-on, no config. 6 tests.

- **OSS:** the full `x-tokenlean-*` header suite ships in every tier.
- **[Enterprise]:** portal/dashboard drill-down and FinOps cost-attribution built on the same per-call fields — <https://tokenlean.cbeyond.cloud/>.

## 2026-07-18

### Grounding-coverage metric now emitted live (G07 → response path) — Enhancement (OSS + Enterprise)
The grounding-coverage heuristic shipped earlier today is now **wired to emit**. G07 stashes the injected chunk texts, and once the answer is produced the pipeline computes the fraction of answer sentences supported by the retrieved context and records `token_opt_grounding_coverage{tenant_id}`. No-op for non-RAG requests and tool-call answers; never breaks the response path. This lights up the last dark metric in the application-quality surface. 5 tests.

- **OSS:** the metric emits at `/metrics`.
- **[Enterprise]:** grounding-coverage trends + low-grounding anomaly alerting in the context-quality dashboards — <https://tokenlean.cbeyond.cloud/>.

### PII/PHI ingest masking now runs in the GCP doc-pipeline Job — Bug fix
The opt-in ingest masking shipped earlier today worked locally but **silently no-op'd in the GCP Cloud Run Job** — that container's build context copies only `pipeline.py`, so the `guardrails` engine wasn't importable and the defensive import fell through. The build now stages the 3 public `guardrails` files into the doc-pipeline image (never the commercial `ruleset_feed.py`), so `INGEST_PII_MODE=mask` actually masks before embedding in production. Verified with a local image build. Default off → no behaviour change unless enabled.

### Output JSON-schema validation (G11) — Enhancement (OSS + Enterprise)
When a request asks for **structured output** (OpenAI `response_format` `json_object`/`json_schema`, or a `json_schema` param), G11 now validates the answer is parseable JSON and schema-conformant — closing the malformed-JSON / missing-field gap on the response path. Opt-in via `groups.G11_output.validate_output`: `off` (default) / `flag` (record + annotate, non-mutating) / `repair` (one bounded re-ask — never loops; `repair_fallback: flag|block`) / `block` (withhold with a content-filter 200, not cached). Tool-call and multimodal answers are untouched. Emits `token_opt_output_schema_failures_total`; 11 tests.

- **OSS:** the JSON/schema validator + `flag`/`repair`/`block` modes ship in every tier.
- **[Enterprise]:** `output-reliability` dashboards + anomaly alerting over schema-failure rates — <https://tokenlean.cbeyond.cloud/>.

### Application-quality metrics surface — Enhancement (OSS + Enterprise)
A new metrics module (`middleware/quality_metrics.py`), kept **separate** from the operational/savings metrics (G18) so reasoning-quality is never confused with gateway health. PII-free (labels are `tenant_id` only): **Context Quality** — retrieval hit-rate, chunks-returned, context freshness, and a cheap grounding-coverage heuristic; **Output Reliability** — schema failures, tool-eligibility denials, inline-judge scores. This release wires the retrieval metrics live from G07 (hit or miss) and ships the grounding heuristic tested; the reliability counters are defined for later features to emit. 13 tests.

- **OSS:** the metric emission ships in every tier at `/metrics`.
- **[Enterprise]:** `context-quality` + `output-reliability` dashboards, trends, and anomaly alerting — <https://tokenlean.cbeyond.cloud/>.

### RAG context freshness (ingest timestamps + max-age filter) — Enhancement (OSS + Enterprise)
RAG chunks now carry freshness metadata: ingestion (G03) stamps `ingested_at` (and `source_date` when supplied via `SOURCE_DATE`), and retrieval (G07) can **soft-filter stale context** with `max_age_days`, dropping chunks older than the window before they reach the model. Fails safe: `max_age_days: null` (default) is off, and a chunk with no timestamp is never dropped, so existing corpora keep working. Chunk age is surfaced on the retrieval trace. Config: `groups.G7_retrieval.max_age_days`; 10 tests.

- **OSS:** the freshness stamp + max-age filter ship in every tier.
- **[Enterprise]:** freshness/staleness dashboards + alerting over the retrieval corpus — <https://tokenlean.cbeyond.cloud/>.

### PII/PHI redaction at RAG ingest (opt-in, G03) — Enhancement (OSS + Enterprise)
The ingestion pipeline (G03) can now **mask PII/PHI before a document is chunked, embedded, and stored** — so the vector store never holds raw personal data and G07 can't inject it into a prompt. Scanning the full text before chunking also stops a value split across a chunk boundary from evading the scan. Opt-in via `INGEST_PII_MODE=flag|mask` (default `off`) and `INGEST_PII_PHI=true`; it reuses the same precision-biased OSS `guardrails` engine as G29. An end-to-end test proves the stored chunk payload carries placeholders, not the original PII.

- **OSS:** the ingest-time masking ships in the engine.
- **[Enterprise]:** managed medical-NER recognisers + HIPAA/PCI attestation over ingested corpora — <https://tokenlean.cbeyond.cloud/>.

### PHI detection (opt-in) added to PII redaction (G29) — Enhancement (OSS + Enterprise)
G29 can now detect **health identifiers** as well as PII — US **DEA** and **NPI** numbers (checksum-validated) and, behind a required medical context cue, **MRN** and **ICD-10** codes. It is **opt-in** (`phi: true`) and precision-biased so it does not fire on look-alikes — a bare 10-digit number, an order id, or a version like "B20.1" stays clean. PHI flows through G29's existing `flag`/`mask`/`block` modes and PII-free metrics/audit. Default off. Config: `groups.G29_pii_redaction.phi`; shipped with a false-positive corpus and 20+ tests.

- **OSS:** the checksum/context-gated regex detectors ship in every tier.
- **[Enterprise]:** higher-recall medical NER (Presidio) + HIPAA/PCI policy mapping and attestation — <https://tokenlean.cbeyond.cloud/>.

### G30 response-side injection/moderation scan — Enhancement (OSS + Enterprise)
G30 gained an opt-in **response-side scan** (`scan_response`, default off) that applies the injection engine to the model's **output** — catching a model that echoes an attack payload or emits unsafe instructions a downstream agent might act on. Modes: `flag` (detect + record, non-mutating) or `block` (withhold with a content-filter 200; not cached). Non-streaming responses only; behaviour is unchanged until enabled. New verdict on the existing guardrail metric (`action=response_flag|response_block`). Config: `groups.G30_guardrails.scan_response` / `response_mode`.

- **OSS:** the output-scan engine + static ruleset ship in every tier.
- **[Enterprise]:** the managed moderation ruleset feed (`extra_rules`) raises recall on novel output-safety patterns — <https://tokenlean.cbeyond.cloud/>.

### Malformed OpenAI requests return a clean 400 — Bug fix
The `/v1/chat/completions` (OpenAI) route now validates the request envelope and returns a clean, OpenAI-shaped **400** for a malformed body — a non-JSON body, or `messages` that isn't a non-empty array of role-bearing objects. Previously such requests surfaced as a 500 (or 400'd at the provider); the Anthropic (`/v1/messages`) and Gemini routes already returned a proper 400, so this brings the OpenAI route to parity. The check is envelope-only — semantic validation still belongs to litellm/the provider. 8 tests.

### RAG retrieval fails closed (relevance floor hardening) — Bug fix
Two RAG relevance gaps in retrieval (G07) closed so low-relevance context can't slip into the prompt: (1) the cross-encoder **reranker now fails *closed*** — on error it re-applies the retrieval cosine floor to cosine-scored chunks (RRF-fused chunks keep their fusion ranking, where a cosine floor is meaningless) instead of returning the unfiltered set; (2) the **dense-only Qdrant paths now pass `score_threshold`**, matching the pgvector path, so weak matches are dropped at retrieval rather than relying on the reranker. No config change; strictly more conservative. 4 tests.

### GCP cost-inventory script + teardown status wiring + `--nuke` — Enhancement (OSS)
Operator tooling for cleanly exiting / auditing a GCP deployment:
- **New `scripts/gcp/gcp-running-inventory.sh`** — a read-only, project-wide sweep across all regions of every cost-bearing resource, grouped by cost behaviour (bills-continuously / scale-to-zero / storage) and ending in a two-tier **COST SUMMARY**; exits non-zero if anything bills continuously. Optional `--asset` adds a Cloud Asset Inventory dump.
- **`teardown-gcp.sh` consolidated status** — teardown now ends by running the status + inventory scripts for one post-teardown view (skip with `--no-status`).
- **`teardown-gcp.sh --nuke`** — "exit the project" mode: everything `--full` does **plus** deleting the tf-state and Cloud Build buckets, emptying the project to the GCP floor while keeping the project + KMS key ring (GCP forbids deleting rings, and keeping it lets `terraform apply` reattach on rebuild). Residual ≈ $0.06/mo; rebuildable (infra only — data is not restored); requires typing `nuke`.

### Test-harness doctrine, Security Suite & deploy-readiness gating — Enhancement (OSS + Enterprise)
Clarified and enforced the change-completion doctrine, and expanded deployment verification:
- **Harness routing by feature type.** `examples/benchmark` (and the internal pitch-test-plan) are now savings-validation only — a non-savings change no longer touches them, protecting the calibrated benchmark number and the reproducible savings headline. Non-savings validation (trust & safety, protocols, auth, billing, portal) lives in the deployment-readiness harness.
- **[Enterprise] Security Suite** — a standalone, non-destructive security posture check (auth/authz, endpoint-exposure, BYOK/402, trust-safety engine proof) that also runs as a gating section of the readiness harness — <https://tokenlean.cbeyond.cloud/>.
- **[Enterprise] Deployment-readiness tiers + gating** — `--quick` (cheap deploy gate) and `--full` (deep pre-release) tiers; every deploy auto-runs the quick gate and a NOT-READY verdict blocks it — <https://tokenlean.cbeyond.cloud/>.
- **Commit-time enforcement (OSS):** a change under `src/` must ship a `release-notes.md` entry and a matching `tests/` change, or the commit is blocked (override with `[skip-relnotes]` / `[skip-tests]` tokens). A guard test keeps trust-safety groups out of the savings registry.

## 2026-07-15

### G31 Context-Trust: indirect (RAG) prompt-injection defence — Enhancement (OSS + Enterprise)
New **G31** middleware closes the indirect prompt-injection gap. G30 scans the untrusted user prompt, but retrieval (G07) and memory (G10) append retrieved documents / stored memories into the prompt **after** G30 runs — so a poisoned document could previously reach the model un-inspected. G31 re-scans the *assembled* context (`system` / `tool` roles) with the `guardrails/injection.py` engine, runs non-bypassably right after the G07/G10/G22 stages, and supports `allow` / `flag` (default, non-mutating) / `block` (content-filter 200) / `strip` (drop only the poisoned content). New metric `token_opt_context_trust_events_total{category,action}`. Config: `groups.G31_context_trust`.

- **OSS:** the scanner engine + static default ruleset ship in every tier; default `flag` mode is non-mutating.
- **[Enterprise]:** the continuously-updated managed red-team ruleset feed (via `extra_rules`) and the Security dashboards/console — <https://tokenlean.cbeyond.cloud/>.
