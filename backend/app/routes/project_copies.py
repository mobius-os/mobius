"""Reviewed, revocable source snapshots; importing never executes shared code."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import shutil
import uuid

import httpx
from datetime import timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import models, workspace_files
from app.common_transport import federation_request, FederationTransportError
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_owner, reject_cross_site
from app.project_retention import PROJECT_LIFECYCLE_LOCK
from app.routes.projects import _live_project, _project_root, _project_response, _template_by_id, _safe_template
from app.timeutil import now_naive_utc

router = APIRouter(prefix="/api/project-copies", tags=["project-copies"])
MAX_PACKAGE = 16 * 1024 * 1024
MAX_SOURCE = 10 * 1024 * 1024
MAX_FILES = 500
EXCLUDED = frozenset({'.git', '.hg', '.svn', 'node_modules', '__pycache__', 'artifacts', '.venv', 'venv', 'dist', 'build', 'data', 'storage', 'uploads', 'chats', '.ssh', '.aws', '.mobius'})
SOURCE_SUFFIXES = frozenset({'.jsx', '.tsx', '.js', '.ts', '.html', '.htm', '.css', '.scss', '.json', '.md', '.txt', '.py', '.tex', '.bib', '.sty', '.cls', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.toml', '.yaml', '.yml'})
WARNING = 'Only selected files are shared. Check them for private information or passwords written into the files. Chats, saved app data, and built previews are not included.'

class StrictBody(BaseModel):
  model_config = ConfigDict(extra='forbid')

class ShareCreate(StrictBody):
  paths: list[str] = Field(min_length=1, max_length=MAX_FILES)
  digest: str = Field(pattern=r'^[a-f0-9]{64}$')

class CopyURL(StrictBody):
  url: str = Field(min_length=1, max_length=4096)

class CopyImport(CopyURL):
  digest: str = Field(pattern=r'^[a-f0-9]{64}$')
  name: str | None = Field(default=None, min_length=1, max_length=256)
  recovery_request_id: str | None = Field(default=None, min_length=1, max_length=128)

class CopyToken(StrictBody):
  token: str = Field(pattern=r'^[A-Za-z0-9_-]{43}$')


def eligible(path: str) -> bool:
  rel = PurePosixPath(path)
  if not path or any(ord(char) < 32 or ord(char) == 127 for char in path) or ':' in path or len(path) > 2048 or '\\' in path or '\x00' in path or rel.is_absolute() or path != rel.as_posix() or any(p in {'.', '..'} for p in rel.parts):
    return False
  for part in rel.parts:
    lower = part.lower()
    if lower in EXCLUDED or lower == '.env' or lower.startswith('.env.') or lower.endswith(('.pem', '.key', '.p12', '.sqlite', '.sqlite3', '.db')) or lower in {'credentials', 'credentials.json', 'secrets.json', '.npmrc', '.pypirc', '.secret-key', 'service-token.txt', 'cli-auth', 'app-secrets'}:
      return False
  return True


def digest(value: dict) -> str:
  return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def source_package(project) -> dict:
  root = _project_root(project)
  listing = workspace_files.list_entries(root, root, recursive=True, hidden_dirs=EXCLUDED)
  if listing['truncated']:
    raise HTTPException(413, 'Too many files to review. Reduce the project files before sharing.')
  files = []
  total = 0
  for item in sorted(listing['entries'], key=lambda row: row['path']):
    path = item['path']
    if not eligible(path):
      continue
    target = workspace_files.resolve_path(root, path)
    if not target.is_file():
      continue
    with target.open('rb') as stream:
      data = stream.read(MAX_SOURCE + 1)
    total += len(data)
    if total > MAX_SOURCE or len(files) >= MAX_FILES:
      raise HTTPException(413, 'Source files exceed the 10 MB or 500 file sharing limit.')
    files.append({'path': path, 'content': base64.b64encode(data).decode('ascii')})
  package = {'format': 'mobius-project-copy-v1', 'name': project.name, 'project_type': project.project_type, 'files': files}
  if len(json.dumps(package).encode()) > MAX_PACKAGE:
    raise HTTPException(413, 'The project copy exceeds the sharing limit.')
  return package


def validate_package(package: object) -> dict:
  if not isinstance(package, dict) or set(package) != {'format', 'name', 'project_type', 'files'} or package.get('format') != 'mobius-project-copy-v1':
    raise HTTPException(422, 'This is not a supported project copy.')
  if not isinstance(package['name'], str) or not 1 <= len(package['name']) <= 256 or not isinstance(package['project_type'], str) or len(package['project_type']) > 128:
    raise HTTPException(422, 'Invalid project copy details.')
  files = package['files']
  if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
    raise HTTPException(422, 'Invalid project copy file count.')
  paths = set()
  total = 0
  for item in files:
    if not isinstance(item, dict) or set(item) != {'path', 'content'} or not isinstance(item['path'], str) or not eligible(item['path']) or not isinstance(item['content'], str):
      raise HTTPException(422, 'The copy includes an unavailable file path.')
    path = item['path']
    if path in paths or any(path.startswith(p + '/') or p.startswith(path + '/') for p in paths):
      raise HTTPException(422, 'The copy contains conflicting file paths.')
    paths.add(path)
    try:
      data = base64.b64decode(item['content'], validate=True)
    except (ValueError, binascii.Error) as exc:
      raise HTTPException(422, 'The copy contains invalid file content.') from exc
    total += len(data)
    if total > MAX_SOURCE:
      raise HTTPException(413, 'The copy exceeds the 10 MB source limit.')
  return package


def preview(package: dict, *, selection=False) -> dict:
  files = []
  for item in package['files']:
    row = {'path': item['path'], 'size': len(base64.b64decode(item['content']))}
    if selection:
      row['selected'] = PurePosixPath(item['path']).suffix.lower() in SOURCE_SUFFIXES
    files.append(row)
  return {'name': package['name'], 'project_type': package['project_type'], 'digest': digest(package), 'files': files, 'total_bytes': sum(row['size'] for row in files), 'warning': WARNING}


def scrub_expired(db):
  db.query(models.ProjectSourceCopy).filter(models.ProjectSourceCopy.expires_at <= now_naive_utc(), models.ProjectSourceCopy.package_json.is_not(None)).update({'package_json': None}, synchronize_session=False)
  db.commit()


def share_view(row):
  return {'id': row.id, 'created_at': row.created_at, 'expires_at': row.expires_at, 'revoked_at': row.revoked_at}

@router.get('/projects/{project_id}/preview')
def preview_source(project_id: str, _: models.Owner = Depends(get_current_owner), db: Session = Depends(get_db)):
  return preview(source_package(_live_project(db, project_id)), selection=True)

@router.get('/projects/{project_id}/shares')
def list_shares(project_id: str, _: models.Owner = Depends(get_current_owner), db: Session = Depends(get_db)):
  _live_project(db, project_id)
  scrub_expired(db)
  return [share_view(row) for row in db.query(models.ProjectSourceCopy).filter_by(project_id=project_id).order_by(models.ProjectSourceCopy.created_at.desc()).all()]

@router.post('/projects/{project_id}/shares', dependencies=[Depends(reject_cross_site)])
def create_share(project_id: str, body: ShareCreate, _: models.Owner = Depends(get_current_owner), db: Session = Depends(get_db)):
  with PROJECT_LIFECYCLE_LOCK:
    scrub_expired(db)
    project = _live_project(db, project_id)
    package = source_package(project)
    if digest(package) != body.digest:
      raise HTTPException(409, 'Project files changed. Review the files again before sharing.')
    paths = set(body.paths)
    if len(paths) != len(body.paths) or not paths.issubset({row['path'] for row in package['files']}):
      raise HTTPException(422, 'Select only files from the reviewed list.')
    package['files'] = [row for row in package['files'] if row['path'] in paths]
    validate_package(package)
    if db.query(models.ProjectSourceCopy).filter_by(project_id=project_id, revoked_at=None).filter(models.ProjectSourceCopy.expires_at > now_naive_utc()).count() >= 20:
      raise HTTPException(409, 'Stop sharing an existing copy before creating another link (20 active links maximum).')
    token = secrets.token_urlsafe(32)
    row = models.ProjectSourceCopy(id=str(uuid.uuid4()), project_id=project_id, token_hash=hashlib.sha256(token.encode()).hexdigest(), package_json=package, expires_at=now_naive_utc() + timedelta(days=7))
    db.add(row)
    db.commit()
    return {**share_view(row), 'copy_url': get_settings().frontend_origin.rstrip('/') + '/project-copy#' + token}

@router.delete('/shares/{share_id}', status_code=204, dependencies=[Depends(reject_cross_site)])
def revoke_share(share_id: str, _: models.Owner = Depends(get_current_owner), db: Session = Depends(get_db)):
  row = db.get(models.ProjectSourceCopy, share_id)
  if row is None:
    raise HTTPException(404, 'Shared copy not found.')
  row.revoked_at = now_naive_utc()
  row.package_json = None
  db.commit()
  return Response(status_code=204)

@router.post('/package')
def public_package(body: CopyToken, response: Response, db: Session = Depends(get_db)):
  row = db.query(models.ProjectSourceCopy).filter_by(token_hash=hashlib.sha256(body.token.encode()).hexdigest(), revoked_at=None).filter(models.ProjectSourceCopy.expires_at > now_naive_utc()).first()
  if row is None or row.package_json is None:
    raise HTTPException(404, 'This copy link has expired or stopped being shared.')
  _live_project(db, row.project_id)
  response.headers['Cache-Control'] = 'no-store'
  response.headers['Referrer-Policy'] = 'no-referrer'
  return row.package_json

async def fetch_package(url: str) -> dict:
  try:
    parsed = urlsplit(url)
  except ValueError as exc:
    raise HTTPException(422, 'Enter a complete Share a copy link.') from exc
  if parsed.scheme not in {'http', 'https'} or parsed.path not in {'/project-copy', '/project-copy/'} or parsed.query or parsed.username or parsed.password or not re.fullmatch(r'[A-Za-z0-9_-]{43}', parsed.fragment):
    raise HTTPException(422, 'Enter a complete Share a copy link.')
  target = urlunsplit((parsed.scheme, parsed.netloc, '/api/project-copies/package', '', ''))
  try:
    result = await federation_request('POST', target, json={'token': parsed.fragment}, max_response_bytes=MAX_PACKAGE)
  except (FederationTransportError, ValueError, httpx.HTTPError) as exc:
    raise HTTPException(422, 'This project copy could not be downloaded safely.') from exc
  if result.status_code != 200:
    raise HTTPException(422, 'This copy link is unavailable or has expired.')
  try:
    package = result.json()
  except ValueError as exc:
    raise HTTPException(422, 'The copy contains invalid project data.') from exc
  return validate_package(package)

def local_template(db, project_type):
  try:
    template, app = _template_by_id(db, project_type)
    return project_type, template, app, None
  except HTTPException as exc:
    if exc.status_code != 422:
      raise
    template, app = _template_by_id(db, 'blank')
    return 'blank', template, app, 'This project’s builder is not installed here. Files remain editable.'


@router.post('/preview', dependencies=[Depends(reject_cross_site)])
async def preview_remote(body: CopyURL, _: models.Owner = Depends(get_current_owner), db: Session = Depends(get_db)):
  package = await fetch_package(body.url)
  kind, _template, _app, warning = local_template(db, package['project_type'])
  return {**preview(package), 'imported_type': kind, 'copy_warning': warning}

@router.post('/import', dependencies=[Depends(reject_cross_site)])
async def import_copy(body: CopyImport, _: models.Owner = Depends(get_current_owner), db: Session = Depends(get_db)):
  project_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'mobius:source-copy:' + body.recovery_request_id)) if body.recovery_request_id else str(uuid.uuid4())
  def existing_copy():
    existing = db.get(models.Project, project_id)
    if existing is None:
      return None
    if existing.deleted_at is not None:
      raise HTTPException(409, 'This imported project was deleted.')
    if (existing.template_snapshot_json or {}).get('source_copy', {}).get('digest') != body.digest:
      raise HTTPException(409, 'This import request was already used for a different copy.')
    return {**_project_response(existing), 'copy_warning': None}
  with PROJECT_LIFECYCLE_LOCK:
    existing = existing_copy()
    if existing is not None:
      return existing
  package = await fetch_package(body.url)
  if digest(package) != body.digest:
    raise HTTPException(409, 'The copy differs from the one you reviewed. Review it again.')
  with PROJECT_LIFECYCLE_LOCK:
    existing = existing_copy()
    if existing is not None:
      return existing
    locator = Path('projects') / project_id
    root = Path(get_settings().data_dir) / locator
    # Shared bytes never supply executable builder contracts: resolve only
    # the recipient's own installed template with the same identity.
    template_id, template, app, warning = local_template(db, package['project_type'])
    snapshot = _safe_template(template, app)
    snapshot['files'] = {}
    snapshot['source_copy'] = {'digest': body.digest}
    if root.exists() or root.is_symlink():
      raise HTTPException(409, 'The project destination already exists.')
    root.mkdir(parents=True)
    try:
      for item in package['files']:
        target = workspace_files.resolve_path(root, item['path'])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(item['content'], validate=True))
      project = models.Project(id=project_id, name=(body.name or package['name']).strip() or 'Project copy', project_type=template_id, root_path=locator.as_posix(), chat_id=None, source_app_id=app.id if app is not None else None, template_snapshot_json=snapshot, artifacts_json=None)
      db.add(project)
      db.commit()
    except Exception:
      db.rollback()
      shutil.rmtree(root, ignore_errors=True)
      raise
    db.refresh(project)
    return {**_project_response(project), 'copy_warning': warning}

@router.post('/metadata')
def public_metadata(body: CopyToken, response: Response, db: Session = Depends(get_db)):
  return preview(public_package(body, response, db))
