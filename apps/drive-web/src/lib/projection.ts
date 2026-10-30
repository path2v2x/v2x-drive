/**
 * Project CARLA world points into the streamed camera image, so names can be drawn above actors
 * on top of the MJPEG frame.
 *
 * CARLA's frame is Unreal's: x forward, y right, z up (left-handed), rotations in degrees with
 * pitch about y (positive = nose up), yaw about z (positive = turning towards +y) and roll about x.
 * The rotation matrix below is the one carla::geom::Transform builds, so a camera's world
 * transform as reported by `sensor.get_transform()` inverts exactly.  The camera "fov"
 * attribute is the horizontal field of view.
 */
import type { CameraInfo } from '$lib/types';

const DEG = Math.PI / 180;

export interface ProjectedPoint {
	/** pixel column in the camera image (0..w) */
	u: number;
	/** pixel row in the camera image (0..h) */
	v: number;
	/** distance along the camera's forward axis (m) */
	depth: number;
}

/** The image coordinates of a world point, or null when it is behind the camera. */
export function projectPoint(cam: CameraInfo, x: number, y: number, z: number): ProjectedPoint | null {
	const cy = Math.cos(cam.yaw * DEG), sy = Math.sin(cam.yaw * DEG);
	const cp = Math.cos(cam.pitch * DEG), sp = Math.sin(cam.pitch * DEG);
	const cr = Math.cos(cam.roll * DEG), sr = Math.sin(cam.roll * DEG);
	// columns of R are the camera's forward, right and up axes in world coordinates
	const fwd = [cp * cy, cp * sy, sp];
	const right = [cy * sp * sr - sy * cr, sy * sp * sr + cy * cr, -cp * sr];
	const up = [-cy * sp * cr - sy * sr, -sy * sp * cr + cy * sr, cp * cr];
	const dx = x - cam.x, dy = y - cam.y, dz = z - cam.z;
	const lx = fwd[0] * dx + fwd[1] * dy + fwd[2] * dz;      // forward
	if (lx < 0.3) return null;
	const ly = right[0] * dx + right[1] * dy + right[2] * dz; // right
	const lz = up[0] * dx + up[1] * dy + up[2] * dz;         // up
	const f = cam.w / 2 / Math.tan((cam.fov * DEG) / 2);
	return { u: cam.w / 2 + (f * ly) / lx, v: cam.h / 2 - (f * lz) / lx, depth: lx };
}

export interface FrameRect {
	left: number;
	top: number;
	width: number;
	height: number;
	/** displayed pixels per image pixel */
	scale: number;
}

/** Where an `object-contain` image of frameW x frameH sits inside a boxW x boxH element. */
export function frameRect(boxW: number, boxH: number, frameW: number, frameH: number): FrameRect {
	const scale = Math.min(boxW / frameW, boxH / frameH);
	const width = frameW * scale, height = frameH * scale;
	return { left: (boxW - width) / 2, top: (boxH - height) / 2, width, height, scale };
}

/** role_names the drive server gives its own cars (traffic, egos, playback); not worth a label. */
const GENERIC_ROLE_PREFIXES = ['ego_vehicle', 'autopilot', 'trajectory', 'dynamic_geofence', 'drive_', 'hero'];

export function isGenericRole(role: string | null | undefined): boolean {
	if (!role) return true;
	return GENERIC_ROLE_PREFIXES.some((p) => role.startsWith(p));
}
