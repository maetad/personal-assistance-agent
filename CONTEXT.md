# Personal Tracking

Tracks what the user reports about themselves — activities, metrics, plans — and derives both a full history of what was said and a single current-known state per tracked thing.

## Language

**Observation**:
An immutable record of what the user reported, and when. One Observation is created per relevant chat turn; it is never updated or deleted. Stored in `structured_logs`.
_Avoid_: Log entry, structured log, entry

**Fact**:
The single current best-known value for one tracked attribute of a profile (e.g. current weight). Exactly one Fact exists per (profile, fact key); a new Observation for the same fact key overwrites it. A Fact always carries a pointer back to the Observation that last set it.
_Avoid_: Current value, latest reading, snapshot

**Fact Key**:
The curated, per-profile identifier a Fact is upserted against (e.g. `weight`, `blood_pressure`). The classifier is constrained to pick from this set; it is never invented freely, which is what keeps a Fact's "exactly one current answer" guarantee reliable. A profile can add one directly via the `add_fact_key` chat tool, which commits immediately (no confirmation step) since the profile explicitly asked for it.
_Avoid_: log_type (when talking about Facts), tag, category

**Fact Key Proposal**:
A suggested new Fact Key raised when the classifier notices, on its own, that an Observation looks Fact-shaped but doesn't match any existing Fact Key. Stored in `fact_key_proposals` with a `pending`/`confirmed`/`declined` status. Unlike an explicit `add_fact_key` chat request, this is unprompted, so it's surfaced to the user for confirmation on a later turn (via a `pre_llm_call` context injection) rather than the turn that raised it; only becomes a real Fact Key if confirmed via `add_fact_key`, and `decline_fact_key_proposal` leaves the taxonomy unchanged.
_Avoid_: pending fact, unmatched observation
