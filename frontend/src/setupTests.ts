import '@testing-library/jest-dom';
import { configure } from '@testing-library/react';

// Testing Library's async helpers default to a 1000ms ceiling. That is ample for a
// test running alone, and not ample under full-suite parallel load: the
// ConnectorDetailPanel scope-picker tests `await import()` a component and then wait
// for it to mount, and at ~100 files across workers that pair intermittently missed
// the window — failing on Oracle in one run and PostgreSQL in the next, while
// passing in isolation and on re-run every time.
//
// Raising the ceiling changes only how long a CORRECT assertion is allowed to take,
// never whether it has to hold: a genuinely broken expectation still fails, just
// later. The alternative — patching individual call sites — leaves the same trap set
// for the next test that mounts something lazily.
configure({ asyncUtilTimeout: 5000 });
