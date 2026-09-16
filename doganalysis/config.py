"""Where things live: the saved key, the data folder, and private file writes."""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = ROOT / "config.json"
DEFAULT_BASE = "https://api.greyhoundapi.com/v1"


def base_url():
    """The API root. GAPI_BASE_URL exists for testing against a stand-in server."""
    return (os.environ.get("GAPI_BASE_URL") or DEFAULT_BASE).rstrip("/")


def private_dir(path):
    """Create a folder only this user can read, and lock down every folder between it and data/."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    data = DATA.resolve()
    current = path.resolve()
    while current == data or data in current.parents:
        try:
            os.chmod(str(current), 0o700)
        except OSError:
            pass
        if current == data:
            break
        current = current.parent
    return path


def load_json(path):
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def write_json(path, payload):
    """Write JSON atomically inside a private folder."""
    path = Path(path)
    private_dir(path.parent)
    temp = path.with_name(path.name + ".tmp")
    with open(str(temp), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(str(temp), str(path))
    return path


def load_config():
    data = load_json(CONFIG)
    return data if isinstance(data, dict) else {}


def save_config(data):
    """config.json holds the key, so it is created at mode 600 and never widened."""
    temp = CONFIG.with_name(CONFIG.name + ".tmp")
    handle = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
    try:
        os.chmod(str(temp), 0o600)
    except OSError:
        pass
    os.replace(str(temp), str(CONFIG))
    try:
        os.chmod(str(CONFIG), 0o600)
    except OSError:
        pass


def saved_key():
    return str(load_config().get("api_key") or "").strip()


def store_key(key):
    data = load_config()
    data["api_key"] = key
    save_config(data)


def relative(path):
    """Show a path relative to the repo when it sits inside it."""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def web_user_note():
    """This folder may sit under a web root, so the key must never be written by the web server's user."""
    if os.name == "nt":
        return None
    try:
        import pwd

        name = pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        return None
    if name in ("www-data", "apache", "nginx", "http"):
        return (
            "Running as %s. The saved key and data would then belong to the web server's user. "
            "Run as the owner of this folder instead." % name
        )
    return None
