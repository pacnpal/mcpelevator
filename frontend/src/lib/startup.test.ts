import { describe, expect, it } from 'vitest';

import {
	FAST_POLL_MS,
	SLOW_POLL_MS,
	canRetryServer,
	formatCountdown,
	formatElapsed,
	pollingInterval,
	primaryServerAction,
	shouldPollFast,
	startupPhaseLabel
} from './startup';
import type { ServerState, StartupStatus } from './types';

const activation: StartupStatus = {
	phase: 'setup',
	attempt: 2,
	max_attempts: 5,
	activation_started_at: '2026-07-15T12:00:00Z',
	deadline_at: '2026-07-15T12:01:00Z',
	next_retry_at: null,
	message: null
};

function server(state: ServerState, enabled = true, startup_status: StartupStatus | null = null) {
	return { state, enabled, startup_status };
}

describe('startup policy', () => {
	it('labels every startup phase', () => {
		expect(startupPhaseLabel('queued')).toBe('Queued');
		expect(startupPhaseLabel('setup')).toBe('Running setup');
		expect(startupPhaseLabel('bridge')).toBe('Starting bridge');
		expect(startupPhaseLabel('readiness')).toBe('Checking readiness');
		expect(startupPhaseLabel('retry_wait')).toBe('Waiting to retry');
	});

	it('prioritizes Stop for active startup and Retry for enabled terminal states', () => {
		expect(primaryServerAction(server('failed', true, activation))).toBe('stop');
		expect(primaryServerAction(server('failed'))).toBe('retry');
		expect(primaryServerAction(server('unhealthy'))).toBe('retry');
		expect(primaryServerAction(server('stopped', false))).toBe('start');
		expect(canRetryServer(server('failed', false))).toBe(false);
	});

	it('polls active, transitional, and desired-state mismatch states quickly', () => {
		expect(shouldPollFast(server('stopped', true, activation))).toBe(true);
		expect(shouldPollFast(server('starting'))).toBe(true);
		expect(shouldPollFast(server('running', false))).toBe(true);
		expect(pollingInterval([server('running'), server('stopped', false)])).toBe(SLOW_POLL_MS);
		expect(pollingInterval([server('stopped', true)])).toBe(FAST_POLL_MS);
	});

	it('keeps terminal failures on the slow poll without an active startup', () => {
		expect(shouldPollFast(server('failed'))).toBe(false);
		expect(shouldPollFast(server('unhealthy'))).toBe(false);
	});

	it('formats elapsed and countdown values safely', () => {
		const now = Date.parse('2026-07-15T12:01:05.500Z');
		expect(formatElapsed('2026-07-15T12:00:00Z', now)).toBe('1m 5s');
		expect(formatElapsed('2026-07-15T12:02:00Z', now)).toBe('0s');
		expect(formatCountdown('2026-07-15T12:01:07Z', now)).toBe('2s');
		expect(formatCountdown('2026-07-15T12:01:00Z', now)).toBe('now');
		expect(formatElapsed('bad date', now)).toBeNull();
		expect(formatCountdown(null, now)).toBeNull();
	});
});
