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
