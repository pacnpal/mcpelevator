<script lang="ts">
	import '../app.css';
	import favicon from '$lib/assets/favicon.svg';
	import HealthDot from '$lib/components/HealthDot.svelte';
	import Logo from '$lib/components/Logo.svelte';

	let { children } = $props();
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
	<title>mcpelevator</title>
</svelte:head>

<div class="flex min-h-[100dvh] flex-col">
	<header
		class="sticky top-0 z-40 border-b border-[var(--color-line)] bg-[color-mix(in_oklab,var(--color-base)_85%,transparent)] backdrop-blur-md"
	>
		<div
			class="mx-auto flex h-14 w-full max-w-6xl items-center justify-between gap-4 px-4 sm:px-6"
		>
			<a
				href="/"
				class="rounded-lg outline-offset-4 transition-opacity hover:opacity-90"
				aria-label="mcpelevator home"
			>
				<Logo />
			</a>
			<HealthDot />
		</div>
	</header>

	<main class="mx-auto w-full max-w-6xl flex-1 px-4 py-6 sm:px-6 sm:py-10">
		{@render children()}
	</main>
</div>
