export const IDENTITY_STYLES = `
.id-root, .id-root * { box-sizing: border-box; }

.id-root button,
.id-root input { font: inherit; }

.id-root button { user-select: none; -webkit-user-select: none; }

.id-root {
  --id-radius: 18px;
  --id-control-radius: 12px;
  /* The membership card's own dark surface — deliberately independent of the
     theme surface so the card reads as an object lying ON the page. */
  --id-card-face: radial-gradient(120% 160% at 12% 8%, #232330 0%, #17171c 48%, #121216 100%);
  --id-card-fg: #f2f2f4;
  --id-card-muted: #9a9aa3;
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  background: var(--bg, #0d0d0d);
  color: var(--text, #f5f5f5);
  font-family: var(--font, Inter, ui-sans-serif, system-ui, sans-serif);
  user-select: text;
  -webkit-user-select: text;
}

.id-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  overscroll-behavior: contain;
  padding:
    18px max(16px, env(safe-area-inset-right))
    max(28px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left));
  scrollbar-width: none;
}

.id-scroll::-webkit-scrollbar { display: none; }

.id-shell {
  width: min(680px, 100%);
  margin: 0 auto;
}

/* Gentle load entrance for the main sections — ambient, not scroll-triggered. */
.id-shell > * {
  animation: id-rise .5s cubic-bezier(.22, 1, .36, 1) backwards;
}
.id-shell > *:nth-child(2) { animation-delay: .06s; }
.id-shell > *:nth-child(3) { animation-delay: .12s; }
.id-shell > *:nth-child(4) { animation-delay: .18s; }

@keyframes id-rise {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: none; }
}

/* apps-sdk icons render <svg width="1em" height="1em">; a width={N} prop sets
   width but leaves height at 1em, so icons distort in any non-16px context.
   Derive height from the (square) width instead — one fix for the whole set. */
.id-root svg[height="1em"] { height: auto; }

.id-top,
.id-brand,
.id-status,
.id-title-row,
.id-email {
  display: flex;
  align-items: center;
}

/* The header floats over the page instead of drawing a full-width bar, so the
   whole screen reads as one centered column like the sibling apps. */
.id-top {
  flex: 0 0 auto;
  width: 100%;
  min-height: 52px;
  background: var(--bg);
}

.id-top-inner {
  display: flex;
  align-items: center;
  width: 100%;
  max-width: 712px;
  margin-inline: auto;
  justify-content: space-between;
  gap: 16px;
  padding: max(14px, env(safe-area-inset-top)) 16px 12px;
  border-bottom: 1px solid var(--border);
}

.id-brand {
  min-width: 0;
  gap: 10px;
}

.id-brand img {
  width: 32px;
  height: 32px;
  flex: 0 0 auto;
  border-radius: 8px;
}

.id-brand strong {
  display: block;
  color: var(--text, #f5f5f5);
  font-size: 16px;
  font-weight: 650;
  line-height: 1.2;
  letter-spacing: -.015em;
}

.id-kicker {
  margin-top: 2px;
  color: var(--muted, #999);
  font-size: 11px;
  line-height: 1.3;
}

.id-status {
  min-width: 0;
  gap: 8px;
  padding: 7px 10px;
  border: 1px solid var(--border-light, var(--border, #2a2a2a));
  border-radius: 999px;
  background: var(--surface, #171717);
  color: var(--muted, #999);
  font-size: 12px;
  font-weight: 550;
  text-align: right;
}

/* The connection pill can act as a menu trigger (click to reveal Unlink). */
.id-status-menu { position: relative; }
.id-status--menu { display: flex; align-items: center; cursor: pointer; }
.id-status--menu:hover {
  border-color: color-mix(in srgb, var(--accent, #8b7cf6) 40%, var(--border, #2a2a2a));
}
.id-status-caret { flex: none; margin-left: 1px; opacity: .65; }
.id-status-dropdown {
  position: absolute;
  right: 0;
  top: calc(100% + 6px);
  z-index: 30;
  min-width: 220px;
  padding: 6px;
  border: 1px solid var(--border, #2a2a2a);
  border-radius: 12px;
  background: var(--surface, #171717);
  box-shadow: 0 18px 46px rgba(0, 0, 0, .3);
}
.id-status-item {
  width: 100%;
  padding: 9px 11px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--danger, #e67a7a);
  font-size: 13px;
  font-weight: 550;
  text-align: left;
  cursor: pointer;
}
.id-status-item:hover { background: color-mix(in srgb, var(--danger, #d65a5a) 13%, transparent); }

.id-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 50%;
}

.id-dot--online {
  background: var(--success, #38b86c);
  animation: id-pulse 2.6s ease-in-out infinite;
}

@keyframes id-pulse {
  0%, 100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--success, #38b86c) 45%, transparent); }
  55% { box-shadow: 0 0 0 5px transparent; }
}
.id-dot--muted { background: var(--muted, #777); }
.id-dot--warning { background: var(--warning, #d9982f); }
.id-dot--error { background: var(--danger, #d65a5a); }

/* Signed-out welcome: the blank membership card, waiting for its owner. */
.id-auth {
  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  justify-content: center;
  margin-bottom: 18px;
  padding: 32px 26px 30px;
  border: 1px solid rgba(255, 255, 255, .09);
  border-radius: 20px;
  background: var(--id-card-face);
  color: var(--id-card-fg);
  box-shadow: 0 20px 48px rgba(0, 0, 0, .45);
}

.id-auth::before {
  content: "";
  position: absolute;
  inset: 0;
  padding: 1px;
  border-radius: inherit;
  background: conic-gradient(from 210deg,
    rgba(139, 124, 246, .55), rgba(94, 234, 212, .35),
    rgba(244, 114, 182, .4), rgba(139, 124, 246, .55));
  -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
  -webkit-mask-composite: xor;
  mask-composite: exclude;
  opacity: .7;
  pointer-events: none;
}

.id-auth > * { position: relative; z-index: 1; }

.id-auth h1 {
  max-width: 22ch;
  margin: 0 0 12px;
  color: var(--id-card-fg);
  font-size: clamp(26px, 4.2vw, 36px);
  font-weight: 760;
  line-height: 1.06;
  letter-spacing: -.03em;
  text-wrap: balance;
}

.id-auth > p {
  max-width: 52ch;
  margin: 0;
  color: var(--id-card-muted);
  font-size: 14.5px;
  line-height: 1.6;
}

.id-auth-button {
  min-width: 190px;
  margin-top: 24px;
}

/* ===== The membership card =====
   Your identity as a tactile object: a dark card with an iridescent edge that
   tilts under the pointer (its sheen follows), and floats gently on touch
   devices. Pure presentation lives here; motion comes from IdentityCard. */
.id-hero { margin-bottom: 18px; }

.id-tilt-zone { perspective: 900px; padding-top: 6px; }

/* Sized like an object you could hold, not a stretched banner. */
.id-card-3d {
  position: relative;
  overflow: hidden;
  max-width: 460px;
  margin: 0 auto;
  padding: 20px 22px 18px;
  border: 1px solid rgba(255, 255, 255, .09);
  border-radius: 20px;
  background: var(--id-card-face);
  color: var(--id-card-fg);
  transform-style: preserve-3d;
  transition: transform .18s ease;
  box-shadow: 0 20px 48px rgba(0, 0, 0, .45);
}

/* Iridescent edge: a conic gradient masked down to a 1px border ring. */
.id-card-3d::before {
  content: "";
  position: absolute;
  inset: 0;
  padding: 1px;
  border-radius: inherit;
  background: conic-gradient(from var(--id-holo, 210deg),
    rgba(139, 124, 246, .55), rgba(94, 234, 212, .35),
    rgba(244, 114, 182, .4), rgba(139, 124, 246, .55));
  -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
  -webkit-mask-composite: xor;
  mask-composite: exclude;
  opacity: .7;
  pointer-events: none;
}

/* The sheen that follows the pointer. */
.id-card-3d::after {
  content: "";
  position: absolute;
  inset: -40%;
  background: radial-gradient(circle at var(--id-mx, 30%) var(--id-my, 20%),
    rgba(255, 255, 255, .12), transparent 42%);
  pointer-events: none;
}

.id-cardhead {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.id-cardword {
  color: var(--id-card-muted);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .16em;
  text-transform: uppercase;
}

.id-cardring {
  position: relative;
  width: 22px;
  height: 22px;
  border: 2.5px solid color-mix(in srgb, var(--accent, #8b7cf6) 75%, #fff);
  border-radius: 50%;
}

.id-cardring::after {
  content: "";
  position: absolute;
  inset: 3px;
  border: 2px solid rgba(255, 255, 255, .25);
  border-radius: 50%;
}

.id-cardid {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-top: 20px;
}

.id-cardfoot {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px 18px;
  margin-top: 22px;
}

.id-cardkv {
  color: var(--id-card-muted);
  font-size: 9.5px;
  font-weight: 600;
  letter-spacing: .12em;
  text-transform: uppercase;
}

.id-cardkv b {
  display: block;
  margin-top: 3px;
  color: var(--id-card-fg);
  font-size: 12.5px;
  font-weight: 650;
  letter-spacing: .02em;
  text-transform: none;
}

.id-cardlink {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: color-mix(in srgb, var(--success, #3fbf77) 80%, var(--id-card-fg));
  font-size: 11px;
  font-weight: 650;
}

.id-profile-copy { min-width: 0; }

.id-avatar {
  position: relative;
  width: 80px;
  aspect-ratio: 1;
  flex: none;
  display: grid;
  place-items: center;
  border: 1px solid rgba(255, 255, 255, .2);
  border-radius: 50%;
  background: rgba(255, 255, 255, .1);
  color: var(--id-card-fg);
  font-size: 30px;
  font-weight: 720;
  letter-spacing: -.05em;
}

.id-avatar.is-disabled { filter: saturate(.45); }

/* The circle clips the photo, not the avatar box — the camera badge sits on
   the circle's edge and must not be cut off. */
.id-avatar img {
  width: 100%;
  height: 100%;
  border-radius: 50%;
  object-fit: cover;
}

/* Small circular "change photo" badge sitting on the avatar's corner, the way
   most apps do it — sized to the avatar, not competing with it. */
.id-avatar-edit {
  position: absolute;
  right: -2px;
  bottom: -2px;
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  border: 2px solid #17171c;
  border-radius: 50%;
  background: rgba(255, 255, 255, .92);
  box-shadow: 0 2px 8px rgba(0, 0, 0, .3);
  color: #17171c;
  cursor: pointer;
  transition: transform .15s ease, background .15s ease;
}
.id-avatar-edit:not(:disabled):hover { transform: scale(1.08); background: #fff; }
.id-avatar-edit:not(:disabled):active { transform: scale(.94); }
.id-avatar-edit svg { width: 14px; height: 14px; }

.id-title-row {
  min-width: 0;
  align-items: center;
  gap: 7px;
}

.id-title {
  min-width: 0;
  margin: 0;
  color: var(--id-card-fg);
  font-size: clamp(22px, 3vw, 26px);
  font-weight: 740;
  line-height: 1.05;
  letter-spacing: -.025em;
  overflow-wrap: anywhere;
}

.id-handle-btn,
.id-open {
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 10px;
  background: transparent;
  color: var(--muted, #999);
  cursor: pointer;
  transition: background .15s ease, color .15s ease, transform .12s ease;
}

.id-handle-btn {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  color: var(--id-card-muted);
}
.id-handle-btn:hover { background: rgba(255, 255, 255, .12); color: var(--id-card-fg); }
.id-handle-btn:active { transform: scale(.92); }

.id-email {
  width: fit-content;
  max-width: 100%;
  gap: 7px;
  margin-top: 4px;
  color: var(--id-card-muted);
  font-size: 12.5px;
}

.id-email > span:first-of-type {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.id-private-label {
  padding-left: 7px;
  border-left: 1px solid rgba(255, 255, 255, .18);
  color: var(--id-card-muted);
}


.id-card {
  min-width: 0;
  overflow: hidden;
  padding: 0;
  border: 1px solid var(--border-light, var(--border, #2a2a2a));
  border-radius: var(--id-radius);
  background: var(--surface, #171717);
}

.id-card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin: 0;
  padding: 15px 16px;
  border-bottom: 1px solid var(--border-light, var(--border, #2a2a2a));
}

.id-deploy-actions,
.id-manage-links,
.id-delete-confirm > div {
  display: flex;
  align-items: center;
  gap: 7px;
}

.id-card h2 {
  margin: 0;
  color: var(--text, #f5f5f5);
  font-size: 15px;
  font-weight: 650;
  letter-spacing: -.01em;
}

.id-label,
.id-deploy-meta { color: var(--muted, #999); }

.id-deployments {
  display: grid;
  gap: 0;
}

.id-deployment {
  min-width: 0;
  display: flex;
  flex-direction: column;
  min-height: 76px;
  padding: 13px 16px;
  border-top: 1px solid var(--border-light, var(--border, #2a2a2a));
  border-radius: 0;
  background: transparent;
  transition: background .15s ease;
}

.id-deployment-main {
  width: 100%;
  min-width: 0;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 13px;
}

.id-deployment:first-child { border-top: 0; }

.id-deployment--danger {
  background: color-mix(in srgb, var(--danger, #d65a5a) 10%, var(--surface, #171717));
}

.id-deployment--progress {
  background: color-mix(in srgb, var(--accent, #8b7cf6) 9%, var(--surface, #171717));
}

.id-deploy-mark {
  width: 38px;
  height: 38px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  background: color-mix(in srgb, var(--accent, #8b7cf6) 12%, var(--surface-2, #1e1e1e));
}

.id-deploy-mark img {
  width: 26px;
  height: 26px;
}

.id-deploy-name,
.id-value { overflow-wrap: anywhere; }

.id-deploy-copy { min-width: 0; }

.id-deploy-name-row {
  min-width: 0;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
}

.id-name-editor {
  position: relative;
  flex: none;
  display: inline-flex;
}

.id-name-edit {
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  margin: -7px 0;
  padding: 0;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--muted, #999);
  cursor: pointer;
}

.id-name-edit:hover,
.id-name-edit:focus-visible,
.id-name-editor.is-editing .id-name-edit {
  background: color-mix(in srgb, var(--accent, #8b7cf6) 12%, transparent);
  color: var(--accent, #8b7cf6);
}

.id-name-edit:disabled { opacity: .45; cursor: default; }

.id-name-form {
  position: absolute;
  z-index: 8;
  top: calc(100% + 9px);
  left: -8px;
  width: min(330px, calc(100vw - 72px));
  display: grid;
  gap: 10px;
  padding: 12px;
  border: 1px solid var(--border, #333);
  border-radius: 13px;
  background: var(--surface, #171717);
  box-shadow: 0 18px 45px rgb(0 0 0 / .25);
}

.id-name-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.id-name-actions .id-btn { min-height: 40px; }

.id-deploy-name {
  min-width: 0;
  color: var(--text, #f5f5f5);
  font-size: 15px;
  font-weight: 650;
}

.id-current-chip {
  flex: none;
  padding: 3px 8px;
  border: 0;
  border-radius: 999px;
  background: color-mix(in srgb, var(--accent, #8b7cf6) 16%, transparent);
  color: color-mix(in srgb, var(--accent, #8b7cf6) 70%, var(--text, #f5f5f5));
  font-size: 10px;
  font-weight: 650;
}

.id-deploy-meta {
  margin-top: 3px;
  font-size: 12px;
  line-height: 1.4;
}

/* Status reads as a soft pill on the row's right edge. */
.id-status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: none;
  padding: 6px 11px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 650;
}

.id-status-pill svg { flex: none; }

.id-status-pill--success {
  background: color-mix(in srgb, var(--success, #3fbf77) 13%, transparent);
  color: color-mix(in srgb, var(--success, #3fbf77) 80%, var(--text, #f5f5f5));
}

.id-status-pill--progress {
  background: color-mix(in srgb, var(--accent, #8b7cf6) 14%, transparent);
  color: color-mix(in srgb, var(--accent, #8b7cf6) 75%, var(--text, #f5f5f5));
}

.id-status-pill--danger {
  background: color-mix(in srgb, var(--danger, #d65a5a) 14%, transparent);
  color: var(--danger, #e67a7a);
}

.id-status-pill--muted {
  background: var(--surface-2, #1e1e1e);
  color: var(--muted, #999);
}

.id-deploy-detail {
  min-width: 0;
  overflow: hidden;
  display: -webkit-box;
  margin-top: 3px;
  color: var(--muted, #999);
  font-size: 12px;
  line-height: 1.4;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.id-deploy-detail--danger { color: var(--danger, #e67a7a); }

/* Adding a deployment is a quiet row at the end of the list, not a button
   competing with the section title. */
.id-add-row {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 13px;
  padding: 13px 16px;
  border: 0;
  border-top: 1px solid var(--border-light, var(--border, #2a2a2a));
  background: transparent;
  color: var(--muted, #999);
  font-size: 13.5px;
  font-weight: 600;
  text-align: left;
  cursor: pointer;
  transition: background .15s ease, color .15s ease;
}

.id-add-row:hover { background: color-mix(in srgb, var(--text, #f5f5f5) 3%, transparent); color: var(--text, #f5f5f5); }
.id-add-row:active { background: color-mix(in srgb, var(--text, #f5f5f5) 6%, transparent); }

.id-add-plus {
  width: 38px;
  height: 38px;
  flex: none;
  display: grid;
  place-items: center;
  border: 1px dashed var(--border, #333);
  border-radius: 10px;
  color: var(--muted, #999);
}

.id-add-row:hover .id-add-plus {
  border-color: color-mix(in srgb, var(--accent, #8b7cf6) 55%, var(--border, #333));
  color: var(--accent, #8b7cf6);
}

/* Hosting details are a footnote, not a headline. */
.id-dep-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  min-height: 44px;
  padding: 0 12px 0 16px;
  border-top: 1px solid var(--border-light, var(--border, #2a2a2a));
  background: color-mix(in srgb, var(--surface-2, #1e1e1e) 55%, var(--surface, #171717));
  color: var(--muted, #999);
  font-size: 12px;
}

.id-railway-plan-link {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  min-height: 40px;
  color: var(--accent, #8b7cf6);
  text-decoration: none;
  white-space: nowrap;
}
.id-railway-plan-link:hover { text-decoration: underline; text-underline-offset: 3px; }

.id-live-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--muted, #999);
  font-size: 12px;
  font-weight: 600;
}

.id-open {
  width: 40px;
  height: 40px;
}

.id-deploy-actions { justify-content: flex-end; }

.id-deployment { container-type: inline-size; }

/* Labelled deployment actions reflow inside the card, not the viewport. */
.id-deploy-buttons {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
  gap: 6px;
  margin-top: 12px;
}

.id-deploy-buttons .id-btn {
  min-width: 0;
  min-height: 40px;
  gap: 5px;
  padding: 0 6px;
  font-size: 12.5px;
  white-space: nowrap;
  box-shadow: none;
}

.id-deploy-buttons .id-btn svg { flex: none; }
.id-deploy-buttons a.id-btn { text-decoration: none; }

.id-deploy-buttons--building { display: flex; flex-wrap: wrap; }
.id-deploy-buttons--building .id-btn { flex: 0 1 auto; padding-inline: 14px; }

.id-deploy-buttons .id-btn.is-attention {
  border-color: color-mix(in srgb, var(--danger, #d65a5a) 48%, var(--border, #333));
  color: var(--danger, #e67a7a);
}

@container (max-width: 430px) {
  .id-deploy-buttons { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .id-deploy-buttons .id-btn:last-child:nth-child(odd) { grid-column: 1 / -1; }
}

.id-label {
  margin-bottom: 6px;
  font-size: 11px;
}

.id-value {
  min-width: 0;
  color: var(--text, #f5f5f5);
  font-size: 13px;
  font-weight: 600;
}

.id-btn {
  min-height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 0 16px;
  border: 1px solid var(--border, #333);
  border-radius: var(--id-control-radius);
  background: var(--surface-2, #1e1e1e);
  color: var(--text, #f5f5f5);
  font-weight: 650;
  cursor: pointer;
  transition: background .15s ease, border-color .15s ease, color .15s ease,
    transform .12s ease, box-shadow .15s ease, filter .15s ease;
}

.id-btn:not(:disabled):active { transform: scale(.97); }

.id-btn--primary {
  border-color: transparent;
  background: var(--accent, #8b7cf6);
  color: var(--accent-fg, #fff);
  box-shadow: 0 6px 18px color-mix(in srgb, var(--accent, #8b7cf6) 32%, transparent);
}

.id-btn--danger {
  border-color: color-mix(in srgb, var(--danger, #d65a5a) 50%, var(--border, #333));
  background: color-mix(in srgb, var(--danger, #d65a5a) 14%, var(--surface, #171717));
  color: var(--danger, #e67a7a);
}

.id-btn--quiet {
  width: 100%;
  margin-top: 10px;
  border-color: color-mix(in srgb, var(--danger, #d65a5a) 28%, var(--border, #333));
  background: transparent;
  color: var(--danger, #e67a7a);
}

.id-btn:disabled,
.id-provider:disabled,
.id-avatar-edit:disabled {
  opacity: .52;
  cursor: default;
}

.id-railway-callout {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin: 12px 16px;
  padding: 14px;
  border: 1px solid color-mix(in srgb, var(--accent, #8b7cf6) 25%, var(--border, #333));
  border-radius: 10px;
  background: color-mix(in srgb, var(--accent, #8b7cf6) 8%, var(--surface-2, #1e1e1e));
}

.id-railway-callout strong,
.id-railway-callout span { display: block; }

.id-railway-callout strong {
  color: var(--text, #f5f5f5);
  font-size: 13px;
}

.id-railway-callout span {
  max-width: 52ch;
  margin-top: 3px;
  color: var(--muted, #999);
  font-size: 12px;
  line-height: 1.45;
}

.id-avatar-edit:focus-visible,
.id-btn:focus-visible,
.id-handle-btn:focus-visible,
.id-open:focus-visible,
.id-provider:focus-visible,
.id-input--boxed:focus-visible {
  outline: 2px solid var(--accent, #8b7cf6);
  outline-offset: 2px;
}

.id-notice {
  display: grid;
  gap: 5px;
  margin: -10px 0 18px;
  padding: 14px 16px;
  border: 1px solid color-mix(in srgb, var(--warning, #d9982f) 38%, var(--border, #333));
  border-radius: 13px;
  background: color-mix(in srgb, var(--warning, #d9982f) 9%, var(--surface, #171717));
  color: var(--text, #f5f5f5);
}

.id-notice span {
  color: var(--muted, #999);
  font-size: 13px;
  line-height: 1.5;
}

.id-notice--error {
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  border-color: color-mix(in srgb, var(--danger, #d65a5a) 38%, var(--border, #333));
  background: color-mix(in srgb, var(--danger, #d65a5a) 9%, var(--surface, #171717));
}

.id-notice--error .id-btn {
  grid-row: 1 / span 2;
  grid-column: 2;
}

.id-error,
.id-signin-error {
  color: var(--danger, #e67a7a);
  font-size: 13px;
  line-height: 1.5;
}

.id-error {
  padding: 20px 12px;
  text-align: center;
}

.id-signin-error { margin-top: 12px; }

.id-sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.id-loading-layout { pointer-events: none; }

.id-skeleton {
  position: relative;
  overflow: hidden;
  border-radius: 999px;
  background: var(--surface-2, #1e1e1e);
}

.id-skeleton::after {
  content: '';
  position: absolute;
  inset: 0;
  transform: translateX(-110%);
  background: linear-gradient(
    90deg,
    transparent,
    color-mix(in srgb, var(--text, #f5f5f5) 7%, transparent),
    transparent
  );
  animation: id-skeleton-sweep 1.5s ease-in-out infinite;
}

/* The loading state sketches the membership card so nothing jumps on load. */
.id-loading-hero {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 30px 22px;
  border: 1px solid rgba(255, 255, 255, .09);
  border-radius: 20px;
  background: var(--id-card-face);
}

.id-loading-hero .id-skeleton { background: rgba(255, 255, 255, .16); }

.id-loading-avatar {
  width: 80px;
  aspect-ratio: 1;
  border-radius: 50%;
}

.id-loading-profile { min-width: 0; }
.id-loading-title { width: min(270px, 72%); height: 32px; }

.id-loading-line { width: min(220px, 64%); height: 11px; margin-top: 12px; }

.id-loading-email { width: min(300px, 82%); height: 24px; margin-top: 17px; }

.id-loading-section-title { width: 142px; height: 16px; }

.id-loading-line--short { width: 210px; max-width: 70vw; margin-top: 9px; }

.id-loading-deploy-mark { width: 38px; height: 38px; border-radius: 11px; }

.id-loading-deploy-name { width: 120px; height: 14px; }

.id-loading-line--deployment { width: 190px; max-width: 48vw; margin-top: 7px; }

.id-fatal { max-width: 620px; }

.id-fatal h1 {
  margin: 0 0 10px;
  color: var(--text, #f5f5f5);
  font-size: 28px;
}

.id-fatal p {
  margin: 0 0 20px;
  color: var(--muted, #999);
  line-height: 1.55;
}

.id-modal-backdrop {
  position: fixed;
  z-index: 20;
  inset: 0;
  min-height: 100dvh;
  overflow: auto;
  display: grid;
  place-items: center;
  padding: 18px;
  background: color-mix(in srgb, var(--bg, #0d0d0d) 76%, transparent);
  backdrop-filter: blur(8px);
}

.id-modal {
  width: min(500px, 100%);
  max-height: calc(100dvh - 36px);
  overflow: auto;
  padding: 25px;
  border: 1px solid var(--border, #333);
  border-radius: 18px;
  background: var(--surface, #171717);
  box-shadow: 0 26px 72px rgba(0, 0, 0, .28);
}

.id-modal h2 {
  margin: 0 0 7px;
  color: var(--text, #f5f5f5);
  font-size: 22px;
  letter-spacing: -.025em;
}

.id-modal > p {
  max-width: 54ch;
  margin: 0 0 20px;
  color: var(--muted, #999);
  font-size: 14px;
  line-height: 1.55;
}

.id-handle-preview {
  width: fit-content;
  max-width: 100%;
  margin-bottom: 14px;
  overflow: hidden;
  color: var(--accent, #8b7cf6);
  font-size: 18px;
  font-weight: 680;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.id-input-wrap {
  display: flex;
  align-items: center;
  padding: 0 13px;
  border: 1px solid var(--border, #333);
  border-radius: 12px;
  background: var(--surface-2, #1e1e1e);
}

/* The wrapper draws the focus ring; the shell's input ring would double it. */
.id-input-wrap .id-input:focus-visible { outline: 0; }

.id-input-wrap:focus-within {
  border-color: var(--accent, #8b7cf6);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent, #8b7cf6) 15%, transparent);
}

.id-input-prefix { color: var(--muted, #999); }

.id-input {
  min-width: 0;
  width: 100%;
  height: 46px;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--text, #f5f5f5);
}

.id-input--boxed {
  width: 100%;
  margin-top: 6px;
  padding: 0 12px;
  border: 1px solid var(--border, #333);
  border-radius: 11px;
  background: var(--surface-2, #1e1e1e);
}

.id-check-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: start;
  gap: 11px;
  margin-top: 18px;
  color: var(--text, #f5f5f5);
  cursor: pointer;
}

.id-check-row input {
  width: 18px;
  height: 18px;
  margin: 2px 0 0;
  accent-color: var(--accent, #8b7cf6);
}

.id-check-row strong,
.id-check-row small { display: block; }
.id-check-row strong { font-size: 13px; }
.id-check-row small {
  margin-top: 3px;
  color: var(--muted, #999);
  font-size: 12px;
  line-height: 1.45;
}

.id-manage-modal { width: min(620px, 100%); }

.id-manage-panel {
  min-width: 0;
  margin-top: 18px;
  padding: 0 2px 4px;
  border-top: 1px solid var(--border-light, var(--border, #2a2a2a));
}

.id-manage-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}

.id-manage-head p {
  margin: 5px 0 0;
  color: var(--muted, #999);
  font-size: 13px;
}

.id-connection-modal { width: min(480px, 100%); }

.id-connection-head { align-items: flex-start; margin-bottom: 18px; }

.id-connection-head p { overflow-wrap: anywhere; }

.id-connection-close {
  flex: none;
  padding-inline: 8px;
  border-color: transparent;
  background: transparent;
  color: var(--muted, #999);
}

.id-connection-facts {
  border-top: 1px solid var(--border, #333);
  border-bottom: 1px solid var(--border, #333);
}

.id-connection-fact {
  display: grid;
  grid-template-columns: 82px minmax(0, 1fr);
  align-items: center;
  gap: 0 14px;
  padding: 11px 0;
}

.id-connection-fact .id-label { align-self: start; padding-top: 2px; }

.id-connection-value {
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--text, #f5f5f5);
  font-size: 14px;
  font-weight: 650;
}

.id-connection-plan-actions { grid-column: 2; }

.id-connection-plan-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 16px;
}

.id-connection-plan-actions .id-btn {
  min-height: 44px;
  padding: 0;
  border: 0;
  background: transparent;
  font-size: 12px;
  text-decoration: none;
}

.id-connection-plan-link { color: var(--accent, #8b7cf6); }
.id-connection-refresh { color: var(--muted, #999); }

.id-connection-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  padding-top: 18px;
}

.id-connection-disconnect {
  width: auto;
  margin-top: 0;
  padding-inline: 14px;
}

.id-operation-status {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: start;
  gap: 10px;
  margin: -5px 0 18px;
  padding: 13px;
  border: 1px solid var(--border, #333);
  border-radius: 13px;
  background: var(--surface-2, #1e1e1e);
}

.id-operation-status > svg { margin-top: 1px; }
.id-operation-status strong,
.id-operation-status span { display: block; }
.id-operation-status strong { font-size: 13px; }
.id-operation-status span {
  margin-top: 3px;
  color: var(--muted, #999);
  font-size: 12px;
  line-height: 1.5;
}
.id-operation-status--danger {
  border-color: color-mix(in srgb, var(--danger, #d65a5a) 38%, var(--border, #333));
  color: var(--danger, #e67a7a);
}
.id-operation-status--progress {
  border-color: color-mix(in srgb, var(--accent, #8b7cf6) 34%, var(--border, #333));
  color: var(--accent, #a99bf8);
}

.id-deletion-recovery {
  margin: -5px 0 18px;
  padding: 15px;
  border: 1px solid color-mix(in srgb, var(--warning, #d9982f) 38%, var(--border, #333));
  border-radius: 13px;
  background: color-mix(in srgb, var(--warning, #d9982f) 8%, var(--surface-2, #1e1e1e));
}

.id-deletion-recovery-head {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: start;
  gap: 10px;
}

.id-deletion-recovery-head > svg {
  margin-top: 1px;
  color: var(--warning, #d9982f);
}

.id-deletion-recovery h3,
.id-deletion-recovery p { margin: 0; }

.id-deletion-recovery h3 {
  font-size: 13px;
  line-height: 1.35;
}

.id-deletion-recovery p {
  max-width: 68ch;
  margin-top: 4px;
  color: var(--muted, #999);
  font-size: 12px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.id-deletion-recovery-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 13px;
}

.id-absence-confirm {
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid color-mix(in srgb, var(--warning, #d9982f) 28%, var(--border, #333));
}

.id-absence-confirm strong,
.id-absence-confirm span { display: block; }

.id-absence-confirm strong { font-size: 13px; }

.id-absence-confirm span {
  max-width: 68ch;
  margin-top: 4px;
  color: var(--muted, #999);
  font-size: 12px;
  line-height: 1.5;
}

.id-absence-confirm > div {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 13px;
}

.id-plan {
  padding: 6px 8px;
  border-radius: 999px;
  background: var(--surface-2, #1e1e1e);
  color: var(--muted, #999);
  font-size: 11px;
  text-transform: capitalize;
}

.id-select {
  width: 100%;
  height: 46px;
  padding: 0 36px 0 12px;
  border: 1px solid var(--border, #333);
  border-radius: 11px;
  background-color: var(--surface-2, #1e1e1e);
  color: var(--text, #f5f5f5);
  appearance: none;
  -webkit-appearance: none;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%23999' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='m6 9 6 6 6-6'/></svg>");
  background-repeat: no-repeat;
  background-position: right 12px center;
  cursor: pointer;
}

.id-select:disabled { opacity: .55; cursor: default; }

.id-resource-fields {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.id-field-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.id-field-block small {
  color: var(--muted, #999);
  font-size: 11px;
  line-height: 1.4;
}

.id-manage-resources,
.id-manage-updates,
.id-manage-recovery {
  display: flex;
  flex-direction: column;
  gap: 13px;
}

.id-manage-updates > .id-btn { align-self: flex-start; }

.id-update-state {
  color: var(--muted, #999);
  font-size: 11px;
  line-height: 1.45;
}

.id-storage-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: end;
  gap: 12px;
}

.id-storage-row .id-btn { white-space: nowrap; }

.id-manage-storage {
  display: grid;
  gap: 12px;
  margin-top: 18px;
  padding-top: 18px;
  border-top: 1px solid var(--border, #333);
}

.id-manage-storage-intro strong,
.id-manage-storage-intro span { display: block; }
.id-manage-storage-intro strong { font-size: 13px; font-weight: 600; }
.id-manage-storage-intro span { margin-top: 3px; color: var(--muted, #999); font-size: 12px; line-height: 1.45; }
.id-storage-limit-note { margin: 0; color: var(--muted, #999); font-size: 12px; line-height: 1.45; }

.id-storage-confirm {
  padding: 14px;
  border: 1px solid var(--border, #333);
  border-radius: 12px;
  background: var(--surface-2, #1e1e1e);
}
.id-storage-confirm strong { display: block; font-size: 13px; }
.id-storage-confirm p { margin: 5px 0 14px; color: var(--muted, #999); font-size: 12px; line-height: 1.45; }
.id-storage-confirm-actions { display: flex; flex-wrap: wrap; gap: 8px; }

@media (max-width: 560px) {
  .id-storage-confirm-actions { display: grid; }
  .id-storage-confirm-actions .id-btn { width: 100%; }
}

.id-railway-conn-account {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.id-railway-plan {
  flex: 0 0 auto;
  padding: 3px 9px;
  border-radius: 999px;
  background: var(--surface-2, #1e1e1e);
  color: var(--muted, #999);
  font-size: 11px;
  text-transform: capitalize;
}

.id-railway-manage {
  flex: 0 0 auto;
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  min-height: 44px;
  gap: 2px;
  padding: 0 4px 0 10px;
  border: 0;
  border-radius: 10px;
  background: transparent;
  color: var(--text, #f5f5f5);
  font-size: 12.5px;
  font-weight: 650;
  cursor: pointer;
  transition: color .15s ease;
}

.id-railway-manage svg { color: var(--muted, #999); }
.id-railway-manage:hover { color: var(--accent, #8b7cf6); }

.id-metrics {
  display: flex;
  flex-direction: column;
  gap: 13px;
  padding: 2px 0 17px;
  border-bottom: 1px solid var(--border-light, var(--border, #2a2a2a));
}

.id-metrics--card {
  width: 100%;
  margin-top: 13px;
  padding: 13px 0 2px;
  border-top: 1px solid var(--border-light, var(--border, #2a2a2a));
  border-bottom: 0;
}

.id-manage-disclosure .id-metrics--card { margin-top: 0; padding-top: 0; border-top: 0; }

.id-metrics--card .id-meters {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.id-rename-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: end;
  gap: 12px;
}

.id-metrics.is-loading .id-meters { opacity: .5; }

.id-metrics-runtime {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--muted, #999);
  font-size: 12px;
}

.id-metrics-note {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0 17px;
  color: var(--muted, #999);
  font-size: 12px;
  border-bottom: 1px solid var(--border-light, var(--border, #2a2a2a));
}

.id-meters {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}

.id-meter {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.id-meter-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.id-meter-label {
  color: var(--muted, #999);
  font-size: 10px;
  letter-spacing: .05em;
  text-transform: uppercase;
}

.id-meter-value {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text, #f5f5f5);
  font-size: 12px;
}

.id-meter-limit { color: var(--muted, #999); }

.id-meter-track {
  height: 6px;
  border-radius: 999px;
  background: var(--surface-2, #1e1e1e);
  overflow: hidden;
}

.id-meter-fill {
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(
    90deg,
    color-mix(in srgb, var(--accent, #8b7cf6) 70%, #7c3aed 30%),
    var(--accent, #8b7cf6)
  );
  transition: width .4s ease;
}

.id-recovery {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 2px;
}

.id-recovery-note {
  font-size: 12px;
  line-height: 1.4;
  color: var(--muted, #999);
}

.id-recovery-note.is-error { color: var(--danger, #f87171); }

.id-recovery-hint {
  color: var(--muted, #999);
  font-size: 11px;
  line-height: 1.4;
}

.id-composer-modal { width: min(560px, 100%); }

.id-composer-sub {
  margin: 4px 0 20px;
  color: var(--muted, #999);
  font-size: 13px;
}

.id-input-wrap--tick { position: relative; padding-right: 42px; }

.id-tick {
  position: absolute;
  right: 12px;
  top: 50%;
  transform: translateY(-50%);
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--success, #38b86c);
  color: #fff;
}

.id-launch-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  margin: 14px 0 2px;
  color: var(--muted, #999);
  font-size: 12px;
}

/* .id-modal > p would otherwise win and turn this into body copy with no gap. */
.id-modal > .id-launch-summary { max-width: none; margin: 14px 0 20px; font-size: 12px; }

.id-launch-summary b { color: var(--text, #f5f5f5); font-weight: 620; }

.id-launch-summary i {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: var(--border, #555);
}

.id-disclosure {
  margin-top: 16px;
  border: 1px solid var(--border, #333);
  border-radius: var(--id-control-radius, 12px);
  background: var(--surface-2, #1e1e1e);
  overflow: hidden;
}

.id-disclosure > summary {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 13px 15px;
  color: var(--text, #f5f5f5);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  list-style: none;
}

.id-disclosure > summary::-webkit-details-marker { display: none; }

.id-disclosure-state {
  margin-left: auto;
  color: var(--muted, #999);
  font-size: 11px;
  font-weight: 500;
}

.id-disclosure-caret {
  display: grid;
  place-items: center;
  color: var(--muted, #999);
  transition: transform .2s ease;
}

.id-disclosure[open] .id-disclosure-caret { transform: rotate(90deg); }

.id-disclosure-body {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 15px;
  border-top: 1px solid var(--border-light, var(--border, #2a2a2a));
}

.id-region-group {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-top: 14px;
  border-top: 1px solid var(--border-light, var(--border, #2a2a2a));
}

.id-switch {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: start;
  gap: 12px;
  cursor: pointer;
}

.id-switch-input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}

.id-switch-track {
  position: relative;
  width: 40px;
  height: 24px;
  margin-top: 1px;
  flex: 0 0 auto;
  border-radius: 999px;
  background: var(--border, #444);
  transition: background .2s ease;
}

.id-switch-track::after {
  content: "";
  position: absolute;
  top: 3px;
  left: 3px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #fff;
  transition: transform .2s ease;
}

.id-switch-input:checked + .id-switch-track { background: var(--accent, #8b7cf6); }
.id-switch-input:checked + .id-switch-track::after { transform: translateX(16px); }
.id-switch-input:focus-visible + .id-switch-track {
  outline: 2px solid var(--accent, #8b7cf6);
  outline-offset: 2px;
}

.id-switch-copy {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.id-switch-copy strong { font-size: 13px; color: var(--text, #f5f5f5); }
.id-switch-copy span { font-size: 12px; line-height: 1.45; color: var(--muted, #999); }

.id-eyebrow {
  margin: 2px 0 -4px;
  color: var(--muted, #999);
  font-size: 10px;
  letter-spacing: .06em;
  text-transform: uppercase;
}

/* Cost reassurance: Möbius takes no payment; Railway bills the user directly. */
.id-cost-note {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  margin: 0;
  padding: 11px 13px;
  border: 1px solid var(--border, #2a2a2a);
  border-radius: var(--id-control-radius, 12px);
  background: var(--surface-2, #1c1c1c);
  color: var(--muted, #999);
  font-size: 12.5px;
  line-height: 1.45;
}
.id-cost-note svg { flex: none; margin-top: 1px; opacity: .8; }
.id-cost-note strong { color: var(--accent, #8b7cf6); font-weight: 600; }

.id-composer-foot {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 20px;
}

.id-composer-note {
  margin: 0;
  color: var(--muted, #999);
  font-size: 11px;
  line-height: 1.45;
}

.id-composer-foot .id-modal-actions { margin: 0; }

.id-deploy-btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.id-railway-callout--warn {
  border-color: color-mix(in srgb, #e5a13a 42%, var(--border, #333));
  background: color-mix(in srgb, #e5a13a 12%, var(--surface-2, #1e1e1e));
}

.id-manage-links {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin: 18px 0 0;
}

.id-manage-links .id-btn {
  min-width: 0;
  justify-content: center;
}

.id-manage-links .id-btn:nth-child(n + 3) {
  grid-column: 1 / -1;
}

.id-manage-settings {
  display: flex;
  flex-direction: column;
  gap: 0;
  margin-top: 0;
}

.id-manage-retry {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  margin-top: 18px;
  padding: 13px;
  border: 1px solid color-mix(in srgb, #e5a13a 42%, var(--border, #333));
  border-radius: 12px;
  background: color-mix(in srgb, #e5a13a 12%, var(--surface-2, #1e1e1e));
}

.id-manage-retry div { min-width: 0; }
.id-manage-retry strong,
.id-manage-retry span { display: block; }
.id-manage-retry strong { font-size: 13px; }
.id-manage-retry span {
  margin-top: 4px;
  color: var(--muted, #999);
  font-size: 12px;
  line-height: 1.4;
}
.id-manage-retry .id-btn { flex: none; }

@media (max-width: 560px) {
  .id-manage-retry { align-items: stretch; flex-direction: column; }
  .id-manage-retry .id-btn { width: 100%; justify-content: center; }
}

.id-manage-disclosure {
  margin-top: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
}

.id-manage-disclosure > summary {
  display: grid;
  grid-template-columns: minmax(0, auto) minmax(0, 1fr) auto;
  padding: 15px 2px;
}
.id-manage-disclosure > summary:focus-visible {
  outline: 2px solid var(--accent, #8b7cf6);
  outline-offset: 2px;
  border-radius: 6px;
}

.id-manage-disclosure .id-disclosure-body {
  padding: 17px 2px 3px;
}

.id-manage-disclosure .id-disclosure-state {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: right;
}

@media (max-width: 560px) {
  .id-manage-disclosure > summary { grid-template-columns: minmax(0, 1fr) auto; }
  .id-manage-disclosure .id-disclosure-state { display: none; }
}

.id-manage-recovery .id-recovery-btn {
  align-self: flex-start;
}

.id-manage-error,
.id-delete-confirm {
  margin-top: 15px;
  padding: 13px;
  border-radius: 12px;
}

.id-manage-error {
  background: color-mix(in srgb, var(--danger, #d65a5a) 10%, var(--surface-2, #1e1e1e));
  color: var(--danger, #e67a7a);
  font-size: 12px;
  line-height: 1.45;
}

.id-delete-confirm {
  border: 1px solid color-mix(in srgb, var(--danger, #d65a5a) 35%, var(--border, #333));
  background: color-mix(in srgb, var(--danger, #d65a5a) 8%, var(--surface, #171717));
}

.id-delete-confirm strong,
.id-delete-confirm span { display: block; }
.id-delete-confirm strong { font-size: 13px; }
.id-delete-confirm span {
  margin: 4px 0 13px;
  color: var(--muted, #999);
  font-size: 12px;
}

.id-delete-trigger { margin-top: 18px; }
.id-modal-close { width: 100%; margin-top: 12px; }

.id-hint {
  min-height: 20px;
  margin-top: 8px;
  color: var(--muted, #999);
  font-size: 12px;
}

.id-hint.is-error { color: var(--danger, #e67a7a); }

.id-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
  margin-top: 20px;
}

.id-lock {
  width: 44px;
  height: 44px;
  margin: 0 0 18px;
  display: grid;
  place-items: center;
  border-radius: 13px;
  background: color-mix(in srgb, var(--accent, #8b7cf6) 13%, var(--surface-2, #1e1e1e));
  color: var(--accent, #8b7cf6);
}

.id-provider-list {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 9px;
}

.id-provider {
  width: 100%;
  min-height: 48px;
  display: grid;
  grid-template-columns: 20px minmax(0, 1fr) 20px;
  align-items: center;
  gap: 10px;
  padding: 0 13px;
  border: 1px solid #dadce0;
  border-radius: 12px;
  background: #fff;
  color: #171717;
  font-size: 13px;
  font-weight: 650;
  cursor: pointer;
  transition: background 150ms ease, transform 150ms ease;
}

.id-provider-mark,
.id-provider-balance {
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
}

.id-provider-mark svg {
  display: block;
  width: 20px;
  height: 20px;
}

.id-provider-copy {
  grid-column: 2;
  text-align: center;
  white-space: nowrap;
}

.id-progress {
  min-height: 22px;
  margin-top: 13px;
  color: var(--muted, #999);
  font-size: 13px;
}

.id-retry-completion,
.id-cancel-signin {
  width: 100%;
  margin-top: 14px;
}

.id-cancel-signin {
  border-color: transparent;
  background: transparent;
  color: var(--muted, #999);
}

.id-spin { animation: id-spin 1s linear infinite; }

@keyframes id-spin { to { transform: rotate(360deg); } }

@media (hover: hover) {
  .id-open:hover {
    background: var(--surface-2, #1e1e1e);
    color: var(--text, #f5f5f5);
  }

  .id-btn:not(:disabled):hover { border-color: color-mix(in srgb, var(--accent, #8b7cf6) 45%, var(--border, #333)); }
  .id-btn--primary:not(:disabled):hover {
    border-color: transparent;
    background: var(--accent-hover, var(--accent, #8b7cf6));
    filter: brightness(1.06);
  }
  .id-provider:not(:disabled):hover { background: #f4f4f4; transform: translateY(-1px); }
}

@media (max-width: 720px) {
  /* Drop the redundant subtitle on mobile and let the connection pill size to
     its label (the short brand yields the space) so it shows "Linked to
     mobius.you" in full instead of ellipsizing to "Linked…". */
  .id-kicker { display: none; }
  .id-brand { min-width: 0; }
  .id-brand strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .id-status {
    max-width: none;
    flex: 0 0 auto;
    padding: 6px 8px;
  }

  .id-status-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .id-auth {
    min-height: 330px;
    padding: 26px 4px 38px;
  }

  .id-auth h1 { font-size: 48px; }

  .id-card-3d { padding: 18px 18px 16px; }
  .id-cardid { gap: 14px; margin-top: 18px; }
  .id-avatar { width: 72px; font-size: 27px; }
  .id-title { font-size: clamp(20px, 5.6vw, 24px); }
  .id-cardfoot { margin-top: 18px; }

  .id-auth { padding: 26px 20px 26px; }

  .id-provider-list { grid-template-columns: 1fr; }

  .id-card-head { align-items: center; }

  .id-dep-foot {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 6px 12px;
    padding: 14px 16px 8px;
  }
  .id-dep-foot .id-railway-conn-account {
    grid-area: 1 / 1;
    line-height: 1.5;
    overflow: visible;
    white-space: normal;
    overflow-wrap: anywhere;
  }
  .id-dep-foot .id-railway-plan { grid-area: 1 / 2; justify-self: end; }
  .id-dep-foot .id-railway-plan-link {
    grid-area: 2 / 1;
    justify-self: start;
    min-height: 44px;
    white-space: normal;
  }
  .id-dep-foot .id-railway-manage {
    grid-area: 2 / 2;
    justify-self: end;
    margin: 0;
    padding: 0;
  }
  .id-loading-avatar { width: 72px; }
  .id-loading-title { height: 24px; }

  .id-railway-callout {
    align-items: stretch;
    flex-direction: column;
  }

  .id-railway-callout .id-btn { width: 100%; }

  .id-resource-fields { grid-template-columns: 1fr; }
  .id-composer-modal .id-disclosure-state { display: none; }
  .id-composer-modal .id-disclosure-title { white-space: nowrap; }
  .id-composer-modal .id-disclosure-caret { margin-left: auto; }
  .id-meters { grid-template-columns: 1fr; }
  .id-metrics--card .id-meters { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .id-rename-row { grid-template-columns: 1fr; align-items: stretch; }
  .id-rename-row .id-btn { width: 100%; }
  .id-storage-row { grid-template-columns: 1fr; align-items: stretch; }
  .id-storage-row .id-btn { width: 100%; }
  .id-manage-links { display: grid; grid-template-columns: 1fr; }
  .id-connection-fact { grid-template-columns: 78px minmax(0, 1fr); }
  .id-connection-plan-actions { grid-column: 1 / -1; margin-top: 2px; }
  .id-connection-footer { display: grid; grid-template-columns: minmax(0, 1fr) auto; }
  .id-connection-footer > .id-btn:first-child { min-width: 0; white-space: normal; font-size: 12px; }
  .id-deletion-recovery-actions { display: grid; grid-template-columns: 1fr; }
  .id-absence-confirm > div { display: grid; grid-template-columns: 1fr; }
  .id-delete-confirm > div { align-items: stretch; flex-direction: column; }

  .id-notice--error { grid-template-columns: 1fr; }

  .id-notice--error .id-btn {
    grid-row: auto;
    grid-column: auto;
    width: 100%;
    margin-top: 8px;
  }

  .id-modal-actions { flex-direction: column-reverse; }
  .id-modal-actions .id-btn { width: 100%; }
}

.id-agent-card {
  margin-top: 18px;
  background: var(--surface, #171717);
}

.id-model-visibility-card {
  margin-top: 18px;
  background: var(--surface, #171717);
}

.id-model-visibility-status {
  color: var(--muted, #999);
  font-size: 12px;
  font-weight: 700;
}

.id-model-visibility-error {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  color: var(--danger, #e77979);
  font-size: 12px;
}

.id-agent-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 53px;
  padding: 15px 16px;
  border-bottom: 1px solid var(--border-light, var(--border, #2b2b2b));
}

.id-agent-heading h2 { margin: 0; font-size: 15px; line-height: 1.25; }
.id-agent-balance { flex: 0 0 auto; font-size: 15px; font-weight: 700; font-variant-numeric: tabular-nums; }
.id-agent-body { display: grid; gap: 14px; padding: 15px 16px 16px; }
.id-agent-muted, .id-model-unit { margin: 0; color: var(--muted, #999); font-size: 12px; line-height: 1.5; }

.id-model-details {
  min-width: 0;
  border-top: 1px solid var(--border-light, var(--border, #2b2b2b));
}
.id-model-details > summary {
  min-height: 52px;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 0 16px;
  color: var(--text, #f5f5f5);
  font-size: 13px;
  font-weight: 650;
  list-style: none;
  cursor: pointer;
}
.id-model-details > summary::-webkit-details-marker { display: none; }
.id-model-details > summary:focus-visible {
  outline: 2px solid var(--accent, #8b7cf6);
  outline-offset: 3px;
  border-radius: 5px;
}
.id-model-summary-meta {
  margin-left: auto;
  color: var(--muted, #999);
  font-size: 11.5px;
  font-weight: 550;
}
.id-model-details > summary svg {
  flex: 0 0 auto;
  color: var(--muted, #999);
  transition: transform .16s ease;
}
.id-model-details[open] > summary svg { transform: rotate(90deg); }
.id-model-details-body { padding: 0 16px 16px; }
.id-model-list { display: grid; border-top: 1px solid var(--border-light, var(--border, #2b2b2b)); }
.id-model-row {
  min-width: 0;
  padding: 12px 0;
  border-bottom: 1px solid var(--border, #2b2b2b);
  display: grid;
  grid-template-columns: minmax(90px, .55fr) minmax(0, 1fr);
  gap: 12px;
  align-items: baseline;
}
.id-model-row strong { font-size: 14px; }
.id-model-row span { color: var(--muted, #999); font-size: 11.5px; line-height: 1.45; text-align: right; }
.id-model-row i { font-style: normal; opacity: .55; }
.id-model-unit { margin-top: 8px; }

.id-agent-activate { display: grid; gap: 14px; }
.id-retention-notice {
  display: grid;
  grid-template-columns: 20px minmax(0, 1fr);
  gap: 11px;
  align-items: start;
  color: var(--muted, #aaa);
  font-size: 12.5px;
  line-height: 1.5;
}
.id-retention-notice svg { flex: 0 0 auto; margin-top: 1px; color: var(--accent, #8b7cf6); }
.id-agent-alert {
  padding: 11px 13px;
  border: 1px solid color-mix(in srgb, var(--danger, #e67a7a) 35%, transparent);
  border-radius: 11px;
  background: color-mix(in srgb, var(--danger, #e67a7a) 8%, transparent);
  color: var(--text, #f5f5f5);
  font-size: 12.5px;
  line-height: 1.45;
}
.id-agent-skeleton { display: block; width: min(260px, 70%); height: 26px; border-radius: 8px; }

@media (max-width: 480px) {
  .id-model-row { grid-template-columns: 1fr; gap: 4px; }
  .id-model-row span { text-align: left; }
}

@media (max-width: 480px) {
  .id-deployment-main {
    grid-template-columns: auto minmax(0, 1fr);
    align-items: start;
    gap: 11px;
  }

  .id-deploy-actions {
    grid-column: 2;
    justify-content: flex-start;
    margin-top: 2px;
  }

  .id-deploy-detail { -webkit-line-clamp: 3; }
}

@media (max-width: 430px) {
  .id-avatar { width: 64px; font-size: 24px; }
  .id-avatar-edit { width: 26px; height: 26px; }
  .id-title { font-size: 20px; }
  .id-email {
    width: 100%;
    display: grid;
    grid-template-columns: 13px minmax(0, 1fr);
    align-items: center;
    gap: 4px 7px;
    font-size: 11.5px;
  }
  .id-email > span:first-of-type {
    overflow: visible;
    text-overflow: clip;
    white-space: normal;
    overflow-wrap: anywhere;
  }
  .id-private-label {
    grid-column: 2;
    margin-left: 0;
    padding-left: 0;
    border-left: 0;
  }
  .id-cardfoot { gap: 10px 14px; }
  .id-loading-avatar { width: 64px; }
  .id-loading-title { height: 22px; }
}

@media (prefers-reduced-motion: reduce) {
  .id-spin { animation: none; }
  .id-provider { transition: none; }
  .id-model-details > summary svg { transition: none; }
}

/* mobius-ui:CenteredRail v1 */
@media (min-width: 900px) {
  .id-root {

  }
  .id-top { width: min(100%, 712px); margin-inline: auto; }
}
/* /mobius-ui:CenteredRail */

.settings .id-root--settings {
  height: auto;
  min-height: 0;
  overflow: visible;
  background: transparent;
  font-family: inherit;
}
.settings .id-root--settings .id-scroll {
  flex: initial;
  min-height: 0;
  overflow: visible;
  padding: 0;
}
.settings .id-root--settings .id-shell { width: 100%; max-width: none; }
.settings .id-root--settings .id-settings-unlink {
  margin: 0 0 12px;
  color: var(--muted);
}
[data-theme="light"] .settings .id-root--settings {
  --id-card-face: radial-gradient(120% 160% at 12% 8%, #fffdfd 0%, #f3eff9 55%, #eae4f3 100%);
  --id-card-fg: #29232f;
  --id-card-muted: #686071;
}
[data-theme="light"] .settings .id-root--settings .id-card-3d {
  border-color: rgba(69, 45, 103, .16);
  box-shadow: 0 18px 42px rgba(62, 43, 87, .14);
}
[data-theme="light"] .settings .id-root--settings .id-card-3d::after {
  background: radial-gradient(circle at var(--id-mx, 30%) var(--id-my, 20%), rgba(139, 108, 247, .16), transparent 42%);
}
[data-theme="light"] .settings .id-root--settings .id-avatar {
  border-color: rgba(79, 54, 113, .2);
  background: rgba(139, 108, 247, .1);
}
[data-theme="light"] .settings .id-root--settings .id-avatar-edit {
  border-color: #f2edf9;
  background: #fff;
  color: #29232f;
}

/* Railway account window, tuned to Settings' type scale and control sizes. */
.settings .id-root--settings .id-connection-modal {
  width: min(440px, 100%);
  padding: 22px 22px 20px;
  border-radius: 14px;
}
.settings .id-root--settings .id-connection-head { align-items: center; margin-bottom: 16px; }
.settings .id-root--settings .id-connection-head h2 { margin: 0; font-size: 17px; font-weight: 600; }
.settings .id-root--settings .id-connection-head p { margin: 3px 0 0; color: var(--muted); font-size: 13px; }
.settings .id-root--settings .id-connection-close {
  min-height: 32px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: transparent;
  color: var(--text);
  font-size: 13px;
}
.settings .id-root--settings .id-connection-fact {
  grid-template-columns: 92px minmax(0, 1fr);
  gap: 6px 14px;
  padding: 12px 0;
}
.settings .id-root--settings .id-connection-fact + .id-connection-fact {
  border-top: 1px solid var(--border-light, var(--border));
}
.settings .id-root--settings .id-connection-fact .id-label {
  align-self: center;
  padding: 0;
  margin: 0;
  color: var(--muted);
  font-size: 13px;
  font-weight: 500;
}
.settings .id-root--settings .id-connection-fact .id-select {
  height: 36px;
  padding: 0 32px 0 12px;
  border-radius: 8px;
  font: inherit;
  font-size: 14px;
}
.settings .id-root--settings .id-connection-value { font-size: 14px; font-weight: 500; }
.settings .id-root--settings .id-connection-plan-actions { gap: 4px 14px; }
.settings .id-root--settings .id-connection-plan-actions .id-btn {
  min-height: 28px;
  font-size: 13px;
  font-weight: 500;
}
.settings .id-root--settings .id-connection-footer {
  gap: 10px;
  margin-top: 4px;
  padding-top: 16px;
}
.settings .id-root--settings .id-connection-footer .id-btn {
  min-height: 36px;
  padding: 0 14px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
}
.settings .id-root--settings .id-connection-footer .id-connection-disconnect {
  border: 1px solid color-mix(in srgb, var(--danger, #e67a7a) 45%, transparent);
  background: transparent;
  color: var(--danger, #e67a7a);
}
.settings .id-root--settings .id-connection-footer .id-connection-disconnect svg { width: 14px; height: 14px; }

/* Phone: keep plan links under the value, labels centred, and equal footer buttons. */
@media (max-width: 640px) {
  .settings .id-root--settings .id-connection-fact { grid-template-columns: 84px minmax(0, 1fr); }
  .settings .id-root--settings .id-connection-plan-actions { grid-column: 2; margin-top: 0; }
  .settings .id-root--settings .id-connection-footer { display: grid; grid-template-columns: 1fr; gap: 8px; }
  .settings .id-root--settings .id-connection-plan-actions { flex-direction: column; align-items: flex-start; gap: 0; }
  .settings .id-root--settings .id-connection-footer > .id-btn {
    justify-content: center;
    min-width: 0;
    white-space: nowrap;
    font-size: 13px;
  }
}
`
