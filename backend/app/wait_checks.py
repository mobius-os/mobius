"""The typed GitHub wait contract, separate from custom shell exit semantics."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GitHubChecks(BaseModel):
  model_config = ConfigDict(extra="forbid")

  repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)
  pull_request: int = Field(gt=0, strict=True)
  head_sha: str = Field(pattern=r"^[a-fA-F0-9]{7,40}$")

  @property
  def url(self) -> str:
    return f"https://github.com/{self.repository}/pull/{self.pull_request}/checks"

  def command(self) -> str:
    script = Path(__file__).resolve().parents[2] / "scripts" / "pr-checks.py"
    return shlex.join([
      sys.executable, str(script), "--json", self.repository,
      str(self.pull_request), self.head_sha,
    ])


class CheckObservation(BaseModel):
  """Bounded progress from the standard checker, never arbitrary shell output."""

  model_config = ConfigDict(extra="ignore")
  state: Literal["pending", "met", "failed"]
  summary: str = Field(min_length=1, max_length=500)
  completed: int = Field(ge=0, strict=True)
  total: int = Field(ge=0, strict=True)

  @model_validator(mode="after")
  def counts_agree(self):
    if self.completed > self.total:
      raise ValueError("completed checks exceed total")
    if self.state == "met" and (not self.total or self.completed != self.total):
      raise ValueError("completion needs a nonempty finished check set")
    if self.state == "pending" and self.total and self.completed == self.total:
      raise ValueError("pending checks need unfinished work, or no checks yet")
    return self


def read_check_observation(exit_code: int, output: str | None) -> CheckObservation:
  if exit_code == 0:
    try:
      return CheckObservation.model_validate_json(output or "")
    except ValueError:
      pass
  return CheckObservation(
    state="failed", summary="The GitHub check could not be read. The agent will investigate.",
    completed=0, total=0,
  )
