// Shared page toast — one auto-dismissing flash message, rendered once in the root
// layout so pages just call `flashToast(...)` instead of each owning state + markup.

let current = $state<{ message: string; tone: 'error' | 'info' } | null>(null);
let timer: ReturnType<typeof setTimeout> | undefined;

export function flashToast(message: string, tone: 'error' | 'info' = 'error') {
	current = { message, tone };
	clearTimeout(timer);
	timer = setTimeout(() => (current = null), 6000);
}

export function dismissToast() {
	clearTimeout(timer);
	current = null;
}

export const toast = {
	get current() {
		return current;
	}
};
