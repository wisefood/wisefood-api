"""Fetch the GeoLite2 country database onto the analytics volume.

Runs as an init container on the API pod, so a fresh node has the file before
the first request. It is written to never be the reason the pod does not
start: every failure path exits 0 with one line in the log, and the API treats
a missing file as "country unknown" — which is what it was before this existed.

Configuration, all by environment:

    GEOIP_DB_PATH        where the .mmdb goes (the API reads the same variable)
    GEOIP_MAX_AGE_DAYS   refetch when the file is older than this (default 30)
    MAXMIND_LICENSE_KEY  a free GeoLite2 licence; unset means "do nothing"
    GEOIP_EDITION        GeoLite2-Country unless you want the city one
"""
import io
import os
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

DOWNLOAD = "https://download.maxmind.com/app/geoip_download"


def log(message: str) -> None:
    print(f"geoip: {message}", flush=True)


def main() -> int:
    path = os.environ.get("GEOIP_DB_PATH", "").strip()
    key = os.environ.get("MAXMIND_LICENSE_KEY", "").strip()
    edition = os.environ.get("GEOIP_EDITION", "GeoLite2-Country").strip()
    try:
        max_age_days = float(os.environ.get("GEOIP_MAX_AGE_DAYS", "30"))
    except ValueError:
        max_age_days = 30.0

    if not path:
        log("GEOIP_DB_PATH is not set; nothing to do")
        return 0
    if not key:
        log("no MAXMIND_LICENSE_KEY; country lookup stays off")
        return 0

    if os.path.isfile(path):
        age_days = (time.time() - os.path.getmtime(path)) / 86400
        if age_days < max_age_days:
            log(f"{path} is {age_days:.0f} days old, keeping it")
            return 0
        log(f"{path} is {age_days:.0f} days old, refreshing")

    url = DOWNLOAD + "?" + urllib.parse.urlencode(
        {"edition_id": edition, "license_key": key, "suffix": "tar.gz"}
    )
    try:
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 — fixed host
            archive = response.read()
    except urllib.error.HTTPError as exc:
        # 401 is the licence being wrong; anything else is MaxMind's day.
        log(f"download failed ({exc.code}); keeping whatever is on the volume")
        return 0
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        log(f"download failed ({exc}); keeping whatever is on the volume")
        return 0

    wanted = f"{edition}.mmdb"
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            member = next(
                (m for m in tar.getmembers() if m.isfile() and m.name.endswith("/" + wanted)),
                None,
            )
            if member is None:
                log(f"archive has no {wanted}; keeping whatever is on the volume")
                return 0
            extracted = tar.extractfile(member)
            if extracted is None:
                log(f"could not read {wanted} from the archive")
                return 0
            body = extracted.read()
    except (tarfile.TarError, EOFError) as exc:
        log(f"archive unreadable ({exc}); keeping whatever is on the volume")
        return 0

    # Write beside, then rename: the API may be reading the old file on the
    # same volume, and a half-written database is worse than a stale one.
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=directory, prefix=".geoip-", suffix=".mmdb")
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        os.replace(temporary, path)
    except OSError as exc:
        log(f"could not write {path} ({exc}); keeping whatever is on the volume")
        return 0

    log(f"refreshed {path} ({len(body) // 1024} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
