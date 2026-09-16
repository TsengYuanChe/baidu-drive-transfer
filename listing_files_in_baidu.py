import time
from urllib.parse import unquote

import httpx


SURL = "Jvh3N5J37JgOFota9btIRw"
PWD = "q7ua"

INIT_URL = "https://pan.baidu.com/share/init"
VERIFY_URL = "https://pan.baidu.com/share/verify"

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

with httpx.Client(
    headers=headers,
    follow_redirects=True,
) as client:

    # Step 1: open extraction-code page
    init_response = client.get(
        INIT_URL,
        params={"surl": SURL},
    )

    print("=== INIT ===")
    print("status:", init_response.status_code)
    print("url:", init_response.url)

    # Step 2: verify extraction code
    verify_response = client.post(
        VERIFY_URL,
        params={
            "t": int(time.time() * 1000),
            "bioc": "1",
            "surl": SURL,
            "channel": "chunlei",
            "web": "1",
            "app_id": "250528",
            "bdstoken": "",
            "clienttype": "0",
        },
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://pan.baidu.com",
            "Referer": f"https://pan.baidu.com/share/init?surl={SURL}",
            "X-Requested-With": "XMLHttpRequest",
        },
        data={
            "pwd": PWD,
            "vcode": "",
            "vcode_str": "",
        },
    )

    data = verify_response.json()

    if data.get("errno") != 0:
        raise RuntimeError(f"Verify failed: {data}")

    sekey = unquote(data["randsk"])
    
    print("\n=== VERIFY ===")
    print("randsk:", data["randsk"])
    print("sekey:", sekey)

    list_response = client.get(
        "https://pan.baidu.com/share/list",
        params={
            "is_from_web": "true",
            "sekey": sekey,
            "uk": "2395778897",
            "shareid": "24595776019",
            "order": "name",
            "desc": "0",
            "showempty": "0",
            "web": "1",
            "page": "1",
            "num": "100",
            "dir": "/9.14线香拍摄",
            "channel": "chunlei",
            "app_id": "250528",
            "bdstoken": "",
            "clienttype": "0",
        },
    )

    print("\n=== LIST ===")
    print("status:", list_response.status_code)
    print("request url:", list_response.request.url)

    list_data = list_response.json()
    print("errno:", list_data.get("errno"))

    for item in list_data.get("list", []):
        item_type = "DIR " if int(item["isdir"]) else "FILE"
        print(
            f"{item_type:4} "
            f"{item['server_filename']} "
            f"({item['size']} bytes)"
        )