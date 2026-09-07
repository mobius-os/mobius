import test from 'node:test'
import assert from 'node:assert/strict'

import { handleAppProjectsRequest } from '../appProjectControl.js'

const response = body => ({ body })
const readJson = async value => value.body

function project({
  id,
  sourceAppId = 7,
  template = { id: 'site', files: { 'index.html': 'private-template.html' } },
  ...overrides
}) {
  return {
    id,
    name: `${id} project`,
    color: '#123456',
    project_type: 'studio:site',
    source_app_id: sourceAppId,
    template,
    updated_at: '2026-09-01T12:00:00Z',
    chat_id: `${id}-primary-chat`,
    chats: [{ id: `${id}-chat`, title: 'Private chat', provider: 'private-provider' }],
    legacy_source: { storage_root: `apps/7/projects/${id}` },
    artifacts: [{ id: `${id}-artifact`, source: 'private-source.html' }],
    ...overrides,
  }
}

function runtimeView(row) {
  return {
    id: row.id,
    name: row.name,
    color: row.color,
    project_type: row.project_type,
    template: { id: row.template.id },
    updated_at: row.updated_at,
  }
}

function harness({ projects = [], templates = [], legacy = [], created } = {}) {
  const calls = []
  const opened = []
  const published = []
  const createdProject = created || project({ id: 'created' })
  const client = {
    list: async () => response(projects),
    templates: async () => response(templates),
    legacy: async () => response(legacy),
    importLegacy: async payload => {
      calls.push(['import', payload])
      return response({ ok: true })
    },
    create: async payload => {
      calls.push(['create', payload])
      return response({ ...createdProject, name: payload.name })
    },
  }
  const options = {
    app: { id: 7, name: 'Studio' },
    client,
    readJson,
    browseProjects: () => calls.push(['browse']),
    openProject: value => opened.push(value),
    publishProjects: rows => published.push(rows),
    randomUUID: () => 'request-1',
  }
  return { calls, opened, options, published }
}

test('project listing keeps ordinary and legacy app projects behind a least-privilege view', async () => {
  const ordinary = project({ id: 'ordinary' })
  const legacy = project({ id: 'legacy', legacy_source: {
    app_id: 7,
    project_id: 'old-site',
    storage_root: 'apps/7/projects/old-site',
  } })
  const imported = project({
    id: 'project-owned-import',
    template: { id: 'site', imported_from: { artifact_id: 'private-artifact' } },
  })
  const h = harness({ projects: [ordinary, legacy, imported, project({
    id: 'other-app', sourceAppId: 8,
  })] })

  const result = await handleAppProjectsRequest(h.options, { action: 'list' })

  assert.deepEqual(result, [runtimeView(ordinary), runtimeView(legacy)])
  assert.equal(h.published.length, 1)
})

test('project open rejects foreign and Project-owned imports without navigating', async () => {
  const imported = project({
    id: 'project-owned-import',
    template: { id: 'site', imported_from: { artifact_id: 'private-artifact' } },
  })
  const h = harness({ projects: [
    project({ id: 'other', sourceAppId: 8 }),
    imported,
  ] })
  for (const projectId of ['other', 'project-owned-import']) {
    await assert.rejects(
      handleAppProjectsRequest(h.options, { action: 'open', projectId }),
      /unavailable to this app/,
    )
  }
  assert.deepEqual(h.opened, [])
})

test('project open returns only the runtime view', async () => {
  const owned = project({ id: 'owned' })
  const h = harness({ projects: [owned] })

  const result = await handleAppProjectsRequest(
    h.options, { action: 'open', projectId: 'owned' },
  )

  assert.deepEqual(result, runtimeView(owned))
  assert.deepEqual(h.opened, [owned])
})

test('project creation uses the exact authorized template and navigates once', async () => {
  const existing = project({ id: 'existing' })
  const created = project({ id: 'created' })
  const h = harness({
    projects: [existing],
    created,
    templates: [
      { key: 'foreign', source_app_id: 8, name: 'Foreign' },
      { key: 'studio:site', source_app_id: 7, name: 'Site' },
    ],
  })
  const result = await handleAppProjectsRequest(h.options, {
    action: 'create', templateId: 'studio:site', name: '',
  })

  assert.deepEqual(result, runtimeView({ ...created, name: 'Untitled site' }))
  assert.deepEqual(h.calls, [['create', {
    name: 'Untitled site',
    template_id: 'studio:site',
    recovery_request_id: 'request-1',
  }]])
  assert.deepEqual(h.published.at(-1).map(row => row.id), ['created', 'existing'])
  assert.equal(h.opened.length, 1, 'create owns exactly one Project navigation')
  assert.equal(h.opened[0].id, 'created')
})

test('stale or unauthorized template ids do not create or open a project', async () => {
  for (const templateId of ['', 'studio:removed', 'foreign:site']) {
    const h = harness({ templates: [
      { key: 'studio:site', source_app_id: 7, name: 'Site' },
      { key: 'foreign:site', source_app_id: 8, name: 'Foreign site' },
    ] })
    await assert.rejects(
      handleAppProjectsRequest(h.options, { action: 'create', templateId, name: '' }),
      /project type is unavailable to this app/,
    )
    assert.deepEqual(h.calls, [])
    assert.deepEqual(h.opened, [])
  }
})

test('a created Project-owned import is not exposed or opened', async () => {
  const h = harness({
    templates: [{ key: 'studio:site', source_app_id: 7, name: 'Site' }],
    created: project({
      id: 'imported',
      template: { id: 'site', imported_from: { artifact_id: 'private-artifact' } },
    }),
  })

  await assert.rejects(
    handleAppProjectsRequest(h.options, {
      action: 'create', templateId: 'studio:site', name: 'Imported',
    }),
    /created project is unavailable to this app/,
  )
  assert.equal(h.calls.filter(([action]) => action === 'create').length, 1)
  assert.deepEqual(h.opened, [])
})

test('legacy migration imports only this app and returns narrow refreshed projects', async () => {
  const own = project({ id: 'own' })
  const imported = project({
    id: 'project-owned-import',
    template: { id: 'site', imported_from: { artifact_id: 'private-artifact' } },
  })
  const h = harness({
    projects: [own, imported, project({ id: 'other', sourceAppId: 8 })],
    legacy: [
      { app_id: 7, legacy_project_id: 'site', name: 'Site', imported: false },
      { app_id: 7, legacy_project_id: 'done', name: 'Done', imported: true },
      { app_id: 8, legacy_project_id: 'foreign', name: 'Foreign', imported: false },
    ],
  })
  const result = await handleAppProjectsRequest(h.options, { action: 'migrate' })
  assert.deepEqual(h.calls, [['import', {
    app_id: 7,
    legacy_project_id: 'site',
    name: 'Site',
  }]])
  assert.deepEqual(result, [runtimeView(own)])
})

 test('template discovery exposes only this installed app and no private contract fields', async () => {
  const h = harness({ templates: [
    { key: 'renamed:game', id: 'game', source_app_id: 7, name: 'Canvas game', kind: 'game', description: 'Play', guidance: 'private', files: { secret: 'path' } },
    { key: 'foreign:game', id: 'game', source_app_id: 8, name: 'Other' },
  ] })
  assert.deepEqual(await handleAppProjectsRequest(h.options, { action: 'templates' }), [
    { key: 'renamed:game', id: 'game', name: 'Canvas game', kind: 'game', description: 'Play' },
  ])
  assert.deepEqual(h.opened, [])
})

test('Pages discovers only its own linked eligible sources, without source paths or app inventory', async () => {
  const h = harness()
  h.options.app = { id: 9, slug: 'pages' }
  h.options.client.importSources = async () => response({ management: 'linked', apps: [{ id: 'secret' }], artifacts: [
    { id: 'site', name: 'Site', kind: 'artifact', catalog_app_id: 9, private: 'omit' },
    { id: 'foreign', name: 'Foreign', kind: 'artifact', catalog_app_id: 8 },
  ] })
  assert.deepEqual(await handleAppProjectsRequest(h.options, { action: 'import-sources' }), [{ id: 'site', name: 'Site' }])
})

test('other apps cannot discover or import Pages sources even with a forged source id', async () => {
  const h = harness()
  h.options.client.importSources = () => { throw new Error('must not fetch') }
  for (const action of ['import-sources', 'import-source']) {
    await assert.rejects(handleAppProjectsRequest(h.options, { action, sourceId: 'site' }), /unavailable to this app/)
  }
})

test('an old copy-import server exposes no eligible sources and cannot be invoked', async () => {
  const h = harness()
  h.options.app = { id: 9, slug: 'pages' }
  h.options.client.importSources = async () => response({ artifacts: [{ id: 'site', kind: 'artifact', catalog_app_id: 9 }] })
  h.options.client.importSource = () => { throw new Error('must not import') }
  assert.deepEqual(await handleAppProjectsRequest(h.options, { action: 'import-sources' }), [])
  await assert.rejects(handleAppProjectsRequest(h.options, { action: 'import-source', sourceId: 'site' }), /server update/)
})

test('Pages imports its eligible source and opens the linked Project, exposing only the narrow result', async () => {
  const linked = project({ id: 'linked', template: { id: 'website', imported_from: { management: 'linked', kind: 'artifact', id: 'site' } } })
  const h = harness({ projects: [linked] })
  h.options.app = { id: 9, slug: 'pages' }
  h.options.client.importSources = async () => response({ management: 'linked', artifacts: [{ id: 'site', kind: 'artifact', catalog_app_id: 9 }] })
  h.options.client.importSource = async body => { h.calls.push(body); return response(linked) }
  assert.deepEqual(await handleAppProjectsRequest(h.options, { action: 'import-source', sourceId: 'site', kind: 'app' }), runtimeView(linked))
  assert.deepEqual(h.calls, [{ kind: 'artifact', source_id: 'site' }])
  assert.deepEqual(h.opened, [linked])
})

test('already-managed or foreign sources are hidden and stale import actions cannot mutate', async () => {
  const h = harness()
  h.options.app = { id: 9, slug: 'pages' }
  h.options.client.importSources = async () => response({ management: 'linked', artifacts: [{ id: 'foreign', kind: 'artifact', catalog_app_id: 8 }] })
  h.options.client.importSource = () => { throw new Error('must not import') }
  assert.deepEqual(await handleAppProjectsRequest(h.options, { action: 'import-sources' }), [])
  for (const sourceId of ['managed', 'foreign', '../escape']) {
    await assert.rejects(handleAppProjectsRequest(h.options, { action: 'import-source', sourceId }), /already managed/)
  }
  assert.deepEqual(h.opened, [])
})

test('Pages never opens a copied or mismatched import result', async () => {
  for (const imported_from of [{ kind: 'artifact', id: 'site' }, { management: 'linked', kind: 'app', id: 'site' }]) {
    const h = harness()
    h.options.app = { id: 9, slug: 'pages' }
    h.options.client.importSources = async () => response({ management: 'linked', artifacts: [{ id: 'site', kind: 'artifact', catalog_app_id: 9 }] })
    h.options.client.importSource = async () => response(project({ id: 'wrong', template: { id: 'website', imported_from } }))
    await assert.rejects(handleAppProjectsRequest(h.options, { action: 'import-source', sourceId: 'site' }), /did not return a linked/)
    assert.deepEqual(h.opened, [])
  }
})
