"""Token usage and estimated cost of one review run, read from agentic-ci's OTEL log.

The action's review step runs this with the agentic-ci release that ran the agent, so the
totals and the price map are that release's:

    uvx --from agentic-ci==<version> python -I usage.py LOG OUT --job-name NAME --mode MODE

LOG is the runner's private copy of the log agentic-ci wrote. It came from a directory the agent
could write, so it is untrusted data: only numbers and span ids are read from it, and a record of
the wrong shape is skipped. The script prints the totals for the job log, writes them to OUT as a
small JSON object for review.py's summary line, and appends a code-review-usage span under the
run's root span to LOG, so an MLflow export can group runs by job and mode.

Exit status: 0 when OUT was written, 3 when the log holds no token data (OUT is not written),
1 on any other failure.

Only the harnesses the action runs are covered: codex and claude-code.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass

from agentic_ci import otel
from agentic_ci.cost import estimate_cost

NO_USAGE_EXIT = 3
_SPAN_NAME = "code-review-usage"
_ROOT_SPAN_NAME = "agentic-ci-run"
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
# The job name and mode go into the span as strings; anything longer is cut.
_MAX_LABEL_CHARS = 100


@dataclass(frozen=True)
class ReviewUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    estimated_cost_usd: float | None

    def log_line(self) -> str:
        cost = (
            f"${self.estimated_cost_usd:.4f}"
            if self.estimated_cost_usd is not None
            else "unavailable"
        )
        return (
            f"Input: {self.input_tokens:,} • Output: {self.output_tokens:,}"
            f" • Cache read: {self.cache_read_tokens:,}"
            f" • Cache write: {self.cache_write_tokens:,} • Est. cost: {cost}"
        )

    def as_json(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
        }


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _objects(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _attribute_value(value):
    if not isinstance(value, dict):
        return None
    for kind in ("stringValue", "intValue", "doubleValue"):
        if kind in value:
            return value[kind]
    return None


def _attributes(owner: dict) -> dict:
    attrs = {}
    for item in _objects(owner.get("attributes")):
        key = item.get("key")
        if isinstance(key, str) and key:
            attrs[key] = _attribute_value(item.get("value"))
    return attrs


def _number(value) -> float | None:
    """A finite number of zero or more, or None. Booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _numeric(attributes: dict, *names: str) -> float:
    for name in names:
        number = _number(attributes.get(name))
        if number is not None:
            return number
    return 0.0


def _payload_items(record: dict, path: str, *keys: str):
    """Yield the innermost objects under keys of a record whose path contains path."""
    record_path = record.get("path")
    if not isinstance(record_path, str) or path not in record_path:
        return
    level = [_mapping(record.get("payload"))]
    for key in keys:
        level = [child for parent in level for child in _objects(parent.get(key))]
    yield from level


def _has_incomplete_claude_cost(records: list[dict]) -> bool:
    """Whether an export batch with Claude tokens lacks a cost point for one of its models."""
    for record in records:
        token_models = set()
        cost_models = set()
        for metric in _payload_items(
            record, "/v1/metrics", "resourceMetrics", "scopeMetrics", "metrics"
        ):
            name = metric.get("name")
            if name not in ("claude_code.token.usage", "claude_code.cost.usage"):
                continue
            data = _mapping(metric.get("sum", metric.get("gauge")))
            for point in _objects(data.get("dataPoints")):
                model = _attributes(point).get("model") or "unknown"
                if not isinstance(model, str):
                    model = "unknown"
                if name == "claude_code.token.usage" and _numeric(point, "asInt", "asDouble"):
                    token_models.add(model)
                elif name == "claude_code.cost.usage" and ("asInt" in point or "asDouble" in point):
                    cost_models.add(model)
        if token_models - cost_models:
            return True
    return False


def _has_unpriced_codex_call(records: list[dict]) -> bool:
    """Whether a completed Codex response has neither a direct cost nor a known price.

    Checked per response, since a model's summed cost hides a call that was not priced.
    """
    for record in records:
        for event in _payload_items(record, "/v1/logs", "resourceLogs", "scopeLogs", "logRecords"):
            attrs = _attributes(event)
            if (
                attrs.get("event.name") != "codex.sse_event"
                or attrs.get("event.kind") != "response.completed"
            ):
                continue
            total_input = _numeric(attrs, "input_token_count")
            output = _numeric(attrs, "output_token_count")
            cache_read = _numeric(attrs, "cached_token_count")
            cache_write = _numeric(attrs, "cache_write_token_count")
            if not any((total_input, output, cache_read, cache_write)):
                continue
            if any(
                _number(attrs.get(key)) is not None
                for key in ("cost_usd", "total_cost_usd", "cost", "total_cost")
            ):
                continue
            model = attrs.get("model")
            model = model if isinstance(model, str) and model else "unknown"
            fresh = max(total_input - cache_read - cache_write, 0)
            tier = attrs.get("service_tier")
            cost = estimate_cost(
                model,
                fresh,
                output,
                cache_read,
                cache_write,
                service_tier=tier if isinstance(tier, str) else None,
            )
            if cost is None:
                return True
    return False


def _normalize_metric_ints(records: list[dict]) -> None:
    """Turn OTLP/JSON int64 strings into numbers, for agentic-ci releases that do not."""
    for record in records:
        for metric in _payload_items(
            record, "/v1/metrics", "resourceMetrics", "scopeMetrics", "metrics"
        ):
            data = _mapping(metric.get("sum", metric.get("gauge", metric.get("histogram"))))
            for point in _objects(data.get("dataPoints")):
                value = point.get("asInt")
                if isinstance(value, str):
                    number = _number(value)
                    if number is None:
                        del point["asInt"]
                    else:
                        point["asInt"] = int(number)


def read_records(log_file: str) -> list[dict]:
    records = []
    with open(log_file, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                # A stopped collector can leave an incomplete final line.
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def read_usage(records: list[dict]) -> ReviewUsage | None:
    """Total every model call of the run, subagents included, with agentic-ci's price map."""
    _normalize_metric_ints(records)
    token_totals, cost_totals, _, _ = otel.parse_metrics(records)
    if not token_totals:
        return None
    unpriced = _has_unpriced_codex_call(records) or _has_incomplete_claude_cost(records)
    tokens = defaultdict(float)
    for (_, kind), count in token_totals.items():
        number = _number(count)
        if number is not None:
            tokens[kind] += number
    if not any(tokens.values()):
        return None
    models = {model for (model, _), count in token_totals.items() if _number(count)}
    cost = None
    if not unpriced and models.issubset(cost_totals):
        total = sum(_number(value) or 0.0 for value in cost_totals.values())
        cost = total if math.isfinite(total) else None
    return ReviewUsage(
        input_tokens=round(tokens["input"]),
        output_tokens=round(tokens["output"]),
        cache_read_tokens=round(tokens["cacheRead"]),
        cache_write_tokens=round(tokens["cacheCreation"]),
        estimated_cost_usd=cost,
    )


def _root_span(records: list[dict]) -> tuple[str, str, str | None] | None:
    """The trace id, span id and end time of the run's root span, or None.

    agentic-ci stitches the run's traces under one synthetic root span, which it names
    agentic-ci-run. That one wins; otherwise the last span without a parent does.
    """
    found = None
    for record in records:
        for span in _payload_items(record, "/v1/traces", "resourceSpans", "scopeSpans", "spans"):
            if span.get("parentSpanId"):
                continue
            trace_id, span_id = span.get("traceId"), span.get("spanId")
            if not (
                isinstance(trace_id, str)
                and _TRACE_ID_RE.match(trace_id)
                and isinstance(span_id, str)
                and _SPAN_ID_RE.match(span_id)
            ):
                continue
            end = span.get("endTimeUnixNano")
            end = str(end) if isinstance(end, (int, str)) and str(end).isdigit() else None
            candidate = (trace_id, span_id, end)
            if span.get("name") == _ROOT_SPAN_NAME:
                found = (candidate, True)
            elif found is None or not found[1]:
                found = (candidate, False)
    return found[0] if found else None


def _label(value: str) -> str:
    return "".join(c for c in value if c.isprintable())[:_MAX_LABEL_CHARS]


def append_usage_span(
    log_file: str, records: list[dict], usage: ReviewUsage, job_name: str, mode: str
) -> bool:
    """Attach the totals to the review trace as a child of its root span; False without one."""
    root = _root_span(records)
    if root is None:
        return False
    trace_id, root_span_id, root_end_ns = root
    now = root_end_ns or str(time.time_ns())
    attributes = {
        "ci.job.name": _label(job_name),
        "code_review.mode": _label(mode),
        "code_review.input_tokens": usage.input_tokens,
        "code_review.output_tokens": usage.output_tokens,
        "code_review.cache_read_tokens": usage.cache_read_tokens,
        "code_review.cache_write_tokens": usage.cache_write_tokens,
    }
    if usage.estimated_cost_usd is not None:
        attributes["code_review.estimated_cost_usd"] = float(usage.estimated_cost_usd)

    def value(item):
        if isinstance(item, str):
            return {"stringValue": item}
        if isinstance(item, float):
            return {"doubleValue": item}
        return {"intValue": str(item)}

    span = {
        "traceId": trace_id,
        "spanId": uuid.uuid4().hex[:16],
        "parentSpanId": root_span_id,
        "name": _SPAN_NAME,
        "kind": 1,
        "startTimeUnixNano": now,
        "endTimeUnixNano": now,
        "status": {"code": 1},
        "attributes": [{"key": key, "value": value(item)} for key, item in attributes.items()],
    }
    record = {
        "path": "/v1/traces",
        "payload": {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "code-review"}}
                        ]
                    },
                    "scopeSpans": [{"scope": {"name": "code_review.usage"}, "spans": [span]}],
                }
            ]
        },
    }
    # A stopped collector can leave a final line without its newline, which would swallow this one.
    with open(log_file, "rb") as stream:
        ends_open = False
        if stream.seek(0, 2) > 0:
            stream.seek(-1, 2)
            ends_open = stream.read(1) != b"\n"
    with open(log_file, "a", encoding="utf-8") as stream:
        stream.write(("\n" if ends_open else "") + json.dumps(record) + "\n")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", help="the runner's copy of agentic-ci's OTEL log")
    parser.add_argument("out", help="where to write the totals as JSON")
    parser.add_argument(
        "--job-name",
        default="",
        help="<GITHUB_JOB>-<mode>, the relay upload's job, for the MLflow span",
    )
    parser.add_argument("--mode", default="", help="standard or deep, for the MLflow span")
    args = parser.parse_args(argv)

    # Only the exception's type is printed: its message can quote the untrusted log, and a line
    # of it could start a workflow command.
    try:
        records = read_records(args.log)
        usage = read_usage(records)
    except Exception as exc:
        print(f"Could not read the telemetry ({type(exc).__name__})")
        return 1
    if usage is None:
        print("Review usage unavailable: the telemetry holds no token data.")
        return NO_USAGE_EXIT
    print(f"Review usage: {usage.log_line()}", flush=True)
    try:
        with open(args.out, "w", encoding="utf-8") as stream:
            json.dump(usage.as_json(), stream)
    except (OSError, ValueError) as exc:
        print(f"Could not write the usage totals ({type(exc).__name__})")
        return 1
    try:
        appended = append_usage_span(args.log, records, usage, args.job_name, args.mode)
    except (OSError, ValueError) as exc:
        print(f"Could not add the code-review-usage span ({type(exc).__name__})")
    else:
        if not appended:
            print("No root span in the telemetry, so no code-review-usage span was added.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
