"""What `/metrics` renders when eight workers have each written the same gauge.

`rate_limit_remaining` does not measure a per-worker quantity. It measures **one
shared number in Redis**, sampled by whichever worker happened to serve a query.
The multiprocess mode decides how eight such samples become one rendered value,
and that choice is a correctness decision rather than a formatting one:

* `livemin` reports the smallest live reading — so a stale low sample from a
  worker that served a query during an earlier drain outlives the refill, and an
  operator alerting on "budget exhausted" pages for a tenant who is fine.
* `livemostrecent` reports the newest — which for a single shared quantity
  observed by several processes is the only reading that is ever current.

These tests write directly into the multiprocess sample directory the way real
workers do (one file per pid), then render through the same `render_metrics`
the route calls. No Docker, no second process — the aggregation is the subject,
not the concurrency.
"""

import os
import time
from pathlib import Path

import pytest
from prometheus_client import Gauge, values
from prometheus_client.values import MultiProcessValue

from src.observability import metrics as metrics_module
from src.observability.metrics import (
    MULTIPROC_DIR_ENV,
    RATE_LIMIT_REMAINING_LABELS,
    RATE_LIMIT_REMAINING_NAME,
    multiprocess_enabled,
    render_metrics,
)

CONNECTOR = "github"
TENANT = "tenant_acme"


def sample_value(body: bytes) -> float | None:
    """The one `rate_limit_remaining` series this module writes."""
    needle = f'{RATE_LIMIT_REMAINING_NAME}{{connector="{CONNECTOR}",tenant="{TENANT}"}} '
    for line in body.decode().splitlines():
        if line.startswith(needle):
            return float(line[len(needle) :])
    return None


@pytest.fixture
def multiprocess_dir(tmp_path, monkeypatch):
    """Turn on multiprocess mode for the duration of one test.

    The gauge is a module-level collector built at import time, so the mode it
    was declared with is fixed; what this fixture controls is whether
    `render_metrics` takes the `MultiProcessCollector` path at all.
    """
    directory = tmp_path / "prometheus"
    directory.mkdir()
    monkeypatch.setenv(MULTIPROC_DIR_ENV, str(directory))
    assert multiprocess_enabled(), "the env var is what switches the render path"
    yield directory
    # `prometheus_client` caches the per-process value class globally; leaving it
    # set would make every later test in this session write sample files.
    values.ValueClass = values.MutexValue


def write_as_worker(pid: int, value: float) -> None:
    """Write one worker's reading, the way that worker's process would.

    `MultiProcessValue(pid)` is exactly what `prometheus_client` installs inside
    a forked worker: it names the sample file after the pid, so two calls with
    two pids produce the two files a real deployment produces.

    **Goes through `Gauge.set`, not through the raw value class.** The first
    draft of this test wrote the value directly and every assertion failed with
    "no samples at all" — because `mostrecent` merges on a *timestamp*, and only
    `Gauge.set` records one (`prometheus_client.metrics.Gauge.set` branches on
    `_is_most_recent`). A test that reimplements the write would have declared
    the production mode broken when it is not, or worse, passed while the real
    gauge rendered nothing.

    The mode is read off the production collector rather than repeated here, so
    the two cannot silently diverge.
    """
    values.ValueClass = MultiProcessValue(lambda: pid)
    worker_gauge = Gauge(
        RATE_LIMIT_REMAINING_NAME,
        "Requests left in the tenant's token bucket for a connector.",
        RATE_LIMIT_REMAINING_LABELS,
        multiprocess_mode=metrics_module.rate_limit_remaining._multiprocess_mode,
        # Unregistered: the sample files are what the collector reads, and
        # registering a second collector under this name would collide with the
        # module-level one.
        registry=None,
    )
    worker_gauge.labels(CONNECTOR, TENANT).set(value)
    # `mostrecent` orders by wall-clock timestamp, so two writes inside the same
    # clock tick are not ordered at all. Real workers are milliseconds apart.
    time.sleep(0.002)


def test_two_workers_one_label_set_renders_the_newest_reading(multiprocess_dir) -> None:
    """**The bug.** Worker A served an earlier drain and last saw 1 token left.
    Worker B then served the query under test, after a refill, and saw 6.

    `/metrics` must report 6 — the reading that is current — because the
    envelope that query returned said 6. Under `livemin` it reports 1, and the
    integration test comparing the two fails while both look authoritative.
    """
    write_as_worker(4242, 1.0)  # an older drain, on one worker
    write_as_worker(4243, 6.0)  # the query under test, on another

    rendered = sample_value(render_metrics()[0])

    assert rendered == 6.0, (
        f"/metrics rendered {rendered}, but the most recent observation of this "
        f"bucket was 6.0 — the gauge and the envelope are describing the same "
        f"bucket with different numbers"
    )


def test_a_single_worker_is_unaffected(multiprocess_dir) -> None:
    """The one-worker case must keep working whatever the mode is."""
    write_as_worker(5150, 4.0)

    assert sample_value(render_metrics()[0]) == 4.0


def test_the_newest_reading_wins_even_when_it_is_larger(multiprocess_dir) -> None:
    """A refill must be visible. This is the direction `livemin` gets wrong: it
    pins the gauge to an exhausted reading long after the bucket recovered."""
    write_as_worker(6001, 0.0)
    write_as_worker(6002, 7.0)

    assert sample_value(render_metrics()[0]) == 7.0


def test_the_newest_reading_wins_when_it_is_smaller(multiprocess_dir) -> None:
    """And the other direction: a drain must be visible too, so this is not
    simply `max` wearing a different name."""
    write_as_worker(7001, 7.0)
    write_as_worker(7002, 0.0)

    assert sample_value(render_metrics()[0]) == 0.0


def test_render_falls_back_to_the_plain_registry_without_the_env_var() -> None:
    """One worker, no multiprocess dir: the ordinary path still renders."""
    assert os.environ.get(MULTIPROC_DIR_ENV) is None
    assert not multiprocess_enabled()

    body, content_type = render_metrics()

    assert content_type.startswith("text/plain")
    assert isinstance(body, bytes)


def test_the_sample_directory_is_where_prometheus_client_expects_it(
    multiprocess_dir: Path,
) -> None:
    """A guard on the wiring itself: if the env var and the files disagree, every
    assertion above would pass against an empty directory."""
    write_as_worker(8001, 3.0)

    assert list(multiprocess_dir.glob("*.db")), "no sample file was written"
