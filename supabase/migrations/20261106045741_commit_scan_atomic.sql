-- Save one whole scan in a single transaction.
--
-- Before this, the server wrote documents, versions, files, news cards and the scan
-- result as separate requests. A failure in the middle left partial rows (seen on
-- 2026-11-06: 5 documents saved, last_scan_at still empty) or saved versions whose news
-- cards were never written, which then looks like "no change" on the next scan.
--
-- public.commit_scan() does all of it or none of it. A plpgsql function runs in one
-- transaction, so any exception rolls back every insert and update below.
--
-- p_documents: [{external_id, doc_id (null for a new post), first_seen_at,
--                values{title, source_url, posted_on, status, miss_count, removed_at,
--                       review_reason},
--                versions[{version_no, change_type, title, body_hash, body_text, created_at,
--                          summary, priority, category, summary_source, classification,
--                          files[{filename, size_label, sha256, storage_path, text_content,
--                                 hash_only_reason, extract_note}]}]}]
-- p_seen:      external_ids of the posts read in this scan (sets last_seen_at)
-- p_cards:     audit_logs rows in write order; external_id (or null) names the document
-- Returns {external_id: document id} for every document written.

create function public.commit_scan(
  p_source_id bigint,
  p_now       timestamptz,
  p_documents jsonb,
  p_seen      text[],
  p_cards     jsonb,
  p_status    text,
  p_error     text
)
returns jsonb
language plpgsql
set search_path = ''
as $$
declare
  doc       jsonb;
  ver       jsonb;
  card      jsonb;
  vals      jsonb;
  v_doc_id  bigint;
  v_ver_id  bigint;
  v_latest  bigint;
  v_is_new  boolean;
  v_ids     jsonb := '{}'::jsonb;
begin
  if not exists (select 1 from public.sources where id = p_source_id) then
    raise exception 'source % does not exist', p_source_id;
  end if;

  for doc in select * from jsonb_array_elements(p_documents) loop
    vals     := doc -> 'values';
    v_latest := null;

    if (doc ->> 'doc_id') is null then
      v_is_new := true;
      insert into public.documents (
        source_id, external_id, first_seen_at, title, source_url, posted_on,
        status, miss_count, removed_at, review_reason
      ) values (
        p_source_id, doc ->> 'external_id', (doc ->> 'first_seen_at')::timestamptz,
        vals ->> 'title', vals ->> 'source_url', (vals ->> 'posted_on')::date,
        vals ->> 'status', (vals ->> 'miss_count')::smallint,
        (vals ->> 'removed_at')::timestamptz, vals ->> 'review_reason'
      )
      returning id into v_doc_id;
    else
      v_is_new := false;
      v_doc_id := (doc ->> 'doc_id')::bigint;
      if not exists (
        select 1 from public.documents where id = v_doc_id and source_id = p_source_id
      ) then
        raise exception 'document % does not belong to source %', v_doc_id, p_source_id;
      end if;
    end if;

    v_ids := v_ids || jsonb_build_object(doc ->> 'external_id', v_doc_id);

    -- versions arrive in ascending version_no, so the last one inserted is the latest
    for ver in select * from jsonb_array_elements(doc -> 'versions') loop
      insert into public.document_versions (
        document_id, source_id, version_no, change_type, title, body_hash, body_text,
        created_at, summary, priority, category, summary_source, classification
      ) values (
        v_doc_id, p_source_id, (ver ->> 'version_no')::integer, ver ->> 'change_type',
        ver ->> 'title', ver ->> 'body_hash', ver ->> 'body_text',
        (ver ->> 'created_at')::timestamptz, ver ->> 'summary', ver ->> 'priority',
        ver ->> 'category', ver ->> 'summary_source',
        nullif(ver -> 'classification', 'null'::jsonb)
      )
      returning id into v_ver_id;
      v_latest := v_ver_id;

      insert into public.document_version_files (
        version_id, source_id, filename, size_label, sha256, storage_path,
        text_content, hash_only_reason, extract_note
      )
      select v_ver_id, p_source_id, f ->> 'filename', f ->> 'size_label', f ->> 'sha256',
             f ->> 'storage_path', f ->> 'text_content', f ->> 'hash_only_reason',
             f ->> 'extract_note'
      from jsonb_array_elements(ver -> 'files') as f;
    end loop;

    update public.documents
    set title             = vals ->> 'title',
        source_url        = vals ->> 'source_url',
        posted_on         = (vals ->> 'posted_on')::date,
        status            = vals ->> 'status',
        miss_count        = (vals ->> 'miss_count')::smallint,
        removed_at        = (vals ->> 'removed_at')::timestamptz,
        review_reason     = vals ->> 'review_reason',
        latest_version_id = coalesce(v_latest, latest_version_id)
    where id = v_doc_id;
  end loop;

  update public.documents
  set last_seen_at = p_now
  where source_id = p_source_id and external_id = any (p_seen);

  for card in select * from jsonb_array_elements(p_cards) loop
    insert into public.audit_logs (
      category, event_type, actor_id, source_id, document_id,
      title, summary, priority, payload
    ) values (
      card ->> 'category', card ->> 'event_type', (card ->> 'actor_id')::uuid, p_source_id,
      (select d.id from public.documents d
        where d.source_id = p_source_id and d.external_id = card ->> 'external_id'),
      card ->> 'title', card ->> 'summary', card ->> 'priority',
      coalesce(nullif(card -> 'payload', 'null'::jsonb), '{}'::jsonb)
    );
  end loop;

  -- The scan result and the lock release belong to the same transaction: last_scan_at
  -- (the baseline marker) moves only if everything above was saved.
  update public.sources
  set scan_started_at  = null,
      last_scan_status = p_status,
      last_scan_error  = p_error,
      last_scan_at     = p_now
  where id = p_source_id;

  return v_ids;
end;
$$;

-- Supabase's default privileges would let anon and signed-in users call this through
-- the API. Only the server (service_role) may.
revoke all on function public.commit_scan(bigint, timestamptz, jsonb, text[], jsonb, text, text)
  from public, anon, authenticated;
grant execute on function public.commit_scan(bigint, timestamptz, jsonb, text[], jsonb, text, text)
  to service_role;
