async (page) => {
  // Run with playwright-cli -s=<isolated-session> run-code --filename=<this file>.
  // All data comes from playbook_fixture_server.py, never a live AWS incident.
  const incident = '33333333-3333-4333-8333-333333333333';
  const url = `http://localhost:3211/report/${incident}?engine=strands`;
  const results = [];
  const consoleProblems = [];
  page.on('console', (message) => {
    if (['error', 'warning'].includes(message.type())) consoleProblems.push(message.text());
  });
  const check = (condition, message) => {
    if (!condition) throw new Error(message);
    results.push(message);
  };
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(url);
  await page.waitForFunction(() =>
    document.querySelector('#__nuxt')?.__vue_app__?.config.globalProperties.$nuxt?.isHydrating === false,
  );
  await page.getByRole('heading', { name: '원인 요약', exact: true }).waitFor();
  check(await page.getByRole('heading', { name: '5 Whys · 증상에서 원인까지', exact: true }).isVisible(),
    '5 Whys is visible in the first summary level');
  check(await page.locator('dialog[open]').count() === 0, 'initial view contains no open detail');
  check(await page.getByText('aws cloudwatch describe-alarms', { exact: false }).count() === 0,
    'initial summary does not expose the command body');
  await page.screenshot({ animations: 'disabled', path: 'docs/test-reports/playbook-drilldown-2026-09-15/summary-desktop.png' });
  const entry = page.getByRole('button', { name: '런북 전체 검토 · 실행 승인', exact: true });
  await entry.click();
  const dialog = page.locator('dialog[open]');
  await dialog.waitFor();
  check(await dialog.count() === 1, 'runbook opens one modal');
  check((await dialog.innerText()).includes('current-fixture'), 'current incident command is displayed in full');
  check(!(await dialog.innerText()).includes('historical-fixture'), 'historical target is not the approval command');
  const approve = dialog.getByRole('button', { name: '검토한 런북 승인하고 실행', exact: true });
  check(await approve.isDisabled(), 'approval requires the full-plan review checkbox');
  await dialog.getByRole('checkbox').check();
  check(await approve.isEnabled(), 'reviewing the plan enables a separate explicit approval action');
  await page.screenshot({ animations: 'disabled', path: 'docs/test-reports/playbook-drilldown-2026-09-15/runbook-modal.png' });
  for (const title of [
    '보고서 전문 · 근거', '원인 사슬 · 5 Whys', '사고 타임라인',
    '분석 경로 · 가설', '이번 사고의 플레이북 지식', '생성에 사용한 참고 자료', '실행 이력',
  ]) {
    await dialog.getByRole('navigation', { name: '상세 자료 전환' })
      .getByRole('button', { name: title, exact: true }).click();
    check(page.url() === url, `${title}: URL is unchanged`);
    check(await page.locator('dialog[open]').count() === 1, `${title}: no nested modal`);
    const analysisHeading = dialog.getByRole('heading', { name: '검토한 가설과 판단 근거', exact: true });
    if (title === '분석 경로 · 가설') {
      await analysisHeading.waitFor();
      check(await analysisHeading.isVisible(), 'analysis content is visible in its selected panel');
    }
    if (['이번 사고의 플레이북 지식', '생성에 사용한 참고 자료', '실행 이력'].includes(title)) {
      check(!await analysisHeading.isVisible(), `${title}: prior analysis content is hidden`);
    }
    if (title === '보고서 전문 · 근거') {
      check((await dialog.innerText()).includes('원문 로그') || (await dialog.innerText()).includes('fixture-evidence-h1'),
        'the complete report evidence is still readable');
    }
    if (title === '생성에 사용한 참고 자료') {
      check((await dialog.innerText()).includes('요청 취소 경로에서도 연결 반환을 검증'),
        'comparison displays proposed knowledge');
      await page.screenshot({ animations: 'disabled', path: 'docs/test-reports/playbook-drilldown-2026-09-15/comparison-modal.png' });
    }
  }
  await dialog.getByRole('button', { name: '상세 닫기', exact: true }).first().click();
  check(await page.locator('dialog[open]').count() === 0, 'closing returns to the summary');
  check(await entry.evaluate((element) => element === document.activeElement), 'focus returns to the original entry');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ animations: 'disabled', path: 'docs/test-reports/playbook-drilldown-2026-09-15/summary-mobile.png' });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    'mobile summary has no horizontal page overflow');
  await page.getByRole('button', { name: '참고 원본 · 변경 제안 검토', exact: true }).click();
  await dialog.waitFor();
  check(await page.locator('dialog[open]').count() === 1, 'mobile comparison stays at the second level');
  await page.screenshot({ animations: 'disabled', path: 'docs/test-reports/playbook-drilldown-2026-09-15/comparison-mobile.png' });
  await page.keyboard.press('Escape');
  check(await page.locator('dialog[open]').count() === 0, 'Escape closes the mobile modal');
  await page.setViewportSize({ width: 1440, height: 1000 });
  check(consoleProblems.length === 0, `browser has no errors or directive/hydration warnings: ${consoleProblems.join('; ')}`);
  return { fixture: true, results, count: results.length };
}
