import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  goalObjectiveFromText,
  latestGoalObjective,
  progressRailViewModel,
} from '../goalProgress.js'

const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const progressRail = readFileSync(new URL('../ProgressRail.jsx', import.meta.url), 'utf8')
const chatCss = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')

test('goalObjectiveFromText follows the backend command boundary', () => {
  assert.equal(goalObjectiveFromText('/goal Ship the review'), 'Ship the review')
  assert.equal(
    goalObjectiveFromText('\n/goal   Build the first slice\nthen verify it'),
    'Build the first slice then verify it',
  )
  assert.equal(goalObjectiveFromText('please /goal later'), '')
  assert.equal(goalObjectiveFromText(' /goal indented is prose'), '')
  assert.equal(goalObjectiveFromText('/data/apps/x'), '')
})

test('goalObjectiveFromText does not present clear or an empty command as active', () => {
  assert.equal(goalObjectiveFromText('/goal'), '')
  assert.equal(goalObjectiveFromText('/goal   '), '')
  assert.equal(goalObjectiveFromText('/goal clear'), '')
  assert.equal(goalObjectiveFromText('/goal CLEAR'), '')
})

test('latestGoalObjective recovers only the latest visible owner turn', () => {
  assert.equal(latestGoalObjective([
    { role: 'user', content: '/goal old objective' },
    { role: 'assistant', content: 'Done' },
    { role: 'user', content: 'ordinary follow-up' },
  ]), '')
  assert.equal(latestGoalObjective([
    { role: 'user', content: '/goal build the indicator' },
    { role: 'user', content: 'hidden answer', hidden: true },
    { role: 'assistant', content: 'Working', partial: true },
  ]), 'build the indicator')
})

test('the goal reuses the progress rail and stays as context for build phases', () => {
  assert.deepEqual(
    progressRailViewModel('Build the indicator', []),
    [{
      key: 'goal',
      label: 'Goal · Build the indicator',
      current: true,
      expandable: true,
    }],
  )
  assert.deepEqual(
    progressRailViewModel('Build the indicator', [
      { ts: 1, label: 'First slice ready' },
      { ts: 2, label: 'Verifying' },
    ]),
    [
      {
        key: 'goal',
        label: 'Goal · Build the indicator',
        current: false,
        expandable: true,
      },
      { key: 'phase-1', label: 'First slice ready', current: false },
      { key: 'phase-2', label: 'Verifying', current: true },
    ],
  )
})

test('ChatView binds goal state to the existing run-start and turn-end lifecycle', () => {
  const runStarts = chatView.split('setBuildPhases(railAtRunStart())').slice(1)
  assert.equal(runStarts.length, 4, 'every current run-start seam should be covered')
  for (const suffix of runStarts) {
    assert.match(
      suffix.slice(0, 260),
      /setActiveGoalState\(goalObjectiveFromText\(/,
      'goal and build progress must reset together at each run boundary',
    )
  }
  assert.match(
    chatView,
    /if \(!turnActive\) \{\s*if \(activeGoalObjective\) setActiveGoalState\(''\)/,
    'a completed or stopped turn must retire its goal indication',
  )
  assert.match(
    chatView,
    /activeGoalObjective: objective/,
    'the existing chat cache should retain a goal across chat switches and steers',
  )
  assert.match(
    chatView,
    /<ProgressRail\s+items=\{progressRail\}/,
    'the goal should render through the shared progress rail',
  )
  assert.match(
    chatView,
    /`Following goal: \$\{activeGoalObjective\}\.`/,
    'screen readers should receive the same active-goal status',
  )
  assert.match(progressRail, /chat__progress-rail/)
  assert.match(progressRail, /aria-expanded=\{expanded\}/)
  assert.match(progressRail, /label\.scrollWidth > step\.clientWidth/)
  assert.match(
    chatCss,
    /\.chat__foot \.chat__progress-step--toggle[\s\S]*?\{ pointer-events: auto; \}/,
    'an expandable step must opt back into pointer input inside the transparent footer',
  )
  assert.doesNotMatch(progressRail, /goal|build/i,
    'the shared rail should not encode one producer’s domain')
})
