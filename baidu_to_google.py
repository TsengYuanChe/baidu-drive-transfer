import json
import mimetypes
import sys

import httpx
import requests

from baidu_share_transfer import (
    USER_AGENT,
    parse_share_url,
    get_verify_surl,
    extract_share_metadata,
    verify_share,
    list_all_files,
    get_download_config,
    get_download_links,
)

from google_drive_upload import (
    parse_folder_id,
    get_credentials,
)


GOOGLE_UPLOAD_URL = (
    "https://www.googleapis.com/upload/drive/v3/files"
)

CHUNK_SIZE = 8 * 1024 * 1024


def create_google_resumable_session(
    access_token: str,
    folder_id: str,
    filename: str,
    file_size: int,
    mime_type: str,
) -> str:
    print("\n=== GOOGLE RESUMABLE SESSION ===")

    response = requests.post(
        GOOGLE_UPLOAD_URL,
        params={
            "uploadType": "resumable",
            "fields": "id,name,size,webViewLink",
        },
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mime_type,
            "X-Upload-Content-Length": str(file_size),
        },
        json={
            "name": filename,
            "parents": [folder_id],
        },
        timeout=30,
    )

    print("status:", response.status_code)

    response.raise_for_status()

    upload_url = response.headers.get(
        "Location"
    )

    if not upload_url:
        raise RuntimeError(
            "Google did not return resumable upload URL"
        )

    print("upload session: created")

    return upload_url


def stream_baidu_to_google(
    baidu_client: httpx.Client,
    dlink: str,
    upload_url: str,
    filename: str,
    file_size: int,
) -> dict:
    print("\n=== BAIDU -> GOOGLE TRANSFER ===")

    print("file:", filename)
    print("size:", file_size)

    uploaded = 0
    buffer = bytearray()

    with baidu_client.stream(
        "GET",
        dlink,
        follow_redirects=True,
        timeout=None,
    ) as baidu_response:

        print(
            "Baidu status:",
            baidu_response.status_code,
        )

        baidu_response.raise_for_status()

        for data in baidu_response.iter_bytes(
            chunk_size=1024 * 1024
        ):
            buffer.extend(data)

            while len(buffer) >= CHUNK_SIZE:
                chunk = bytes(
                    buffer[:CHUNK_SIZE]
                )

                del buffer[:CHUNK_SIZE]

                result = upload_google_chunk(
                    upload_url=upload_url,
                    chunk=chunk,
                    start=uploaded,
                    total_size=file_size,
                )

                uploaded += len(chunk)

                print_progress(
                    uploaded,
                    file_size,
                )

                if result is not None:
                    return result

        if buffer:
            chunk = bytes(buffer)

            result = upload_google_chunk(
                upload_url=upload_url,
                chunk=chunk,
                start=uploaded,
                total_size=file_size,
            )

            uploaded += len(chunk)

            print_progress(
                uploaded,
                file_size,
            )

            if result is not None:
                return result

    raise RuntimeError(
        "Google upload did not return final file metadata"
    )


def upload_google_chunk(
    upload_url: str,
    chunk: bytes,
    start: int,
    total_size: int,
) -> dict | None:
    end = start + len(chunk) - 1

    response = requests.put(
        upload_url,
        headers={
            "Content-Length": str(
                len(chunk)
            ),
            "Content-Range": (
                f"bytes {start}-{end}/{total_size}"
            ),
        },
        data=chunk,
        timeout=120,
    )

    if response.status_code == 308:
        return None

    if response.status_code in (
        200,
        201,
    ):
        return response.json()

    raise RuntimeError(
        "Google chunk upload failed: "
        f"{response.status_code} "
        f"{response.text}"
    )


def print_progress(
    uploaded: int,
    total_size: int,
) -> None:
    percent = (
        uploaded
        / total_size
        * 100
    )

    print(
        f"progress: "
        f"{uploaded}/{total_size} "
        f"({percent:.1f}%)"
    )


def main() -> None:
    if len(sys.argv) != 3:
        raise RuntimeError(
            "Usage: "
            "python baidu_to_google.py "
            "'<baidu-share-url>' "
            "'<google-folder-url>'"
        )

    baidu_url = sys.argv[1]
    google_folder_url = sys.argv[2]

    # --------------------------------------------------
    # Google authentication
    # --------------------------------------------------

    google_creds = get_credentials()

    if google_creds.expired:
        from google.auth.transport.requests import Request

        google_creds.refresh(
            Request()
        )

    if not google_creds.token:
        raise RuntimeError(
            "Google access token not available"
        )

    google_folder_id = parse_folder_id(
        google_folder_url
    )

    print("\n=== GOOGLE ===")
    print(
        "folder_id:",
        google_folder_id,
    )

    # --------------------------------------------------
    # Baidu
    # --------------------------------------------------

    raw_surl, pwd = parse_share_url(
        baidu_url
    )

    cookie = __import__(
        "os"
    ).environ.get(
        "BAIDU_COOKIE"
    )

    if not cookie:
        raise RuntimeError(
            "BAIDU_COOKIE is not set"
        )

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "application/json, "
            "text/javascript, */*; q=0.01"
        ),
        "Origin": "https://pan.baidu.com",
        "Referer": baidu_url,
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

        share_response = client.get(
            baidu_url
        )

        share_response.raise_for_status()

        metadata = extract_share_metadata(
            share_response.text
        )

        if metadata["loginstate"] != "1":
            raise RuntimeError(
                "Baidu session is not logged in"
            )

        print("\n=== BAIDU ===")
        print(
            "root:",
            metadata["root_path"],
        )

        # ----------------------------------------------
        # Share verification
        # ----------------------------------------------

        sekey = verify_share(
            client=client,
            verify_surl=get_verify_surl(raw_surl),
            pwd=pwd,
            bdstoken=metadata["bdstoken"],
        )

        # ----------------------------------------------
        # Recursive listing
        # ----------------------------------------------

        all_files, all_directories = (
            list_all_files(
                client=client,
                sekey=sekey,
                share_uk=metadata["share_uk"],
                share_id=metadata["share_id"],
                bdstoken=metadata["bdstoken"],
                root_path=metadata["root_path"],
            )
        )

        if not all_files:
            raise RuntimeError(
                "No files found in Baidu share"
            )

        # PoC: first file only
        file_info = all_files[0]

        print(
            "\n=== SELECTED FILE ==="
        )

        print(
            "filename:",
            file_info[
                "server_filename"
            ],
        )

        print(
            "size:",
            file_info["size"],
        )

        # ----------------------------------------------
        # Baidu dlink
        # ----------------------------------------------

        sign, timestamp = (
            get_download_config(
                client=client,
                raw_surl=raw_surl,
                bdstoken=metadata[
                    "bdstoken"
                ],
            )
        )

        download_items = (
            get_download_links(
                client=client,
                file_infos=[
                    file_info,
                ],
                sign=sign,
                timestamp=timestamp,
                bdstoken=metadata[
                    "bdstoken"
                ],
                js_token=metadata[
                    "js_token"
                ],
                sekey=sekey,
                share_uk=metadata[
                    "share_uk"
                ],
                share_id=metadata[
                    "share_id"
                ],
            )
        )

        if not download_items:
            raise RuntimeError(
                "Baidu returned no download link"
            )

        dlink = download_items[0].get(
            "dlink"
        )

        if not dlink:
            raise RuntimeError(
                "Baidu dlink missing"
            )

        # ----------------------------------------------
        # Google upload session
        # ----------------------------------------------

        filename = file_info[
            "server_filename"
        ]

        file_size = int(
            file_info["size"]
        )

        mime_type, _ = (
            mimetypes.guess_type(
                filename
            )
        )

        if not mime_type:
            mime_type = (
                "application/octet-stream"
            )

        upload_url = (
            create_google_resumable_session(
                access_token=(
                    google_creds.token
                ),
                folder_id=(
                    google_folder_id
                ),
                filename=filename,
                file_size=file_size,
                mime_type=mime_type,
            )
        )

        # ----------------------------------------------
        # Direct streaming
        # ----------------------------------------------

        result = stream_baidu_to_google(
            baidu_client=client,
            dlink=dlink,
            upload_url=upload_url,
            filename=filename,
            file_size=file_size,
        )

        print("\n=== RESULT ===")

        print(
            "id:",
            result.get("id"),
        )

        print(
            "name:",
            result.get("name"),
        )

        print(
            "size:",
            result.get("size"),
        )

        print(
            "webViewLink:",
            result.get(
                "webViewLink"
            ),
        )

        print(
            "Baidu -> Google Drive OK"
        )


if __name__ == "__main__":
    main()