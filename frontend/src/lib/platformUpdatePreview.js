// Pure helpers for the platform update-review sheet. Kept side-effect-free (no
// React, no fetch) so the "what does this preview contain" decisions — trivial
// vs reviewable, the compact totals, the short sha — are unit-testable in
// isolation, mirroring restartReadiness.js / streamPromotion.js.

// Abbreviate a commit sha for display. The backend already sends short shas, so
// this only guards against a full sha slipping through.
export function shortSha(sha) {
  const s = typeof sha === 'string' ? sha.trim() : ''
  return s ? s.slice(0, 8) : ''
}

// The compact totals the sheet header shows. Insertions/deletions skip binary
// files (backend sends null counts for those) so the numbers stay meaningful.
export function summarizePreview(preview) {
  const files = Array.isArray(preview?.files) ? preview.files : []
  const commits = Array.isArray(preview?.commits) ? preview.commits : []
  const totalCommits = Number.isInteger(preview?.total_commits)
    && preview.total_commits >= commits.length
    ? preview.total_commits
    : commits.length
  let insertions = 0
  let deletions = 0
  for (const file of files) {
    if (typeof file?.insertions === 'number') insertions += file.insertions
    if (typeof file?.deletions === 'number') deletions += file.deletions
  }
  const diff = typeof preview?.diff === 'string' ? preview.diff : ''
  return {
    commitCount: totalCommits,
    listedCommitCount: commits.length,
    commitsTruncated: !!preview?.commits_truncated
      || totalCommits > commits.length,
    fileCount: files.length,
    insertions,
    deletions,
    hasDiff: diff.length > 0,
    diffTruncated: !!preview?.diff_truncated,
  }
}

// A trivial update carries no file changes (e.g. a version/commit bump with no
// tree delta): the sheet then shows a one-line confirm, never an empty diff
// panel. An update with no preview data at all (files + commits both empty) is
// also trivial — there's nothing to render but the Apply confirmation.
export function isTrivialUpdate(preview) {
  const files = Array.isArray(preview?.files) ? preview.files : []
  return files.length === 0
}

// Whether the sheet has anything worth showing beyond the Apply confirmation.
export function hasReviewableChanges(preview) {
  const commits = Array.isArray(preview?.commits) ? preview.commits : []
  return !isTrivialUpdate(preview) || commits.length > 0
}
