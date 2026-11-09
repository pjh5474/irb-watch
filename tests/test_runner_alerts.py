from irb_watch.services.runner import MANUAL, SCHEDULED, ScanRunner
from irb_watch.services.scan import OK, ScanSummary
from tests.fakes import FakeAudit
from tests.test_runner import NOW, FakeScans, src


class Hooks:
    def __init__(self, fail=False):
        self.reports = []
        self.evaluated = []
        self.fail = fail

    def after_run(self, report):
        self.reports.append(report)
        if self.fail:
            raise RuntimeError("mail service exploded")

    def evaluate_source(self, source_id):
        self.evaluated.append(source_id)


def make(sources, hooks, scans=None):
    return ScanRunner(
        lambda: list(sources),
        lambda sid: next((s for s in sources if s.id == sid), None),
        scans or FakeScans(sources),
        FakeAudit(),
        lambda sid, due: None,
        lambda: NOW,
        alerts=hooks,
    )


def test_the_alert_hook_runs_after_a_full_run_with_the_report_and_the_scan_counts():
    sources = [src(1)]
    scans = FakeScans(sources, outcomes={1: ScanSummary(1, OK, new=3, updated=2)})
    hooks = Hooks()
    report = make(sources, hooks, scans).run_all(SCHEDULED)
    assert hooks.reports == [report]
    assert report.summaries[1].new == 3 and report.summaries[1].updated == 2


def test_a_failing_hook_never_fails_the_run():
    sources = [src(1), src(2)]
    hooks = Hooks(fail=True)
    report = make(sources, hooks).run_all(MANUAL)
    assert set(report.scanned) == {1, 2}  # the scans still happened and are reported
    assert len(hooks.reports) == 1


def test_the_lock_is_released_even_when_the_hook_fails():
    sources = [src(1)]
    runner = make(sources, Hooks(fail=True))
    runner.run_all(MANUAL)
    assert runner.is_running() is False
    assert set(runner.run_all(MANUAL).scanned) == {1}  # a second run is allowed


def test_a_planned_retry_also_calls_the_hook():
    sources = [src(1)]
    hooks = Hooks()
    make(sources, hooks).run_retry(1)
    assert len(hooks.reports) == 1 and hooks.reports[0].trigger == "retry"


def test_a_runner_without_alerts_behaves_as_before():
    sources = [src(1)]
    runner = ScanRunner(
        lambda: list(sources), lambda sid: sources[0], FakeScans(sources), FakeAudit(),
        lambda sid, due: None, lambda: NOW,
    )
    assert set(runner.run_all(MANUAL).scanned) == {1}
