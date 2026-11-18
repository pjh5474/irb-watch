"""IRB pipeline phase C2: copy a step to sibling sites (pure planning, no I/O).

Rules (owner, 2026-11-16; docs/decisions.md "IRB pipeline phase C2: copy a step to sibling
sites" and docs/pipeline-design.md 4.5):
  * Siblings are the other live sites of the same project and hospital (same IRB, other PIs).
    A sibling without a live pipeline is skipped ("start it first").
  * Only main steps are copied; follow-ups are added at each site.
  * What comes along: the type, the name, the subject, the slot layout (order, the user's own
    slots, "not needed" marks; a sibling's own "not needed" mark is never removed), the due date
    and the target meeting (when it is still a valid target). Never files, drafts, notes, status, result or the submitted and decided dates.
  * A sibling with a live copy of this step is skipped. A typed step fills the sibling's
    untouched step of the same type (the lowest number first) and only where its fields are
    empty; with none untouched, a numbered type gets a new step with the sibling's next number,
    other types are skipped when the sibling's step has work. A sibling whose untouched step of
    the type is already a copy of another site's step is skipped (no second round). Free steps
    (no type) are always added as new steps.
  * Untouched: live main step, not a copy, open and not submitted, no submitted or decided
    date, no result, no live follow-ups, no file in any slot (removed slots too).
"""

from dataclasses import dataclass, field

from irb_watch.services.pipelines import (
    MAX_DOCUMENTS,
    MAX_SEQUENCE,
    MAX_STEPS,
    NEEDED,
    NOT_NEEDED,
    SUBMITTED,
    Pipeline,
    Step,
    StepDocument,
    slot_order,
)
from irb_watch.services.sites import Site

ADD, FILL, SKIP = "add", "fill", "skip"
# Why a site is skipped (the page translates the code; the server's text is for errors).
REASONS = {
    "no_pipeline": "no pipeline yet; start it first",
    "already_copied": "already has a copy of this step",
    "has_work": "its step of this type already has work",
    "copied_from_other": "its step of this type is already a copy of another site's step",
    "too_many_steps": f"its pipeline has {MAX_STEPS} steps",
    "too_many_documents": f"the step would have more than {MAX_DOCUMENTS} documents",
}
MEETING_NOT_COPIED = "meeting_not_copied"  # the target meeting is no longer a valid target


@dataclass(frozen=True)
class CopyTarget:
    """One sibling site and what a copy does there."""

    site: Site
    pipeline_id: int | None
    action: str  # add | fill | skip
    reason: str | None = None  # a key of REASONS when skipped
    step_id: int | None = None  # fill: the step filled; skip: the step in the way; add: set after the copy
    step_label: str = ""  # the step at the site ("2차 변경심의")
    notes: tuple[str, ...] = ()
    plan: dict | None = field(default=None, compare=False)  # what goes to the database

    @property
    def site_label(self) -> str:
        return self.site.label


def untouched(
    step: Step, steps: list[Step], docs: list[StepDocument], closed: set[str], *, copy_ok: bool = False
) -> bool:
    """The sibling's step may be filled: nobody worked on it yet. With copy_ok, a copy of
    another step counts too (to tell "a copy nobody worked on" from "has work")."""
    return (
        step.deleted_at is None and step.parent_step_id is None
        and (copy_ok or step.copied_from_step_id is None)
        and step.status_key != SUBMITTED and step.status_key not in closed
        # A decision date next to a picked meeting is that meeting's day, not work.
        and not step.submitted_on and not (step.decided_on and step.target_session_id is None)
        and step.result_key is None
        and not any(s.parent_step_id == step.id and s.deleted_at is None for s in steps)
        and not any(d.versions for d in docs if d.step_id == step.id)
    )


def _status(doc: StepDocument) -> str:
    """No file comes along, so a copied slot is needed, unless it was marked not needed."""
    return NOT_NEEDED if doc.status == NOT_NEEDED else NEEDED


def _insert(doc: StepDocument) -> dict:
    return {
        "name": doc.name, "kind_id": doc.kind_id, "listed": doc.listed, "section": doc.section,
        "origin": doc.origin, "required": doc.required, "status": _status(doc), "position": doc.position,
        "copied_from_document_id": doc.id,
    }


def _key(name: str) -> str:
    return name.strip().lower()


def _merge(source_docs: list[StepDocument], target_docs: list[StepDocument]) -> tuple[list[dict], list[dict]]:
    """(updates, inserts) that give the target step the source's layout.
      * A target slot is matched by its list row first (for every source slot), then by name
        (any case); each target slot once.
      * A matched slot takes the source's order when it is in the same section (else it goes
        last in its own section), and the source's "not needed" mark; the target's own mark is
        never taken away (owner, after the code review).
      * Source slots with no match are added, except one whose name a live target slot already
        has (a renamed list row): the target's slot stays, as names are unique per step.
      * The target's other slots stay, after the copied ones."""
    live = [d for d in target_docs if d.deleted_at is None]
    match: dict[int, StepDocument] = {}  # source id -> target slot
    claimed: set[int] = set()  # target ids
    for sd in source_docs:
        if sd.kind_id is not None:
            hit = next((d for d in live if d.kind_id == sd.kind_id and d.id not in claimed), None)
            if hit is not None:
                match[sd.id] = hit
                claimed.add(hit.id)
    for sd in source_docs:
        if sd.id not in match:
            hit = next((d for d in live if _key(d.name) == _key(sd.name) and d.id not in claimed), None)
            if hit is not None:
                match[sd.id] = hit
                claimed.add(hit.id)
    taken = {_key(d.name) for d in live}
    updates: list[dict] = []
    inserts: list[dict] = []
    moved: list[tuple[StepDocument, StepDocument]] = []  # matched across sections: placed last
    for sd in source_docs:
        hit = match.get(sd.id)
        if hit is None:
            if _key(sd.name) not in taken:
                inserts.append(_insert(sd))
            continue
        row: dict = {"id": hit.id, "copied_from_document_id": sd.id}
        if sd.status == NOT_NEEDED:
            row["status"] = NOT_NEEDED
        if hit.section == sd.section:
            row["position"] = sd.position
            updates.append(row)
        else:
            moved.append((hit, sd))
    top = max((d.position for d in source_docs), default=0)
    rest = [d for d in sorted(live, key=lambda d: (d.position, d.id)) if d.id not in claimed]
    n = top
    for hit, sd in moved:
        n += 1
        row = {"id": hit.id, "position": n, "copied_from_document_id": sd.id}
        if sd.status == NOT_NEEDED:
            row["status"] = NOT_NEEDED
        updates.append(row)
    for d in rest:
        n += 1
        updates.append({"id": d.id, "position": n})
    return updates, inserts


def _meeting_note(source: Step, meeting_ok: bool, target: Step | None = None) -> tuple[str, ...]:
    """MEETING_NOT_COPIED when the source's meeting would have been copied but is no longer a
    valid target (for a fill: only where the target has no meeting of its own)."""
    if source.target_session_id is None or meeting_ok:
        return ()
    if target is not None and target.target_session_id is not None:
        return ()
    return (MEETING_NOT_COPIED,)


def _label(name: str, number: int | None) -> str:
    return f"{number}차 {name}" if number else name


def plan_copy(
    source: Step,
    source_docs: list[StepDocument],
    siblings: list[tuple[Site, Pipeline | None]],
    steps: list[Step],
    docs: list[StepDocument],
    *,
    numbered: bool,
    closed: set[str],
    first_status: str,
    meeting_ok: bool,
    meeting_day: str | None = None,
) -> list[CopyTarget]:
    """What copying `source` does at each sibling site. `steps` holds every step of the
    siblings' pipelines; `docs` the slots of the steps that could be filled."""
    layout = [d for d in slot_order(source_docs) if d.deleted_at is None]
    meeting = source.target_session_id if meeting_ok else None
    # The meeting's day (IRB 심의일) goes with the meeting only when the step's date IS that
    # day (it followed the pick); a typed or actual decision date never does.
    planned = meeting_day if meeting is not None and source.decided_on == meeting_day else None
    out: list[CopyTarget] = []
    for site, pipeline in sorted(siblings, key=lambda x: (x[0].site_no or "~", x[0].pi_name.lower(), x[0].id)):
        if pipeline is None:
            out.append(CopyTarget(site, None, SKIP, "no_pipeline"))
            continue
        own = [s for s in steps if s.pipeline_id == pipeline.id]
        live = [s for s in own if s.deleted_at is None]
        copy = next((s for s in live if s.copied_from_step_id == source.id), None)
        if copy is not None:
            out.append(CopyTarget(site, pipeline.id, SKIP, "already_copied", copy.id, copy.label))
            continue
        same_type = (
            sorted(
                (s for s in live if s.parent_step_id is None and s.step_type_id == source.step_type_id),
                key=lambda s: (s.sequence_no or 0, s.position, s.id),
            )
            if source.step_type_id is not None else []
        )
        fillable = next((s for s in same_type if untouched(s, own, docs, closed)), None)
        if fillable is not None:
            notes = _meeting_note(source, meeting_ok, fillable)
            updates, inserts = _merge(layout, [d for d in docs if d.step_id == fillable.id])
            if sum(1 for d in docs if d.step_id == fillable.id and d.deleted_at is None) + len(inserts) > MAX_DOCUMENTS:
                out.append(CopyTarget(site, pipeline.id, SKIP, "too_many_documents", fillable.id, fillable.label))
                continue
            values = {
                k: v for k, v in (
                    ("subject", source.subject or None), ("due_on", source.due_on), ("target_session_id", meeting),
                    ("decided_on", planned),
                ) if v is not None
            }
            plan = {
                "pipeline_id": pipeline.id, "mode": FILL, "step_id": fillable.id, "step": values,
                "slot_updates": updates, "slot_inserts": inserts,
            }
            out.append(CopyTarget(site, pipeline.id, FILL, None, fillable.id, fillable.label, notes, plan))
            continue
        # A copy of another site's step that nobody worked on yet: neither "has work" nor a
        # reason for a second round; the user decides which copy the site keeps.
        other_copy = next((s for s in same_type if untouched(s, own, docs, closed, copy_ok=True)), None)
        if other_copy is not None:
            out.append(CopyTarget(site, pipeline.id, SKIP, "copied_from_other", other_copy.id, other_copy.label))
            continue
        if same_type and not numbered:
            out.append(CopyTarget(site, pipeline.id, SKIP, "has_work", same_type[0].id, same_type[0].label))
            continue
        if len(live) >= MAX_STEPS:
            out.append(CopyTarget(site, pipeline.id, SKIP, "too_many_steps"))
            continue
        number = None
        if numbered and source.step_type_id is not None:
            number = min(max((s.sequence_no or 0 for s in same_type), default=0) + 1, MAX_SEQUENCE)
        notes = _meeting_note(source, meeting_ok)
        plan = {
            "pipeline_id": pipeline.id, "mode": ADD,
            "step": {
                "name": source.name, "step_type_id": source.step_type_id, "status_key": first_status,
                "sequence_no": number, "subject": source.subject, "due_on": source.due_on,
                "target_session_id": meeting, "decided_on": planned,
            },
            "slot_inserts": [_insert(d) for d in layout],
        }
        out.append(CopyTarget(site, pipeline.id, ADD, None, None, _label(source.name, number), notes, plan))
    return out
