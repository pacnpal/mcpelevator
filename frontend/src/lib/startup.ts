import type { ServerState, StartupPhase, StartupStatus } from './types';

type StartupServer = {
	enabled: boolean;
	state: ServerState;
	startup_status: StartupStatus | null;
};

export type PrimaryServerAction = 'start' | 'stop' | 'retry';

export const FAST_POLL_MS = 1000;
export const SLOW_POLL_MS = 5000;

const PHASE_LABELS: Record<StartupPhase, string> = {
	queued: 'Queued',
	setup: 'Running setup',
	bridge: 'Starting bridge',
	readiness: 'Checking readiness',
	retry_wait: 'Waiting to retry'
};

export function startupPhaseLabel(phase: StartupPhase): string {
	return PHASE_LABELS[phase];
}

export function hasActiveStartup(server: Pick<StartupServer, 'startup_status'>): boolean {
	return server.startup_status !== null;
}

export function canRetryServer(server: StartupServer): boolean {
	return (
		server.enabled &&
		!hasActiveStartup(server) &&
		(server.state === 'failed' || server.state === 'unhealthy')
	);
}

export function primaryServerAction(server: StartupServer): PrimaryServerAction {
	if (hasActiveStartup(server)) return 'stop';
	if (canRetryServer(server)) return 'retry';
	return server.enabled ? 'stop' : 'start';
}

export function shouldPollFast(server: StartupServer): boolean {
	if (hasActiveStartup(server) || server.state === 'starting' || server.state === 'stopping') {
		return true;
	}
	if (server.state === 'failed' || server.state === 'unhealthy') return false;
	// "idle" is a stable resting state (deliberately quiesced, wakes on demand) —
	// slow-poll it like running/stopped, not like a transition.
	if (server.state === 'idle') return false;
	return server.enabled ? server.state !== 'running' : server.state !== 'stopped';
}

export function pollingInterval(servers: readonly StartupServer[]): number {
	return servers.some(shouldPollFast) ? FAST_POLL_MS : SLOW_POLL_MS;
}

function timestamp(value: string | null, now: number): number | null {
	if (!value || !Number.isFinite(now)) return null;
	const parsed = Date.parse(value);
	return Number.isFinite(parsed) ? parsed : null;
}

function duration(milliseconds: number, roundUp: boolean): string {
	const seconds = Math.max(0, roundUp ? Math.ceil(milliseconds / 1000) : Math.floor(milliseconds / 1000));
	if (seconds < 60) return `${seconds}s`;
	const minutes = Math.floor(seconds / 60);
	if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
	return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function formatElapsed(startedAt: string, now = Date.now()): string | null {
	const started = timestamp(startedAt, now);
	return started === null ? null : duration(now - started, false);
}

export function formatCountdown(targetAt: string | null, now = Date.now()): string | null {
	const target = timestamp(targetAt, now);
	return target === null ? null : target <= now ? 'now' : duration(target - now, true);
}
