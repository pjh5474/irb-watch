"""IRB pipeline phase A2: the user's open IRB work over all projects, and which steps get a
due-date reminder in the daily mail. Pure rules over WorkStep rows; no I/O.

Owner decisions (2026-11-13; see docs/decisions.md "IRB pipeline redesign, phase A2"):
  * The work page has four lists: steps overdue or due within 14 days; steps submitted and
    waiting for the IRB's decision; open steps not yet submitted with required documents
    missing; AI drafts being written or failed (built in the API from the draft jobs).
  * A submitted step is never due or overdue: it waits for the IRB, and gets no reminder.
  * The window is 14 days, fixed for everyone (a per-organization setting can come later).
  * Reminders go out on set days only: 14, 7, 3 and 1 days before the due date, on the day, then
    every 7 days while overdue. Only today counts: a reminder day without a mail (no scheduled
    run, a failed send) is missed, not caught up (owner's option; a catch-up was dropped after the
    code review because a newly set due date would "catch up" days that never applied).
"""

from dataclasses import dataclass
from datetime import date

from irb_watch.services.pipelines import SUBMITTED, WorkStep

DUE_WINDOW_DAYS = 14
REMINDER_DAYS = frozenset({14, 7, 3, 1, 0})  # days before the due date (0 = on the day)
OVERDUE_EVERY = 7  # then every 7 days overdue


def days_left(due_on: str, today: date) -> int:
    """Days from today to the due date; negative = overdue."""
    return (date.fromisoformat(due_on) - today).days


def counts_for_due(step: WorkStep) -> bool:
    """An open step that is not submitted and has a due date."""
    return step.due_on is not None and not step.closed and step.status_key != SUBMITTED


@dataclass(frozen=True)
class WorkLists:
    due: list[WorkStep]  # overdue first, then by due date
    waiting: list[WorkStep]  # submitted, no decision yet; longest waiting first
    missing: list[WorkStep]  # open, not submitted, required documents missing


def work_lists(steps: list[WorkStep], today: date) -> WorkLists:
    due = sorted(
        (s for s in steps if counts_for_due(s) and days_left(s.due_on, today) <= DUE_WINDOW_DAYS),  # type: ignore[arg-type]
        key=lambda s: (s.due_on, s.project_name or "", s.step_id),
    )
    waiting = sorted(
        (s for s in steps if s.status_key == SUBMITTED and not s.closed and s.decided_on is None),
        key=lambda s: (s.submitted_on is None, s.submitted_on or "", s.step_id),
    )
    missing = sorted(
        (s for s in steps if not s.closed and s.status_key != SUBMITTED and s.required_missing > 0),
        key=lambda s: (s.due_on is None, s.due_on or "", s.project_name or "", s.step_id),
    )
    return WorkLists(due, waiting, missing)


def is_reminder_day(left: int) -> bool:
    return left in REMINDER_DAYS or (left < 0 and -left % OVERDUE_EVERY == 0)


def reminders_for(steps: list[WorkStep], today: date) -> list[tuple[WorkStep, int]]:
    """The steps whose reminder day is today, with their days left, overdue first."""
    out = []
    for s in steps:
        if not counts_for_due(s):
            continue
        left = days_left(s.due_on, today)  # type: ignore[arg-type]
        if is_reminder_day(left):
            out.append((s, left))
    return sorted(out, key=lambda pair: (pair[1], pair[0].project_name or "", pair[0].step_id))
