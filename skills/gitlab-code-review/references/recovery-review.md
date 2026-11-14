# Recovery and retry review

Use this checklist for changes to recovery, retries, persisted state, artifact
handling or time/resource budgets. Apply the checks relevant to the changed
contracts; do not require every combination or new tests for unrelated edits.
Follow the invoking skill's diff scope, execution permissions, severity rules
and output format. This checklist grants no permission to execute reviewed code.

## Trace contracts across boundaries

Follow each changed field or decision from producer to its real consumers,
serialization/schema, checkpoint and next attempt. Check successful and failed
delivery, interrupted work, temporary feature disables and resumed processing
where those transitions exist. Deferred work must remain discoverable until it
is assessed, delivered or explicitly retired by policy.

At file/state publication boundaries, ask what a caller sees if the operation
fails immediately before or after each changed write. A completed item should
reference complete evidence. Follow the repository's partial-result or rollback
policy; when partial results are retained, keep valid completed items without
publishing missing directories or references to downstream consumers.

## Trace failure classification

Follow exceptions through inner and outer handlers, including sibling paths for
the same operation. Check whether an inner fallback discards the errno, artifact
identity or cause that a new outer handler promises to retain. Distinguish local
I/O failures, corrupt input, transport errors and genuine negative results using
the repository's contracts. Check exception subclasses and provenance before
treating their fields as OS diagnostics: filename, strerror and other structured
fields can carry request text or signed URLs even when exception text is omitted.
Useful diagnostics must respect existing secret and capability-URL redaction rules.

## Check identity and budgets across attempts

Compare accepted aliases and equivalent identifiers at every eligibility,
lookup and grouping decision. For revisions or deduplication keys, compare the
same payload retried in different work directories, temporary paths or job
attempts. Incidental runtime paths should not create new evidence; meaningful
artifact or outcome changes must still be distinguishable. Follow the resulting
identity through checkpoint filtering and delivery, rather than stopping at the
hash helper.

For nested budgets, consider just before, at and after each limit, including a
remaining aggregate allowance smaller than one operation's allowance. Verify
whether exhaustion should defer work, report incomplete coverage or fail the
operation, and whether the next attempt can still find that work.

## Complete the review before reporting

For each relevant contract, identify the reachable caller/state, the protection
or regression test, and a concrete counterexample if it fails. A green suite
establishes only the behavior its assertions exercise. Missing checklist items
alone are not findings; apply the invoking skill's evidence and severity rules.

After a correction, inspect the accumulated committed diff and other instances
of the violated contract, including error paths introduced by the fix. Keep
reports focused on distinct, evidenced bugs within the permitted diff scope.
