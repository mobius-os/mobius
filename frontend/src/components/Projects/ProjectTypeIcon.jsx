/* Project kinds choose familiar glyphs; unfamiliar Project apps keep a neutral folder. */
import { FileDocument, Folder, FolderShared, Chart, FileCode, FilePresentation, WebsiteNetwork, Functions, Grid } from '@openai/apps-sdk-ui/components/Icon'
import { defaultProjectName, projectTypeKind } from '../../lib/projectTypes.js'
export { defaultProjectName, projectTypeKind }
const ICONS = { github: FolderShared, latex: Functions, visualization: Chart, 'mini-app': FileCode, slides: FilePresentation, web: WebsiteNetwork, sheet: Grid, document: FileDocument }
export default function ProjectTypeIcon({ value, size = 20, strokeWidth, ...props }) {
  const Icon = ICONS[projectTypeKind(value)] || Folder
  return <Icon width={size} height={size} {...props} />
}
