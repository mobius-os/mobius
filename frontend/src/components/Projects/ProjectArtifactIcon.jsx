/* Shared visual identity for project artifacts in the workspace and Recents. */
import FileImage from 'lucide-react/dist/esm/icons/file-image.mjs'
import FileText from 'lucide-react/dist/esm/icons/file-text.mjs'
import Blocks from 'lucide-react/dist/esm/icons/blocks.mjs'
import ChartNoAxesCombined from 'lucide-react/dist/esm/icons/chart-no-axes-combined.mjs'
import Globe2 from 'lucide-react/dist/esm/icons/globe-2.mjs'
import Presentation from 'lucide-react/dist/esm/icons/presentation.mjs'
import Sigma from 'lucide-react/dist/esm/icons/sigma.mjs'
import Table2 from 'lucide-react/dist/esm/icons/table-2.mjs'
import { artifactVisualKind } from '../../lib/projectArtifacts.js'

export default function ProjectArtifactIcon({ artifact, size = 18, ...props }) {
  const kind = artifactVisualKind(artifact)
  if (kind === 'pdf') return <Sigma size={size} {...props} />
  if (kind === 'image') return <FileImage size={size} {...props} />
  if (kind === 'presentation') return <Presentation size={size} {...props} />
  if (kind === 'sheet') return <Table2 size={size} {...props} />
  if (kind === 'document') return <FileText size={size} {...props} />
  if (kind === 'visualization') return <ChartNoAxesCombined size={size} {...props} />
  if (kind === 'mini-app') return <Blocks size={size} {...props} />
  return <Globe2 size={size} {...props} />
}
