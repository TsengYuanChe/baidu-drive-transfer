import mimetypes
import re
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


SCOPES = [
    "https://www.googleapis.com/auth/drive",
]

CREDENTIALS_FILE = Path("credentials.json")
TOKEN_FILE = Path("token.json")


def parse_folder_id(folder_url: str) -> str:
    patterns = [
        r"/folders/([^/?]+)",
        r"[?&]id=([^&]+)",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            folder_url,
        )

        if match:
            return match.group(1)

    raise RuntimeError(
        f"Could not parse Google Drive "
        f"folder ID: {folder_url}"
    )


def get_credentials() -> Credentials:
    creds = None

    if TOKEN_FILE.exists():
        creds = (
            Credentials.from_authorized_user_file(
                TOKEN_FILE,
                SCOPES,
            )
        )

    if (
        creds
        and creds.expired
        and creds.refresh_token
    ):
        print("Refreshing Google OAuth token...")

        creds.refresh(
            Request()
        )

    if not creds or not creds.valid:
        if not CREDENTIALS_FILE.exists():
            raise RuntimeError(
                "credentials.json not found"
            )

        print(
            "Starting Google OAuth login..."
        )

        flow = (
            InstalledAppFlow
            .from_client_secrets_file(
                CREDENTIALS_FILE,
                SCOPES,
            )
        )

        creds = flow.run_local_server(
            port=0,
        )

    TOKEN_FILE.write_text(
        creds.to_json(),
        encoding="utf-8",
    )

    return creds


def verify_folder(
    service,
    folder_id: str,
) -> dict:
    print("\n=== GOOGLE DRIVE FOLDER ===")

    folder = (
        service.files()
        .get(
            fileId=folder_id,
            fields=(
                "id,name,mimeType"
            ),
        )
        .execute()
    )

    if (
        folder.get("mimeType")
        != "application/vnd.google-apps.folder"
    ):
        raise RuntimeError(
            "Target is not a Google Drive folder"
        )

    print(
        "name:",
        folder["name"],
    )

    print(
        "id:",
        folder["id"],
    )

    return folder


def upload_file(
    service,
    folder_id: str,
    file_path: Path,
) -> dict:
    if not file_path.exists():
        raise RuntimeError(
            f"File not found: {file_path}"
        )

    if not file_path.is_file():
        raise RuntimeError(
            f"Not a file: {file_path}"
        )

    mime_type, _ = (
        mimetypes.guess_type(
            file_path.name
        )
    )

    if not mime_type:
        mime_type = (
            "application/octet-stream"
        )

    print("\n=== GOOGLE DRIVE UPLOAD ===")

    print(
        "file:",
        file_path,
    )

    print(
        "size:",
        file_path.stat().st_size,
    )

    print(
        "mime:",
        mime_type,
    )

    metadata = {
        "name": file_path.name,
        "parents": [
            folder_id,
        ],
    }

    media = MediaFileUpload(
        str(file_path),
        mimetype=mime_type,
        resumable=True,
    )

    request = (
        service.files()
        .create(
            body=metadata,
            media_body=media,
            fields=(
                "id,name,size,"
                "parents,webViewLink"
            ),
        )
    )

    response = None

    while response is None:
        status, response = (
            request.next_chunk()
        )

        if status:
            progress = int(
                status.progress() * 100
            )

            print(
                f"progress: {progress}%"
            )

    print("\n=== RESULT ===")

    print(
        "id:",
        response["id"],
    )

    print(
        "name:",
        response["name"],
    )

    print(
        "size:",
        response.get("size"),
    )

    print(
        "webViewLink:",
        response.get("webViewLink"),
    )

    print("Upload OK")

    return response


def main() -> None:
    if len(sys.argv) != 3:
        raise RuntimeError(
            "Usage: "
            "python google_drive_upload.py "
            "'<google-drive-folder-url>' "
            "'<local-file>'"
        )

    folder_url = sys.argv[1]

    file_path = Path(
        sys.argv[2]
    )

    folder_id = parse_folder_id(
        folder_url
    )

    print(
        "folder_id:",
        folder_id,
    )

    creds = get_credentials()

    print(
        "Google OAuth: OK"
    )

    service = build(
        "drive",
        "v3",
        credentials=creds,
    )

    verify_folder(
        service=service,
        folder_id=folder_id,
    )

    upload_file(
        service=service,
        folder_id=folder_id,
        file_path=file_path,
    )


if __name__ == "__main__":
    main()