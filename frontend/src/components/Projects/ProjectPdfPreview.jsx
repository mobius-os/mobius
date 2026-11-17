import { useEffect, useRef, useState } from 'react'
import ArrowLeft from 'lucide-react/dist/esm/icons/arrow-left.mjs'
import ArrowRight from 'lucide-react/dist/esm/icons/arrow-right.mjs'

export function projectPdfScale(natural, width, height, fit, zoom) {
  const available = Math.max(fit === 'width' ? 240 : 1, width - 28)
  const widthScale = Math.min(2, available / natural.width)
  const heightScale = Math.max(1, height - 28) / natural.height
  return (fit === 'page' ? Math.min(widthScale, heightScale) : widthScale) * zoom
}

export default function ProjectPdfPreview({ data, title, defaultFit = 'width', onDownload, controls = false }) {
  const containerRef = useRef(null)
  const canvasRef = useRef(null)
  const [document, setDocument] = useState(null)
  const [pageNumber, setPageNumber] = useState(1)
  const [pageCount, setPageCount] = useState(0)
  const [renderVersion, setRenderVersion] = useState(0)
  const [error, setError] = useState('')
  const [fit, setFit] = useState(defaultFit)
  const [zoom, setZoom] = useState(1)

  useEffect(() => {
    if (!data?.byteLength) return undefined
    let active = true
    let loadingTask = null
    let loadedDocument = null
    setDocument(null)
    setPageNumber(1)
    setPageCount(0)
    setError('')

    void import('pdfjs-dist').then(pdfjs => {
      if (!active) return null
      pdfjs.GlobalWorkerOptions.workerSrc = '/vendor/pdfjs/pdf.worker.mjs'
      loadingTask = pdfjs.getDocument({ data: data.slice() })
      return loadingTask.promise
    }).then(nextDocument => {
      if (!nextDocument || !active) return
      loadedDocument = nextDocument
      setDocument(nextDocument)
      setPageCount(nextDocument.numPages)
    }).catch(cause => {
      if (active) setError(cause?.message || 'The PDF could not be rendered.')
    })

    return () => {
      active = false
      void loadingTask?.destroy?.()
      void loadedDocument?.destroy?.()
    }
  }, [data])

  useEffect(() => {
    const container = containerRef.current
    if (!container || typeof ResizeObserver === 'undefined') return undefined
    let frame = 0
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => setRenderVersion(version => version + 1))
    })
    observer.observe(container)
    return () => {
      cancelAnimationFrame(frame)
      observer.disconnect()
    }
  }, [])

  useEffect(() => {
    if (!document || !canvasRef.current || !containerRef.current) return undefined
    let active = true
    let renderTask = null
    let page = null

    void document.getPage(pageNumber).then(nextPage => {
      if (!active) return
      page = nextPage
      const canvas = canvasRef.current
      const context = canvas?.getContext('2d')
      if (!canvas || !context) throw new Error('The PDF canvas is unavailable.')
      const natural = page.getViewport({ scale: 1 })
      const scale = projectPdfScale(natural, containerRef.current.clientWidth, containerRef.current.clientHeight, fit, zoom)
      const viewport = page.getViewport({ scale })
      const outputScale = Math.min(2, window.devicePixelRatio || 1)
      canvas.width = Math.floor(viewport.width * outputScale)
      canvas.height = Math.floor(viewport.height * outputScale)
      canvas.style.width = `${Math.floor(viewport.width)}px`
      canvas.style.height = `${Math.floor(viewport.height)}px`
      renderTask = page.render({
        canvasContext: context,
        viewport,
        transform: outputScale === 1 ? null : [outputScale, 0, 0, outputScale, 0, 0],
      })
      return renderTask.promise
    }).then(() => {
      if (active) setError('')
    }).catch(cause => {
      if (active && cause?.name !== 'RenderingCancelledException') {
        setError(cause?.message || 'The PDF page could not be rendered.')
      }
    })

    return () => {
      active = false
      renderTask?.cancel?.()
      page?.cleanup?.()
    }
  }, [document, pageNumber, renderVersion, fit, zoom])

  if (error) return (
    <div className="project-document__empty" role="alert">
      <h2>Couldn’t render this PDF</h2>
      <p>{error}</p>
    </div>
  )

  return (
    <div className="project-pdf" aria-label={`${title} PDF preview`}>
      <div className="project-pdf__toolbar">
        <button type="button" aria-label="Previous PDF page" disabled={pageNumber <= 1} onClick={() => setPageNumber(page => page - 1)}><ArrowLeft size={16} /></button>
        <span>{pageCount ? `Page ${pageNumber} of ${pageCount}` : 'Loading PDF…'}</span>
        <button type="button" aria-label="Next PDF page" disabled={!pageCount || pageNumber >= pageCount} onClick={() => setPageNumber(page => page + 1)}><ArrowRight size={16} /></button>
        {controls && <>
          <button type="button" className="project-pdf__text-control" aria-pressed={fit === 'page' && zoom === 1} onClick={() => { setFit('page'); setZoom(1) }}>Fit page</button>
          <button type="button" className="project-pdf__text-control" aria-pressed={fit === 'width' && zoom === 1} onClick={() => { setFit('width'); setZoom(1) }}>Fit width</button>
          <button type="button" aria-label="Zoom out" disabled={zoom <= 0.5} onClick={() => setZoom(value => Math.max(0.5, Math.round((value - 0.25) * 100) / 100))}>−</button>
          <span aria-label={`Zoom ${Math.round(zoom * 100)} percent`}>{Math.round(zoom * 100)}%</span>
          <button type="button" aria-label="Zoom in" disabled={zoom >= 3} onClick={() => setZoom(value => Math.min(3, Math.round((value + 0.25) * 100) / 100))}>+</button>
          {onDownload && <button type="button" className="project-pdf__text-control" onClick={onDownload}>Download</button>}
        </>}
      </div>
      <div ref={containerRef} className="project-pdf__viewport">
        <canvas ref={canvasRef} role="img" aria-label={`${title}, page ${pageNumber}`} />
      </div>
    </div>
  )
}
