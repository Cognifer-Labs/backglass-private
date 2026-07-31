# PyInstaller spec for the Backglass sidecar. Build via desktop/build-sidecar.sh
# from the repo root. onedir on purpose: onefile self-extracts ~100MB per launch,
# leaves _MEI* orphans when the Tauri shell SIGKILLs it, and cannot be deep-signed.
#
# --add-data destinations mirror the repo layout so both path-resolution families
# keep working: Path(__file__)-relative (db/, web/) and REPO_ROOT-relative
# (specs/, design/ — config._repo_root returns sys._MEIPASS when frozen).

from PyInstaller.utils.hooks import copy_metadata

datas = [
    ("../../backglass/db/migrations", "backglass/db/migrations"),
    ("../../backglass/db/queries", "backglass/db/queries"),
    ("../../backglass/web/templates", "backglass/web/templates"),
    ("../../backglass/web/static", "backglass/web/static"),
    ("../../specs/extraction-prompts", "specs/extraction-prompts"),
    ("../../specs/roadmaps", "specs/roadmaps"),
    ("../../design", "design"),
]

# googleapiclient loads its bundled discovery documents lazily inside a
# try/except that static analysis misses; only the three used APIs are added —
# the full documents dir is ~50MB.
import googleapiclient  # noqa: E402
from pathlib import Path  # noqa: E402

_docs = Path(googleapiclient.__file__).parent / "discovery_cache" / "documents"
for name in ("gmail.v1.json", "calendar.v3.json", "drive.v3.json"):
    doc = _docs / name
    if doc.exists():
        datas.append((str(doc), "googleapiclient/discovery_cache/documents"))

datas += copy_metadata("google-api-python-client")

hiddenimports = [
    "googleapiclient.discovery_cache",
    "google_auth_httplib2",
    "python_multipart",
    "multipart",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.lifespan.on",
]

a = Analysis(
    ["entry.py"],
    pathex=["../.."],
    datas=datas,
    hiddenimports=hiddenimports,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="backglass-server",
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="backglass-server",
)
