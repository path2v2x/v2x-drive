/**
 * Editor store: the free camera's pose, the outliner and the selected actor's details.
 *
 * Protocol (drive WebSocket, see docs/editor-mode.md):
 *   -> start_session {mode: "editor", pose?}        <- session_ready {mode: "editor", camera}
 *   -> camera_move {f, r, u, dyaw, dpitch, dt, speed?, boost?}   <- camera_pose
 *   -> camera_focus {actor_id} / camera_follow {actor_id | null}   <- camera_pose
 *   -> list_world_actors {max}                      <- world_actors
 *   -> inspect_actor {actor_id}                     <- actor_details
 */

import { writable } from 'svelte/store';
import {
	editorStartCamera,
	registerDriveMessageHandler,
	sendDriveMessage
} from './driveSocket';
import type { ActorDetails, CameraPoseMessage, DriveMessage, WorldActor, WorldActorsMessage } from '$lib/types';

export const GROUP_LABELS: Record<string, string> = {
	ego: 'Browser cars',
	voices: 'VOICES vehicles',
	ghost: 'VOICES ghosts',
	fleet: 'V2X fleet',
	traffic: 'Traffic',
	dynamic: 'Dynamic actors',
	trajectory: 'Trajectory playback',
	foreign: 'Other clients',
	walker: 'Walkers',
	prop: 'Props',
	sensor: 'Sensors',
	light: 'Signals',
	other: 'Other'
};

export const cameraPose = writable<CameraPoseMessage | null>(null);
export const worldActors = writable<WorldActor[]>([]);
export const worldActorsInfo = writable<{ total: number; truncated: boolean; groups: Record<string, number>; listedAt: number }>({
	total: 0,
	truncated: false,
	groups: {},
	listedAt: 0
});
export const selectedActorId = writable<number | null>(null);
export const actorDetails = writable<ActorDetails | null>(null);
export const followingId = writable<number | null>(null);

registerDriveMessageHandler('camera_pose', (msg: DriveMessage) => {
	cameraPose.set(msg as CameraPoseMessage);
	followingId.set(typeof msg.following === 'number' ? msg.following : null);
});

registerDriveMessageHandler('world_actors', (msg: DriveMessage) => {
	const m = msg as WorldActorsMessage;
	worldActors.set(Array.isArray(m.actors) ? m.actors : []);
	worldActorsInfo.set({
		total: typeof m.total === 'number' ? m.total : 0,
		truncated: Boolean(m.truncated),
		groups: m.groups && typeof m.groups === 'object' ? m.groups : {},
		listedAt: Date.now()
	});
	followingId.set(typeof m.following === 'number' ? m.following : null);
});

registerDriveMessageHandler('actor_details', (msg: DriveMessage) => {
	actorDetails.set(msg as unknown as ActorDetails);
});

editorStartCamera.subscribe((camera) => {
	if (camera) {
		cameraPose.set(camera);
		followingId.set(typeof camera.following === 'number' ? camera.following : null);
	}
});

export function sendCameraMove(msg: DriveMessage): boolean {
	return sendDriveMessage(msg);
}

export function requestWorldActors(max = 400): boolean {
	return sendDriveMessage({ type: 'list_world_actors', max });
}

export function inspectActor(actorId: number): boolean {
	return sendDriveMessage({ type: 'inspect_actor', actor_id: actorId });
}

export function focusActor(actorId: number): boolean {
	return sendDriveMessage({ type: 'camera_focus', actor_id: actorId });
}

export function followActor(actorId: number | null): boolean {
	return sendDriveMessage({ type: 'camera_follow', actor_id: actorId });
}

/** Select a row of the outliner (null clears) and ask for its details. */
export function selectActor(actorId: number | null): void {
	selectedActorId.set(actorId);
	actorDetails.set(null);
	if (actorId !== null) inspectActor(actorId);
}

export function resetEditorStore(): void {
	cameraPose.set(null);
	worldActors.set([]);
	worldActorsInfo.set({ total: 0, truncated: false, groups: {}, listedAt: 0 });
	selectedActorId.set(null);
	actorDetails.set(null);
	followingId.set(null);
}

/** Distance from the camera to an actor, for the outliner rows. */
export function distanceFromCamera(camera: CameraPoseMessage | null, actor: WorldActor): number | null {
	if (!camera || !actor.pos) return null;
	return Math.hypot(actor.pos[0] - camera.x, actor.pos[1] - camera.y, actor.pos[2] - camera.z);
}
