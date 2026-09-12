/* Shared visual identity for project artifacts in the workspace and Recents. */
import FileImage from 'lucide-react/dist/esm/icons/file-image.mjs'
import FileText from 'lucide-react/dist/esm/icons/file-text.mjs'
import Blocks from 'lucide-react/dist/esm/icons/blocks.mjs'
import Globe2 from 'lucide-react/dist/esm/icons/globe-2.mjs'
import Sigma from 'lucide-react/dist/esm/icons/sigma.mjs'
import { artifactVisualKind } from '../../lib/projectArtifacts.js'

export default function ProjectArtifactIcon({ artifact, size = 18, ...props }) {
  const kind = artifactVisualKind(artifact)
  if (kind === 'pdf') return <Sigma size={size} {...props} />
  if (kind === 'image') return <FileImage size={size} {...props} />
  if (kind === 'mini-app') return <Blocks size={size} {...props} />
  if (kind === 'html') return <Globe2 size={size} {...props} />
  return <FileText size={size} {...props} />
}
