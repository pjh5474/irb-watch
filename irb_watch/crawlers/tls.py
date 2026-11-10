"""TLS settings for sites with a broken certificate chain.

www.r-bay.co.kr sends only its own certificate, without the intermediate that links it to a
trusted root (checked 2026-11-10 with `openssl s_client`: "unable to verify the first
certificate"). Browsers fetch the missing intermediate themselves; httpx does not. Instead of
turning verification off, we ship that one public intermediate (Sectigo Public Server
Authentication CA DV R36, issued by Sectigo Root R46, valid until 2036-03-21, SHA-256
8C:54:C3:34:B6:6B:A4:E4:26:77:2A:F4:A3:F9:13:6C:19:A1:AE:C7:29:FD:B2:8C:53:5C:07:A5:A4:EF:22:E0)
next to the normal root list. Verification stays fully on: the intermediate is only a link,
the trust still comes from the roots in certifi.
"""

import ssl
from functools import lru_cache
from pathlib import Path

import certifi

SECTIGO_DV_R36 = Path(__file__).with_name("certs") / "sectigo_dv_r36.pem"


@lru_cache
def rbay_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=str(SECTIGO_DV_R36))
    # Python 3.13+ turns on "partial chain" by default, which would let the intermediate alone
    # be a trust anchor. Switch it off so every chain must end at a root in certifi.
    context.verify_flags &= ~ssl.VERIFY_X509_PARTIAL_CHAIN
    return context
