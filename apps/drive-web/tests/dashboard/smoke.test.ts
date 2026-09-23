import { describe, it, expect } from 'vitest';

describe('vitest setup', () => {
	it('runs at all', () => {
		expect(1 + 1).toBe(2);
	});

	it('has jsdom DOM available', () => {
		const el = document.createElement('div');
		el.textContent = 'hello';
		expect(el.textContent).toBe('hello');
	});
});
