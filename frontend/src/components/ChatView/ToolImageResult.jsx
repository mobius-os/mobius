/* Render a viewed image after its source and dimensions are prepared. */
import ImagePreviewButton from './ImagePreviewButton.jsx'

export default function ToolImageResult({ reference, preview }) {
  const current = preview?.reference === reference
    ? preview
    : { status: 'failed', src: '', width: 0, height: 0 }
  const alt = reference?.filename || 'Viewed image'

  if (current.status !== 'ready') {
    return (
      <span className="chat__tool-image-status" role="status">
        Image preview unavailable
      </span>
    )
  }

  return (
    <ImagePreviewButton
      src={current.src}
      alt={alt}
      buttonClassName="chat__tool-image-button"
      imageClassName="chat__tool-image"
      intrinsicWidth={current.width}
      intrinsicHeight={current.height}
      imageLoading="eager"
    />
  )
}
