import { execFile } from 'node:child_process';

const CC_TIMEOUT_MS = 600_000; // 10 minutes for RCA analysis
const MAX_BUFFER = 10 * 1024 * 1024; // 10 MB

export interface CcRunnerResult {
  success: boolean;
  result: string;
  rawOutput: string;
}

export function runClaude(
  prompt: string,
  opts?: {
    timeoutMs?: number;
    maxTurns?: number;
    mcpConfig?: string;
  },
): Promise<CcRunnerResult> {
  const args = [
    '-p',
    prompt,
    '--output-format',
    'json',
    '--dangerously-skip-permissions',
  ];

  if (opts?.maxTurns) {
    args.push('--max-turns', String(opts.maxTurns));
  }

  if (opts?.mcpConfig) {
    args.push('--mcp-config', opts.mcpConfig);
  }

  const timeout = opts?.timeoutMs ?? CC_TIMEOUT_MS;

  return new Promise((resolve) => {
    const env = {
      ...process.env,
      HOME: '/tmp',
    };
    console.log('CC CLI args:', JSON.stringify(args));
    execFile(
      'claude',
      args,
      { timeout, maxBuffer: MAX_BUFFER, env, cwd: '/var/task' },
      (error, stdout, stderr) => {
        if (error) {
          console.error('CC CLI error:', error.message);
          console.error('CC CLI error stderr:', stderr?.slice(0, 3000));
          console.error('CC CLI error stdout:', stdout?.slice(0, 3000));
          if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
            resolve({
              success: false,
              result: 'Claude Code CLI not found. Ensure @anthropic-ai/claude-code is installed globally.',
              rawOutput: stderr,
            });
            return;
          }

          if (error.killed) {
            resolve({
              success: false,
              result: `Claude Code timed out after ${timeout / 1000}s`,
              rawOutput: stdout || stderr,
            });
            return;
          }

          console.error('CC CLI stderr:', stderr);
          console.error('CC CLI stdout:', stdout?.slice(0, 2000));
          resolve({
            success: false,
            result: `Claude Code process error: ${error.message}`,
            rawOutput: stdout || stderr,
          });
          return;
        }

        console.log('CC CLI stderr:', stderr?.slice(0, 3000) || '(empty)');
        console.log('CC CLI stdout length:', stdout?.length ?? 0);
        console.log('CC CLI stdout preview:', stdout?.slice(0, 2000) || '(empty)');

        try {
          const parsed = JSON.parse(stdout);
          const result = parsed?.result ?? parsed?.data?.result ?? stdout;
          resolve({
            success: true,
            result: typeof result === 'string' ? result : JSON.stringify(result),
            rawOutput: stdout,
          });
        } catch {
          resolve({
            success: true,
            result: stdout.trim(),
            rawOutput: stdout,
          });
        }
      },
    );
  });
}
