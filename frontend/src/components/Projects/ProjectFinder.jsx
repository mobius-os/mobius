import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import DOMPurify from 'dompurify'
import { Marked } from 'marked'
import ArrowLeft from 'lucide-react/dist/esm/icons/arrow-left.mjs'
import ChevronRight from 'lucide-react/dist/esm/icons/chevron-right.mjs'
import Download from 'lucide-react/dist/esm/icons/download.mjs'
import Ellipsis from 'lucide-react/dist/esm/icons/ellipsis.mjs'
import File from 'lucide-react/dist/esm/icons/file.mjs'
import FileCode from 'lucide-react/dist/esm/icons/file-code.mjs'
import FileText from 'lucide-react/dist/esm/icons/file-text.mjs'
import Folder from 'lucide-react/dist/esm/icons/folder.mjs'
import FolderPlus from 'lucide-react/dist/esm/icons/folder-plus.mjs'
import GitBranch from 'lucide-react/dist/esm/icons/git-branch.mjs'
import GitCommitHorizontal from 'lucide-react/dist/esm/icons/git-commit-horizontal.mjs'
import Hammer from 'lucide-react/dist/esm/icons/hammer.mjs'
import Image from 'lucide-react/dist/esm/icons/image.mjs'
import Pencil from 'lucide-react/dist/esm/icons/pencil.mjs'
import Plus from 'lucide-react/dist/esm/icons/plus.mjs'
import Search from 'lucide-react/dist/esm/icons/search.mjs'
import Upload from 'lucide-react/dist/esm/icons/upload.mjs'
import Trash2 from 'lucide-react/dist/esm/icons/trash-2.mjs'
import { api, jsonOrThrow } from '../../api/client.js'
import { assembleProjectHtmlPreview } from '../../lib/projectPreview.js'
import { artifactTypeForFile } from '../../lib/projectArtifacts.js'
import { lineNumbersFor, windowedCode } from '../../lib/codeWindow.js'
import {
  addProjectCsvColumn,
  addProjectCsvRow,
  parseProjectCsv,
  serializeProjectCsv,
  updateProjectCsvCell,
} from '../../lib/projectFormats.js'
import {
  gitAnnotationForEntry,
  canInitializeProjectGit,
  gitChangeCount,
  gitIdentityLabel,
  gitStatusPresentation,
} from '../../lib/projectGit.js'
import { useHistoryDismissControls } from '../../hooks/useHistoryDismiss.jsx'
import {
  back as finderBack,
  filterEntries,
  finderCrumbs,
  initFinder,
  joinPath,
  openFile as finderOpenFile,
  openFolder as finderOpenFolder,
  parentPath,
} from '../../lib/projectFinderNav.js'
import ProjectPdfPreview from './ProjectPdfPreview.jsx'
import ImageLightbox from '../ChatView/markdown/ImageLightbox.jsx'
import ProjectPreviewFrame from './ProjectPreviewFrame.jsx'
import { highlightCode } from '../ChatView/markdown/highlight.js'
import './Projects.css'

// A scan that settles inside this window swaps content with no spinner at all —
// only a genuinely slow read surfaces the indicator (deepseek-harness pattern).
const SLOW_SCAN_DELAY_MS = 300
// Highlighting/rendering the whole of a very large file janks the tab; window it
// to the head and offer Download for the rest (Möbius users pay their own CPU).
const WINDOW_CHARS = 200_000
const PROJECT_MARKDOWN = new Marked({ gfm: true, breaks: false })

const CODE_EXTS = new Set([
  'html', 'htm', 'css', 'js', 'jsx', 'ts', 'tsx', 'mjs', 'cjs', 'json', 'py',
  'sh', 'bash', 'yml', 'yaml', 'toml', 'sql', 'rs', 'go', 'java', 'c', 'cpp',
  'h', 'xml', 'svg',
])
const HLJS_LANG = {
  js: 'javascript', jsx: 'javascript', mjs: 'javascript', cjs: 'javascript',
  ts: 'typescript', tsx: 'typescript', py: 'python', sh: 'bash', bash: 'bash',
  yml: 'yaml', html: 'xml', htm: 'xml', svg: 'xml', json: 'json', css: 'css',
  sql: 'sql',
}

function extensionOf(path) {
  return String(path ?? '').split('.').pop()?.toLowerCase() || ''
}

function entryIcon(entry, size) {
  if (entry.type === 'directory') return <Folder size={size} />
  const ext = extensionOf(entry.name)
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'avif'].includes(ext)) return <Image size={size} />
  if (CODE_EXTS.has(ext)) return <FileCode size={size} />
  if (['md', 'txt', 'tex', 'csv', 'pdf'].includes(ext)) return <FileText size={size} />
  return <File size={size} />
}

function formatSize(bytes) {
  if (!Number.isFinite(bytes)) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// The Finder: a breadcrumb bar over a list (mobile) or list + preview pane
// (desktop, container-query driven), with in-place inspection that never leaves
// the tab. Folder + file navigation is wired into the shell's history stack so
// the browser Back button walks back through it in-tab.
export default function ProjectFinder({
  projectId,
  projectName,
  artifactTypes,
  onBuildFile,
  onSourceSaved,
  overview,
  fileSource,
}) {
  const history = useHistoryDismissControls()
  const queryClient = useQueryClient()
  const source = useMemo(() => fileSource || ({
    id: String(projectId),
    readOnly: false,
    liveSync: true,
    filesKey: nextPath => ['projects', 'files', projectId, nextPath],
    gitStatusKey: () => ['projects', 'git', projectId, 'status'],
    gitDiffKey: nextPath => ['projects', 'git', projectId, 'diff', nextPath],
    files: (nextPath, options) => api.projects.files(projectId, nextPath, options),
    gitStatus: options => api.projects.gitStatus(projectId, options),
    gitDiff: (nextPath, options) => api.projects.gitDiff(projectId, nextPath, options),
    initGit: () => api.projects.initGit(projectId),
    commitGit: payload => api.projects.commitGit(projectId, payload),
    readFile: (nextPath, options) => api.projects.readFile(projectId, nextPath, options),
    changes: (after, options) => api.projects.changes(projectId, after, options),
    claimWork: payload => api.projects.claimWork(projectId, payload),
    releaseWork: () => api.projects.releaseWork(projectId),
    writeFile: (nextPath, content, expectedRevision) => (
      api.projects.writeFile(projectId, nextPath, content, expectedRevision)
    ),
    writeBytes: (nextPath, bytes, expectedRevision) => (
      api.projects.writeBytes(projectId, nextPath, bytes, expectedRevision)
    ),
    createFolder: nextPath => api.projects.createFolder(projectId, nextPath),
    deleteFile: nextPath => api.projects.deleteFile(projectId, nextPath),
    move: payload => api.projects.move(projectId, payload),
    invalidate: client => Promise.all([
      client.invalidateQueries({ queryKey: ['projects', 'files', projectId] }),
      client.invalidateQueries({ queryKey: ['projects', 'git', projectId] }),
    ]),
  }), [fileSource, projectId])
  const readOnly = !!source.readOnly
  const [nav, setNav] = useState(() => initFinder())
  const path = nav.current.path
  const selected = nav.current.selected

  // Parallel stack of history entry ids, one per forward step, popped LIFO by
  // Back or by the in-UI back controls so history and nav stay in sync.
  const entryStackRef = useRef([])
  const navRef = useRef(nav)
  navRef.current = nav

  // Browser Back / swipe consumes the top sentinel and lands here: pop one nav
  // step. Stable identity — every sentinel shares it (Back always pops the top).
  const onHistoryPop = useCallback(() => {
    entryStackRef.current.pop()
    const next = finderBack(navRef.current).state
    navRef.current = next
    setNav(next)
  }, [])

  // A forward step (open folder / open file / crumb jump). Compute the transition
  // from the current nav, then — only for a real change — push one history
  // sentinel so the browser Back button retraces it in-tab. The history push is
  // kept OUT of the setState updater (updaters must stay pure / StrictMode-safe).
  const goForward = useCallback((compute) => {
    const { state, pushed } = compute(navRef.current)
    if (!pushed) return
    navRef.current = state
    setNav(state)
    if (history?.open) {
      const id = history.open(onHistoryPop)
      if (id) entryStackRef.current.push(id)
    }
  }, [history, onHistoryPop])

  // The in-UI back controls (parent folder, close file, breadcrumb-up) go
  // through the SAME pop path as the browser Back button by consuming the top
  // sentinel, whose dismissal calls onHistoryPop.
  const goBack = useCallback(() => {
    const id = entryStackRef.current[entryStackRef.current.length - 1]
    if (id && history?.close) history.close(id)
    else setNav(current => finderBack(current).state)
  }, [history])

  const openFolder = useCallback((next) => goForward(s => finderOpenFolder(s, next)), [goForward])
  const openFileAt = useCallback((filePath) => goForward(s => finderOpenFile(s, filePath)), [goForward])

  // Reset the finder for a new project, and on unmount / project switch release
  // its history sentinels without a traversal (just drop the registrations).
  useEffect(() => {
    navRef.current = initFinder()
    setNav(initFinder())
    return () => {
      for (const id of entryStackRef.current) history?.unregister?.(id)
      entryStackRef.current = []
    }
  }, [source.id, history])

  // ── Folder listing: keep the stale view while the next folder loads, and only
  // reveal a spinner if the scan stays silent past SLOW_SCAN_DELAY_MS.
  const filesQuery = useQuery({
    queryKey: source.filesKey(path),
    queryFn: async ({ signal }) => jsonOrThrow(
      await source.files(path, { signal }),
      'Files failed:',
    ),
    placeholderData: keepPreviousData,
  })
  const entries = useMemo(
    () => filesQuery.data?.entries || [],
    [filesQuery.data?.entries],
  )
  const gitQuery = useQuery({
    queryKey: source.gitStatusKey(),
    queryFn: async ({ signal }) => jsonOrThrow(
      await source.gitStatus({ signal }),
      'Changes failed:',
    ),
    staleTime: 5_000,
  })
  const gitStatus = gitQuery.data
  const gitChanges = useMemo(() => gitStatus?.changes || [], [gitStatus?.changes])
  const gitTotal = gitChangeCount(gitStatus)
  const [commitOpen, setCommitOpen] = useState(false)
  const [commitMessage, setCommitMessage] = useState('')
  const [versionBusy, setVersionBusy] = useState(false)
  const [versionError, setVersionError] = useState('')
  // Type-to-filter narrows the visible entries; Enter opens the first match.
  // Filter state is per-folder — navigating anywhere clears it.
  const [filter, setFilter] = useState('')
  useEffect(() => { setFilter('') }, [path])
  const visibleEntries = useMemo(() => filterEntries(entries, filter), [entries, filter])
  const [slowScan, setSlowScan] = useState(false)
  useEffect(() => {
    if (!filesQuery.isFetching) { setSlowScan(false); return undefined }
    const timer = setTimeout(() => setSlowScan(true), SLOW_SCAN_DELAY_MS)
    return () => clearTimeout(timer)
  }, [filesQuery.isFetching])

  // Tail-pin the breadcrumb: deep paths overflow, keep the current directory in
  // view whenever the chain grows.
  const crumbTrailRef = useRef(null)
  const crumbs = useMemo(() => finderCrumbs('Files', path), [path])
  useEffect(() => {
    const el = crumbTrailRef.current
    if (el) el.scrollLeft = el.scrollWidth
  }, [crumbs])

  // ── Inspection surface state (driven by `selected`) ─────────────────────────
  const [fileKind, setFileKind] = useState('none') // none|text|image|pdf|binary|error
  const [content, setContent] = useState('')
  const [baseline, setBaseline] = useState('')
  const [revision, setRevision] = useState(null)
  const [remoteChange, setRemoteChange] = useState(null)
  const [htmlPreview, setHtmlPreview] = useState('')
  const [htmlPreviewError, setHtmlPreviewError] = useState('')
  const [objectUrl, setObjectUrl] = useState(null)
  const [pdfData, setPdfData] = useState(null)
  const [fileError, setFileError] = useState('')
  const [fileLoading, setFileLoading] = useState(false)
  const [editing, setEditing] = useState(false)
  const [highlighted, setHighlighted] = useState(null)
  const [lightboxOpen, setLightboxOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const dirty = fileKind === 'text' && editing && content !== baseline
  const liveFileRef = useRef(null)
  liveFileRef.current = { selected, dirty, editing, revision }

  const objectUrlRef = useRef(null)
  const replaceObjectUrl = useCallback((next) => {
    if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current)
    objectUrlRef.current = next
    setObjectUrl(next)
  }, [])
  useEffect(() => () => { if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current) }, [])

  // Window big files to the head, cut on a line boundary so the preview can
  // show real line numbers and an honest "showing N of M lines".
  const codeWindow = useMemo(
    () => (fileKind === 'text' ? windowedCode(content, WINDOW_CHARS) : null),
    [fileKind, content],
  )
  const windowed = !!codeWindow?.windowed
  const codeLike = selected && CODE_EXTS.has(extensionOf(selected))
  const htmlLike = selected && ['html', 'htm'].includes(extensionOf(selected))
  const markdownLike = selected && ['md', 'markdown'].includes(extensionOf(selected))
  const csvLike = selected && extensionOf(selected) === 'csv'
  const markdownPreview = useMemo(() => {
    if (fileKind !== 'text' || !markdownLike) return ''
    return DOMPurify.sanitize(String(PROJECT_MARKDOWN.parse(content) || ''))
  }, [content, fileKind, markdownLike])
  const csvRows = useMemo(
    () => (fileKind === 'text' && csvLike ? parseProjectCsv(content) : []),
    [content, csvLike, fileKind],
  )
  const selectedGitChange = useMemo(
    () => gitChanges.find(change => change.path === selected) || null,
    [gitChanges, selected],
  )
  const gitDiffQuery = useQuery({
    queryKey: source.gitDiffKey(selected || ''),
    queryFn: async ({ signal }) => jsonOrThrow(
      await source.gitDiff(selected, { signal }),
      'File changes failed:',
    ),
    enabled: !!selected && !!selectedGitChange && selectedGitChange.status !== 'deleted',
    staleTime: 5_000,
  })
  const gitDiff = gitDiffQuery.data
  const changedLines = useMemo(
    () => new Set(gitDiff?.changed_lines || []),
    [gitDiff?.changed_lines],
  )

  // Load whatever `selected` names. Supersede an in-flight read (AbortController)
  // and keep the previously-shown file until the new one arrives.
  useEffect(() => {
    if (!selected) {
      setFileKind('none'); setContent(''); setBaseline(''); setHighlighted(null)
      setRevision(null); setRemoteChange(null)
      setPdfData(null); replaceObjectUrl(null); setFileError(''); setEditing(false)
      return undefined
    }
    const controller = new AbortController()
    let active = true
    setFileError('')
    setEditing(false)
    const spinner = setTimeout(() => { if (active) setFileLoading(true) }, SLOW_SCAN_DELAY_MS)
    ;(async () => {
      try {
        const res = await source.readFile(selected, { signal: controller.signal })
        if (!active) return
        const type = res.headers.get('content-type') || ''
        if (type.includes('application/json')) {
          const data = await jsonOrThrow(res, 'File open failed:')
          if (!active) return
          setContent(data.content); setBaseline(data.content); setFileKind('text')
          setRevision(data.revision || null); setRemoteChange(null)
          setPdfData(null); replaceObjectUrl(null)
        } else if (res.ok) {
          const blob = await res.blob()
          if (!active) return
          const isPdf = blob.type === 'application/pdf' || selected.toLowerCase().endsWith('.pdf')
          if (isPdf) {
            replaceObjectUrl(null)
            setPdfData(new Uint8Array(await blob.arrayBuffer())); setFileKind('pdf')
          } else if (blob.type.startsWith('image/')) {
            setPdfData(null); replaceObjectUrl(URL.createObjectURL(blob)); setFileKind('image')
          } else {
            setPdfData(null); replaceObjectUrl(URL.createObjectURL(blob)); setFileKind('binary')
          }
          setContent(''); setBaseline('')
          setRevision(res.headers.get('etag')?.replaceAll('"', '') || null)
          setRemoteChange(null)
        } else {
          throw new Error(`File open failed: ${res.status}`)
        }
      } catch (cause) {
        if (!active || cause?.name === 'AbortError') return
        setFileError(cause?.message || 'Could not open that file.')
        setFileKind('error')
      } finally {
        if (active) setFileLoading(false)
      }
    })()
    return () => { active = false; controller.abort(); clearTimeout(spinner) }
  }, [source, selected, replaceObjectUrl])

  const refreshSelectedFromRemote = useCallback(async ({ force = false, change = null } = {}) => {
    const snapshot = liveFileRef.current
    if (!snapshot?.selected) return
    const selectedAtStart = snapshot.selected
    const res = await source.readFile(selectedAtStart)
    if (res.status === 404) {
      if (liveFileRef.current?.selected !== selectedAtStart) return
      const movedTo = change?.prior_path === selectedAtStart ? change?.path || null : null
      if (movedTo && !liveFileRef.current?.dirty) {
        setRemoteChange(null)
        setNav(current => ({ ...current, current: { ...current.current, selected: movedTo } }))
        return
      }
      setRemoteChange({ deleted: true, movedTo })
      return
    }
    const type = res.headers.get('content-type') || ''
    if (type.includes('application/json')) {
      const data = await jsonOrThrow(res, 'File refresh failed:')
      if (liveFileRef.current?.selected !== selectedAtStart) return
      const nextRevision = data.revision || null
      if (nextRevision === liveFileRef.current?.revision) return
      if (!force && liveFileRef.current?.dirty) {
        setRemoteChange({ content: data.content, revision: nextRevision })
        return
      }
      setContent(data.content)
      setBaseline(data.content)
      setFileKind('text')
      setRevision(nextRevision)
      setRemoteChange(null)
      setPdfData(null)
      replaceObjectUrl(null)
      if (force) setEditing(false)
      return
    }
    if (!res.ok) throw new Error(`File refresh failed: ${res.status}`)
    const nextRevision = res.headers.get('etag')?.replaceAll('"', '') || null
    if (nextRevision && nextRevision === liveFileRef.current?.revision) return
    const blob = await res.blob()
    if (liveFileRef.current?.selected !== selectedAtStart) return
    const isPdf = blob.type === 'application/pdf' || selectedAtStart.toLowerCase().endsWith('.pdf')
    if (isPdf) {
      replaceObjectUrl(null)
      setPdfData(new Uint8Array(await blob.arrayBuffer()))
      setFileKind('pdf')
    } else if (blob.type.startsWith('image/')) {
      setPdfData(null)
      replaceObjectUrl(URL.createObjectURL(blob))
      setFileKind('image')
    } else {
      setPdfData(null)
      replaceObjectUrl(URL.createObjectURL(blob))
      setFileKind('binary')
    }
    setContent('')
    setBaseline('')
    setRevision(nextRevision)
    setRemoteChange(null)
  }, [replaceObjectUrl, source])

  // The durable cursor repairs missed owner events and gives invited editors
  // the same near-live file refresh without granting them the shell event
  // stream. Clean files update automatically; dirty drafts are never replaced.
  useEffect(() => {
    if (!source.liveSync || !source.changes) return undefined
    let active = true
    let cursor = null
    let controller = null
    let timer = null
    let delay = 2_500
    const handleChanges = async (changes, truncated = false) => {
      if (!active || (!truncated && (!changes || changes.length === 0))) return
      await source.invalidate(queryClient)
      const selectedNow = liveFileRef.current?.selected
      if (
        selectedNow
        && (truncated || changes.some(change => (
          !change?.path
          || change.path === selectedNow
          || change.prior_path === selectedNow
        )))
      ) {
        const selectedChange = changes.find(change => (
          !change?.path
          || change.path === selectedNow
          || change.prior_path === selectedNow
        )) || null
        try {
          await refreshSelectedFromRemote({ change: selectedChange })
        } catch { /* the completion-scheduled poll retries */ }
      }
    }
    const schedule = (wait = delay) => {
      if (!active) return
      window.clearTimeout(timer)
      timer = window.setTimeout(() => { void poll() }, wait)
    }
    const poll = async () => {
      if (!active) return
      if (document.hidden) return
      controller = new AbortController()
      try {
        const establishingBaseline = cursor === null
        const payload = await jsonOrThrow(
          await source.changes(cursor, { signal: controller.signal }),
          'Project refresh failed:',
        )
        if (!active) return
        cursor = Number(payload.cursor || cursor || 0)
        // Reconcile once after the baseline arrives. This closes the gap where
        // a save lands between the first file read and the first cursor read.
        await handleChanges(
          payload.changes || [],
          !!payload.truncated || establishingBaseline,
        )
        delay = 2_500
      } catch (cause) {
        if (cause?.name !== 'AbortError') delay = Math.min(delay * 2, 30_000)
      } finally {
        controller = null
        if (!document.hidden) schedule()
      }
    }
    const onLiveChange = event => {
      const detail = event?.detail
      if (String(detail?.projectId ?? '') !== String(projectId)) return
      void handleChanges(detail?.change ? [detail.change] : [], false)
    }
    const onVisibility = () => {
      if (document.hidden) {
        window.clearTimeout(timer)
        controller?.abort()
      } else {
        schedule(0)
      }
    }
    window.addEventListener('mobius:project-change', onLiveChange)
    document.addEventListener('visibilitychange', onVisibility)
    void poll()
    return () => {
      active = false
      controller?.abort()
      window.clearTimeout(timer)
      window.removeEventListener('mobius:project-change', onLiveChange)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [projectId, queryClient, refreshSelectedFromRemote, source])

  // Claims are expiring coordination hints. Selection/editing changes publish
  // immediately; a quiet heartbeat keeps the current scope visible.
  useEffect(() => {
    if (!source.claimWork) return undefined
    let active = true
    const claim = async () => {
      const target = selected || path || null
      const summary = selected
        ? `${editing ? 'Editing' : 'Viewing'} ${selected}`
        : path ? `Browsing ${path}` : 'Browsing project files'
      try {
        await source.claimWork({ path: target, summary })
      } catch { /* coordination must never block the editor */ }
    }
    void claim()
    const timer = window.setInterval(() => { if (active) void claim() }, 30_000)
    return () => { active = false; window.clearInterval(timer) }
  }, [editing, path, selected, source])
  useEffect(() => () => {
    const release = source.releaseWork?.()
    release?.catch?.(() => {})
  }, [source])

  // HTML is a first-class live editing surface. Keep source text as the one
  // editable value and assemble its isolated preview separately, so typing can
  // repaint the result without replacing the source with an inlined build.
  useEffect(() => {
    setHtmlPreviewError('')
    if (fileKind !== 'text' || !htmlLike) {
      setHtmlPreview('')
      return undefined
    }
    let active = true
    const timer = window.setTimeout(() => {
      ;(async () => {
        try {
          const assembled = await assembleProjectHtmlPreview(
            content,
            selected,
            async dep => (await jsonOrThrow(
              await source.readFile(dep), 'Preview dependency failed:',
            )).content,
          )
          if (active) setHtmlPreview(assembled)
        } catch (cause) {
          if (active) setHtmlPreviewError(cause?.message || 'Could not refresh the preview.')
        }
      })()
    }, editing ? 120 : 0)
    return () => { active = false; window.clearTimeout(timer) }
  }, [content, editing, fileKind, htmlLike, selected, source])

  // Lazy syntax highlight for the read view of a code file (windowed head only).
  useEffect(() => {
    setHighlighted(null)
    if (fileKind !== 'text' || editing || !codeLike || !content) return
    let active = true
    const slice = windowedCode(content, WINDOW_CHARS).text
    const lang = HLJS_LANG[extensionOf(selected)]
    // highlight.js already escapes the code text; DOMPurify is defense-in-depth
    // before dangerouslySetInnerHTML (same posture as the markdown CodeBlock).
    highlightCode(slice, lang).then(html => { if (active && html) setHighlighted(DOMPurify.sanitize(html)) })
    return () => { active = false }
  }, [fileKind, editing, codeLike, content, selected])

  // ── File mutations ──────────────────────────────────────────────────────────
  const refetchWorkspace = useCallback(async () => {
    await source.invalidate(queryClient)
  }, [source, queryClient])

  async function initializeVersioning() {
    if (!source.initGit || versionBusy) return
    setVersionBusy(true); setVersionError('')
    try {
      await jsonOrThrow(await source.initGit(), 'Versioning failed:')
      await refetchWorkspace()
    } catch (cause) {
      setVersionError(cause?.message || 'Could not start versioning.')
    } finally { setVersionBusy(false) }
  }

  async function commitChanges(event) {
    event.preventDefault()
    const message = commitMessage.trim()
    if (!source.commitGit || !message || versionBusy) return
    setVersionBusy(true); setVersionError('')
    try {
      await jsonOrThrow(await source.commitGit({
        message,
        expected_head: gitStatus?.head || null,
      }), 'Commit failed:')
      setCommitMessage(''); setCommitOpen(false)
      await refetchWorkspace()
    } catch (cause) {
      setVersionError(cause?.message || 'Could not commit these changes.')
    } finally { setVersionBusy(false) }
  }

  async function saveFile() {
    if (fileKind !== 'text' || busy || !dirty) return
    setBusy(true); setError('')
    try {
      const saved = await jsonOrThrow(
        await source.writeFile(selected, content, revision),
        'File save failed:',
      )
      setBaseline(content); setEditing(false)
      setRevision(saved.revision || null); setRemoteChange(null)
      await refetchWorkspace()
      if (onSourceSaved) {
        try {
          await onSourceSaved(selected)
        } catch (cause) {
          setError(cause?.message || 'The file was saved, but its Creation could not rebuild.')
        }
      }
    } catch (cause) {
      if (cause?.code === 'file_revision_conflict') {
        setError('')
        try { await refreshSelectedFromRemote() } catch { /* card retries below */ }
      } else {
        setError(cause?.message || 'Could not save that file.')
      }
    } finally { setBusy(false) }
  }

  async function saveConflictCopy() {
    if (!selected || busy) return
    const slash = selected.lastIndexOf('/')
    const folder = slash >= 0 ? selected.slice(0, slash + 1) : ''
    const name = slash >= 0 ? selected.slice(slash + 1) : selected
    const dot = name.lastIndexOf('.')
    const stem = dot > 0 ? name.slice(0, dot) : name
    const suffix = dot > 0 ? name.slice(dot) : ''
    const stamp = new Date().toISOString().slice(11, 19).replaceAll(':', '')
    const target = `${folder}${stem}-draft-${stamp}${suffix}`
    setBusy(true); setError('')
    try {
      await jsonOrThrow(
        await source.writeFile(target, content, null),
        'Draft copy failed:',
      )
      setRemoteChange(null)
      await refetchWorkspace()
      openFileAt(target)
    } catch (cause) {
      setError(cause?.message || 'Could not preserve the draft as a copy.')
    } finally { setBusy(false) }
  }

  function updateCsv(rowIndex, columnIndex, value) {
    setContent(serializeProjectCsv(updateProjectCsvCell(csvRows, rowIndex, columnIndex, value)))
  }

  function appendCsvRow() {
    setContent(serializeProjectCsv(addProjectCsvRow(csvRows)))
  }

  function appendCsvColumn() {
    setContent(serializeProjectCsv(addProjectCsvColumn(csvRows)))
  }

  async function downloadFile(target = selected) {
    if (!target) return
    try {
      const res = await source.readFile(target, { download: true })
      if (!res.ok) throw new Error(`Download failed: ${res.status}`)
      const url = URL.createObjectURL(await res.blob())
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = target.split('/').pop()
      anchor.click()
      setTimeout(() => URL.revokeObjectURL(url), 0)
    } catch (cause) {
      setError(cause?.message || 'Could not download that file.')
    }
  }

  async function deleteEntry(entryPath) {
    if (busy || !window.confirm(`Delete “${entryPath}”?`)) return
    setBusy(true); setError('')
    try {
      await jsonOrThrow(await source.deleteFile(entryPath), 'File deletion failed:')
      if (selected === entryPath) goBack()
      await refetchWorkspace()
    } catch (cause) {
      setError(cause?.message || 'Could not delete that.')
    } finally { setBusy(false) }
  }

  // Rename + move both go through POST /{id}/move — rename keeps the parent dir,
  // move retargets it. `to` is the full destination path.
  async function moveEntry(from, to) {
    const target = String(to || '').trim()
    if (!target || target === from) return true
    setBusy(true); setError('')
    try {
      await jsonOrThrow(await source.move({ from_path: from, to_path: target }), 'Move failed:')
      if (selected === from) {
        // The inspected file moved — follow it so the preview stays coherent.
        setNav(current => ({ ...current, current: { ...current.current, selected: target } }))
      }
      await refetchWorkspace()
      return true
    } catch (cause) {
      setError(cause?.message || 'Could not move that.')
      return false
    } finally { setBusy(false) }
  }

  // ── Create / upload ───────────────────────────────────────────────────────
  const [creation, setCreation] = useState(null) // 'file' | 'folder' | null
  const [creationName, setCreationName] = useState('')
  const uploadRef = useRef(null)
  async function submitCreate(event) {
    event.preventDefault()
    const name = creationName.trim()
    if (!name || !creation || busy) return
    const target = joinPath(path, name)
    setBusy(true); setError('')
    try {
      if (creation === 'file') {
        await jsonOrThrow(await source.writeFile(target, '', null), 'File creation failed:')
      } else {
        await jsonOrThrow(await source.createFolder(target), 'Folder creation failed:')
      }
      setCreation(null); setCreationName('')
      await refetchWorkspace()
      if (creation === 'file') openFileAt(target)
    } catch (cause) {
      setError(cause?.message || `Could not create that ${creation}.`)
    } finally { setBusy(false) }
  }
  async function uploadFiles(event) {
    const files = [...(event.target.files || [])]
    event.target.value = ''
    if (files.length === 0) return
    setBusy(true); setError('')
    try {
      for (const file of files) {
        await jsonOrThrow(
          await source.writeBytes(joinPath(path, file.name), await file.arrayBuffer(), null),
          `Upload of ${file.name} failed:`,
        )
      }
      await refetchWorkspace()
    } catch (cause) {
      setError(cause?.message || 'Could not upload that file.')
    } finally { setBusy(false) }
  }

  const inspecting = !!selected
  const selectedName = selected ? selected.split('/').pop() : ''

  return (
    <section className="project-finder" aria-label={`${projectName} workspace`} data-inspecting={inspecting || undefined}>
      <div className="project-finder__body">
        <div className="project-finder__explorer">
          <div className="project-finder__explorer-scroll">
            {overview}

            <div className="project-finder__explorer-head">
              <nav className="project-finder__crumbs" aria-label="Folder location" ref={crumbTrailRef}>
                {crumbs.map((crumb, index) => (
                  <span key={crumb.path || 'root'} className="project-finder__crumb">
                    {index > 0 && <ChevronRight size={13} aria-hidden="true" />}
                    <button
                      type="button"
                      aria-current={index === crumbs.length - 1 && !selected ? 'page' : undefined}
                      onClick={() => openFolder(crumb.path)}
                    >
                      {crumb.label}
                    </button>
                  </span>
                ))}
                {slowScan && <span className="project-finder__scan" role="status">Loading…</span>}
              </nav>

              {gitStatus?.available && !canInitializeProjectGit(gitStatus) && (
                <div
                  className="project-finder__git-summary"
                  title={`${gitStatus.repository_scope === 'project' ? 'Project repository' : 'Shared Möbius repository, scoped to this project'}${gitStatus.head ? ` · ${gitStatus.head}` : ''}`}
                  aria-label={`${gitIdentityLabel(gitStatus)} ${gitStatus.branch ? 'branch' : 'revision'}, ${gitTotal ? `${gitTotal} changed ${gitTotal === 1 ? 'file' : 'files'}` : 'clean'}`}
                >
                  <GitBranch size={13} aria-hidden="true" />
                  <span>{gitIdentityLabel(gitStatus)}</span>
                  <b>{gitTotal || 'Clean'}</b>
                </div>
              )}

              {!readOnly && gitQuery.isSuccess && canInitializeProjectGit(gitStatus) && source.initGit && (
                <button
                  type="button"
                  className="project-finder__version-action"
                  disabled={versionBusy}
                  title={gitStatus?.repository_scope === 'shared' ? 'Create an independent repository for this project' : undefined}
                  onClick={initializeVersioning}
                >
                  <GitBranch size={13} aria-hidden="true" /> {versionBusy ? 'Starting…' : 'Start versioning'}
                </button>
              )}

              {(path || !readOnly) && <div className="project-finder__toolbar" role="toolbar" aria-label="File actions">
                {path && (
                  <button type="button" className="project-finder__tool" aria-label="Up one folder" title="Up one folder" onClick={goBack}>
                    <ArrowLeft size={16} aria-hidden="true" />
                  </button>
                )}
                {!readOnly && <>
                  <button type="button" className="project-finder__tool" aria-label="New file" title="New file" disabled={busy} onClick={() => { setCreation('file'); setCreationName('') }}>
                    <FileText size={16} aria-hidden="true" />
                  </button>
                  <button type="button" className="project-finder__tool" aria-label="New folder" title="New folder" disabled={busy} onClick={() => { setCreation('folder'); setCreationName('') }}>
                    <FolderPlus size={16} aria-hidden="true" />
                  </button>
                  <button type="button" className="project-finder__tool" aria-label="Upload" title="Upload" disabled={busy} onClick={() => uploadRef.current?.click()}>
                    <Upload size={16} aria-hidden="true" />
                  </button>
                  <input ref={uploadRef} type="file" multiple hidden onChange={uploadFiles} />
                </>}
              </div>}
            </div>

            {gitTotal > 0 && gitStatus?.repository_scope === 'project' && (
              <details className="project-finder__changes">
                <summary>
                  <span><GitBranch size={14} aria-hidden="true" /> Changes</span>
                  <b>{gitTotal}</b>
                </summary>
                {!readOnly && gitStatus?.repository_scope === 'project' && source.commitGit && <div className="project-finder__commit-action"><button type="button" onClick={() => { setCommitOpen(current => !current); setVersionError('') }} aria-expanded={commitOpen}><GitCommitHorizontal size={13} /> Commit changes</button></div>}
                {commitOpen && <form className="project-finder__commit" onSubmit={commitChanges}>
                  <label htmlFor={`project-commit-${projectId}`}>Describe this snapshot</label>
                  <div><input id={`project-commit-${projectId}`} autoFocus value={commitMessage} maxLength={500} placeholder="What changed?" disabled={versionBusy} onChange={event => setCommitMessage(event.target.value)} /><button type="submit" disabled={versionBusy || !commitMessage.trim()}>{versionBusy ? 'Committing…' : 'Commit locally'}</button></div>
                  <small>The owner controls publishing separately.</small>
                </form>}
                <div className="project-finder__change-list">
                  {gitChanges.slice(0, 12).map(change => {
                    const presentation = gitStatusPresentation(change.status)
                    const contents = <><i aria-hidden="true">{presentation.code}</i><span>{change.path}</span></>
                    return change.status === 'deleted' ? (
                      <div key={change.path} className="project-finder__change" title={presentation.label}>{contents}</div>
                    ) : (
                      <button key={change.path} type="button" className="project-finder__change" title={`${presentation.label}: ${change.path}`} onClick={() => openFileAt(change.path)}>{contents}</button>
                    )
                  })}
                  {gitChanges.length > 12 && <small>+{gitChanges.length - 12} more</small>}
                </div>
              </details>
            )}

            {versionError && <p className="projects-error" role="alert">{versionError}</p>}

            <label className="project-finder__search">
              <Search size={15} aria-hidden="true" />
              <input
                type="search"
                value={filter}
                placeholder="Filter files"
                aria-label={`Filter files in ${path || projectName}`}
                onChange={e => setFilter(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Escape' && filter) { e.stopPropagation(); setFilter('') }
                  if (e.key === 'Enter') {
                    const first = visibleEntries[0]
                    if (!first) return
                    e.preventDefault()
                    setFilter('')
                    if (first.type === 'directory') openFolder(first.path)
                    else openFileAt(first.path)
                  }
                }}
              />
            </label>

            {!readOnly && creation && (
              <form className="project-inline-create" onSubmit={submitCreate} onKeyDown={e => { if (e.key === 'Escape' && !busy) setCreation(null) }}>
                <label htmlFor="project-finder-create">{creation === 'file' ? 'New file' : 'New folder'}</label>
                <input
                  id="project-finder-create"
                  autoFocus
                  value={creationName}
                  maxLength={2048}
                  placeholder={path ? `${path}/name` : 'name'}
                  onChange={e => setCreationName(e.target.value)}
                />
                <button type="submit" disabled={busy || !creationName.trim()}>{busy ? 'Creating…' : 'Create'}</button>
                <button type="button" disabled={busy} onClick={() => setCreation(null)}>Cancel</button>
              </form>
            )}

            {error && <p className="projects-error" role="alert">{error}</p>}

            <div className="project-finder__list" role="list" aria-label={`Files in ${path || projectName}`}>
              {filesQuery.isLoading ? (
                <p className="projects-empty" role="status">Loading files…</p>
              ) : filesQuery.isError ? (
                <div className="projects-empty" role="alert"><p>Files are unavailable.</p><button type="button" onClick={() => filesQuery.refetch()}>Try again</button></div>
              ) : entries.length === 0 ? (
                <p className="projects-empty">This folder is empty.</p>
              ) : visibleEntries.length === 0 ? (
                <p className="projects-empty" role="status">Nothing matches “{filter}”.</p>
              ) : (
                visibleEntries.map(entry => (
                  <FinderRow
                    key={entry.path}
                    entry={entry}
                    active={entry.path === selected}
                    disabled={busy}
                    readOnly={readOnly}
                    onOpen={() => (entry.type === 'directory' ? openFolder(entry.path) : openFileAt(entry.path))}
                    onRename={(next) => moveEntry(entry.path, joinPath(parentPath(entry.path), next))}
                    onMove={(toDir) => moveEntry(entry.path, joinPath(toDir, entry.name))}
                    onDownload={() => downloadFile(entry.path)}
                    onDelete={() => deleteEntry(entry.path)}
                    gitAnnotation={gitAnnotationForEntry(gitChanges, entry)}
                    artifactType={artifactTypeForFile(entry.name, artifactTypes)}
                    onBuildAs={onBuildFile
                      ? (type) => onBuildFile(entry.path, type.id)
                      : null}
                  />
                ))
              )}
            </div>
          </div>
        </div>

        <div className="project-finder__pane" aria-live="polite">
          {!inspecting ? (
            <div className="project-finder__placeholder" role="status">
              <File size={38} strokeWidth={1.3} aria-hidden="true" />
              <p>Select a file to preview it here.</p>
            </div>
          ) : (
            <>
              <header className="project-finder__pane-head">
                <button type="button" className="project-icon-button project-finder__pane-back" aria-label="Back to files" onClick={goBack}><ArrowLeft size={18} /></button>
                <div className="project-finder__pane-title">
                  <strong>{selectedName}</strong>
                  <small>{selected}</small>
                </div>
                {gitDiff?.available && gitDiff.status !== 'clean' && (
                  <span className="project-finder__diff-total" aria-label={`${gitDiff.additions} additions and ${gitDiff.deletions} deletions`}>
                    <b>+{gitDiff.additions}</b><i>−{gitDiff.deletions}</i>
                  </span>
                )}
                <div className="project-finder__pane-actions">
                  {['image', 'pdf', 'binary'].includes(fileKind) && (
                    <button type="button" onClick={() => downloadFile()}>Download</button>
                  )}
                  {!readOnly && fileKind === 'text' && !windowed && !editing && (
                    <button type="button" onClick={() => setEditing(true)}><Pencil size={14} aria-hidden="true" /> Edit</button>
                  )}
                  {fileKind === 'text' && editing && (
                    <button type="button" disabled={!dirty || busy} onClick={saveFile}>{busy ? 'Saving…' : 'Save'}</button>
                  )}
                </div>
              </header>
              {fileError && <p className="projects-error" role="alert">{fileError}</p>}
              {remoteChange && (
                <div className="project-finder__conflict" role="alert">
                  <div>
                    <strong>{remoteChange.movedTo ? 'This file moved elsewhere' : remoteChange.deleted ? 'This file was removed elsewhere' : 'This file changed elsewhere'}</strong>
                    <span>{remoteChange.deleted
                      ? 'The version you opened is still here. Preserve it as a new file, or leave the removed path.'
                      : 'Your draft is still here. Preserve it as a new file, or load the latest shared version.'}</span>
                  </div>
                  <div>
                    <button type="button" disabled={busy} onClick={() => void saveConflictCopy()}>
                      Save draft as copy
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        if (remoteChange.movedTo) {
                          setRemoteChange(null)
                          setEditing(false)
                          setNav(current => ({ ...current, current: { ...current.current, selected: remoteChange.movedTo } }))
                          return
                        }
                        if (remoteChange.deleted) {
                          setRemoteChange(null)
                          setEditing(false)
                          goBack()
                          return
                        }
                        setContent(remoteChange.content)
                        setBaseline(remoteChange.content)
                        setRevision(remoteChange.revision)
                        setRemoteChange(null)
                        setEditing(false)
                      }}
                    >
                      {remoteChange.movedTo ? 'Open moved file' : remoteChange.deleted ? 'Close file' : 'Load latest'}
                    </button>
                  </div>
                </div>
              )}
              <div className="project-finder__surface">
                {fileLoading && fileKind === 'none' ? (
                  <div className="project-document__empty" role="status"><p>Opening file…</p></div>
                ) : fileKind === 'text' ? (
                  csvLike && !windowed ? (
                    <div className="project-sheet" data-editing={editing || undefined}>
                      {editing && <div className="project-sheet__toolbar" role="toolbar" aria-label="Spreadsheet actions">
                        <button type="button" onClick={appendCsvRow}><Plus size={14} aria-hidden="true" /> Row</button>
                        <button type="button" onClick={appendCsvColumn}><Plus size={14} aria-hidden="true" /> Column</button>
                        <span>{csvRows.length > 1 ? `${csvRows.length - 1} rows` : 'No data rows'}</span>
                      </div>}
                      <div className="project-sheet__scroll">
                        <table aria-label={`${selectedName} spreadsheet`}>
                          <thead><tr>{(csvRows[0] || ['']).map((cell, columnIndex) => <th key={columnIndex}>{editing ? <input aria-label={`Column ${columnIndex + 1} heading`} value={cell} onChange={event => updateCsv(0, columnIndex, event.target.value)} /> : cell || `Column ${columnIndex + 1}`}</th>)}</tr></thead>
                          <tbody>{csvRows.slice(1).map((row, rowOffset) => <tr key={rowOffset}>{row.map((cell, columnIndex) => <td key={columnIndex}>{editing ? <input aria-label={`Row ${rowOffset + 1}, column ${columnIndex + 1}`} value={cell} onChange={event => updateCsv(rowOffset + 1, columnIndex, event.target.value)} /> : cell}</td>)}</tr>)}</tbody>
                        </table>
                      </div>
                    </div>
                  ) : markdownLike && editing && !windowed ? (
                    <div className="project-finder__live-edit">
                      <section className="project-finder__live-code" aria-label="Markdown editor">
                        <span>Markdown</span>
                        <textarea
                          aria-label={`Edit ${selected}`}
                          value={content}
                          spellCheck="true"
                          onChange={e => setContent(e.target.value)}
                          onKeyDown={e => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') { e.preventDefault(); void saveFile() } }}
                        />
                      </section>
                      <section className="project-finder__live-preview" aria-label="Live reading preview">
                        <span>Reading view</span>
                        <article className="project-markdown" dangerouslySetInnerHTML={{ __html: markdownPreview }} />
                      </section>
                    </div>
                  ) : markdownLike ? (
                    <div className="project-markdown__scroll"><article className="project-markdown" dangerouslySetInnerHTML={{ __html: markdownPreview }} /></div>
                  ) : htmlLike && editing && !windowed ? (
                    <div className="project-finder__live-edit">
                      <section className="project-finder__live-code" aria-label="Source editor">
                        <span>Source</span>
                        <textarea
                          aria-label={`Edit ${selected}`}
                          value={content}
                          spellCheck="false"
                          onChange={e => setContent(e.target.value)}
                          onKeyDown={e => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') { e.preventDefault(); void saveFile() } }}
                        />
                      </section>
                      <section className="project-finder__live-preview" aria-label="Live preview">
                        <span>Live preview</span>
                        {htmlPreviewError
                          ? <p role="alert">{htmlPreviewError}</p>
                          : <ProjectPreviewFrame projectId={projectId} sourcePath={selected} title={`${selected} live preview`} srcDoc={htmlPreview} />}
                      </section>
                    </div>
                  ) : htmlLike ? (
                    <div className="project-preview">
                      <p>Isolated preview · edit to see source and preview together.</p>
                      {htmlPreviewError
                        ? <div className="project-document__empty" role="alert"><p>{htmlPreviewError}</p></div>
                        : <ProjectPreviewFrame projectId={projectId} sourcePath={selected} title={`${selected} preview`} srcDoc={htmlPreview} />}
                    </div>
                  ) : editing && !windowed ? (
                    <textarea
                      aria-label={`Edit ${selected}`}
                      value={content}
                      spellCheck="false"
                      onChange={e => setContent(e.target.value)}
                      onKeyDown={e => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') { e.preventDefault(); void saveFile() } }}
                    />
                  ) : (
                    <div className="project-finder__code">
                      {windowed && (
                        <p className="project-finder__notice" role="status">
                          Large file — showing lines 1–{codeWindow.shownLines.toLocaleString()} of {codeWindow.totalLines.toLocaleString()}. Download to see all of it.
                        </p>
                      )}
                      <div className={`project-finder__code-body${windowed || codeLike ? ' project-finder__code-body--numbered' : ''}`}>
                        {(windowed || codeLike) && (
                          <CodeGutter count={codeWindow.shownLines} changedLines={changedLines} />
                        )}
                        {highlighted
                          ? <pre className="hljs"><code dangerouslySetInnerHTML={{ __html: highlighted }} /></pre>
                          : <pre><code>{codeWindow?.text ?? content}</code></pre>}
                      </div>
                    </div>
                  )
                ) : fileKind === 'image' ? (
                  <div className="project-preview project-preview--asset">
                    <button type="button" className="project-finder__image-btn" onClick={() => setLightboxOpen(true)} aria-label={`Zoom ${selectedName}`}>
                      <img src={objectUrl || ''} alt={`Preview of ${selected}`} />
                    </button>
                  </div>
                ) : fileKind === 'pdf' ? (
                  <ProjectPdfPreview data={pdfData} title={selected} />
                ) : fileKind === 'binary' ? (
                  <div className="project-document__empty"><File size={42} strokeWidth={1.4} /><h2>Preview unavailable</h2><p>This file is preserved as-is and can be downloaded.</p><button type="button" onClick={() => downloadFile()}>Download</button></div>
                ) : fileKind === 'error' ? (
                  <div className="project-document__empty" role="alert"><File size={42} strokeWidth={1.4} /><h2>Couldn’t open this file</h2><p>{fileError || 'The file may have moved or become unavailable.'}</p></div>
                ) : (
                  <div className="project-document__empty" role="status"><p>Opening file…</p></div>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {lightboxOpen && objectUrl && createPortal(
        <ImageLightbox src={objectUrl} alt={selectedName} onClose={() => setLightboxOpen(false)} />,
        document.body,
      )}
    </section>
  )
}

// One entry row with a context action menu (open / rename / move / download /
// delete). Rename + move both call POST /{id}/move via the parent.
function FinderRow({
  entry,
  active,
  disabled,
  readOnly,
  gitAnnotation,
  artifactType,
  onOpen,
  onRename,
  onMove,
  onDownload,
  onDelete,
  onBuildAs,
}) {
  const [menu, setMenu] = useState(false)
  const [mode, setMode] = useState(null) // 'rename' | 'move' | null
  const [value, setValue] = useState('')
  const rootRef = useRef(null)
  const isDir = entry.type === 'directory'

  useEffect(() => {
    if (!menu) return undefined
    function onPointer(e) { if (!rootRef.current?.contains(e.target)) setMenu(false) }
    function onEsc(e) { if (e.key === 'Escape') setMenu(false) }
    document.addEventListener('pointerdown', onPointer)
    document.addEventListener('keydown', onEsc)
    return () => { document.removeEventListener('pointerdown', onPointer); document.removeEventListener('keydown', onEsc) }
  }, [menu])

  function begin(next) {
    setMenu(false)
    setMode(next)
    setValue(next === 'rename' ? entry.name : '')
  }
  async function submit(e) {
    e.preventDefault()
    const trimmed = value.trim()
    if (!trimmed) { setMode(null); return }
    const ok = mode === 'rename' ? await onRename(trimmed) : await onMove(trimmed)
    if (ok !== false) setMode(null)
  }

  if (!readOnly && mode) {
    return (
      <form className="project-finder__rename" onSubmit={submit} onKeyDown={e => { if (e.key === 'Escape') setMode(null) }}>
        <label>{mode === 'rename' ? 'Rename to' : 'Move to folder'}</label>
        <input autoFocus value={value} maxLength={2048} placeholder={mode === 'move' ? 'destination/folder' : entry.name} onChange={e => setValue(e.target.value)} />
        <button type="submit" disabled={disabled}>Save</button>
        <button type="button" onClick={() => setMode(null)}>Cancel</button>
      </form>
    )
  }

  return (
    <div ref={rootRef} className={`project-finder__row${active ? ' project-finder__row--active' : ''}`} role="listitem">
      <button type="button" className="project-finder__row-main" disabled={disabled} onClick={onOpen} aria-current={active ? 'true' : undefined}>
        <span className="project-finder__row-icon" aria-hidden="true">{entryIcon(entry, 19)}</span>
        <span className="project-finder__row-text"><strong>{entry.name}</strong><small>{isDir ? 'Folder' : formatSize(entry.size)}</small></span>
        {gitAnnotation && (
          <span
            className={`project-finder__git-badge project-finder__git-badge--${gitAnnotation.kind}`}
            title={gitAnnotation.label}
            aria-label={gitAnnotation.label}
          >
            {gitAnnotation.code}
          </span>
        )}
        {isDir && <ChevronRight size={15} className="project-finder__row-chevron" aria-hidden="true" />}
      </button>
      {(!readOnly || !isDir) && <div className="project-menu">
        <button type="button" className="project-icon-button project-finder__row-menu" aria-label={`Actions for ${entry.name}`} aria-haspopup="menu" aria-expanded={menu} disabled={disabled} onClick={() => setMenu(v => !v)}><Ellipsis size={17} /></button>
        {menu && (
          <div className="project-menu__popover project-menu__popover--end" role="menu">
            {!readOnly && <button type="button" role="menuitem" onClick={() => begin('rename')}><Pencil size={15} /> Rename</button>}
            {!readOnly && <button type="button" role="menuitem" onClick={() => begin('move')}><FolderPlus size={15} /> Move…</button>}
            {!isDir && <button type="button" role="menuitem" onClick={() => { setMenu(false); onDownload() }}><Download size={15} /> Download</button>}
            {!isDir && onBuildAs && artifactType && (
              <button type="button" role="menuitem" onClick={() => { setMenu(false); onBuildAs(artifactType) }}><Hammer size={15} /> Build as {artifactType.name}</button>
            )}
            {!readOnly && <button type="button" role="menuitem" className="project-menu__danger" onClick={() => { setMenu(false); onDelete() }}><Trash2 size={15} /> Delete</button>}
          </div>
        )}
      </div>}
    </div>
  )
}

function CodeGutter({ count, changedLines }) {
  return (
    <pre className="project-finder__gutter" aria-hidden="true">
      {Array.from(changedLines)
        .filter(line => line > 0 && line <= count)
        .map(line => (
          <span key={line} className="project-finder__gutter-mark" style={{ '--line-index': line - 1 }} />
        ))}
      {lineNumbersFor(count)}
    </pre>
  )
}
