import { defineConfig } from 'vitest/config';

/**
 * Tests run in a Node environment on purpose.
 *
 * The valuable frontend logic here — the connection state machine, the narrative
 * rules, the bounded buffers, backoff — is deliberately free of DOM access
 * (`FatigueSession` takes an injectable socket and frame provider), so it can be
 * tested directly without jsdom, a fake browser or a rendering library. Component
 * rendering is not covered by these tests, and the dashboard is not claimed to be
 * visually regression-tested.
 */
export default defineConfig({
  test: {
    environment: 'node',
    include: ['tests/**/*.test.ts'],
    reporters: ['default'],
  },
});
