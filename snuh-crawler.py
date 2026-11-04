import requests
from bs4 import BeautifulSoup

BASE = "https://hrpp.snuh.org"
session = requests.Session()

params = {
    "key": "2605277034",
    "pageIndex": "1",
    "sc_bbsCtgrySn": "0",
    "sc": "pstTtl",
    "sw": "",
}

response = session.get(
    f"{BASE}/hrpp/bbs/list.do",
    params=params,
    timeout=20,
)
response.raise_for_status()

soup = BeautifulSoup(response.content, "html.parser")

for a in soup.select("a[onclick*='goView']"):
    title = a.select_one(".title-text")
    if title:
        print(a["onclick"], title.get_text(strip=True))