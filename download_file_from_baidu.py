import json
import os
import re
from pathlib import Path
from urllib.parse import unquote

import httpx


# --------------------------------------------------
# Test share
# --------------------------------------------------

RAW_SURL = "1Jvh3N5J37JgOFota9btIRw"

# /share/init and /share/verify use the token
# without the leading "1" in our observed flow.
VERIFY_SURL = RAW_SURL[1:]

PWD = "q7ua"

SHARE_URL = (
    f"https://pan.baidu.com/s/{RAW_SURL}"
    f"?pwd={PWD}"
)

SHARE_INIT_URL = "https://pan.baidu.com/share/init"
SHARE_VERIFY_URL = "https://pan.baidu.com/share/verify"
TPLCONFIG_URL = "https://pan.baidu.com/share/tplconfig"
DOWNLOAD_API = "https://pan.baidu.com/api/sharedownload"


# --------------------------------------------------
# Test file
# --------------------------------------------------

FS_ID = 929381890102639

EXPECTED_SIZE = 16286240

OUTPUT_FILE = Path("_DSC7914.JPG")


# --------------------------------------------------
# Cookie
# --------------------------------------------------

cookie = os.environ.get("BAIDU_COOKIE")

if not cookie:
    raise RuntimeError("BAIDU_COOKIE is not set")


# --------------------------------------------------
# Headers
# --------------------------------------------------

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "application/json, text/javascript, */*; q=0.01"
    ),
    "Origin": "https://pan.baidu.com",
    "Referer": SHARE_URL,
    "X-Requested-With": "XMLHttpRequest",
    "Cookie": cookie,
}


with httpx.Client(
    headers=headers,
    follow_redirects=True,
    timeout=30.0,
) as client:

    # --------------------------------------------------
    # Step 1: Load authenticated share page
    # --------------------------------------------------

    print("=== SHARE PAGE ===")

    share_response = client.get(SHARE_URL)

    print("status:", share_response.status_code)
    print("final url:", share_response.url)

    share_response.raise_for_status()

    html = share_response.text

    # --------------------------------------------------
    # Extract loginstate
    # --------------------------------------------------

    loginstate_match = re.search(
        r"""loginstate\s*:\s*['"]?(\d+)['"]?""",
        html,
    )

    if not loginstate_match:
        raise RuntimeError(
            "Could not find loginstate in share page"
        )

    loginstate = loginstate_match.group(1)

    print("loginstate:", loginstate)

    if loginstate != "1":
        raise RuntimeError(
            "Baidu session is not logged in"
        )

    # --------------------------------------------------
    # Extract bdstoken
    # --------------------------------------------------

    bdstoken_match = re.search(
        r"""bdstoken\s*:\s*['"]([^'"]+)['"]""",
        html,
    )

    if not bdstoken_match:
        raise RuntimeError(
            "Could not find bdstoken in share page"
        )

    bdstoken = bdstoken_match.group(1)

    print("bdstoken: found")

    # --------------------------------------------------
    # Extract share_uk
    # --------------------------------------------------

    share_uk_match = re.search(
        r"""share_uk\s*:\s*['"]([^'"]+)['"]""",
        html,
    )

    if not share_uk_match:
        raise RuntimeError(
            "Could not find share_uk in share page"
        )

    share_uk = share_uk_match.group(1)

    print("share_uk:", share_uk)

    # --------------------------------------------------
    # Extract shareid
    # --------------------------------------------------

    share_id_match = re.search(
        r"""shareid\s*:\s*['"]([^'"]+)['"]""",
        html,
    )

    if not share_id_match:
        raise RuntimeError(
            "Could not find shareid in share page"
        )

    share_id = share_id_match.group(1)

    print("shareid:", share_id)

    # --------------------------------------------------
    # Extract jsToken
    #
    # The page contains:
    #
    # eval(decodeURIComponent(
    #   '...window.jsToken...fn%28%22TOKEN%22%29'
    # ))
    #
    # Decode the encoded script first, then extract token.
    # --------------------------------------------------

    encoded_script_match = re.search(
        r"""eval\(decodeURIComponent\(['"]([^'"]+)['"]\)\)""",
        html,
    )

    if not encoded_script_match:
        raise RuntimeError(
            "Could not find encoded jsToken script"
        )

    decoded_script = unquote(
        encoded_script_match.group(1)
    )

    js_token_match = re.search(
        r"""window\.jsToken\s*=\s*a.*?fn\(["']([^"']+)["']\)""",
        decoded_script,
    )

    if not js_token_match:
        raise RuntimeError(
            "Could not extract jsToken"
        )

    js_token = js_token_match.group(1)

    print("jsToken: found")

    # --------------------------------------------------
    # Step 2: Initialize share verification session
    # --------------------------------------------------

    print("\n=== SHARE INIT ===")

    init_response = client.get(
        SHARE_INIT_URL,
        params={
            "surl": VERIFY_SURL,
        },
    )

    print("status:", init_response.status_code)

    init_response.raise_for_status()

    # --------------------------------------------------
    # Step 3: Verify extraction code → sekey
    # --------------------------------------------------

    print("\n=== SHARE VERIFY ===")

    verify_response = client.post(
        SHARE_VERIFY_URL,
        params={
            "surl": VERIFY_SURL,
            "channel": "chunlei",
            "web": "1",
            "app_id": "250528",
            "bdstoken": bdstoken,
            "clienttype": "0",
            "bioc": "1",
        },
        data={
            "pwd": PWD,
            "vcode": "",
            "vcode_str": "",
        },
    )

    print("status:", verify_response.status_code)

    verify_data = verify_response.json()

    print("errno:", verify_data.get("errno"))

    if verify_data.get("errno") != 0:
        raise RuntimeError(
            f"share verify failed: {verify_data}"
        )

    randsk = verify_data.get("randsk")

    if not randsk:
        raise RuntimeError(
            "share verify returned no randsk"
        )

    # randsk is URL encoded in the observed response.
    sekey = unquote(randsk)

    print("sekey: found")

    # --------------------------------------------------
    # Step 4: Get fresh sign + timestamp
    # --------------------------------------------------

    print("\n=== TPLCONFIG ===")

    tpl_response = client.get(
        TPLCONFIG_URL,
        params={
            "surl": RAW_SURL,
            "fields": "sign,timestamp",
            "view_mode": "1",
            "channel": "chunlei",
            "web": "1",
            "app_id": "250528",
            "bdstoken": bdstoken,
            "clienttype": "0",
        },
    )

    print("status:", tpl_response.status_code)
    print(
        "content-type:",
        tpl_response.headers.get("content-type"),
    )

    content_type = tpl_response.headers.get(
        "content-type",
        "",
    )

    if "application/json" not in content_type:
        raise RuntimeError(
            "tplconfig returned non-JSON response. "
            "The Baidu login session may have expired. "
            f"Final URL: {tpl_response.url}"
        )

    tpl_data = tpl_response.json()

    print("errno:", tpl_data.get("errno"))

    if tpl_data.get("errno") != 0:
        raise RuntimeError(
            f"tplconfig failed: {tpl_data}"
        )

    sign = tpl_data["data"]["sign"]
    timestamp = tpl_data["data"]["timestamp"]

    print("sign: found")
    print("timestamp:", timestamp)

    # --------------------------------------------------
    # Step 5: Request download link
    # --------------------------------------------------

    print("\n=== SHARED DOWNLOAD ===")

    params = {
        "sign": sign,
        "timestamp": timestamp,
        "channel": "chunlei",
        "web": "1",
        "app_id": "250528",
        "bdstoken": bdstoken,
        "clienttype": "0",
        "jsToken": js_token,
    }

    form_data = {
        "encrypt": "0",
        "extra": json.dumps(
            {
                "sekey": sekey,
            },
            separators=(",", ":"),
        ),
        "product": "share",
        "uk": share_uk,
        "primaryid": share_id,
        "fid_list": json.dumps(
            [FS_ID]
        ),
        "path_list": "",
        "vip": "0",
    }

    response = client.post(
        DOWNLOAD_API,
        params=params,
        data=form_data,
    )

    print("status:", response.status_code)

    data = response.json()

    print("errno:", data.get("errno"))

    if data.get("errno") != 0:
        raise RuntimeError(
            f"sharedownload failed: {data}"
        )

    files = data.get("list", [])

    if not files:
        raise RuntimeError(
            "sharedownload returned no files"
        )

    file_info = files[0]

    print(
        "filename:",
        file_info["server_filename"],
    )

    print(
        "size:",
        file_info["size"],
    )

    dlink = file_info["dlink"]

    # --------------------------------------------------
    # Step 6: Download actual file
    # --------------------------------------------------

    print("\n=== DOWNLOAD FILE ===")

    downloaded = 0

    with client.stream(
        "GET",
        dlink,
    ) as download_response:

        print(
            "status:",
            download_response.status_code,
        )

        print(
            "final host:",
            download_response.url.host,
        )

        download_response.raise_for_status()

        with OUTPUT_FILE.open("wb") as f:

            for chunk in download_response.iter_bytes():

                f.write(chunk)

                downloaded += len(chunk)

    # --------------------------------------------------
    # Result
    # --------------------------------------------------

    print("\n=== RESULT ===")

    print("file:", OUTPUT_FILE)
    print("downloaded:", downloaded)
    print("expected:", EXPECTED_SIZE)

    if downloaded != EXPECTED_SIZE:
        raise RuntimeError(
            f"Size mismatch: "
            f"expected {EXPECTED_SIZE}, "
            f"got {downloaded}"
        )

    print("Download OK")