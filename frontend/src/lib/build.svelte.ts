// The running build's version, as the layout's health poll last reported it. One source
// for every place that shows it (the footer, Settings → About), so they agree and both
// follow an upgrade on the next successful poll.
export const build = $state<{ version: string | null }>({ version: null });
