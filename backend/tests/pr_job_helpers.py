"""A PR job source for tests: what the routes hand the status and merge code as ``pr_job``."""
from contextlib import asynccontextmanager


def holding(job=None):
  """A ``pr_job`` whose every look answers ``job``, or ``job()`` when it is callable."""
  look = job if callable(job) else (lambda: job)

  @asynccontextmanager
  async def hold():
    yield look

  return hold
