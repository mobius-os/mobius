"""Serve mutable storage files from the inode that owns their version token."""

import os
import stat

import anyio
from fastapi import HTTPException
from fastapi.responses import FileResponse

from app.storage_io import file_version_token


class StorageFileResponse(FileResponse):
  """Keep atomic replacements from changing a deferred FileResponse's body."""

  async def __call__(self, scope, receive, send):
    try:
      source = await anyio.to_thread.run_sync(open, self.path, "rb")
    except (FileNotFoundError, IsADirectoryError):
      raise HTTPException(404, "File not found.")
    with source:
      self.stat_result = os.fstat(source.fileno())
      if not stat.S_ISREG(self.stat_result.st_mode):
        raise HTTPException(404, "File not found.")
      self.headers["etag"] = file_version_token(
        self.path, stat_result=self.stat_result,
      )
      self.set_stat_headers(self.stat_result)
      # The Linux container's descriptor alias keeps Starlette's normal
      # bounded reads, seeking and multipart ranges on this exact inode.
      self.path = f"/proc/self/fd/{source.fileno()}"
      # Do not hand a descriptor alias to an offloaded path-send whose
      # lifetime could extend beyond this response's owned file handle.
      scope = {
        **scope,
        "extensions": {
          name: value for name, value in scope.get("extensions", {}).items()
          if name != "http.response.pathsend"
        },
      }
      await super().__call__(scope, receive, send)
