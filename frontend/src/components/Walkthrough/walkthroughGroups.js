// The app screens of the first-run guide. Each app is described in the guide's
// own words. Whether it is installed, listed, and what artwork it has come from
// the live apps list and App Store catalog (see WalkthroughStore.jsx).
// `title` is [plain, accent]. Effects (title, lead, eyebrow) and the card entrance differ on every
// screen, so no two neighbouring screens animate alike.
export const APP_GROUPS = [
  {
    id: 'system',
    eyebrow: 'Ready on day one', title: ['Collaboration apps.', 'Included with Möbius.'],
    lead: 'Already installed and ready to use. Find and share apps, manage your profile, and connect with the Möbius community.',
    apps: [
      { id: 'store', name: 'App Store', blurb: 'Discover, install, publish, and update apps.' },
      { id: 'identity', name: 'Möbius · You', blurb: 'Your Möbius account, public profile, and the deployments you run.' },
      { id: 'social', name: 'Social', blurb: 'Message other Möbius people and join the community board.' },
    ],
  },
  {
    id: 'personalize',
    eyebrow: 'Make it yours', title: ['Apps for personalization', 'and self improvement.'],
    lead: 'Memory keeps what matters. Reflection learns from what went wrong, so your agent keeps improving.',
    apps: [
      { id: 'memory', name: 'Memory', blurb: 'A graph of lasting facts your agent can recall when it matters, without cluttering every chat.' },
      { id: 'reflection', name: 'Reflection', blurb: 'Your agent notes what went wrong. Reflection reviews it daily and suggests fixes.' },
    ],
  },
  {
    id: 'artifacts',
    eyebrow: 'Create and share', title: ['Build useful', 'artifacts.'],
    lead: 'Pages, maps, and websites your agent builds are saved, versioned, and ready to share.',
    apps: [
      { id: 'pages', name: 'Pages', blurb: 'Browse, version, and share the pages and documents your agent builds.' },
      { id: 'maps', name: 'Maps', blurb: 'Keep the maps your agent creates, with links back to where they came from.' },
      { id: 'webstudio', name: 'Web Studio', blurb: 'Build a website with your agent and preview it live without leaving Möbius.' },
    ],
  },
  {
    id: 'explore',
    eyebrow: 'Have some fun', title: ['Play, learn,', 'and explore.'],
    lead: 'Make music, learn a language, track your travels, and catch up on the news.',
    apps: [
      { id: 'beat-machine', name: 'Beat Machine', blurb: 'Make beats with 32 steps and your own sounds.' },
      { id: 'tandem', name: 'Tandem', blurb: 'Learn languages through made-for-you stories.' },
      { id: 'atlas', name: 'Atlas', blurb: 'Mark where you have been on a living 3D globe.' },
      { id: 'news', name: 'News', blurb: 'A daily briefing on the topics you care about.' },
    ],
  },
  {
    id: 'insight',
    eyebrow: 'Work smarter', title: ['Get more done.', 'Understand your agent.'],
    lead: 'See the skills your agent has, what it has scheduled, and which services it can reach. You can even give it a voice.',
    apps: [
      { id: 'skills', name: 'Skills', blurb: 'Browse the skill guides that shape what your agent can do.' },
      { id: 'integrations', name: 'Integrations', blurb: 'Connect outside services once and every agent can use them.' },
      { id: 'tasks', name: 'Tasks', blurb: 'See your agent’s scheduled check ins and what needs your attention.' },
      { id: 'voice', name: 'Voice', blurb: 'Give your agent a voice with on device text to speech.' },
    ],
  },
]
