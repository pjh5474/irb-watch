"""In-memory stand-in for services.organizations.OrganizationStore."""

import itertools
from datetime import UTC, datetime

from irb_watch.core.org_stats import UsageRow
from irb_watch.core.weekly import UserInfo
from irb_watch.services.organizations import (
    DEFAULT_ORGANIZATION_ID,
    Member,
    NameTakenError,
    Organization,
)
from tests.fakes import FakeStore


class FakeOrgStore:
    def __init__(self, users: FakeStore) -> None:
        self.users = users
        self._ids = itertools.count(2)
        self.orgs: dict[str, dict] = {
            DEFAULT_ORGANIZATION_ID: {
                "name": "Default", "created_at": "2026-11-11T00:00:00+00:00", "deleted_at": None,
            }
        }
        self.membership: dict[str, str] = {}
        self.org_roles: dict[str, str] = {}
        self.usage_rows: list[UsageRow] = []
        self.account_list: list[UserInfo] = []
        self.usage_calls: list[tuple] = []
        self.fail_move = False

    # helpers for tests
    def add_org(self, name: str, deleted: bool = False) -> str:
        org_id = f"00000000-0000-4000-8000-{next(self._ids):012d}"
        self.orgs[org_id] = {
            "name": name,
            "created_at": datetime.now(UTC).isoformat(),
            "deleted_at": "2026-11-01T00:00:00+00:00" if deleted else None,
        }
        return org_id

    def join(self, user_id: str, org_id: str = DEFAULT_ORGANIZATION_ID) -> None:
        self.membership[user_id] = org_id

    def _all(self) -> dict[str, str]:
        """Like the database: every profile joined Default on creation (the trigger)."""
        return {**dict.fromkeys(self.users.profiles, DEFAULT_ORGANIZATION_ID), **self.membership}

    def _org(self, org_id: str) -> Organization:
        o = self.orgs[org_id]
        count = sum(1 for v in self._all().values() if v == org_id)
        return Organization(org_id, o["name"], o["created_at"], o["deleted_at"], count)

    def _name_taken(self, name: str, except_id: str | None = None) -> bool:
        return any(
            o["deleted_at"] is None and o["name"].lower() == name.lower() and oid != except_id
            for oid, o in self.orgs.items()
        )

    # protocol
    def list_organizations(self):
        return [self._org(oid) for oid in self.orgs]

    def get_organization(self, organization_id):
        return self._org(organization_id) if organization_id in self.orgs else None

    def create_organization(self, name):
        if self._name_taken(name):
            raise NameTakenError
        org_id = self.add_org(name)
        return self._org(org_id)

    def rename_organization(self, organization_id, name):
        if self._name_taken(name, organization_id):
            raise NameTakenError
        self.orgs[organization_id]["name"] = name
        return self._org(organization_id)

    def set_deleted_at(self, organization_id, deleted_at):
        if deleted_at is None and self._name_taken(self.orgs[organization_id]["name"], organization_id):
            raise NameTakenError
        self.orgs[organization_id]["deleted_at"] = deleted_at
        return self._org(organization_id)

    def members(self, organization_id):
        result = []
        for uid, oid in self._all().items():
            if oid != organization_id:
                continue
            p = self.users.get_profile(uid)
            result.append(
                Member(uid, p.email if p else None, None, p.role if p else "user",
                       p.created_at if p else "", p.deleted_at if p else None)
            )
        return result

    def organization_of(self, user_ids):
        everyone = self._all()
        return {u: everyone[u] for u in user_ids if u in everyone}

    def all_memberships(self):
        return self._all()

    def user_exists(self, user_id):
        return self.users.get_profile(user_id) is not None

    def set_user_organization(self, user_id, organization_id):
        if self.fail_move:
            raise RuntimeError("database down")
        self.membership[user_id] = organization_id
        self.org_roles[user_id] = "member"  # the role never travels with a move

    def set_organization_role(self, user_id, organization_id, role):
        if self.membership.get(user_id) != organization_id:
            return False
        self.org_roles[user_id] = role
        return True

    def usage(self, start, end, types):
        self.usage_calls.append((start, end, tuple(types)))
        return list(self.usage_rows)

    def accounts(self):
        return list(self.account_list)
