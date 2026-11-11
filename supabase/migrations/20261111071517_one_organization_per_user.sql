-- One organization per user (owner, 2026-11-11; see docs/decisions.md "Organizations (SaaS step 1)").
-- Organizations only group users and are the unit for statistics, so each user counts toward
-- exactly one organization. Moving a user = updating their one membership row.
-- The plain user index is replaced by the unique one.

drop index public.organization_members_user_idx;

create unique index organization_members_one_per_user
  on public.organization_members (user_id);
