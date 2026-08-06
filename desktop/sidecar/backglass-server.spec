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

# googleapiclient loads its bundled discovery documents lazily inside a try/except
# that static analysis misses, so the three APIs this app actually calls are added
# by hand. Adding them is not enough on its own — see the filter after Analysis.
import googleapiclient  # noqa: E402
from pathlib import Path  # noqa: E402

USED_DISCOVERY_DOCS = ("gmail.v1.json", "calendar.v3.json", "drive.v3.json")
DISCOVERY_DIR = "googleapiclient/discovery_cache/documents"

_docs = Path(googleapiclient.__file__).parent / "discovery_cache" / "documents"
for name in USED_DISCOVERY_DOCS:
    doc = _docs / name
    if doc.exists():
        datas.append((str(doc), DISCOVERY_DIR))

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
# PyInstaller's bundled googleapiclient hook collects the WHOLE discovery_cache
# directory, which silently defeated the three-document selection above: 586 files
# and 99MB of a 160MB app, for APIs this program never calls (Spanner, Healthcare,
# DocumentAI…). The hook wins because it runs during Analysis, so the only place to
# undo it is here, afterwards. Filtering by destination path rather than by source
# keeps this correct whichever route put an entry in the table.
a.datas = [
    entry
    for entry in a.datas
    if DISCOVERY_DIR not in str(entry[0]).replace("\\", "/")
    or Path(str(entry[0])).name in USED_DISCOVERY_DOCS
]

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
