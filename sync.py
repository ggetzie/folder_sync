#! /usr/local/src/folder_sync/.venv/bin/python
# This is a command line app to sync a single folder from Dropbox.
# It is meant to be run periodically via a cron job for situations
# where the normal drop box app doesn't work such as on a headless server
# unlike selective_sync, newly added Dropbox folders will never be included
# only the specified folder will be synced.

import argparse
import datetime
import json
import os
import pathlib
import requests

import environ
from dateutil.relativedelta import relativedelta
from dateutil.parser import parse as dt_parse
from dateutil.tz import gettz

ROOT_DIR = environ.Path(__file__) - 1
TEST_FOLDER = pathlib.Path(ROOT_DIR) / "test"
LOGFILE = pathlib.Path(ROOT_DIR.path("log"))
env = environ.Env()
env.read_env((str(ROOT_DIR.path(".env"))))
TOKEN = env("DROPBOX_TOKEN")
URL_BASE = "https://api.dropboxapi.com/2/files/"
DOWNLOAD_URL = "https://content.dropboxapi.com/2/files/download"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


class InvalidDropboxPath(Exception):
    pass


class DropboxAPIError(Exception):
    pass


def cleanup(current_paths, root_folder):
    for current, _, files in os.walk(root_folder):
        for f in files:
            p = pathlib.Path(current) / f
            if str(p) not in current_paths:
                write_log(f"Removed filed {p} - not found in Dropbox")
                os.remove(p)


def prune_log():
    # delete log entries older than 30 days
    def under30(d):
        now = datetime.datetime.now()
        thirtyago = now - relativedelta(days=30)
        return d > thirtyago

    lines = [
        line.strip() for line in LOGFILE.open() if under30(dt_parse(line.split()[0]))
    ]
    LOGFILE.write_text("\n".join(lines))


def delete_empty_folders(root):

    deleted = set()

    for current_dir, subdirs, files in os.walk(root, topdown=False):
        if not (files or subdirs):
            deleted.add(current_dir)
            os.rmdir(current_dir)
            write_log(f"Removed empty directory {current_dir}")
    return deleted


def download_file(dropbox_path):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Dropbox-API-arg": json.dumps({"path": dropbox_path}),
    }
    res = requests.post(DOWNLOAD_URL, headers=headers)
    if res.status_code != 200:
        raise DropboxAPIError(f"Error downloading {dropbox_path}")
    return res


def write_log(msg):
    with open(LOGFILE, "a") as log:
        log.write(f"{datetime.datetime.now().isoformat()} - {msg}\n")


def handle_folder(root_folder, entry):
    local_path = root_folder / entry["path_display"].lstrip("/")
    if not local_path.exists():
        write_log(f"Creating new folder ${local_path}")
        local_path.mkdir(parents=True, exist_ok=True)


def handle_file(root_folder, entry):
    # download files if they don't exist or if the
    # dropbox modified time is more recent than the local modified time
    dropbox_path = entry["path_display"]
    local_path = root_folder / (dropbox_path.lstrip("/"))
    local_path.parent.mkdir(parents=True, exist_ok=True)
    needs_download = False
    if not local_path.exists():
        needs_download = True
    else:
        last_mod_dropbox = dt_parse(entry["client_modified"])
        last_mod_local = datetime.datetime.fromtimestamp(
            local_path.stat().st_mtime, tz=gettz()
        )
        needs_download = last_mod_dropbox > last_mod_local
    if needs_download:
        try:
            res = download_file(dropbox_path)
            local_path.write_bytes(res.content)
            write_log(f"Saved {local_path}")
        except DropboxAPIError as e:
            write_log(f"Error downloading {dropbox_path}")
    else:
        write_log(f"{dropbox_path} up to date.")
    return {str(local_path)}


def download_folder(dropbox_path, root_folder=TEST_FOLDER):
    current_paths = set()
    data = {"path": dropbox_path, "recursive": False}
    res = requests.post(
        URL_BASE + "list_folder", headers=HEADERS, data=json.dumps(data)
    )
    if res.status_code != 200:
        write_log(f"Download folder error - {res.status_code}: {res.content}")
        raise DropboxAPIError
    res_json = res.json()
    entries = res_json["entries"]
    for entry in entries:
        if entry[".tag"] == "folder":
            current_paths = current_paths | download_folder(
                entry["path_display"], root_folder
            )
        elif entry[".tag"] == "file":
            current_paths = current_paths | handle_file(root_folder, entry)
    cursor = res_json.get("cursor", "")
    has_more = res_json.get("has_more", False)
    while has_more:
        data = {"cursor": cursor}
        res = requests.post(
            URL_BASE + "list_folder/continue", data=json.dumps(data), headers=HEADERS
        )
        if res.status_code != 200:
            write_log(f"Download folder error - {res.status_code}: {res.content}")
            raise DropboxAPIError
        res_json = res.json()
        entries = res_json["entries"]
        for entry in entries:
            if entry[".tag"] == "folder":
                current_paths = current_paths | download_folder(
                    entry["path_display"], root_folder
                )
            elif entry[".tag"] == "file":
                current_paths = current_paths | handle_file(root_folder, entry)
        cursor = res_json.get("cursor", "")
        has_more = res_json.get("has_more", False)

    return current_paths


if __name__ == "__main__":
    desc = """Sync a folder from dropbox"""
    parser = argparse.ArgumentParser(desc)
    parser.add_argument(
        "dropbox_path",
        nargs=1,
        help="The name of the folder in Dropbox. Must begin with '/'",
    )
    parser.add_argument(
        "output",
        nargs=1,
        type=pathlib.Path,
        help="The absolute path of where to save the downloaded folder",
    )
    args = parser.parse_args()
    dropbox_path = args.dropbox_path[0]
    if not dropbox_path[0] == "/":
        raise InvalidDropboxPath("Dropbox path must start with '/'")
    output = args.output[0]
    current_paths = download_folder(dropbox_path=dropbox_path, root_folder=output)
    cleanup(current_paths, output)
    delete_empty_folders(output)
    prune_log()
