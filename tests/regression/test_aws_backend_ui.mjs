import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const html = fs.readFileSync(new URL('../../src/assets/static/index.html', import.meta.url), 'utf8');
const start = html.indexOf('function compatibilityPreviewText(result){');
const end = html.indexOf("el('compatibilityPreview').onclick", start);
assert.ok(start >= 0 && end > start);
const render = vm.runInNewContext(html.slice(start, end) + '\ncompatibilityPreviewText;', {});

test('unimplemented AWS options expose suitability, pending proof and source without claiming a URL', () => {
  const text = render({
    application_ir: {evidence: [{id: 'E-handler', path: 'handler.py'}]},
    candidates: [{
      id: 'aws-lambda', status: 'unsupported_by_sky', structural_status: 'potentially_compatible',
      reasons: ['Sky 배포 어댑터는 미구현입니다.'], selection_mode: 'unavailable',
      constraint_results: [
        {rule_id: 'LAMBDA-HANDLER-01', status: 'satisfied', reason: '함수 핸들러 형태 관찰', evidence_ids: ['E-handler']},
        {rule_id: 'LAMBDA-DURATION-01', status: 'unknown', reason: '실행 시간 미검증', evidence_ids: []},
      ],
    }],
  });
  assert.match(text, /AWS Lambda \(요청 핸들러\)/);
  assert.match(text, /비교 후보 · Sky 배포 미지원/);
  assert.match(text, /적합 가능 · 추가 검증 필요/);
  assert.match(text, /접근: 미구현 · 접근 방식 미정/);
  assert.match(text, /handler\.py/);
  assert.match(text, /실행 시간 미검증/);
  assert.doesNotMatch(text, /배포 시도 가능|선택한 공개 범위 미지원/);
});

test('EC2 is not preferred without host requirements', () => {
  const text = render({candidates: [{
    id: 'aws-ec2', status: 'unsupported_by_sky', structural_status: 'not_preferred',
    reasons: ['호스트 제어 필요성 미확인'], selection_mode: 'unavailable',
  }]});
  assert.match(text, /AWS EC2 \(호스트 제어\)/);
  assert.match(text, /호스트 제어 근거 없어 우선하지 않음/);
  assert.match(text, /Sky 배포 미지원/);
});
