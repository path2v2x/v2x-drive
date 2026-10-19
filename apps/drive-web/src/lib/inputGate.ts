/**
 * One rule for every keyboard shortcut on the drive page: keys typed into a text
 * field, select or editable element belong to that element, not to the car or
 * the panels. Shared by the driving controls, the page shortcuts and the HUD keys.
 */
export function isTypingTarget(target: EventTarget | null | undefined): boolean {
	if (!target || typeof (target as Element).tagName !== 'string') return false;
	const el = target as HTMLElement;
	const tag = el.tagName.toLowerCase();
	if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
	return el.isContentEditable === true;
}
