import { api } from '../../api/client.js'

function publicationFailure(response, body) {
  const detail = body?.detail
  return {
    status: response.status,
    message: typeof detail === 'string'
      ? detail
      : detail?.message || 'Could not complete the reviewed GitHub action.',
    detail: typeof detail?.detail === 'string' ? detail.detail : '',
    code: typeof detail?.code === 'string' ? detail.code : '',
  }
}

const RECONCILE_ATTEMPTS = 4
const RECONCILE_DELAY_MS = 300

function waitForReconciliation(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

async function refetchUntilSettled({ refetch, select, pending, wait }) {
  if (typeof refetch !== 'function') return null
  let selected = null
  for (let attempt = 0; attempt < RECONCILE_ATTEMPTS; attempt += 1) {
    const refreshed = await refetch().catch(() => null)
    selected = select(refreshed?.data)
    if (!pending(selected) || attempt === RECONCILE_ATTEMPTS - 1) return selected
    await wait(RECONCILE_DELAY_MS)
  }
  return selected
}

function durablePublicationFailure(record, fallback) {
  const message = String(record?.last_submit_error || '').trim()
  if (!message) return fallback
  return {
    status: Number(fallback?.status) || 0,
    message,
    detail: String(record?.last_submit_error_detail || '').trim(),
    code: String(record?.last_submit_error_code || fallback?.code || ''),
  }
}

export function projectPublishedContribution(payload, recordId, publication = null) {
  if (!payload || !Array.isArray(payload.records)) return payload
  const status = publication?.record?.status === 'draft' ? 'draft' : 'open'
  return {
    ...payload,
    records: payload.records.map(record => record.id === recordId ? {
      ...record,
      status,
      number: publication?.number ?? record.number,
      url: publication?.url ?? record.url,
      needs_attention: false,
    } : record),
  }
}

/**
 * Execute one exact reviewed publication and reconcile an ambiguous outcome.
 * Both chat surfaces use this path so their state transitions cannot drift.
 */
export async function publishContribution({
  appId,
  record,
  autopilot,
  refetch,
  publish = api.contributions.publish,
  wait = waitForReconciliation,
}) {
  let failure
  try {
    const response = await publish(appId, record, { autopilot })
    const body = await response.json().catch(() => null)
    if (response.ok) return { kind: 'published', publication: body }
    failure = publicationFailure(response, body)
  } catch {
    failure = {
      status: 0,
      message: 'The result could not be confirmed.',
      detail: 'Contribute will reconcile the current branch and pull request before trying anything else.',
      code: 'unconfirmed_result',
    }
  }

  const latest = await refetchUntilSettled({
    refetch,
    select: data => data?.records?.find(row => row.id === record.id) || null,
    pending: current => !current || current.status === 'submitting',
    wait,
  })
  if (latest?.status === 'submitting') {
    return { kind: 'pending', failure, record: latest }
  }
  if (latest && latest.status !== 'prepared') {
    return { kind: 'reconciled', record: latest }
  }
  return {
    kind: 'failed',
    failure: durablePublicationFailure(latest, failure),
    record: latest || record,
  }
}

/** Execute one complete immutable stack action and reconcile partial/lost outcomes. */
export async function publishContributionStack({
  appId,
  item,
  refetch,
  publish = api.contributions.publishStack,
  wait = waitForReconciliation,
}) {
  const records = Array.isArray(item?.records) ? item.records : []
  let failure
  try {
    const response = await publish(appId, records)
    const body = await response.json().catch(() => null)
    if (response.ok) {
      await refetch?.().catch(() => null)
      return { kind: 'published', records: body?.records || [] }
    }
    failure = publicationFailure(response, body)
  } catch {
    failure = {
      status: 0,
      message: 'The stack result could not be confirmed.',
      detail: 'Contribute will reconcile every linked pull request before another action is offered.',
      code: 'unconfirmed_result',
    }
  }

  const currentRecords = await refetchUntilSettled({
    refetch,
    select: data => {
      const lifecycle = new Map(
        (data?.records || []).map(record => [record.id, record]),
      )
      const stacked = new Map(
        (data?.stack_units || [])
          .flatMap(unit => unit?.records || [])
          .map(record => [record.id, record]),
      )
      return records
        .map(record => stacked.get(record.id) || lifecycle.get(record.id))
        .filter(Boolean)
    },
    pending: current => !current || current.length < records.length
      || current.some(record => record.status === 'submitting'),
    wait,
  }) || []
  if (currentRecords.some(record => record.status === 'submitting')) {
    return { kind: 'pending', failure, records: currentRecords }
  }
  const failedRecord = currentRecords.find(record => (
    record.status === 'prepared'
    && String(record.last_submit_error || '').trim()
  ))
  if (failedRecord) {
    return {
      kind: 'failed',
      failure: durablePublicationFailure(failedRecord, failure),
      records: currentRecords,
    }
  }
  const current = new Map(currentRecords.map(record => [record.id, record]))
  const advanced = records.some(record => {
    const latest = current.get(record.id)
    if (!latest || latest.status === 'prepared') return false
    return latest.status !== record.status
      || (record.status !== 'prepared' && latest.action_key !== record.action_key)
  })
  if (advanced) return {
    kind: 'reconciled',
    records: currentRecords,
  }
  return { kind: 'failed', failure, records: currentRecords.length ? currentRecords : records }
}
