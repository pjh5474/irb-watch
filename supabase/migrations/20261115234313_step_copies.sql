-- IRB pipeline phase C2: copy a step to sibling sites (owner, 2026-11-16; see docs/decisions.md
-- "IRB pipeline phase C2: copy a step to sibling sites" and docs/pipeline-design.md 4.5).
--
--   * pipeline_steps.copied_from_step_id: the step this one was copied from (a new step, or an
--     untouched step that was filled). At most one live copy of a step per pipeline (the
--     "already copied to this site" rule, also against a double click).
--   * step_documents.copied_from_document_id: the slot it was copied from.
--   * copy_pipeline_step(): writes the whole copy, every site, in one transaction (all or
--     nothing) with its history rows. The server plans the copy (which sites, add or fill,
--     the slot layout); the function checks again what could have changed meanwhile and
--     raises P0001 with the reason.
--   * Server only, as the other pipeline tables (the columns follow the tables' grants).

alter table public.pipeline_steps
  add column copied_from_step_id bigint references public.pipeline_steps (id) on delete set null;
create index pipeline_steps_copied_from_idx on public.pipeline_steps (copied_from_step_id);
create unique index pipeline_steps_live_copy_key
  on public.pipeline_steps (pipeline_id, copied_from_step_id)
  where deleted_at is null and copied_from_step_id is not null;

alter table public.step_documents
  add column copied_from_document_id bigint references public.step_documents (id) on delete set null;
create index step_documents_copied_from_idx on public.step_documents (copied_from_document_id);

comment on column public.pipeline_steps.copied_from_step_id is
  'The step this one was copied from (phase C2); later changes there do not flow over.';
comment on column public.step_documents.copied_from_document_id is
  'The slot this one was copied from (phase C2).';

-- p_targets: one object per site to write, as the server planned it:
--   {"pipeline_id": 12, "mode": "add",
--    "step": {"name", "step_type_id", "status_key", "sequence_no", "subject", "due_on", "target_session_id"},
--    "slot_inserts": [{"name", "kind_id", "listed", "section", "origin", "required", "status", "position",
--                      "copied_from_document_id"}]}
--   {"pipeline_id": 13, "mode": "fill", "step_id": 55,
--    "step": {"subject", "due_on", "target_session_id"},   -- used only where the step's field is empty
--    "slot_updates": [{"id", "position", "status", "copied_from_document_id"}],
--    "slot_inserts": [...]}
-- p_skipped: the sites the server skipped, kept in the source step's history.
-- Returns [{"pipeline_id", "step_id", "mode"}, ...].
create function public.copy_pipeline_step(
  p_step_id bigint, p_actor uuid, p_targets jsonb, p_skipped jsonb
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
  src record;
  tgt record;
  t jsonb;
  s jsonb;
  d jsonb;
  target_step bigint;
  done jsonb := '[]'::jsonb;
begin
  select st.id, st.pipeline_id, st.name, st.step_type_id, p.project_id, p.hospital_id
  into src
  from public.pipeline_steps st
  join public.pipelines p on p.id = st.pipeline_id
  where st.id = p_step_id and st.deleted_at is null and st.parent_step_id is null and p.deleted_at is null;
  if not found then
    raise exception 'the step to copy was removed meanwhile' using errcode = 'P0001';
  end if;

  for t in select value from jsonb_array_elements(p_targets) loop
    -- The row lock keeps two copies into one pipeline from taking the same position.
    select id into tgt
    from public.pipelines
    where id = (t->>'pipeline_id')::bigint and deleted_at is null
      and project_id = src.project_id and hospital_id = src.hospital_id and id <> src.pipeline_id
    for update;
    if not found then
      raise exception 'the pipeline of a site was removed meanwhile' using errcode = 'P0001';
    end if;
    s := coalesce(t->'step', '{}'::jsonb);

    if t->>'mode' = 'add' then
      -- The server's limit of 60 live steps per pipeline, checked again under the row lock.
      if (select count(*) from public.pipeline_steps x where x.pipeline_id = tgt.id and x.deleted_at is null) >= 60 then
        raise exception 'the pipeline of a site reached 60 steps meanwhile' using errcode = 'P0001';
      end if;
      insert into public.pipeline_steps
        (pipeline_id, step_type_id, name, position, status_key, sequence_no, subject, due_on,
         target_session_id, copied_from_step_id)
      values (
        tgt.id, (s->>'step_type_id')::bigint, s->>'name',
        coalesce((select max(x.position) from public.pipeline_steps x where x.pipeline_id = tgt.id), 0) + 1,
        s->>'status_key', (s->>'sequence_no')::integer, coalesce(s->>'subject', ''),
        (s->>'due_on')::date, (s->>'target_session_id')::bigint, src.id
      )
      returning id into target_step;
    elsif t->>'mode' = 'fill' then
      target_step := (t->>'step_id')::bigint;
      -- Still untouched: live, a main step, not a copy, open and not submitted, no dates or
      -- result, no live follow-ups, no file in any slot. Copied values go only where empty.
      update public.pipeline_steps st
      set subject = case when btrim(st.subject) = '' then coalesce(s->>'subject', st.subject) else st.subject end,
          due_on = coalesce(st.due_on, (s->>'due_on')::date),
          target_session_id = coalesce(st.target_session_id, (s->>'target_session_id')::bigint),
          copied_from_step_id = src.id,
          updated_at = now()
      where st.id = target_step and st.pipeline_id = tgt.id
        and st.step_type_id is not distinct from src.step_type_id
        and st.deleted_at is null and st.parent_step_id is null and st.copied_from_step_id is null
        and st.submitted_on is null and st.decided_on is null and st.result_key is null
        and st.status_key <> 'submitted'
        and not exists (select 1 from public.step_statuses x where x.key = st.status_key and x.closed)
        and not exists (
          select 1 from public.pipeline_steps f where f.parent_step_id = st.id and f.deleted_at is null
        )
        and not exists (
          select 1 from public.step_documents sd
          join public.step_document_versions v on v.document_id = sd.id
          where sd.step_id = st.id
        );
      if not found then
        raise exception 'a step at one of the sites changed meanwhile' using errcode = 'P0001';
      end if;
    else
      raise exception 'unknown copy mode' using errcode = 'P0001';
    end if;

    for d in select value from jsonb_array_elements(coalesce(t->'slot_updates', '[]'::jsonb)) loop
      update public.step_documents
      set position = (d->>'position')::integer,
          status = coalesce(d->>'status', status),
          copied_from_document_id = coalesce((d->>'copied_from_document_id')::bigint, copied_from_document_id),
          updated_at = now()
      where id = (d->>'id')::bigint and step_id = target_step and deleted_at is null;
      if not found then
        raise exception 'a document of a step at one of the sites changed meanwhile' using errcode = 'P0001';
      end if;
    end loop;

    insert into public.step_documents
      (step_id, name, kind_id, listed, section, origin, required, status, position, copied_from_document_id)
    select target_step, e.v->>'name', (e.v->>'kind_id')::bigint, coalesce((e.v->>'listed')::boolean, false),
           e.v->>'section', e.v->>'origin', coalesce((e.v->>'required')::boolean, false), e.v->>'status',
           (e.v->>'position')::integer, (e.v->>'copied_from_document_id')::bigint
    from jsonb_array_elements(coalesce(t->'slot_inserts', '[]'::jsonb)) as e(v);
    -- The server's limit of 60 live documents per step, checked again after the inserts.
    if (select count(*) from public.step_documents x where x.step_id = target_step and x.deleted_at is null) > 60 then
      raise exception 'a step at one of the sites would have more than 60 documents' using errcode = 'P0001';
    end if;

    insert into public.pipeline_events (pipeline_id, step_id, actor_id, event_type, payload)
    values (
      tgt.id, target_step, p_actor, 'step.copied_in',
      jsonb_build_object('from_pipeline_id', src.pipeline_id, 'from_step_id', src.id, 'mode', t->>'mode', 'name', src.name)
    );
    update public.pipelines set updated_at = now() where id = tgt.id;
    done := done || jsonb_build_array(jsonb_build_object('pipeline_id', tgt.id, 'step_id', target_step, 'mode', t->>'mode'));
  end loop;

  insert into public.pipeline_events (pipeline_id, step_id, actor_id, event_type, payload)
  values (
    src.pipeline_id, src.id, p_actor, 'step.copied',
    jsonb_build_object('to', done, 'skipped', coalesce(p_skipped, '[]'::jsonb))
  );
  update public.pipelines set updated_at = now() where id = src.pipeline_id;
  return done;
end;
$$;

revoke all on function public.copy_pipeline_step(bigint, uuid, jsonb, jsonb) from public, anon, authenticated;
grant execute on function public.copy_pipeline_step(bigint, uuid, jsonb, jsonb) to service_role;
