import argparse
import mimetypes
import os
import time
from dataclasses import dataclass

import httpx
import requests
from google.auth.transport.requests import Request

from baidu_share_transfer import (
    USER_AGENT,
    extract_share_metadata,
    get_download_config,
    get_download_links,
    get_verify_surl,
    list_all_files,
    parse_share_url,
    verify_share,
)
from google_drive_upload import (
    get_credentials,
    parse_folder_id,
)


GOOGLE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
GOOGLE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"

CHUNK_SIZE = 8 * 1024 * 1024
BAIDU_READ_CHUNK_SIZE = 100 * 1024
DLINK_BATCH_SIZE = 3


@dataclass
class TransferProgress:
    total_files: int
    total_bytes: int
    started_at: float

    baidu_downloaded_bytes: int = 0
    baidu_completed_files: int = 0

    google_uploaded_bytes: int = 0
    google_completed_files: int = 0

    @property
    def baidu_percent(self) -> float:
        if self.total_bytes == 0:
            return 100.0

        return (
            self.baidu_downloaded_bytes
            / self.total_bytes
            * 100
        )

    @property
    def google_percent(self) -> float:
        if self.total_bytes == 0:
            return 100.0

        return (
            self.google_uploaded_bytes
            / self.total_bytes
            * 100
        )

    @property
    def baidu_speed(self) -> float:
        elapsed = time.monotonic() - self.started_at

        if elapsed <= 0:
            return 0.0

        return (
            self.baidu_downloaded_bytes
            / elapsed
        )

    @property
    def google_speed(self) -> float:
        elapsed = time.monotonic() - self.started_at

        if elapsed <= 0:
            return 0.0

        return (
            self.google_uploaded_bytes
            / elapsed
        )


def format_bytes(value: int) -> str:
    mb = value / 1024 / 1024
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb / 1024:.2f} GB"


def format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "--"

    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def print_job_progress(
    progress: TransferProgress,
    first_print: bool = False,
) -> None:
    baidu_speed_mb = (
        progress.baidu_speed
        / 1024
        / 1024
    )

    google_speed_mb = (
        progress.google_speed
        / 1024
        / 1024
    )

    baidu_line = (
        f"Baidu Download Process: "
        f"{format_bytes(progress.baidu_downloaded_bytes)}"
        f" / {format_bytes(progress.total_bytes)} | "
        f"{progress.baidu_percent:6.2f}% | "
        f"{progress.baidu_completed_files}/"
        f"{progress.total_files} | "
        f"{baidu_speed_mb:.2f} MB/s"
    )

    google_line = (
        f"Google Upload Process : "
        f"{format_bytes(progress.google_uploaded_bytes)}"
        f" / {format_bytes(progress.total_bytes)} | "
        f"{progress.google_percent:6.2f}% | "
        f"{progress.google_completed_files}/"
        f"{progress.total_files} | "
        f"{google_speed_mb:.2f} MB/s"
    )

    if not first_print:
        # Move cursor up two lines.
        print("\033[2A", end="")

    # Clear each line before redrawing.
    print("\r\033[K" + baidu_line)
    print("\r\033[K" + google_line)

    print(end="", flush=True)


def refresh_google_credentials(credentials) -> None:
    if not credentials.valid or credentials.expired:
        credentials.refresh(Request())

    if not credentials.token:
        raise RuntimeError("Google access token not available")


def create_google_folder(
    access_token: str,
    folder_name: str,
    parent_folder_id: str,
) -> dict:
    response = requests.post(
        GOOGLE_FILES_URL,
        params={"fields": "id,name,mimeType"},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_folder_id],
        },
        timeout=30,
    )

    response.raise_for_status()
    return response.json()


def create_google_resumable_session(
    access_token: str,
    folder_id: str,
    filename: str,
    file_size: int,
    mime_type: str,
) -> str:
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

    response.raise_for_status()

    upload_url = response.headers.get("Location")
    if not upload_url:
        raise RuntimeError(
            "Google did not return resumable upload URL"
        )

    return upload_url


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
            "Content-Length": str(len(chunk)),
            "Content-Range": (
                f"bytes {start}-{end}/{total_size}"
            ),
        },
        data=chunk,
        timeout=120,
    )

    if response.status_code == 308:
        return None

    if response.status_code in (200, 201):
        return response.json()

    raise RuntimeError(
        "Google chunk upload failed: "
        f"{response.status_code} {response.text}"
    )


def stream_baidu_to_google(
    baidu_client: httpx.Client,
    dlink: str,
    upload_url: str,
    filename: str,
    file_size: int,
    progress: TransferProgress,
) -> dict:
    uploaded = 0
    downloaded = 0
    buffer = bytearray()

    with baidu_client.stream(
        "GET",
        dlink,
        follow_redirects=True,
        timeout=None,
    ) as baidu_response:
        baidu_response.raise_for_status()

        for data in baidu_response.iter_bytes(
            chunk_size=BAIDU_READ_CHUNK_SIZE
        ):
            buffer.extend(data)
            downloaded += len(data)
            
            progress.baidu_downloaded_bytes += len(data)

            print_job_progress(progress)

            if downloaded > file_size:
                raise RuntimeError(
                    "Baidu downloaded more bytes than expected: "
                    f"file={filename}, "
                    f"downloaded={downloaded}, "
                    f"expected={file_size}"
                )

            while len(buffer) >= CHUNK_SIZE:
                chunk = bytes(buffer[:CHUNK_SIZE])
                del buffer[:CHUNK_SIZE]

                result = upload_google_chunk(
                    upload_url=upload_url,
                    chunk=chunk,
                    start=uploaded,
                    total_size=file_size,
                )

                uploaded += len(chunk)

                progress.google_uploaded_bytes += len(chunk)

                print_job_progress(progress)

                if result is not None:
                    return result
                
        progress.baidu_completed_files += 1
        print_job_progress(progress)

        if buffer:
            chunk = bytes(buffer)

            result = upload_google_chunk(
                upload_url=upload_url,
                chunk=chunk,
                start=uploaded,
                total_size=file_size,
            )

            uploaded += len(chunk)
            
            progress.google_uploaded_bytes += len(chunk)

            print_job_progress(progress)

            if result is not None:
                return result

    raise RuntimeError(
        "Google upload did not return final file metadata"
    )


def batched(items: list[dict], batch_size: int):
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]


def run_transfer(
    baidu_url: str,
    google_folder_url: str,
    mode: str,
) -> None:
    cookie = os.environ.get("BAIDU_COOKIE")
    if not cookie:
        raise RuntimeError("BAIDU_COOKIE is not set")

    google_credentials = get_credentials()
    refresh_google_credentials(google_credentials)

    google_parent_folder_id = parse_folder_id(
        google_folder_url
    )

    raw_surl, pwd = parse_share_url(baidu_url)

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
        share_response = client.get(baidu_url)
        share_response.raise_for_status()

        metadata = extract_share_metadata(
            share_response.text
        )

        if metadata["loginstate"] != "1":
            raise RuntimeError(
                "Baidu session is not logged in"
            )

        sekey = verify_share(
            client=client,
            verify_surl=get_verify_surl(raw_surl),
            pwd=pwd,
            bdstoken=metadata["bdstoken"],
        )

        all_files, _ = list_all_files(
            client=client,
            sekey=sekey,
            share_uk=metadata["share_uk"],
            share_id=metadata["share_id"],
            bdstoken=metadata["bdstoken"],
            root_path=metadata["root_path"],
        )

        if not all_files:
            raise RuntimeError(
                "No files found in Baidu share"
            )

        selected_files = (
            all_files[:1]
            if mode == "test"
            else all_files
        )

        root_folder_name = (
            metadata["root_path"]
            .rstrip("/")
            .split("/")[-1]
        )

        refresh_google_credentials(
            google_credentials
        )

        google_root_folder = create_google_folder(
            access_token=google_credentials.token,
            folder_name=root_folder_name,
            parent_folder_id=google_parent_folder_id,
        )

        google_root_folder_id = (
            google_root_folder["id"]
        )

        total_bytes = sum(
            int(file_info["size"])
            for file_info in selected_files
        )

        progress = TransferProgress(
            total_files=len(selected_files),
            total_bytes=total_bytes,
            started_at=time.monotonic(),
        )

        print("\n=== TRANSFER START ===")
        print("mode:", mode)
        print("folder:", root_folder_name)
        print("files:", progress.total_files)
        print("size:", format_bytes(progress.total_bytes))
        print()
        
        print_job_progress(
            progress,
            first_print=True,
        )

        for batch in batched(
            selected_files,
            DLINK_BATCH_SIZE,
        ):
            # Refresh sign/timestamp for each small batch so
            # long-running transfers do not depend on one
            # download configuration for the whole job.
            sign, timestamp = get_download_config(
                client=client,
                raw_surl=raw_surl,
                bdstoken=metadata["bdstoken"],
            )

            download_items = get_download_links(
                client=client,
                file_infos=batch,
                sign=sign,
                timestamp=timestamp,
                bdstoken=metadata["bdstoken"],
                js_token=metadata["js_token"],
                sekey=sekey,
                share_uk=metadata["share_uk"],
                share_id=metadata["share_id"],
            )

            if len(download_items) != len(batch):
                raise RuntimeError(
                    "Baidu download link count mismatch: "
                    f"requested={len(batch)}, "
                    f"returned={len(download_items)}"
                )

            download_items_by_fsid = {
                str(item["fs_id"]): item
                for item in download_items
            }

            for file_info in batch:
                fs_id = str(file_info["fs_id"])
                filename = file_info["server_filename"]
                file_size = int(file_info["size"])

                download_item = (
                    download_items_by_fsid.get(fs_id)
                )

                if not download_item:
                    raise RuntimeError(
                        "Baidu download item missing: "
                        f"fs_id={fs_id}, file={filename}"
                    )

                dlink = download_item.get("dlink")
                if not dlink:
                    raise RuntimeError(
                        f"Baidu dlink missing: {filename}"
                    )

                mime_type, _ = mimetypes.guess_type(
                    filename
                )
                if not mime_type:
                    mime_type = "application/octet-stream"

                refresh_google_credentials(
                    google_credentials
                )

                upload_url = (
                    create_google_resumable_session(
                        access_token=(
                            google_credentials.token
                        ),
                        folder_id=(
                            google_root_folder_id
                        ),
                        filename=filename,
                        file_size=file_size,
                        mime_type=mime_type,
                    )
                )

                result = stream_baidu_to_google(
                    baidu_client=client,
                    dlink=dlink,
                    upload_url=upload_url,
                    filename=filename,
                    file_size=file_size,
                    progress=progress,
                )

                returned_size = result.get("size")
                if (
                    returned_size is not None
                    and int(returned_size) != file_size
                ):
                    raise RuntimeError(
                        "Google file size mismatch: "
                        f"file={filename}, "
                        f"expected={file_size}, "
                        f"actual={returned_size}"
                    )

                progress.google_completed_files += 1

                print_job_progress(progress)

        elapsed = time.monotonic() - progress.started_at

        print("\n=== TRANSFER COMPLETE ===")
        print("folder:", root_folder_name)
        print(
            "files:",
            f"{progress.google_completed_files}/"
            f"{progress.total_files}",
        )
        print("size:", format_bytes(progress.total_bytes))
        print("elapsed:", format_eta(elapsed))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Stream files from a Baidu share "
            "directly to Google Drive."
        )
    )

    parser.add_argument(
        "baidu_url",
        help="Baidu share URL",
    )
    parser.add_argument(
        "google_folder_url",
        help="Google Drive destination folder URL",
    )
    parser.add_argument(
        "--mode",
        choices=("test", "all"),
        default="test",
        help=(
            "test transfers the first file; "
            "all transfers every file"
        ),
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    run_transfer(
        baidu_url=args.baidu_url,
        google_folder_url=args.google_folder_url,
        mode=args.mode,
    )


if __name__ == "__main__":
    main()
