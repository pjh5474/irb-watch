"""A session on R-BAY as the person who started a job (never shared, never pooled, never the platform's).

Used by IRB jobs a person starts (later: a new-plan draft). It reads the person's own login from
`UserLoginService` only. It does not use RBAY_POOL (which shares sessions per platform login and
remembers rejections by label) and never touches the admin credential store, so one person's job
can never run on another person's, or the owner's, account. The session is logged out when the
job's block ends, whatever happened inside it.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from irb_watch.crawlers.credentials import Credentials
from irb_watch.crawlers.errors import LoginRejectedError
from irb_watch.crawlers.rbay_login import EntryRefusedError, RbaySession, browser_enter, logout
from irb_watch.services.user_logins import UserLoginService

log = logging.getLogger(__name__)


class UserRbaySessions:
    def __init__(
        self,
        logins: UserLoginService,
        login: Callable[[str, Credentials], RbaySession] = browser_enter,
        do_logout: Callable[[RbaySession], bool] = logout,
    ) -> None:
        self._logins = logins
        self._login = login
        self._logout = do_logout

    @contextmanager
    def session(self, user_id: str, agency: str) -> Iterator[RbaySession]:
        """Log in as this user and enter the hospital's e-IRB; log out when the block ends.

        Raises MissingLoginError (nothing saved), LoginRejectedError (R-BAY refused the password;
        also remembered so no job tries it again until a new one is saved), EntryRefusedError (the
        account cannot enter this hospital; NOT held against the password) or SiteUnavailableError.
        """
        creds = self._logins.credentials_for_job(user_id)
        try:
            session = self._login(agency, creds)
        except EntryRefusedError:
            raise
        except LoginRejectedError as exc:
            self._logins.mark_rejected(user_id, str(exc))
            raise
        session.user = f"user:{user_id}"
        try:
            yield session
        finally:
            try:
                self._logout(session)
            except Exception:
                log.exception("a user's R-BAY session could not be logged out; it ends by idle timeout")
