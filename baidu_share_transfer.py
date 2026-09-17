import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx


SHARE_INIT_URL = "https://pan.baidu.com/share/init"
SHARE_VERIFY_URL = "https://pan.baidu.com/share/verify"
SHARE_LIST_URL = "https://pan.baidu.com/share/list"
TPLCONFIG_URL = "https://pan.baidu.com/share/tplconfig"
DOWNLOAD_API = "https://pan.baidu.com/api/sharedownload"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


def parse_share_url(share_url: str) -> tuple[str, str]:
    parsed = urlparse(share_url)

    match = re.search(r"/s/([^/?]+)", parsed.path)

    if not match:
        raise RuntimeError(
            f"Could not parse Baidu share token: {share_url}"
        )

    raw_surl = match.group(1)

    query = parse_qs(parsed.query)
    pwd = query.get("pwd", [""])[0]

    if not pwd:
        raise RuntimeError(
            "Share URL does not contain pwd"
        )

    return raw_surl, pwd


def get_verify_surl(raw_surl: str) -> str:
    # In the currently observed Baidu flow:
    #
    # /s/1Jvh...
    #
    # becomes:
    #
    # /share/init?surl=Jvh...
    # /share/verify?surl=Jvh...
    #
    # Keep this isolated so it is easy to change
    # if another share behaves differently.

    if raw_surl.startswith("1"):
        return raw_surl[1:]

    return raw_surl


def extract_share_metadata(
    html: str,
) -> dict[str, str]:
    
    file_list_match = re.search(
        r'"file_list"\s*:\s*(\[[\s\S]*?\])\s*,',
        html,
    )

    if not file_list_match:
        raise RuntimeError(
            "Could not find file_list"
        )

    file_list = json.loads(
        file_list_match.group(1)
    )

    if not file_list:
        raise RuntimeError(
            "Share file_list is empty"
        )

    root_item = file_list[0]

    root_path = root_item.get("path")

    if not root_path:
        raise RuntimeError(
            "Could not find root path"
        )

    loginstate_match = re.search(
        r"""loginstate\s*:\s*['"]?(\d+)['"]?""",
        html,
    )

    if not loginstate_match:
        raise RuntimeError(
            "Could not find loginstate"
        )

    loginstate = loginstate_match.group(1)

    if loginstate != "1":
        raise RuntimeError(
            "Baidu session is not logged in"
        )

    bdstoken_match = re.search(
        r"""bdstoken\s*:\s*['"]([^'"]+)['"]""",
        html,
    )

    share_uk_match = re.search(
        r"""share_uk\s*:\s*['"]([^'"]+)['"]""",
        html,
    )

    share_id_match = re.search(
        r"""shareid\s*:\s*['"]([^'"]+)['"]""",
        html,
    )

    if not bdstoken_match:
        raise RuntimeError(
            "Could not find bdstoken"
        )

    if not share_uk_match:
        raise RuntimeError(
            "Could not find share_uk"
        )

    if not share_id_match:
        raise RuntimeError(
            "Could not find shareid"
        )

    encoded_script_match = re.search(
        r"""eval\(decodeURIComponent\(['"]([^'"]+)['"]\)\)""",
        html,
    )

    if not encoded_script_match:
        raise RuntimeError(
            "Could not find jsToken script"
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

    return {
        "loginstate": loginstate,
        "bdstoken": bdstoken_match.group(1),
        "share_uk": share_uk_match.group(1),
        "share_id": share_id_match.group(1),
        "js_token": js_token_match.group(1),
        "root_path": root_path,
    }


def verify_share(
    client: httpx.Client,
    verify_surl: str,
    pwd: str,
    bdstoken: str,
) -> str:

    print("\n=== SHARE INIT ===")

    init_response = client.get(
        SHARE_INIT_URL,
        params={
            "surl": verify_surl,
        },
    )

    print("status:", init_response.status_code)

    init_response.raise_for_status()

    print("\n=== SHARE VERIFY ===")

    verify_response = client.post(
        SHARE_VERIFY_URL,
        params={
            "t": int(time.time() * 1000),
            "bioc": "1",
            "surl": verify_surl,
            "channel": "chunlei",
            "web": "1",
            "app_id": "250528",
            "bdstoken": bdstoken,
            "clienttype": "0",
        },
        data={
            "pwd": pwd,
            "vcode": "",
            "vcode_str": "",
        },
    )

    print("status:", verify_response.status_code)

    data = verify_response.json()

    print("errno:", data.get("errno"))

    if data.get("errno") != 0:
        raise RuntimeError(
            f"Share verify failed: {data}"
        )

    randsk = data.get("randsk")

    if not randsk:
        raise RuntimeError(
            "Share verify returned no randsk"
        )

    sekey = unquote(randsk)

    print("sekey: found")

    return sekey


def list_directory(
    client: httpx.Client,
    sekey: str,
    share_uk: str,
    share_id: str,
    bdstoken: str,
    dir_path: str,
) -> list[dict]:

    all_items: list[dict] = []
    page = 1
    page_size = 100

    while True:
        print(
            f"Listing: {dir_path} "
            f"(page {page})"
        )

        response = client.get(
            SHARE_LIST_URL,
            params={
                "is_from_web": "true",
                "sekey": sekey,
                "uk": share_uk,
                "shareid": share_id,
                "order": "name",
                "desc": "0",
                "showempty": "0",
                "web": "1",
                "page": str(page),
                "num": str(page_size),
                "dir": dir_path,
                "channel": "chunlei",
                "app_id": "250528",
                "bdstoken": bdstoken,
                "clienttype": "0",
            },
        )

        response.raise_for_status()

        data = response.json()

        if data.get("errno") != 0:
            raise RuntimeError(
                f"Share list failed "
                f"for {dir_path}, "
                f"page {page}: {data}"
            )

        items = data.get("list", [])

        print(
            f"  items: {len(items)}"
        )

        all_items.extend(items)

        if len(items) < page_size:
            break

        page += 1

    return all_items

def list_all_files(
    client: httpx.Client,
    sekey: str,
    share_uk: str,
    share_id: str,
    bdstoken: str,
    root_path: str,
) -> tuple[list[dict], list[str]]:

    all_files: list[dict] = []
    all_directories: list[str] = []

    visited_directories: set[str] = set()
    seen_files: set[int] = set()

    def walk(
        dir_path: str,
        depth: int = 0,
    ) -> None:

        if dir_path in visited_directories:
            return

        visited_directories.add(
            dir_path
        )

        all_directories.append(
            dir_path
        )

        indent = "  " * depth

        print(
            f"{indent}[DIR] {dir_path}"
        )

        items = list_directory(
            client=client,
            sekey=sekey,
            share_uk=share_uk,
            share_id=share_id,
            bdstoken=bdstoken,
            dir_path=dir_path,
        )

        for item in items:

            is_dir = (
                int(item.get("isdir", 0))
                == 1
            )

            path = item.get(
                "path",
                "",
            )

            filename = item.get(
                "server_filename",
                "",
            )

            if is_dir:
                print(
                    f"{indent}  "
                    f"[DIR] {filename}"
                )

                if not path:
                    raise RuntimeError(
                        "Directory item "
                        "does not contain path: "
                        f"{item}"
                    )

                walk(
                    path,
                    depth + 1,
                )

                continue

            fs_id = int(
                item["fs_id"]
            )

            if fs_id in seen_files:
                continue

            seen_files.add(
                fs_id
            )

            file_info = {
                "fs_id": fs_id,
                "server_filename": (
                    filename
                ),
                "path": path,
                "size": int(
                    item.get(
                        "size",
                        0,
                    )
                ),
            }

            all_files.append(
                file_info
            )

            print(
                f"{indent}  "
                f"[FILE] {filename} "
                f"({format_bytes(file_info['size'])})"
            )

    walk(root_path)

    return (
        all_files,
        all_directories,
    )


def format_bytes(
    size: int,
) -> str:

    value = float(size)

    units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
    ]

    for unit in units:

        if (
            value < 1024
            or unit == units[-1]
        ):
            return (
                f"{value:.2f} {unit}"
            )

        value /= 1024

    return f"{size} B"


def find_first_file(
    items: list[dict],
) -> dict:

    for item in items:
        if int(item.get("isdir", 0)) == 0:
            return item

    raise RuntimeError(
        "No file found in share list"
    )


def get_download_config(
    client: httpx.Client,
    raw_surl: str,
    bdstoken: str,
) -> tuple[str, int]:

    print("\n=== TPLCONFIG ===")

    response = client.get(
        TPLCONFIG_URL,
        params={
            "surl": raw_surl,
            "fields": "sign,timestamp",
            "view_mode": "1",
            "channel": "chunlei",
            "web": "1",
            "app_id": "250528",
            "bdstoken": bdstoken,
            "clienttype": "0",
        },
    )

    print("status:", response.status_code)

    content_type = response.headers.get(
        "content-type",
        "",
    )

    if "application/json" not in content_type:
        raise RuntimeError(
            "tplconfig returned non-JSON response. "
            "The Baidu session may have expired. "
            f"Final URL: {response.url}"
        )

    data = response.json()

    print("errno:", data.get("errno"))

    if data.get("errno") != 0:
        raise RuntimeError(
            f"tplconfig failed: {data}"
        )

    sign = data["data"]["sign"]
    timestamp = data["data"]["timestamp"]

    print("sign: found")
    print("timestamp:", timestamp)

    return sign, timestamp


def get_download_link(
    client: httpx.Client,
    file_info: dict,
    sign: str,
    timestamp: int,
    bdstoken: str,
    js_token: str,
    sekey: str,
    share_uk: str,
    share_id: str,
) -> str:

    print("\n=== SHARED DOWNLOAD ===")

    fs_id = file_info["fs_id"]

    response = client.post(
        DOWNLOAD_API,
        params={
            "sign": sign,
            "timestamp": timestamp,
            "channel": "chunlei",
            "web": "1",
            "app_id": "250528",
            "bdstoken": bdstoken,
            "clienttype": "0",
            "jsToken": js_token,
        },
        data={
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
                [fs_id]
            ),
            "path_list": "",
            "vip": "0",
        },
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

    return files[0]["dlink"]

def get_download_links(
    client: httpx.Client,
    file_infos: list[dict],
    sign: str,
    timestamp: int,
    bdstoken: str,
    js_token: str,
    sekey: str,
    share_uk: str,
    share_id: str,
) -> list[dict]:

    print("\n=== BATCH SHARED DOWNLOAD ===")

    fs_ids = [
        file_info["fs_id"]
        for file_info in file_infos
    ]

    print("files:", len(fs_ids))
    print("fs_ids:", fs_ids)

    response = client.post(
        DOWNLOAD_API,
        params={
            "sign": sign,
            "timestamp": timestamp,
            "channel": "chunlei",
            "web": "1",
            "app_id": "250528",
            "bdstoken": bdstoken,
            "clienttype": "0",
            "jsToken": js_token,
        },
        data={
            "encrypt": "0",
            "extra": json.dumps(
                {"sekey": sekey},
                separators=(",", ":"),
            ),
            "product": "share",
            "uk": share_uk,
            "primaryid": share_id,
            "fid_list": json.dumps(fs_ids),
            "path_list": "",
            "vip": "0",
        },
    )

    print("status:", response.status_code)

    data = response.json()

    print("errno:", data.get("errno"))

    if data.get("errno") != 0:
        raise RuntimeError(
            f"Batch sharedownload failed: {data}"
        )

    files = data.get("list", [])

    print("returned files:", len(files))

    for item in files:
        print(
            "dlink:",
            item.get("server_filename"),
            "found" if item.get("dlink") else "missing",
        )

    return files


def download_file(
    client: httpx.Client,
    dlink: str,
    file_info: dict,
) -> None:

    filename = file_info["server_filename"]
    expected_size = int(file_info["size"])

    output_file = Path(filename)

    print("\n=== DOWNLOAD FILE ===")
    print("filename:", filename)
    print("expected:", expected_size)

    downloaded = 0

    with client.stream(
        "GET",
        dlink,
    ) as response:

        print("status:", response.status_code)
        print("final host:", response.url.host)

        response.raise_for_status()

        with output_file.open("wb") as f:

            for chunk in response.iter_bytes():

                f.write(chunk)

                downloaded += len(chunk)

    print("\n=== RESULT ===")
    print("file:", output_file)
    print("downloaded:", downloaded)
    print("expected:", expected_size)

    if downloaded != expected_size:
        raise RuntimeError(
            f"Size mismatch: "
            f"expected {expected_size}, "
            f"got {downloaded}"
        )

    print("Download OK")


def main() -> None:

    if len(sys.argv) != 2:
        raise RuntimeError(
            "Usage: "
            "python baidu_share_transfer.py "
            "'<baidu-share-url>'"
        )

    share_url = sys.argv[1]

    cookie = os.environ.get(
        "BAIDU_COOKIE"
    )

    if not cookie:
        raise RuntimeError(
            "BAIDU_COOKIE is not set"
        )

    raw_surl, pwd = parse_share_url(
        share_url
    )

    verify_surl = get_verify_surl(
        raw_surl
    )

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "application/json, "
            "text/javascript, */*; q=0.01"
        ),
        "Origin": "https://pan.baidu.com",
        "Referer": share_url,
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": cookie,
    }

    with httpx.Client(
        headers=headers,
        follow_redirects=True,
        timeout=30.0,
    ) as client:

        # ----------------------------------------------
        # Authenticated share page
        # ----------------------------------------------

        print("=== SHARE PAGE ===")

        response = client.get(
            share_url
        )

        print(
            "status:",
            response.status_code,
        )

        print(
            "final url:",
            response.url,
        )

        response.raise_for_status()

        metadata = extract_share_metadata(
            response.text
        )

        print(
            "loginstate:",
            metadata["loginstate"],
        )

        print("bdstoken: found")
        print("jsToken: found")

        print(
            "share_uk:",
            metadata["share_uk"],
        )

        print(
            "shareid:",
            metadata["share_id"],
        )

        # ----------------------------------------------
        # Verify share
        # ----------------------------------------------

        sekey = verify_share(
            client=client,
            verify_surl=verify_surl,
            pwd=pwd,
            bdstoken=metadata["bdstoken"],
        )

        # ----------------------------------------------
        # Recursive listing
        # ----------------------------------------------

        print(
            "\n=== RECURSIVE LISTING ==="
        )

        all_files, all_directories = (
            list_all_files(
                client=client,
                sekey=sekey,
                share_uk=metadata[
                    "share_uk"
                ],
                share_id=metadata[
                    "share_id"
                ],
                bdstoken=metadata[
                    "bdstoken"
                ],
                root_path=metadata[
                    "root_path"
                ],
            )
        )

        # ----------------------------------------------
        # Summary
        # ----------------------------------------------

        total_size = sum(
            file_info["size"]
            for file_info in all_files
        )

        print(
            "\n=== SUMMARY ==="
        )

        print(
            "root:",
            metadata["root_path"],
        )

        print(
            "directories:",
            len(all_directories),
        )

        print(
            "files:",
            len(all_files),
        )

        print(
            "total bytes:",
            total_size,
        )

        print(
            "total size:",
            format_bytes(total_size),
        )
        
        # ----------------------------------------------
        # Two-file download test
        # ----------------------------------------------

        test_files = all_files[:2]

        if len(test_files) < 2:
            raise RuntimeError(
                "Need at least 2 files for multi-file test"
            )

        print(
            "\n=== TWO-FILE BATCH DLink TEST ==="
        )

        test_files = all_files[:2]

        sign, timestamp = get_download_config(
            client=client,
            raw_surl=raw_surl,
            bdstoken=metadata["bdstoken"],
        )

        download_items = get_download_links(
            client=client,
            file_infos=test_files,
            sign=sign,
            timestamp=timestamp,
            bdstoken=metadata["bdstoken"],
            js_token=metadata["js_token"],
            sekey=sekey,
            share_uk=metadata["share_uk"],
            share_id=metadata["share_id"],
        )

        print("\n=== BATCH RESULT ===")

        print(
            "requested:",
            len(test_files),
        )

        print(
            "returned:",
            len(download_items),
        )


if __name__ == "__main__":
    main()