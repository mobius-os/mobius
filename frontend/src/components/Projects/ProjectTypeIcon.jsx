import FileText from 'lucide-react/dist/esm/icons/file-text.mjs'
import FolderKanban from 'lucide-react/dist/esm/icons/folder-kanban.mjs'
import Github from 'lucide-react/dist/esm/icons/folder-git-2.mjs'
import ChartNoAxesCombined from 'lucide-react/dist/esm/icons/chart-no-axes-combined.mjs'
import FileCode2 from 'lucide-react/dist/esm/icons/file-code-2.mjs'
import Presentation from 'lucide-react/dist/esm/icons/presentation.mjs'
import Globe2 from 'lucide-react/dist/esm/icons/globe-2.mjs'
import SquareFunction from 'lucide-react/dist/esm/icons/square-function.mjs'
import Table2 from 'lucide-react/dist/esm/icons/table-2.mjs'
import { defaultProjectName, projectTypeKind } from '../../lib/projectTypes.js'

export { defaultProjectName, projectTypeKind }

export default function ProjectTypeIcon({ value, size = 20, ...props }) {
  const kind = projectTypeKind(value)
  if (kind === 'github') return <Github size={size} {...props} />
  if (kind === 'latex') return <SquareFunction size={size} {...props} />
  if (kind === 'visualization') return <ChartNoAxesCombined size={size} {...props} />
  if (kind === 'mini-app') return <FileCode2 size={size} {...props} />
  if (kind === 'slides') return <Presentation size={size} {...props} />
  if (kind === 'web') return <Globe2 size={size} {...props} />
  if (kind === 'sheet') return <Table2 size={size} {...props} />
  if (kind === 'document') return <FileText size={size} {...props} />
  return <FolderKanban size={size} {...props} />
}
