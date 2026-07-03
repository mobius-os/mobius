import { S } from '../constants.js'

export function Th({ label, subLabel, active, dir, onSort, align }) {
  // scope=col names the column for assistive tech; aria-sort reflects the live
  // sort on the one active column ('ascending'/'descending') and 'none' on the
  // other sortable columns, so a screen reader announces the current ordering.
  const ariaSort = onSort
    ? (active ? (dir === 'asc' ? 'ascending' : 'descending') : 'none')
    : undefined;
  const inner = (
    <>
      <span style={S.thMain}>
        {label}
        {active && <span style={S.sortCaret}>{dir === 'asc' ? '↑' : '↓'}</span>}
      </span>
      {subLabel && <span style={S.thSub}>{subLabel}</span>}
    </>
  );
  // Sortable headers are a real <button> inside the <th>: native Enter/Space
  // activation + focus, instead of an un-focusable <th onClick>. A non-sortable
  // header (e.g. Type) stays a plain, non-interactive cell.
  if (onSort) {
    return (
      <th scope="col" style={{ ...S.th, textAlign: align || 'right' }} aria-sort={ariaSort}>
        <button
          type="button"
          className="mg-th"
          style={{ ...S.thButton, justifyContent: align === 'left' ? 'flex-start' : 'flex-end' }}
          onClick={onSort}
        >
          {inner}
        </button>
      </th>
    );
  }
  return (
    <th scope="col" style={{ ...S.th, textAlign: align || 'right' }}>
      {inner}
    </th>
  );
}
