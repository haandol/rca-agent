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
    '--verbose',
  ];

  if (opts?.maxTurns) {
    args.push('--max-turns', String(opts.maxTurns));
  }

  if (opts?.mcpConfig) {
    args.push('--mcp-config', opts.mcpConfig);
  }

  const timeout = opts?.timeoutMs ?? CC_TIMEOUT_MS;

  return new Promise((resolve) => {
    execFile(
      'claude',
      args,
      { timeout, maxBuffer: MAX_BUFFER },
      (error, stdout, stderr) => {
        if (error) {
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

          resolve({
            success: false,
            result: `Claude Code process error: ${error.message}`,
            rawOutput: stdout || stderr,
          });
          return;
        }

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
