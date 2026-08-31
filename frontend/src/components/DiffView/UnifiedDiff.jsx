// CANONICAL DIFF VIEWER: copy this entire folder verbatim. It imports only
// React and its own flat sibling modules. Styles ship as a JavaScript string
// because the mini-app compiler rejects CSS side-output.
// Parse and render one raw unified patch through the canonical file viewer.

import { useMemo } from 'react'
import FileDiffList from './FileDiffList.jsx'
import { parseUnifiedDiff } from './parseUnifiedDiff.js'

export default function UnifiedDiff({
  diff,
  summaryOverrides,
  diffTruncated = false,
}) {
  const files = useMemo(() => parseUnifiedDiff(diff), [diff])
  return (
    <FileDiffList
      files={files}
      summaryOverrides={summaryOverrides}
      diffTruncated={diffTruncated}
    />
  )
}
