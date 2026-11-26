// Step 24: a 10-request smoke run against the deployed /v1/retrieve, as the load-test tenant
// (docs/runbooks/load-test.md). It checks the wiring before the real runs in step 26:
// the token, the route, recall over the loadtest corpus.
//
//   export TOKEN=$(PYTHONPATH=services AWS_PROFILE=ragdp uv run python scripts/loadtest.py token)
//   k6 run -e API_URL=https://<api-id>.execute-api.us-east-2.amazonaws.com load/smoke.js
//
// One virtual user, one request after another, so it stays far below the stage throttle
// (10 requests/s). Reranking is off: no Cohere calls. Each request embeds its question
// with Titan (well under a cent for all 10).

import http from 'k6/http';
import { check } from 'k6';

// The golden questions are about the same 12 guidances the load tenant holds, so recall
// has something to find. open() runs once, at init; the path is relative to this file.
const QUESTIONS = open('../eval/golden/questions.jsonl')
  .split('\n')
  .filter((line) => line.trim() !== '')
  .map((line) => JSON.parse(line).question);

const API_URL = __ENV.API_URL;
const TOKEN = __ENV.TOKEN;

export const options = {
  scenarios: {
    smoke: { executor: 'shared-iterations', vus: 1, iterations: 10, maxDuration: '2m' },
  },
  // k6 exits non-zero unless every check passes.
  thresholds: { checks: ['rate==1'] },
};

export function setup() {
  if (!API_URL || !TOKEN) {
    throw new Error('set API_URL (-e API_URL=...) and TOKEN (scripts/loadtest.py token)');
  }
}

export default function () {
  const question = QUESTIONS[__ITER % QUESTIONS.length];
  const res = http.post(
    `${API_URL}/v1/retrieve`,
    JSON.stringify({ query: question, top_k: 10, rerank: false }),
    {
      headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' },
      tags: { route: 'retrieve' },
    },
  );
  check(res, {
    'status is 200': (r) => r.status === 200,
    'chunks returned': (r) => r.status === 200 && r.json('chunks').length > 0,
    'not degraded': (r) => r.status === 200 && r.json('degraded').length === 0,
  });
}
