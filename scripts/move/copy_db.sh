#!/usr/bin/env bash
# Copies the data of one Supabase project into another whose schema is already in place
# (all migrations applied). Runs inside the postgres:17 image; steps in docs/move-supabase.md.
#
#   OLD_DB / NEW_DB: the two "Session pooler" connection URIs, typed by the owner (never stored).
#   /w: a work folder mounted from the host (dump files and row counts; delete it afterwards).
#
#   copy_db.sh dump    read the old project: public data, auth.users and auth.identities, row counts
#   copy_db.sh try     load into the new project inside a transaction, count, then ROLLBACK
#   copy_db.sh load    the same, then COMMIT
#
# The new project's public tables are emptied first (the migrations' seed rows come back from
# the dump with the same ids). Triggers and foreign-key checks are off while loading
# (session_replication_role = replica), so the signup trigger does not make second profiles.
# Storage files are copied separately (copy_storage.py).
set -euo pipefail
W=/w
mode="${1:-}"

# The table lists are materialized first: otherwise the planner may run query_to_xml on rows
# the schema filter has not removed yet (seen 2026-11-16: realtime.subscription).
COUNTS="
with t as materialized (
  select table_name from information_schema.tables
  where table_schema = 'public' and table_type = 'BASE TABLE'
)
select 'public.' || table_name || ' ' ||
       (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from public.%I', table_name), false, true, '')))[1]::text
from t
union all select 'auth.users ' || count(*) from auth.users
union all select 'auth.identities ' || count(*) from auth.identities
order by 1;"

# Who may do what (tables, sequences, column grants, functions, RLS policies): compared old vs
# new, because equal row counts say nothing about access. Grantor names may differ (noise).
ACCESS="
select 'rel ' || c.relname || ' ' || coalesce(c.relacl::text, '-')
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind in ('r', 'S', 'v')
union all
select 'col ' || c.relname || '.' || a.attname || ' ' || a.attacl::text
from pg_attribute a join pg_class c on c.oid = a.attrelid join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and a.attacl is not null
union all
select 'fn ' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ') ' || coalesce(p.proacl::text, '-')
from pg_proc p join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public'
union all
select 'pol ' || tablename || ' ' || policyname || ' ' || cmd || ' ' || roles::text || ' '
       || coalesce(qual, '') || ' ' || coalesce(with_check, '')
from pg_policies where schemaname = 'public'
order by 1;"

# After the load: every identity sequence must be at or past the highest id, or the first
# insert after the switch fails. Raises (and so rolls back) when one is behind.
SEQUENCES="
do \$\$ declare bad text; begin
  with c as materialized (
    select table_name as t, column_name as col,
           pg_get_serial_sequence(format('public.%I', table_name), column_name) as s
    from information_schema.columns where table_schema = 'public' and is_identity = 'YES'
  )
  select string_agg(t || '.' || col, ', ') into bad from c
  where (xpath('/row/m/text()', query_to_xml(format('select coalesce(max(%I), 0) as m from public.%I', col, t),
         false, true, '')))[1]::text::bigint
        > coalesce((select q.last_value from pg_sequences q where format('%I.%I', q.schemaname, q.sequencename) = c.s), 0);
  if bad is not null then raise exception 'identity sequences behind their data: %', bad; end if;
end \$\$;"

EMPTY="
do \$\$ begin
  execute (select 'truncate table ' || string_agg(format('public.%I', tablename), ', ') || ' cascade'
           from pg_tables where schemaname = 'public');
end \$\$;"

case "$mode" in
  dump)
    : "${OLD_DB:?set OLD_DB}"
    pg_dump "$OLD_DB" --data-only --quote-all-identifiers --schema public -f "$W/public.sql"
    pg_dump "$OLD_DB" --data-only --quote-all-identifiers --table auth.users --table auth.identities -f "$W/auth.sql"
    psql "$OLD_DB" -X -A -t -v ON_ERROR_STOP=1 -c "$COUNTS" > "$W/old_counts.txt"
    psql "$OLD_DB" -X -A -t -v ON_ERROR_STOP=1 -c "$ACCESS" > "$W/old_access.txt"
    echo "dumped: public.sql $(wc -c < "$W/public.sql") bytes, auth.sql $(wc -c < "$W/auth.sql") bytes"
    echo "old project: $(grep -c . "$W/old_counts.txt") tables counted, $(grep '^auth.users ' "$W/old_counts.txt")"
    ;;
  try|load)
    : "${NEW_DB:?set NEW_DB}"
    test -s "$W/public.sql" && test -s "$W/auth.sql" && test -s "$W/old_counts.txt" || { echo "run dump first"; exit 1; }
    [ "$mode" = load ] && finish=commit || finish=rollback
    {
      echo "begin;"
      echo "set session_replication_role = replica;"
      echo "$EMPTY"
      cat "$W/auth.sql"
      cat "$W/public.sql"
      echo "set session_replication_role = origin;"
      echo "$SEQUENCES"
      echo "\\o $W/new_counts.txt"
      echo "\\pset tuples_only on"
      echo "\\pset format unaligned"
      echo "$COUNTS"
      echo "\\o"
      echo "$finish;"
    } | psql "$NEW_DB" -X -q -v ON_ERROR_STOP=1 > /dev/null
    psql "$NEW_DB" -X -A -t -v ON_ERROR_STOP=1 -c "$ACCESS" > "$W/new_access.txt"
    if diff "$W/old_access.txt" "$W/new_access.txt" > "$W/access_diff.txt"; then
      echo "$mode: access (grants, column grants, functions, policies) is the same as the old project"
    else
      echo "$mode: ACCESS DIFFERS: send these lines (also in ~/irb-move/access_diff.txt; no secrets in them):"
      cat "$W/access_diff.txt"
    fi
    if diff "$W/old_counts.txt" "$W/new_counts.txt"; then
      echo "$mode: every table has the same row count as the old project ($finish done)"
    else
      echo "$mode: ROW COUNTS DIFFER (lines above: < old, > new); $finish done"
      exit 2
    fi
    ;;
  *)
    echo "usage: copy_db.sh dump|try|load"; exit 1 ;;
esac
