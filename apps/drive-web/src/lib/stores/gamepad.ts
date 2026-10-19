/**
 * Gamepad Store — steering wheel + pedal input via the browser Gamepad API.
 *
 * Pedal normalization strategy:
 *   G923 pedals always rest at +1.0 and travel toward -1.0 when pressed, so
 *   trackers are seeded with `DEFAULT_CALIBRATION.gasRest`/`brakeRest` and
 *   `detected=true` at module load. Input is live the moment the page loads.
 *
 *   Sweep-detection only runs when the user remaps an axis via the wizard,
 *   since the new axis's rest value is unknown. After remap,
 *   `recalibrateRestValues()` flips trackers back into detect mode and the
 *   poll loop captures the new rest from a full press+release sweep (or a
 *   2 s fallback at an extreme).
 */

import { writable, derived, get } from 'svelte/store';
import { GAMEPAD_DEADZONE, DEFAULT_CALIBRATION } from '$lib/constants';
import type { GamepadCalibration } from '$lib/types';

// ── Public Stores ──

export const gamepadConnected = writable<boolean>(false);
export const gamepadName = writable<string>('');
export const gamepadIndex = writable<number>(-1);
export const rawAxes = writable<number[]>([]);
export const rawButtons = writable<boolean[]>([]);

// Wheel reverse gear. The G923 has no reverse pedal, so a face button
// stands in for the gear stick. Press once → R, press again → D. The
// gas pedal magnitude carries through unchanged; the bridge interprets
// `reverse=true` and drives the throttle backward.
const REVERSE_BUTTON_INDEX = 0; // PS X / Xbox A — first face button
export const wheelReverseGear = writable<boolean>(false);
let _prevReverseButtonPressed = false;

// ── Calibration (persisted) ──

const STORAGE_KEY = 'drive_calibration_v4';

// Whether the user has completed calibration (wizard or previously saved).
let _hasStoredCalibration = false;

function loadCalibration(): GamepadCalibration {
	if (typeof localStorage === 'undefined') return DEFAULT_CALIBRATION;
	// Clean up legacy keys
	localStorage.removeItem('drive_calibration');
	localStorage.removeItem('drive_calibration_v2');
	localStorage.removeItem('drive_calibration_v3');

	const saved = localStorage.getItem(STORAGE_KEY);
	if (saved) {
		try {
			const axes = JSON.parse(saved);
			_hasStoredCalibration = true;
			return {
				...DEFAULT_CALIBRATION,
				steerAxis: axes.steerAxis ?? DEFAULT_CALIBRATION.steerAxis,
				gasAxis: axes.gasAxis ?? DEFAULT_CALIBRATION.gasAxis,
				brakeAxis: axes.brakeAxis ?? DEFAULT_CALIBRATION.brakeAxis,
			};
		} catch {
			return DEFAULT_CALIBRATION;
		}
	}
	return DEFAULT_CALIBRATION;
}

export const calibrated = writable<boolean>(_hasStoredCalibration);

export const calibration = writable<GamepadCalibration>(loadCalibration());

calibration.subscribe((cal) => {
	if (typeof localStorage !== 'undefined') {
		localStorage.setItem(STORAGE_KEY, JSON.stringify({
			steerAxis: cal.steerAxis,
			gasAxis: cal.gasAxis,
			brakeAxis: cal.brakeAxis,
		}));
	}
});

// ── Pedal Detection State ──

interface PedalTracker {
	min: number;
	max: number;
	rest: number;
	detected: boolean;
}

let gas: PedalTracker = {
	min: Infinity, max: -Infinity,
	rest: DEFAULT_CALIBRATION.gasRest, detected: true,
};
let brake: PedalTracker = {
	min: Infinity, max: -Infinity,
	rest: DEFAULT_CALIBRATION.brakeRest, detected: true,
};
let framesPolled = 0;

const SWEEP_THRESHOLD = 1.0;   // min-max range that confirms a full press+release
const FALLBACK_FRAMES = 120;   // ~2s: if no sweep seen, snapshot current value

function resetDetection(): void {
	gas = { min: Infinity, max: -Infinity, rest: 0, detected: false };
	brake = { min: Infinity, max: -Infinity, rest: 0, detected: false };
	framesPolled = 0;
}

function isAtExtreme(value: number): boolean {
	return Math.abs(value) > 0.85 || Math.abs(value) < 0.15;
}

function updateTracker(tracker: PedalTracker, raw: number, frames: number): void {
	if (tracker.detected) return;

	tracker.min = Math.min(tracker.min, raw);
	tracker.max = Math.max(tracker.max, raw);
	const hasSweep = tracker.max - tracker.min > SWEEP_THRESHOLD;

	// Primary: full sweep seen AND pedal has returned to an extreme (rest).
	// This avoids capturing a mid-transit value as rest.
	if (hasSweep && isAtExtreme(raw)) {
		tracker.rest = raw;
		tracker.detected = true;
		return;
	}

	// Fallback: after 2 seconds with no sweep, use current extreme value.
	if (!hasSweep && frames >= FALLBACK_FRAMES && isAtExtreme(raw)) {
		tracker.rest = raw;
		tracker.detected = true;
		return;
	}
}

// ── Normalization ──

export interface NormalizedInput {
	steer: number;    // -1 (left) to 1 (right)
	throttle: number; // 0 to 1
	brake: number;    // 0 to 1
	reverse: boolean;
}

/**
 * Normalize a pedal axis to 0 (released) → 1 (pressed).
 * Uses the detected rest value to determine direction.
 */
function normalizePedal(raw: number, rest: number): number {
	if (Math.abs(rest) < 0.3) {
		// Rest near 0 → range is 0..1
		return Math.max(0, Math.min(1, raw));
	} else if (rest > 0.5) {
		// Rest near +1 → pressed goes toward -1
		return Math.max(0, Math.min(1, (1 - raw) / 2));
	} else {
		// Rest near -1 → pressed goes toward +1
		return Math.max(0, Math.min(1, (raw + 1) / 2));
	}
}

export const normalizedInput = derived(
	[rawAxes, calibration, wheelReverseGear],
	([$rawAxes, $cal, $reverse]): NormalizedInput => {
		if ($rawAxes.length === 0 || !gas.detected) {
			return { steer: 0, throttle: 0, brake: 0, reverse: false };
		}

		// Steering (always works — no rest ambiguity)
		let steer = $cal.steerInverted
			? -($rawAxes[$cal.steerAxis] ?? 0)
			: ($rawAxes[$cal.steerAxis] ?? 0);
		if (Math.abs(steer) < GAMEPAD_DEADZONE) steer = 0;
		// Non-linear curve: gentle near center, ramps toward full lock.
		// Mirrors CARLA's manual_control_steeringwheel.py G29 mapping
		// (`K1 * tan(1.1 * input)`, K1 = 0.55). At quarter-turn the
		// actual steer command is ~0.15 instead of 0.25, which keeps
		// lateral force inside the tire's grip envelope at speed.
		if (steer !== 0) {
			steer = 0.55 * Math.tan(1.1 * steer);
		}
		// Full lock is the car's full lock: the ego's sweep-wheel collision and
		// tire friction tweaks (drive_server.py) took care of the sliding that
		// the old ±0.7 cap used to mask.
		steer = Math.max(-1.0, Math.min(1.0, steer));

		// Pedals
		let throttle = normalizePedal($rawAxes[$cal.gasAxis] ?? 0, gas.rest);
		let brakeVal = brake.detected
			? normalizePedal($rawAxes[$cal.brakeAxis] ?? 0, brake.rest)
			: 0;
		if (throttle < GAMEPAD_DEADZONE) throttle = 0;
		if (brakeVal < GAMEPAD_DEADZONE) brakeVal = 0;

		return { steer, throttle, brake: brakeVal, reverse: $reverse };
	}
);

// ── Polling ──

let animFrameId: number | null = null;

function poll() {
	const gp = navigator.getGamepads()[get(gamepadIndex)];
	if (!gp) {
		if (get(gamepadIndex) >= 0) gamepadConnected.set(false);
		animFrameId = requestAnimationFrame(poll);
		return;
	}

	rawAxes.set([...gp.axes]);
	const buttonsPressed = gp.buttons.map((b) => b.pressed);
	rawButtons.set(buttonsPressed);

	// Edge-trigger reverse-gear toggle on the dedicated button. Press
	// once → R, press again → D. State sticks across frames the way the
	// keyboard's S-key gear does — releasing the button doesn't shift
	// out of R, only pressing again does.
	const reversePressed = buttonsPressed[REVERSE_BUTTON_INDEX] ?? false;
	if (reversePressed && !_prevReverseButtonPressed) {
		wheelReverseGear.update((r) => !r);
	}
	_prevReverseButtonPressed = reversePressed;

	// Update pedal detection
	if (!gas.detected || !brake.detected) {
		framesPolled++;
		const cal = get(calibration);
		const gasWas = gas.detected;
		const brakeWas = brake.detected;
		updateTracker(gas, gp.axes[cal.gasAxis] ?? 0, framesPolled);
		updateTracker(brake, gp.axes[cal.brakeAxis] ?? 0, framesPolled);

		if (gas.detected && !gasWas) {
			console.log(`[Gamepad] Gas rest detected: ${gas.rest.toFixed(3)} (after ${framesPolled} frames)`);
		}
		if (brake.detected && !brakeWas) {
			console.log(`[Gamepad] Brake rest detected: ${brake.rest.toFixed(3)} (after ${framesPolled} frames)`);
		}
	}

	animFrameId = requestAnimationFrame(poll);
}

// ── Lifecycle ──

export function startPolling(): void {
	if (animFrameId !== null) return;

	window.addEventListener('gamepadconnected', onConnect);
	window.addEventListener('gamepaddisconnected', onDisconnect);

	for (const gp of navigator.getGamepads()) {
		if (gp) {
			gamepadIndex.set(gp.index);
			gamepadConnected.set(true);
			gamepadName.set(gp.id);
			break;
		}
	}

	animFrameId = requestAnimationFrame(poll);
}

export function stopPolling(): void {
	if (animFrameId !== null) {
		cancelAnimationFrame(animFrameId);
		animFrameId = null;
	}
	window.removeEventListener('gamepadconnected', onConnect);
	window.removeEventListener('gamepaddisconnected', onDisconnect);
	wheelReverseGear.set(false);
	_prevReverseButtonPressed = false;
}

/**
 * Re-detect pedal rest values. Called after the calibration wizard
 * reassigns axes. User should have feet off pedals.
 */
export function recalibrateRestValues(): void {
	calibrated.set(true);
	resetDetection();
}

/**
 * Apply the hardcoded G923 rest values without running detection. Used by
 * the wizard's Skip path so input is live immediately and a held pedal can't
 * be miscaptured as the rest position.
 */
export function applyDefaultRests(): void {
	gas = {
		min: Infinity, max: -Infinity,
		rest: DEFAULT_CALIBRATION.gasRest, detected: true,
	};
	brake = {
		min: Infinity, max: -Infinity,
		rest: DEFAULT_CALIBRATION.brakeRest, detected: true,
	};
	framesPolled = 0;
	calibrated.set(true);
}

// ── Event Handlers ──

function onConnect(e: GamepadEvent) {
	gamepadIndex.set(e.gamepad.index);
	gamepadConnected.set(true);
	gamepadName.set(e.gamepad.id);
	console.log(`[Gamepad] Connected: ${e.gamepad.id} (${e.gamepad.axes.length} axes, ${e.gamepad.buttons.length} buttons)`);
}

function onDisconnect(e: GamepadEvent) {
	if (e.gamepad.index === get(gamepadIndex)) {
		gamepadIndex.set(-1);
		gamepadConnected.set(false);
		gamepadName.set('');
		rawAxes.set([]);
		rawButtons.set([]);
		resetDetection();
		console.log(`[Gamepad] Disconnected: ${e.gamepad.id}`);
	}
}
